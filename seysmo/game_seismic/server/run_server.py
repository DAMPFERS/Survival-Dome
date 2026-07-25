"""
run_server.py — точка входа для сервера-эмулятора.

Запуск: python run_server.py
"""
import sys
import queue
import random
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget,
                              QVBoxLayout, QHBoxLayout, QPushButton, QLabel)
from PyQt6.QtCore import QTimer
from ..common.seismic_core import Sensor
from .seismic_generator import SeismicDataGenerator
from ..common.protocol import pack_chunk
from .tcp_server import TCPServerThread


class SeismicServerWindow(QMainWindow):
    """Главное окно сервера-эмулятора."""
    
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Сейсмический сервер-эмулятор")
        self.resize(600, 400)
        
        # Создаём 3 датчика
        self.sensors = [
            Sensor(id="STA_01", x=10.0, y=15.0, sampling_rate=100.0),
            Sensor(id="STA_02", x=25.0, y=8.0, sampling_rate=100.0),
            Sensor(id="STA_03", x=18.0, y=22.0, sampling_rate=100.0),
        ]
        
        # Очередь для передачи чанков от генератора к TCP-серверу
        self.chunk_queue = queue.Queue(maxsize=100)
        
        # Генератор сейсмических данных
        self.generator = SeismicDataGenerator(sensors=self.sensors)
        self.generator.new_chunk.connect(self._on_new_chunk)
        self.generator.start()
        
        # TCP-сервер
        self.tcp_server = TCPServerThread(self.chunk_queue, 
                                          host='127.0.0.1', port=9001)
        self.tcp_server.start()
        
        # GUI
        self._build_ui()
        
        # Таймер авто-событий (каждые 45 секунд)
        self.auto_timer = QTimer(self)
        self.auto_timer.timeout.connect(self._on_random_earthquake)
        self.auto_timer.start(45000)
        
        # Первое событие через 5 секунд
        QTimer.singleShot(5000, self._on_specific_earthquake)
        
        # Таймер обновления статуса клиентов (1 Гц)
        self.client_timer = QTimer(self)
        self.client_timer.timeout.connect(self._update_client_status)
        self.client_timer.start(1000)
    
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        
        # Панель управления
        control = QHBoxLayout()
        
        btn_random = QPushButton("🌍 Случайное землетрясение")
        btn_random.clicked.connect(self._on_random_earthquake)
        control.addWidget(btn_random)
        
        btn_specific = QPushButton("📍 Заданное событие (M6.5, центр)")
        btn_specific.clicked.connect(self._on_specific_earthquake)
        control.addWidget(btn_specific)
        
        control.addStretch()
        
        self.status_label = QLabel("Сервер запущен на 127.0.0.1:9001")
        self.status_label.setStyleSheet("font-size: 12px; color: #888;")
        control.addWidget(self.status_label)
        
        layout.addLayout(control)
        
        # Информация о датчиках
        info = QLabel("Датчики:\n" + "\n".join(
            f"  {s.id}: ({s.x:.1f}, {s.y:.1f}) км, {s.sampling_rate:.0f} Гц" 
            for s in self.sensors
        ))
        info.setStyleSheet("font-size: 13px; padding: 10px; background: #2a2a2a; border-radius: 4px;")
        layout.addWidget(info)
        
        layout.addStretch()
        
        # Подсказка
        hint = QLabel("💡 Запустите клиент (run_client.py) для подключения к серверу")
        hint.setStyleSheet("font-size: 12px; color: #aaa; padding: 10px;")
        layout.addWidget(hint)
    
    def _on_new_chunk(self, payload: dict):
        """
        Слот: получает чанк от генератора и упаковывает в бинарный формат.
        
        payload: dict sensor_id -> {axis: (t_array, acc_array)}
        """
        for sensor_id, axes_data in payload.items():
            # Время одинаковое для всех осей
            t_chunk = axes_data['Z'][0]
            n_samples = len(t_chunk)
            timestamp = float(t_chunk[0])
            
            z = axes_data['Z'][1]
            n = axes_data['N'][1]
            e = axes_data['E'][1]
            
            # Упаковываем в бинарный формат
            chunk_bytes = pack_chunk(sensor_id, timestamp, n_samples, z, n, e)
            
            # Кладём в очередь (если переполнена — пропускаем)
            try:
                self.chunk_queue.put_nowait(chunk_bytes)
            except queue.Full:
                pass  # клиент не успевает читать, пропускаем чанк
    
    def _on_random_earthquake(self):
        """Случайное землетрясение в пределах 50×50 км."""
        x = random.uniform(0, 50)
        y = random.uniform(0, 50)
        depth = random.uniform(5, 30)
        magnitude = random.uniform(3.0, 7.0)
        
        self.generator.trigger_earthquake(x=x, y=y, depth=depth, magnitude=magnitude)
        self.status_label.setText(
            f"Событие: ({x:.1f}, {y:.1f}) км, глубина {depth:.1f} км, M{magnitude:.1f}"
        )
    
    def _on_specific_earthquake(self):
        """Заданное событие в центре области датчиков."""
        x, y, depth, magnitude = 18.0, 15.0, 12.0, 6.5
        self.generator.trigger_earthquake(x=x, y=y, depth=depth, magnitude=magnitude)
        self.status_label.setText(
            f"Заданное событие: ({x:.1f}, {y:.1f}) км, глубина {depth:.1f} км, M{magnitude:.1f}"
        )
    
    def _update_client_status(self):
        """Обновляет статус подключённых клиентов."""
        count = self.tcp_server.get_client_count()
        base = "Сервер запущен на 127.0.0.1:9001"
        if count > 0:
            self.status_label.setText(f"{base} | Клиентов: {count}")
        else:
            self.status_label.setText(f"{base} | Ожидание подключений...")
    
    def closeEvent(self, event):
        """Корректное завершение всех потоков при закрытии окна."""
        self.auto_timer.stop()
        self.client_timer.stop()
        
        self.generator.stop()
        self.tcp_server.requestInterruption()
        
        if not self.generator.wait(2000):
            print("[Server] WARNING: генератор не завершился, принудительно...")
            self.generator.terminate()
            self.generator.wait(500)
        
        if not self.tcp_server.wait(2000):
            print("[Server] WARNING: TCP-сервер не завершился, принудительно...")
            self.tcp_server.terminate()
            self.tcp_server.wait(500)
        
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = SeismicServerWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()