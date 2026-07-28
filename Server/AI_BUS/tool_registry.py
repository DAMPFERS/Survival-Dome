"""
Реестр инструментов: JSON-схемы для Function Calling (формат OpenAI/DeepSeek)
+ диспетчер вызовов к общему DomeTelemetryClient.

ВАЖНО: методы связиста (send_external_message, get_signal_strength, ...) и
производственника (start_printer, get_mill_status, ...) в приложенном примере
client.py отсутствуют — предполагается, что вы добавите их по аналогии с уже
существующими методами (get_temperature, toggle_channel и т.д.). Если метод
ещё не реализован, вызов не уронит систему, а вернёт агенту {"error": "..."},
и агент честно ответит в чате, что функция пока недоступна.
"""

import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


def _schema(name: str, description: str, properties: Optional[Dict[str, Any]] = None,
            required: Optional[List[str]] = None) -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
                "required": required or [],
            },
        },
    }


CHANNEL_ID_PROP = {"channel_id": {"type": "integer", "description": "ID канала (1-8)"}}

TOOL_SCHEMAS: Dict[str, Dict[str, Any]] = {
    # --- Энергетик ---
    "get_power_summary": _schema(
        "get_power_summary",
        "Сводка по питанию: солнечная генерация, батарея, общая нагрузка",
    ),
    "get_solar_generation": _schema(
        "get_solar_generation", "Мощность солнечных панелей в kW"
    ),
    "get_battery": _schema("get_battery", "Уровень заряда батареи в процентах"),
    "get_channel_power": _schema(
        "get_channel_power", "Мощность конкретного канала нагрузки в ваттах",
        CHANNEL_ID_PROP, ["channel_id"],
    ),
    "is_channel_enabled": _schema(
        "is_channel_enabled", "Проверить, включен ли канал нагрузки",
        CHANNEL_ID_PROP, ["channel_id"],
    ),
    "get_channels": _schema("get_channels", "Состояние всех каналов нагрузки"),
    "toggle_channel": _schema(
        "toggle_channel", "Переключить канал нагрузки (вкл/выкл)",
        CHANNEL_ID_PROP, ["channel_id"],
    ),

    # --- Телеметрист ---
    "get_climate": _schema(
        "get_climate", "Текущие показатели климата (температура, влажность, CO2)"
    ),
    "get_temperature": _schema("get_temperature", "Текущая температура в градусах Цельсия"),
    "get_humidity": _schema("get_humidity", "Текущая влажность в процентах"),
    "get_co2": _schema("get_co2", "Текущий уровень CO2 в ppm"),

    # --- Связист ---
    "get_signal_strength": _schema(
        "get_signal_strength", "Уровень сигнала связи с внешним миром (0-100)"
    ),
    "list_comm_channels": _schema(
        "list_comm_channels", "Список доступных каналов связи и их статус"
    ),
    "send_external_message": _schema(
        "send_external_message", "Отправить текстовое сообщение во внешний мир",
        {"text": {"type": "string", "description": "Текст сообщения"}}, ["text"],
    ),
    "send_distress_signal": _schema(
        "send_distress_signal", "Отправить аварийный сигнал бедствия"
    ),

    # --- Производственник ---
    "get_printer_status": _schema(
        "get_printer_status", "Статус 3D-принтера (работает/простаивает, задание)"
    ),
    "start_printer": _schema(
        "start_printer", "Запустить 3D-принтер",
        {"job": {"type": "string", "description": "Название/описание задания на печать"}},
    ),
    "stop_printer": _schema("stop_printer", "Остановить 3D-принтер"),
    "get_mill_status": _schema("get_mill_status", "Статус фрезерного станка"),
    "start_mill": _schema(
        "start_mill", "Запустить фрезерный станок",
        {"job": {"type": "string", "description": "Название/описание задания на фрезеровку"}},
    ),
    "stop_mill": _schema("stop_mill", "Остановить фрезерный станок"),
}


class ToolExecutor:
    """Выполняет вызовы инструментов поверх общего DomeTelemetryClient."""

    def __init__(self, client: Any):
        self._client = client
        self._handlers: Dict[str, Callable[[Dict[str, Any]], Any]] = {
            "get_power_summary": lambda a: self._client.get_power(),
            "get_solar_generation": lambda a: {"solar_generation": self._client.get_solar_generation()},
            "get_battery": lambda a: {"battery": self._client.get_battery()},
            "get_channel_power": lambda a: {
                "channel_id": a["channel_id"],
                "power": self._client.get_channel_power(a["channel_id"]),
            },
            "is_channel_enabled": lambda a: {
                "channel_id": a["channel_id"],
                "enabled": self._client.is_channel_enabled(a["channel_id"]),
            },
            "get_channels": lambda a: self._client.get_channels(),
            "toggle_channel": lambda a: self._client.toggle_channel(a["channel_id"]),

            "get_climate": lambda a: self._client.get_climate(),
            "get_temperature": lambda a: {"temperature": self._client.get_temperature()},
            "get_humidity": lambda a: {"humidity": self._client.get_humidity()},
            "get_co2": lambda a: {"co2": self._client.get_co2()},

            "get_signal_strength": lambda a: {"signal_strength": self._client.get_signal_strength()},
            "list_comm_channels": lambda a: self._client.list_comm_channels(),
            "send_external_message": lambda a: self._client.send_external_message(a["text"]),
            "send_distress_signal": lambda a: self._client.send_distress_signal(),

            "get_printer_status": lambda a: self._client.get_printer_status(),
            "start_printer": lambda a: self._client.start_printer(a.get("job", "")),
            "stop_printer": lambda a: self._client.stop_printer(),
            "get_mill_status": lambda a: self._client.get_mill_status(),
            "start_mill": lambda a: self._client.start_mill(a.get("job", "")),
            "stop_mill": lambda a: self._client.stop_mill(),
        }

    def execute(self, name: str, args: Dict[str, Any]) -> Any:
        handler = self._handlers.get(name)
        if handler is None:
            return {"error": f"Неизвестный инструмент: {name}"}
        try:
            return handler(args)
        except AttributeError as e:
            # Метод ещё не реализован в client.py — не роняем систему, сообщаем агенту
            logger.warning("Метод для '%s' не реализован в клиенте: %s", name, e)
            return {"error": f"Функция '{name}' пока не реализована на стороне клиента/сервера"}
        except Exception as e:
            logger.error("Ошибка выполнения инструмента %s(%s): %s", name, args, e)
            return {"error": str(e)}


def get_tool_schemas(tool_names: List[str]) -> List[Dict[str, Any]]:
    """Возвращает список JSON-схем инструментов для конкретного агента (по allowed_tools)."""
    return [TOOL_SCHEMAS[name] for name in tool_names if name in TOOL_SCHEMAS]
