#!/usr/bin/env python3
"""Probe which codecs /call_invite.cgi actually negotiates.

Handshake only: opens the session, reads the descriptor, closes.  No audio is
sent, so nothing can desynchronise the camera's decoder.
"""
import base64, socket, sys, time
sys.path.insert(0, r'C:\camhi\work')
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from camhi_speaker_http import _recv_http, _field, CAM_DEFAULT, USER_DEFAULT, PWD_DEFAULT

CAM = CAM_DEFAULT
AUTH = 'Basic ' + base64.b64encode(f'{USER_DEFAULT}:{PWD_DEFAULT}'.encode()).decode()

CANDIDATES = [
    ('g711a',   'G.711 A-law  (known good)'),
    ('g711u',   'G.711 mu-law (contains "g711")'),
    ('g726',    'G.726  (exact match required)'),
    ('g726-32', 'G.726-32 (NOT exact -> should fail)'),
    ('aac',     'AAC'),
    ('aac-lc',  'AAC-LC (contains "aac")'),
    ('g722',    'G.722 (unknown -> should fail)'),
]


def probe(codec, chn=1):
    s = socket.create_connection((CAM, 80), 6)
    try:
        s.sendall((f'GET /call_invite.cgi?chn={chn}&type={codec}& HTTP/1.1\r\n'
                   f'Host: {CAM}\r\nAuthorization: {AUTH}\r\n'
                   'Connection: keep-alive\r\n\r\n').encode())
        resp = _recv_http(s, 4.0)
        head, _, body = resp.partition(b'\r\n\r\n')
        status = head.split(b'\r\n', 1)[0].decode('latin1') if head else '(no reply)'
        desc = body.decode('latin1').replace('\r\n', ' | ').strip()
        return status, desc, _field(body, 'Session-ID')
    except Exception as e:
        return f'ERROR {type(e).__name__}', str(e), None
    finally:
        s.close()


print(f'{"type=":<10} {"status":<16} descriptor')
print('-' * 100)
results = {}
for codec, note in CANDIDATES:
    status, desc, sid = probe(codec)
    results[codec] = (status, desc, sid)
    print(f'{codec:<10} {status:<16} {desc[:70]}')
    time.sleep(1.5)

print('\n--- summary ---')
for codec, note in CANDIDATES:
    status, desc, sid = results[codec]
    ok = '200' in status and sid
    fmt = ''
    if 'AEnc-Format' in desc:
        fmt = desc.split('AEnc-Format:')[1].split('|')[0].strip()
    print(f'{codec:<10} {"ACCEPTED" if ok else "rejected":<10} {fmt:<24} {note}')
