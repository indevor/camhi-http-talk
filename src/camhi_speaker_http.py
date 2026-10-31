#!/usr/bin/env python3
"""
camhi_speaker_http.py — send audio to a CamHi / HiChip camera speaker over the
camera's own HTTP talk endpoint.  No P2P, no crypto, no login packet needed.

Why this is the better path
===========================
The camera runs a **Mongoose** web server on port 80.  `HI_Websvr_Start`
registers `HI_Media_Interface` as the media distributor, and the media thread
(`FUN_0046fd80`) routes by a substring of the request:

    "livestream/"      -> HI_Media_LiveStreamParseStream     (video)
    "livestream.cgi"   -> HI_Media_LiveStreamParseStreamEx   (video)
    "call_invite.cgi"  -> HI_Media_LiveStreamParseTalkEx     (TALK, our path)

`/call_invite.cgi` therefore opens a two-way audio session on the plain HTTP
port.  Everything is ordinary Basic auth — the P2P login problem disappears.

The protocol
------------
1) open the session (CGI query string, exactly as the camera parses it):

       GET /call_invite.cgi?chn=1&type=g711a& HTTP/1.1
       Host: <cam>
       Authorization: Basic <base64(user:pass)>
       Connection: keep-alive

   The camera derives the channel from  "chn=" .. "&"  and the codec from
   "type=" .. "&" (g711*/g726/aac), then starts RecvTalkDataThread.

2) the camera answers with its own session descriptor:

       HTTP/1.1 200 OK
       Server: Camera
       Content-Type: application/octet-stream
       Content-Length: 91

       Session-ID: 1942332
       AEnc-Format: g711a 64000 0 1
       Transport-Type: TCP RTP
       Audio-Port: 0                       <- 0 = stay on this connection

3) the camera then waits for a client "response" that carries the session id
   (`RecvTalkDataThread` only enters its audio loop when it sees
   `sessid=<Session-ID>` next to ` HTTP/1.1`, or the literal
   `Session-ID : <id>`):

       POST http://<cam>/call_invite.cgi?sessid=<Session-ID> HTTP/1.1
       Host: <cam>

4) audio frames, 20 bytes of header + payload (disassembled at 0x4776c8):

       [0:2]   ignored            (sent as 80 08)
       [2:4]   BE u16 = 16 + payload_len      <- the length the camera trusts
       [4:8]   ignored
       [8:12]  BE u32 timestamp               <- handed to the decoder
       [12:20] ignored
       [20:..] payload: G.711 A-law, 8 kHz mono

   The camera reads 16 bytes, takes `len` from [2:4], reads `len-12` more,
   then decodes the `len-16` payload bytes starting at offset 20.

Standard library only.
"""
from __future__ import annotations

import argparse
import base64
import math
import os
import re
import socket
import struct
import sys
import time
import wave

# --- credentials come from the environment, never from the source ---
CAM_DEFAULT = os.environ.get('CAMHI_CAMERA', '192.168.1.100')
USER_DEFAULT = os.environ.get('CAMHI_USER', 'admin')
PWD_DEFAULT = os.environ.get('CAMHI_PASSWORD', '')

RATE = 8000
PAYLOAD_BYTES = 160          # 20 ms of G.711 @ 8 kHz
SILENCE = 0xD5               # canonical A-law silence


# ------------------------------------------------------- timing / smoothing
def boost_timer() -> None:
    """Ask Windows for 1 ms timer resolution.

    The default granularity is ~15.6 ms, which makes 20 ms frame pacing very
    jittery; the camera then underruns and the audio crackles.
    """
    if os.name != 'nt':
        return
    try:
        import ctypes
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass


def release_timer() -> None:
    if os.name != 'nt':
        return
    try:
        import ctypes
        ctypes.windll.winmm.timeEndPeriod(1)
    except Exception:
        pass


# --------------------------------------------------------------- G.711 A-law
# Reference ITU-T G.711 A-law for 16-bit linear input.  The segment table must
# span the full 16-bit magnitude range; a 12-bit table (as in the original Sun
# listing for 14-bit input) saturates everything above ~4095 and destroys the
# audio.  Silence encodes to 0xD5, and encode/decode is round-trip stable.
_ALAW_SEG = (0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF, 0x3FFF, 0x7FFF)


