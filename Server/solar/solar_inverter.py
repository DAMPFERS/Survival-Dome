"""
Модуль опроса инвертора MUST PH1800.
Работает в отдельном потоке, предоставляет потокобезопасный доступ
к трём ключевым величинам: потребляемая мощность, генерируемая мощность,
SOC батареи. При ошибках связи автоматически переподключается.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Optional, Tuple

import serial.tools.list_ports
from pymodbus.client import ModbusSerialClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InverterSnapshot:
    """Атомарный снимок всех показаний инвертора.
    Возвращается геттерами под Lock, чтобы избежать рассинхрона между полями.
    """
    consumed_power_kw: Optional[float]      # нагрузка, Вт
    generated_power_kw: Optional[float]     # PV-генерация, Вт
    battery_soc_percent: Optional[float]    # SOC батареи, %
    battery_voltage_v: Optional[float]      # напряжение АКБ, В (для отладки)
    is_online: bool                         # флаг связи с инвертором


class InverterMonitor:
    """Фоновый монитор инвертора MUST PH1800.

    Параметры
    ---------
    poll_interval : float
        Интервал опроса инвертора в секундах.
    port : str | None
        Имя COM-порта. Если None — будет выбран первый доступный порт.
    slave_id : int
        Modbus-адрес инвертора (по умолчанию 4, как у PH1800).
    baudrate : int
        Скорость UART (19200 для PH1800).
    timeout : float
        Таймаут Modbus-запроса в секундах.
    max_battery_voltage : float
        Напряжение батареи, принимаемое за 100% SOC (по умолчанию 24 В).
    max_errors_before_reconnect : int
        Сколько подряд ошибок чтения допустить перед попыткой переподключения.
    max_reconnect_attempts : int
        Сколько раз пытаться переподключиться, прежде чем пометить устройство offline.
    reconnect_interval : float
        Пауза между попытками переподключения, секунды.
    """

    # Адреса регистров (из карты в must_ph1800.py)
    _REG_P_LOAD              = 25215   # PLoad, W
    _REG_CHARGER_POWER       = 15208   # ChargerPower, W (мощность с PV)
    _REG_INVERTER_BATT_VOLT  = 25205   # InverterBatteryVoltage, raw*0.1 = V

    def __init__(
        self,
        poll_interval: float,
        port: Optional[str] = None,
        slave_id: int = 4,
        baudrate: int = 19200,
        timeout: float = 1.0,
        max_battery_voltage: float = 24.0,
        max_errors_before_reconnect: int = 3,
        max_reconnect_attempts: int = 5,
        reconnect_interval: float = 5.0,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval должен быть > 0")

        # --- параметры подключения ---
        self._port = port or self._find_port()
        if self._port is None:
            raise RuntimeError(
                "Не найден ни один COM-порт. Подключите инвертор или укажите port вручную."
            )
        self._slave_id = slave_id
        self._baudrate = baudrate
        self._timeout = timeout

        # --- параметры расчёта SOC ---
        self._max_battery_voltage = max_battery_voltage

        # --- параметры переподключения ---
        self._max_errors_before_reconnect = max_errors_before_reconnect
        self._max_reconnect_attempts = max_reconnect_attempts
        self._reconnect_interval = reconnect_interval
        self._poll_interval = poll_interval

        # --- Modbus-клиент (создаём лениво, в рабочем потоке) ---
        self._client: Optional[ModbusSerialClient] = None

        # --- потокобезопасное хранилище данных ---
        self._data_lock = threading.Lock()
        self._consumed_power_kw: Optional[float] = None
        self._generated_power_kw: Optional[float] = None
        self._battery_soc_percent: Optional[float] = None
        self._battery_voltage_v: Optional[float] = None
        self._is_online: bool = False

        # --- управление потоком ---
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        logger.info(
            f"InverterMonitor инициализирован: port={self._port}, "
            f"slave={self._slave_id}, poll={poll_interval}s, "
            f"Umax={max_battery_voltage}V"
        )

    # ------------------------------------------------------------------
    # Автоопределение порта
    # ------------------------------------------------------------------
    @staticmethod
    def _find_port() -> Optional[str]:
        """Возвращает имя первого доступного COM-порта из списка системы.

        Логика простая: «подключаться к первому в списке».
        Если в будущем понадобится фильтр по VID/PID инвертора —
        это единственное место, которое придётся поправить
        """
        ports = serial.tools.list_ports.comports()
        if not ports:
            logger.warning("В системе не обнаружено ни одного COM-порта")
            return None
        # Сортируем по имени для детерминированности (COM3 раньше COM10)
        ports_sorted = sorted(ports, key=lambda p: p.device)
        chosen = ports_sorted[0]
        logger.info(f"Автовыбор порта: {chosen.device} ({chosen.description})")
        return chosen.device

    # ------------------------------------------------------------------
    # Подключение / переподключение
    # ------------------------------------------------------------------
    def _create_client(self) -> ModbusSerialClient:
        return ModbusSerialClient(
            port=self._port,
            baudrate=self._baudrate,
            stopbits=1,
            bytesize=8,
            parity="N",
            timeout=self._timeout,
        )

    def _connect(self) -> bool:
        """Подключиться (или переподключиться) к инвертору.
        Возвращает True при успехе, False при неудаче.
        """
        # Закрываем старый клиент, если он был
        if self._client is not None:
            try:
                self._client.close()
            except Exception as e:
                logger.debug(f"Ошибка закрытия старого клиента: {e}")
            self._client = None

        self._client = self._create_client()
        try:
            ok = self._client.connect()
        except Exception as e:
            logger.warning(f"Исключение при подключении к {self._port}: {e}")
            ok = False

        if ok:
            logger.info(f"✅ Подключено к {self._port} (slave={self._slave_id})")
        else:
            logger.warning(f"❌ Не удалось подключиться к {self._port}")
        return ok

    def _disconnect(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception as e:
                logger.debug(f"Ошибка закрытия клиента: {e}")
            self._client = None
    
    def _to_signed(self, value: int):
        """Преобразует 16-битное беззнаковое значение в знаковое (int16)."""
        if value > 32767:
            return value - 65536
        return value

    
    # ------------------------------------------------------------------
    # Чтение регистров и обновление атрибутов
    # ------------------------------------------------------------------
    def _read_and_update(self) -> bool:
        """Читает нужные регистры и атомарно обновляет атрибуты под Lock.

        Возвращает True, если все три величины прочитаны успешно.
        При любой ошибке (ответ isError, исключение, None) — возвращает False,
        атрибуты НЕ обновляются (сохраняются последние валидные значения).
        """
        if self._client is None:
            return False

        try:
            # --- Запрос 1: ChargerPower (15208) ---
            resp_pv = self._client.read_holding_registers(
                address=15208, count=1, device_id=self._slave_id
            )
            if resp_pv is None or resp_pv.isError():
                logger.debug(f"Ошибка чтения 15208 (ChargerPower): {resp_pv}")
                return False
            charger_power_w = resp_pv.registers[0]  # W, без множителя

            # --- Запрос 2: InverterBatteryVoltage (25205) + PLoad (25215) ---
            # Диапазон 25205..25215 = 11 регистров.
            # Индексы: 0 → 25205 (U акб), 10 → 25215 (P нагрузки)
            resp_inv = self._client.read_holding_registers(
                address=25205, count=11, device_id=self._slave_id
            )
            if resp_inv is None or resp_inv.isError():
                logger.debug(f"Ошибка чтения 25205..25215: {resp_inv}")
                return False
            if len(resp_inv.registers) < 11:
                logger.debug(f"Недостаточно регистров в ответе: {len(resp_inv.registers)}")
                return False

            battery_voltage_raw = resp_inv.registers[0]   # * 0.1 = В
            p_load_w = resp_inv.registers[10]             # W, без множителя

            battery_voltage_v = battery_voltage_raw * 0.1
            
            
            consumed_kw = self._to_signed(p_load_w) / 1.0
            generated_kw = self._to_signed(charger_power_w) / 1.0

            # Линейная оценка SOC: U / Umax * 100, с ограничением [0; 100]
            soc_percent = max(0.0, min(100.0,
                (battery_voltage_v / self._max_battery_voltage) * 100.0
            ))

            # --- Атомарное обновление под Lock ---
            with self._data_lock:
                self._consumed_power_kw = consumed_kw
                self._generated_power_kw = generated_kw
                self._battery_voltage_v = battery_voltage_v
                self._battery_soc_percent = soc_percent
                self._is_online = True

            return True

        except Exception as e:
            logger.warning(f"Исключение при чтении регистров: {e}")
            return False

    # ------------------------------------------------------------------
    # Управление состоянием online/offline
    # ------------------------------------------------------------------
    def _set_online(self) -> None:
        with self._data_lock:
            self._is_online = True

    def _set_offline(self) -> None:
        """Помечаем устройство offline. Атрибуты НЕ обнуляем —
        пусть UI видит последние валидные значения и флаг is_online=False."""
        with self._data_lock:
            self._is_online = False

    # ------------------------------------------------------------------
    # Переподключение с повторами
    # ------------------------------------------------------------------
    def _try_connect_with_retries(self) -> bool:
        """Пытается подключиться max_reconnect_attempts раз с паузой.
        Возвращает True при успехе, False если все попытки провалены.
        Корректно реагирует на stop_event — прерывает ожидание.
        """
        for attempt in range(1, self._max_reconnect_attempts + 1):
            if self._stop_event.is_set():
                return False
            logger.info(
                f"Попытка подключения {attempt}/{self._max_reconnect_attempts} "
                f"к {self._port}..."
            )
            if self._connect():
                return True
            # Ждём, но с возможностью досрочного выхода по stop_event
            self._stop_event.wait(self._reconnect_interval)
        logger.error(
            f"Все {self._max_reconnect_attempts} попыток подключения провалены. "
            f"Устройство помечено offline."
        )
        return False

    # ------------------------------------------------------------------
    # Главный цикл потока
    # ------------------------------------------------------------------
    def _poll_loop(self) -> None:
        """Рабочий цикл потока опроса.

        Логика:
        1. Если нет соединения — пробуем подключиться с повторами.
           Если не вышло — помечаем offline, ждём reconnect_interval, повторяем.
        2. Если соединение есть — читаем регистры.
           При успехе — сбрасываем счётчик ошибок.
           При ошибке — инкрементируем счётчик; если достигнут лимит,
           разъединяемся и помечаем offline (переход к шагу 1).
        3. Ждём poll_interval (с возможностью досрочного выхода по stop_event).
        """
        logger.info("🔄 Поток опроса запущен")
        self._consecutive_errors = 0
        self._connected = False

        try:
            while not self._stop_event.is_set():
                # --- Шаг 1: обеспечиваем соединение ---
                if not self._connected:
                    if not self._try_connect_with_retries():
                        self._set_offline()
                        # Ждём и попробуем снова (внешний цикл)
                        self._stop_event.wait(self._reconnect_interval)
                        continue
                    self._connected = True

                # --- Шаг 2: читаем регистры ---
                if self._read_and_update():
                    self._consecutive_errors = 0
                    # _read_and_update сам выставил is_online=True при успехе
                else:
                    self._consecutive_errors += 1
                    logger.warning(
                        f"Ошибка опроса ({self._consecutive_errors}/"
                        f"{self._max_errors_before_reconnect})"
                    )
                    if self._consecutive_errors >= self._max_errors_before_reconnect:
                        logger.warning(
                            "Превышен лимит ошибок — переподключаемся..."
                        )
                        self._disconnect()
                        self._connected = False
                        self._set_offline()
                        continue  # сразу на переподключение, не ждём poll_interval

                # --- Шаг 3: ждём интервал ---
                self._stop_event.wait(self._poll_interval)

        except Exception as e:
            logger.exception(f"Необработанное исключение в потоке опроса: {e}")
            self._set_offline()
        finally:
            self._disconnect()
            logger.info("Поток опроса завершён")
        # ------------------------------------------------------------------
    # Публичный API: запуск и остановка
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Запускает поток опроса.

        Raises
        ------
        RuntimeError
            Если поток уже запущен.
        """
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("Поток опроса уже запущен. Вызовите stop() перед повторным start().")
        
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, name="InverterMonitorThread", daemon=True)
        self._thread.start()
        logger.info("▶️ Поток опроса запущен")

    def stop(self, timeout: float = 5.0) -> None:
        """Останавливает поток опроса и ожидает его завершения.

        Parameters
        ----------
        timeout : float
            Максимальное время ожидания завершения потока (секунды).
            Если поток не завершился за это время — логируется предупреждение.
        """
        if self._thread is None:
            logger.debug("Поток не был запущен, stop() проигнорирован")
            return
        
        logger.info("⏹ Запрошена остановка потока...")
        self._stop_event.set()
        
        self._thread.join(timeout=timeout)
        
        if self._thread.is_alive():
            logger.warning(
                f"⚠️ Поток не завершился за {timeout} сек. "
                f"Возможно, Modbus-клиент заблокирован на таймауте."
            )
        else:
            logger.info("✅ Поток опроса корректно завершён")
        
        self._thread = None

    # ------------------------------------------------------------------
    # Контекстный менеджер
    # ------------------------------------------------------------------
    def __enter__(self) -> "InverterMonitor":
        """Позволяет использовать класс с конструкцией `with`."""
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Автоматически останавливает поток при выходе из `with`."""
        self.stop()

    # ------------------------------------------------------------------
    # Потокобезопасные геттеры
    # ------------------------------------------------------------------
    def get_consumed_power_kw(self) -> Optional[float]:
        """Возвращает потребляемую мощность (нагрузка), Вт.
        
        Returns
        -------
        float | None
            Мощность в Вт или None, если данные ещё не получены.
        """
        with self._data_lock:
            return self._consumed_power_kw

    def get_generated_power_kw(self) -> Optional[float]:
        """Возвращает генерируемую мощность (от PV-панелей), Вт.
        
        Returns
        -------
        float | None
            Мощность в Вт или None, если данные ещё не получены.
        """
        with self._data_lock:
            return self._generated_power_kw

    def get_battery_soc_percent(self) -> Optional[float]:
        """Возвращает уровень заряда батареи (SOC), %.
        
        Рассчитывается линейно: U / Umax * 100, где Umax = 24 В (по умолчанию).
        
        Returns
        -------
        float | None
            SOC в процентах [0..100] или None, если данные ещё не получены.
        """
        with self._data_lock:
            return self._battery_soc_percent

    def get_battery_voltage_v(self) -> Optional[float]:
        """Возвращает напряжение батареи, В (для отладки).
        
        Returns
        -------
        float | None
            Напряжение в вольтах или None, если данные ещё не получены.
        """
        with self._data_lock:
            return self._battery_voltage_v

    def is_online(self) -> bool:
        """Проверяет, есть ли связь с инвертором.
        
        Returns
        -------
        bool
            True, если последний опрос был успешным, False иначе.
        """
        with self._data_lock:
            return self._is_online

    def get_snapshot(self) -> InverterSnapshot:
        """Возвращает атомарный снимок всех показаний инвертора.
        
        Все значения читаются под одним Lock, что гарантирует согласованность:
        нельзя получить ситуацию, когда SOC от одного опроса, а мощность — от другого.
        
        Returns
        -------
        InverterSnapshot
            Датакласс с полями:
            - consumed_power_kw: потребляемая мощность, Вт
            - generated_power_kw: генерируемая мощность, Вт
            - battery_soc_percent: SOC батареи, %
            - battery_voltage_v: напряжение батареи, В
            - is_online: флаг связи с инвертором
        """
        with self._data_lock:
            return InverterSnapshot(
                consumed_power_kw=self._consumed_power_kw,
                generated_power_kw=self._generated_power_kw,
                battery_soc_percent=self._battery_soc_percent,
                battery_voltage_v=self._battery_voltage_v,
                is_online=self._is_online,
            )

    # ------------------------------------------------------------------
    # Вспомогательные методы для отладки
    # ------------------------------------------------------------------
    def get_port(self) -> str:
        """Возвращает имя используемого COM-порта."""
        return self._port

    def get_poll_interval(self) -> float:
        """Возвращает интервал опроса в секундах."""
        return self._poll_interval




# ======================================================================
# Пример использования
# ======================================================================
if __name__ == "__main__":
    """
    Демонстрация работы модуля InverterMonitor.
    
    Запускает опрос инвертора каждые 2 секунды,
    10 раз выводит снимок показаний, затем корректно останавливается.
    """
    import time
    
    # Настраиваем логирование
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S"
    )
    
    print("=" * 70)
    print("Демонстрация работы InverterMonitor")
    print("=" * 70)
    
    # Вариант 1: Использование с контекстным менеджером (рекомендуется)
    print("\nВариант 1: Использование с 'with' (автоматическая остановка)")
    print("-" * 70)
    
    try:
        with InverterMonitor(poll_interval=2.0) as monitor:
            print(f"✅ Монитор запущен на порту {monitor.get_port()}")
            print(f"   Интервал опроса: {monitor.get_poll_interval()} сек")
            print()
            
            # Читаем данные 10 раз
            for i in range(1, 11):
                time.sleep(1)  # Ждём 1 сек между выводами (не обязательно кратно poll_interval)
                
                snapshot = monitor.get_snapshot()
                
                print(f"[{i:2d}/10] ", end="")
                
                if snapshot.is_online:
                    print(
                        f"P_load={snapshot.consumed_power_kw:6.2f} Вт | "
                        f"P_pv={snapshot.generated_power_kw:6.2f} Вт | "
                        f"U_batt={snapshot.battery_voltage_v:5.1f} В | "
                        f"SOC={snapshot.battery_soc_percent:5.1f}%"
                    )
                else:
                    print("Инвертор offline (нет связи)")
                
                # Также можно использовать отдельные геттеры:
                # consumed = monitor.get_consumed_power_kw()
                # generated = monitor.get_generated_power_kw()
                # soc = monitor.get_battery_soc_percent()
                # online = monitor.is_online()
        
        print("\nМонитор автоматически остановлен при выходе из 'with'")
        
    except KeyboardInterrupt:
        print("\nПрервано пользователем (Ctrl+C)")
    except Exception as e:
        print(f"\nОшибка: {e}")
        logger.exception("Необработанное исключение")
    
    print("\n" + "=" * 70)
    print("Демонстрация завершена")
    print("=" * 70)
    
    # Вариант 2: Ручное управление (если нужен более сложный сценарий)
    # print("\n📌 Вариант 2: Ручное управление start()/stop()")
    # print("-" * 70)
    # 
    # monitor = InverterMonitor(poll_interval=2.0)
    # monitor.start()
    # 
    # try:
    #     for i in range(5):
    #         time.sleep(2)
    #         snapshot = monitor.get_snapshot()
    #         print(f"[{i+1}] {snapshot}")
    # finally:
    #     monitor.stop(timeout=5.0)
    #     print("✅ Монитор остановлен")