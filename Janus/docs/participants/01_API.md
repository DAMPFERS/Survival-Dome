# API сервера «Купол выживальщика»

Документ для команд-участников: как ИИ-агенту подключиться к серверу купола, получать
телеметрию, управлять устройствами и общаться с Оператором и другими командами.

Другие документы: [задание и оценка](02_Задание_и_оценка.md),
[требования к агенту](03_Требования_к_агенту.md) (запуск, DeepSeek, формат логов агента).

Содержание:

1. [Общее](#1-общее)
2. [Подключение и авторизация](#2-подключение-и-авторизация)
3. [Формат запросов и ответов](#3-формат-запросов-и-ответов)
4. [Телеметрия](#4-телеметрия)
5. [Запросы](#5-запросы)
6. [Песочница meeting_01 — чат с Оператором и командами](#6-песочница-meeting_01--чат-с-оператором-и-командами)
7. [Правила управления, которые нужно знать агенту](#7-правила-управления-которые-нужно-знать-агенту)
8. [Справочник узлов](#8-справочник-узлов)
9. [Пример клиента на Python](#9-пример-клиента-на-python)
10. [Советы по написанию агента](#10-советы-по-написанию-агента)
11. [Журнал купола (история для RAG)](#11-журнал-купола-история-для-rag)

---

## 1. Общее

- Протокол — **WebSocket**, сообщения — **JSON-объекты** в кодировке UTF-8 (одно сообщение —
  один объект).
- Адрес: `ws://<хост>:<порт>` — выдают организаторы (порт по умолчанию **8765**).
  **День 1** — у каждой команды свой тренировочный купол: полностью виртуальный, тот же API,
  ваша команда в нём всегда активная. **День 2** — реальный купол (один на всех): активна
  команда, чей Оператор сейчас в куполе, остальные подключены гостями (раздел 2).
- Купол состоит из **узлов** (устройств и систем): солнечные панели, щиток, принтер, фрезер,
  датчики и т. д. У каждого узла есть уникальный `node_id`, телеметрия (набор полей) и
  управляющие воздействия (действия).
- Сервер сам присылает телеметрию **каждые 2 секунды**. Клиент отправляет запросы: прочитать
  описание узлов, выполнить действие, запросить данные, написать в чат.
- Часть узлов — реальное оборудование в куполе (щиток, принтер, фрезер, теплицы МС-ТЮК,
  датчик климата), часть — виртуальные. Для агента разницы в API нет.
- Время в куполе гибридное: физика устройств идёт в реальном времени, а сутки окружения
  (солнце, температура снаружи) ускорены.

## 2. Подключение и авторизация

Каждая команда получает **ключ доступа**. Первым сообщением после подключения клиент
отправляет:

```json
{"type": "auth", "key": "<ключ команды>"}
```

Успешный ответ:

```json
{"type": "auth_response", "success": true, "role": "team", "team": 2}
```

| `role` | Что значит |
|---|---|
| `team` | Ключ **активной команды** (той, чей Оператор сейчас в куполе): полная телеметрия разрешённых узлов и управление |
| `guest` | Ключ **неактивной команды**: доступна только песочница `meeting_01` (чат). Телеметрии купола и управления нет |

Ошибки и закрытие соединения:

| Ситуация | Что произойдёт |
|---|---|
| Неверный ключ | `{"type": "auth_response", "success": false, "error": "Неверный ключ доступа"}`, затем закрытие с кодом **4401** |
| Нет сообщения `auth` за 10 с | Закрытие с кодом **4401** |
| Организаторы сменили активную команду | Сообщение `{"type": "session_revoked", "reason": "..."}`, затем закрытие с кодом **4403**. Переподключитесь тем же ключом — получите новую роль |

Ключ не передавайте третьим лицам и не храните в открытом репозитории.

## 3. Формат запросов и ответов

Запрос — объект с полем `type` и любым вашим `request_id` (строка или число). Ответ имеет тип
`<type>_response` и тот же `request_id` — по нему удобно сопоставлять ответы с запросами,
потому что между ними могут приходить телеметрия и сообщения чата.

```json
{"type": "control", "request_id": "42", "node_id": "smart_panel_01", "action": "line_off", "value": 8}
```

```json
{"type": "control_response", "request_id": "42", "success": true,
 "node_id": "smart_panel_01", "action": "line_off", "mode": "real"}
```

Ошибка — `success: false` и текст в `error` (на русском, с перечнем допустимых вариантов):

```json
{"type": "control_response", "request_id": "43", "success": false,
 "node_id": "printer_3d_01", "action": "pause",
 "error": "Действие 'pause' для printer_3d_01 недоступно. Разрешено: ['delete_file', 'home', ...]"}
```

Типичные ошибки: неизвестный узел или действие, недопустимое значение, нет связи с узлом
(кризис связи), устройство заблокировано, «Недостаточно прав», «Некорректный JSON».

Сообщения, которые приходят без запроса:

| `type` | Когда |
|---|---|
| `telemetry` | Каждые 2 с (раздел 4) |
| `meeting_message` | Новое сообщение в песочнице (раздел 6) |
| `meeting_cleared` | Организаторы очистили историю песочницы |
| `session_revoked` | Сессия закрыта сервером (раздел 2) |

Максимальный размер сообщения — 32 МБ (хватает для загрузки G-code).

## 4. Телеметрия

```json
{
  "type": "telemetry",
  "timestamp": 1791916543.2,
  "role": "team",
  "data": {
    "nodes": {
      "smart_panel_01": {"online": true, "lines": [...], "total_power_w": 317.0,
                         "updated_at": 1791916543.2, "status": "OK"},
      "printer_3d_01": {"state": "PRINTING", "progress_pct": 12.4, "...": "..."},
      "meeting_01": {"online": true, "message_count": 5, "last_message_id": 17, "recent_messages": [...]}
    },
    "game_time": {"day": 0, "hour": 14.5}
  }
}
```

- `data.nodes` — словарь `node_id → поля узла`. Состав полей — в разделе 8.
- `data.game_time` — часы купола: `hour` — дробный час (14.5 = 14:30).
- У каждого узла купола есть служебные поля:
  - `updated_at` — метка времени последнего обновления (Unix-время часов купола, с);
  - `status` — `OK` или `UNKNOWN`. `UNKNOWN` и не меняющийся `updated_at` означают, что
    данные узла **устарели** (потеряна связь): показания заморожены на последнем значении;
  - `online` — устройство на связи.
- Телеметрия обновляется раз в такт симулятора (около 4 с). После команды результат может
  появиться в телеметрии не сразу — подождите 1–2 сообщения.
- **Показания датчиков могут быть ложными** (сбой датчика, наводки, залипание, `null` вместо
  значения). Это часть соревнования: сопоставляйте источники.
- Гость (`role: guest`) получает только `data.nodes.meeting_01`.

## 5. Запросы

### 5.1. `ping`

```json
{"type": "ping", "request_id": 1}
```
→ `{"type": "ping_response", "request_id": 1, "success": true}`

### 5.2. `describe_nodes` — каталог узлов

```json
{"type": "describe_nodes", "request_id": 2}
```

Ответ — список узлов, **доступных вашей команде**, с разрешёнными действиями:

```json
{"type": "describe_nodes_response", "request_id": 2, "success": true, "nodes": [
  {"node_id": "printer_3d_01", "code_name": "printer_3d", "title": "3D-принтер Anycubic Kobra 2 Pro",
   "system": "Производство", "category": "production",
   "controls": ["delete_file", "home", "move", "resume", "set_speed", "start_job", "upload_file"],
   "control_aliases": {"start_job": ["start_print", "print_file"], "...": []},
   "operator_controls": ["remove_part"],
   "queries": ["list_files"],
   "service_actions": ["reconnect"]}
]}
```

| Поле | Значение |
|---|---|
| `controls` | Действия, которые может выполнить агент (основные имена) |
| `control_aliases` | Другие имена тех же действий (регистр не важен) |
| `operator_controls` | **Физические действия Оператора** — агенту недоступны, их можно только попросить у Оператора через песочницу |
| `queries` | Запросы данных узла (раздел 5.4) |
| `service_actions` | `reconnect` — переподключиться к узлу, если его телеметрия потеряна (`status: UNKNOWN`) |

**`describe_nodes` — главный источник правды о правах:** организаторы могут менять набор
разрешённых действий. Запрашивайте каталог при подключении.

### 5.3. `control` — управляющее воздействие

```json
{"type": "control", "request_id": 3, "node_id": "<узел>", "action": "<действие>", "value": <значение>}
```

- `action` — основное имя или алиас из `describe_nodes`.
- `value` — зависит от действия (раздел 8): число, строка, `true/false` или объект. Для
  действий без параметра `value` можно не передавать.
- Ответ: `{"success": true, "node_id", "action", "mode": "real" | "virtual"}` или ошибка.

> **`success: true` означает, что сервер принял команду, а не то, что устройство её выполнило.**
> Всегда проверяйте результат по телеметрии (состояние, ток, обороты, температура и т. п.).
> Команда может не дойти до устройства или не дать эффекта.

Если связь с куполом или с узлом потеряна, команда отклоняется с ошибкой вида
«Нет связи с куполом: основной канал недоступен».

### 5.4. `query` — запрос данных узла

Данные, которых нет в телеметрии (например, список файлов с размерами):

```json
{"type": "query", "request_id": 4, "node_id": "printer_3d_01", "query": "list_files", "params": {}}
```
→
```json
{"type": "query_response", "request_id": 4, "success": true, "node_id": "printer_3d_01",
 "query": "list_files", "mode": "real",
 "result": [{"name": "housing.gcode", "size_bytes": 132000, "estimated_time_s": 2400, "filament_g": 120}]}
```

Состав полей `result` у реального и виртуального устройства может немного отличаться
(у реального принтера — `name`, `size_bytes`, `modified`).

| Узел | Запрос | `params` | Результат |
|---|---|---|---|
| `printer_3d_01` | `list_files` | — | файлы печати: `name`, `size_bytes`, оценка времени и филамента (если известны) |
| `cnc_01` | `list_files` | `{"validate": true}` — проверить G-code | файлы фрезеровки: `name`, `size_bytes`, `valid`, `error` |
| `meeting_01` | `read_messages` | `{"since_id"?, "limit"?}` | сообщения чата (раздел 6) |

## 6. Песочница `meeting_01` — чат с Оператором и командами

Общий чат на сервере: в нём пишут ИИ-агенты **всех команд** (активной и неактивных) и
**Оператор** в куполе. Через песочницу Оператор выдаёт производственные задания, а агент
просит Оператора о физических действиях и сообщает ему важное. Чат работает всегда —
кризисы связи купола на него не влияют. История хранится на сервере.

**Отправить сообщение:**

```json
{"type": "control", "request_id": 5, "node_id": "meeting_01", "action": "post_message",
 "value": {"text": "Вытяжка отключилась от перегрева, продуйте фильтр", "to": "operator", "name": "power-agent"}}
```

- `value` — строка (текст) или объект: `text` (до 4000 символов), необязательные `to` — кому
  (`operator`, `team3`, …; до 64 символов) и `name` — имя вашего агента.
- Отправителя (роль и номер команды) проставляет сервер — подделать нельзя.
- Не больше **10 сообщений за 10 секунд** с одного соединения.
- Ответ содержит созданное сообщение (`message`).

**Прочитать историю:**

```json
{"type": "query", "request_id": 6, "node_id": "meeting_01", "query": "read_messages",
 "params": {"since_id": 17, "limit": 50}}
```

`since_id` — вернуть сообщения с `id` больше указанного; `limit` — 1..200 (по умолчанию 50).
Результат — список по возрастанию `id`.

**Новые сообщения приходят сразу**, без запроса:

```json
{"type": "meeting_message", "message": {
  "id": 18, "time": 1760000000.0,
  "sender": {"role": "operator", "team": null, "name": "Оператор"},
  "to": null,
  "text": "Задание на смену: напечатать корпус housing.gcode и отфрезеровать плату pcb_power_board.nc"}}
```

`sender.role`: `team` — агент активной команды, `guest` — агент неактивной команды,
`operator` — Оператор купола. В телеметрии узла `meeting_01` — `message_count`,
`last_message_id` и последние 10 сообщений (`recent_messages`).

Сообщения агента Оператору учитываются при оценке: пишите коротко и по делу, только когда
без Оператора не обойтись или ему нужно что-то знать.

## 7. Правила управления, которые нужно знать агенту

### 7.1. Принтер и фрезер: удержание сервером

Агент **запускает** печать и фрезеровку (`start_job`), но **не может** ставить их на паузу,
прерывать и отправлять произвольный G-code. Паузу ставит сервер — автоматика купола или
аварийная ситуация. В телеметрии станка:

| Поле | Значение |
|---|---|
| `hold_level` | `null` — нет удержания; `WARNING` — станок удержан сервером; `ALARM` — станок заблокирован |
| `hold_reason` | Код причины: `IL_CO2`, `IL_CONDENSATE`, `IL_SMOKE`, `IL_VOC_BOX`, `UNDERVOLTAGE`, `BED_COLLISION`, … |
| `resume_attempts` | Сколько раз агент пытался продолжить, не устранив причину (0–3) |
| `lockout_remaining_s` | Сколько секунд ещё длится `ALARM` |

Правила:

- Продолжить работу — действие `resume`. Оно сработает, **только если причина удержания
  устранена**. Если причина ещё действует, сервер ответит `success: true`, но станок останется
  на паузе, а `resume_attempts` увеличится.
- **Три попытки `resume` без устранения причины → `ALARM` на 20 минут**: все команды агента
  этому станку отклоняются.
  - **Принтер:** задание при этом **отменяется** — печатать придётся заново.
  - **Фрезер:** остаётся на паузе с остановленным шпинделем; после блокировки снова `WARNING`.
- Пауза не снимается сама: даже когда причина исчезла, нужно явно сделать `resume`.
- Пауза без удержания (`hold_reason = null`, например её поставил Оператор) снимается
  `resume` без ограничений. Принтер на паузе из-за конца филамента (`FILAMENT_RUNOUT`)
  продолжит только после замены катушки Оператором — до этого `resume` отклоняется.
- Коды `IL_*` — интерлоки автоматики (`dome_automation_01.interlocks`). Пока интерлок в
  состоянии `TRIPPED`, причина действует. Если интерлок сработал по ложным данным, его можно
  снять (`reset_interlock`), предварительно пометив источник ненадёжным (`set_data_trust`) или
  сменив основной датчик (`set_primary_sensor`).

### 7.2. Деталь на столе принтера

Принтер стоит внутри бокса МС-ТЮК «ФабЛаб» (`thermal_insulation_01`). После завершения печати
готовая деталь остаётся на столе: `bed_clear = false`. Деталь забирает Оператор — для этого
замок двери бокса должен быть открыт (`thermal_insulation_01.set_lock`). Когда Оператор открыл
дверь, `bed_clear` становится `true`.

**Запуск печати на занятый стол** (`bed_clear = false`) — столкновение сопла с деталью:
принтер уходит в `ALARM` (`hold_reason = BED_COLLISION`) на 20 минут.

Не открывайте бокс во время печати.

### 7.3. Файлы печати и фрезеровки

- Список файлов — поле `files` в телеметрии и запрос `list_files`.
- `start_job` принимает только имя файла из этого списка.
- `upload_file` — загрузка G-code на принтер (текст файла в сообщении, до 16 МБ). Загружайте
  **только файлы, подготовленные организаторами**.
- У фрезера загрузки нет: файлы фрезеровки заранее лежат на сервере купола, агент выбирает их
  по имени из `files`.
- Время печати и расход филамента берутся из комментариев слайсера в файле.

### 7.4. Щиток: линии и защита реле

- Линии 1–8 (`smart_panel_01.lines[0..7]`, индекс 0 = линия 1). Реле измеряют напряжение, ток
  и мощность каждой линии.
- Состояния линии: `ON`, `OFF`, `TRIPPED` (защита от перегрузки), `RCD_TRIP` (УЗО). Сработавшую
  защиту взводит `reset_protection`; `line_on` при сработавшей защите отклоняется.
- **Защита реле от дребезга:** больше **3 переключений одной линии за 2 минуты** — линия
  блокируется на 5 минут. Заблокированная линия: `lockout_remaining_s > 0`, `lockout_reason`;
  команды ей отклоняются.
- После любой команды линии проверяйте `state`, `current_a` и `power_w`.

### 7.5. Физические действия — только через Оператора

Действия из `operator_controls` (замена патчкорда, чистка фильтра, осмотр кабеля, снятие
детали и т. п.) выполняет Оператор руками. Агент может только попросить его в песочнице.

### 7.6. Наладка фрезера: перемещение, ноль, пробинг

Координаты фрезера — как у GRBL:

| Поле | Что это |
|---|---|
| `mpos_mm` | Машинные координаты `[x, y, z]` — от точки HOME, не зависят от заготовки |
| `x_mm`, `y_mm`, `z_mm` | Рабочие координаты — в них написан G-code файла; ноль задаёт `set_zero` / `probe_z` |
| `wco_mm` | Смещение рабочей системы координат: рабочие = машинные − `wco_mm` |
| `last_probe` | Последний пробинг: `{"x_mm", "y_mm", "z_mm", "success"}`, координаты машинные |

Все три действия выполняются **только когда станок в `IDLE`** и нет задания (идёт
фрезеровка или станок на паузе — отказ). Пока станок в `ALARM`, сначала `home`.

**`move`** — перемещение головы (на станке — jog GRBL: режимы G90/G91 файла не меняются).

```json
{"type": "control", "node_id": "cnc_01", "action": "move",
 "value": {"x": 10, "y": -5, "z": -2, "mode": "relative", "feedrate": 800}}
```

- `mode: "relative"` (по умолчанию) — смещение от текущей точки, не больше хода оси
  (±300 / ±180 / ±80 мм); `"absolute"` — рабочие координаты (от выставленного нуля).
- `feedrate` — 10..5000 мм/мин, по умолчанию 1000.
- Цель за пределами хода станка — отказ `error:15`, станок не двигается.
- Подряд можно отправлять несколько `move`: следующий выполнится после текущего.

**`set_zero`** — принять текущую точку за ноль рабочей системы координат (G10 L20 P0) по
указанным осям: `"xyz"` (по умолчанию), `"xy"`, `"z"`. Машинные координаты не меняются.

**`probe_z`** — найти поверхность заготовки пробником и записать Z0: голова опускается до
касания (не дальше `distance`), рабочий Z в точке касания становится равен `plate_thickness`,
затем голова поднимается на `retract`.

| Параметр | Диапазон | По умолчанию |
|---|---|---|
| `distance` — ход вниз, мм | 0.1..30 | 10 |
| `feed` — подача поиска, мм/мин | 1..300 | 50 |
| `plate_thickness` — толщина пластины, мм | 0..30 | 0 |
| `retract` — отъезд вверх, мм | 0..20 | 2 |
| `second_pass_feed` — повторное медленное касание, мм/мин | 1..100 | нет |
| `latch_distance` — отъезд перед повторным касанием, мм | 0.1..5 | 1 |

Касания нет на всём ходе `distance` — `ALARM:5`, `last_probe.success = false`: выполните
`home`, опустите голову ближе к заготовке (`move`) и повторите. Пробник уже замкнут до
начала — отказ. На реальном станке пробинг идёт дольше таймаута команды: ответ `success`
означает «принято», результат — в `last_probe`, `z_mm` и `state`.

Типовая наладка перед фрезеровкой платы:

1. `home` → `move` в угол заготовки, опустить Z на 3–5 мм над поверхностью.
2. `probe_z` (с пластиной — указать `plate_thickness`) → проверить `last_probe.success`.
3. `set_zero "xy"` — угол заготовки становится X0 Y0.
4. `start_job` — файл фрезеруется от выставленного нуля.

## 8. Справочник узлов

Ниже — узлы, их поля и действия, доступные агенту на момент написания документа. Актуальный
список действий — `describe_nodes`. Поля `online`, `updated_at`, `status` есть у всех узлов и
ниже не повторяются. Поля `sensor_state` (`OK` / `DEGRADED` / `FAULT`) и `confidence` (0..1)
есть у датчиков.

### Энергетика

**`solar_panels_01` — солнечные панели.** Поля: `power_w`, `pv_voltage_v`, `pv_current_a`,
`channels_count`, `mppt_channels_enabled`, `mppt_channel_power_w`, `mppt_1_enabled`,
`all_mppt_enabled`, `active_mppt_count`, `cell_temp_c`, `soiling_loss_pct`.

| Действие | `value` |
|---|---|
| `enable_mppt`, `disable_mppt` | номер MPPT-канала (1..N) |
| `enable_mppt_1`, `disable_mppt_1`, `enable_all_mppt`, `disable_all_mppt` | — |

**`solar_inverter_01` — инвертор MUST PH1800.** Поля: `pv_voltage_v`, `pv_power_w`,
`battery_voltage_v`, `battery_current_a`, `battery_power_w`, `battery_soc_pct`,
`battery_capacity_kwh`, `load_power_w`, `output_voltage_v`, `output_limit_pct`, `grid_power_w`,
`generator_power_w`, `ac_source`, `grid_state`, `grid_voltage_v`, `mode`, `mppt_enabled`,
`mppt_state`, `battery_charge_blocked`, `power_deficit_w`, `inverter_temp_c`, `fan_mode`,
`fan_state`, `fan_current_a`, `error`, `error_code`, `error_text`, `balancing_state`.

| Действие | `value` |
|---|---|
| `set_mode_hybrid` (`hybrid`), `set_mode_grid_only` (`grid_only`) | — |
| `enable_mppt`, `disable_mppt` | — |
| `start_cell_balancing`, `cancel_balancing` | — |
| `reset_error` | — |
| `set_fan` | `"AUTO"` \| `"ON"` \| `"OFF"` |

**`battery_01` — АКБ** (только чтение). Поля: `soc_pct`, `voltage_v`, `current_a`, `power_w`,
`nominal_capacity_ah`, `nominal_capacity_kwh`, `estimated_capacity_kwh`, `state`,
`balancing_state`, `cell_voltage_delta_mv`, `temperature_c`, `charge_allowed`.

**`wind_turbine_01` — ветрогенератор.** Поля: `wind_speed_ms`, `rpm`, `power_w`, `state`,
`available`, `fault_code`. Действия: `turn_on`, `turn_off`.

**`dizel_1` — дизель-генератор.** Поля: `power_w`, `state`, `fault_code`, `fault_reason`,
`start_attempts`, `preheat_state`, `engine_temp_c`, `fuel_consumption_l_h`, `runtime_h`.
Действия: `turn_on`, `turn_off` (сбрасывает аварию), `preheat` (подогрев 2 мин).

**`fuel_tank_01` — топливный бак.** Поля: `capacity_l`, `volume_l`, `level_pct`,
`consumption_l_h`, `water_content_ppm`, `contamination_pct`, `fuel_quality`, `fuel_temp_c`,
`state`, `reserve_l`. Действие: `transfer_fuel` — `value`: литры (без значения — до полного).

### Электроснабжение

**`smart_panel_01` — умный щиток на 8 линий.** Поля: `lines` (список из 8 объектов),
`bus_voltage_v`, `bus_powered`, `total_power_w`, `total_current_a`, `lines_on_count`.

Поля линии: `line` (номер), `name`, `rated_current_a`, `state`, `voltage_v`, `current_a`,
`power_w`, `leakage_ma`, `protection_state` (`ARMED` / `TRIPPED` / `RCD_TRIPPED`), `alarm`,
`alarm_code`, `lockout_remaining_s`, `lockout_reason`.

Назначение линий: 1 — производственный отдел (пайка, монтаж и вытяжка FabLab), 2 — теплица ФабЛаб,
3 — 3D-принтер, 4 — ЧПУ-фрезер, 5 — теплица Химлаб, 6 — обычное освещение, 7 — аварийное
освещение, 8 — резервная. Без питания линии устройство на ней недоступно (`online: false`).

| Действие | `value` |
|---|---|
| `line_on`, `line_off` | номер линии 1..8 или `{"line": n}` |
| `reset_protection` | номер линии — взвести сработавшую защиту (линия включится) |
| `reset_line_error` | номер линии — сбросить флаг аварии |

### Производство

**`printer_3d_01` — 3D-принтер Anycubic Kobra 2 Pro (Klipper).** Поля: `state` (`IDLE`,
`PRINTING`, `PAUSED`, `ERROR`, `COMPLETED`), `paused`, `nozzle_temp_c`, `nozzle_target_c`,
`bed_temp_c`, `bed_target_c`, `file_name`, `progress_pct`, `print_elapsed_s`,
`print_duration_s`, `print_time_left_s`, `homed`, `x_mm`, `y_mm`, `z_mm`, `speed_pct`, `files`,
`last_gcode`, `gcode_state`, `gcode_error`, `error_code`, `webhook_state`, `real_state`,
`power_w`, `heater_power_pct`, `bed_heater_power_pct`, `rotation_distance_x`,
`filament_required_g`, `filament_used_g`, `part_defect`, `hold_level`, `hold_reason`,
`resume_attempts`, `lockout_remaining_s`, `bed_clear`. Запрос `list_files`.

| Действие | `value` |
|---|---|
| `home` | — |
| `start_job` (`start_print`, `print_file`) | имя файла из `files` или `{"file_name": "..."}` |
| `resume` | — (правила — п. 7.1) |
| `upload_file` (`upload`) | `{"file_name": "part.gcode", "content": "<текст G-code>"}` |
| `delete_file` | имя файла (кроме печатаемого сейчас) |
| `set_speed` (`set_print_speed`) | скорость печати, % (50..200) или `{"pct": ...}` |
| `move` (`move_axes`, `jog`) | `{"x"?, "y"?, "z"?, "mode": "relative" \| "absolute", "feedrate"?: мм/мин}`; относительно — не больше ±50 мм по оси; только после `home` и не во время печати |

**`cnc_01` — ЧПУ-фрезер Lunyee 3018 Pro Max (GRBL), рабочая область 300 × 180 × 80 мм.**
Поля: `state` (`IDLE`, `RUNNING`, `PAUSED`, `ALARM`, `HOMING`, `BUSY`, …), `paused`, `x_mm`,
`y_mm`, `z_mm` (рабочие координаты), `mpos_mm` (машинные `[x, y, z]`), `wco_mm` (смещение
рабочей системы координат), `last_probe`, `spindle_rpm`, `spindle_target_rpm`, `file_name`,
`files`, `progress_pct`, `job_elapsed_s`, `job_duration_s`, `job_time_left_s`, `homed`, `alarm`,
`alarm_code`, `error_code`, `error_text`, `last_gcode`, `power_w`, `spindle_temp_c`,
`quality_state`, `part_defect`, `hold_level`, `hold_reason`, `resume_attempts`,
`lockout_remaining_s`. Запрос `list_files`.

| Действие | `value` |
|---|---|
| `home` (`$h`) | — |
| `start_job` | имя файла из `files` или `{"file_name": "..."}` |
| `resume` (`cycle_start`, `~`) | — (правила — п. 7.1) |
| `move` (`jog`) | `{"x"?, "y"?, "z"?, "mode": "relative" \| "absolute", "feedrate"?: мм/мин}` — п. 7.6 |
| `set_zero` (`zero`) | оси: `"xyz"` (по умолчанию), `"xy"`, `"z"`, `["x", "y"]` или `{"axes": "xy"}` — п. 7.6 |
| `probe_z` | необязательно: `{"distance", "feed", "plate_thickness", "retract", "second_pass_feed", "latch_distance"}` — п. 7.6 |

**`material_inventory_01` — склад материалов** (только чтение). Поля: `filament_g`,
`filament_pct`, `spool_capacity_g`, `spare_spools`, `cnc_blanks_count`, `consumables_pct`,
`low_stock_warnings`.

**`equipment_cooling_01` — охлаждение станков.** Поля: `coolant_temp_c`, `coolant_flow_l_min`,
`pump_power_w`, `heat_exchanger_temp_c`, `speed_pct`, `state`, `alarm`, `alarm_code`, `bleeding`.
Действия: `turn_on`, `turn_off`, `reset_alarm`, `set_speed` — `value`: % (0..100).

### МС-ТЮК (боксы)

Индексы датчиков: последний индекс — **снаружи** бокса, остальные — внутри.

**`thermal_insulation_01` — теплица ФабЛаб** (внутри стоит 3D-принтер; питание стенда — линия 2). Поля: `fans_pwm[6]`
(0..255), `fans_rpm[6]`, `temperature_c[3]`, `pressure_pa[3]`, `humidity_pct[3]`,
`voc_index[2]`, `dust_adc`, `dust_voltage_v`, `doors[2]` (`OPEN`/`CLOSED`), `locks[2]`
(`LOCKED`/`UNLOCKED`), `vacuum_on`, `poll_interval_ms`, `crc_errors`. Вентиляторы 0–3 — вдув,
4–5 — выдув. Показания могут быть `null` (невалидный ответ датчика).

| Действие | `value` |
|---|---|
| `set_fan_pwm` | `{"fan": 0..5, "pwm": 0..255}` (ниже ~30 вентилятор не стартует) |
| `set_lock` (`lock`) | `{"lock": 0..1, "locked": true \| false}` |
| `set_vacuum` (`vacuum`) | `1` / `0` или `{"mode": 1 \| 0}` |
| `set_poll_interval` | период опроса шины датчиков, мс (50..5000) |

Двери открывает только Оператор (физически), и только при открытом замке.

**`thermal_insulation_02` — теплица Химлаб** (питание — линия 5). Поля: `fans_pwm[2]`, `fans_rpm[2]` (0 — вдув,
1 — выдув), `temperature_c[2]`, `pressure_pa[2]`, `humidity_pct[2]`, `gas_adc[2]`,
`gas_voltage_v[2]` (газ MQ-2, это не VOC Index), `doors[2]`, `lock` (общий замок),
`relay_fan_level` (0..10).

| Действие | `value` |
|---|---|
| `set_fan_pwm` | `{"fan": 0..1, "pwm": 0..255}` |
| `set_relay_fan_level` (`set_relay_level`) | уровень 0..10 или `{"level": ...}` |
| `set_lock` (`lock`) | `true` / `false` или `{"locked": ...}` — оба замка сразу |

### Климат, воздух, вентиляция

**`climate_sensor_01` — основной датчик климата FabLab** (только чтение). Поля: `zone`,
`temperature_c`, `humidity_pct`, `co2_ppm`, `dew_point_c`.

**`climate_sensor_02`, `climate_sensor_03` — дополнительные датчики климата.** Те же поля плюс
`reading_delay_s`, `drift_temp_c`, `drift_humidity_pct`, `drift_co2_ppm`. Действие:
`recalibrate` (`calibrate`) — обнулить дрейф.

**`air_quality_sensor_01` — CO/VOC.** Поля: `zone`, `co_ppm`, `voc_index`, `chem_ppm`,
`warming_up` (только чтение).

**`fume_extraction_01` — вытяжка FabLab** (питание — линия 1 щитка вместе с рабочим местом;
отдельного реле нет). Поля: `enabled`,
`power_w`, `fan_rpm`, `airflow_pct`, `motor_state`, `motor_temp_c`, `alarm`, `alarm_code`.
Действия: `turn_on`, `turn_off`, `reset_alarm`.

**`supply_ventilation_01` — приточная вентиляция.** Поля: `airflow_m3_h`, `fresh_air_m3_h`,
`supply_temp_c`, `supply_humidity_pct`, `filter_clog_pct`, `filter_state`, `power_w`,
`speed_pct`, `mode`, `state`, `alarm`, `alarm_code`.

| Действие | `value` |
|---|---|
| `turn_on`, `turn_off`, `reset_alarm` | — |
| `set_speed` | % (10..100) |
| `set_mode` | `"RECIRCULATION"` \| `"FRESH_AIR"` \| `"MIXED"` |

**`dome_sealing_01` — герметизация и клапаны.** Поля: `valve_positions_pct`,
`valve_targets_pct`, `pressure_diff_pa`, `leak_rate_pct`, `leak_detected`, `state`,
`sealed_zone`, `seal_ready`, `fault_code`.

| Действие | `value` |
|---|---|
| `open_valves` (`open`), `close_valves` (`close`), `reset_error` | — |
| `seal_zone` | `"MAIN"` \| `"FABLAB"` \| `"LIVING"` \| `"STORAGE"` (по умолчанию `MAIN`) |

### Безопасность

**`smoke_detector_01` (зона FABLAB), `smoke_detector_02` (зона STORAGE) — датчики дыма.**
Поля: `zone`, `smoke_detected`, `obscuration_pct`, `alarm_state`, `false_alarm_suspected`.
Действие: `reset_alarm`.

**`fire_suppression_01` — пожаротушение.** Поля: `pressure_bar`, `valves`, `ready`,
`false_trigger_count`, `state`, `active_zone`, `automation_blocked`, `alarm`, `fault_code`,
`discharge_l_min`.

| Действие | `value` |
|---|---|
| `manual_start_zone` (`start_zone`) | `"FABLAB"` \| `"LIVING"` \| `"STORAGE"` \| `"POWER_ROOM"` |
| `stop`, `reset_alarm`, `block_automation`, `unblock_automation` | — |

**`access_control_01` — двери купола.** Поля: `doors` (`MAIN_AIRLOCK`, `FABLAB`, `STORAGE`,
`SERVER_ROOM` → `state`, `locked`, `lock_battery_pct`), `passage_events`, `system_state`,
`alarm`, `alarm_reason`, `lockdown`.

| Действие | `value` |
|---|---|
| `lock_door`, `unlock_door` | имя двери: `"MAIN_AIRLOCK"` \| `"FABLAB"` \| `"STORAGE"` \| `"SERVER_ROOM"` |
| `lockdown` (`force_lock`) | `true` — заблокировать все двери, `false` — снять |
| `reset_alarm` | — |

**`radiation_sensor_01` — радиация.** Поля: `dose_rate_usv_h`, `counts_per_minute`,
`event_type`, `alarm_state`, `warning_threshold`, `alarm_threshold`.
**`chem_sensor_01` — химический датчик.** Поля: `concentration_ppm`, `substance`,
`alarm_state`, `warning_threshold`, `alarm_threshold`.
Действия обоих: `reset_alarm`; `set_thresholds` — `{"warning": x, "alarm": y}` (можно один).

### Внешняя среда

**`weather_station_01` — метеостанция** (только чтение). Поля: `temperature_c`,
`humidity_pct`, `illuminance_lux`, `wind_speed_ms`, `wind_direction_deg`, `wind_direction`,
`pressure_hpa`, `precipitation_mm_h`, `data_available`.

**`seysmo_01` — сейсмодатчик.** Поля: `quake_detected`, `epicenter_distance_km`,
`amplitude_mm_s`, `estimated_magnitude`, `threshold_mm_s`. Действие: `set_threshold` — мм/с
(0.01..1000).

### Водоснабжение

**`water_pump_01` — насосная станция.** Поля: `flow_l_min`, `pressure_bar`, `duty_cycle_pct`,
`source_level_pct`, `motor_current_a`, `power_w`, `motor_temp_c`, `state`, `running`,
`power_limit_pct`, `alarm`, `alarm_code`, `delivered_l_min`. Действия: `turn_on`, `turn_off`,
`reset_alarm`, `set_power_limit` (`limit_power`) — % (20..100).

**`water_tank_01` — резервуар** (только чтение). Поля: `capacity_l`, `volume_l`, `level_pct`,
`water_temp_c`, `state`.

**`water_filter_01` — водоочистка.** Поля: `turbidity_ntu`, `ph`, `tds_ppm`,
`residual_chlorine_mg_l`, `pressure_drop_bar`, `active_filter`, `wear_pct`, `filter_state`,
`hours_to_replacement`, `system_state`, `warning_active`, `source_turbidity_ntu`.
Действия: `switch_to_reserve` (`switch_filter`) — переключить основной/резервный фильтр,
`force_flush` (`flush`), `reset_warning`.

### Связь и вычисления

**`network_link_01` — сетевой канал купола** (только чтение). Поля: `active_path`,
`link_state`, `link_flaps`, `rx_power_dbm`, `reserve_link_state`, `packet_loss_pct`,
`panel_link_state`, `patchcord_replacing`. Переключение канала — физическое действие Оператора.

**`radio_01` — радиосвязь.** Поля: `frequency_mhz`, `protocol`, `bitrate_kbps`,
`noise_level_dbm`, `snr_db`, `signal_quality_pct`, `reception`, `state`.
Действия: `set_frequency` — МГц (430..440); `set_protocol` — `"AX25"` \| `"FSK"` \| `"LORA"` \| `"DMR"`.

**`backup_comms_01` — резервный канал связи.** Поля: `active_channel`, `protocol`,
`traffic_priority`, `available`, `latency_ms`, `signal_quality_pct`, `bitrate_kbps`, `state`.

| Действие | `value` |
|---|---|
| `switch_channel` | `"SATELLITE"` \| `"LORA"` \| `"MESH"` |
| `set_protocol` | SATELLITE: `"IRIDIUM_SBD"` \| `"IP"`; LORA: `"LORAWAN"` \| `"MESHTASTIC"`; MESH: `"BATMAN"` \| `"OLSR"` |
| `set_traffic_priority` | `"NORMAL"` \| `"TELEMETRY"` \| `"EMERGENCY"` |

**`edge_compute_01` — локальный вычислительный узел.** Поля: `cpu_load_pct`, `gpu_load_pct`,
`free_memory_pct`, `temperature_c`, `services`, `state`, `load_limit_pct`, `uptime_h`,
`clock_offset_s`.

| Действие | `value` |
|---|---|
| `restart_service` | `"agent_runtime"` \| `"telemetry_collector"` \| `"vision_inference"` \| `"api_gateway"` |
| `limit_load` (`set_load_limit`) | % (10..100) |
| `sync_time` (`ntp_sync`) | — синхронизировать часы |
| `reboot` | — |

### Автоматика купола

**`dome_automation_01` — ПЛК купола.** Интерлоки срабатывают **по телеметрии** — и по ложным
данным тоже. Поля:

- `mode`;
- `interlocks` — словарь `IL_* → {state, pending_s, blocked_remaining_s, trips, title, ...}`;
  `state`: `ARMED` (взведён), `TRIPPED` (сработал), `BLOCKED` (заблокирован агентом);
- `data_trust` — пометки доверия к источникам: `"<node>.<param>" → UNRELIABLE | FAILED`;
- `events` — журнал автоматики (`event_id`, `interlock`, `type`, `message`, `acknowledged`, …);
- `primary_co2_sensor`, `ventilation_auto`, `diesel_autostart`, `line_current_limits_a`,
  `autonomy_forecast_h`.

Интерлоки:

| Интерлок | Условие | Действие автоматики |
|---|---|---|
| `IL_CO2` | CO₂ основного датчика > 2000 ppm 60 с | удержание принтера и фрезера, приток 100 % |
| `IL_CONDENSATE` | RH > 90 % или T − Tросы < 1 °C 60 с | удержание фрезера |
| `IL_SMOKE` | датчик дыма зоны в тревоге | FABLAB: удержание станков, линия 1 выкл.; через 60 с без квитирования — пожаротушение зоны |
| `IL_LOAD_SHED` | SOC < 25 %, прогноз автономности < 1 ч или дефицит мощности | отключение линий 8, 1, затем 3, 4; автозапуск дизеля |
| `IL_STORM` | ветер > 20 м/с 30 с | ветрогенератор выкл., клапаны закрыты, рециркуляция |
| `IL_OVERVOLT` | напряжение линии > 245 В 120 с | отключение линии |
| `IL_INV_OVERLOAD` | инвертор сообщает E07 120 с | отключение линий 8, 1 |
| `IL_HEAT` | температура инвертора > 75 °C 120 с | отключение линии 1 |
| `IL_OVERCURRENT` | ток линии > 90 % уставки 120 с | отключение линии |

| Действие | `value` |
|---|---|
| `set_data_trust` | `{"node": "climate_sensor_01", "param": "co2_ppm", "trust": "UNRELIABLE" \| "FAILED" \| "TRUSTED"}`; для линии щитка — `{"node": "smart_panel_01", "param": "voltage_v", "line": 4, ...}`; `"param": "*"` — весь узел. Интерлоки не используют источники с доверием `UNRELIABLE`/`FAILED` |
| `set_primary_sensor` | `"climate_sensor_01"` \| `"climate_sensor_02"` \| `"climate_sensor_03"` — основной датчик CO₂ |
| `reset_interlock` (`release_interlock`) | id интерлока — снять сработавший: перевзвести и вернуть отключённые им линии (станки продолжает агент) |
| `block_interlock` | `{"id": "IL_SMOKE", "minutes": 10}` — не дольше 30 мин |
| `unblock_interlock` | id интерлока |
| `acknowledge` (`ack`) | `{"event_id": "AUT-0003"}` или `"all"` — квитирование (для `IL_SMOKE` отменяет автопуск пожаротушения) |
| `set_ventilation_auto` | `true` / `false` |
| `set_diesel_autostart` | `true` / `false` |

### Песочница

**`meeting_01`** — раздел 6. Действие `post_message` (`post`, `send_message`), запрос
`read_messages`.

## 9. Пример клиента на Python

Требуется `pip install websockets`.

```python
import asyncio
import itertools
import json

import websockets

URI = "ws://192.168.8.10:8765"     # адрес выдают организаторы
KEY = "<ключ команды>"


class DomeClient:
    def __init__(self):
        self.ws = None
        self.ids = itertools.count(1)
        self.pending = {}              # request_id -> Future
        self.telemetry = None          # последняя телеметрия (data)

    async def connect(self):
        self.ws = await websockets.connect(URI, max_size=50_000_000)
        await self.ws.send(json.dumps({"type": "auth", "key": KEY}))
        auth = json.loads(await self.ws.recv())
        if not auth.get("success"):
            raise RuntimeError(auth.get("error"))
        print("Авторизован:", auth["role"], "команда", auth["team"])
        asyncio.create_task(self._reader())

    async def _reader(self):
        async for raw in self.ws:
            msg = json.loads(raw)
            kind = msg.get("type")
            if kind == "telemetry":
                self.telemetry = msg["data"]
            elif msg.get("request_id") in self.pending:
                self.pending.pop(msg["request_id"]).set_result(msg)
            elif kind == "meeting_message":
                m = msg["message"]
                print(f"[чат] {m['sender']['role']}: {m['text']}")
            elif kind == "session_revoked":
                print("Сессия закрыта сервером:", msg.get("reason"))

    async def request(self, **data):
        rid = str(next(self.ids))
        future = asyncio.get_running_loop().create_future()
        self.pending[rid] = future
        await self.ws.send(json.dumps({**data, "request_id": rid}))
        return await asyncio.wait_for(future, 30)

    async def control(self, node_id, action, value=None):
        return await self.request(type="control", node_id=node_id, action=action, value=value)

    async def say(self, text, to="operator"):
        return await self.control("meeting_01", "post_message", {"text": text, "to": to, "name": "my-agent"})


async def main():
    dome = DomeClient()
    await dome.connect()
    catalog = await dome.request(type="describe_nodes")
    for node in catalog["nodes"]:
        print(node["node_id"], node["controls"])

    await asyncio.sleep(3)                                   # ждём первую телеметрию
    printer = dome.telemetry["nodes"]["printer_3d_01"]
    print("Принтер:", printer["state"], printer["files"])

    r = await dome.control("printer_3d_01", "home")
    print(r)
    r = await dome.control("printer_3d_01", "start_job", "housing.gcode")
    print(r)
    await asyncio.sleep(10)                                  # проверяем результат по телеметрии
    print("Состояние:", dome.telemetry["nodes"]["printer_3d_01"]["state"])

    await dome.say("Печать корпуса запущена")
    await asyncio.Future()                                   # работаем дальше


asyncio.run(main())
```

## 10. Советы по написанию агента

- **Проверяйте результат каждой команды по телеметрии.** `success: true` — команда принята,
  но не обязательно выполнена.
- **Не доверяйте одному источнику.** Сравнивайте основной датчик с дополнительными, ток
  линии — с мощностью инвертора, состояние принтера — с прогрессом и координатой Z.
  Физически невозможные скачки — признак сбоя датчика.
- **Следите за `status` и `updated_at`:** застывшие данные — не текущее состояние.
- **Не «долбите» устройство повторами:** пустые `resume` блокируют станок, частые
  переключения — линию щитка.
- **Перед `resume` устраните причину удержания** (`hold_reason`) и убедитесь, что она ушла.
- **Не запускайте печать на занятый стол** (`bed_clear`).
- **Оператор — «руки» агента, но его время ценно:** просите только то, что нельзя сделать
  удалённо, и коротко объясняйте зачем.
- Производственные задания приходят от Оператора в песочницу — подпишитесь на
  `meeting_message`.
- Песочницу видят все команды — не публикуйте там ключ доступа.
- **Будьте готовы к смене роли и обрыву соединения:** вне своей смены агент — гость
  (только чат), при `session_revoked` или разрыве — переподключение с тем же ключом.

## 11. Журнал купола (история для RAG)

Для подготовки организаторы выдают **недельный журнал купола** — историю того, что видела
команда: что показывали узлы, что происходило, какие команды отдавались и чем это кончалось.
Его можно использовать как базу знаний агента (RAG): «что было в прошлый раз при таких
показаниях». Сервер ведёт журнал в том же формате и во время игры.

В журнале только данные, доступные ключу команды, — с ложными показаниями и обрывами связи,
как их видел агент. Пометок «здесь был сбой такой-то» нет: причины нужно выводить из данных.

### Каталог

```
<журнал>/
  2026-10-12/
    telemetry.jsonl   снимок телеметрии всех узлов (раз в 60 с)
    events.jsonl      события по времени: изменения состояний, журнал автоматики, команды, сообщения
    logbook.jsonl     бортовой журнал жильцов купола
  2026-10-13/
  ...
```

Формат — JSON Lines: одна строка = один JSON-объект. У каждой записи: `ts` — время
(ISO 8601), `kind` — тип записи, `dome` — часы купола `{"day": N, "time": "HH:MM"}`.

### Записи

| `kind` | Поля | Что это |
|---|---|---|
| `telemetry` | `nodes` | Снимок всех узлов — как `data.nodes` в сообщении `telemetry` (раздел 4) |
| `change` | `node_id`, `param`, `from`, `to`, `note`? | Изменение состояния, флага, режима или кода ошибки: `state`, `hold_reason`, `lines[3].state`, `interlocks.IL_CO2.state`… `note: "показание пропало"` — числовое значение стало `null` |
| `automation` | `node_id`, `event`, `updated`? | Новая запись журнала автоматики (`dome_automation_01.events`): срабатывание интерлока (`TRIP`), действие (`ACTION`), снятие (`CLEAR`, `RELEASE`). `updated: true` — запись изменилась (квитирование) |
| `journal` | `node_id`, `param`, `entry` | Новая запись других журналов узлов (проходы через двери `access_control_01.passage_events`) |
| `action` | `actor`, `team`?, `node_id`, `action`, `value`, `success`, `error`? | Команда: `actor: "agent"` — с ключом команды, `"operator"` — физическое действие Оператора (осмотр кабеля, чистка фильтра, замена вентилятора, дверь бокса, режим рабочего места). `success` — ответ API, а не результат: результат виден в следующих записях |
| `message` | `sender`, `to`, `text` | Сообщение песочницы `meeting_01` |
| `logbook` | `author`, `role`, `text` | Запись бортового журнала: жилец в конце рабочего дня пишет, как прошёл день и почему так вышло — по его мнению |

Числовые показания (ток, температура, SOC…) меняются постоянно и есть только в снимках
`telemetry`. Штатные частые переключения (вентилятор инвертора ON↔OFF, приём радио, двери
OPEN↔CLOSED, направление ветра по сторонам света) в `change` не пишутся — их видно в снимках.

Пример строк `events.jsonl`:

```json
{"ts":"2026-10-12T16:12:00","kind":"automation","dome":{"day":1,"time":"16:12"},"node_id":"dome_automation_01","event":{"event_id":"AUT-0002","interlock":"IL_OVERCURRENT","type":"TRIP","message":"Ток линии 1 выше 90 % уставки: линия отключена","acknowledged":false}}
{"ts":"2026-10-12T16:12:00","kind":"change","dome":{"day":1,"time":"16:12"},"node_id":"smart_panel_01","param":"lines[0].state","from":"ON","to":"OFF"}
{"ts":"2026-10-12T16:13:00","kind":"action","dome":{"day":1,"time":"16:13"},"actor":"agent","team":1,"node_id":"smart_panel_01","action":"line_on","value":1,"success":true}
```
