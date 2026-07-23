#!/usr/bin/env python3
"""
Программа считывания данных с умного датчика CO2 (Wi-Fi, протокол 3.4)
Получение уровня CO2, температуры и влажности.
"""
import tinytuya
import sys
import logging
from typing import Optional, Dict

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('co2_meter.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

class Co2Meter:
    def __init__(self):
        # ДАННЫЕ ИЗ devices.json (пробелы в конце удалены для корректной работы tinytuya)
        self.device_id = "bf00b61720d1979fc25jss"
        self.local_key = "k2X#2<EQ]j@n{/=c"
        self.ip_address = "192.168.0.189"
        self.device: Optional[tinytuya.OutletDevice] = None

    def connect(self) -> bool:
        try:
            logger.info(f"Подключение к {self.ip_address} (ID: {self.device_id})...")
            self.device = tinytuya.OutletDevice(
                dev_id=self.device_id,
                address=self.ip_address,
                local_key=self.local_key
            )
            # КРИТИЧЕСКИ ВАЖНО: версия 3.4 для вашего датчика
            self.device.set_version(3.4)
            
            # Проверка соединения
            status = self.device.status()
            if status and 'Error' not in status:
                logger.info("✓ Успешно подключено к датчику")
                return True
            else:
                logger.error(f"✗ Ошибка подключения: {status}")
                return False
        except Exception as e:
            logger.error(f"✗ Критическая ошибка подключения: {e}")
            return False

    def get_sensor_data(self) -> Optional[Dict]:
        """Считывает данные с датчиков CO2, температуры и влажности (DPS 2, 18, 19)"""
        if not self.device:
            return None
        try:
            status = self.device.status()
            if status and 'Error' not in status:
                dps = status.get('dps', {})
                
                # Согласно карте DPS из devices.json:
                # 2: co2_value (ppm, scale 0)
                # 18: temp_current (°C, scale 1 -> делим на 10)
                # 19: humidity_value (%, scale 1 -> делим на 10)
                co2_ppm = dps.get('2', 0)
                temp_c = dps.get('18', 0) / 10.0
                humidity_pct = dps.get('19', 0) / 10.0
                
                logger.info("🌡️ Данные с датчиков:")
                logger.info(f"   💨 CO2: {co2_ppm} ppm")
                logger.info(f"   🌡️ Температура: {temp_c:.1f} °C")
                logger.info(f"   💧 Влажность: {humidity_pct:.1f} %")
                
                return {
                    "co2": co2_ppm,
                    "temperature": temp_c,
                    "humidity": humidity_pct
                }
            return None
        except Exception as e:
            logger.error(f"Ошибка получения данных: {e}")
            return None

    def get_device_status(self) -> Optional[Dict]:
        """Считывает служебные статусы устройства (тревога, прогрев, самопроверка)"""
        if not self.device:
            return None
        try:
            status = self.device.status()
            if status and 'Error' not in status:
                dps = status.get('dps', {})
                
                co2_state = dps.get('1', 'unknown')
                self_checking = dps.get('8', False)
                preheat = dps.get('10', False)
                alarm_switch = dps.get('13', False)
                
                logger.info("📟 Статус устройства:")
                logger.info(f"   🚦 Состояние CO2: {co2_state}")
                logger.info(f"   🔬 Самопроверка: {'Да' if self_checking else 'Нет'}")
                logger.info(f"   🔥 Прогрев датчика: {'Да' if preheat else 'Нет'}")
                logger.info(f"   🔔 Звуковая сигнализация: {'Вкл' if alarm_switch else 'Выкл'}")
                
                return {
                    "co2_state": co2_state,
                    "self_checking": self_checking,
                    "preheat": preheat,
                    "alarm_switch": alarm_switch
                }
            return None
        except Exception as e:
            logger.error(f"Ошибка получения статуса устройства: {e}")
            return None

    def disconnect(self):
        if self.device:
            self.device = None

def main():
    if len(sys.argv) < 2:
        print("\nИспользование:")
        print("  python co2_meter.py data    - Показать CO2, температуру и влажность")
        print("  python co2_meter.py status  - Показать статус устройства (прогрев, тревога)")
        print("  python co2_meter.py all     - Показать все данные")
        sys.exit(1)

    command = sys.argv[1].lower()
    meter = Co2Meter()

    try:
        if not meter.connect():
            logger.error("Не удалось подключиться. Проверьте IP-адрес и сеть.")
            sys.exit(1)

        success = False
        if command == "data":
            data = meter.get_sensor_data()
            success = data is not None
        elif command == "status":
            status = meter.get_device_status()
            success = status is not None
        elif command == "all":
            meter.get_sensor_data()
            meter.get_device_status()
            success = True
        else:
            logger.error(f"Неизвестная команда: {command}")

        sys.exit(0 if success else 1)

    except KeyboardInterrupt:
        logger.info("\nПрервано пользователем")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Непредвиденная ошибка: {e}")
        sys.exit(1)
    finally:
        meter.disconnect()

if __name__ == "__main__":
    main()