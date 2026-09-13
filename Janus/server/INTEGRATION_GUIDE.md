# Руководство по интегрированному серверу

## Обзор

`server_integrated.py` — это WebSocket-сервер, объединяющий реальное оборудование купола и виртуальный симулятор в единую систему.

### Ключевые возможности:

✅ **Гибридная телеметрия** — real + virtual узлы работают одновременно  
✅ **Hot-swap на лету** — переключение между real ↔ virtual без перезагрузки  
✅ **Полная интеграция** — зависимости корректно работают с любыми источниками данных  
✅ **Расширенное управление** — управление виртуальными узлами, кризисами, временем симуляции  
✅ **Обратная совместимость** — работает даже если реальное железо недоступно

---

## Быстрый старт

### 1. Запуск сервера

```bash
cd D:\PROGRAMS\Survival-Dome\Janus\server
python server_integrated.py
```

**Вывод при успешном запуске:**
```
2026-09-14 00:28:36 [INFO] Initializing dome simulator...
2026-09-14 00:28:36 [INFO] Dome simulator started
2026-09-14 00:28:36 [INFO] Node modes initialized: 12 real nodes
2026-09-14 00:28:36 [INFO] SmartDeviceManager started
2026-09-14 00:28:36 [INFO] WebSocket server started on ws://0.0.0.0:8765
```

### 2. Подключение клиента

```javascript
const ws = new WebSocket('ws://localhost:8765');

ws.onmessage = (event) => {
    const message = JSON.parse(event.data);
    console.log('Type:', message.type);
    console.log('Data:', message.data);
};
```

---

## Архитектура

### Компоненты системы

```
┌─────────────────────────────────────────────────────────────┐
│                  server_integrated.py                       │
│                                                             │
│  ┌─────────────┐  ┌──────────────┐  ┌──────────────────┐    │
│  │   Real HW   │  │  Simulator   │  │  WebSocket API   │    │
│  │             │  │              │  │                  │    │
│  │ • Inverter  │  │ • 65+ nodes  │  │ • Telemetry      │    │
│  │ • Relays    │  │ • Rules      │  │ • Control        │    │
│  │ • Sensors   │  │ • Crises     │  │ • Mode switch    │    │
│  └──────┬──────┘  └───────┬──────┘  └─────────┬────────┘    │
│         │                 │                   │             │
│         └────────┬────────┘                   │             │
│                  │                            │             │
│         ┌────────▼─────────┐          ┌───────▼──────┐      │
│         │  sync_real_to_   │◄─────────│  Clients     │      │
│         │  simulator()     │          └──────────────┘      │
│         └──────────────────┘                                │
└─────────────────────────────────────────────────────────────┘
```

### Маппинг real → virtual

| Реальное устройство | Узел симулятора | Параметры |
|---------------------|-----------------|-----------|
| InverterMonitor | `solar_1` | `output_kw` |
| InverterMonitor (SOC) | `battery_1` | `charge_pct`, `charge_kwh` |
| Реле L1-L8 | `line_1` - `line_8` | `status`, `current_load_kw` |
| CO2 Sensor | `co2_sensor_1` | `ppm` |
| Climate Sensor | `climate_residential` | `temperature_c`, `humidity_pct` |

### Режимы узлов

Каждый узел может быть в одном из двух режимов:

- **`"real"`** — данные берутся с реального железа, виртуальная логика узла отключена
- **`"virtual"`** — данные генерируются симулятором

**По умолчанию** (если железо доступно):
- `solar_1`, `battery_1`, `line_1-8`, `co2_sensor_1`, `climate_residential` → **real**
- Все остальные 50+ узлов → **virtual**

---

## WebSocket Protocol

### 1. Телеметрия (Server → Client)

**Отправляется автоматически каждые ~4 секунды**

```json
{
  "type": "telemetry",
  "timestamp": 1694635815.123,
  "data": {
    "nodes": {
      "solar_1": {
        "output_kw": 8.5,
        "irradiance": 0.85,
        "health": 1.0,
        "control_override_source": "real",
        "category": "power"
      },
      "battery_1": {
        "capacity_kwh": 0.96,
        "charge_kwh": 0.768,
        "charge_pct": 80.0,
        "health": 1.0,
        "control_override_source": "real"
      },
      "line_1": {
        "status": "ok",
        "current_load_kw": 2.3,
        "control_override_source": "real"
      },
      "co2_sensor_1": {
        "ppm": 450.0,
        "control_override_source": "real"
      },
      // ... еще 60+ узлов
    },
    "active_crises": [],
    "sim_time": 1245.6,
    "time_scale": 1.0,
    "node_modes": {
      "solar_1": "real",
      "battery_1": "real",
      // ...
    }
  }
}
```

**Ключевые поля:**
- `nodes` — состояние всех 65+ узлов симулятора
- `active_crises` — список активных кризисов
- `sim_time` — накопленное симуляционное время (сек)
- `time_scale` — множитель скорости (1.0 = реальное время)
- `node_modes` — режим каждого узла (real/virtual)

---

### 2. Управление узлом (Client → Server)

#### Включить/выключить линию

