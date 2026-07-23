#!/usr/bin/env python3
"""
WebSocket-сервер для телеметрии купола (Интегрированная версия).
Работает с реальным оборудованием: солнечным инвертором и умными реле/датчиками.
"""
import asyncio
import json
import logging
import signal
from websockets import serve

# Импорты модулей оборудования
from solar.solar_inverter import InverterMonitor
from smart_rele.smart_devices import (
    SmartDeviceManager, 
    L1, L2, L3, L4, L5, L6, L7, L8, 
    CO2_SENSOR
)

# ================= НАСТРОЙКА ЛОГИРОВАНИЯ =================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("Server")

# ================= КОНФИГУРАЦИЯ =================
# Маппинг ID канала (для фронтенда) -> Метке устройства (L1-L8)
CHANNEL_MAP = {
    1: "L1", 2: "L2", 3: "L3", 4: "L4",
    5: "L5", 6: "L6", 7: "L7", 8: "L8"
}

# Жестко заданные имена для фронтенда (сохраняем оригинальные)
CHANNEL_NAMES = {
    1: "LIGHTING", 2: "WATER PUMP", 3: "GREENHOUSE", 4: "WORKSHOP",
    5: "VENTILATION", 6: "COMPUTERS", 7: "REACTOR", 8: "RESERVE"
}

# Список конфигураций для менеджера
SWITCH_CONFIGS = [L1, L2, L3, L4, L5, L6, L7, L8]

# ================= СОСТОЯНИЕ СЕРВЕРА =================
# Инициализируем структуру один раз, чтобы сохранить порядок и имена каналов
state = {
    "climate": {
        "temperature": 0.0,
        "humidity": 0.0,
        "co2": 0,
    },
    "power": {
        "solarGeneration": 0.0,  # Вт (от инвертора)
        "battery": 0.0,          # %
        "inverter_online": False, # Флаг связи с инвертором
        "channels": [
            {"id": i, "name": CHANNEL_NAMES[i], "enabled": False, "power": 0.0}
            for i in range(1, 9)
        ],
    },
}

# Глобальные экземпляры менеджеров (инициализируются в main)
inverter_monitor = None
smart_manager = None

# ================= ЛОГИКА ТЕЛЕМЕТРИИ =================
def fetch_real_telemetry():
    """
    Считывает актуальные данные с железа и обновляет глобальный state.
    Выполняется синхронно, но так как геттеры используют Lock и работают быстро,
    это не блокирует event-loop.
    """
    global state
    
    # 1. Инвертор (Атомарный снимок под Lock)
    if inverter_monitor:
        inv_snap = inverter_monitor.get_snapshot()
        # generated_power_kw содержит Ватты (согласно ТЗ)
        state["power"]["solarGeneration"] = inv_snap.generated_power_kw or 0.0
        state["power"]["battery"] = inv_snap.battery_soc_percent or 0.0
        state["power"]["inverter_online"] = inv_snap.is_online
    else:
        state["power"]["inverter_online"] = False

    # 2. Датчик климата
    if smart_manager:
        sensor_data = smart_manager.get_sensor_data()
        state["climate"]["temperature"] = sensor_data["temperature"]
        state["climate"]["humidity"] = sensor_data["humidity"]
        state["climate"]["co2"] = sensor_data["co2"]

    # 3. Реле (Каналы)
    if smart_manager:
        # Получаем все данные сразу под одним локом (быстро)
        switches_data = smart_manager.get_all_switch_data()
        
        for channel in state["power"]["channels"]:
            ch_id = channel["id"]
            label = CHANNEL_MAP[ch_id]
            
            # Берем данные реле
            sw_data = switches_data.get(label, {})
            channel["enabled"] = sw_data.get("state", False)
            # Мощность в кВт (согласно ТЗ)
            channel["power"] = sw_data.get("power", 0.0) 

