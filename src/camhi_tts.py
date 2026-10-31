#!/usr/bin/env python3
"""
camhi_tts.py — say anything on a CamHi / HiChip camera speaker.

Pipeline:  text -> Windows SAPI voice -> 8 kHz mono WAV -> camera speaker
           (over the camera's own HTTP talk endpoint /call_invite.cgi)

Requires the matching camhi_speaker_http.py next to this file.

Examples
--------
    python work\\camhi_tts.py "Кто-то у двери"
    python work\\camhi_tts.py "Внимание, обнаружено движение" --voice "Microsoft Irina Desktop"
    python work\\camhi_tts.py "Testing the speaker" --voice "Microsoft Zira Desktop" --keep test.wav
    python work\\camhi_tts.py --list-voices
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# make a cmd.exe console able to show Cyrillic status lines
if os.name == 'nt':
    try:
        os.system('chcp 65001 >nul 2>&1')
    except Exception:
        pass

CAM_DEFAULT = os.environ.get('CAMHI_CAMERA', '192.168.1.100')
DEFAULT_VOICE = 'Microsoft Irina Desktop'      # ru-RU

PS_LIST_VOICES = (
    "Add-Type -AssemblyName System.Speech; "
    "(New-Object System.Speech.Synthesis.SpeechSynthesizer)"
    ".GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name }"
)


def powershell(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
        capture_output=True, text=True, errors='replace')


def list_voices() -> list[str]:
    r = powershell(PS_LIST_VOICES)
    return [l.strip() for l in (r.stdout or '').splitlines() if l.strip()]


def text_to_wav(text: str, wav_path: str, voice: str, rate: int = 0,
                volume: int = 100) -> None:
    """Synthesise `text` into `wav_path` as 8 kHz mono 16-bit PCM."""
    # the text goes through a temp file so no quoting/escaping can break it
    with tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False,
                                     encoding='utf-8') as tf:
        tf.write(text)
        txt_path = tf.name
    ps = f'''
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Speech
$text = Get-Content -Raw -Encoding UTF8 -LiteralPath "{txt_path}"
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
        8000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
        [System.Speech.AudioFormat.AudioChannel]::Mono)
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {{ $synth.SelectVoice("{voice}") }} catch {{ Write-Error "voice not found: {voice}"; exit 2 }}
$synth.Rate = {rate}
$synth.Volume = {volume}
$synth.SetOutputToWaveFile("{wav_path}", $fmt)
$synth.Speak($text.Trim())
$synth.Dispose()
'''
    try:
        r = powershell(ps)
        if r.returncode != 0:
            raise SystemExit(f'Ошибка синтеза речи:\n{r.stderr or r.stdout}')
    finally:
        try:
            os.unlink(txt_path)
        except OSError:
            pass
    if not os.path.isfile(wav_path) or os.path.getsize(wav_path) < 100:
        raise SystemExit('Не удалось создать WAV')


def describe(path: str) -> str:
    with wave.open(path, 'rb') as w:
        secs = w.getnframes() / max(1, w.getframerate())
        return (f'{w.getnchannels()}ch {w.getsampwidth()*8}bit '
                f'{w.getframerate()}Hz {secs:.1f}s')


def main():
    ap = argparse.ArgumentParser(
        description='Сказать фразу через динамик камеры (Windows TTS -> камера)')
    ap.add_argument('text', nargs='?', help='что сказать')
    ap.add_argument('--cam', default=CAM_DEFAULT, help='IP камеры')
    ap.add_argument('--voice', default=DEFAULT_VOICE, help='голос SAPI')
    ap.add_argument('--rate', type=int, default=0, help='скорость -10..10')
    ap.add_argument('--tts-volume', type=int, default=100,
                    help='громкость синтеза речи (0..100, по умолчанию 100)')
    ap.add_argument('--volume', type=int, default=100, metavar='0-400',
                    help='программное усиление при воспроизведении: 100 = без '
                         'изменений, 0 = тишина, >100 = громче (камеру не трогает)')
    ap.add_argument('--user', default='admin')
    ap.add_argument('--pwd', default=PWD_DEFAULT)
    ap.add_argument('--frame-ms', type=int, default=20,
                    help='размер кадра: 20 (160 Б) или 100 (800 Б)')
    ap.add_argument('--lead-ms', type=int, default=400,
                    help='предбуфер, мс')
    ap.add_argument('--keep', metavar='FILE', help='сохранить WAV по этому пути')
    ap.add_argument('--list-voices', action='store_true')
    a = ap.parse_args()

    if a.list_voices:
        for v in list_voices():
            print(' ', v)
        return 0

    if not a.text:
        ap.error('укажите текст (или --list-voices)')

    wav_path = a.keep or os.path.join(tempfile.gettempdir(), 'camhi_tts.wav')
    text_to_wav(a.text, wav_path, a.voice, a.rate, a.tts_volume)
    print(f'речь: "{a.text}"')
    print(f'голос: {a.voice};  WAV: {describe(wav_path)}')

    from camhi_speaker_http import say
    ok = say(a.cam, wav=wav_path, user=a.user, pwd=a.pwd,
             frame_ms=a.frame_ms, lead_ms=a.lead_ms, volume=a.volume)
    print('OK' if ok else 'FAILED')

    if not a.keep and os.path.isfile(wav_path):
        try:
            os.unlink(wav_path)
        except OSError:
            pass
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
