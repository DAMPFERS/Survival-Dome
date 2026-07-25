"""
tcp_client.py — TCP-клиент для приёма сейсмических данных.

Подключается к серверу и читает бинарные чанки по протоколу из protocol.py.
Работает в отдельном потоке, чтобы не блокировать GUI.
"""
import socket
import threading
from typing import Optional
from PyQt6.QtCore import QThread, pyqtSignal
from ..common.protocol import HEADER_SIZE, unpack_header, unpack_payload


class TCPClientThread(QThread):
    """
    TCP-клиент в отдельном потоке.
    
    Читает бинарные чанки с сервера и отправляет их через сигнал chunk_received.
    """
    
    chunk_received = pyqtSignal(dict)  # {'sensor_id': str, 'timestamp': float, 'Z': np.ndarray, 'N': np.ndarray, 'E': np.ndarray}
    connection_status = pyqtSignal(str)  # 'connected' / 'disconnected' / 'error: ...'
    
    def __init__(self, host: str = '127.0.0.1', port: int = 9001, parent=None):
        super().__init__(parent)
        self.host = host
        self.port = port
        self._socket: Optional[socket.socket] = None
    
    def run(self):
        """Основной цикл клиента: подключение и чтение чанков."""
        retry_delay = 1.0
        
        while not self.isInterruptionRequested():
            try:
                # Подключаемся к серверу
                self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self._socket.settimeout(5.0)
                self._socket.connect((self.host, self.port))
                self._socket.settimeout(None)  # блокирующий режим для чтения
                
                print(f"[TCPClient] Connected to {self.host}:{self.port}")
                self.connection_status.emit('connected')
                retry_delay = 1.0  # сбрасываем задержку после успешного подключения
                
                # Читаем чанки
                while not self.isInterruptionRequested():
                    chunk = self._read_chunk()
                    if chunk is None:
                        break  # соединение закрыто
                    self.chunk_received.emit(chunk)
                
            except (ConnectionRefusedError, socket.timeout, OSError) as e:
                print(f"[TCPClient] Connection error: {e}")
                self.connection_status.emit(f'error: {e}')
            finally:
                if self._socket:
                    try:
                        self._socket.close()
                    except:
                        pass
                    self._socket = None
                self.connection_status.emit('disconnected')
            
            # Ждём перед повторной попыткой (с экспоненциальной задержкой)
            if not self.isInterruptionRequested():
                self._interruptible_sleep(retry_delay)
                retry_delay = min(retry_delay * 1.5, 10.0)  # макс 10 секунд
    
    def _read_chunk(self) -> Optional[dict]:
        """
        Читает один чанк из сокета.
        
        Returns:
            dict: {'sensor_id': str, 'timestamp': float, 'Z': np.ndarray, 'N': np.ndarray, 'E': np.ndarray}
            None: если соединение закрыто
        """
        # Читаем заголовок
        header_bytes = self._recv_exact(HEADER_SIZE)
        if header_bytes is None:
            return None
        
        try:
            header = unpack_header(header_bytes)
        except ValueError as e:
            print(f"[TCPClient] Invalid header: {e}")
            return None
        
        # Читаем тело
        payload_bytes = self._recv_exact(header['payload_len'])
        if payload_bytes is None:
            return None
        
        try:
            payload = unpack_payload(payload_bytes, header['n_samples'])
        except ValueError as e:
            print(f"[TCPClient] Invalid payload: {e}")
            return None
        
        # Собираем результат
        return {
            'sensor_id': header['sensor_id'],
            'timestamp': header['timestamp'],
            **payload,
        }
    
    def _recv_exact(self, n_bytes: int) -> Optional[bytes]:
        """
        Читает ровно n_bytes из сокета.
        
        Returns:
            bytes: прочитанные данные
            None: если соединение закрыто до чтения всех данных
        """
        data = bytearray()
        while len(data) < n_bytes:
            try:
                chunk = self._socket.recv(n_bytes - len(data))
                if not chunk:
                    return None  # соединение закрыто
                data.extend(chunk)
            except (ConnectionResetError, OSError):
                return None
        return bytes(data)
    
    def _interruptible_sleep(self, seconds: float):
        """Спит с проверкой прерывания."""
        ms_total = int(seconds * 1000)
        slept = 0
        while slept < ms_total and not self.isInterruptionRequested():
            chunk = min(100, ms_total - slept)
            QThread.msleep(chunk)
            slept += chunk