```json
{
  "type": "control",
  "node_id": "line_1",
  "action": "enable"  // или "disable"
}
```

#### Запустить задание на производственном узле

```json
{
  "type": "control",
  "node_id": "cnc_1",
  "action": "start_job",
  "job_name": "part_manufacturing",
  "duration_s": 120.0
}
```

#### Управление дизель-генератором

```json
{
  "type": "control",
  "node_id": "diesel_1",
  "action": "start"  // или "stop"
}

// Установить целевую мощность
{
  "type": "control",
  "node_id": "diesel_1",
  "action": "set_target_output_kw",
  "value": 10.0
}
```

**Ответ сервера:**

```json
{
  "type": "control_response",
  "success": true,
  "node_id": "line_1",
  "action": "enable",
  "mode": "real"  // или "virtual"
}
```

---

### 3. Переключение режима узла (Client → Server)

#### Перевести узел в виртуальный режим

```json
{
  "type": "switch_mode",
  "node_id": "solar_1",
  "mode": "virtual"
}
```

**Эффект:** узел перестанет брать данные с реального инвертора и начнёт симулироваться виртуально.

#### Перевести узел в реальный режим

```json
{
  "type": "switch_mode",
  "node_id": "solar_1",
  "mode": "real"
}
```

**Эффект:** 
1. Проверяется доступность реального устройства
2. Симулятор синхронизируется с текущими реальными значениями
3. Узел переходит в режим real

**Ответ сервера:**

```json
{
  "type": "switch_mode_response",
  "success": true,
  "node_id": "solar_1",
  "mode": "real"
}
```

**Возможные ошибки:**

```json
{
  "type": "switch_mode_response",
  "success": false,
  "error": "Real device not available"
}
```

---

### 4. Управление кризисами (Client → Server)

#### Запустить кризис

```json
{
  "type": "trigger_crisis",
  "crisis_name": "power_loss",
  "params": {
    "target_node_id": "solar_1",
    "duration_s": 90
  }
}
```

**Доступные кризисы:**
- `power_loss` — отключение источника энергии
- `co2_spike` — резкий рост CO2 (multiplier=5.0)
- `temperature_anomaly` — температурный шок (shock_c=±8.0°C)
- `solar_degradation` — деградация солнечных панелей (severity=0.002)

#### Остановить кризис

```json
{
  "type": "stop_crisis",
  "crisis_name": "power_loss"
}
```

---

### 5. Управление временем симуляции (Client → Server)

#### Ускорить симуляцию в 2 раза

```json
{
  "type": "time_control",
  "action": "set_scale",
  "value": 2.0
}
```

#### Поставить на паузу

```json
{
  "type": "time_control",
  "action": "pause"
}
```

#### Возобновить

```json
{
  "type": "time_control",
  "action": "resume"
}
```

---

## Как работает hot-swap

### Механизм переключения

Когда узел находится в режиме **"real"**:

1. **Перед каждым tick:** `sync_real_to_simulator()` записывает реальные данные в StateStore
2. **В tick узла:** проверяется `control_override_source == "real"` → виртуальная логика пропускается
3. **Правила зависимостей:** читают актуальное состояние из StateStore (неважно, откуда оно взялось)

**Пример: energy_balance_rule**

```python
# Правило читает output_kw всех производителей
total_production_kw = sum(p["output_kw"] for p in producers.values())

# Если solar_1 в режиме "real":
#   output_kw = реальные данные с инвертора (записаны sync_real_to_simulator)
# Если solar_1 в режиме "virtual":
#   output_kw = симуляционное значение (рассчитано в SolarInverter.tick())

# Правило работает корректно в обоих случаях!
```

### Синхронизация при переключении

**Virtual → Real:**
1. Получаем реальные данные с устройства
2. Записываем их в симулятор через `simulator.update()`
3. Выставляем `control_override_source = "real"`
4. Меняем режим в `node_modes`

**Real → Virtual:**
1. Убираем флаг `control_override_source` (→ `None`)
2. Меняем режим в `node_modes`
3. Симулятор продолжит с текущего состояния виртуально

---

## Управление командами в гибридном режиме

### Логика маршрутизации

```python
if node_mode == "real":
    # 1. Отправляем команду на реальное железо
    success = await send_to_real_device(node_id, action, value)
    
    # 2. Оптимистично обновляем симулятор
    if success:
        simulator.control(node_id, action, value)
    
    # 3. На следующем тике sync_real_to_simulator() перезапишет
    #    актуальным состоянием с железа

elif node_mode == "virtual":
    # Только в симулятор
    simulator.control(node_id, action, value)
```

**Преимущества:**
- Реальное железо управляется напрямую (быстро, надёжно)
- Симулятор знает о команде сразу (зависимости работают корректно)
- Синхронизация устраняет расхождения

---

## Конфигурация

### Изменение маппинга устройств

Отредактируйте `REAL_TO_SIM_MAPPING` в `server_integrated.py`:

```python
REAL_TO_SIM_MAPPING = {
    "inverter": {
        "node_id": "solar_1",  # Можно изменить на "solar_2"
        "mappings": {
            "generated_power_kw": "output_kw",
        }
    },
    # ...
}
```

