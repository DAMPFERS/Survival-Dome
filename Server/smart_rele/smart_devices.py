#!/usr/bin/env python3
"""
Модуль управления 8 умными выключателями TONGOU TO-Q-SY2-JWT.
Работает в фоновом потоке, обеспечивает потокобезопасный доступ к данным.
"""
import threading
import time
import logging
import sys
from dataclasses import dataclass
from typing import Optional, Dict, List
import tinytuya


# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('switch_manager.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SwitchConfig:
    """Конфигурация одного выключателя."""
    label: str              # Метка: "L1", "L2", ..., "L8"
    device_id: str          # ID устройства Tuya
    local_key: str          # Локальный ключ
    ip_address: str         # IP-адрес в локальной сети


class TongouSwitchManager:
    """
    Менеджер 8 выключателей TONGOU.
    
    Работает в фоновом daemon-потоке, периодически опрашивает устройства
    и хранит актуальные данные с потокобезопасным доступом.
    """
    
    def __init__(self, configs: List[SwitchConfig], poll_interval: float):
        """
        Инициализация менеджера.
        
        Args:
            configs: Список из 8 конфигураций выключателей
            poll_interval: Интервал опроса в секундах
        """
        if len(configs) != 8:
            raise ValueError(f"Ожидается ровно 8 выключателей, получено {len(configs)}")
        
        self._configs = {cfg.label: cfg for cfg in configs}
        self._poll_interval = poll_interval
        
        # Потокобезопасное хранилище данных
        self._lock = threading.Lock()
        self._data: Dict[str, Dict[str, float]] = {
            label: {
                "power": 0.0,      # кВт
                "current": 0.0,    # А
                "voltage": 0.0,    # В
                "state": False     # Состояние реле (вкл/выкл)
            }
            for label in self._configs.keys()
        }
        
        # Управление потоком
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
    
    def start(self) -> None:
        """Запуск фонового потока опроса."""
        if self._thread and self._thread.is_alive():
            logger.warning("Поток уже запущен")
            return
        
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="TongouSwitchManager")
        self._thread.start()
        logger.info(f"Поток опроса запущен (интервал: {self._poll_interval}с)")
    
    def stop(self) -> None:
        """Остановка фонового потока."""
        if not self._thread or not self._thread.is_alive():
            return
        
        logger.info("Остановка потока опроса...")
        self._stop_event.set()
        self._thread.join(timeout=5.0)
        logger.info("Поток остановлен")
    
    def _poll_loop(self) -> None:
        """Основной цикл опроса всех устройств."""
        logger.info("Цикл опроса запущен")
        
        while not self._stop_event.is_set():
            for label, config in self._configs.items():
                if self._stop_event.is_set():
                    break
                self._poll_device(label, config)
            
            # Ожидаем следующий цикл или сигнала остановки
            self._stop_event.wait(self._poll_interval)
        
        logger.info("Цикл опроса завершён")
    
    def _poll_device(self, label: str, config: SwitchConfig) -> None:
        """Опрос одного устройства."""
        try:
            device = tinytuya.OutletDevice(
                dev_id=config.device_id,
                address=config.ip_address,
                local_key=config.local_key
            )
            device.set_version(3.5)
            
            status = device.status()
            if not status or 'Error' in status:
                logger.warning(f"[{label}] - Ошибка опроса: {status}")
                return
            
            dps = status.get('dps', {})
            
            # Извлечение данных (DPS 18, 19, 20, 1)
            current_ma = dps.get('18', 0)
            power_w = dps.get('19', 0) / 10.0
            voltage_v = dps.get('20', 0) / 10.0
            state = dps.get('1', False)
            
            # Преобразование единиц
            power_kw = power_w / 1000.0
            current_a = current_ma / 1000.0
            
            
            # Потокобезопасное обновление данных
            with self._lock:
                self._data[label]["power"] = power_kw
                self._data[label]["current"] = current_a
                self._data[label]["voltage"] = voltage_v
                self._data[label]["state"] = state
            
            logger.debug(
                f"[{label}] ⚡{voltage_v:.1f}В | 🔌{current_a:.2f}А | "
                f"💡{power_kw:.3f}кВт | {'ВКЛ' if state else 'ВЫКЛ'}"
            )
        
        except Exception as e:
            logger.error(f"[{label}] - Критическая ошибка опроса: {e}")
    
    # ==================== ГЕТТЕРЫ ====================
    
    def get_power(self, label: str) -> float:
        """Получить мощность в кВт."""
        with self._lock:
            return self._data.get(label, {}).get("power", 0.0)
    
    def get_current(self, label: str) -> float:
        """Получить силу тока в А."""
        with self._lock:
            return self._data.get(label, {}).get("current", 0.0)
    
    def get_voltage(self, label: str) -> float:
        """Получить напряжение в В."""
        with self._lock:
            return self._data.get(label, {}).get("voltage", 0.0)
    
    def get_switch_state(self, label: str) -> bool:
        """Получить состояние выключателя (вкл/выкл)."""
        with self._lock:
            return self._data.get(label, {}).get("state", False)
    
    def get_all_states(self) -> Dict[str, bool]:
        """Получить состояния всех выключателей."""
        with self._lock:
            return {label: data["state"] for label, data in self._data.items()}
    
    def get_all_data(self) -> Dict[str, Dict[str, float]]:
        """Получить все данные (копия)."""
        with self._lock:
            return {label: data.copy() for label, data in self._data.items()}
    
    # ==================== СЕТТЕРЫ ====================
    
    def set_switch(self, label: str, state: bool) -> bool:
        """
        Установить состояние выключателя.
        
        Args:
            label: Метка выключателя ("L1"-"L8")
            state: True - включить, False - выключить
        
        Returns:
            True если команда успешно отправлена
        """
        config = self._configs.get(label)
        if not config:
            logger.error(f"Неизвестная метка: {label}")
            return False
        
        try:
            device = tinytuya.OutletDevice(
                dev_id=config.device_id,
                address=config.ip_address,
                local_key=config.local_key
            )
            device.set_version(3.5)
            
            result = device.set_status(state)
            if result and 'Error' not in result:
                # Обновляем флаг состояния
                with self._lock:
                    self._data[label]["state"] = state
                logger.info(f"[{label}] + Выключатель {'включён' if state else 'выключён'}")
                return True
            else:
                logger.error(f"[{label}] - Ошибка установки состояния: {result}")
                return False
        
        except Exception as e:
            logger.error(f"[{label}] - Критическая ошибка: {e}")
            return False
    
    def turn_on(self, label: str) -> bool:
        """Включить выключатель."""
        return self.set_switch(label, True)
    
    def turn_off(self, label: str) -> bool:
        """Выключить выключатель."""
        return self.set_switch(label, False)


