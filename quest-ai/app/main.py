from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Header, HTTPException, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.config import settings
from app.game_state import game_state, init_db
from app.rag import retriever
from app.prompts import build_system_prompt
from app.llm_adapter import get_llm_adapter

app = FastAPI(title="Quest AI Character")

init_db()
llm = get_llm_adapter()

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    return FileResponse("static/chat.html")


@app.get("/admin")
async def admin_page():
    return FileResponse("static/admin.html")


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

            # 2. Достаём релевантный лор, отфильтрованный по этапу
            lore_chunks = retriever.retrieve(user_message, current_stage)

            # 3. Собираем системный промпт + короткую историю диалога
            system_prompt = build_system_prompt(current_stage, lore_chunks)
            history = game_state.get_recent_history(session_id, settings.HISTORY_WINDOW)

            messages = [{"role": "system", "content": system_prompt}] + history

            # 4. Запрос к LLM
            try:
                answer = await llm.chat(messages)
            except Exception as e:
                answer = "Связь с духами прервалась... попробуйте спросить ещё раз."
                print(f"[LLM ERROR] {e}")

            # 5. Сохраняем и отправляем ответ
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
