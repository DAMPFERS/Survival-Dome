# AGENTS.md — Архитектура симулятора купола (Simulator Core)

> Контекстный файл для AI-агентов и разработчиков.  
> Описывает **только симулятор**. Логирование для RAG, API Gateway, hot-swap и аутентификация команд реализуются на стороне сервера.

## 1. Назначение

Симулятор расширяет физическую инфраструктуру купола виртуальными узлами, скрытыми зависимостями и кризисными сценариями.  
Для участников и ИИ-агентов Real + Virtual выглядят как единая система через общий API.

Требования:
- Работает в **отдельном потоке**.
- Обновление состояния строго раз в N секунд (по умолчанию 4, задаётся при запуске).
- Потокобезопасный доступ к параметрам (`get` / `set`).
- Внутренний механизм событий и кризисов.
- Сложные зависимости между узлами.
- Высокая расширяемость (новые узлы добавляются быстро).
- Поддержка управления временем симуляции (нормальная скорость, ускорение, пауза).

## 2. Высокоуровневая структура

```
Simulator
├── SimulatorThread          # поток + главный цикл tick()
├── StateStore               # потокобезопасное хранилище состояния
├── NodeRegistry             # реестр всех узлов
├── EventBus                 # шина событий (кризисы + внутренние события)
├── DependencyEngine         # граф зависимостей + правила влияния
├── ScenarioEngine           # запуск/остановка сценариев и кризисов
└── TimeController           # управление скоростью и паузой симуляции
```

## 3. Ключевые компоненты

### 3.1 Simulator (главный класс)

Точка входа.

```python
class Simulator:
    def __init__(self, tick_interval: float = 4.0, time_scale: float = 1.0):
        ...

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def pause(self) -> None: ...
    def resume(self) -> None: ...
    def set_time_scale(self, scale: float) -> None: ...   # 0.0 = пауза, 1.0 = realtime, 2.0 = x2 и т.д.

    # Потокобезопасный доступ
    def get(self, node_id: str, param: str) -> Any: ...
    def get_node(self, node_id: str) -> dict: ...
    def get_all(self) -> dict: ...
    def set(self, node_id: str, param: str, value: Any) -> None: ...
    def update(self, node_id: str, **kwargs) -> None: ...
    def snapshot(self) -> dict: ...

    # Управление сценариями
    def trigger_crisis(self, name: str, params: dict | None = None) -> None: ...
    def stop_crisis(self, name: str) -> None: ...
```

### 3.2 SimulatorThread

- Отдельный `threading.Thread`.
- Главный цикл:
  1. Рассчитать `dt` с учётом `time_scale`.
  2. Вызвать `tick(dt)` у всех узлов.
  3. Применить зависимости (`DependencyEngine`).
  4. Обработать события из `EventBus`.
  5. Обновить `StateStore`.
  6. Спать с коррекцией дрейфа, чтобы средний интервал ≈ `tick_interval`.

### 3.3 StateStore

Единый источник правды.

- Внутри: `dict[str, dict[str, Any]]` + `threading.RLock`.
- Все чтение/запись **только** через методы Store.
- Методы: `get`, `get_node`, `get_all`, `set`, `update`, `snapshot`, `bulk_update`.

### 3.4 Node (базовый класс)

Каждый узел системы — наследник `BaseNode`.

```python
class BaseNode(ABC):
    node_id: str
    node_type: str
    params: dict[str, Any]          # текущие значения
    meta: dict[str, Any]            # описание, единицы измерения, limits

    def tick(self, dt: float, state: StateStore, event_bus: EventBus) -> None: ...
    def on_event(self, event: Event) -> None: ...
    def get_state(self) -> dict: ...
    def apply_control(self, action: str, value: Any) -> None: ...  # для управляемых узлов
```

**Рекомендуемый начальный набор узлов (богатая модель):**

**Энергетика**
- `SolarInverter` (реальный + виртуальные расширения)
- `BatteryBank`
- `DieselGenerator` (резервный)
- `PowerLine` (x8 реальных + дополнительные виртуальные линии)
- `EnergyConsumer` (группы потребителей)