# ==================== ПРИМЕР ИСПОЛЬЗОВАНИЯ ====================





L1_CONFIG = SwitchConfig("L1", "bf8c7f04c16e60cf708gns", "_#z.Gi|+[bi-EkLr", "192.168.8.29")
L2_CONFIG = SwitchConfig("L2", "bf1812bfb2d18ce9ddkvsn", "YZ9WKV;+4/7Vzw|'", "192.168.8.30")
L3_CONFIG = SwitchConfig("L3", "bf9322c1705a091c5absxh", "v-<`pWjK1D/.P36Z", "192.168.8.31")
L4_CONFIG = SwitchConfig("L4", "bf191de6c5e2f9b8afyyrk", "r21}Sf*cykbd^MI<", "192.168.8.32")
L5_CONFIG = SwitchConfig("L5", "bfbc598d8717144230hsj1", "rX.B}6i{:Ns~nk17", "192.168.8.26")
L6_CONFIG = SwitchConfig("L6", "bfac2e9d88c981d76e9k2x", "Ao]ULnbA]CnHKuG#", "192.168.8.28")
L7_CONFIG = SwitchConfig("L7", "bf7f8a04d3d6ea04f2nwhq", "yS1o`:e3Po*9[SS+", "192.168.8.33")
L8_CONFIG = SwitchConfig("L8", "bfb1c8ae96a0f61b84fphs", "ESr2pzGG<@~]=w4h", "192.168.8.34")

if __name__ == "__main__":
    # Пример конфигурации 8 выключателей
    
    
    # configs = [
    #     SwitchConfig("L1", "device_id_1", "local_key_1", "192.168.1.101"),
    #     SwitchConfig("L2", "device_id_2", "local_key_2", "192.168.1.102"),
    #     SwitchConfig("L3", "device_id_3", "local_key_3", "192.168.1.103"),
    #     SwitchConfig("L4", "device_id_4", "local_key_4", "192.168.1.104"),
    #     SwitchConfig("L5", "device_id_5", "local_key_5", "192.168.1.105"),
    #     SwitchConfig("L6", "device_id_6", "local_key_6", "192.168.1.106"),
    #     SwitchConfig("L7", "device_id_7", "local_key_7", "192.168.1.107"),
    #     SwitchConfig("L8", "device_id_8", "local_key_8", "192.168.1.108"),
    # ]
    
    configs = [
            L1_CONFIG,
            L2_CONFIG,
            L3_CONFIG,
            L4_CONFIG,
            L5_CONFIG,
            L6_CONFIG,
            L7_CONFIG,
            L8_CONFIG,
    ]
    
    
    # Создание менеджера (опрос каждые 5 секунд)
    manager = TongouSwitchManager(configs, poll_interval=5.0)
    
    try:
        manager.start()
        
        # Демонстрация работы
        time.sleep(2)
        
        # Чтение данных
        print(f"\nМощность L1: {manager.get_power('L1'):.3f} кВт")
        print(f"Ток L2: {manager.get_current('L2'):.2f} А")
        print(f"Напряжение L3: {manager.get_voltage('L3'):.1f} В")
        print(f"Состояние L4: {'ВКЛ' if manager.get_switch_state('L4') else 'ВЫКЛ'}")
        
        # Управление
        manager.turn_on('L1')
        time.sleep(3)
        # manager.turn_off('L6')
        
        # Получение всех состояний
        print("\nВсе состояния:")
        for label, state in manager.get_all_states().items():
            print(f"  {label}: {'ВКЛ' if state else 'ВЫКЛ'}")
        
        # Работа в основном приложении
        while True:
            time.sleep(1)
    
    except KeyboardInterrupt:
        print("\nОстановка...")
    finally:
        manager.stop()