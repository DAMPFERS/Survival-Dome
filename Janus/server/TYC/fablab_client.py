"""
Клиент для теплицы ФабЛаб (порт 8000)
Периодический опрос датчиков в фоновом потоке с автоматическим переподключением
"""

import threading
import time
import requests
from typing import Dict, Any, Optional
from dataclasses import dataclass

@dataclass
class SensorData:
    """Структура для хранения данных датчика"""
    value: Any
    valid: bool
    fresh: bool
    age_ms: int
    received_at: str
    timestamp: float # Время получения клиентом


class FabLabClient:
    """
    Клиент для управления теплицей ФабЛаб
    Параметры:
    base_url: URL сервера (по умолчанию http://127.0.0.1:8000)
    poll_interval: Интервал опроса в секундах (по умолчанию 3.0)
    timeout: Таймаут HTTP запросов в секундах (по умолчанию 45)
    auto_reconnect: Автоматическое переподключение при ошибках (по умолчанию True)
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        poll_interval: float = 3.0,
        timeout: float = 45.0,
        auto_reconnect: bool = True
    ):
        self.base_url = base_url.rstrip('/')
        self.poll_interval = poll_interval
        self.timeout = timeout
        self.auto_reconnect = auto_reconnect
        
        # Кэш данных датчиков
        self._cache: Dict[str, SensorData] = {}
        self._cache_lock = threading.RLock()
        
        # Состояние подключения
        self._is_connected = False
        self._connection_lock = threading.Lock()
        
        # Флаг работы фонового потока
        self._running = False
        self._poll_thread: Optional[threading.Thread] = None
        
        # Конфигурация датчиков для опроса
        self._sensor_config = {
            'temp': {'range': range(3), 'path': '/api/v1/temperature/{idx}'},
            'pressure': {'range': range(3), 'path': '/api/v1/pressure/{idx}'},
            'voc': {'range': range(2), 'path': '/api/v1/voc/{idx}'},
            'humidity': {'range': range(3), 'path': '/api/v1/humidity/{idx}'},
            'dust': {'path': '/api/v1/dust'},
            'door': {'range': range(2), 'path': '/api/v1/doors/{idx}/state'},
            'pwm': {'range': range(6), 'path': '/api/v1/fans/{idx}/pwm'},
            'rpm': {'range': range(6), 'path': '/api/v1/fans/{idx}/rpm'},
            'lock': {'range': range(2), 'path': '/api/v1/locks/{idx}/state'},
            'vacuum': {'path': '/api/v1/vacuum/state'},
            'mode': {'path': '/api/v1/admin/mode'},
        }

    def start(self) -> None:
        """Запуск фонового потока опроса"""
        if self._running:
            return
        
        self._running = True
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

    def stop(self) -> None:
        """Остановка фонового потока опроса"""
        self._running = False
        if self._poll_thread:
            self._poll_thread.join(timeout=5.0)
            self._poll_thread = None

    def _poll_loop(self) -> None:
        """Цикл фонового опроса датчиков"""
        while self._running:
            try:
                # Проверка здоровья сервиса
                self._check_health()
                
                # Опрос всех датчиков
                for sensor_name, config in self._sensor_config.items():
                    if not self._running:
                        break
                    
                    if 'range' in config:
                        for idx in config['range']:
                            try:
                                self._poll_sensor(sensor_name, idx)
                            except Exception:
                                pass  # Продолжаем опрос остальных датчиков
                    else:
                        try:
                            self._poll_sensor(sensor_name)
                        except Exception:
                            pass
                
                # Устанавливаем флаг подключения
                with self._connection_lock:
                    self._is_connected = True
                    
            except requests.exceptions.RequestException:
                with self._connection_lock:
                    self._is_connected = False
            except Exception:
                with self._connection_lock:
                    self._is_connected = False
            
            # Ждем следующий цикл опроса
            time.sleep(self.poll_interval)

    def _check_health(self) -> None:
        """Проверка здоровья сервиса"""
        response = requests.get(f"{self.base_url}/health", timeout=self.timeout)
        response.raise_for_status()

    def _poll_sensor(self, sensor_name: str, idx: Optional[int] = None) -> None:
        """Опрос одного датчика"""
        config = self._sensor_config[sensor_name]
        path = config['path']
        
        if idx is not None:
            path = path.format(idx=idx)
            cache_key = f"{sensor_name}_{idx}"
        else:
            cache_key = sensor_name
        
        response = requests.get(f"{self.base_url}{path}", timeout=self.timeout)
        response.raise_for_status()
        data = response.json()
        
        # Извлекаем значение из разных форматов ответа
        value = None
        if 'value' in data:
            value = data['value']
        elif 'closed' in data:
            value = data['closed']
        elif 'lock_output_enabled' in data:
            value = data['lock_output_enabled']
        elif 'enabled' in data:
            value = data['enabled']
        elif 'mode' in data:
            value = data['mode']
        elif 'adc' in data:
            value = {'adc': data['adc'], 'voltage_mv': data.get('voltage_mv'), 'voltage_v': data.get('voltage_v')}
        
        # Сохраняем в кэш
        sensor_data = SensorData(
            value=value,
            valid=data.get('valid', True),
            fresh=data.get('fresh', True),
            age_ms=data.get('age_ms', 0),
            received_at=data.get('received_at', ''),
            timestamp=time.time()
        )
        
        with self._cache_lock:
            self._cache[cache_key] = sensor_data

    def get(self, parameter: str, idx: Optional[int] = None) -> Optional[SensorData]:
        """
        Получить данные датчика из кэша
        
        Параметры:
            parameter: Имя параметра (temp, pressure, voc, humidity, dust, door, pwm, rpm, lock, vacuum, mode)
            idx: Индекс датчика (если применимо)
            
        Возвращает:
            SensorData или None если данных нет
        """
        if idx is not None:
            cache_key = f"{parameter}_{idx}"
        else:
            cache_key = parameter
        
        with self._cache_lock:
            return self._cache.get(cache_key)

    def get_value(self, parameter: str, idx: Optional[int] = None) -> Optional[Any]:
        """Получить только значение датчика из кэша"""
        data = self.get(parameter, idx)
        return data.value if data else None

    def is_connected(self) -> bool:
        """Проверить состояние подключения"""
        with self._connection_lock:
            return self._is_connected

    def set_pwm(self, idx: int, pwm: int) -> bool:
        """
        Установить PWM вентилятора
        
        Параметры:
            idx: Индекс вентилятора (0-5)
            pwm: Значение PWM (0-255)
            
        Возвращает:
            True если успешно, False при ошибке
        """
        try:
            response = requests.post(
                f"{self.base_url}/api/v1/fans/{idx}/pwm",
                json={"pwm": pwm},
                timeout=self.timeout
            )
            response.raise_for_status()
            
            # Обновляем кэш
            data = response.json()
            sensor_data = SensorData(
                value=data.get('value'),
                valid=True,
                fresh=True,
                age_ms=0,
                received_at=data.get('received_at', ''),
                timestamp=time.time()
            )
            
            with self._cache_lock:
                self._cache[f"pwm_{idx}"] = sensor_data
            
            return True
        except Exception:
            return False

    def set_door_lock(self, idx: int, lock_state: bool) -> bool:
        """
        Установить состояние магнитного замка
        
        Параметры:
            idx: Индекс замка (0-1)
            lock_state: True - заблокировать, False - разблокировать
            
        Возвращает:
            True если успешно, False при ошибке
        """
        try:
            response = requests.post(
                f"{self.base_url}/api/v1/admin/locks/{idx}",
                json={"lock_state": lock_state},
                timeout=self.timeout
            )
            response.raise_for_status()
            
            # Обновляем кэш
            sensor_data = SensorData(
                value=lock_state,
                valid=True,
                fresh=True,
                age_ms=0,
                received_at='',
                timestamp=time.time()
            )
            
            with self._cache_lock:
                self._cache[f"lock_{idx}"] = sensor_data
            
            return True
        except Exception:
            return False

    def set_vacuum(self, mode: int) -> bool:
        """
        Установить состояние пылесоса
        
        Параметры:
            mode: 1 - включить, 0 - выключить
            
        Возвращает:
            True если успешно, False при ошибке
        """
        try:
            response = requests.post(
                f"{self.base_url}/api/v1/admin/vacuum",
                json={"mode": mode},
                timeout=self.timeout
            )
            response.raise_for_status()
            
            # Обновляем кэш
            sensor_data = SensorData(
                value=(mode == 1),
                valid=True,
                fresh=True,
                age_ms=0,
                received_at='',
                timestamp=time.time()
            )
            
            with self._cache_lock:
                self._cache["vacuum"] = sensor_data
            
            return True
        except Exception:
            return False

    def set_mode(self, mode: int, idx: int) -> bool:
        """
        Установить режим неисправности
        
        Параметры:
            mode: Режим (0 - выкл, 1 - случайный PWM каждые 30с, 2 - случайный PWM при set_pwm)
            idx: Индекс вентилятора (0-5)
            
        Возвращает:
            True если успешно, False при ошибке
        """
        try:
            response = requests.post(
                f"{self.base_url}/api/v1/admin/mode",
                json={"mode": mode, "idx": idx},
                timeout=self.timeout
            )
            response.raise_for_status()
            
            # Обновляем кэш
            data = response.json()
            sensor_data = SensorData(
                value={'mode': data.get('mode'), 'idx': data.get('idx')},
                valid=True,
                fresh=True,
                age_ms=0,
                received_at=data.get('received_at', ''),
                timestamp=time.time()
            )
            
            with self._cache_lock:
                self._cache["mode"] = sensor_data
            
            return True
        except Exception:
            return False

    def get_capabilities(self) -> Optional[Dict[str, Any]]:
        """Получить конфигурацию сервиса"""
        try:
            response = requests.get(f"{self.base_url}/api/v1/capabilities", timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except Exception:
            return None

    def probe_hub(self) -> bool:
        """Проверить связь с хабом"""
        try:
            response = requests.get(f"{self.base_url}/api/v1/hub/probe", timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
            return data.get('responding', False)
        except Exception:
            return False
        
        
        
if __name__ == "__main__":
    client = FabLabClient(
    base_url="http://192.168.8.36:8000",
    poll_interval=3.0,
    timeout=45.0
    )
    # Запуск фонового опроса
    client.start()

    try:
        # Ждем пока накопятся данные
        time.sleep(3)
        print(f"Подключен: {client.is_connected()}")
        
        # Чтение данных из кэша
        temp = client.get_value('temp', 0)
        print(f"Температура 0: {temp}")
        
        voc = client.get_value('voc', 1)
        print(f"VOC 1: {voc}")
        
        dust = client.get_value('dust')
        print(f"Пыль: {dust}")
        
        # Управление
        client.set_pwm(0, 255)
        client.set_pwm(1, 255)
        client.set_pwm(2, 255)
        client.set_pwm(3, 255)
        client.set_pwm(4, 255)
        client.set_pwm(5, 255)
        client.set_door_lock(0, True)
        client.set_door_lock(1, True)
        client.set_vacuum(1)
        # client.set_mode(2, 4)  # Включаем режим неисправности для вентилятора 4
        
        # Проверка состояния
        
        # Работаем дальше...
        time.sleep(4)
        
    finally:
        # Остановка
        client.stop()