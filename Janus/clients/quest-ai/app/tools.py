"""
Определение инструментов (tools) для function calling.
Эти инструменты позволяют AI управлять системами купола.
"""

# Схема инструментов в формате OpenAI/DeepSeek/Mistral
DOME_CONTROL_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "control_power_line",
            "description": "Включить или выключить линию электропередачи в куполе. Линии нумеруются от 1 до 8.",
            "parameters": {
                "type": "object",
                "properties": {
                    "line_number": {
                        "type": "integer",
                        "description": "Номер линии (1-8)",
                        "minimum": 1,
                        "maximum": 8
                    },
                    "action": {
                        "type": "string",
                        "enum": ["enable", "disable"],
                        "description": "Действие: enable (включить) или disable (выключить)"
                    }
                },
                "required": ["line_number", "action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "control_diesel_generator",
            "description": "Управление дизель-генератором купола: запуск, остановка или установка целевой мощности.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["start", "stop", "set_power"],
                        "description": "Действие: start (запустить), stop (остановить), set_power (установить мощность)"
                    },
                    "target_kw": {
                        "type": "number",
                        "description": "Целевая мощность в кВт (только для действия set_power)",
                        "minimum": 0,
                        "maximum": 20
                    }
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "control_climate_system",
            "description": "Управление системами климат-контроля и жизнеобеспечения купола.",
            "parameters": {
                "type": "object",
                "properties": {
                    "system": {
                        "type": "string",
                        "enum": ["ventilation", "co2_scrubber", "humidity_control"],
                        "description": "Система: ventilation (вентиляция), co2_scrubber (скруббер CO2), humidity_control (контроль влажности)"
                    },
                    "action": {
                        "type": "string",
                        "enum": ["start", "stop"],
                        "description": "Действие: start (запустить) или stop (остановить)"
                    }
                },
                "required": ["system", "action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_node_status",
            "description": "Получить детальную информацию о состоянии конкретного узла системы купола.",
            "parameters": {
                "type": "object",
                "properties": {
                    "node_id": {
                        "type": "string",
                        "description": "Идентификатор узла (например: solar_1, battery_1, line_1, diesel_1, co2_sensor_1)"
                    }
                },
                "required": ["node_id"]
            }
        }
    }
]


# Маппинг систем на node_id для control_climate_system
CLIMATE_SYSTEM_MAPPING = {
    "ventilation": "ventilation_1",
    "co2_scrubber": "co2_scrubber_1",
    "humidity_control": "humidity_1"
}
