# Архитектура симулятора купола (Dome Simulator)

## Оглавление
1. [Общее описание](#общее-описание)
2. [Высокоуровневая архитектура](#высокоуровневая-архитектура)
3. [Детальное описание компонентов](#детальное-описание-компонентов)
4. [Система узлов](#система-узлов)
5. [Правила зависимостей](#правила-зависимостей)
6. [Система кризисов](#система-кризисов)
7. [Поток данных и взаимодействие компонентов](#поток-данных-и-взаимодействие-компонентов)
8. [Потокобезопасность](#потокобезопасность)

---

## Общее описание

Dome Simulator — это многопоточный симулятор физической инфраструктуры купола для проекта Survival-Dome. Симулятор расширяет реальную инфраструктуру виртуальными узлами, скрытыми зависимостями и кризисными сценариями, создавая единую систему для участников хакатона и ИИ-агентов.

**Ключевые характеристики:**
- Работает в отдельном потоке с фиксированным интервалом обновления (по умолчанию 4 секунды)
- Потокобезопасный доступ к состоянию из внешних потоков (API Gateway)
- Управление скоростью симуляции (пауза, ускорение, замедление)
- Компенсация дрейфа времени для стабильного среднего периода тиков
- Событийно-ориентированная архитектура с отложенной рассылкой событий
- Декларативные правила зависимостей между узлами
- Система кризисных сценариев с жизненным циклом

---

## Высокоуровневая архитектура

```
┌─────────────────────────────────────────────────────────────────┐
│                         Simulator (фасад)                        │
│  Публичный API: start/stop/pause/resume, get/set, trigger_crisis│
└────────────┬────────────────────────────────────────────────────┘
             │
             │ владеет и координирует:
             │
    ┌────────┴────────┬────────────┬─────────────┬──────────────┐
    │                 │            │             │              │
    ▼                 ▼            ▼             ▼              ▼
┌─────────┐   ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐
│StateStore│  │EventBus  │  │NodeRegistry│ │Dependency│ │Scenario  │
│         │  │          │  │           │  │Engine    │ │Engine    │
└─────────┘   └──────────┘  └──────────┘  └──────────┘  └──────────┘
     ▲              ▲             ▲             ▲             ▲
     │              │             │             │             │
     └──────────────┴─────────────┴─────────────┴─────────────┘
                            │
                            │ использует все компоненты
                            ▼
                   ┌─────────────────┐
                   │SimulatorThread  │
                   │  (главный цикл) │
                   └─────────────────┘
                            │
                            │ каждый тик:
                            │
            ┌───────────────┼───────────────┐
            │               │               │
            ▼               ▼               ▼
     1. tick() всех   2. Сценарии   3. Правила
        узлов         (кризисы)      зависимостей
            │               │               │
            └───────────────┴───────────────┘
                            │
                            ▼
                  4. Рассылка событий
                     (dispatch_pending)
```

---

## Детальное описание компонентов

### 1. Simulator (`simulator.py`)

**Назначение:** Главный фасад системы. Единственная точка входа для внешних систем (API Gateway, админка).

**Ключевая логика:**
- Собирает все подсистемы при инициализации
- Управляет жизненным циклом SimulatorThread
- Предоставляет потокобезопасные методы доступа к данным
- Делегирует управляющие команды (`control()`) соответствующим узлам

**Публичный API:**

```python
# Жизненный цикл
start()                              # Запускает поток симулятора
stop(timeout=10.0)                   # Останавливает поток
pause() / resume()                   # Управление паузой
set_time_scale(scale: float)         # 0.0=пауза, 1.0=реалтайм, 2.0=x2

# Доступ к данным (потокобезопасный)
get(node_id, param, default=None)    # Один параметр узла
get_node(node_id)                    # Все параметры узла (копия)
get_all()                            # Все узлы (глубокая копия)
snapshot()                           # Атомарный слепок с timestamp

set(node_id, param, value)           # Изменить один параметр
update(node_id, **kwargs)            # Изменить несколько параметров

# Управление кризисами
trigger_crisis(name, params=None)    # Запустить кризис
stop_crisis(name)                    # Остановить кризис
active_crises()                      # Список активных кризисов

# Управление узлами
add_node(node: BaseNode)             # Зарегистрировать инстанс узла
add_node_from_type(type_name, node_id, **config)  # Создать и зарегистрировать
remove_node(node_id)                 # Удалить узел
control(node_id, action, value=None) # Управляющая команда

# События
subscribe(event_type, callback)      # Подписаться на тип события
subscribe_all(callback)              # Подписаться на все события
recent_events(n=50, event_type=None) # История событий

# Персистентность
save_snapshot(path) / load_snapshot(path)  # Сохранение/загрузка состояния
```

**Логика метода `control()`:**
Централизованная точка обработки управляющих команд. Различает три типа команд:

1. **Простые команды** (enable/disable/start/stop/arm/disarm и т.д.) — напрямую маппятся на поля состояния через таблицу `_ACTION_TO_FIELD`
2. **Команды с валидацией** (start_job, set_target_output_kw, refuel и т.д.) — обрабатываются явными if-блоками
3. **Неизвестные команды** — делегируются методу `apply_control()` самого узла (который обычно бросает исключение)

---

### 2. StateStore (`store.py`)

**Назначение:** Единый источник правды (Single Source of Truth). Потокобезопасное хранилище состояния всех узлов.

**Структура данных:**
```python
_data: dict[node_id: str, dict[param: str, value: Any]]
```

**Ключевая логика:**
- Внутренний `threading.RLock` защищает все операции
- Все методы чтения возвращают **копии** данных, чтобы наружу не утекали мутабельные ссылки
- Единственный регулярный писатель — SimulatorThread, но точечная запись (`set`/`update`) возможна из любого потока
- Поддержка персистентности через JSON snapshot

**Методы:**

```python
# Регистрация
register_node(node_id, initial_state)  # Создаёт запись под узел
remove_node(node_id)                   # Удаляет узел

# Чтение (всё возвращает копии)
get(node_id, param, default=None)      # Один параметр
get_node(node_id)                      # Весь узел (dict)
get_all()                              # Все узлы
node_ids()                             # Список ID узлов
snapshot()                             # Атомарный слепок + timestamp

# Запись
set(node_id, param, value)             # Один параметр
update(node_id, **kwargs)              # Несколько параметров
bulk_update(updates)                   # Атомарная пакетная запись

# Персистентность
dump_json(path) / load_json(path)      # JSON-сериализация
```

**Паттерн использования:**
- Узлы **не хранят** мутабельное состояние внутри себя — только конфигурацию (константы)
- Вся телеметрия живёт в StateStore
- `tick()` узла читает состояние через `state.get_node()`, работает с ним локально, затем пишет обратно через `state.update()`

---

### 3. SimulatorThread (`simulator_thread.py`)

**Назначение:** Главный цикл симулятора в отдельном daemon-потоке.

**Ключевая логика:**

```python
def run():
    next_tick_time = time.monotonic()
    while not stop_event.is_set():
        _run_single_tick()
        tick_count += 1
        
        # Компенсация дрейфа времени
        next_tick_time += tick_interval
        sleep_time = next_tick_time - time.monotonic()
        
        if sleep_time > 0:
            stop_event.wait(timeout=sleep_time)  # прерываемый sleep
        else:
            # Тик выполнялся дольше интервала — сбрасываем базу
            next_tick_time = time.monotonic()
```

**Компенсация дрейфа:**
- Не использует `time.sleep(tick_interval)` на каждой итерации
- Вычисляет **абсолютную** точку следующего тика: `next_tick_time += interval`
- Спит до этой точки
- Суммарная ошибка не накапливается — средний период стремится к `tick_interval`

**Порядок выполнения одного тика:**

```python
def _run_single_tick():
    dt = time_controller.compute_dt()  # учитывает time_scale и паузу
    
    # 1-2. Тик всех узлов + централизованная публикация их событий
    for node in registry.all_nodes():
        events = node.tick(dt, store, event_bus)
        for event in events:
            event_bus.publish(event)
    
    # 3. Сценарии/кризисы
    scenario_engine.tick(store, event_bus, dt)
    
    # 4. Правила зависимостей между узлами
    dependency_engine.run(store, event_bus, dt)
    
    # 5. Единственная точка рассылки событий подписчикам за тик
    event_bus.dispatch_pending()
```

**Обработка ошибок:**
- Исключение в `tick()` одного узла не роняет весь поток — логируется и пропускается
- Исключение в правиле зависимости — логируется + публикуется событие `dependency.violation`

---

### 4. EventBus (`events.py`)

**Назначение:** Потокобезопасная шина событий с отложенной рассылкой.

**Структура события:**
```python
@dataclass(frozen=True, slots=True)
class Event:
    type: str                          # "crisis.power_loss", "line.tripped", ...
    source: str                        # node_id или имя подсистемы
    payload: dict[str, Any]            # данные события
    timestamp: float                   # time.time()
    severity: Severity                 # INFO | WARNING | CRITICAL
```

**Ключевая логика:**

```python
# Публикация (неблокирующая, из любого потока)
publish(event: Event)
    -> кладёт событие в queue.Queue

# Рассылка (вызывается ТОЛЬКО из SimulatorThread, один раз в конце тика)
dispatch_pending() -> int
    -> достаёт все накопленные события
    -> рассылает подписчикам
    -> добавляет в историю (deque с ограниченным размером)
    -> возвращает количество обработанных событий
```

**Паттерн "publish-dispatch":**
- Узлы и правила **не рассылают** события сами — только кладут в очередь
- Рассылка происходит **после** того, как все узлы и правила выполнились
- Подписчики видят **консистентное** состояние тика, а не "недособранное"

**Подписки:**
```python
subscribe(event_type, callback)      # На конкретный тип
subscribe_all(callback)              # На все события
unsubscribe(event_type, callback)    # Отписка
```

**История:**
- Хранит последние N событий (по умолчанию 500) в `deque`
- Доступна через `get_recent(n=50, event_type=None)`
- Используется правилами для анализа недавних кризисов

---

### 5. TimeController (`time_control.py`)

**Назначение:** Управление скоростью симуляционного времени.

**Разделение времени:**
- **Реальное время** — период цикла потока (`tick_interval`, секунды). Держится постоянным через компенсацию дрейфа.
- **Симуляционное время** — `dt`, передаваемое в `tick()` узлов. Зависит от `time_scale`.

**Формула:**
```python
dt = tick_interval * time_scale   # если не пауза
```

**Ключевая логика:**

```python
def compute_dt() -> float:
    with _lock:
        if _paused or _time_scale == 0.0:
            return 0.0
        dt = _tick_interval * _time_scale
        _sim_time += dt  # накопленное симуляционное время
        return dt
```

**Управление:**
```python
set_time_scale(scale)  # 0.0=пауза, 1.0=реалтайм, 2.0=x2, 0.5=x0.5
pause() / resume()     # Явная пауза (флаг)
set_tick_interval(seconds)  # Изменить период цикла (редко используется)
```

**Свойства:**
```python
tick_interval: float   # Период реального времени
time_scale: float      # Множитель скорости
is_paused: bool        # Флаг паузы
sim_time: float        # Накопленное симуляционное время (сек)
```

---

### 6. NodeRegistry (`nodes/base.py`)

**Назначение:** Хранит живые инстансы узлов и синхронизирует их с StateStore.

**Ключевая логика:**

```python
def register(node: BaseNode):
    with _lock:
        _nodes[node.node_id] = node
        _store.register_node(node.node_id, node.get_state())
```

При регистрации узел:
1. Добавляется в реестр инстансов
2. Его начальное состояние (`get_state()`) записывается в StateStore

**Методы:**
```python
register(node)              # Зарегистрировать узел
unregister(node_id)         # Удалить узел + его состояние
get(node_id)                # Получить инстанс узла
all_nodes()                 # Все инстансы (список)
node_ids()                  # Все ID
nodes_by_category(category) # Узлы определённой категории
```

---

### 7. DependencyEngine (`dependency_engine.py`)

**Назначение:** Выполняет правила зависимостей между узлами каждый тик.

**Правило:**
```python
RuleFunc = Callable[[StateStore, EventBus, float], None]
    # float — dt текущего тика
```

**Ключевая логика:**

```python
def run(store: StateStore, event_bus: EventBus, dt: float):
    with _lock:
        names = list(_order)  # порядок важен!
    
    for name in names:
        rule = _rules.get(name)
        if rule and rule.enabled:
            try:
                rule.func(store, event_bus, dt)
            except Exception:
                logger.exception(...)
                event_bus.publish(Event("dependency.violation", ...))
```

**Вызывается:**
- Один раз за тик
- **ПОСЛЕ** `tick()` всех узлов
- **ДО** `event_bus.dispatch_pending()`

Это позволяет правилам:
- Читать свежее состояние после обновления узлов
- Реагировать на события узлов текущего тика (например, `battery.critical`)
- Писать `control_*` параметры, которые узлы прочитают в следующем тике

**Управление правилами:**
```python
add_rule(name, func, enabled=True)   # Добавить правило
remove_rule(name)                    # Удалить правило
enable(name) / disable(name)         # Включить/выключить
list_rules()                         # Список правил с состоянием
```

---

### 8. ScenarioEngine (`scenario_engine.py`)

**Назначение:** Управляет запуском, выполнением и завершением кризисных сценариев.

**Жизненный цикл сценария:**

```python
class CrisisScenario:
    def on_start(store, event_bus):
        # Однократный эффект при запуске (например, публикация события кризиса)
    
    def on_tick(store, event_bus, dt):
        # Эффект, повторяющийся каждый тик
    
    def on_end(store, event_bus):
        # Снятие эффекта при завершении
    
    def is_finished(store) -> bool:
        # Дополнительное условие завершения
```

**Условия завершения:**
1. **По времени:** `duration_s` истекла
2. **По условию:** `is_finished(store)` вернул `True`
3. **Принудительно:** вызов `stop_crisis(name)`

**Типы триггеров:**

```python
TimerTrigger        # Периодический запуск (каждые N секунд)
RandomTrigger       # Случайный запуск (вероятность на тик + cooldown)
ConditionTrigger    # Запуск по условию (предикат на StateStore + cooldown)
```

**Ключевая логика:**

```python
def tick(store, event_bus, dt):
    # 1. Тик всех активных сценариев
    _advance_active(store, event_bus, dt)
    
    if dt > 0:  # на паузе новые кризисы не планируем
        # 2. Проверка триггеров
        _check_timers(store, event_bus, dt)
        _check_randoms(store, event_bus, dt)
        _check_conditions(store, event_bus, dt)
```

**Публичный API:**
```python
trigger_crisis(name, store, event_bus, params=None)  # Ручной запуск
stop_crisis(name, store, event_bus)                  # Ручная остановка
active_crises()                                      # Список активных
register_scenario_type(name, factory)                # Регистрация шаблона
```

---

## Система узлов

### Базовый контракт: BaseNode (`nodes/base.py`)

Все узлы наследуются от `BaseNode` и реализуют контракт:

```python
class BaseNode(ABC):
    category: str = "generic"  # "power", "climate", "life_support", "production", "infra"
    
    def __init__(self, node_id: str):
        self.node_id = node_id
    
    @abstractmethod
    def tick(self, dt: float, state: StateStore, event_bus: EventBus) -> list[Event]:
        # Выполняется каждый тик симулятора
        # Читает состояние из state, обновляет его, возвращает события
        ...
    
    @abstractmethod
    def on_event(self, event: Event) -> None:
        # Реакция на событие от другого узла/движка
        # Не должна тяжело считать — только выставлять флаги для tick()
        ...
    
    @abstractmethod
    def get_state(self) -> dict[str, Any]:
        # Начальные значения телеметрии для регистрации в StateStore
        ...
    
    def apply_control(self, action: str, value: Any = None) -> None:
        # Точка входа для внешних управляющих команд
        # По умолчанию бросает NotImplementedError
        ...
```

### Паттерн хранения состояния

**Узел хранит:**
- Только конфигурацию (константы): `capacity_kw`, `day_length_s`, `base_drift_rate` и т.д.
- Хранятся как атрибуты инстанса: `self.capacity_kw = capacity_kw`

**StateStore хранит:**
- Всю изменяемую телеметрию: `output_kw`, `temperature_c`, `charge_kwh`, `status` и т.д.
- Специальные `control_*` параметры для межузловой координации

**Типичный `tick()`:**

```python
def tick(self, dt: float, state: StateStore, event_bus: EventBus) -> list[Event]:
    events: list[Event] = []
    
    # 1. Читаем текущее состояние (копия)
    node = state.get_node(self.node_id)
    
    # 2. Вычисляем новое состояние
    new_value = self._compute_something(node, dt)
    
    # 3. Пишем обратно в StateStore
    state.update(self.node_id, param=new_value)
    
    # 4. Генерируем события (если надо)
    if new_value > threshold:
        events.append(Event("node.overload", self.node_id, {...}))
    
    return events
```

### Категории узлов

#### 1. Энергетика (`nodes/power.py`)

**SolarInverter** — производитель энергии
- Мощность зависит от суточного цикла (синусоида) и `health`
- Параметры: `capacity_kw`, `output_kw`, `irradiance`, `health`, `elapsed_s`
- Деградирует при кризисе `crisis.solar_degradation`

**BatteryBank** — накопитель
- Заряд/разряд управляется `DependencyEngine` (правило `energy_balance_rule`)
- Параметры: `capacity_kwh`, `charge_kwh`, `charge_pct`, `health`, `control_max_discharge_kw`
- Самостоятельно: саморазряд, деградация health при глубоком разряде (<5%)
- Публикует `battery.critical` при низком заряде

**PowerLine** — линия передачи
- Проверяет перегрузку (`current_load_kw > max_capacity_kw`)
- При перегрузке: `status = "tripped"`, событие `line.tripped`
- Автовосстановление через `reset_cooldown_s`

**DieselGenerator** — резервный генератор
- Запускается вручную или автоматически (правило `diesel_autostart_rule`)
- Расходует топливо: `fuel_burn_l_per_kwh * output_kw * dt`
- При исчерпании топлива — автоостанов + событие

**EnergyConsumer** — группа потребителей
- Параметры: `base_demand_kw`, `demand_kw`, `actual_kw`, `priority`, `shed`
- Demand имеет случайный шум (±15%)
- Load shedding управляется `energy_balance_rule`

#### 2. Климат и атмосфера (`nodes/climate.py`)

**ZoneClimate** — температура и давление зоны
- Дрейфует к `target_temp_c` со скоростью `base_drift_rate * control_drift_multiplier`
- `control_drift_multiplier` выставляется правилом `ventilation_coupling_rule`
- Поддерживает разовый шок `external_shock_c` (от кризиса `temperature_anomaly`)

**CO2Sensor** — датчик CO2
- `ppm` растёт от `base_generation_ppm_s * control_generation_multiplier`
- Падает от `control_scrub_effect_ppm_s` (выставляет правило `co2_scrubber_to_sensor_rule`)
- Публикует `crisis.co2_spike` при ppm > 1500

**CO2Scrubber** — скруббер CO2
- Эффективность: `capacity_ppm_s * health` (если `enabled && control_powered`)
- Записывает эффект в `effect_ppm_s` (читается правилом)

**VentilationSystem** — вентиляция
- Влияет на `control_drift_multiplier` и `control_generation_multiplier` через правило
- При `health < 0.4` публикует предупреждение

**HumidityControl** — контроль влажности
- Дрейфует к `target_humidity_pct` с шумом
- Без `enabled` дрейф усиливается

**AirFilter** — воздушный фильтр
- Засоряется со временем: `clog_pct += clog_rate_pct_s * dt`
- `efficiency = 1.0 - clog_pct / 100`
- `apply_control("replace")` обнуляет засор

#### 3. Жизнеобеспечение (`nodes/life_support.py`)

**WaterTank** — резервуар воды
- `level_l` падает от потребления, растёт от `control_inflow_l_s`
- `control_inflow_l_s` выставляется правилом `water_recycling_to_tank_rule`

**WaterRecycling** — переработка воды
- Производит `capacity_l_s * health` (если `control_powered`)
- Записывает в `output_l_s` (читается правилом)

**OxygenGenerator** — генератор кислорода
- `o2_pct` растёт от `output_pct_s` (если `control_powered`)
- Падает от `consumption_pct_s` (базовое потребление людьми)

**WasteManagement** — управление отходами
- `level_kg` растёт от `accumulation_kg_s`
- При `processing=True` уровень падает быстрее

#### 4. Производство (`nodes/production.py`)

Все производственные узлы используют миксин `_JobRunnerMixin`:

**Общая логика задания:**
```python
status: "idle" | "running" | "interrupted" | "done"
job_name: str | None
job_remaining_s: float
```

**Цикл выполнения:**
1. `control("start_job", job_name=..., duration_s=...)` → `status="running"`
2. Каждый тик: `job_remaining_s -= dt`
3. При потере питания (`control_powered=False`): `status="interrupted"`, событие `production.interrupted`
4. При завершении: `status="done"`, событие `production.job_done`

**Узлы:**
- **CNC_Mill** — фрезер (2.5 kW)
- **Printer3D** — 3D-принтер (0.6 kW)
- **SolderingStation** — паяльная станция (0.3 kW)
- **WorkStation** — рабочее место (0.4 kW)

#### 5. Инфраструктура (`nodes/infra.py`)

**MainController** — "мозг" купола
- Агрегирует сводные флаги для дашбордов
- Считает количество узлов с `health < 0.3` или `status in {"tripped", "interrupted"}`
- Выставляет `overall_status: "nominal" | "degraded" | "critical"`

**EmergencyLighting** — аварийное освещение
- Активируется автоматически правилом `emergency_lighting_rule` при дефиците энергии
- Разряжает `battery_pct` во включённом состоянии

**FireSuppression** — пожаротушение
- Виртуальный узел (физически не существует)
- При `triggered=True` расходует `agent_pct`

**StructuralIntegrity** — износ конструкции
- Параметр `wear_pct` медленно растёт всегда
- `control_extra_wear_rate` выставляется правилом `structural_wear_from_crises_rule` (больше критических событий = быстрее износ)

**NetworkNode** — связность сети
- Имитирует задержки/обрывы API Gateway при кризисах
- Параметры: `online`, `latency_ms`, `packet_loss_pct`

---

## Правила зависимостей

Файл: `rules.py`

Правила выполняются **после** всех узлов, но **до** рассылки событий. Порядок выполнения важен!

### 1. energy_balance_rule

**Самое важное правило — управляет энергобалансом купола.**

**Алгоритм:**

```
1. Считаем суммарную выработку (SolarInverter.output_kw + DieselGenerator.output_kw)
2. Считаем суммарный спрос:
   - controllable_demand_kw (CO2Scrubber, WaterRecycling, производство — у них есть control_powered)
   - base_demand_kw (EnergyConsumer — бытовые потребители)
3. Дефицит = demand - production

4. Если дефицит > 0:
   a. Пытаемся покрыть батареями (в пределах control_max_discharge_kw):
      - Вычитаем energy_kwh из battery.charge_kwh
   
   b. Если после батарей дефицит остался:
      - Публикуем crisis.power_loss
      - Отключаем управляемые нагрузки по приоритету:
        * Сначала производство (priority=1)
        * Потом климат (priority=2)
        * Последним жизнеобеспечение (priority=3)
        Отключение: control_powered = False
      
      - Шэдим базовых потребителей по EnergyConsumer.priority (больше число = раньше отключаем)
        Шэдинг: shed = True → actual_kw = 0

5. Если избыток > 0:
   - Восстанавливаем все нагрузки (control_powered=True, shed=False)
   - Заряжаем батареи (распределяем избыток поровну)
```

### 2. diesel_autostart_rule

**Автоматический запуск/останов дизель-генератора.**

```
Если любая батарея charge_pct < 20% и дизель не работает и есть топливо:
    → Запустить: running=True, control_target_output_kw=max_output_kw

Если все батареи charge_pct > 60% и дизель работает:
    → Остановить: running=False
```

### 3. co2_scrubber_to_sensor_rule

**Передаёт эффект скрубберов в датчики CO2.**

```
total_effect = Σ(CO2Scrubber.effect_ppm_s)

Для всех CO2Sensor:
    sensor.control_scrub_effect_ppm_s = total_effect
```

### 4. ventilation_coupling_rule

**Состояние вентиляции влияет на климат и CO2.**

```
Если все VentilationSystem не работают:
    multiplier = 3.0
Иначе:
    avg_health = среднее(VentilationSystem.health)
    multiplier = 1.0 + (1.0 - avg_health) * 2.0

Для всех ZoneClimate:
    zone.control_drift_multiplier = multiplier  # хуже вентиляция → медленнее сходится температура

Для всех CO2Sensor:
    sensor.control_generation_multiplier = multiplier  # хуже вентиляция → быстрее накапливается CO2
```

### 5. water_recycling_to_tank_rule

**Передаёт воду из переработки в резервуары.**

```
total_inflow = Σ(WaterRecycling.output_l_s)

Для всех WaterTank:
    tank.control_inflow_l_s = total_inflow
```

### 6. structural_wear_from_crises_rule

**Кризисы ускоряют износ конструкции.**

```
recent_events = последние 100 событий
critical_count = количество событий с severity=CRITICAL

extra_rate = 0.00003 * critical_count

Для всех StructuralIntegrity:
    structure.control_extra_wear_rate = extra_rate
```

### 7. emergency_lighting_rule

**Включает аварийное освещение при дефиците энергии.**

```
recent_power_loss = есть ли crisis.power_loss в последних 20 событиях

Для всех EmergencyLighting:
    Если есть дефицит и освещение выключено:
        lighting.active = True
    Если дефицита нет и освещение включено:
        lighting.active = False
```

---

## Система кризисов

Файл: `crises.py`

Зарегистрированные кризисы из `register_default_crises()`:

### 1. PowerLossCrisis

**Параметры:**
- `target_node_id: str` — ID узла-производителя или линии для отключения
- `duration_s: float` — длительность (по умолчанию 90с)

**Логика:**
```python
on_start():
    Если target_node_id не указан:
        → Найти первый узел с "output_kw" (производитель)
    
    Если это узел с "capacity_kw":
        → capacity_kw = 0.0  (полное отключение)
    Если это узел с "status":
        → status = "disabled"
    
    Публикует: crisis.power_loss

on_end():
    Восстанавливает сохранённые значения
    Публикует: line.restored
```

**Важно:** Это **внешний триггер** дефицита энергии (искусственное отключение источника). Событие `crisis.power_loss` также публикуется **автоматически** правилом `energy_balance_rule` при реальном дефиците.

### 2. CO2SpikeCrisis

**Параметры:**
- `multiplier: float` — множитель генерации CO2 (по умолчанию 5.0)
- `duration_s: float` — длительность (по умолчанию 60с)

**Логика:**
```python
on_start():
    Для всех узлов с "ppm" и "control_generation_multiplier":
        → control_generation_multiplier = multiplier
    Публикует: crisis.co2_spike

on_end():
    Для всех затронутых узлов:
        → control_generation_multiplier = 1.0
```

### 3. TemperatureAnomalyCrisis

**Параметры:**
- `shock_c: float` — скачок температуры в °C (по умолчанию ±8.0)
- `duration_s: float` — длительность (по умолчанию 45с)
- `zone_node_id: str | None` — конкретная зона (или все зоны)

**Логика:**
```python
on_start():
    Для целевых зон (все или одна):
        → external_shock_c = shock_c  (разовое возмущение)
    Публикует: crisis.temperature_anomaly

# external_shock_c одноразовый — ZoneClimate.tick() сам обнулит его
# on_end() не нужен
```

### 4. SolarDegradationCrisis

**Параметры:**
- `severity: float` — доля потери health в секунду (по умолчанию 0.002)
- `duration_s: float` — длительность (по умолчанию 120с)

**Логика:**
```python
on_start():
    Публикует: crisis.solar_degradation

on_tick(dt):
    Для всех узлов с "irradiance" и "health" (SolarInverter):
        health -= severity * dt
        health = max(0.0, health)

# on_end() намеренно пустой — деградация необратима без ремонта
```

---

## Поток данных и взаимодействие компонентов

### Диаграмма потока данных в одном тике:

```
┌─────────────────────────────────────────────────────────────────────┐
│                  НАЧАЛО ТИКА (SimulatorThread)                      │
└────────────────────────────────┬────────────────────────────────────┘
                                 │
                                 ▼
                    ┌────────────────────────┐
                    │ TimeController         │
                    │ .compute_dt()          │
                    │ → dt (симуляционное    │
                    │   время с учётом       │
                    │   time_scale и паузы)  │
                    └────────────┬───────────┘
                                 │
                                 ▼
        ╔════════════════════════════════════════════════════╗
        ║  ФАЗА 1: Тик всех узлов (последовательно)         ║
        ╚════════════════════════════════════════════════════╝
                                 │
        ┌────────────────────────┴────────────────────────┐
        │                                                  │
        ▼                                                  ▼
┌───────────────┐                                  ┌──────────────┐
│ Node 1        │                                  │ Node N       │
│ .tick(dt, →   │                                  │ .tick(dt, →  │
│   state, →    │  Читает: state.get_node()       │   state, →   │
│   event_bus)  │  Пишет: state.update()          │   event_bus) │
│               │  Возвращает: list[Event]        │              │
└───────┬───────┘                                  └──────┬───────┘
        │                                                  │
        │   События складываются в список,                │
        │   затем публикуются централизованно:            │
        │                                                  │
        └──────────────────┬───────────────────────────────┘
                           │
                           ▼
                  ┌────────────────┐
                  │ EventBus       │
                  │ .publish(event)│  → Кладёт в очередь
                  └────────────────┘    (НЕ рассылает!)
                           │
                           ▼
        ╔════════════════════════════════════════════════════╗
        ║  ФАЗА 2: Сценарии/кризисы                         ║
        ╚════════════════════════════════════════════════════╝
                           │
                           ▼
                  ┌────────────────┐
                  │ ScenarioEngine │
                  │ .tick(store, → │
                  │   event_bus,   │
                  │   dt)          │  → Вызывает on_tick() всех
                  └────────────────┘    активных сценариев
                           │              + проверяет триггеры
                           │              → Мутирует StateStore
                           │              → Публикует события
                           ▼
        ╔════════════════════════════════════════════════════╗
        ║  ФАЗА 3: Правила зависимостей                     ║
        ╚════════════════════════════════════════════════════╝
                           │
                           ▼
                  ┌────────────────┐
                  │DependencyEngine│
                  │ .run(store, →  │  → Выполняет правила по порядку
                  │   event_bus,   │    (см. раздел "Правила")
                  │   dt)          │  → Читает состояние узлов
                  └────────────────┘  → Пишет control_* параметры
                           │          → Публикует события
                           │
                           ▼
        ╔════════════════════════════════════════════════════╗
        ║  ФАЗА 4: Рассылка событий подписчикам             ║
        ╚════════════════════════════════════════════════════╝
                           │
                           ▼
                  ┌────────────────┐
                  │ EventBus       │
                  │ .dispatch_     │  → Достаёт все накопленные
                  │  pending()     │    события из очереди
                  └────────────────┘  → Рассылает подписчикам
                           │          → Добавляет в историю
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│  КОНЕЦ ТИКА → Компенсация дрейфа → Sleep до следующего тика        │
└─────────────────────────────────────────────────────────────────────┘
```

### Взаимодействие через `control_*` параметры:

Паттерн **"producer-consumer через StateStore"**:

```
Пример: CO2Scrubber → CO2Sensor

1. CO2Scrubber.tick():
   effect = capacity_ppm_s * health (если powered)
   state.update(self.node_id, effect_ppm_s=effect)

2. DependencyEngine.run() → co2_scrubber_to_sensor_rule:
   total_effect = Σ(scrubber.effect_ppm_s)
   for sensor in CO2Sensor:
       state.set(sensor.node_id, "control_scrub_effect_ppm_s", total_effect)

3. CO2Sensor.tick() (следующий тик):
   node = state.get_node(self.node_id)
   ppm -= node["control_scrub_effect_ppm_s"] * dt
```

**Ключевые моменты:**
- Производитель пишет свой выход в **свой** узел
- Правило **читает** из производителей, **пишет** в потребителей
- Потребитель **читает** из своего узла в следующем тике

---

## Потокобезопасность

### Архитектура потоков:

```
┌──────────────────┐       ┌──────────────────┐
│  Main Thread     │       │ SimulatorThread  │
│  (API Gateway)   │       │  (daemon)        │
└──────────────────┘       └──────────────────┘
         │                          │
         │  Simulator.get()         │  tick() всех узлов
         │  Simulator.set()         │  + правила
         │  Simulator.control()     │  + сценарии
         │  Simulator.snapshot()    │
         │                          │
         └─────────┬────────────────┘
                   │
                   ▼
            ┌─────────────┐
            │ StateStore  │
            │ (RLock)     │
            └─────────────┘
```

### Гарантии потокобезопасности:

**1. StateStore:**
- Все методы защищены `threading.RLock`
- Возвращает **копии** данных (не мутабельные ссылки)
- Единственный регулярный писатель — SimulatorThread
- Точечная запись извне (API) безопасна

**2. EventBus:**
- `publish()` использует `queue.Queue` (потокобезопасна)
- `dispatch_pending()` вызывается ТОЛЬКО из SimulatorThread
- Подписчики защищены `RLock` при регистрации/отписке

**3. TimeController, NodeRegistry, DependencyEngine, ScenarioEngine:**
- Все защищены `RLock` при изменении структур
- Чтение состояния (например, `time_controller.time_scale`) также под локом

**4. Узлы (BaseNode):**
- Инстансы узлов **не мутируют** свои атрибуты в tick()
- Вся мутабельность только через StateStore
- Безопасно читать конфигурацию узла из любого потока

### Паттерн изоляции:

```python
# Поток 1 (API Gateway)
value = simulator.get(node_id, param)  # Читает копию
simulator.set(node_id, param, new_value)  # Атомарная запись

# Поток 2 (SimulatorThread)
def tick():
    node = state.get_node(node_id)  # Локальная копия
    # ... работа с локальной копией ...
    state.update(node_id, **changes)  # Атомарная пакетная запись
```

**Ключевое решение:** Никто не работает с разделяемыми мутабельными ссылками. Все операции чтения возвращают копии.

---

## Точка входа: `__init__.py`

Функция `create_dome_simulator()` собирает полный симулятор "из коробки":

```python
def create_dome_simulator(tick_interval=4.0, time_scale=1.0) -> Simulator:
    sim = Simulator(tick_interval, time_scale)
    
    # 1. Регистрирует все узлы (энергетика, климат, жизнеобеспечение, производство, инфраструктура)
    #    - 2 солнечных инвертора
    #    - 1 батарейный банк
    #    - 1 дизель-генератор
    #    - 8 линий электропередачи
    #    - 2 группы потребителей
    #    - 2 климатические зоны
    #    - CO2 датчик, скруббер
    #    - Влажность, вентиляция, фильтр
    #    - Резервуары воды, переработка, кислород, отходы
    #    - CNC, 3D-принтер, паяльная станция, рабочее место
    #    - Главный контроллер, аварийное освещение, пожаротушение, износ конструкции, сеть
    
    # 2. Добавляет все правила зависимостей (из build_default_rules())
    #    - energy_balance, diesel_autostart, co2_scrubber_to_sensor, ventilation_coupling,
    #      water_recycling_to_tank, structural_wear_from_crises, emergency_lighting
    
    # 3. Регистрирует все кризисы (из register_default_crises())
    #    - power_loss, co2_spike, temperature_anomaly, solar_degradation
    
    return sim
```

**Использование:**

```python
from dome_simulator import create_dome_simulator

sim = create_dome_simulator(tick_interval=4.0, time_scale=1.0)
sim.start()

# Чтение состояния (из любого потока)
temp = sim.get("climate_residential", "temperature_c")
all_state = sim.get_all()

# Управление (из любого потока)
sim.control("cnc_1", "start_job", job_name="bracket_v2", duration_s=120.0)
sim.set("climate_residential", "target_temp_c", 23.0)

# Кризисы
sim.trigger_crisis("power_loss", params={"target_node_id": "solar_1", "duration_s": 90.0})

# Управление временем
sim.pause()
sim.set_time_scale(2.0)  # Ускорить в 2 раза
sim.resume()

# Остановка
sim.stop()
```

---

## Резюме ключевых архитектурных решений

1. **Разделение Real/Virtual на уровне API Gateway** — симулятор не знает, какие узлы реальные. Все узлы равны.

2. **Состояние в StateStore, не в узлах** — узлы stateless, вся телеметрия централизована. Упрощает персистентность и дебаг.

3. **Отложенная рассылка событий** — подписчики видят консистентное состояние тика, а не "недособранное".

4. **Правила зависимостей как чистые функции** — просто читают/пишут StateStore, не знают о конкретных типах узлов (распознают по наличию полей).

5. **Компенсация дрейфа времени** — абсолютная точка следующего тика вместо `sleep(interval)` на каждой итерации.

6. **Копирование при чтении** — наружу из StateStore и EventBus никогда не утекают мутабельные ссылки.

7. **Декоративность и расширяемость** — регистрация узлов через `@register_node_type`, правил через `add_rule()`, сценариев через `register_scenario_type()`. Добавление новых элементов не требует изменения ядра.

---

Документ подготовлен: 2026-09-07
