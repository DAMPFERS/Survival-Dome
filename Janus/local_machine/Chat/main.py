from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Optional, Union
import time
import json
import asyncio
import uuid

app = FastAPI(title="Echo OpenAI-compatible API")

class Message(BaseModel):
    role: str
    content: Union[str, List]  # может быть строка или multimodal

class ChatCompletionRequest(BaseModel):
    model: str = "echo-model"
    messages: List[Message]
    stream: Optional[bool] = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    # остальные поля OpenAI можно игнорировать

@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {
                "id": "echo-model",
                "object": "model",
                "created": int(time.time()),
                "owned_by": "local"
            }
        ]
    }

@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    # Берём последнее сообщение пользователя
    last_user_msg = ""
    for msg in reversed(request.messages):
        if msg.role == "user":
            if isinstance(msg.content, str):
                last_user_msg = msg.content
            else:
                # multimodal — берём только текст
                for part in msg.content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        last_user_msg = part.get("text", "")
                        break
            break

    reply = f"Echo: {last_user_msg}"

    if request.stream:
        async def event_generator():
            # Отправляем по словам (имитация стриминга)
            words = reply.split()
            for i, word in enumerate(words):
                chunk = {
                    "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": request.model,
                    "choices": [{
                        "index": 0,
                        "delta": {"content": word + (" " if i < len(words)-1 else "")},
                        "finish_reason": None
                    }]
                }
                yield f"data: {json.dumps(chunk)}\n\n"
                await asyncio.sleep(0.05)  # небольшая задержка

            # Финальный чанк
            final = {
                "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": request.model,
                "choices": [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop"
                }]
            }
            yield f"data: {json.dumps(final)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    # Обычный (не-streaming) ответ
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": request.model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": reply
            },
            "finish_reason": "stop"
        }],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 10,
            "total_tokens": 20
        }
    }

@app.get("/")
async def root():
    return {"status": "ok", "message": "Echo API is running. Use /v1/chat/completions"}