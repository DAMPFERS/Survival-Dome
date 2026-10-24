# Интеграция quest-ai с сервером купола

quest-ai подключается к серверу купола (`server/server_integrated.py`) как обычный клиент
WebSocket: первым сообщением — `{"type": "auth", "key": DOME_ACCESS_KEY}`, затем получает
телеметрию раз в 2 с и отправляет `control` / `describe_nodes` с `request_id`
(`app/dome_client.py`).

Протокол, узлы и их поля — [docs/participants/01_API.md](../../docs/participants/01_API.md).
Админские запросы (кризисы, время, режимы real/virtual, смена) — `server/admin/README.md`
и `server/admin/Инструкция администратора.md`.

## Что использует quest-ai

| Где | Что |
|---|---|
| `app/dome_client.py` | Подключение, авторизация, переподключение, телеметрия, каталог `describe_nodes`, `control` |
| `app/prompts.py` | Сводка ключевых узлов купола для промпта Хранителя (`KEY_NODES`) |
| `app/tools.py` | Инструменты LLM → действия узлов; граница полномочий Хранителя — `KEEPER_ALLOWED_ACTIONS` |
| `app/panels.py` | Данные веб-панелей: участника (`/api/telemetry`) и энергетика (`/api/energy`) |

## Ключ доступа

`DOME_ACCESS_KEY` в `.env`. С ключом администратора сервер Хранителя не ограничивает — действия
режет `KEEPER_ALLOWED_ACTIONS`. С ключом команды действуют права участника
(`server/authorization/access_policy.json`), а вне смены этой команды — только чат (роль `guest`).

Схема линий щитка: 1 — производственный отдел (рабочее место и вытяжка FabLab), 2 — бокс ФабЛаб,
3 — 3D-принтер, 4 — ЧПУ-фрезер, 5 — бокс Химлаб, 6 — основное освещение, 7 — аварийное освещение,
8 — резервная. Источник истины — `PANEL_LINES` в `server/simulation/dome_simulator/nodes/power.py`.
