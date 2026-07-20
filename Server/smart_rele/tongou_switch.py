#!/usr/bin/env python3
"""
Программа управления умным выключателем TONGOU TO-Q-SY2-JWT (Wi-Fi, протокол 3.5)
С поддержкой включения/выключения и считывания параметров энергопотребления.
"""

import tinytuya
import sys
import time
import logging
from typing import Optional, Dict

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('switch_control.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


class TongouSwitch:
    def __init__(self):
        # ВАШИ ДАННЫЕ ИЗ devices.json (уже вставлены)
        self.device_id = "bfb1c8ae96a0f61b84fphs"
        self.local_key = "gcFlH4i4PgO+`O#4"
        self.ip_address = "192.168.137.193"
        self.device: Optional[tinytuya.OutletDevice] = None
        
    def connect(self) -> bool:
        try:
            logger.info(f"Подключение к {self.ip_address} (ID: {self.device_id})...")
            self.device = tinytuya.OutletDevice(
                dev_id=self.device_id,
                address=self.ip_address,
                local_key=self.local_key
            )
            # КРИТИЧЕСКИ ВАЖНО: версия 3.5 для вашего устройства
            self.device.set_version(3.5)
            
            # Проверка соединения
            status = self.device.status()
            if status and 'Error' not in status:
                logger.info("✓ Успешно подключено к выключателю")
                return True
            else:
                logger.error(f"✗ Ошибка подключения: {status}")
                return False
        except Exception as e:
            logger.error(f"✗ Критическая ошибка подключения: {e}")
            return False
    
    def turn_on(self) -> bool:
        return self._set_switch(True)

    def turn_off(self) -> bool:
        return self._set_switch(False)

    def _set_switch(self, state: bool) -> bool:
        if not self.device:
            return False
        try:
            action = "ВКЛЮЧИТЬ" if state else "ВЫКЛЮЧИТЬ"
            logger.info(f"Отправка команды: {action}")
            result = self.device.set_status(state)
            if result and 'Error' not in result:
                logger.info(f"✓ Выключатель успешно {action}")
                return True
            logger.error(f"✗ Ошибка: {result}")
            return False
        except Exception as e:
            logger.error(f"✗ Критическая ошибка: {e}")
            return False

    def get_status(self) -> Optional[bool]:
        if not self.device:
            return None
        try:
            status = self.device.status()
            if status and 'Error' not in status:
                # DPS 1 отвечает за основной переключатель (switch)
                is_on = status.get('dps', {}).get('1', False)
                logger.info(f"Текущее состояние реле: {'ВКЛЮЧЕНО' if is_on else 'ВЫКЛЮЧЕНО'}")
                return is_on
            return None
        except Exception as e:
            logger.error(f"Ошибка получения статуса: {e}")
            return None

    def get_metering(self) -> Optional[Dict]:
        """Считывает данные с датчиков энергопотребления (DPS 18, 19, 20)"""
        if not self.device:
            return None
        try:
            status = self.device.status()
            if status and 'Error' not in status:
                dps = status.get('dps', {})
                # Согласно вашей карте DPS:
                # 18: cur_current (mA)
                # 19: cur_power (W * 10, т.е. нужно делить на 10)
                # 20: cur_voltage (V * 10, т.е. нужно делить на 10)
                
                current_ma = dps.get('18', 0)
                power_w = dps.get('19', 0) / 10.0
                voltage_v = dps.get('20', 0) / 10.0
                
                logger.info("📊 Данные энергопотребления:")
                logger.info(f"   ⚡ Напряжение: {voltage_v:.1f} В")
                logger.info(f"   🔌 Ток: {current_ma} мА ({current_ma/1000:.2f} А)")
                logger.info(f"   💡 Мощность: {power_w:.1f} Вт")
                
                return {"voltage": voltage_v, "current": current_ma, "power": power_w}
            return None
        except Exception as e:
            logger.error(f"Ошибка получения данных метрики: {e}")
            return None

    def disconnect(self):
        if self.device:
            self.device = None


def main():
    if len(sys.argv) < 2:
        print("\nИспользование:")
        print("  python tongou_switch.py on       - Включить")
        print("  python tongou_switch.py off      - Выключить")
        print("  python tongou_switch.py status   - Проверить состояние реле")
        print("  python tongou_switch.py meter    - Показать напряжение, ток и мощность")
        print("  python tongou_switch.py all      - Состояние + метрика")
        sys.exit(1)
    
    command = sys.argv[1].lower()
    switch = TongouSwitch()
    
    try:
        if not switch.connect():
            logger.error("Не удалось подключиться. Проверьте IP-адрес и сеть.")
            sys.exit(1)
        
        success = False
        if command == "on":
            success = switch.turn_on()
        elif command == "off":
            success = switch.turn_off()
        elif command == "status":
            status = switch.get_status()
            success = status is not None
        elif command == "meter":
            metrics = switch.get_metering()
            success = metrics is not None
        elif command == "all":
            switch.get_status()
            switch.get_metering()
            success = True
        else:
            logger.error(f"Неизвестная команда: {command}")
        
        time.sleep(0.5)
        sys.exit(0 if success else 1)
        
    except KeyboardInterrupt:
        logger.info("\nПрервано пользователем")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Непредвиденная ошибка: {e}")
        sys.exit(1)
    finally:
        switch.disconnect()



if __name__ == "__main__":
    main()