def alaw_encode(pcm: int) -> int:
    """ITU-T G.711 A-law encode of one signed 16-bit sample (0 -> 0xd5)."""
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


def alaw_gain(alaw: bytes, volume: int) -> bytes:
    """Apply a software volume to A-law audio.

    100 = unchanged, 0 = silence, values above 100 amplify (up to 400).
    Scaling happens in the linear PCM domain, which is what makes the control
    feel natural rather than logarithmic; anything past full scale simply
    clips inside the A-law encoder.

    The camera's own `setaudiooutvolume` CGI reports success but does not
    actually change the level on this firmware, so loudness is handled here.
    """
    vol = max(0, min(400, int(volume)))
    if vol == 100:
        return alaw
    if vol == 0:
        return bytes([SILENCE]) * len(alaw)
    scale = vol / 100.0
    return bytes(alaw_encode(int(alaw_decode(b) * scale)) for b in alaw)


def tone_alaw(seconds: float, freq: float = 1000.0, amp: int = 14000,
              volume: int = 100) -> bytes:
    n = int(seconds * RATE)
    raw = bytes(alaw_encode(int(amp * math.sin(2 * math.pi * freq * i / RATE)))
                for i in range(n))
    return alaw_gain(raw, volume)


def wav_to_alaw(path: str, volume: int = 100) -> bytes:
    """Read a WAV and return G.711 A-law at 8 kHz mono (linear resample)."""
    if not os.path.isfile(path):
        raise SystemExit(
            f'Файл не найден: {path}\n'
            f'Укажите существующий WAV, например:\n'
            f'  python work\\camhi_speaker_http.py --tone 5\n'
            f'  python work\\camhi_speaker_http.py --wav C:\\sounds\\voice.wav\n'
            f'(16-бит PCM; 8 кГц моно идеально, другой формат будет пересэмплирован)')
    with wave.open(path, 'rb') as w:
        ch, sw, fr = w.getnchannels(), w.getsampwidth(), w.getframerate()
        data = w.readframes(w.getnframes())
    if sw != 2:
        raise SystemExit(f'{path}: need 16-bit PCM WAV (got {sw * 8}-bit)')
    samples = list(struct.unpack('<%dh' % (len(data) // 2), data))
    if ch == 2:
        samples = [(samples[i] + samples[i + 1]) // 2
                   for i in range(0, len(samples) - 1, 2)]
    if fr != RATE:
        ratio = fr / RATE
        n = int(len(samples) / ratio)
        samples = [samples[min(len(samples) - 1, int(i * ratio))] for i in range(n)]
    return bytes(alaw_encode(s) for s in samples)


# ------------------------------------------------------------------- framing
def build_frame(payload: bytes, timestamp: int) -> bytes:
    """20-byte header + payload, exactly as RecvTalkDataThread expects."""
    length = 16 + len(payload)
    return (b'\x80\x08' + struct.pack('>H', length) + b'\x00' * 4 +
            struct.pack('>I', timestamp & 0xFFFFFFFF) + b'\x00' * 8 + payload)


def _recv_http(s: socket.socket, timeout: float = 5.0) -> bytes:
    """Read an HTTP response: head plus exactly Content-Length body bytes."""
    s.settimeout(timeout)
    d = b''
    while b'\r\n\r\n' not in d:
        c = s.recv(4096)
        if not c:
            return d
        d += c
    head, sep, body = d.partition(b'\r\n\r\n')
    clen = 0
    for line in head.split(b'\r\n'):
        k, _, v = line.partition(b':')
        if k.strip().lower() == b'content-length':
            try:
                clen = int(v.strip())
            except ValueError:
                clen = 0
    while len(body) < clen:
        c = s.recv(4096)
        if not c:
            break
        body += c
    return head + sep + body


def _field(body: bytes, key: str):
    for line in body.split(b'\r\n'):
        k, sep, v = line.partition(b':')
        if sep and k.strip().lower() == key.lower().encode():
            return v.strip().decode('latin1')
    return None


# --------------------------------------------------------------------- talk
class CamHiSpeakerHTTP:
    """Context manager around one camera talk session."""

    def __init__(self, cam: str = CAM_DEFAULT, user: str = USER_DEFAULT,
                 pwd: str = PWD_DEFAULT, channel: int = 1, codec: str = 'g711a',
                 port: int = 80, verbose: bool = True):
        self.cam, self.user, self.pwd = cam, user, pwd
        self.channel, self.codec, self.port = channel, codec, port
        self.verbose = verbose
        self.sock: socket.socket | None = None
        self.session_id: str | None = None

    def _http_get(self, uri: str, auth: str) -> tuple[socket.socket, str, bytes]:
        s = socket.create_connection((self.cam, self.port), 6)
        s.sendall((f'GET {uri} HTTP/1.1\r\nHost: {self.cam}\r\n'
                   f'Authorization: Basic {auth}\r\n'
                   'Connection: keep-alive\r\n\r\n').encode())
        resp = _recv_http(s)
        head, _, body = resp.partition(b'\r\n\r\n')
        status = head.split(b'\r\n', 1)[0].decode('latin1') if head else '(no reply)'
        return s, status, body

    # -- speaker volume --------------------------------------------------
    def _cgi(self, cmd: str, timeout: float = 5.0) -> str:
        """Issue a param.cgi command and return the decoded body.

        The full body is read using Content-Length: a truncated read here would
        be dangerous, because `reset_volume()` writes back what it read.
        """
        auth = base64.b64encode(f'{self.user}:{self.pwd}'.encode()).decode()
        s = socket.create_connection((self.cam, self.port), 6)
        try:
            s.sendall((f'GET /cgi-bin/hi3510/param.cgi?cmd={cmd} HTTP/1.0\r\n'
                       f'Host: {self.cam}\r\n'
                       f'Authorization: Basic {auth}\r\n\r\n').encode())
            resp = _recv_http(s, timeout)
            return resp.partition(b'\r\n\r\n')[2].decode('latin1', 'replace')
        finally:
            s.close()

    def get_volume(self) -> int | None:
        """Current speaker volume (0..100), or None if it could not be read."""
        try:
            body = self._cgi('getaudiooutvolume')
            m = re.search(r'ao_volume\s*=\s*"?(\d{1,3})', body)
            if not m:
                return None
            return max(0, min(100, int(m.group(1))))
        except Exception:
            return None

    def set_volume(self, volume: int, force: bool = False) -> int | None:
        """Set the camera speaker volume (0..100).

        The value lives in the camera's JFFS2 config, so the write is skipped
        when it is already what we want (avoids pointless flash wear).
        `force=True` always writes -- useful to re-arm the output after a
        session left it muted.
        """
        vol = max(0, min(100, int(volume)))
        cur = self.get_volume()
        if cur == vol and not force:
            if self.verbose:
                print(f'  speaker volume already {vol}')
            return vol
        try:
            body = self._cgi(f'setaudiooutvolume&-ao_volume={vol}')
            ok = 'succeed' in body.lower() or 'ok' in body.lower()
            if self.verbose:
                print(f'  speaker volume -> {vol}'
                      + ('' if ok else f'  (camera said: {body.strip()[:60]!r})'))
            return vol
        except Exception as e:
            if self.verbose:
                print(f'  set volume failed: {e}')
            return None

    def reset_volume(self) -> bool:
        """Re-apply the current speaker volume.

        Every talk session ends with `HI_SDK_StopAudioDecode()`, which calls
        `IMP_AO_SetVolMute(...,1)` and `IMP_AO_PauseChn()`.  If an old session
        thread finishes *after* a new one has already unmuted the output, the
        speaker stays muted until something re-arms it; writing the volume back
        is a cheap way to do that.
        """
        cur = self.get_volume()
        if cur is None:
            if self.verbose:
                print('  could not read speaker volume')
            return False
        return self.set_volume(cur, force=True) is not None

    # -- session ---------------------------------------------------------
    def open(self, retries: int = 3) -> str:
        """Open a talk session. Retries while the camera is busy closing the
        previous one (it answers nothing until `HI_PthreadJoin` returns)."""
        auth = base64.b64encode(f'{self.user}:{self.pwd}'.encode()).decode()
        uri = f'/call_invite.cgi?chn={self.channel}&type={self.codec}&'
        last = '(no reply)'
        for attempt in range(1, retries + 1):
            try:
                s, status, body = self._http_get(uri, auth)
            except Exception as e:
                last = f'{type(e).__name__}: {e}'
                s = None
                status, body = '', b''
            if s is not None and ' 200 ' in status:
                sid = _field(body, 'Session-ID')
                if sid:
                    self.sock, self.session_id = s, sid
                    if self.verbose:
                        print(f'  {status}')
                        print(f'  Session-ID={sid}  '
                              f'{_field(body, "AEnc-Format")}  '
                              f'{_field(body, "Transport-Type")}')
                    s.sendall((f'POST http://{self.cam}/call_invite.cgi?sessid={sid} '
                               f'HTTP/1.1\r\nHost: {self.cam}\r\n\r\n').encode())
                    time.sleep(0.35)
                    return sid
                last = 'no Session-ID in reply'
            elif s is not None and self.verbose:
                print(f'  {status}')
            last = status if s is not None else last
            if s is not None:
                s.close()
            if attempt < retries:
                wait = 1.5 * attempt
                if self.verbose:
                    print(f'  camera busy ({last}); retry {attempt + 1}/{retries} '
                          f'in {wait:.1f}s')
                time.sleep(wait)
        raise RuntimeError(f'talk session refused: {last}')

    # -- audio -----------------------------------------------------------
    def send_alaw(self, alaw: bytes, payload_bytes: int = PAYLOAD_BYTES,
                  pace: bool = True, lead_ms: int = 400,
                  quiet: bool = True) -> int:
        """Stream A-law to the camera. Returns the number of bytes sent.

        `lead_ms` of audio is pushed out immediately to pre-fill the camera's
        jitter buffer; the rest follows at real-time pace.  Without that
        cushion any scheduling hiccup is audible as a click.
        """
        if self.sock is None:
            raise RuntimeError('session not open')
        pad = (-len(alaw)) % payload_bytes
        alaw = alaw + bytes([SILENCE]) * pad
        total = len(alaw)
        lead = min(total, int(lead_ms * RATE / 1000) // payload_bytes * payload_bytes)

        boost_timer()
        try:
            t0 = time.perf_counter()
            sent = 0
            for off in range(0, total, payload_bytes):
                chunk = alaw[off:off + payload_bytes]
                self.sock.sendall(build_frame(chunk, off))
                sent += len(chunk)
                if pace and sent > lead:
                    # paced only after the pre-buffer is exhausted
                    target = t0 + (sent - lead) / RATE
                    while True:
                        dt = target - time.perf_counter()
                        if dt <= 0:
                            break
                        time.sleep(dt if dt > 0.002 else 0)
            return sent
        finally:
            release_timer()


    def close(self, settle: float = 1.2):
        """Half-close, then wait.

        `shutdown(SHUT_WR)` makes the camera's `recv()` return 0 immediately, so
        `RecvTalkDataThread` runs its cleanup (StopAudioDecode -> flush/mute/
        pause) right away instead of blocking until its 10 s timeout.  Waiting
        here keeps the next session from racing that cleanup.
        """
        if self.sock is None:
            return
        try:
            self.sock.shutdown(socket.SHUT_WR)
        except Exception:
            pass
        if settle > 0:
            time.sleep(settle)
        try:
            self.sock.close()
        except Exception:
            pass
        self.sock = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def say(cam: str = CAM_DEFAULT, wav: str | None = None, alaw: bytes | None = None,
        user: str = USER_DEFAULT, pwd: str = PWD_DEFAULT,
        channel: int = 1, codec: str = 'g711a', verbose: bool = True,
        frame_ms: int = 20, lead_ms: int = 400,
        volume: int = 100, reset_ao: bool = False,
        retries: int = 3) -> bool:
    """Play a WAV file (or raw A-law) on the camera speaker. True on success.

    `volume` (0..100) is a **software** gain applied to the audio before it is
    encoded to A-law -- the camera's configuration is never written.  (The
    camera's own `setaudiooutvolume` CGI reports success but does not actually
    change the level on this firmware.)

    `reset_ao` re-writes the *current* camera volume, which is what re-arms the
    output after a session left it muted.
    """
    if alaw is None:
        if wav is None:
            raise ValueError('need wav or alaw')
        alaw = wav_to_alaw(wav)
    if volume != 100:
        alaw = alaw_gain(alaw, volume)
    payload_bytes = max(160, int(frame_ms * RATE / 1000))
    sp = CamHiSpeakerHTTP(cam, user, pwd, channel, codec, verbose=verbose)
    try:
        if reset_ao:
            sp.reset_volume()
        sp.open(retries=retries)
        n = sp.send_alaw(alaw, payload_bytes=payload_bytes, lead_ms=lead_ms)
        if verbose:
            print(f'  sent {n} bytes ({n / RATE:.1f} s), '
                  f'{payload_bytes} B/frame, lead {lead_ms} ms, gain {volume}%')
        time.sleep(0.3)
        return True
    except Exception as e:
        if verbose:
            print(f'  FAILED: {e}')
        return False
    finally:
        sp.close()


# ---------------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(
        description='Send audio to a CamHi / HiChip camera speaker over HTTP')
    ap.add_argument('--cam', default=CAM_DEFAULT, help='camera IP')
    ap.add_argument('--user', default=USER_DEFAULT)
    ap.add_argument('--pwd', default=PWD_DEFAULT,
                    help='camera password (or set CAMHI_PASSWORD)')
    ap.add_argument('--channel', type=int, default=1)
    ap.add_argument('--codec', default='g711a',
                    help='type= value for the session: g711a/g711u, g726, '
                         'aac.  NOTE: the camera only ever decodes G.711a, so '
                         'the payload must stay A-law whatever is negotiated.')
    ap.add_argument('--wav', help='16-bit PCM WAV to play')
    ap.add_argument('--tone', type=float, help='play a test tone for N seconds')
    ap.add_argument('--freq', type=float, default=1000.0, help='tone frequency')
    ap.add_argument('--frame-ms', type=int, default=20,
                    help='audio bytes per frame in ms: 20 (160 B) or 100 (800 B)')
    ap.add_argument('--lead-ms', type=int, default=400,
                    help='pre-buffer pushed out before real-time pacing')
    ap.add_argument('--reset-ao', action='store_true',
                    help='re-arm the speaker (rewrites the CURRENT volume, '
                         'never changes it) - cures a stuck mute')
    ap.add_argument('--volume', type=int, default=100, metavar='0-400',
                    help='software playback gain: 100 = unchanged (default), '
                         '0 = silence, >100 amplifies; the camera '
                         'configuration is never touched')
    ap.add_argument('--get-volume', action='store_true',
                    help='print the camera-reported speaker volume and exit')
    ap.add_argument('--retries', type=int, default=3,
                    help='how many times to retry while the camera is busy')
    a = ap.parse_args()

    if a.get_volume:
        sp = CamHiSpeakerHTTP(a.cam, a.user, a.pwd)
        v = sp.get_volume()
        print(f'camera speaker volume: {v}'
              if v is not None else 'could not read volume')
        return 0 if v is not None else 1

    if a.wav:
        alaw = wav_to_alaw(a.wav)
        print(f'{a.wav}: {len(alaw)} A-law bytes ({len(alaw) / RATE:.1f} s)')
    elif a.tone:
        alaw = tone_alaw(a.tone, a.freq)
        print(f'tone {a.tone}s @ {a.freq} Hz')
    else:
        ap.error('give --wav or --tone (or --get-volume)')

    ok = say(a.cam, alaw=alaw, user=a.user, pwd=a.pwd, channel=a.channel,
             codec=a.codec, frame_ms=a.frame_ms, lead_ms=a.lead_ms,
             volume=a.volume, reset_ao=a.reset_ao, retries=a.retries)
    print('OK' if ok else 'FAILED')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
