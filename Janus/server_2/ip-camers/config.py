# -*- coding: utf-8 -*-
"""Конфигурация камер и сервера"""

CAMERAS = {
    0: {
        "name": "Camera 1",
        "rtsp": "rtsp://admin:!Dezmant2805@192.168.8.65:554/Streaming/Channels/101",
        "enabled": True,
    },
    1: {
        "name": "Camera 2",
        "rtsp": "rtsp://admin:password@192.168.8.66:554/Streaming/Channels/101",
        "enabled": True,
    },
    2: {
        "name": "Camera 3",
        "rtsp": "rtsp://admin:password@192.168.8.67:554/Streaming/Channels/101",
        "enabled": True,
    },
}

# Сервер
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 5555
RECONNECT_DELAY = 3.0
MAX_RECONNECT_ATTEMPTS = 0          # 0 = бесконечно

# Клиент
CLIENT_HOST = "127.0.0.1"
CLIENT_PORT = 5555
CLIENT_RECONNECT_DELAY = 2.0
DISPLAY_FPS = 25

# Размер очередей (чем меньше — тем меньше задержка)
VIDEO_QUEUE_SIZE = 6
AUDIO_QUEUE_SIZE = 10