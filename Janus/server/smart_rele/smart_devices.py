#!/usr/bin/env python3
"""
Единый модуль управления умными устройствами:
  - 8 выключателей TONGOU TO-Q-SY2-JWT (протокол 3.5)
  - 1 датчик CO2 / температуры / влажности (протокол 3.4)

Работает в фоновом daemon-потоке, обеспечивает потокобезопасный
доступ ко всем данным через единый threading.Lock.
"""

import threading
import time
import logging
import sys
from dataclasses import dataclass
from typing import Optional, Dict, List

import tinytuya
import concurrent.futures

# ======================== ЛОГИРОВАНИЕ ========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("smart_devices.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


# ===================== КОНФИГУРАЦИИ ==========================

@dataclass(frozen=True)
class SwitchConfig:
    """Конфигурация одного выключателя."""
    label: str          # Метка: "L1" … "L8"
    device_id: str
    local_key: str
    ip_address: str


@dataclass(frozen=True)
class SensorConfig:
    """Конфигурация датчика CO2 / температуры / влажности."""
    label: str          # Метка, например "CO2_SENSOR"
    device_id: str
    local_key: str
    ip_address: str


# ==================== МЕНЕДЖЕР УСТРОЙСТВ =====================

class SmartDeviceManager:
    """
    Единый менеджер для выключателей и датчика.

    • Один фоновый daemon-поток опрашивает все устройства.
    • Один threading.Lock защищает все разделяемые данные
      (и реле, и датчик) от гонок (race conditions).
    • Частота опроса датчика = частота опроса реле (один цикл).
    """

    def __init__(
        self,
        switch_configs: List[SwitchConfig],
        sensor_config: Optional[SensorConfig] = None,
        poll_interval: float = 5.0,
    ):
        """
        Args:
            switch_configs: Список из 8 конфигураций выключателей.
            sensor_config:  Конфигурация датчика (может быть None).
            poll_interval:  Интервал опроса в секундах.
        """
        if len(switch_configs) != 8:
            raise ValueError(
                f"Ожидается ровно 8 выключателей, получено {len(switch_configs)}"
            )

        self._switch_configs = {cfg.label: cfg for cfg in switch_configs}
        self._sensor_config = sensor_config
        self._poll_interval = poll_interval

        # ---------- Единый мьютекс для ВСЕХ данных ----------
        self._lock = threading.Lock()

        # Хранилище данных выключателей
        self._switch_data: Dict[str, Dict[str, float]] = {
            label: {
                "power": 0.0,     # кВт
                "current": 0.0,   # А
                "voltage": 0.0,   # В
                "state": False,   # вкл / выкл
            }
            for label in self._switch_configs
        }

        # Хранилище данных датчика (защищено тем же _lock)
        self._sensor_data: Dict[str, float] = {
            "co2": 0,             # ppm
            "temperature": 0.0,   # C
            "humidity": 0.0,      # %
        }

        # ---------- Управление потоком ----------
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        
        # НОВОЕ: Кэш объектов устройств для переиспользования соединений
        self._devices: Dict[str, tinytuya.OutletDevice] = {}
        self._device_locks: Dict[str, threading.Lock] = {
            cfg.label: threading.Lock() for cfg in switch_configs
        }
        if sensor_config:
            self._device_locks[sensor_config.label] = threading.Lock()

    # ==================== START / STOP =======================

    
    def _get_or_create_device(self, config) -> tinytuya.OutletDevice:
        """Возвращает кэшированное устройство или создает новое"""
        label = config.label
        if label not in self._devices:
            device = tinytuya.OutletDevice(
                dev_id=config.device_id,
                address=config.ip_address,
                local_key=config.local_key,
            )
            # Устанавливаем версию протокола
            if "SENSOR" in label:
                device.set_version(3.4)
            else:
                device.set_version(3.5)
            self._devices[label] = device
        return self._devices[label]
    
    def _recreate_device(self, config) -> tinytuya.OutletDevice:
        """Пересоздает устройство при ошибке соединения."""
        label = config.label
        if label in self._devices:
            try:
                self._devices[label].close()
            except:
                pass
            del self._devices[label]
        return self._get_or_create_device(config)
    
    
    
    def start(self) -> None:
        """Запуск фонового потока опроса."""
        if self._thread and self._thread.is_alive():
            logger.warning("Поток уже запущен")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            daemon=True,
            name="SmartDeviceManager",
        )
        self._thread.start()
        logger.info(
            f"Поток опроса запущен (интервал: {self._poll_interval}с, "
            f"датчик: {'да' if self._sensor_config else 'нет'})"
        )

    def stop(self) -> None:
        """Остановка фонового потока."""
        if not self._thread or not self._thread.is_alive():
            return
        logger.info("Остановка потока опроса...")
        self._stop_event.set()
        self._thread.join(timeout=5.0)
        logger.info("Поток остановлен")

    # ==================== ЦИКЛ ОПРОСА ========================

    def _poll_loop(self) -> None:
        """
        Основной цикл с ПАРАЛЛЕЛЬНЫМ опросом устройств.
        Порядок: все устройства опрашиваются одновременно -> пауза.
        """
        logger.info("Цикл опроса запущен (РЕЖИМ: ПАРАЛЛЕЛЬНЫЙ)")
        
        # Создаем пул потоков. 10 потоков с запасом хватит на 8 реле + 1 датчик
        with concurrent.futures.ThreadPoolExecutor(max_workers=10, thread_name_prefix="TuyaPoll") as executor:
            while not self._stop_event.is_set():
                futures = []
                
                # 1) Ставим в очередь опрос всех выключателей ОДНОВРЕМЕННО
                for label, config in self._switch_configs.items():
                    futures.append(
                        executor.submit(self._poll_switch, label, config)
                    )
                
                # 2) Ставим в очередь опрос датчика (если есть)
                if self._sensor_config:
                    futures.append(
                        executor.submit(self._poll_sensor)
                    )
                
                # 3) Ждем завершения ВСЕХ запросов этого цикла.
                # Это займет время, равное отклику самого медленного устройства (~0.3-0.5 сек),
                # а не сумму всех откликов (~2.7 сек).
                concurrent.futures.wait(futures)
                
                # 4) Ждем до следующего цикла
                self._stop_event.wait(self._poll_interval)
                
        logger.info("Цикл опроса завершён")

    # ==================== ОПРОС ВЫКЛЮЧАТЕЛЯ ==================
    
    def _poll_switch(self, label: str, config: SwitchConfig) -> None:
        """Опрос одного выключателя с кэшированием соединения."""
        lock = self._device_locks.get(label)
        if not lock:
            return
            
        with lock:  # Блокируем только это конкретное устройство
            try:
                device = self._get_or_create_device(config)
                status = device.status()
                
                if not status or "Error" in status:
                    logger.warning(f"[{label}] Ошибка опроса, пересоздаю соединение...")
                    device = self._recreate_device(config)
                    status = device.status()
                    if not status or "Error" in status:
                        logger.error(f"[{label}] Повторная ошибка: {status}")
                        return
                
                dps = status.get("dps", {})
                current_ma = dps.get("18", 0)
                power_w = dps.get("19", 0) / 10.0
                voltage_v = dps.get("20", 0) / 10.0
                state = dps.get("1", False)
                
                power_kw = power_w / 1.0
                current_a = current_ma / 1000.0
                
                with self._lock:
                    self._switch_data[label]["power"] = power_kw
                    self._switch_data[label]["current"] = current_a
                    self._switch_data[label]["voltage"] = voltage_v
                    self._switch_data[label]["state"] = state
                    
            except Exception as e:
                logger.error(f"[{label}] Критическая ошибка: {e}, пересоздаю соединение...")
                try:
                    self._recreate_device(config)
                except:
                    pass

    # ==================== ОПРОС ДАТЧИКА ======================
    
    def _poll_sensor(self) -> None:
        """Опрос датчика с кэшированием соединения."""
        cfg = self._sensor_config
        if not cfg:
            return
            
        lock = self._device_locks.get(cfg.label)
        if not lock:
            return
            
        with lock:
            try:
                device = self._get_or_create_device(cfg)
                status = device.status()
                
                if not status or "Error" in status:
                    logger.warning(f"[{cfg.label}] Ошибка опроса датчика, пересоздаю...")
                    device = self._recreate_device(cfg)
                    status = device.status()
                    if not status or "Error" in status:
                        logger.error(f"[{cfg.label}] Повторная ошибка: {status}")
                        return
                
                dps = status.get("dps", {})
                co2 = dps.get("2", 0)
                temp = dps.get("18", 0) / 10.0
                hum = dps.get("19", 0) / 10.0
                
                with self._lock:
                    self._sensor_data["co2"] = co2
                    self._sensor_data["temperature"] = temp
                    self._sensor_data["humidity"] = hum
                    
            except Exception as e:
                logger.error(f"[{cfg.label}] Критическая ошибка: {e}, пересоздаю...")
                try:
                    self._recreate_device(cfg)
                except:
                    pass
                
                
    # ==================== ГЕТТЕРЫ (РЕЛЕ) =====================

    def get_power(self, label: str) -> float:
        """Мощность в кВт."""
        with self._lock:
            return self._switch_data.get(label, {}).get("power", 0.0)

    def get_current(self, label: str) -> float:
        """Сила тока в А."""
        with self._lock:
            return self._switch_data.get(label, {}).get("current", 0.0)

    def get_voltage(self, label: str) -> float:
        """Напряжение в В."""
        with self._lock:
            return self._switch_data.get(label, {}).get("voltage", 0.0)

    def get_switch_state(self, label: str) -> bool:
        """Состояние выключателя (вкл/выкл)."""
        with self._lock:
            return self._switch_data.get(label, {}).get("state", False)

    def get_all_states(self) -> Dict[str, bool]:
        """Состояния всех выключателей."""
        with self._lock:
            return {
                label: data["state"]
                for label, data in self._switch_data.items()
            }

    def get_all_switch_data(self) -> Dict[str, Dict[str, float]]:
        """Все данные выключателей (глубокая копия)."""
        with self._lock:
            return {
                label: data.copy()
                for label, data in self._switch_data.items()
            }

    # ==================== ГЕТТЕРЫ (ДАТЧИК) ===================

    def get_co2(self) -> int:
        """Уровень CO2 в ppm."""
        with self._lock:
            return self._sensor_data["co2"]

    def get_temperature(self) -> float:
        """Температура в °C."""
        with self._lock:
            return self._sensor_data["temperature"]

    def get_humidity(self) -> float:
        """Влажность в %."""
        with self._lock:
            return self._sensor_data["humidity"]

    def get_sensor_data(self) -> Dict[str, float]:
        """Все данные датчика (копия)."""
        with self._lock:
            return self._sensor_data.copy()

    # ==================== СЕТТЕРЫ (РЕЛЕ) =====================

    # ==================== СЕТТЕРЫ (РЕЛЕ) =====================
    
    def set_switch(self, label: str, state: bool) -> bool:
        """Установить состояние выключателя с кэшированием."""
        config = self._switch_configs.get(label)
        if not config:
            logger.error(f"Неизвестная метка: {label}")
            return False
            
        lock = self._device_locks.get(label)
        if not lock:
            return False
            
        with lock:
            try:
                device = self._get_or_create_device(config)
                result = device.set_status(state)
                
                if result and "Error" not in result:
                    with self._lock:
                        self._switch_data[label]["state"] = state
                    logger.info(f"[{label}] Выключатель {'включён' if state else 'выключён'}")
                    return True
                else:
                    logger.error(f"[{label}] Ошибка установки состояния: {result}, пересоздаю...")
                    device = self._recreate_device(config)
                    return False
                    
            except Exception as e:
                logger.error(f"[{label}] Критическая ошибка: {e}, пересоздаю...")
                try:
                    self._recreate_device(config)
                except:
                    pass
                return False

    def turn_on(self, label: str) -> bool:
        """Включить выключатель."""
        return self.set_switch(label, True)

    def turn_off(self, label: str) -> bool:
        """Выключить выключатель."""
        return self.set_switch(label, False)


