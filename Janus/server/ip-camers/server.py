# -*- coding: utf-8 -*-
"""
Сервер трансляции 3 IP-камер → TCP-туннель
Stream-copy + extradata + PTS + авто-реконнект
"""

import av
import socket
import struct
import threading
import time
import queue
from typing import Optional, Dict

from config import (
    CAMERAS, SERVER_HOST, SERVER_PORT,
    RECONNECT_DELAY, MAX_RECONNECT_ATTEMPTS
)

av.logging.set_level(av.logging.ERROR)

# Протокол: cam_id(1) + stream_type(1) + size(4) + pts(8)
STREAM_VIDEO = 0
STREAM_AUDIO = 1
STREAM_VIDEO_EXTRADATA = 2
STREAM_AUDIO_EXTRADATA = 3

HEADER_FMT = "!BBIq"
HEADER_SIZE = struct.calcsize(HEADER_FMT)


class CameraWorker(threading.Thread):
    """Один поток на одну камеру."""

    def __init__(self, cam_id: int, rtsp_url: str, packet_queue: queue.Queue, stop_event: threading.Event):
        super().__init__(daemon=True, name=f"CamWorker-{cam_id}")
        self.cam_id = cam_id
        self.rtsp_url = rtsp_url
        self.packet_queue = packet_queue
        self.stop_event = stop_event
        self.connected = False
        self._container = None
        self._video_extradata_sent = False
        self._audio_extradata_sent = False

    def _connect(self) -> bool:
        options = {
            "rtsp_transport": "tcp",
            "stimeout": "10000000",
            "max_delay": "500000",
        }
        try:
            self._container = av.open(self.rtsp_url, options=options)
            self.connected = True
            self._video_extradata_sent = False
            self._audio_extradata_sent = False
            print(f"[Cam {self.cam_id}] ✓ Подключена")
            return True
        except Exception as e:
            print(f"[Cam {self.cam_id}] ✗ Ошибка подключения: {e}")
            self.connected = False
            return False

    def _disconnect(self):
        if self._container:
            try:
                self._container.close()
            except Exception:
                pass
            self._container = None
        self.connected = False

    def _send_extradata(self, stream, stream_type: int):
        if stream is None or stream.codec_context.extradata is None:
            return
        data = bytes(stream.codec_context.extradata)
        if not data:
            return
        try:
            self.packet_queue.put_nowait((self.cam_id, stream_type, data, 0))
            kind = "video" if stream_type == STREAM_VIDEO_EXTRADATA else "audio"
            print(f"[Cam {self.cam_id}] Отправлен {kind} extradata ({len(data)} байт)")
        except queue.Full:
            pass

    def run(self):
        attempts = 0
        while not self.stop_event.is_set():
            if not self.connected:
                if MAX_RECONNECT_ATTEMPTS and attempts >= MAX_RECONNECT_ATTEMPTS:
                    break
                if not self._connect():
                    attempts += 1
                    time.sleep(RECONNECT_DELAY)
                    continue
                attempts = 0

            try:
                video_stream = next((s for s in self._container.streams if s.type == "video"), None)
                audio_stream = next((s for s in self._container.streams if s.type == "audio"), None)

                if not video_stream:
                    print(f"[Cam {self.cam_id}] Нет видео-потока")
                    self._disconnect()
                    time.sleep(RECONNECT_DELAY)
                    continue

                # Отправляем extradata один раз после подключения
                if not self._video_extradata_sent:
                    self._send_extradata(video_stream, STREAM_VIDEO_EXTRADATA)
                    self._video_extradata_sent = True
                if audio_stream and not self._audio_extradata_sent:
                    self._send_extradata(audio_stream, STREAM_AUDIO_EXTRADATA)
                    self._audio_extradata_sent = True

                streams = [video_stream]
                if audio_stream:
                    streams.append(audio_stream)

                for packet in self._container.demux(*streams):
                    if self.stop_event.is_set():
                        break
                    if packet is None or packet.dts is None:
                        continue

                    data = bytes(packet)
                    stream_type = STREAM_VIDEO if packet.stream.type == "video" else STREAM_AUDIO
                    pts = packet.pts if packet.pts is not None else 0

                    try:
                        self.packet_queue.put_nowait((self.cam_id, stream_type, data, pts))
                    except queue.Full:
                        pass

            except Exception as e:
                print(f"[Cam {self.cam_id}] Ошибка потока: {e}")
                self._disconnect()
                time.sleep(RECONNECT_DELAY)

        self._disconnect()
        print(f"[Cam {self.cam_id}] Остановлен")


class CameraServer:
    """Главный сервер."""

    def __init__(self):
        self.stop_event = threading.Event()
        self.packet_queue = queue.Queue(maxsize=400)
        self.workers: Dict[int, CameraWorker] = {}
        self.client_sock: Optional[socket.socket] = None
        self.client_lock = threading.Lock()
        self._server_sock: Optional[socket.socket] = None

    def _start_workers(self):
        for cam_id, conf in CAMERAS.items():
            if not conf.get("enabled", True):
                continue
            worker = CameraWorker(cam_id, conf["rtsp"], self.packet_queue, self.stop_event)
            worker.start()
            self.workers[cam_id] = worker
            print(f"Запущен воркер камеры {cam_id} ({conf['name']})")

    def _send_packet(self, sock: socket.socket, cam_id: int, stream_type: int, data: bytes, pts: int = 0):
        header = struct.pack(HEADER_FMT, cam_id, stream_type, len(data), pts)
        sock.sendall(header + data)

    def _client_handler(self, sock: socket.socket, addr):
        print(f"Клиент подключился: {addr}")
        with self.client_lock:
            self.client_sock = sock

        try:
            while not self.stop_event.is_set():
                try:
                    cam_id, stream_type, data, pts = self.packet_queue.get(timeout=1.0)
                except queue.Empty:
                    continue

                try:
                    self._send_packet(sock, cam_id, stream_type, data, pts)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    print("Клиент отключился")
                    break
        finally:
            with self.client_lock:
                if self.client_sock is sock:
                    self.client_sock = None
            try:
                sock.close()
            except Exception:
                pass
            print(f"Клиент {addr} отключён")

    def start(self):
        print("=" * 60)
        print("Camera Server (stream-copy + TCP + PTS)")
        print("=" * 60)

        self._start_workers()

        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind((SERVER_HOST, SERVER_PORT))
        self._server_sock.listen(1)
        self._server_sock.settimeout(1.0)

        print(f"Сервер слушает {SERVER_HOST}:{SERVER_PORT}")
        print("Ожидание клиента... (Ctrl+C для остановки)")

        try:
            while not self.stop_event.is_set():
                try:
                    client, addr = self._server_sock.accept()
                    t = threading.Thread(target=self._client_handler, args=(client, addr), daemon=True)
                    t.start()
                except socket.timeout:
                    continue
        except KeyboardInterrupt:
            print("\nОстановка сервера...")
        finally:
            self.stop()

    def stop(self):
        self.stop_event.set()
        if self._server_sock:
            try:
                self._server_sock.close()
            except Exception:
                pass
        for w in self.workers.values():
            w.join(timeout=2.0)
        print("Сервер остановлен")


if __name__ == "__main__":
    server = CameraServer()
    server.start()