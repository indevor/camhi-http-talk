#!/usr/bin/env python3
"""
camhi_speaker.py — send audio to a CamHi / HiChip (Ingenic T31) camera speaker.

Fully standalone: the login packet and the session-setup frames are embedded
below, so no pcap is needed at runtime.

Why a replayed login works
==========================
The camera requires opcode 4096 (HI_P2P_LOGIN) as the first command of the
session.  The device-side handler `HI_P2P_Platform_UserPasswd_Check_Encrypt`
(libxqun.so @0x22744) derives its AES key as

    key = MD5(sprintf("%d%s%s%s", handleField, PASSWORD, UID, USERNAME))[:16]

where `handleField` is read from the REQUEST HEADER (bytes [28:32] of the DRW
frame), not from any local state.  It then verifies

    sum(AES_decrypt(body)) & 0xFFFF  ==  header bytes [25:27] (LE)

also transmitted.  Nothing in that chain depends on wall-clock time or on the
session, so a captured login packet stays valid for the same camera for as long
as its P2P credentials are unchanged.  We therefore replay it verbatim instead
of reimplementing the crypto.

Protocol summary (byte-verified against captures/phase_talk.pcap)
================================================================
DRW command frame (0xf1d0), 24-byte header then body::

    [0:2]   f1 d0
    [2:4]   total_len - 4                (BE u16)
    [4]     d1
    [5:7]   stream                       0x0000 = control, 0x0100 = audio
    [7]     DRW sequence                 (must be contiguous per stream)
    [8:12]  99 99 99 99
    [12:16] body length                  (LE u32)
    [16:20] opcode                       (LE u32)
    [20:24] 00 00 00 00
    [24:28] login only: 0x0a, checksum(LE u16), 0x00
    [28:32] login only: handleField      (LE u32)
    [32:..] body

DRW selective acknowledgement (0xf1d1)::

    d1 | stream (BE u16) | count (u8) | count x u16 BE sequence numbers

Audio frame (0xf1d0, stream 0x0100), 824 bytes::

    d1 0100 <seq> "HXAF" <800 LE u32> 00000000 00000000 <800 bytes G.711 A-law>

800 bytes of A-law = 100 ms at 8 kHz mono.  Silence is 0xd5.

OpCodes: 4096 LOGIN, 4097 LIVE_REQ, 4113 START_TALK, 4114 STOP_TALK.

Usage
=====
    python camhi_speaker_p2p.py --wav message.wav
    python camhi_speaker_p2p.py --tone 5 --freq 1000
    python camhi_speaker_p2p.py --wav tts.mp3.wav --cam 192.168.1.100

Requires only the Python standard library.
"""
from __future__ import annotations

import argparse
import math
import os
import socket
import struct
import sys
import threading
import time
import wave

# ======================================================================
# Captured frames (see the module docstring for why these are literals)
# ======================================================================
# ---------------------------------------------------------------------------
# The P2P login packet is bound to ONE camera: the AES key is
#     MD5(handleField + password + UID + username)
# where the UID is the camera's own value and never appears in the packet.
# Supply frames captured from YOUR camera -- see tools/extract_p2p_frames.py
# and docs/P2P.md.  (The HTTP path needs none of this.)
# ---------------------------------------------------------------------------
try:
    from p2p_frames import (CAPTURED_CONTROL, CAPTURED_LOGIN,
                            CAPTURED_START_TALK, CAPTURED_STOP_TALK)
except ImportError:  # pragma: no cover
    raise SystemExit(
        "P2P talk needs frames captured from YOUR OWN camera.\n"
        "\n"
        "  1) capture traffic while pressing the microphone button in the\n"
        "     CamHi Pro app (filter: udp and host <camera-ip>)\n"
        "  2) python tools/extract_p2p_frames.py talk.pcap > p2p_frames.py\n"
        "  3) put p2p_frames.py next to this script\n"
        "\n"
        "The HTTP path (camhi_speaker_http.py) needs none of this.")

CAM_DEFAULT = os.environ.get('CAMHI_CAMERA', '192.168.1.100')
LAN_PORT = 32108
UID_BLOB = bytes.fromhex('41414b4b00000000000174ff4442464242000000')

F_LAN_SEARCH = 0xF130
F_P2P_RDY = 0xF142
F_DRW = 0xF1D0
F_DRW_ACK = 0xF1D1
F_ALIVE = 0xF1E0
F_ALIVE_ACK = 0xF1E1
F_CLOSE = 0xF1F0