# ==================== КОНФИГУРАЦИЯ УСТРОЙСТВ =================

# --- Выключатели (протокол 3.5) ---
L1 = SwitchConfig("L1", "bf8c7f04c16e60cf708gns", "_#z.Gi|+[bi-EkLr", "192.168.8.29")
L2 = SwitchConfig("L2", "bf1812bfb2d18ce9ddkvsn", "YZ9WKV;+4/7Vzw|'", "192.168.8.30")
L3 = SwitchConfig("L3", "bf9322c1705a091c5absxh", "v-<`pWjK1D/.P36Z", "192.168.8.31")
L4 = SwitchConfig("L4", "bf191de6c5e2f9b8afyyrk", "r21}Sf*cykbd^MI<", "192.168.8.32")
L5 = SwitchConfig("L5", "bfbc598d8717144230hsj1", "rX.B}6i{:Ns~nk17", "192.168.8.26")
L6 = SwitchConfig("L6", "bfac2e9d88c981d76e9k2x", "Ao]ULnbA]CnHKuG#", "192.168.8.28")
L7 = SwitchConfig("L7", "bf7f8a04d3d6ea04f2nwhq", "yS1o`:e3Po*9[SS+", "192.168.8.33")
L8 = SwitchConfig("L8", "bfb1c8ae96a0f61b84fphs", "ESr2pzGG<@~]=w4h", "192.168.8.34")

