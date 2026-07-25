"""
tcp_server.py — TCP-сервер для передачи сейсмических данных.

Принимает чанки из queue.Queue и отправляет подключённым клиентам.
Поддерживает несколько одновременных подключений.
"""
import socket
import threading
import queue
from typing import List, Tuple
from PyQt6.QtCore import QThread


class TCPServerThread(QThread):
    """
    TCP-сервер в отдельном потоке.
    
    Читает бинарные чанки из chunk_queue и рассылает их всем подключённым клиентам.
    """
    
    def __init__(self, chunk_queue: queue.Queue, 
                 host: str = '127.0.0.1', port: int = 9001, parent=None):
        super().__init__(parent)
        self.chunk_queue = chunk_queue
        self.host = host
        self.port = port
        
        self._clients: List[Tuple[socket.socket, threading.Thread]] = []
        self._clients_lock = threading.Lock()
        self._server_socket: socket.socket = None
    
    def run(self):
        """Основной цикл сервера: принимает подключения и запускает обработчики."""
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.bind((self.host, self.port))
        self._server_socket.listen(5)
        self._server_socket.settimeout(1.0)  # для проверки isInterruptionRequested()
        
        print(f"[TCPServer] Listening on {self.host}:{self.port}")
        
        while not self.isInterruptionRequested():
            try:
                client_sock, addr = self._server_socket.accept()
                print(f"[TCPServer] Client connected from {addr}")
                
                # Запускаем поток для обслуживания клиента
                handler = threading.Thread(
                    target=self._handle_client, 
                    args=(client_sock, addr), 
                    daemon=True
                )
                handler.start()
                
                with self._clients_lock:
                    self._clients.append((client_sock, handler))
                    
            except socket.timeout:
                continue  # проверяем isInterruptionRequested()
            except OSError:
                break  # сокет закрыт
        
        # Завершение: закрываем все подключения
        print("[TCPServer] Shutting down...")
        with self._clients_lock:
            for sock, _ in self._clients:
                try:
                    sock.close()
                except:
                    pass
        if self._server_socket:
            self._server_socket.close()
        print("[TCPServer] Shutdown complete")
    
    def _handle_client(self, sock: socket.socket, addr):
        """Обслуживает одного клиента: читает из очереди и отправляет."""
        try:
            while not self.isInterruptionRequested():
                # Ждём чанк из очереди (с таймаутом для проверки прерывания)
                try:
                    chunk = self.chunk_queue.get(timeout=1.0)
                except queue.Empty:
                    continue
                
                # Отправляем чанк
                try:
                    sock.sendall(chunk)
                except (BrokenPipeError, ConnectionResetError, OSError) as e:
                    print(f"[TCPServer] Client {addr} disconnected: {e}")
                    break
        finally:
            sock.close()
            with self._clients_lock:
                self._clients = [(s, h) for s, h in self._clients if s != sock]
            print(f"[TCPServer] Client handler for {addr} stopped")
    
    def get_client_count(self) -> int:
        """Возвращает число подключённых клиентов."""
        with self._clients_lock:
            return len(self._clients)