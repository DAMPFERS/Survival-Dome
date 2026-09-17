from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Header, HTTPException, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import logging
import json

from app.config import settings
from app.game_state import game_state, init_db
from app.rag import retriever
from app.prompts import build_system_prompt
from app.llm_adapter import get_llm_adapter
from app.dome_client import dome_client
from app.tools import DOME_CONTROL_TOOLS, CLIMATE_SYSTEM_MAPPING

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Quest AI Character")

init_db()
llm = get_llm_adapter()

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.on_event("startup")
async def startup():
    """Инициализация при запуске приложения."""
    logger.info("Starting Quest AI application...")
    await dome_client.connect()
    logger.info("Dome client connected")


@app.on_event("shutdown")
async def shutdown():
    """Корректное завершение работы."""
    logger.info("Shutting down Quest AI application...")
    await dome_client.disconnect()
    logger.info("Dome client disconnected")


@app.get("/")
async def index():
    return FileResponse("static/chat.html")


@app.get("/admin")
async def admin_page():
    return FileResponse("static/admin.html")


async def execute_tool_call(tool_name: str, arguments: dict) -> dict:
    """Выполнение вызова инструмента и возврат результата."""
    try:
        if tool_name == "control_power_line":
            line_number = arguments.get("line_number")
            action = arguments.get("action")
            node_id = f"line_{line_number}"
            
            result = await dome_client.send_control_command(node_id, action)
            
            if result.get("success"):
                return {
                    "success": True,
                    "message": f"Линия {line_number} {'включена' if action == 'enable' else 'выключена'}"
                }
            else:
                return {
                    "success": False,
                    "message": f"Не удалось выполнить команду: {result.get('error', 'неизвестная ошибка')}"
                }
        
        elif tool_name == "control_diesel_generator":
            action = arguments.get("action")
            
            if action == "set_power":
                target_kw = arguments.get("target_kw")
                result = await dome_client.send_control_command(
                    "diesel_1", 
                    "set_target_output_kw",
                    value=target_kw
                )
                msg = f"Мощность генератора установлена на {target_kw} кВт"
            else:
                result = await dome_client.send_control_command("diesel_1", action)
                msg = f"Генератор {'запущен' if action == 'start' else 'остановлен'}"
            
            if result.get("success"):
                return {"success": True, "message": msg}
            else:
                return {
                    "success": False,
                    "message": f"Не удалось выполнить команду: {result.get('error', 'неизвестная ошибка')}"
                }
        
        elif tool_name == "control_climate_system":
            system = arguments.get("system")
            action = arguments.get("action")
            node_id = CLIMATE_SYSTEM_MAPPING.get(system)
            
            if not node_id:
                return {"success": False, "message": f"Неизвестная система: {system}"}
            
            result = await dome_client.send_control_command(node_id, action)
            
            system_names = {
                "ventilation": "Вентиляция",
                "co2_scrubber": "Скруббер CO2",
                "humidity_control": "Контроль влажности"
            }
            
            if result.get("success"):
                return {
                    "success": True,
                    "message": f"{system_names[system]} {'запущена' if action == 'start' else 'остановлена'}"
                }
            else:
                return {
                    "success": False,
                    "message": f"Не удалось выполнить команду: {result.get('error', 'неизвестная ошибка')}"
                }
        
        elif tool_name == "get_node_status":
            node_id = arguments.get("node_id")
            status = dome_client.get_node_status(node_id)
            
            if status:
                return {
                    "success": True,
                    "message": f"Статус узла {node_id}",
                    "data": status
                }
            else:
                return {
                    "success": False,
                    "message": f"Узел {node_id} не найден или нет данных"
                }
        
        else:
            return {"success": False, "message": f"Неизвестный инструмент: {tool_name}"}
    
    except RuntimeError as e:
        return {"success": False, "message": f"Ошибка подключения к куполу: {str(e)}"}
    except Exception as e:
        logger.error(f"Error executing tool {tool_name}: {e}")
        return {"success": False, "message": f"Ошибка выполнения команды: {str(e)}"}


