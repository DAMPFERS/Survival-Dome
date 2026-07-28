"""
Dome Telemetry Client
Потокобезопасный WebSocket-клиент для системы телеметрии купола.
Работает в отдельном потоке, предоставляет pull-модель (геттеры)
для чтения телеметрии и блокирующий метод toggle_channel() для
управления каналами нагрузки с ожиданием подтверждения от сервера.

Зависимости:
    pip install websocket-client
"""

import json
import logging
import threading
import time
from typing import Optional, Dict, Any

import websocket


logger = logging.getLogger(__name__)


# Имена каналов нагрузки (должны совпадать с сервером)
CHANNEL_NAMES = [
    "LIGHTING",
    "WATER PUMP",
    "GREENHOUSE",
    "WORKSHOP",
    "VENTILATION",
    "COMPUTERS",
    "REACTOR",
    "RESERVE",
]


class DomeTelemetryClient(threading.Thread):
    """
    WebSocket-клиент телеметрии купола.

    Запускается через .start(). При недоступности сервера бросает
    ConnectionError после max_reconnect_attempts неудачных попыток.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 8765,
        control_timeout: float = 5.0,
        max_reconnect_attempts: int = 20,
        reconnect_delay: float = 5.0,
    ):
        super().__init__(daemon=True, name="DomeTelemetryClient")

        self._host = host
        self._port = port
        self._url = f"ws://{host}:{port}"
        self._control_timeout = control_timeout
        self._max_reconnect_attempts = max_reconnect_attempts
        self._reconnect_delay = reconnect_delay

        # Сокет и состояние подключения
        self._ws: Optional[websocket.WebSocketApp] = None
        self._connected = threading.Event()
        self._stop_event = threading.Event()
        self._last_error: Optional[Exception] = None
        self._failed = False  # True, если превышен лимит попыток

        # Разделяемое состояние (под _lock)
        self._lock = threading.RLock()
        self._state: Dict[str, Any] = {
            "climate": {"temperature": None, "humidity": None, "co2": None},
            "power": {
                "solar_generation": None,
                "battery": None,
                "channels": {},  # {channel_id: {"name", "enabled", "power"}}
            },
        }

        # Ожидание подтверждений control_response
        # {channel_id: {"event": Event, "result": Optional[dict], "error": Optional[Exception]}}
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._pending_lock = threading.Lock()

        # Lock для отправки — атомарность "добавить в pending + send"
        self._send_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Жизненный цикл потока
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Основной цикл: подключение и автопереподключение."""
        attempt = 0
        while not self._stop_event.is_set():
            if attempt >= self._max_reconnect_attempts:
                err = ConnectionError(
                    f"Не удалось подключиться к {self._url} после "
                    f"{self._max_reconnect_attempts} попыток"
                )
                logger.error(str(err))
                self._last_error = err
                self._failed = True
                self._fail_all_pending(err)
                return

            logger.info(
                "Попытка подключения к %s (%d/%d)",
                self._url, attempt + 1, self._max_reconnect_attempts,
            )

            try:
                self._ws = websocket.WebSocketApp(
                    self._url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                # run_forever блокирует поток до закрытия сокета
                self._ws.run_forever(ping_timeout=20)
            except Exception as e:
                logger.warning("Ошибка в WebSocket-цикле: %s", e)
                self._last_error = e

            if self._stop_event.is_set():
                break

            self._connected.clear()
            attempt += 1
            logger.info("Переподключение через %.1f сек...", self._reconnect_delay)
            # Прерываемый сон — stop() разбудит мгновенно
            if self._stop_event.wait(self._reconnect_delay):
                break

        logger.info("Поток клиента остановлен")

    def stop(self, timeout: float = 5.0) -> None:
        """Корректно остановить клиента."""
        logger.info("Остановка клиента...")
        self._stop_event.set()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        self._fail_all_pending(RuntimeError("Клиент остановлен"))
        self.join(timeout)

    # ------------------------------------------------------------------
    # Обработчики WebSocket
    # ------------------------------------------------------------------

    def _on_open(self, ws) -> None:
        logger.info("Подключение к %s установлено", self._url)
        self._connected.set()

    def _on_message(self, ws, message: str) -> None:
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            logger.warning("Получено не-JSON сообщение: %r", message[:100])
            return

        msg_type = data.get("type")
        if msg_type == "telemetry":
            self._handle_telemetry(data.get("data", {}))
        elif msg_type == "control_response":
            self._handle_control_response(data)
        else:
            logger.debug("Неизвестный тип сообщения: %s", msg_type)

    def _on_error(self, ws, error) -> None:
        logger.warning("WebSocket error: %s", error)
        self._last_error = error

    def _on_close(self, ws, close_status_code, close_msg) -> None:
        logger.info(
            "Соединение закрыто (code=%s, msg=%s)", close_status_code, close_msg
        )
        self._connected.clear()
        self._fail_all_pending(ConnectionError("Соединение потеряно"))

    # ------------------------------------------------------------------
    # Обработка входящих сообщений
    # ------------------------------------------------------------------

    def _handle_telemetry(self, payload: Dict[str, Any]) -> None:
        with self._lock:
            climate = payload.get("climate")
            if climate:
                for key in ("temperature", "humidity", "co2"):
                    if key in climate:
                        self._state["climate"][key] = climate[key]

            power = payload.get("power")
            if power:
                if "solarGeneration" in power:
                    self._state["power"]["solar_generation"] = power["solarGeneration"]
                if "battery" in power:
                    self._state["power"]["battery"] = power["battery"]
                if "channels" in power:
                    for ch in power["channels"]:
                        ch_id = ch.get("id")
                        if ch_id is not None:
                            self._state["power"]["channels"][ch_id] = {
                                "name": ch.get("name"),
                                "enabled": bool(ch.get("enabled")),
                                "power": float(ch.get("power", 0.0)),
                            }

    def _handle_control_response(self, data: Dict[str, Any]) -> None:
        channel_id = data.get("channel_id")
        success = data.get("success", False)

        with self._pending_lock:
            pending = self._pending.pop(channel_id, None)

        if pending is None:
            logger.debug(
                "Получен control_response для неизвестного channel_id=%s", channel_id
            )
            return

        if success:
            # Обновляем состояние канала
            with self._lock:
                ch = self._state["power"]["channels"].setdefault(channel_id, {})
                ch["enabled"] = data.get("enabled", ch.get("enabled"))
                ch["power"] = data.get("power", ch.get("power", 0.0))

            pending["result"] = {
                "channel_id": channel_id,
                "enabled": data.get("enabled"),
                "power": data.get("power"),
            }
            pending["event"].set()
        else:
            pending["error"] = RuntimeError(
                f"Сервер отклонил команду для канала {channel_id}: "
                f"{data.get('error', 'unknown')}"
            )
            pending["event"].set()

    def _fail_all_pending(self, error: Exception) -> None:
        """Пробудить все ожидающие подтверждения с ошибкой."""
        with self._pending_lock:
            for pending in self._pending.values():
                pending["error"] = error
                pending["event"].set()
            self._pending.clear()

    # ------------------------------------------------------------------
    # Геттеры (потокобезопасные, pull-модель)
    # ------------------------------------------------------------------

    def get_climate(self) -> Dict[str, Optional[float]]:
        with self._lock:
            return dict(self._state["climate"])

    def get_temperature(self) -> Optional[float]:
        with self._lock:
            return self._state["climate"]["temperature"]

    def get_humidity(self) -> Optional[float]:
        with self._lock:
            return self._state["climate"]["humidity"]

    def get_co2(self) -> Optional[float]:
        with self._lock:
            return self._state["climate"]["co2"]

    def get_power(self) -> Dict[str, Any]:
        """Сводка: солнечная генерация, батарея, суммарная нагрузка (Вт)."""
        with self._lock:
            solar = self._state["power"]["solar_generation"]
            battery = self._state["power"]["battery"]
            total_load = sum(
                ch["power"] for ch in self._state["power"]["channels"].values()
                if ch.get("enabled")
            )
            return {
                "solar_generation": solar,
                "battery": battery,
                "total_load": total_load,
            }

    def get_solar_generation(self) -> Optional[float]:
        with self._lock:
            return self._state["power"]["solar_generation"]

    def get_battery(self) -> Optional[float]:
        with self._lock:
            return self._state["power"]["battery"]

    def get_channels(self) -> Dict[int, Dict[str, Any]]:
        """Словарь {channel_id: {"name", "enabled", "power"}}."""
        with self._lock:
            return {k: dict(v) for k, v in self._state["power"]["channels"].items()}

    def is_channel_enabled(self, channel_id: int) -> Optional[bool]:
        with self._lock:
            ch = self._state["power"]["channels"].get(channel_id)
            return ch["enabled"] if ch else None

    def get_channel_power(self, channel_id: int) -> Optional[float]:
        with self._lock:
            ch = self._state["power"]["channels"].get(channel_id)
            return ch["power"] if ch else None

    def is_connected(self) -> bool:
        return self._connected.is_set()

    def is_failed(self) -> bool:
        """True, если превышен лимит попыток подключения."""
        return self._failed

    def get_last_error(self) -> Optional[Exception]:
        return self._last_error

    # ------------------------------------------------------------------
    # Управление каналами
    # ------------------------------------------------------------------

    def toggle_channel(
        self,
        channel_id: int,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Переключить канал нагрузки. Блокирует вызывающий поток до получения
        подтверждения от сервера или истечения таймаута.

        :param channel_id: ID канала (1..8)
        :param timeout: Таймаут ожидания (по умолчанию self._control_timeout)
        :return: {"channel_id", "enabled", "power"}
        :raises ValueError: если channel_id вне диапазона
        :raises ConnectionError: если нет соединения или клиент в отказе
        :raises TimeoutError: если сервер не ответил за timeout секунд
        :raises RuntimeError: если сервер отклонил команду
        """
        if not 1 <= channel_id <= len(CHANNEL_NAMES):
            raise ValueError(
                f"channel_id должен быть в диапазоне 1..{len(CHANNEL_NAMES)}"
            )

        if self._failed:
            raise ConnectionError(
                f"Клиент в состоянии отказа: {self._last_error}"
            )

        if not self._connected.is_set():
            raise ConnectionError("Нет соединения с сервером")

        timeout = timeout if timeout is not None else self._control_timeout

        event = threading.Event()
        pending_entry = {"event": event, "result": None, "error": None}

        with self._pending_lock:
            self._pending[channel_id] = pending_entry

        message = json.dumps({
            "type": "control",
            "action": "toggle",
            "channel_id": channel_id,
        })

        try:
            with self._send_lock:
                if not self._connected.is_set():
                    raise ConnectionError("Соединение потеряно в момент отправки")
                self._ws.send(message)
        except Exception as e:
            with self._pending_lock:
                self._pending.pop(channel_id, None)
            raise ConnectionError(f"Ошибка отправки команды: {e}") from e

        # Ожидаем ответ
        if not event.wait(timeout):
            with self._pending_lock:
                self._pending.pop(channel_id, None)
            raise TimeoutError(
                f"Сервер не ответил на toggle канала {channel_id} "
                f"в течение {timeout} сек"
            )

        if pending_entry["error"] is not None:
            raise pending_entry["error"]

        return pending_entry["result"]
    
    
    
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(threadName)s] %(levelname)s %(name)s: %(message)s",
    )

    # Создаём клиента с нужными параметрами
    client = DomeTelemetryClient(
        host="192.168.8.36",
        port=8765,
        control_timeout=5.0,
        max_reconnect_attempts=20,
        reconnect_delay=5.0,
    )
    client.start()

    try:
        # Ждём установки соединения (с проверкой флага отказа)
        for _ in range(50):  # до 5 секунд
            if client.is_connected():
                break
            if client.is_failed():
                raise client.get_last_error() or ConnectionError("Failed")
            time.sleep(0.1)
        else:
            raise TimeoutError("Не удалось установить соединение")

        # --- Пример 1: Pull-модель (чтение состояния) ---
        print("\n=== Телеметрия ===")
        print(f"Температура: {client.get_temperature()} °C")
        print(f"Влажность:   {client.get_humidity()} %")
        print(f"CO2:         {client.get_co2()} ppm")
        print(f"Солнце:      {client.get_solar_generation()} kW")
        print(f"Батарея:     {client.get_battery()} %")
        print(f"Каналы:      {client.get_channels()}")

        # --- Пример 2: Управление каналом ---
        print("\n=== Переключение канала 3 (GREENHOUSE) ===")
        try:
            result = client.toggle_channel(channel_id=3)
            print(f"Успех: {result}")
        except TimeoutError as e:
            print(f"Таймаут: {e}")
        except (ConnectionError, RuntimeError) as e:
            print(f"Ошибка: {e}")

        # --- Пример 3: Цикл мониторинга в основном потоке ---
        print("\n=== Мониторинг (Ctrl+C для выхода) ===")
        while True:
            climate = client.get_climate()
            power = client.get_power()

            # Проверяем, не "отказался" ли клиент
            if client.is_failed():
                print("Клиент в состоянии отказа, выходим")
                break

            print(
                f"T={climate['temperature']}°C "
                f"H={climate['humidity']}% "
                f"CO2={climate['co2']}ppm | "
                f"Solar={power['solar_generation']}kW "
                f"Batt={power['battery']}% "
                f"Load={power['total_load']/1000:.2f}kW"
            )
            time.sleep(2)

    except KeyboardInterrupt:
        print("\nПолучен сигнал остановки")
    except Exception as e:
        print(f"Критическая ошибка: {e}")
    finally:
        client.stop()
        print("Клиент остановлен")