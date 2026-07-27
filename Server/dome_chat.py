"""
Dome Chat Bot
Чат-бот на базе локальной модели Ollama для управления телеметрией купола.
Использует Function Calling (tools API) для вызова методов клиента.
"""

import json
import logging
import time
from typing import Optional, List, Dict, Any

from ollama import chat, ChatResponse

# Импортируем клиент из предыдущей части
# (предполагается, что файл dome_client.py содержит DomeTelemetryClient)
from client import DomeTelemetryClient, CHANNEL_NAMES


logger = logging.getLogger(__name__)


class DomeChatBot:
    """
    Чат-бот для взаимодействия с телеметрией купола через естественный язык.
    
    Использует локальную модель Ollama с поддержкой Function Calling.
    """

    def __init__(
        self,
        model: str = "gemma4:latest",
        host: str = "192.168.8.36",
        port: int = 8765,
    ):
        self._model = model
        self._client = DomeTelemetryClient(host=host, port=port)
        self._client.start()
        
        # Ждём подключения
        logger.info("Ожидание подключения к серверу телеметрии...")
        for _ in range(50):  # до 5 секунд
            if self._client.is_connected():
                logger.info("Подключение установлено")
                break
            if self._client.is_failed():
                raise self._client.get_last_error() or ConnectionError("Failed")
            time.sleep(0.1)
        else:
            raise TimeoutError("Не удалось подключиться к серверу телеметрии")
        
        # Определяем инструменты для модели
        self._tools = self._define_tools()
        
        # Системный промпт
        self._system_prompt = """Ты — ассистент системы телеметрии купола. 
Отвечай кратко и по делу. Используй доступные инструменты для получения данных 
о климате, питании и управления каналами нагрузки. 
Если пользователь спрашивает о параметрах — вызывай соответствующий инструмент. 
Если просят включить/выключить линию — вызывай toggle_channel.
Отвечай на русском языке."""

    def _define_tools(self) -> List[Dict[str, Any]]:
        """Определяет список инструментов (функций) для модели."""
        return [
            {
                "type": "function",
                "function": {
                    "name": "get_climate",
                    "description": "Получить текущие показатели климата (температура, влажность, CO2)",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_temperature",
                    "description": "Получить текущую температуру в градусах Цельсия",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_humidity",
                    "description": "Получить текущую влажность в процентах",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_co2",
                    "description": "Получить текущий уровень CO2 в ppm",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_power_summary",
                    "description": "Получить сводку по питанию: солнечная генерация, батарея, общая нагрузка",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_solar_generation",
                    "description": "Получить мощность солнечных панелей в kW",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_battery",
                    "description": "Получить уровень заряда батареи в процентах",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_channel_power",
                    "description": "Получить мощность конкретного канала нагрузки в ваттах",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "channel_id": {
                                "type": "integer",
                                "description": "ID канала (1-8)",
                            }
                        },
                        "required": ["channel_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "is_channel_enabled",
                    "description": "Проверить, включен ли конкретный канал нагрузки",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "channel_id": {
                                "type": "integer",
                                "description": "ID канала (1-8)",
                            }
                        },
                        "required": ["channel_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_channels",
                    "description": "Получить состояние всех каналов нагрузки (ID, имя, включен/выключен, мощность)",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "toggle_channel",
                    "description": "Переключить канал нагрузки (включить/выключить). Возвращает новое состояние.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "channel_id": {
                                "type": "integer",
                                "description": "ID канала (1-8)",
                            }
                        },
                        "required": ["channel_id"],
                    },
                },
            },
        ]

    def _execute_tool(self, name: str, args: Dict[str, Any]) -> Any:
        """Выполняет вызов инструмента и возвращает результат."""
        try:
            if name == "get_climate":
                return self._client.get_climate()
            elif name == "get_temperature":
                return {"temperature": self._client.get_temperature()}
            elif name == "get_humidity":
                return {"humidity": self._client.get_humidity()}
            elif name == "get_co2":
                return {"co2": self._client.get_co2()}
            elif name == "get_power_summary":
                return self._client.get_power()
            elif name == "get_solar_generation":
                return {"solar_generation": self._client.get_solar_generation()}
            elif name == "get_battery":
                return {"battery": self._client.get_battery()}
            elif name == "get_channel_power":
                ch_id = args["channel_id"]
                power = self._client.get_channel_power(ch_id)
                return {"channel_id": ch_id, "power": power}
            elif name == "is_channel_enabled":
                ch_id = args["channel_id"]
                enabled = self._client.is_channel_enabled(ch_id)
                return {"channel_id": ch_id, "enabled": enabled}
            elif name == "get_channels":
                return self._client.get_channels()
            elif name == "toggle_channel":
                ch_id = args["channel_id"]
                result = self._client.toggle_channel(ch_id)
                return result
            else:
                return {"error": f"Неизвестный инструмент: {name}"}
        except Exception as e:
            logger.error("Ошибка выполнения инструмента %s: %s", name, e)
            return {"error": str(e)}

    def ask(self, prompt: str) -> str:
        """
        Основной метод: принимает текстовый запрос, возвращает ответ.
        
        :param prompt: Текст запроса от пользователя
        :return: Текстовый ответ модели
        """
        messages = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": prompt},
        ]

        # Цикл agent loop: пока модель вызывает инструменты
        max_iterations = 10  # Защита от бесконечного цикла
        iteration = 0

        while iteration < max_iterations:
            iteration += 1
            
            # Вызов модели с tools
            response: ChatResponse = chat(
                model=self._model,
                messages=messages,
                tools=self._tools,
            )

            # Если модель не вызвала инструменты — возвращаем финальный ответ
            if not response.message.tool_calls:
                return response.message.content or "Нет ответа"

            # Добавляем ответ модели с вызовами инструментов в историю
            messages.append(response.message)

            # Выполняем каждый вызов инструмента
            for tool_call in response.message.tool_calls:
                tool_name = tool_call.function.name
                tool_args = tool_call.function.arguments

                logger.info("Вызов инструмента: %s(%s)", tool_name, tool_args)
                
                # Выполняем инструмент
                result = self._execute_tool(tool_name, tool_args)
                
                logger.info("Результат: %s", result)

                # Добавляем результат в историю сообщений
                messages.append({
                    "role": "tool",
                    "name": tool_name,
                    "content": json.dumps(result, ensure_ascii=False),
                })

        return "Превышен лимит итераций (model loop)"

    def stop(self) -> None:
        """Останавливает клиента."""
        self._client.stop()

    def run_repl(self) -> None:
        """Интерактивный цикл в терминале."""
        print("=" * 60)
        print("Dome Telemetry Chat Bot")
        print("Введите запрос (или 'exit' для выхода)")
        print("=" * 60)
        
        while True:
            try:
                user_input = input("\nВы: ").strip()
                
                if not user_input:
                    continue
                
                if user_input.lower() in ("exit", "quit", "выход"):
                    print("До свидания!")
                    break
                
                # Получаем ответ
                response = self.ask(user_input)
                print(f"\nБот: {response}")
                
            except KeyboardInterrupt:
                print("\n\nПолучен сигнал остановки")
                break
            except Exception as e:
                print(f"\nОшибка: {e}")
                logger.exception("Ошибка в REPL")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Создаём бота
    bot = DomeChatBot(
        # model="gemma3:4b",  # Или другая модель: "llama3.1", "mistral", etc.
        # host="localhost",
        # port=8765,
    )

    try:
        # Запускаем интерактивный режим
        bot.run_repl()
    finally:
        bot.stop()