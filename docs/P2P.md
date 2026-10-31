# The P2P talk path (and why it is not portable)

[Русский](#русский) · [English](#english)

> **Short version: use the HTTP path.** It is simpler, works on any camera and
> needs no traffic capture. This document records the P2P path because it was
> reverse-engineered on the way, and because it explains a common pitfall.

---

<a name="русский"></a>
# Русский

## 1. Что такое P2P-путь

Камера общается с мобильным приложением по собственному протоколу HiChip поверх
UDP. Это **тот же путь, которым пользуется приложение**, просто с компьютера.

```
[1] сессия
    ПК  -> камера:32108   f130 (LAN_SEARCH)
    камера -> ПК          f141 с НОВОГО порта (он меняется каждую сессию)
    ПК  -> этот порт      f142 (UID-blob) xN
    камера -> ПК          f1e0 (ALIVE)
    ⚠ всё время ОДИН и тот же UDP-сокет (камера сверяет sockaddr через memcmp)

[2] логин — opcode 4096, тело 688 байт
[3] настройка — около 9 опкодов (SET_VIDEO_PARAM, LIVE_REQ и др.)
[4] START_TALK — opcode 4113, тело 8 байт (токен)
[5] аудио — кадры 0xf1d0, stream 0x0100, 824 байта:
       d1 | 0100 | seq | "HXAF" | 800 (LE) | 00*8 | 800 б G.711 A-law
       один кадр = 100 мс @ 8 кГц
[6] STOP_TALK — opcode 4114
```

Обязательно отвечать: на `f1e0` → `f1e1`, на каждый `f1d0` → `f1d1`
(список подтверждённых `seq`). Иначе камера рвёт сессию.

## 2. Логин привязан к конкретной камере

Из декомпиляции `HI_P2P_Platform_UserPasswd_Check_Encrypt @ 0x22744`:

```c
/* логин и пароль берутся из таблицы пользователей камеры (шаг 0x81) */
HI_P2P_URldecode(&p2p_sUserInfo, ..., local_1b8);   /* USERNAME */
HI_P2P_URldecode(&DAT_0008e7ec,  ..., local_2b8);   /* PASSWORD */

/* ключ строится с UID САМОЙ КАМЕРЫ — глобальная переменная! */
snprintf(buf, 0x400, "%d%s%s%s", handleField, local_2b8, &p2p_sHichipID, local_1b8);
HI_P2P_MD5_Get(buf, &key);
HI_P2P_Aes_Init(ctx, &key, len);
HI_P2P_Aes_Decrypt(ctx, body, bodylen);
/* далее проверка контрольной суммы и memcmp логина/пароля */
```

* `handleField` (u32) **передаётся в пакете** — камера берёт значение оттуда.
* `p2p_sHichipID` — **глобальная переменная с UID камеры**, в пакет не попадает.

Проверка захваченного пакета подтверждает: UID внутри **не** передаётся (ни
строкой, ни без дефисов, ни hex-ом). Значит камера всегда подставляет свой UID,
и пакет логина годится **только для той камеры, с которой снят**.

## 3. Пароль — тот же, что от админки

USERNAME и PASSWORD читаются из `p2p_sUserInfo` и `DAT_0008e7ec` с шагом **0x81** —
это **та же таблица пользователей**, которую `HI_Websvr_Start` использует для
HTTP Basic-auth. Отдельного «пароля P2P» не существует.

## 4. Что это значит

| | HTTP `/call_invite.cgi` | P2P talk |
|---|---|---|
| Транспорт | TCP/HTTP, порт 80 | UDP, свой протокол |
| Авторизация | **Basic auth** | зашифрованный логин |
| Переносится на другую камеру | ✅ **да** | ❌ нет |
| Нужен захват трафика | нет | да |
| Переживёт смену пароля | да | нет |

**Если P2P всё-таки нужен** на другой камере:

1. снять свой pcap, нажав кнопку микрофона в приложении CamHi Pro;
2. извлечь кадры (`tools/extract_p2p_frames.py`);
3. положить `p2p_frames.py` рядом с `camhi_speaker_p2p.py`.

Либо реализовать логин честно — теперь известны все компоненты:
`MD5(handleField + пароль + UID + "admin")`, AES-расшифровка, контрольная сумма.

## 5. Известная проблема

UDP LAN-discovery (`0xf130` → `0xf141`) периодически **не отвечает** — проверено
на двух камерах. Когда ответа нет, клиент откатывается на порт по умолчанию
32108 и сессия не открывается. HTTP-путь от этого не зависит.

---

<a name="english"></a>
# English

## 1. What the P2P path is

The camera talks to the mobile app over HiChip's own protocol on top of UDP. This
is **exactly what the app does**, only driven from a computer.

```
[1] session
    PC  -> camera:32108   f130 (LAN_SEARCH)
    camera -> PC          f141 from a NEW port (it changes every session)
    PC  -> that port      f142 (UID blob) xN
    camera -> PC          f1e0 (ALIVE)
    ⚠ always the SAME UDP socket (the camera memcmp's the peer sockaddr)

[2] login — opcode 4096, 688-byte body
[3] setup — about 9 opcodes (SET_VIDEO_PARAM, LIVE_REQ, …)
[4] START_TALK — opcode 4113, 8-byte body (a token)
[5] audio — 0xf1d0 frames, stream 0x0100, 824 bytes:
       d1 | 0100 | seq | "HXAF" | 800 (LE) | 00*8 | 800 B G.711 A-law
       one frame = 100 ms @ 8 kHz
[6] STOP_TALK — opcode 4114
```

You must reply: `f1e0` → `f1e1`, and every `f1d0` → `f1d1` (the list of acked
`seq` values). Otherwise the camera drops the session.

## 2. The login is bound to one specific camera

From `HI_P2P_Platform_UserPasswd_Check_Encrypt @ 0x22744`:

```c
/* username and password come from the camera's user table (stride 0x81) */
HI_P2P_URldecode(&p2p_sUserInfo, ..., local_1b8);   /* USERNAME */
HI_P2P_URldecode(&DAT_0008e7ec,  ..., local_2b8);   /* PASSWORD */

/* the key mixes in THE CAMERA'S OWN UID — a global variable! */
snprintf(buf, 0x400, "%d%s%s%s", handleField, local_2b8, &p2p_sHichipID, local_1b8);
HI_P2P_MD5_Get(buf, &key);
HI_P2P_Aes_Init(ctx, &key, len);
HI_P2P_Aes_Decrypt(ctx, body, bodylen);
/* then a checksum and a memcmp of user/password */
```

* `handleField` (u32) **travels inside the packet** — the camera takes it from there.
* `p2p_sHichipID` is a **global holding the camera's UID** and is never transmitted.

Inspecting a captured packet confirms it: the UID is **not** present (not as a
string, not without dashes, not as hex). The camera always substitutes its own, so
a captured login only works on **the camera it came from**.

## 3. The password is the same one the web UI uses

USERNAME and PASSWORD come from `p2p_sUserInfo` and `DAT_0008e7ec` with a stride of
**0x81** — the **same user table** `HI_Websvr_Start` uses for HTTP Basic auth.
There is no separate "P2P password".

## 4. What this means

| | HTTP `/call_invite.cgi` | P2P talk |
|---|---|---|
| Transport | TCP/HTTP, port 80 | UDP, custom protocol |
| Auth | **Basic auth** | encrypted login |
| Portable to another camera | ✅ **yes** | ❌ no |
| Needs traffic capture | no | yes |
| Survives a password change | yes | no |

**If you still want P2P** on another camera:

1. capture a pcap while pressing the microphone button in the CamHi Pro app;
2. extract the frames (`tools/extract_p2p_frames.py`);
3. place `p2p_frames.py` next to `camhi_speaker_p2p.py`.

Or implement the login honestly — every ingredient is now known:
`MD5(handleField + password + UID + "admin")`, AES decryption, checksum.

## 5. Known issue

UDP LAN discovery (`0xf130` → `0xf141`) intermittently gets **no reply** — observed
on two cameras. When that happens the client falls back to the default port 32108
and the session never opens. The HTTP path is unaffected.
