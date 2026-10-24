# GRBL TCP-мост для Raspberry Pi

Замена ESP32: прозрачный мост `TCP :8881 ↔ USB-serial` к GRBL 1.1.
Janus (`CNCController`) подключается к IP малинки так же, как к ESP.

Не подключайте ESP32 и Pi к станку одновременно.

Предпочтительно Ethernet, не Wi‑Fi: шпиндель как раз глушит радио.

## 1. Что нужно

- Raspberry Pi 4, Raspberry Pi OS (Bullseye/Bookworm)
- USB-кабель от Pi к USB-порту контроллера станка (CH340/CP2102/Arduino)
- Pi в той же сети, что и Janus-сервер

## 2. Копирование файлов

На Pi:

```bash
sudo mkdir -p /opt/grbl-tcp-bridge
```

С компьютера (подставьте IP малинки и путь к репозиторию):

```bash
scp Janus/server/frezer/pi-bridge/grbl_tcp_bridge.py \
    Janus/server/frezer/pi-bridge/requirements.txt \
    Janus/server/frezer/pi-bridge/grbl-tcp-bridge.service \
    pi@<IP_МАЛИНКИ>:/tmp/
```

На Pi:

```bash
sudo mv /tmp/grbl_tcp_bridge.py /tmp/requirements.txt /tmp/grbl-tcp-bridge.service /opt/grbl-tcp-bridge/
sudo chmod +x /opt/grbl-tcp-bridge/grbl_tcp_bridge.py
```

Если пользователь не `pi` (Bookworm спрашивает имя при установке) — дальше везде подставьте своё.

## 3. Python и права на COM-порт

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-serial
sudo usermod -aG dialout "$USER"
```

Либо в venv:

```bash
sudo apt install -y python3 python3-venv python3-pip
python3 -m venv /opt/grbl-tcp-bridge/venv
sudo /opt/grbl-tcp-bridge/venv/bin/pip install -r /opt/grbl-tcp-bridge/requirements.txt
```

В этом случае в unit-файле замените `ExecStart` на:

```
ExecStart=/opt/grbl-tcp-bridge/venv/bin/python /opt/grbl-tcp-bridge/grbl_tcp_bridge.py --listen 0.0.0.0 --tcp-port 8881 --serial auto --baud 115200
```

**Перелогиньтесь** (или `newgrp dialout`), иначе группы `dialout` ещё нет.

Проверьте, что станок виден:

```bash
ls -l /dev/ttyUSB* /dev/ttyACM* /dev/serial/by-id/ 2>/dev/null
```

Обычно это `/dev/ttyUSB0`.

Опционально — не сбрасывать Arduino при открытии порта (udev):

```bash
echo 'ACTION=="add", KERNEL=="ttyUSB*", RUN+="/bin/stty -F /dev/%k -hupcl"
ACTION=="add", KERNEL=="ttyACM*", RUN+="/bin/stty -F /dev/%k -hupcl"' | sudo tee /etc/udev/rules.d/99-grbl-no-reset.rules
sudo udevadm control --reload-rules
```

Скрипт и сам снимает DTR/RTS/HUPCL. Правило udev страхует первый импульс при появлении устройства.

## 4. Ручной запуск (проверка)

Кабель вставлен, ESP32 отключён от UART станка.

```bash
python3 /opt/grbl-tcp-bridge/grbl_tcp_bridge.py --serial auto --tcp-port 8881
```

Должно быть что-то вроде:

```
TCP listen 0.0.0.0:8881
serial open /dev/serial/by-id/... (/dev/ttyUSB0) 115200 8N1
```

С другого ПК в той же сети:

```bash
python3 -c "import socket; s=socket.create_connection(('<IP_МАЛИНКИ>', 8881)); s.send(b'?'); print(s.recv(256))"
```

Ожидаемый ответ GRBL: строка вида `<Idle|MPos:...>` (или `Grbl 1.1...` при сбросе).

Остановка: Ctrl+C.

Фиксированный порт, если auto выбрал не то:

```bash
python3 /opt/grbl-tcp-bridge/grbl_tcp_bridge.py --serial /dev/ttyUSB0 --tcp-port 8881
```

## 5. Автозапуск (systemd)

```bash
sudo cp /opt/grbl-tcp-bridge/grbl-tcp-bridge.service /etc/systemd/system/grbl-tcp-bridge.service
```

Отредактируйте при необходимости:

```bash
sudo nano /etc/systemd/system/grbl-tcp-bridge.service
```

- `User=pi` — ваш пользователь
- `Group=dialout`
- `--serial /dev/ttyUSB0` вместо `auto`, если портов несколько

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now grbl-tcp-bridge
sudo systemctl status grbl-tcp-bridge
journalctl -u grbl-tcp-bridge -f
```

После перезагрузки мост поднимается сам.

Перезапуск / стоп:

```bash
sudo systemctl restart grbl-tcp-bridge
sudo systemctl stop grbl-tcp-bridge
```

## 6. Переключение Janus на малинку

Код Janus менять не нужно. На машине, где крутится сервер, задайте IP Pi вместо ESP:

```bash
export CNC_HOST=<IP_МАЛИНКИ>
export CNC_PORT=8881
```

Дальше тот же `cnc_controller.py` / адаптер `cnc_01`.

Проверка мониторинга (со стороны Janus, станок не едет):

```bash
python cnc_controller.py <IP_МАЛИНКИ>
```

Вернуться на ESP: снова `CNC_HOST` IP ESP32 (сейчас по умолчанию `192.168.8.71`).

## 7. Если не работает

| Симптом | Что проверить |
|---|---|
| `Permission denied` на `/dev/ttyUSB0` | пользователь в `dialout`, перелогин |
| `ALARM:3` / `Grbl 1.1` при старте моста | сработал DTR; udev-правило и не открывать порт второй программой |
| пустой ответ на `?` | не тот USB, baud не 115200, ESP всё ещё на UART |
| Janus не коннектится | `ss -lntp \| grep 8881`, фаервол, Pi и сервер в одной подсети |
| порт занят | `sudo systemctl stop grbl-tcp-bridge`; никто больше не держит COM |