# ================= ОБРАБОТКА КЛИЕНТОВ =================
async def handle_client(websocket):
    """Обрабатываем подключение клиента."""
    try:
        logger.info(f"Client connected: {websocket.remote_address}")
        
        # Отправляем текущее состояние сразу при подключении
        telemetry_msg = {"type": "telemetry", "data": state}
        await websocket.send(json.dumps(telemetry_msg))

        while True:
            # 1. Обновляем данные с железа
            fetch_real_telemetry()
            
            # 2. Отправляем телеметрию
            telemetry_msg = {"type": "telemetry", "data": state}
            await websocket.send(json.dumps(telemetry_msg))

            # 3. Ждем команду от клиента (с таймаутом, чтобы не блокировать отправку телеметрии)
            try:
                message = await asyncio.wait_for(websocket.recv(), timeout=3.9)
                data = json.loads(message)

                if data.get("type") == "control" and data.get("action") == "toggle":
                    channel_id = data.get("channel_id")
                    label = CHANNEL_MAP.get(channel_id)

                    if not label:
                        logger.warning(f"Unknown channel_id: {channel_id}")
                        continue

                    # Получаем текущее состояние, чтобы инвертировать его
                    current_state = smart_manager.get_switch_state(label)
                    new_state = not current_state

                    # ВАЖНО: Вызов set_switch блокирующий (сеть).
                    # Используем to_thread, чтобы не вешать asyncio event loop.
                    success = await asyncio.to_thread(
                        smart_manager.set_switch, label, new_state
                    )

                    if success:
                        response = {
                            "type": "control_response",
                            "success": True,
                            "channel_id": channel_id,
                            "enabled": new_state,
                            "power": smart_manager.get_power(label)
                        }
                        logger.info(f"Toggle {label} -> {new_state} OK")
                    else:
                        response = {
                            "type": "control_response",
                            "success": False,
                            "channel_id": channel_id,
                            "error": "Device offline or command failed"
                        }
                        logger.error(f"Toggle {label} -> {new_state} FAILED")

                    await websocket.send(json.dumps(response))

            except asyncio.TimeoutError:
                continue # Просто идем на новый круг отправки телеметрии
            except json.JSONDecodeError:
                logger.warning("Invalid JSON received")
            except Exception as e:
                logger.error(f"Error processing message: {e}")

    except Exception as e:
        logger.info(f"Client disconnected: {e}")

# ================= ЗАПУСК И ЖИЗНЕННЫЙ ЦИКЛ =================
# ================= ЗАПУСК И ЖИЗНЕННЫЙ ЦИКЛ =================
async def main():
    """Запускаем сервер и фоновые потоки опроса."""
    global inverter_monitor, smart_manager

    # 1. Инициализация и запуск монитора инвертора
    try:
        inverter_monitor = InverterMonitor(poll_interval=2.0)
        inverter_monitor.start()
        logger.info("InverterMonitor started")
    except Exception as e:
        logger.error(f"Failed to start InverterMonitor: {e}")

    # 2. Инициализация и запуск менеджера умных устройств
    try:
        smart_manager = SmartDeviceManager(
            switch_configs=SWITCH_CONFIGS,
            sensor_config=CO2_SENSOR,
            poll_interval=4.0
        )
        smart_manager.start()
        logger.info("SmartDeviceManager started")
    except Exception as e:
        logger.error(f"Failed to start SmartDeviceManager: {e}")

    # 3. Запуск WebSocket сервера с кроссплатформенной обработкой остановки
    try:
        async with serve(handle_client, "0.0.0.0", 8765):
            logger.info("WebSocket server started on ws://0.0.0.0:8765")
            logger.info("Press Ctrl+C to stop the server gracefully.")
            
            # Бесконечное ожидание. При нажатии Ctrl+C возникнет KeyboardInterrupt,
            # который прервет это ожидание и перейдет в блок except/finally.
            await asyncio.Future() 
            
    except KeyboardInterrupt:
        logger.info("Shutdown signal received (Ctrl+C)...")
    except asyncio.CancelledError:
        logger.info("Server task cancelled.")
    finally:
        # 4. Корректная остановка фоновых потоков при выходе (работает на любой ОС)
        logger.info("Stopping hardware monitors...")
        if inverter_monitor:
            inverter_monitor.stop()
        if smart_manager:
            smart_manager.stop()
        logger.info("Server stopped gracefully")



if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass