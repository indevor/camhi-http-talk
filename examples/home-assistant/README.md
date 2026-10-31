# Home Assistant integration

[Русский](#русский) · [English](#english)

---

<a name="русский"></a>
# Русский

## 1. Положить скрипты

Скопируйте папку `src` в конфиг Home Assistant:

```
/config/camhi/src/camhi_speaker_http.py
/config/camhi/src/camhi_tts.py          (опционально)
/config/sounds/*.wav                     (ваши сообщения)
```

## 2. Объявить shell_command

```yaml
# configuration.yaml
shell_command:
  camhi_say: >-
    python3 /config/camhi/src/camhi_speaker_http.py
    --cam 192.168.1.129 --wav {{ wav }}
  camhi_tone: >-
    python3 /config/camhi/src/camhi_speaker_http.py
    --cam 192.168.1.129 --tone 2
```

Пароль лучше не писать в конфиге — задайте его переменной окружения для
контейнера Home Assistant:

```yaml
# configuration.yaml
shell_command:
  camhi_say: >-
    CAMHI_CAMERA=192.168.1.129 CAMHI_PASSWORD='ваш-пароль'
    python3 /config/camhi/src/camhi_speaker_http.py --wav {{ wav }}
```

## 3. Автоматизация

```yaml
automation:
  - alias: "Камера: кто-то у двери"
    trigger:
      - platform: state
        entity_id: binary_sensor.doorbell
        to: "on"
    action:
      - service: shell_command.camhi_say
        data:
          wav: /config/sounds/who_is_there.wav
```

```yaml
  - alias: "Камера: движение"
    trigger:
      - platform: state
        entity_id: binary_sensor.camera_motion
        to: "on"
    action:
      - service: shell_command.camhi_tone
```

## 4. Кнопка в интерфейсе

```yaml
# ui-lovelace.yaml
type: button
name: Сказать на камеру
icon: mdi:bullhorn
tap_action:
  action: call-service
  service: shell_command.camhi_say
  data:
    wav: /config/sounds/attention.wav
```

## 5. Замечания

* Home Assistant OS / Supervised: `python3` доступен внутри контейнера
  `homeassistant`. Сторонние пакеты не нужны — только стандартная библиотека.
* Если звук очень тихий — громкость самой камеры низкая. Поднимите её в
  веб-интерфейсе камеры или добавьте `--volume 300` в команду.
* Не запускайте два сообщения одновременно: камера обслуживает одну talk-сессию.
  Добавьте `mode: queued` в автоматизацию при необходимости.

---

<a name="english"></a>
# English

## 1. Install the scripts

Copy the `src` folder into your Home Assistant config:

```
/config/camhi/src/camhi_speaker_http.py
/config/camhi/src/camhi_tts.py          (optional)
/config/sounds/*.wav                     (your messages)
```

## 2. Declare shell_command

```yaml
# configuration.yaml
shell_command:
  camhi_say: >-
    python3 /config/camhi/src/camhi_speaker_http.py
    --cam 192.168.1.129 --wav {{ wav }}
  camhi_tone: >-
    python3 /config/camhi/src/camhi_speaker_http.py
    --cam 192.168.1.129 --tone 2
```

Prefer an environment variable over putting the password in the config:

```yaml
shell_command:
  camhi_say: >-
    CAMHI_CAMERA=192.168.1.129 CAMHI_PASSWORD='your-password'
    python3 /config/camhi/src/camhi_speaker_http.py --wav {{ wav }}
```

## 3. Automation

```yaml
automation:
  - alias: "Camera: someone at the door"
    trigger:
      - platform: state
        entity_id: binary_sensor.doorbell
        to: "on"
    action:
      - service: shell_command.camhi_say
        data:
          wav: /config/sounds/who_is_there.wav
```

## 4. A button in the dashboard

```yaml
type: button
name: Speak on camera
icon: mdi:bullhorn
tap_action:
  action: call-service
  service: shell_command.camhi_say
  data:
    wav: /config/sounds/attention.wav
```

## 5. Notes

* On Home Assistant OS / Supervised, `python3` is available inside the
  `homeassistant` container. No third-party packages are needed.
* If playback is very quiet, the **camera's own** volume is low. Raise it in the
  camera's web UI, or add `--volume 300` to the command.
* Do not fire two messages at once: the camera serves one talk session at a time.
  Add `mode: queued` to the automation if needed.