**Климат и атмосфера**
- `TemperatureSensor` / `ZoneClimate`
- `HumiditySensor`
- `CO2Sensor` / `CO2Scrubber`
- `VentilationSystem`
- `AirFilter`

**Жизнеобеспечение**
- `WaterTank` / `WaterRecycling`
- `OxygenGenerator`
- `WasteManagement`

**Производство (важно для хакатона)**
- `CNC_Mill` (мини-фрезер)
- `3DPrinter`
- `SolderingStation`
- `WorkStation` (общее место работы участника)

**Инфраструктура и скрытые системы**
- `MainController`
- `NetworkNode`
- `EmergencyLighting`
- `FireSuppression` (виртуальный)
- `StructuralIntegrity` (скрытый параметр «усталости» конструкций)

### 3.5 EventBus

Простая и надёжная шина.

```python
@dataclass
class Event:
    type: str
    source: str | None
    payload: dict
    timestamp: float
    severity: str = "info"          # info / warning / critical
```

- `publish(event: Event)`
- `subscribe(event_type: str, handler: Callable)`
- `subscribe_all(handler)`
- Очередь на `queue.Queue` + обработка в конце каждого tick.

**Типы событий:**
- Внешние кризисы: `crisis.power_loss`, `crisis.co2_spike`, `crisis.temp_anomaly`, `crisis.solar_flare` и т.д.
- Внутренние: `node.overload`, `line.tripped`, `battery.low`, `dependency.violation`, `production.interrupted`.

### 3.6 DependencyEngine

Декларативные правила влияния.

Рекомендуемый формат правил — **чистый Python** (самый простой и надёжный для отладки) + возможность подгрузки из YAML позже.

Пример правила:

```python
@dependency_rule
def low_power_affects_climate(state: StateStore, event_bus: EventBus):
    if state.get("battery", "level") < 20 and state.get("solar", "power") < 500:
        state.set("zone_living", "temperature_target", 18.0)
        event_bus.publish(Event("dependency.power_saving", ...))
```

Движок на каждом тике выполняет активные правила (можно кэшировать и делать инкрементально).

### 3.7 ScenarioEngine

Управление кризисами и длительными сценариями.

- Сценарий = последовательность или набор параллельных эффектов + условия завершения.
- Можно запускать по команде (`trigger_crisis`), по таймеру, по условию состояния или случайно.
- Поддерживает интенсивность и параметры.

### 3.8 TimeController

- `time_scale: float` (1.0 = реальное время)
- `paused: bool`
- При `time_scale = 0` или `pause()` — симуляция замирает, но поток жив.
- `dt` в `tick()` всегда умножается на `time_scale`.

## 4. Потоки данных и безопасность

- Единственный писатель состояния в tick-е — SimulatorThread.
- Внешние `set`/`update` (от Gateway или админки) идут через StateStore под lock.
- Рекомендуется разделять:
  - `control_*` параметры (то, что можно менять командам),
  - `telemetry_*` (только чтение + влияние симулятора).

## 5. Расширяемость

Добавление нового узла:
1. Создать класс-наследник `BaseNode`.
2. Зарегистрировать в `NodeRegistry`.
3. (Опционально) добавить правила в `DependencyEngine`.
4. (Опционально) добавить события/сценарии.

Никакого изменения ядра не требуется.

## 6. Конфигурация при запуске

```python
sim = Simulator(
    tick_interval=4.0,
    time_scale=1.0,
    nodes_config="config/nodes.yaml",      # опционально
    scenarios_config="config/scenarios.yaml"
)
sim.start()
```

## 7. Что сознательно вынесено за пределы симулятора

- Логирование для RAG (делает сервер, получая snapshot + события).
- Hot-swap Real/Virtual (решает API Gateway).
- Аутентификация команд и «активная команда».
- WebSocket и Cloudflare-туннель.
- Персистентность состояния (можно добавить позже через snapshot).

## 8. Принципы реализации

- Простота важнее микрооптимизаций.
- Все публичные методы симулятора потокобезопасны.
- Предпочитаем явный код правилам «магии».
- Богатая модель узлов с самого начала (см. список выше).
- Время симуляции полностью контролируемо.
```
