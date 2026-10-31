#!/usr/bin/env python3
"""
extract_p2p_frames.py — turn your own capture into `p2p_frames.py`.

The P2P talk client needs the control/login frames that the CamHi Pro app sends
to YOUR camera.  Capture them once, then this tool pulls them out of the pcap.

How to capture
--------------
1. Start Wireshark (or tcpdump) on the machine that will send the audio.
2. Filter: `udp and host <camera-ip>`
3. In the CamHi Pro app, open the camera's live view and **press the microphone
   button**, say something, release.
4. Stop the capture and save it, e.g. `talk.pcap`.

Usage
-----
    python tools/extract_p2p_frames.py talk.pcap > p2p_frames.py
    # then place p2p_frames.py next to src/camhi_speaker_p2p.py

Only standard library.  Supports classic libpcap and pcapng files with
Ethernet + IPv4 + UDP.
"""
from __future__ import annotations

import argparse
import struct
import sys

MAGIC_LE = 0xA1B2C3D4
MAGIC_BE = 0xD4C3B2A1
MAGIC_NG = 0x0A0D0D0A

DRW = 0xF1D0


# ----------------------------------------------------------------- pcap input
def read_classic(data: bytes, endian: str):
    _, _, _, _, _, _, linktype = struct.unpack(endian + 'IHHiIII', data[:24])
    off = 24
    while off + 16 <= len(data):
        _, _, incl, _ = struct.unpack(endian + 'IIII', data[off:off + 16])
        off += 16
        yield linktype, data[off:off + incl]
        off += incl


def read_pcapng(data: bytes):
    off = 0
    linktype = 1
    while off + 12 <= len(data):
        btype, blen = struct.unpack('<II', data[off:off + 8])
        if blen < 12 or off + blen > len(data):
            break
        body = data[off + 8:off + blen - 4]
        if btype == 1 and len(body) >= 8:                 # Interface Description
            linktype = struct.unpack('<H', body[:2])[0]
        elif btype == 6:                                  # Enhanced Packet
            caplen = struct.unpack('<I', body[12:16])[0]
            yield linktype, body[20:20 + caplen]
        off += blen


def packets(path: str):
    data = open(path, 'rb').read()
    if len(data) < 4:
        return
    magic = struct.unpack('<I', data[:4])[0]
    if magic == MAGIC_NG:
        yield from read_pcapng(data)
    elif magic == MAGIC_LE:
        yield from read_classic(data, '<')
    elif magic == MAGIC_BE:
        yield from read_classic(data, '>')
    else:
        raise SystemExit(f'{path}: not a pcap/pcapng file')


# --------------------------------------------------------------- packet parse
def udp_payload(linktype: int, frame: bytes):
    """Return (src_ip, sport, dst_ip, dport, payload) or None."""
    if linktype == 1:                                     # Ethernet
        if len(frame) < 14:
            return None
        ethertype = struct.unpack('>H', frame[12:14])[0]
        off = 14
        while ethertype == 0x8100 and len(frame) >= off + 4:   # VLAN
            ethertype = struct.unpack('>H', frame[off + 2:off + 4])[0]
            off += 4
        if ethertype != 0x0800:
            return None
    elif linktype in (101, 228):                          # RAW / LINKTYPE_IPV4
        off = 0
    else:
        return None
    if len(frame) < off + 20:
        return None
    ip = frame[off:]
    ihl = (ip[0] & 0x0F) * 4
    if ip[9] != 17:                                       # UDP
        return None
    src = '.'.join(str(b) for b in ip[12:16])
    dst = '.'.join(str(b) for b in ip[16:20])
    udp = ip[ihl:]
    if len(udp) < 8:
        return None
    sport, dport, ulen = struct.unpack('>HHH', udp[:6])
    return src, sport, dst, dport, udp[8:max(8, ulen)]


# -------------------------------------------------------------------- extract
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('pcap', help='capture containing the app talking to your camera')
    ap.add_argument('--camera', help='camera IP (default: auto-detect the sender of f141)')
    a = ap.parse_args()

    frames = []
    seen = set()
    for linktype, raw in packets(a.pcap):
        p = udp_payload(linktype, raw)
        if not p:
            continue
        src, sport, dst, dport, pl = p
        if a.camera and a.camera not in (src, dst):
            continue
        off = pl.find(b'\xf1\xd0')
        if off < 0:
            continue
        f = pl[off:]
        if not f or f in seen:
            continue
        seen.add(f)
        frames.append(f)

    if not frames:
        raise SystemExit('no 0xf1d0 frames found — check the capture and the filter')

    # the login is the longest 0x1000 frame; start/stop talk are small opcodes
    login = b''
    start = b''
    stop = b''
    for f in frames:
        if len(f) < 24:
            continue
        op = struct.unpack('<I', f[16:20])[0]
        if op == 4096 and len(f) > len(login):
            login = f
        elif op == 4113 and not start:
            start = f
        elif op == 4114 and not stop:
            stop = f

    def lit(b: bytes) -> str:
        return "b'" + ''.join('\\x%02x' % c for c in b) + "'"

    out = sys.stdout
    out.write('"""P2P frames extracted from %s — generated, do not commit."""\n\n' % a.pcap)
    out.write('# ordered phone -> camera DRW frames\n')
    out.write('CAPTURED_CONTROL = [\n')
    for f in frames:
        out.write('    %r,\n' % f.hex())
    out.write(']\n\n')
    out.write('CAPTURED_LOGIN = %r\n' % login.hex())
    out.write('CAPTURED_START_TALK = %s\n' % lit(start))
    out.write('CAPTURED_STOP_TALK = %s\n' % lit(stop))
    out.write('\nif __name__ == "__main__":\n'
              '    print(len(CAPTURED_CONTROL), "control frames,",\n'
              '          len(CAPTURED_LOGIN) // 2, "login bytes")\n')
    print(f'extracted {len(frames)} frames, login {len(login)} bytes',
          file=sys.stderr)


if __name__ == '__main__':
    main()
