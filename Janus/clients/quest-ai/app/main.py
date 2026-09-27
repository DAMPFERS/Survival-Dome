from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Header, HTTPException, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
import logging
import json

from app.config import settings
from app.game_state import game_state, init_db
from app.rag import retriever
from app.prompts import build_system_prompt
from app.llm_adapter import get_llm_adapter
from app.dome_client import dome_client
from app.tools import DOME_CONTROL_TOOLS, STATUS_NODES, resolve_tool_call
from app.panels import energy_telemetry, participant_telemetry

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
    logger.info("Dome client started")


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


# ---------- Инструменты Хранителя ----------

def _node_title(node_id: str) -> str:
    return dome_client.get_catalog().get(node_id, {}).get("title", node_id)


async def execute_tool_call(tool_name: str, arguments: dict) -> dict:
    """Выполнение вызова инструмента и возврат результата."""
    try:
        if tool_name == "get_node_status":
            node_id = arguments.get("node_id")
            if node_id not in STATUS_NODES:
                return {"success": False, "message": f"Узел {node_id} недоступен для запроса"}
            status = dome_client.get_node_status(node_id)
            if status is None:
                return {"success": False, "message": f"Нет данных по узлу {node_id}"}
            return {"success": True, "message": f"Состояние: {_node_title(node_id)}", "data": status}

        try:
            node_id, action, value = resolve_tool_call(tool_name, arguments)
        except (ValueError, TypeError) as e:
            return {"success": False, "message": str(e)}

        result = await dome_client.send_control_command(node_id, action, value)
        target = _node_title(node_id) + (f", линия {value}" if node_id == "smart_panel_01" else "")
        if result.get("success"):
            return {"success": True, "message": f"Выполнено: {target} — {action}"}
        return {"success": False,
                "message": f"Не удалось ({target} — {action}): {result.get('error', 'неизвестная ошибка')}"}

    except RuntimeError as e:
        return {"success": False, "message": f"Ошибка подключения к куполу: {e}"}
    except Exception as e:
        logger.error(f"Error executing tool {tool_name}: {e}")
        return {"success": False, "message": f"Ошибка выполнения команды: {e}"}


async def answer_player(user_message: str) -> str:
    """Полный цикл ответа Хранителя на реплику игрока (лор + телеметрия + инструменты)."""
    session_id = game_state.get_session_id()
    current_stage = game_state.get_stage()

    # 1. Сохраняем вопрос игрока
    game_state.log_message(session_id, "user", user_message)

    # 2. Телеметрия купола и релевантный лор, отфильтрованный по этапу
    telemetry = dome_client.get_key_nodes()
    lore_chunks = retriever.retrieve(user_message, current_stage)

    # 3. Системный промпт + короткая история диалога
    system_prompt = build_system_prompt(current_stage, lore_chunks, telemetry)
    history = game_state.get_recent_history(session_id, settings.HISTORY_WINDOW)
    messages = [{"role": "system", "content": system_prompt}] + history

    # 4. Запрос к LLM с поддержкой function calling
    try:
        response_text, tool_calls = await llm.chat_with_tools(messages, DOME_CONTROL_TOOLS)

        if tool_calls:
            logger.info(f"Executing {len(tool_calls)} tool call(s)")
            tool_results = []
            for call in tool_calls:
                tool_name = call.get("name")
                arguments = call.get("arguments")
                if isinstance(arguments, str):
                    arguments = json.loads(arguments or "{}")

                logger.info(f"Calling tool: {tool_name} with args: {arguments}")
                result = await execute_tool_call(tool_name, arguments or {})
                tool_results.append(result)
                game_state.log_message(
                    session_id,
                    "system",
                    f"[COMMAND] {tool_name}: {json.dumps(arguments, ensure_ascii=False)} -> {result.get('message')}",
                )

            # 5. Результаты — в контекст, финальный ответ в характере персонажа
            messages.append({"role": "assistant", "content": response_text or "Выполняю команду..."})
            results_lines = []
            for r in tool_results:
                line = f"- {r.get('message')}"
                if r.get("data"):
                    line += f" | данные: {json.dumps(r['data'], ensure_ascii=False)}"
                results_lines.append(line)
            messages.append({
                "role": "user",
                "content": "Результаты выполнения команд:\n" + "\n".join(results_lines)
                           + "\n\nСообщи результат игроку в характере персонажа.",
            })
            answer = await llm.chat(messages)
        else:
            answer = response_text or "Связь нарушена..."

    except Exception as e:
        answer = "Связь с духами прервалась... попробуйте спросить ещё раз."
        logger.error(f"[LLM ERROR] {e}", exc_info=True)

    # 6. Сохраняем ответ
    game_state.log_message(session_id, "assistant", answer)
    return answer


@app.websocket("/ws/chat")
async def chat_ws(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            user_message = await websocket.receive_text()
            await websocket.send_text(await answer_player(user_message))
    except WebSocketDisconnect:
        pass


# ---------- API веб-панелей (static/files, static/files-energy) ----------

class ChatPayload(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class LinePayload(BaseModel):
    line: int = Field(ge=1, le=8)
    on: bool


def _require_telemetry() -> dict:
    telemetry = dome_client.get_latest_telemetry()
    if not telemetry:
        raise HTTPException(status_code=503, detail="Нет связи с куполом")
    return telemetry


@app.post("/api/chat")
async def api_chat(payload: ChatPayload):
    return {"reply": await answer_player(payload.message)}


@app.get("/api/telemetry")
async def api_telemetry():
    return participant_telemetry(_require_telemetry())


@app.get("/api/energy")
async def api_energy():
    return energy_telemetry(_require_telemetry())


@app.post("/api/energy/line")
async def api_energy_line(payload: LinePayload):
    """Кнопки линий панели энергетика. Единственное действие, доступное панели: вкл/выкл линии щитка."""
    try:
        result = await dome_client.send_control_command(
            "smart_panel_01", "line_on" if payload.on else "line_off", payload.line)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    if not result.get("success"):
        raise HTTPException(status_code=409, detail=result.get("error", "Команда не выполнена"))
    return {"ok": True, "line": payload.line, "on": payload.on, "mode": result.get("mode")}


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
        "dome": dome_client.get_connection_info(),
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
