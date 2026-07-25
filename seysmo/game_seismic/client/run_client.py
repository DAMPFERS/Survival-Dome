"""
run_client.py — точка входа для клиента-аналитика.

Запуск: python run_client.py
"""
import sys
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget,
                              QVBoxLayout, QHBoxLayout, QPushButton, QLabel)
from ..common.seismic_core import Sensor
from .seismic_client_widget import SeismicClientWidget


class SeismicClientWindow(QMainWindow):
    """Главное окно клиента-аналитика."""
    
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Сейсмический клиент-аналитик")
        self.resize(1600, 900)
        
        # Создаём 3 датчика (те же координаты, что на сервере)
        self.sensors = [
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
        
        self.status_info = QLabel("Клиент запущен")
        self.status_info.setStyleSheet("font-size: 12px; color: #888;")
        control_layout.addWidget(self.status_info)
        
        main_layout.addLayout(control_layout)
        
        # Сам виджет с графиками
        self.seismic_widget = SeismicClientWidget(
            sensors=self.sensors,
            tcp_host='127.0.0.1',
            tcp_port=9001,
            history_window=60.0
        )
        main_layout.addWidget(self.seismic_widget, stretch=1)
    
    def closeEvent(self, event):
        """Корректное завершение при закрытии окна."""
        self.seismic_widget.shutdown()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = SeismicClientWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()