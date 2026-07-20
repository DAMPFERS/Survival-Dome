#!/usr/bin/env python3
"""
WebSocket-сервер для телеметрии купола.
Отправляет данные телеметрии клиентам и обрабатывает команды управления каналами.
"""

import asyncio
import json
import random
from websockets import serve

# Состояние сервера
state = {
    "climate": {
        "temperature": 22.4,
        "humidity": 48,
        "co2": 620,
    },
    "power": {
        "solarGeneration": 3.8,
        "battery": 87,
        "channels": [
            {"id": 1, "name": "LIGHTING", "enabled": True, "power": 200},
            {"id": 2, "name": "WATER PUMP", "enabled": False, "power": 0},
            {"id": 3, "name": "GREENHOUSE", "enabled": True, "power": 300},
            {"id": 4, "name": "WORKSHOP", "enabled": True, "power": 400},
            {"id": 5, "name": "VENTILATION", "enabled": False, "power": 0},
            {"id": 6, "name": "COMPUTERS", "enabled": True, "power": 150},
            {"id": 7, "name": "REACTOR", "enabled": True, "power": 450},
            {"id": 8, "name": "RESERVE", "enabled": False, "power": 0},
        ],
    },
}

def generate_random_telemetry():
    """Генерируем случайные данные телеметрии."""
    state["climate"]["temperature"] = round(random.uniform(18, 28), 1)
    state["climate"]["humidity"] = random.randint(30, 70)
    state["climate"]["co2"] = random.randint(450, 1200)

    state["power"]["solarGeneration"] = round(random.uniform(0.2, 5.5), 1)
    state["power"]["battery"] = random.randint(10, 100)

    for channel in state["power"]["channels"]:
        if channel["enabled"]:
            channel["power"] = random.randint(50, 500)
        else:
            channel["power"] = 0

async def handle_client(websocket):
    """Обрабатываем подключение клиента."""
    try:
        print(f"Client connected: {websocket.remote_address}")

        # Отправляем текущее состояние клиенту
        telemetry_msg = {
            "type": "telemetry",
            "data": state
        }
        await websocket.send(json.dumps(telemetry_msg))

        while True:
            generate_random_telemetry()
            telemetry_msg = {
                "type": "telemetry",
                "data": state
            }
            await websocket.send(json.dumps(telemetry_msg))

            try:
                message = await asyncio.wait_for(websocket.recv(), timeout=1.9)
                data = json.loads(message)

                if data["type"] == "control" and data["action"] == "toggle":
                    channel_id = data["channel_id"]
                    channel = next(
                        (ch for ch in state["power"]["channels"] if ch["id"] == channel_id),
                        None
                    )
                    if channel:
                        channel["enabled"] = not channel["enabled"]
                        channel["power"] = random.randint(50, 500) if channel["enabled"] else 0

                        response = {
                            "type": "control_response",
                            "success": True,
                            "channel_id": channel_id,
                            "enabled": channel["enabled"],
                            "power": channel["power"]
                        }
                        await websocket.send(json.dumps(response))

            except asyncio.TimeoutError:
                continue

    except Exception as e:
        print(f"Client disconnected: {e}")

async def main():
    """Запускаем сервер."""
    async with serve(handle_client, "0.0.0.0", 8765):
        print("WebSocket server started on ws://0.0.0.0:8765")
        print("Connect to ws://localhost:8765 or ws://<your-ip>:8765")
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())