@app.websocket("/ws/chat")
async def chat_ws(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            user_message = await websocket.receive_text()

            session_id = game_state.get_session_id()
            current_stage = game_state.get_stage()

            # 1. Сохраняем вопрос игрока
            game_state.log_message(session_id, "user", user_message)

            # 2. Получаем телеметрию купола
            telemetry = dome_client.get_key_nodes()

            # 3. Достаём релевантный лор, отфильтрованный по этапу
            lore_chunks = retriever.retrieve(user_message, current_stage)

            # 4. Собираем системный промпт с телеметрией + короткую историю диалога
            system_prompt = build_system_prompt(current_stage, lore_chunks, telemetry)
            history = game_state.get_recent_history(session_id, settings.HISTORY_WINDOW)

            messages = [{"role": "system", "content": system_prompt}] + history

            # 5. Запрос к LLM с поддержкой function calling
            try:
                response_text, tool_calls = await llm.chat_with_tools(messages, DOME_CONTROL_TOOLS)
                
                # 6. Если LLM вызвал инструменты - выполняем их
                if tool_calls:
                    logger.info(f"Executing {len(tool_calls)} tool call(s)")
                    tool_results = []
                    
                    for call in tool_calls:
                        tool_name = call.get("name")
                        arguments_str = call.get("arguments")
                        
                        # Парсим аргументы (они могут быть строкой или уже словарем)
                        if isinstance(arguments_str, str):
                            arguments = json.loads(arguments_str)
                        else:
                            arguments = arguments_str
                        
                        logger.info(f"Calling tool: {tool_name} with args: {arguments}")
                        
                        # Выполняем инструмент
                        result = await execute_tool_call(tool_name, arguments)
                        tool_results.append(result)
                        
                        # Логируем выполнение команды
                        game_state.log_message(
                            session_id, 
                            "system", 
                            f"[COMMAND] {tool_name}: {json.dumps(arguments, ensure_ascii=False)} -> {result.get('message')}"
                        )
                    
                    # 7. Добавляем результаты в контекст и получаем финальный ответ
                    messages.append({
                        "role": "assistant",
                        "content": response_text or "Выполняю команду..."
                    })
                    
                    # Формируем сообщение с результатами для LLM
                    results_lines = []
                    for r in tool_results:
                        line = f"- {r.get('message')}"
                        if r.get("data"):
                            line += f" | данные: {json.dumps(r['data'], ensure_ascii=False)}"
                        results_lines.append(line)
                    results_text = "\n".join(results_lines)
                    
                    messages.append({
                        "role": "user",
                        "content": f"Результаты выполнения команд:\n{results_text}\n\nСообщи результат игроку в характере персонажа."
                    })
                    
                    # Финальный запрос для получения ответа с результатами
                    answer = await llm.chat(messages)
                else:
                    # Обычный текстовый ответ без вызова инструментов
                    answer = response_text or "Связь нарушена..."
                
            except Exception as e:
                answer = "Связь с духами прервалась... попробуйте спросить ещё раз."
                logger.error(f"[LLM ERROR] {e}", exc_info=True)

            # 8. Сохраняем и отправляем ответ
            game_state.log_message(session_id, "assistant", answer)
            await websocket.send_text(answer)

    except WebSocketDisconnect:
        pass


# ---------- Админ API ----------

class StagePayload(BaseModel):
    stage: int


def check_admin(x_admin_password: str = Header(default="")):
    """Простая проверка пароля из заголовка X-Admin-Password.
    Для мероприятия на закрытой сети этого достаточно; для публичного
    доступа в интернет стоит добавить HTTPS + более серьёзную авторизацию."""
    if x_admin_password != settings.ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Неверный пароль администратора")
    return True


@app.get("/admin/state")
async def admin_state(_: bool = Depends(check_admin)):
    session_id = game_state.get_session_id()
    return {
        "session_id": session_id,
        "stage": game_state.get_stage(),
        "max_stage": settings.MAX_STAGE,
        "log": game_state.get_full_log(session_id),
    }


@app.post("/admin/stage")
async def admin_set_stage(payload: StagePayload, _: bool = Depends(check_admin)):
    game_state.set_stage(payload.stage)
    return {"ok": True, "stage": game_state.get_stage()}


@app.post("/admin/reset")
async def admin_reset(_: bool = Depends(check_admin)):
    new_session = game_state.reset_session()
    return {"ok": True, "session_id": new_session}