# --- Датчик CO2 (протокол 3.4) ---
CO2_SENSOR = SensorConfig(
    label="CO2_SENSOR",
    device_id="bf00b61720d1979fc25jss",
    local_key="~99Yv1m9_kXd4Z2&",
    ip_address="192.168.8.27",
)


# ==================== ПРИМЕР ИСПОЛЬЗОВАНИЯ ===================

if __name__ == "__main__":
    switch_configs = [L1, L2, L3, L4, L5, L6, L7, L8]

    manager = SmartDeviceManager(
        switch_configs=switch_configs,
        sensor_config=CO2_SENSOR,
        poll_interval=4.0,
    )

    try:
        manager.start()
        time.sleep(7)  # Ждём хотя бы один полный цикл опроса

        # --- Чтение данных выключателей ---
        print("\n=== ВЫКЛЮЧАТЕЛИ ===")
        print(f"Мощность L6:  {manager.get_power('L6'):.3f} кВт")
        print(f"Ток L6:       {manager.get_current('L6'):.2f} А")
        print(f"Напряжение L6:{manager.get_voltage('L6'):.1f} В")
        print(f"Состояние L6: {'ВКЛ' if manager.get_switch_state('L6') else 'ВЫКЛ'}")
        

        # --- Чтение данных датчика ---
        print("\n=== ДАТЧИК ===")
        print(f"CO2:          {manager.get_co2()} ppm")
        print(f"Температура:  {manager.get_temperature():.1f} °C")
        print(f"Влажность:    {manager.get_humidity():.1f} %")

        # --- Управление ---
        manager.turn_on("L1")
        time.sleep(3)

        print("\nВсе состояния:")
        for label, state in manager.get_all_states().items():
            print(f"  {label}: {'ВКЛ' if state else 'ВЫКЛ'}")

        # Бесконечная работа
        while True:
            for i in range(1,9):
                print(f"Мощность L{i}:  {manager.get_power(f'L{i}'):.2f} Вт")
            time.sleep(4)
            print("------\n")
            

    except KeyboardInterrupt:
        print("\nОстановка...")
    finally:
        manager.stop()