### Изменение режимов по умолчанию

Отредактируйте функцию `init_default_modes()`:

```python
def init_default_modes():
    global node_modes
    node_modes = {
        "solar_1": "virtual",  # Изменили на virtual
        "battery_1": "real",
        # ...
    }
```

### Параметры симулятора

При вызове `create_dome_simulator()` в `main()`:

```python
simulator = create_dome_simulator(
    tick_interval=4.0,   # Интервал обновления (сек)
    time_scale=1.0       # Скорость времени
)
```

---

## Отладка

### Логирование

Уровень логирования задан в `logging.basicConfig(level=logging.INFO)`.

Для детальной отладки измените на `logging.DEBUG`.

### Проверка режимов узлов

В телеметрии есть поле `node_modes`:

```json
"node_modes": {
  "solar_1": "real",
  "battery_1": "real",
  "diesel_1": "virtual"
}
```

### Проверка флага override

Каждый узел имеет поле `control_override_source`:

```json
"solar_1": {
  "output_kw": 8.5,
  "control_override_source": "real"  // <-- данные с реального железа
}

"diesel_1": {
  "output_kw": 0.0,
  "control_override_source": null    // <-- виртуальные данные
}
```

---

## Часто задаваемые вопросы

### 1. Что если реальное железо недоступно?

Сервер автоматически перейдёт в режим **simulation-only**:

```
[WARNING] Real hardware modules not available
[INFO] Running in simulation-only mode
```

Все узлы будут в режиме `"virtual"`.

### 2. Можно ли переключать режимы во время работы?

Да! Используйте команду `switch_mode`:

```json
{"type": "switch_mode", "node_id": "solar_1", "mode": "virtual"}
```

### 3. Как узнать, какие узлы доступны для hot-swap?

Смотрите на маппинг `REAL_TO_SIM_MAPPING` — только эти узлы можно переключать в режим "real".

### 4. Что происходит с зависимостями при переключении?

Зависимости работают **корректно** независимо от режима узлов. Правила читают актуальное состояние из StateStore и не знают, откуда пришли данные.

### 5. Батарея: ёмкость 40Ah * 24V = 960Wh. Почему в коде 0.96 kWh?

Верно! 960Wh = 0.96 kWh. Конверсия выполняется в маппинге:

```python
"conversions": {
    "charge_pct_to_kwh": lambda pct: (pct / 100.0) * 0.96
}
```

---

## Примеры использования

### Пример 1: Запуск кризиса и отслеживание

```javascript
// Запускаем кризис потери энергии
ws.send(JSON.stringify({
    type: "trigger_crisis",
    crisis_name: "power_loss",
    params: {
        target_node_id: "solar_1",
        duration_s: 90
    }
}));

// В телеметрии появится:
// "active_crises": ["power_loss"]
// "solar_1": {"output_kw": 0.0, ...}
// energy_balance_rule активирует load shedding
```

### Пример 2: Управление временем симуляции

```javascript
// Ускорить в 10 раз (для тестирования)
ws.send(JSON.stringify({
    type: "time_control",
    action: "set_scale",
    value: 10.0
}));

// Кризисы и деградация будут происходить в 10 раз быстрее
```

### Пример 3: Переключение всех узлов в virtual режим

```javascript
const nodesToSwitch = [
    "solar_1", "battery_1", 
    "line_1", "line_2", "line_3", "line_4",
    "line_5", "line_6", "line_7", "line_8",
    "co2_sensor_1", "climate_residential"
];

for (const nodeId of nodesToSwitch) {
    ws.send(JSON.stringify({
        type: "switch_mode",
        node_id: nodeId,
        mode: "virtual"
    }));
}

// Теперь все данные симулируются виртуально
```

---

## Список всех управляемых узлов

### Энергетика
- `solar_1`, `solar_2` — солнечные инверторы
- `battery_1` — батарейный банк
- `diesel_1` — дизель-генератор
- `line_1` - `line_8` — линии передачи
- `consumer_residential`, `consumer_workshop` — потребители

### Климат
- `climate_residential`, `climate_workshop` — зональный климат
- `co2_sensor_1` — датчик CO2
- `co2_scrubber_1` — скруббер CO2
- `humidity_1` — контроль влажности
- `ventilation_1` — вентиляция
- `air_filter_1` — воздушный фильтр

### Жизнеобеспечение
- `water_tank_1` — резервуар воды
- `water_recycling_1` — переработка воды
- `oxygen_gen_1` — генератор кислорода
- `waste_1` — управление отходами

### Производство
- `cnc_1` — CNC-фрезер
- `printer_1` — 3D-принтер
- `solder_1` — паяльная станция
- `workstation_1` — рабочее место

### Инфраструктура
- `main_controller` — главный контроллер
- `emergency_lights_1` — аварийное освещение
- `fire_suppression_1` — пожаротушение
- `structure_1` — износ конструкции
- `network_1` — сетевой узел

---

## Контакты и поддержка

Проект: **Survival-Dome / Janus Server**  
Версия: **1.0 (Integrated)**  
Дата: **2026-09-14**

---

**Happy coding!** 🚀
