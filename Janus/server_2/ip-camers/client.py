# -*- coding: utf-8 -*-
"""
Клиент: 3 видеопотока + 1 аудио (выбор на лету)
2×2 сетка, чёрный экран, авто-реконнект, PTS-синхронизация
"""

import av
import cv2
import numpy as np
import socket
import struct
import threading
import time
import queue
from typing import Optional, Dict, Tuple

from config import (
    CLIENT_HOST, CLIENT_PORT, CLIENT_RECONNECT_DELAY,
    DISPLAY_FPS, CAMERAS, VIDEO_QUEUE_SIZE, AUDIO_QUEUE_SIZE
)

av.logging.set_level(av.logging.ERROR)

STREAM_VIDEO = 0
STREAM_AUDIO = 1
STREAM_VIDEO_EXTRADATA = 2
STREAM_AUDIO_EXTRADATA = 3

HEADER_FMT = "!BBIq"
HEADER_SIZE = struct.calcsize(HEADER_FMT)

FRAME_W, FRAME_H = 1280, 720
BLACK = np.zeros((FRAME_H, FRAME_W, 3), dtype=np.uint8)


class CameraClient:
    def __init__(self):
        self.stop_event = threading.Event()
        self.sock: Optional[socket.socket] = None
        self.connected = False

        self.video_queues: Dict[int, queue.Queue] = {
            i: queue.Queue(maxsize=VIDEO_QUEUE_SIZE) for i in range(3)
        }
        self.audio_queues: Dict[int, queue.Queue] = {
            i: queue.Queue(maxsize=AUDIO_QUEUE_SIZE) for i in range(3)
        }

        # (image, pts)
        self.frames: Dict[int, Tuple[np.ndarray, int]] = {
            i: (BLACK.copy(), 0) for i in range(3)
        }
        self.frame_lock = threading.Lock()

        self.audio_cam_id = 0
        self.audio_lock = threading.Lock()

        # Master clock (PTS аудио)
        self.audio_pts = 0
        self.audio_pts_lock = threading.Lock()

        self.video_codecs: Dict[int, av.CodecContext] = {}
        self.audio_codecs: Dict[int, av.CodecContext] = {}

    # -------------------- Сеть --------------------
    def _connect(self) -> bool:
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(5.0)
            self.sock.connect((CLIENT_HOST, CLIENT_PORT))
            self.sock.settimeout(None)
            self.connected = True
            print(f"✓ Подключено к серверу {CLIENT_HOST}:{CLIENT_PORT}")
            return True
        except Exception as e:
            print(f"✗ Не удалось подключиться: {e}")
            self.connected = False
            if self.sock:
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.sock = None
            return False

    def _recv_exact(self, size: int) -> Optional[bytes]:
        data = b""
        while len(data) < size:
            try:
                chunk = self.sock.recv(size - len(data))
                if not chunk:
                    return None
                data += chunk
            except Exception:
                return None
        return data

    def _receiver_loop(self):
        while not self.stop_event.is_set():
            if not self.connected:
                if not self._connect():
                    time.sleep(CLIENT_RECONNECT_DELAY)
                    continue

            try:
                header = self._recv_exact(HEADER_SIZE)
                if header is None:
                    raise ConnectionError("Сервер закрыл соединение")

                cam_id, stream_type, size, pts = struct.unpack(HEADER_FMT, header)
                data = self._recv_exact(size)
                if data is None:
                    raise ConnectionError("Обрыв при чтении пакета")

                if cam_id not in (0, 1, 2):
                    continue

                if stream_type == STREAM_VIDEO_EXTRADATA:
                    self._init_video_codec(cam_id, data)
                    continue
                if stream_type == STREAM_AUDIO_EXTRADATA:
                    self._init_audio_codec(cam_id, data)
                    continue

                item = (data, pts)
                q = self.video_queues[cam_id] if stream_type == STREAM_VIDEO else self.audio_queues[cam_id]

                try:
                    q.put_nowait(item)
                except queue.Full:
                    try:
                        q.get_nowait()
                        q.put_nowait(item)
                    except queue.Empty:
                        pass

            except Exception as e:
                print(f"Потеря связи: {e}. Переподключение...")
                self.connected = False
                if self.sock:
                    try:
                        self.sock.close()
                    except Exception:
                        pass
                    self.sock = None
                self.video_codecs.clear()
                self.audio_codecs.clear()
                time.sleep(CLIENT_RECONNECT_DELAY)

    # -------------------- Кодеки --------------------
    def _init_video_codec(self, cam_id: int, extradata: bytes):
        if cam_id in self.video_codecs:
            return
        for name in ("h264", "hevc", "h265"):
            try:
                ctx = av.CodecContext.create(name, "r")
                ctx.extradata = extradata
                try:
                    ctx.open()
                except Exception:
                    pass
                self.video_codecs[cam_id] = ctx
                print(f"[Cam {cam_id}] Видео-кодек: {name} (extradata {len(extradata)} байт)")
                return
            except Exception:
                continue
        print(f"[Cam {cam_id}] Не удалось инициализировать видео-кодек")

    def _init_audio_codec(self, cam_id: int, extradata: bytes):
        if cam_id in self.audio_codecs:
            return
        for name in ("aac", "pcm_alaw", "pcm_mulaw", "opus", "mp3"):
            try:
                ctx = av.CodecContext.create(name, "r")
                if extradata:
                    ctx.extradata = extradata
                try:
                    ctx.open()
                except Exception:
                    pass
                self.audio_codecs[cam_id] = ctx
                print(f"[Cam {cam_id}] Аудио-кодек: {name}")
                return
            except Exception:
                continue
        print(f"[Cam {cam_id}] Не удалось инициализировать аудио-кодек")

    # -------------------- Видео --------------------
    def _video_decoder_loop(self, cam_id: int):
        q = self.video_queues[cam_id]
        while not self.stop_event.is_set():
            try:
                data, pts = q.get(timeout=0.3)
            except queue.Empty:
                continue

            ctx = self.video_codecs.get(cam_id)
            if ctx is None:
                continue

            try:
                packet = av.Packet(data)
                packet.pts = pts
                for frame in ctx.decode(packet):
                    img = frame.to_ndarray(format="bgr24")
                    if img.shape[1] != FRAME_W or img.shape[0] != FRAME_H:
                        img = cv2.resize(img, (FRAME_W, FRAME_H))
                    with self.frame_lock:
                        self.frames[cam_id] = (img, pts)
            except Exception:
                pass

    # -------------------- Аудио (master clock) --------------------
    def _audio_player_loop(self):
        try:
            import sounddevice as sd
        except ImportError:
            print("⚠ Установите: pip install sounddevice")
            return

        stream = None
        current_rate = None
        current_channels = None

        while not self.stop_event.is_set():
            with self.audio_lock:
                cam_id = self.audio_cam_id

            q = self.audio_queues[cam_id]
            try:
                data, pts = q.get(timeout=0.15)
            except queue.Empty:
                continue

            ctx = self.audio_codecs.get(cam_id)
            if ctx is None:
                continue

            try:
                packet = av.Packet(data)
                packet.pts = pts
                for frame in ctx.decode(packet):
                    audio = frame.to_ndarray()
                    if audio.size == 0:
                        continue

                    if audio.dtype == np.int16:
                        audio = audio.astype(np.float32) / 32768.0
                    elif audio.dtype == np.int32:
                        audio = audio.astype(np.float32) / 2147483648.0
                    elif audio.dtype != np.float32:
                        audio = audio.astype(np.float32)

                    if audio.ndim == 1:
                        audio = audio.reshape(-1, 1)
                    elif audio.shape[0] < audio.shape[1]:
                        audio = audio.T

                    rate = frame.sample_rate
                    channels = audio.shape[1]

                    if stream is None or rate != current_rate or channels != current_channels:
                        if stream is not None:
                            stream.stop()
                            stream.close()
                        stream = sd.OutputStream(
                            samplerate=rate,
                            channels=channels,
                            dtype="float32",
                            latency="low",
                            blocksize=0,
                        )
                        stream.start()
                        current_rate = rate
                        current_channels = channels
                        print(f"[Audio] {rate} Hz, {channels} ch (cam {cam_id})")

                    stream.write(audio)

                    # Обновляем master clock
                    with self.audio_pts_lock:
                        self.audio_pts = pts

            except Exception:
                pass

        if stream is not None:
            stream.stop()
            stream.close()

    # -------------------- Отображение --------------------
    def _display_loop(self):
        cv2.namedWindow("Cameras 2x2", cv2.WINDOW_NORMAL)
        cv2.resizeWindow("Cameras 2x2", FRAME_W * 2, FRAME_H * 2)

        print("\nУправление:")
        print("  1 / 2 / 3  — выбрать аудио с камеры")
        print("  q / ESC    — выход")
        print("-" * 40)

        while not self.stop_event.is_set():
            with self.frame_lock:
                f0_img, _ = self.frames[0]
                f1_img, _ = self.frames[1]
                f2_img, _ = self.frames[2]

            top = np.hstack([f0_img, f1_img])
            bottom = np.hstack([f2_img, BLACK])
            grid = np.vstack([top, bottom])

            for i, (x, y) in enumerate([(10, 30), (FRAME_W + 10, 30), (10, FRAME_H + 30)]):
                name = CAMERAS.get(i, {}).get("name", f"Cam {i}")
                color = (0, 255, 0) if i == self.audio_cam_id else (180, 180, 180)
                label = f"{name} {'[AUDIO]' if i == self.audio_cam_id else ''}"
                cv2.putText(grid, label, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)

            cv2.imshow("Cameras 2x2", grid)
            key = cv2.waitKey(max(1, 1000 // DISPLAY_FPS)) & 0xFF

            if key in (ord("q"), 27):
                self.stop_event.set()
                break
            elif key in (ord("1"), ord("2"), ord("3")):
                new_id = key - ord("1")
                with self.audio_lock:
                    if self.audio_cam_id != new_id:
                        self.audio_cam_id = new_id
                        print(f"Аудио → камера {new_id}")

        cv2.destroyAllWindows()

    # -------------------- Публичный API --------------------
    def start(self):
        print("=" * 60)
        print("Camera Client (2×2 + audio select + PTS sync)")
        print("=" * 60)

        for cam_id in range(3):
            t = threading.Thread(target=self._video_decoder_loop, args=(cam_id,), daemon=True)
            t.start()

        threading.Thread(target=self._receiver_loop, daemon=True).start()
        threading.Thread(target=self._audio_player_loop, daemon=True).start()

        # OpenCV лучше в главном потоке
        self._display_loop()
        self.stop()

    def stop(self):
        self.stop_event.set()
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        print("Клиент остановлен")


if __name__ == "__main__":
    client = CameraClient()
    client.start()