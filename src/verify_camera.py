#!/usr/bin/env python3
"""
verify_camera.py — fingerprint a CamHi / HiChip camera and test which talk
paths work on it.  Safe: the talk test opens a session, sends a short tone and
closes; nothing is written to the camera.

    python work\\verify_camera.py --cam 192.168.1.129 --user admin --pwd <pass>
    python work\\verify_camera.py --cam 192.168.1.100 --tone 0     # no audio

Reports:
  * reachability + model / firmware / UID
  * HTTP /call_invite.cgi negotiation for g711a / g726 / aac
  * P2P LAN discovery, and whether the login captured on camera #1 is accepted
"""
from __future__ import annotations

import argparse
import base64
import socket
import subprocess
import sys
import time

HERE = r'C:\camhi\work'
sys.path.insert(0, HERE)
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from camhi_speaker_http import (CamHiSpeakerHTTP, _recv_http, _field,
                                tone_alaw, say)          # noqa: E402

CGIS = [
    ('getserverinfo', 'model / versions'),
    ('getdevbaseinfo', 'device base info'),
    ('getuid', 'UID'),
    ('getpppoestatus', 'noop'),
    ('getnetworkinfo', 'network'),
]


def ping(cam: str) -> bool:
    try:
        r = subprocess.run(['ping', '-n', '2', '-w', '1000', cam],
                           capture_output=True, text=True, timeout=20)
        return 'TTL=' in (r.stdout or '')
    except Exception:
        return False


def http_get(cam: str, user: str, pwd: str, cmd: str, timeout=5.0):
    auth = base64.b64encode(f'{user}:{pwd}'.encode()).decode()
    try:
        s = socket.create_connection((cam, 80), 5)
    except Exception as e:
        return None, f'{type(e).__name__}'
    try:
        s.sendall((f'GET /cgi-bin/hi3510/param.cgi?cmd={cmd} HTTP/1.0\r\n'
                   f'Host: {cam}\r\nAuthorization: Basic {auth}\r\n\r\n').encode())
        resp = _recv_http(s, timeout)
        head, _, body = resp.partition(b'\r\n\r\n')
        status = head.split(b'\r\n', 1)[0].decode('latin1') if head else '(no reply)'
        return status, body.decode('latin1', 'replace').strip()
    except Exception as e:
        return None, f'{type(e).__name__}: {e}'
    finally:
        s.close()


def negotiate(cam: str, user: str, pwd: str, codec: str, retries: int = 3):
    """Open a session for `codec`. Retries: the camera replies nothing while it
    is still joining the previous talk thread."""
    auth = base64.b64encode(f'{user}:{pwd}'.encode()).decode()
    last = '(no reply)'
    for attempt in range(retries):
        s = None
        try:
            s = socket.create_connection((cam, 80), 6)
            s.sendall((f'GET /call_invite.cgi?chn=1&type={codec}& HTTP/1.1\r\n'
                       f'Host: {cam}\r\nAuthorization: Basic {auth}\r\n'
                       'Connection: keep-alive\r\n\r\n').encode())
            resp = _recv_http(s, 4.0)
            head, _, body = resp.partition(b'\r\n\r\n')
            status = head.split(b'\r\n', 1)[0].decode('latin1') if head else '(no reply)'
            if '200' in status:
                return status, _field(body, 'AEnc-Format') or ''
            last = status
        except Exception as e:
            last = f'ERROR {type(e).__name__}'
        finally:
            if s:
                s.close()
        time.sleep(1.5 * (attempt + 1))
    return last, ''


def p2p_discover(cam: str) -> str:
    """LAN_SEARCH + read the camera's P2P port (no login attempted)."""
    import struct
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.settimeout(0.4)
        pkt = struct.pack('>HH', 0xF130, 4) + b'\x00\x00'
        s.sendto(pkt, ('255.255.255.255', 32108))
        t0 = time.time()
        port = None
        while time.time() - t0 < 3:
            try:
                data, addr = s.recvfrom(2048)
            except socket.timeout:
                continue
            if addr[0] != cam or len(data) < 4:
                continue
            if struct.unpack('>H', data[:2])[0] == 0xF141:
                port = addr[1]
                break
        s.close()
        return f'P2P port {port}' if port else 'no f141 reply'
    except Exception as e:
        return f'error {type(e).__name__}'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cam', required=True, help='IP камеры, напр. 192.168.1.129')
    ap.add_argument('--uid', help='UID камеры (напр. <YOUR-UID>)')
    ap.add_argument('--user', default='admin')
    ap.add_argument('--pwd', default=PWD_DEFAULT,
                    help='пароль админки/RTSP; он же используется и в P2P-логине')
    ap.add_argument('--tone', type=float, default=2.0,
                    help='seconds of test tone (0 = negotiation only)')
    a = ap.parse_args()

    print('=' * 78)
    print(f'CAMERA {a.cam}   user={a.user}   uid={a.uid or "(не указан)"}')
    print('=' * 78)

    print(f'\n[1] reachability : {"OK" if ping(a.cam) else "NO PING"}')
    if not ping(a.cam):
        print('    камера недоступна — проверьте адрес и питание')
        return 1

    print('\n[2] fingerprint via HTTP')
    authed = None
    for cmd, note in CGIS:
        status, body = http_get(a.cam, a.user, a.pwd, cmd)
        if status and '200' in status:
            authed = True
            one = ' '.join(body.split())
            print(f'    {cmd:<16} {one[:88]}')
        elif status and '401' in status:
            authed = False
            print(f'    {cmd:<16} 401 Unauthorized   <-- неверный логин/пароль')
            break
        else:
            print(f'    {cmd:<16} {status}')

    print('\n[3] HTTP talk negotiation (/call_invite.cgi)')
    for codec in ('g711a', 'g726', 'aac'):
        status, fmt = negotiate(a.cam, a.user, a.pwd, codec)
        print(f'    type={codec:<6} {status:<16} {fmt}')

    print('\n[4] P2P discovery (UDP LAN_SEARCH)')
    print(f'    {p2p_discover(a.cam)}')

    if a.tone > 0:
        print(f'\n[5] playback test — {a.tone:.0f}s tone on the speaker')
        ok = say(a.cam, alaw=tone_alaw(a.tone, 1000.0),
                 user=a.user, pwd=a.pwd, verbose=True)
        print(f'    {"sent" if ok else "FAILED"}')

    print('\n[6] P2P login with the packet captured on camera #1')
    try:
        sys.argv = ['camhi_speaker', '--cam', a.cam, '--tone', '0.1', '--quiet']
        import io
        import contextlib
        buf = io.StringIO()
        import camhi_speaker as cs
        c = cs.CamHiSpeaker(a.cam, verbose=True)
        with contextlib.redirect_stdout(buf):
            opened = c.open()
            st = c.login() if opened else None
            c.close()
        print(f'    session opened: {opened}')
        print(f'    login status  : {st}')
        if opened and st != 0:
            print('    -> ожидаемо: пакет логина привязан к UID первой камеры')
    except Exception as e:
        print(f'    P2P test failed: {type(e).__name__}: {e}')

    print('\n' + '=' * 78)
    return 0


if __name__ == '__main__':
    sys.exit(main())
