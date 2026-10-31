# The camera's HTTP talk protocol — `/call_invite.cgi`

[Русский](#русский) · [English](#english)

---

<a name="русский"></a>
# Русский

## 1. Как это было найдено

1. Скан TCP-портов камеры: открыты только **80, 554, 1935 (RTMP), 8080**.
2. В `ipc_server` нашлась `HI_Media_LiveStreamParseTalk @ 0x478758`, разбирающая
   запрос с заголовками `Audio-Channel:`, `AEnc-Ability:`, `Authorization:`.
3. `HI_Websvr_Start @ 0x4a846c` показала, что веб-сервер камеры — **Mongoose**:

   ```c
   snprintf(buf, 0x28, "Mongoose web server v.%s", ver);
   mg_RegisterDistribLink(0, HI_Media_Interface, 0, HI_Rtsp_O_Http_Interface,
                          local_24, HI_PlayBack_Interface, HI_CGI_Interface);
   ```

4. `HI_Media_Interface @ 0x47043c` порождает поток `FUN_0046fd80`, который
   маршрутизирует по подстроке в запросе:

   | подстрока | обработчик |
   |---|---|
   | `livestream/` | `HI_Media_LiveStreamParseStream` (видео) |
   | `livestream.cgi` | `HI_Media_LiveStreamParseStreamEx` (видео) |
   | **`call_invite.cgi`** | **`HI_Media_LiveStreamParseTalkEx @ 0x47839c`** |

## 2. Протокол

### Шаг 1 — открыть сессию

```http
GET /call_invite.cgi?chn=1&type=g711a& HTTP/1.1
Host: <camera>
Authorization: Basic <base64(admin:пароль)>
Connection: keep-alive
```

* `chn=` — канал (проверяется `chn % 10` против числа каналов);
* `type=` — см. §4;
* без авторизации — `401 Unauthorized` (проверяет Mongoose, до медиа-слоя).

### Шаг 2 — ответ камеры

```
HTTP/1.1 200 OK
Host: <camera>
Server: Camera
Content-Type: application/octet-stream
Content-Length: 91

Session-ID: 1942332
AEnc-Format: g711a 64000 0 1
Transport-Type: TCP RTP
Audio-Port: 0
```

`Session-ID` — **константа сборки** (одинакова на всех камерах с этой прошивкой).
`Audio-Port: 0` означает «оставаться на этом же соединении».

### Шаг 3 — подтверждение сессии (обязательно)

Поток `RecvTalkDataThread @ 0x477044` сначала отправляет ответ из шага 2, затем
читает «response data» и входит в цикл только при совпадении:

```c
iVar6 = HI_Media_LiveStreamRecvData(sock, buf, 0x800, 10000);
if (iVar6 == 0) {
  libs_parsedata(buf, len, "sessid=", " HTTP/1.1", 1, &sid, 0x1f);
  if ((parsedata == 0 && strcmp(sid, "1942332") == 0)
      || strstr(buf, "Session-ID : 1942332") != NULL) {
      printf("RecvTalkDataThread(entry): sock=%d,avchn=%d,format=%d\n", ...);
      ... цикл аудио ...
  } else {
      puts("RecvTalkDataThread: recv response data error!");
  }
}
```

Значит клиент отправляет:

```http
POST http://<camera>/call_invite.cgi?sessid=<Session-ID> HTTP/1.1
Host: <camera>
```

### Шаг 4 — аудиокадры

Разобрано по дизассемблеру цикла `0x4776c8`:

```asm
0x4776c8: lw     $v0, 0x88($s0)        ; флаг выхода
0x4776d4: addiu  $a2, $zero, 0x10      ; читаем РОВНО 16 байт
0x4776e4: bal    0x476e9c              ; RecvDataEx(sock, buf, 16, 10000)
0x477700: lhu    $v0, 0x2a($sp)        ; u16 из [2:4]
0x477704-0x477710: байтовый разворот   ; >>> s6 = BE16(header[2:4])
0x477714: addiu  $v0, $s6, -0xc        ; v0 = s6 - 12
0x47771c: beqz   $v1, 0x477844         ; если (s6-12) >= 0x7f1 -> ошибка
0x477720: move   $a2, $v0              ; дочитываем (s6 - 12) байт в buf+16
0x477728: bal    0x476e9c              ; RecvDataEx(sock, buf+16, s6-12, 10000)
0x477734: addiu  $a0, $sp, 0x3c        ; payload = buf + 20
0x477738: addiu  $a1, $s6, -0x10       ; payload_len = s6 - 16
0x477744: lw     $v0, 0x30($sp)        ; u32 из [8:12]
0x477748-0x47776c: байтовый разворот   ; >>> timestamp = BE32(header[8:12])
0x477768: jalr   $t9                   ; HI_SDK_AudioDecodeData(payload, len, ts)
0x477770: beqz   $v0, 0x4776c8         ; повторять пока ret == 0
```

### Итоговый формат кадра

```
Смещение  Размер  Смысл
[0:2]     2       игнорируется камерой (шлём 80 08)
[2:4]     2       BE u16 = 16 + payload_len     <-- длина, которой камера верит
[4:8]     4       игнорируется (нули)
[8:12]    4       BE u32 timestamp              <-- уходит в декодер
[12:16]   4       игнорируется (нули)
[16:20]   4       игнорируется (нули)
[20:...]  N       полезная нагрузка: G.711 A-law, 8 кГц моно
```

* На провод уходит `20 + payload_len` байт, поле длины `s6 = 16 + payload_len`.
* Тишина в A-law — `0xD5`.
* Типовой кадр — **160 байт = 20 мс** (как в приложении), темп 50 кадров/с.
  Допустимо и 800 байт = 100 мс.

## 3. Карта функций

| Адрес | Функция | Роль |
|---|---|---|
| `0x4a846c` | `HI_Websvr_Start` | запуск Mongoose, регистрация разветвителей |
| `0x4a3040` | `mg_RegisterDistribLink` | сохраняет обработчики |
| `0x47043c` | `HI_Media_Interface` | создаёт поток разбора медиа |
| `0x46fd80` | медиа-поток | **маршрутизация по подстроке** |
| `0x47839c` | `HI_Media_LiveStreamParseTalkEx` | разбор `chn=`/`type=`, старт talk-потока |
| `0x477044` | `RecvTalkDataThread` | ответ 200, проверка `sessid=`, цикл аудио |
| `0x476e9c` | `HI_Media_LiveStreamRecvDataEx` | `select()` + `recv()` до ровно N байт |
| `0x476d24` | `HI_Media_LiveStreamRecvData` | `select()` + один `recv()` |
| `0x4e663c` | `HI_SDK_AudioDecodeData` | декодирование и вывод на динамик |
| `0x4eb944` | `hi_g711a_decode` | **единственный** аудиодекодер камеры |

Маркеры: `0x51b128 = "chn="`, `0x51b19c = "type="`, `0x50bf0c = "&"`.

## 4. Кодеки: `g726` и `aac` — обманка

Камера **принимает** эти `type=` и рапортует о них в дескрипторе:

| `type=` | Ответ |
|---|---|
| `g711a`, `g711u`, что угодно с `g711` | `AEnc-Format: g711a 64000 0 1` |
| **`g726`** (строго точное совпадение) | `AEnc-Format: g726 16000 0 1` |
| **`aac`**, `aac-lc` (подстрока) | `AEnc-Format: aac 48000 0 1` |
| `g726-32`, `g722` | нет ответа |

Логика выбора:

```c
strcmp(type, "g726")   -> format 2
strstr(type, "g711")   -> format 1
strstr(type, "aac")    -> format 3
иначе                  -> отказ
```

**Но декодеров G.726 и AAC у камеры нет.** В `ipc_server` есть только
`hi_g711a_encode @ 0x4eb890` и `hi_g711a_decode @ 0x4eb944`, и
`HI_SDK_AudioDecodeData` безусловно вызывает `hi_g711a_decode`:

```c
undefined4 HI_SDK_AudioDecodeData(undefined4 param_1, undefined4 param_2)
{
  local_c = hi_g711a_decode(DAT_005552cc, param_1, param_2);   /* всегда */
  local_20 = DAT_005552cc;
  iVar1 = IMP_AO_SendFrame(0, 0, auStack_28, 0);
  ...
}
```

`HI_SDK_StartAudioDecode` аргумент-формат тоже игнорирует. Строка `g726_decode`
встречается в `libimp.so`, но `ipc_server` её не вызывает.

**Проверено:** одна и та же фраза, отправленная с заголовками `g711a`, `g726` и
`aac`, но с полезной нагрузкой **G.711a** — все три раза речь звучала одинаково.

> **Вывод: `type=` — только витрина. Полезная нагрузка всегда G.711 A-law.**

## 5. ⚠️ Осторожно: неверный формат кадра роняет камеру

При ошибочной длине в заголовке `HI_SDK_AudioDecodeData` получает мусор и
камера **уходит в перезагрузку** (в UART видны сообщения U-Boot). Наблюдалось
один раз; камера поднялась сама за ~20 с, настройки не пострадали.

Поэтому поле `[2:4]` обязано быть `16 + payload_len`, а нагрузка начинаться ровно
со смещения 20.

## 6. Жизненный цикл сессии и «залипший» динамик

```c
HI_SDK_StartAudioDecode(): IMP_AO_SetVolMute(0,0,0 /*unmute*/); IMP_AO_ResumeChn(0,0);
HI_SDK_StopAudioDecode():  IMP_AO_FlushChnBuf(); IMP_AO_SetVolMute(0,0,1 /*MUTE*/);
                           IMP_AO_PauseChn(0,0);
```

`RecvTalkDataThread` ждёт данные до **10 секунд**. Если новая сессия стартует,
пока старая ещё не завершилась, `HI_PthreadJoin` заставляет камеру ждать: новый
`GET` может не получить ответа вовсе, а старый поток, завершаясь, снова глушит
выход — **звук пропадает до перезагрузки**.

Что делает клиент, чтобы этого не было:

1. **`shutdown(SHUT_WR)`** вместо простого `close()` — камера видит EOF сразу;
2. пауза ~1.2 с после закрытия;
3. повтор `GET` с нарастающей задержкой (`--retries`);
4. опция `--reset-ao` — перезаписать *текущую* громкость через CGI, что заново
   включает выход.

## 7. Громкость

**CGI камеры `setaudiooutvolume` на этой прошивке не работает:**

```
read -> var ao_volume="10";
set 100 -> [Succeed]set ok. | read back -> var ao_volume="10";
set  50 -> [Succeed]set ok. | read back -> var ao_volume="10";
```

Поэтому громкость регулируется **на стороне клиента**: аудио декодируется из
A-law, масштабируется в линейном PCM и снова кодируется. Камера при этом не
трогается. Реальный уровень меняется только через веб-интерфейс камеры.

## 8. ⚠️ Ловушка: таблица сегментов A-law

Классическая таблица `{0x1F, 0x3F, … 0xFFF}` из листинга Sun рассчитана на
**14-битный** вход. На 16-битном PCM она сатурирует всё громче ~4095, а тишину
кодирует как `0xe7` вместо `0xd5`:

```
неверно:  тишина -> 0xe7   round-trip стабилен: НЕТ   макс. ошибка 7984
верно:    тишина -> 0xd5   round-trip стабилен: ДА    макс. ошибка   85
```

Правильная (16-битная) таблица:

```python
_ALAW_SEG = (0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF, 0x3FFF, 0x7FFF)
```

---

<a name="english"></a>
# English

## 1. How it was found

1. TCP port scan: only **80, 554, 1935 (RTMP), 8080** are open.
2. `HI_Media_LiveStreamParseTalk @ 0x478758` in `ipc_server` parses a request with
   `Audio-Channel:`, `AEnc-Ability:` and `Authorization:` headers.
3. `HI_Websvr_Start @ 0x4a846c` revealed the camera's web server is **Mongoose**:

   ```c
   snprintf(buf, 0x28, "Mongoose web server v.%s", ver);
   mg_RegisterDistribLink(0, HI_Media_Interface, 0, HI_Rtsp_O_Http_Interface,
                          local_24, HI_PlayBack_Interface, HI_CGI_Interface);
   ```

4. `HI_Media_Interface @ 0x47043c` spawns `FUN_0046fd80`, which routes by
   substring:

   | substring | handler |
   |---|---|
   | `livestream/` | `HI_Media_LiveStreamParseStream` (video) |
   | `livestream.cgi` | `HI_Media_LiveStreamParseStreamEx` (video) |
   | **`call_invite.cgi`** | **`HI_Media_LiveStreamParseTalkEx @ 0x47839c`** |

## 2. Protocol

### Step 1 — open the session

```http
GET /call_invite.cgi?chn=1&type=g711a& HTTP/1.1
Host: <camera>
Authorization: Basic <base64(admin:password)>
Connection: keep-alive
```

* `chn=` — channel (`chn % 10` is validated against the channel count);
* `type=` — see §4;
* without auth the camera answers `401 Unauthorized` (Mongoose checks before the
  media layer).

### Step 2 — the camera's reply

```
HTTP/1.1 200 OK
Host: <camera>
Server: Camera
Content-Type: application/octet-stream
Content-Length: 91

Session-ID: 1942332
AEnc-Format: g711a 64000 0 1
Transport-Type: TCP RTP
Audio-Port: 0
```

`Session-ID` is a **build-time constant** (identical across cameras running this
firmware). `Audio-Port: 0` means "stay on this connection".

### Step 3 — session acknowledgement (mandatory)

`RecvTalkDataThread @ 0x477044` sends the reply from step 2, then reads "response
data" and only enters its loop when it matches:

```c
iVar6 = HI_Media_LiveStreamRecvData(sock, buf, 0x800, 10000);
if (iVar6 == 0) {
  libs_parsedata(buf, len, "sessid=", " HTTP/1.1", 1, &sid, 0x1f);
  if ((parsedata == 0 && strcmp(sid, "1942332") == 0)
      || strstr(buf, "Session-ID : 1942332") != NULL) {
      printf("RecvTalkDataThread(entry): sock=%d,avchn=%d,format=%d\n", ...);
      ... audio loop ...
  } else {
      puts("RecvTalkDataThread: recv response data error!");
  }
}
```

So the client sends:

```http
POST http://<camera>/call_invite.cgi?sessid=<Session-ID> HTTP/1.1
Host: <camera>
```

### Step 4 — audio frames

Taken from the disassembly of the loop at `0x4776c8`:

```asm
0x4776c8: lw     $v0, 0x88($s0)        ; exit flag
0x4776d4: addiu  $a2, $zero, 0x10      ; read EXACTLY 16 bytes
0x4776e4: bal    0x476e9c              ; RecvDataEx(sock, buf, 16, 10000)
0x477700: lhu    $v0, 0x2a($sp)        ; u16 from [2:4]
0x477704-0x477710: byte swap           ; >>> s6 = BE16(header[2:4])
0x477714: addiu  $v0, $s6, -0xc        ; v0 = s6 - 12
0x47771c: beqz   $v1, 0x477844         ; if (s6-12) >= 0x7f1 -> error
0x477720: move   $a2, $v0              ; read (s6 - 12) more bytes into buf+16
0x477728: bal    0x476e9c              ; RecvDataEx(sock, buf+16, s6-12, 10000)
0x477734: addiu  $a0, $sp, 0x3c        ; payload = buf + 20
0x477738: addiu  $a1, $s6, -0x10       ; payload_len = s6 - 16
0x477744: lw     $v0, 0x30($sp)        ; u32 from [8:12]
0x477748-0x47776c: byte swap           ; >>> timestamp = BE32(header[8:12])
0x477768: jalr   $t9                   ; HI_SDK_AudioDecodeData(payload, len, ts)
0x477770: beqz   $v0, 0x4776c8         ; repeat while ret == 0
```

### Resulting frame layout

```
Offset   Size  Meaning
[0:2]    2     ignored by the camera (we send 80 08)
[2:4]    2     BE u16 = 16 + payload_len     <-- the length the camera trusts
[4:8]    4     ignored (zeros)
[8:12]   4     BE u32 timestamp              <-- handed to the decoder
[12:16]  4     ignored (zeros)
[16:20]  4     ignored (zeros)
[20:..]  N     payload: G.711 A-law, 8 kHz mono
```

* `20 + payload_len` bytes go on the wire; the length field is `s6 = 16 + payload_len`.
* A-law silence is `0xD5`.
* A typical frame is **160 bytes = 20 ms** (as in the app), 50 frames/s.
  800 bytes = 100 ms also works.

## 3. Function map

| Address | Function | Role |
|---|---|---|
| `0x4a846c` | `HI_Websvr_Start` | starts Mongoose, registers distributors |
| `0x4a3040` | `mg_RegisterDistribLink` | stores the handlers |
| `0x47043c` | `HI_Media_Interface` | spawns the media parsing thread |
| `0x46fd80` | media thread | **substring routing** |
| `0x47839c` | `HI_Media_LiveStreamParseTalkEx` | parses `chn=`/`type=`, starts the talk thread |
| `0x477044` | `RecvTalkDataThread` | 200 reply, `sessid=` check, audio loop |
| `0x476e9c` | `HI_Media_LiveStreamRecvDataEx` | `select()` + `recv()` until exactly N bytes |
| `0x476d24` | `HI_Media_LiveStreamRecvData` | `select()` + a single `recv()` |
| `0x4e663c` | `HI_SDK_AudioDecodeData` | decodes and pushes to the speaker |
| `0x4eb944` | `hi_g711a_decode` | the camera's **only** audio decoder |

Markers: `0x51b128 = "chn="`, `0x51b19c = "type="`, `0x50bf0c = "&"`.

## 4. Codecs: `g726` and `aac` are a decoy

The camera **accepts** those `type=` values and advertises them:

| `type=` | Reply |
|---|---|
| `g711a`, `g711u`, anything containing `g711` | `AEnc-Format: g711a 64000 0 1` |
| **`g726`** (exact match only) | `AEnc-Format: g726 16000 0 1` |
| **`aac`**, `aac-lc` (substring) | `AEnc-Format: aac 48000 0 1` |
| `g726-32`, `g722` | no reply |

Selection logic:

```c
strcmp(type, "g726")   -> format 2
strstr(type, "g711")   -> format 1
strstr(type, "aac")    -> format 3
otherwise              -> reject
```

**But the camera has no G.726 or AAC decoder.** `ipc_server` contains only
`hi_g711a_encode @ 0x4eb890` and `hi_g711a_decode @ 0x4eb944`, and
`HI_SDK_AudioDecodeData` unconditionally calls `hi_g711a_decode`:

```c
undefined4 HI_SDK_AudioDecodeData(undefined4 param_1, undefined4 param_2)
{
  local_c = hi_g711a_decode(DAT_005552cc, param_1, param_2);   /* always */
  local_20 = DAT_005552cc;
  iVar1 = IMP_AO_SendFrame(0, 0, auStack_28, 0);
  ...
}
```

`HI_SDK_StartAudioDecode` ignores the format argument too. The string
`g726_decode` appears in `libimp.so` but `ipc_server` never calls it.

**Verified:** the same phrase sent with `g711a`, `g726` and `aac` session headers
but with a **G.711a payload** sounded identical all three times.

> **Conclusion: `type=` is cosmetic. The payload must always be G.711 A-law.**

## 5. ⚠️ Caution: a malformed frame reboots the camera

With a wrong length in the header `HI_SDK_AudioDecodeData` receives garbage and the
camera **reboots** (U-Boot messages appear on the UART). Observed once; the camera
recovered by itself in ~20 s with settings intact.

Field `[2:4]` must be `16 + payload_len`, and the payload must start at offset 20.

## 6. Session lifecycle and a stuck speaker

```c
HI_SDK_StartAudioDecode(): IMP_AO_SetVolMute(0,0,0 /*unmute*/); IMP_AO_ResumeChn(0,0);
HI_SDK_StopAudioDecode():  IMP_AO_FlushChnBuf(); IMP_AO_SetVolMute(0,0,1 /*MUTE*/);
                           IMP_AO_PauseChn(0,0);
```

`RecvTalkDataThread` waits up to **10 seconds** for data. If a new session starts
before the old one finished, `HI_PthreadJoin` makes the camera wait: the new `GET`
may get no reply at all, and the old thread mutes the output as it exits —
**the speaker stays silent until a reboot**.

What the client does about it:

1. **`shutdown(SHUT_WR)`** instead of a bare `close()` — the camera sees EOF at once;
2. a ~1.2 s pause after closing;
3. `GET` retries with growing backoff (`--retries`);
4. `--reset-ao` rewrites the *current* volume through the CGI, which re-arms the output.

## 7. Volume

**The camera's `setaudiooutvolume` CGI does not work on this firmware:**

```
read -> var ao_volume="10";
set 100 -> [Succeed]set ok. | read back -> var ao_volume="10";
set  50 -> [Succeed]set ok. | read back -> var ao_volume="10";
```

Volume is therefore adjusted **client side**: decode from A-law, scale in linear
PCM, re-encode. The camera is never touched. The real level can only be changed
from the camera's web UI.

## 8. ⚠️ Trap: the A-law segment table

The classic `{0x1F, 0x3F, … 0xFFF}` table from the Sun listing is written for
**14-bit** input. On 16-bit PCM it saturates everything above ~4095 and encodes
silence as `0xe7` instead of `0xd5`:

```
wrong:  silence -> 0xe7   round-trip stable: NO    max error 7984
right:  silence -> 0xd5   round-trip stable: YES   max error   85
```

The correct (16-bit) table:

```python
_ALAW_SEG = (0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF, 0x3FFF, 0x7FFF)
```