OP_LOGIN = 4096
OP_LIVE = 4097
OP_START_TALK = 4113
OP_STOP_TALK = 4114


def u16be(v: int) -> bytes:
    return struct.pack('>H', v)


def msg(mtype: int, body: bytes = b'') -> bytes:
    return u16be(mtype) + u16be(len(body)) + body


def opcode_of(pkt: bytes) -> int:
    return struct.unpack_from('<I', pkt, 16)[0]


# ======================================================================
# G.711 A-law
# ======================================================================
# Reference ITU-T G.711 A-law for 16-bit linear input.  The segment table has to
# cover the full 16-bit magnitude range; a 12-bit table (as in the original Sun
# listing written for 14-bit input) saturates everything above ~4095.
_ALAW_SEG = (0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF, 0x3FFF, 0x7FFF)


def alaw_encode(pcm: int) -> int:
    """ITU-T G.711 A-law encode one signed 16-bit sample (0 -> 0xd5)."""
    if pcm >= 0:
        mask = 0xD5
    else:
        mask = 0x55
        pcm = -pcm - 8
    seg = 0
    while seg < 8 and pcm > _ALAW_SEG[seg]:
        seg += 1
    if seg >= 8:
        return 0x7F ^ mask
    aval = seg << 4
    if seg < 2:
        aval |= (pcm >> 4) & 0x0F
    else:
        aval |= (pcm >> (seg + 3)) & 0x0F
    return aval ^ mask


def alaw_decode(byte: int) -> int:
    """ITU-T G.711 A-law decode of one byte to a signed 16-bit sample."""
    a = byte ^ 0x55
    t = (a & 0x0F) << 4
    seg = (a & 0x70) >> 4
    if seg == 0:
        t += 8
    elif seg == 1:
        t += 0x108
    else:
        t += 0x108
        t <<= seg - 1
    return t if (a & 0x80) else -t


def tone_alaw(seconds: float, freq: float = 880.0, rate: int = 8000,
              amp: int = 14000, volume: int = 100) -> bytes:
    n = int(seconds * rate)
    raw = bytes(alaw_encode(int(amp * math.sin(2 * math.pi * freq * i / rate)))
                for i in range(n))
    return alaw_gain(raw, volume)


def alaw_gain(alaw: bytes, volume: int) -> bytes:
    """Software volume: 100 = unchanged, 0 = silence, >100 amplifies (max 400).

    The camera's own `setaudiooutvolume` CGI reports success but does not change
    the level on this firmware, so loudness is applied here before sending.
    """
    vol = max(0, min(400, int(volume)))
    if vol == 100:
        return alaw
    if vol == 0:
        return bytes([0xD5]) * len(alaw)
    scale = vol / 100.0
    return bytes(alaw_encode(int(alaw_decode(b) * scale)) for b in alaw)


