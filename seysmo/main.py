"""
main.py — точка входа для демонстрации SeismicMonitorWidget.
Запуск: python main.py
"""
import sys
import random
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, 
                               QVBoxLayout, QHBoxLayout, QPushButton, QLabel)
from PyQt6.QtCore import QTimer
from seismic_core import Sensor
from seismic_widget import SeismicMonitorWidget


class SeismicDemoWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Сейсмический монитор — демо")
        self.resize(1600, 900)
        
        # Создаём 3 датчика в разных точках
        sensors = [
            Sensor(id="STA_01", x=10.0, y=15.0, sampling_rate=100.0),
            Sensor(id="STA_02", x=25.0, y=8.0, sampling_rate=100.0),
            Sensor(id="STA_03", x=18.0, y=22.0, sampling_rate=100.0),
        ]
        
        # Главный виджет
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(8, 8, 8, 8)
        
        # Панель управления
        control_layout = QHBoxLayout()
        
        self.btn_random = QPushButton("🌍 Случайное землетрясение")
        self.btn_random.clicked.connect(self._on_random_earthquake)
        control_layout.addWidget(self.btn_random)
        
        self.btn_specific = QPushButton("📍 Заданное событие (M6.5, центр)")
        self.btn_specific.clicked.connect(self._on_specific_earthquake)
        control_layout.addWidget(self.btn_specific)
        
        self.btn_axis_z = QPushButton("Ось Z")
        self.btn_axis_z.clicked.connect(lambda: self.seismic_widget.set_axis("Z"))
        control_layout.addWidget(self.btn_axis_z)
        
        self.btn_axis_n = QPushButton("Ось N")
        self.btn_axis_n.clicked.connect(lambda: self.seismic_widget.set_axis("N"))
        control_layout.addWidget(self.btn_axis_n)
        
        self.btn_axis_e = QPushButton("Ось E")
        self.btn_axis_e.clicked.connect(lambda: self.seismic_widget.set_axis("E"))
        control_layout.addWidget(self.btn_axis_e)
        
        control_layout.addStretch()
        
        self.status_info = QLabel("Готово к работе")
        self.status_info.setStyleSheet("font-size: 12px; color: #888;")
        control_layout.addWidget(self.status_info)
        
        main_layout.addLayout(control_layout)
        
        # Сам виджет с графиками
        self.seismic_widget = SeismicMonitorWidget(sensors=sensors, history_window=60.0)
        main_layout.addWidget(self.seismic_widget, stretch=1)
        
        # Таймер для автоматических демо-событий (каждые 45 секунд)
        self.auto_timer = QTimer(self)
        self.auto_timer.timeout.connect(self._on_auto_earthquake)
        self.auto_timer.start(45000)  # 45 секунд
        
        # Первое событие через 5 секунд после запуска
        QTimer.singleShot(5000, self._on_specific_earthquake)
    
    def _on_random_earthquake(self):
        """Случайное землетрясение в пределах 50×50 км"""
        x = random.uniform(0, 50)
        y = random.uniform(0, 50)
        depth = random.uniform(5, 30)
        magnitude = random.uniform(3.0, 7.0)
        
        self.seismic_widget.trigger_earthquake(
            x=x, y=y, depth=depth, magnitude=magnitude
        )
        self.status_info.setText(
            f"Случайное событие: ({x:.1f}, {y:.1f}) км, глубина {depth:.1f} км, M{magnitude:.1f}"
        )
    
    def _on_specific_earthquake(self):
        """Заданное событие в центре области датчиков"""
        x, y, depth, magnitude = 18.0, 15.0, 12.0, 6.5
        self.seismic_widget.trigger_earthquake(
            x=x, y=y, depth=depth, magnitude=magnitude
        )
        self.status_info.setText(
            f"Заданное событие: ({x:.1f}, {y:.1f}) км, глубина {depth:.1f} км, M{magnitude:.1f}"
        )
    
    def _on_auto_earthquake(self):
        """Автоматическое демо-событие"""
        self._on_random_earthquake()
        self.status_info.setText(f"Авто-событие: {self.status_info.text()}")
    
    def closeEvent(self, event):
        """Корректное завершение потока генерации при закрытии окна"""
        self.auto_timer.stop()
        self.seismic_widget.shutdown()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")  # Современный стиль
    
    window = SeismicDemoWindow()
    window.show()
    
    sys.exit(app.exec())


if __name__ == "__main__":
    main()