def wav_to_alaw(path: str, rate: int = 8000, volume: int = 100) -> bytes:
    """Read a WAV and return G.711 A-law at `rate` (linear resample if needed)."""
    with wave.open(path, 'rb') as w:
        ch, sw, fr = w.getnchannels(), w.getsampwidth(), w.getframerate()
        data = w.readframes(w.getnframes())
    if sw != 2:
        raise SystemExit(f'{path}: need 16-bit PCM WAV (got {sw*8}-bit)')
    samples = list(struct.unpack('<%dh' % (len(data) // 2), data))
    if ch == 2:
        samples = [(samples[i] + samples[i + 1]) // 2
                   for i in range(0, len(samples) - 1, 2)]
    if fr != rate:
        ratio = fr / rate
        n = int(len(samples) / ratio)
        samples = [samples[min(len(samples) - 1, int(i * ratio))] for i in range(n)]
    return alaw_gain(bytes(alaw_encode(s) for s in samples), volume)


# ======================================================================
# P2P client
# ======================================================================
class CamHiSpeaker:
    def __init__(self, cam: str = CAM_DEFAULT, verbose: bool = True):
        self.cam = cam
        self.verbose = verbose
        self.s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.s.bind(('0.0.0.0', 0))
        self.s.settimeout(0.05)
        self.local_port = self.s.getsockname()[1]
        self.cam_port = None
        self.rx_log = []
        self.acked = {}
        self.rx_drw = {}
        self.tx_ack = {}
        self.login_status = None
        self.tx_seq = 0
        self.lock = threading.Lock()
        self._stop = False
        self._ack_seqs = {}
        self._t = threading.Thread(target=self._rx_loop, daemon=True)
        self._t.start()

    # ------------------------------------------------------------- RX
    def _rx_loop(self):
        while not self._stop:
            try:
                d, a = self.s.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                return
            except Exception:
                continue
            self._handle(d, a)

    def _handle(self, d: bytes, a):
        if len(d) < 2:
            return
        mt = struct.unpack_from('>H', d, 0)[0]
        self.rx_log.append((time.time(), mt, d, a))
        if mt == F_ALIVE:
            self.s.sendto(msg(F_ALIVE_ACK), a)
            return
        if mt == F_DRW:
            self._drw_ack(a, d)
            if len(d) >= 24 and d[8:12] == b'\x99\x99\x99\x99':
                op = struct.unpack_from('<I', d, 16)[0]
                status = struct.unpack_from('<I', d, 20)[0]
                if op == OP_LOGIN:
                    self.login_status = status
                if self.verbose:
                    print(f'  [RX] op={op} status={status:#010x}')
            return
        if mt == F_DRW_ACK and len(d) >= 8:
            stream = struct.unpack_from('>H', d, 5)[0]
            cnt = d[7]
            seqs = [struct.unpack_from('>H', d, 8 + 2 * i)[0]
                    for i in range(cnt) if 8 + 2 * i + 2 <= len(d)]
            self.acked.setdefault(stream, set()).update(seqs)
            return

    def _drw_ack(self, a, d: bytes):
        stream = struct.unpack_from('>H', d, 5)[0]
        seq = d[7]
        self.rx_drw[stream] = self.rx_drw.get(stream, 0) + 1
        with self.lock:
            lst = self._ack_seqs.setdefault(stream, [])
            lst.append(seq)
            lst[:] = lst[-16:]
            body = b'\xd1' + u16be(stream) + bytes([len(lst)])
            body += b''.join(u16be(x) for x in lst)
        self.tx_ack[stream] = self.tx_ack.get(stream, 0) + 1
        self.s.sendto(msg(F_DRW_ACK, body), a)

    # -------------------------------------------------------- session
    def open(self, tries: int = 14) -> bool:
        """LAN discovery + hole-punch ready + wait for ALIVE."""
        start = len(self.rx_log)
        for p in (LAN_PORT, 32100, 32109):
            self.s.sendto(msg(F_LAN_SEARCH), (self.cam, p))
            time.sleep(0.35)
            for rec in self.rx_log[start:]:
                if rec[3][1] != p:
                    self.cam_port = rec[3][1]
            start = len(self.rx_log)
        if self.cam_port is None:
            self.cam_port = LAN_PORT
        if self.verbose:
            print(f'  camera P2P port = {self.cam_port}')
        for _ in range(tries):
            self.s.sendto(msg(F_P2P_RDY, UID_BLOB), (self.cam, self.cam_port))
            if any(r[1] == F_ALIVE for r in self.rx_log):
                for _ in range(3):
                    self.s.sendto(msg(F_P2P_RDY, UID_BLOB), (self.cam, self.cam_port))
                    time.sleep(0.08)
                return True
            time.sleep(0.12)
        return any(r[1] == F_ALIVE for r in self.rx_log)

    def _send_ctrl(self, pkt: bytes):
        """Send a captured control frame with our own contiguous DRW sequence."""
        pkt = bytearray(pkt)
        pkt[7] = self.tx_seq & 0xFF
        self.tx_seq = (self.tx_seq + 1) & 0xFF
        self.s.sendto(bytes(pkt), (self.cam, self.cam_port))

    def login(self, timeout: float = 3.0):
        login_pkt = bytearray(bytes.fromhex(CAPTURED_LOGIN))
        self.login_status = None
        login_pkt[7] = 0
        self.tx_seq = 1
        self.s.sendto(bytes(login_pkt), (self.cam, self.cam_port))
        end = time.time() + timeout
        while time.time() < end:
            if self.login_status is not None:
                return self.login_status
            time.sleep(0.02)
        return None

    def setup_talk(self, start_video: bool = True):
        """Replay the captured post-login setup, ending with START_TALK."""
        for h in CAPTURED_CONTROL[1:]:
            pkt = bytes.fromhex(h)
            op = opcode_of(pkt)
            if not start_video and op in (OP_LIVE, 61442):
                continue
            self._send_ctrl(pkt)
            time.sleep(0.08)
            if op == OP_START_TALK:
                break
        time.sleep(0.25)

    def keepalive(self):
        self.s.sendto(msg(F_ALIVE), (self.cam, self.cam_port))

    def play_alaw(self, alaw: bytes, pace: float = 0.100) -> int:
        """Stream A-law as 800-byte (100 ms) frames.  Returns frames sent."""
        pad = (-len(alaw)) % 800
        alaw = alaw + b'\xd5' * pad
        n = len(alaw) // 800
        t0 = time.time()
        for i in range(n):
            chunk = alaw[i * 800:(i + 1) * 800]
            # audio frame: d1 | stream | seq | HXAF | len | 8 zeros | payload
            head = (b'\xd1' + u16be(0x0100) + bytes([i & 0xFF]) +
                    b'HXAF' + struct.pack('<I', len(chunk)) + b'\x00' * 8)
            self.s.sendto(msg(F_DRW, head + chunk), (self.cam, self.cam_port))
            if i % 25 == 0:
                self.keepalive()
            dt = t0 + (i + 1) * pace - time.time()
            if dt > 0:
                time.sleep(dt)
        return n

    def stop_talk(self):
        pkt = CAPTURED_STOP_TALK
        if isinstance(pkt, str):
            pkt = bytes.fromhex(pkt)
        for _ in range(2):
            self._send_ctrl(pkt)
            time.sleep(0.15)

    def close(self):
        try:
            self.s.sendto(msg(F_CLOSE), (self.cam, self.cam_port))
        except Exception:
            pass
        self._stop = True
        time.sleep(0.1)
        try:
            self.s.close()
        except Exception:
            pass


# ======================================================================
def say(cam: str = CAM_DEFAULT, wav: str | None = None,
        alaw: bytes | None = None, start_video: bool = True,
        verbose: bool = True, attempts: int = 3, volume: int = 100) -> bool:
    """Play a WAV file (or raw A-law) on the camera speaker.

    `volume` is a software gain (100 = unchanged, 0 = silence, >100 amplifies);
    the camera's configuration is never written.

    Returns True when the login was accepted and the audio was streamed.
    The camera's P2P port changes per session and a login can be missed on a
    busy camera, so the whole session is retried a few times.
    """
    if alaw is None:
        if wav is None:
            raise ValueError('need wav or alaw')
        alaw = wav_to_alaw(wav)
    if volume != 100:
        alaw = alaw_gain(alaw, volume)
    for attempt in range(1, attempts + 1):
        c = CamHiSpeaker(cam, verbose=verbose)
        try:
            if not c.open():
                if verbose:
                    print(f'  [{attempt}/{attempts}] P2P session did not open')
                time.sleep(0.8)
                continue
            st = c.login()
            if st != 0:
                if verbose:
                    print(f'  [{attempt}/{attempts}] login not accepted '
                          f'(status {st}) - retrying')
                time.sleep(1.0)
                continue
            c.setup_talk(start_video=start_video)
            n = c.play_alaw(alaw)
            acked = len(c.acked.get(0x0100, set()))
            if verbose:
                print(f'    sent {n} frames, camera acked {acked}/{n}')
            c.stop_talk()
            return True
        finally:
            c.close()
    if verbose:
        print(f'!! login failed after {attempts} attempts')
    return False


def main():
    ap = argparse.ArgumentParser(
        description='Send audio to a CamHi / HiChip camera speaker over P2P')
    ap.add_argument('--cam', default=CAM_DEFAULT, help='camera IP')
    ap.add_argument('--wav', help='16-bit PCM WAV to play')
    ap.add_argument('--tone', type=float, help='play a test tone for N seconds')
    ap.add_argument('--freq', type=float, default=880.0, help='test tone frequency')
    ap.add_argument('--skip-video', action='store_true',
                    help='do not start the live video stream (saves bandwidth)')
    ap.add_argument('--volume', type=int, default=100, metavar='0-400',
                    help='software playback gain: 100 = unchanged (default), '
                         '0 = silence, >100 amplifies')
    ap.add_argument('--quiet', action='store_true')
    a = ap.parse_args()

    if a.wav:
        alaw = wav_to_alaw(a.wav)
        print(f'{a.wav}: {len(alaw)} A-law bytes ({len(alaw)/8000:.1f} s)')
    elif a.tone:
        alaw = tone_alaw(a.tone, a.freq)
        print(f'tone {a.tone}s @ {a.freq} Hz: {len(alaw)} A-law bytes')
    else:
        ap.error('give --wav or --tone')

    ok = say(a.cam, alaw=alaw, start_video=not a.skip_video,
             verbose=not a.quiet, volume=a.volume)
    print('OK' if ok else 'FAILED')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
