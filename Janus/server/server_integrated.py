#!/usr/bin/env python3
"""
WebSocket-сервер для телеметрии купола (Интегрированная версия с симулятором).
Объединяет реальное оборудование и виртуальные узлы симулятора.
Поддерживает hot-swap между real и virtual данными на лету.
"""
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Literal
from websockets import serve

# Добавляем путь к директории simulation для импорта dome_simulator как пакета
simulation_path = Path(__file__).parent / "simulation"
sys.path.insert(0, str(simulation_path))

# Импорты модулей оборудования
try:
    from solar.solar_inverter import InverterMonitor
    from smart_rele.smart_devices import (
        SmartDeviceManager, 
        L1, L2, L3, L4, L5, L6, L7, L8, 
        CO2_SENSOR
    )
    REAL_HARDWARE_AVAILABLE = True
except ImportError as e:
    logging.warning(f"Real hardware modules not available: {e}")
    REAL_HARDWARE_AVAILABLE = False
    InverterMonitor = None
    SmartDeviceManager = None

# Импорт симулятора
from dome_simulator import create_dome_simulator

# ================= НАСТРОЙКА ЛОГИРОВАНИЯ =================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("IntegratedServer")

# ================= КОНФИГУРАЦИЯ =================

# Маппинг реальных устройств на узлы симулятора
REAL_TO_SIM_MAPPING = {
    # Реальное устройство -> конфигурация маппинга
    "inverter": {
        "node_id": "solar_1",
        "mappings": {
            "generated_power_kw": "output_kw",  # real_field -> sim_field
        }
    },
    "battery": {
        "node_id": "battery_1",
        "mappings": {
            "battery_soc_percent": "charge_pct",  # SOC % -> charge_pct
        },
        "conversions": {
            "charge_pct_to_kwh": lambda pct: (pct / 100.0) * 0.96,  # 40Ah * 24V = 960Wh = 0.96kWh
        }
    },
    "L1": {"node_id": "line_1", "mappings": {"state": "status", "power": "current_load_kw"}},
    "L2": {"node_id": "line_2", "mappings": {"state": "status", "power": "current_load_kw"}},
    "L3": {"node_id": "line_3", "mappings": {"state": "status", "power": "current_load_kw"}},
    "L4": {"node_id": "line_4", "mappings": {"state": "status", "power": "current_load_kw"}},
    "L5": {"node_id": "line_5", "mappings": {"state": "status", "power": "current_load_kw"}},
    "L6": {"node_id": "line_6", "mappings": {"state": "status", "power": "current_load_kw"}},
    "L7": {"node_id": "line_7", "mappings": {"state": "status", "power": "current_load_kw"}},
    "L8": {"node_id": "line_8", "mappings": {"state": "status", "power": "current_load_kw"}},
    "co2_sensor": {
        "node_id": "co2_sensor_1",
        "mappings": {"co2": "ppm"}
    },
    "climate": {
        "node_id": "climate_residential",
        "mappings": {"temperature": "temperature_c", "humidity": "humidity_pct"}
    }
}

# Маппинг ID канала (для обратной совместимости с фронтендом)
CHANNEL_MAP = {
    1: "L1", 2: "L2", 3: "L3", 4: "L4",
    5: "L5", 6: "L6", 7: "L7", 8: "L8"
}

# Список конфигураций для менеджера (если железо доступно)
if REAL_HARDWARE_AVAILABLE:
    SWITCH_CONFIGS = [L1, L2, L3, L4, L5, L6, L7, L8]
else:
    SWITCH_CONFIGS = []

# ================= СОСТОЯНИЕ СЕРВЕРА =================

# Глобальные экземпляры
simulator = None
inverter_monitor = None
smart_manager = None

# Режимы узлов: "real" или "virtual"
node_modes: dict[str, Literal["real", "virtual"]] = {}

# Инициализация режимов по умолчанию (если железо доступно - используем real)
def init_default_modes():
    global node_modes
    if REAL_HARDWARE_AVAILABLE:
        node_modes = {
            "solar_1": "real",
            "battery_1": "real",
            "line_1": "real",
            "line_2": "real",
            "line_3": "real",
            "line_4": "real",
            "line_5": "real",
            "line_6": "real",
            "line_7": "real",
            "line_8": "real",
            "co2_sensor_1": "real",
            "climate_residential": "real",
        }
    else:
        node_modes = {}  # Все узлы в режиме virtual по умолчанию

# ================= ЛОГИКА РАБОТЫ С РЕАЛЬНЫМ ЖЕЛЕЗОМ =================

def get_real_device_data(device_name: str) -> dict[str, Any]:
    """Получает данные с реального устройства."""
    if not REAL_HARDWARE_AVAILABLE:
        return {}
    
    if device_name == "inverter" and inverter_monitor:
        snap = inverter_monitor.get_snapshot()
        return {
            "generated_power_kw": snap.generated_power_kw or 0.0,
            "battery_soc_percent": snap.battery_soc_percent or 0.0,
            "is_online": snap.is_online,
        }
    
    elif device_name == "battery" and inverter_monitor:
        snap = inverter_monitor.get_snapshot()
        return {
            "battery_soc_percent": snap.battery_soc_percent or 0.0,
        }
    
    elif device_name.startswith("L") and smart_manager:
        sw_data = smart_manager.get_all_switch_data().get(device_name, {})
        return {
            "state": sw_data.get("state", False),
            "power": sw_data.get("power", 0.0),
        }
    
    elif device_name == "co2_sensor" and smart_manager:
        sensor_data = smart_manager.get_sensor_data()
        return {
            "co2": sensor_data.get("co2", 0),
            "temperature": sensor_data.get("temperature", 0.0),
            "humidity": sensor_data.get("humidity", 0.0),
        }
    
    elif device_name == "climate" and smart_manager:
        sensor_data = smart_manager.get_sensor_data()
        return {
            "temperature": sensor_data.get("temperature", 0.0),
            "humidity": sensor_data.get("humidity", 0.0),
        }
    
    return {}


def convert_real_value(device_name: str, field: str, value: Any) -> Any:
    """Применяет конверсию значения перед записью в симулятор."""
    config = REAL_TO_SIM_MAPPING.get(device_name, {})
    conversions = config.get("conversions", {})
    
    # Специальные конверсии
    if field == "state":  # bool -> "ok" | "disabled"
        return "ok" if value else "disabled"
    
    # Кастомные конверсии из конфига
    converter_key = f"{field}_to_{config['mappings'].get(field, field)}"
    if converter_key in conversions:
        return conversions[converter_key](value)
    
    return value


def sync_real_to_simulator():
    """
    Обновляет симулятор реальными данными перед каждым tick.
    Вызывается из основного цикла WebSocket.
    """
    if not simulator:
        return
    
    for device_name, config in REAL_TO_SIM_MAPPING.items():
        node_id = config["node_id"]
        
        # Проверяем режим узла
        if node_modes.get(node_id, "virtual") != "real":
            continue
        
        # Получаем реальные данные
        real_data = get_real_device_data(device_name)
        if not real_data:
            continue
        
        # Маппим и записываем в симулятор
        updates = {}
        for real_field, sim_field in config["mappings"].items():
            if sim_field and real_field in real_data:
                value = convert_real_value(device_name, real_field, real_data[real_field])
                updates[sim_field] = value
        
        if updates:
            # Добавляем флаг override
            updates["control_override_source"] = "real"
            try:
                simulator.update(node_id, **updates)
            except Exception as e:
                logger.error(f"Failed to sync {device_name} -> {node_id}: {e}")


def get_real_device_for_node(node_id: str) -> str | None:
    """Находит имя реального устройства по node_id."""
    for device_name, config in REAL_TO_SIM_MAPPING.items():
        if config["node_id"] == node_id:
            return device_name
    return None


def is_device_available(device_name: str) -> bool:
    """Проверяет доступность реального устройства."""
    if not REAL_HARDWARE_AVAILABLE:
        return False
    
    if device_name == "inverter":
        return inverter_monitor is not None
    elif device_name == "battery":
        return inverter_monitor is not None
    elif device_name.startswith("L"):
        return smart_manager is not None
    elif device_name in ("co2_sensor", "climate"):
        return smart_manager is not None
    
    return False


# ================= УПРАВЛЕНИЕ РЕАЛЬНЫМ ЖЕЛЕЗОМ =================

async def send_command_to_real_device(node_id: str, action: str, value: Any = None) -> bool:
    """Отправляет команду на реальное устройство."""
    if not REAL_HARDWARE_AVAILABLE or not smart_manager:
        return False
    
    device_name = get_real_device_for_node(node_id)
    if not device_name:
        return False
    
    # Поддерживаем только управление реле (линиями)
    if device_name.startswith("L"):
        if action in ("enable", "start", "reconnect"):
            new_state = True
        elif action in ("disable", "stop", "disconnect"):
            new_state = False
        elif action == "toggle":
            current_state = smart_manager.get_switch_state(device_name)
            new_state = not current_state
        else:
            logger.warning(f"Unknown action {action} for device {device_name}")
            return False
        
        # Отправляем команду на реле (блокирующий вызов в отдельном потоке)
        success = await asyncio.to_thread(smart_manager.set_switch, device_name, new_state)
        return success
    
    return False


# ================= ОБРАБОТКА WEBSOCKET КОМАНД =================

async def handle_control_command(data: dict) -> dict:
    """Обрабатывает команду управления узлом."""
    node_id = data.get("node_id")
    action = data.get("action")
    value = data.get("value")
    job_name = data.get("job_name")
    duration_s = data.get("duration_s")
    
    if not node_id or not action:
        return {"success": False, "error": "Missing node_id or action"}
    
    mode = node_modes.get(node_id, "virtual")
    
    try:
        if mode == "real":
            # ВАРИАНТ A + синхронизация: команда на реальное железо + оптимистично в симулятор
            success = await send_command_to_real_device(node_id, action, value)
            
            if success:
                # Оптимистично обновляем симулятор
                simulator.control(node_id, action, value, job_name=job_name, duration_s=duration_s)
                return {"success": True, "node_id": node_id, "action": action, "mode": "real"}
            else:
                return {"success": False, "error": "Real device command failed"}
        
        elif mode == "virtual":
            # Только в симулятор
            simulator.control(node_id, action, value, job_name=job_name, duration_s=duration_s)
            return {"success": True, "node_id": node_id, "action": action, "mode": "virtual"}
        
    except Exception as e:
        logger.exception(f"Control command failed for {node_id}")
        return {"success": False, "error": str(e)}
    
    return {"success": False, "error": "Unknown mode"}


async def handle_switch_mode(data: dict) -> dict:
    """Переключает режим узла между real и virtual."""
    node_id = data.get("node_id")
    new_mode = data.get("mode")  # "real" | "virtual"
    
    if not node_id or new_mode not in ("real", "virtual"):
        return {"success": False, "error": "Invalid parameters"}
    
    try:
        if new_mode == "virtual":
            # Real → Virtual: убираем override флаг
            simulator.set(node_id, "control_override_source", None)
            node_modes[node_id] = "virtual"
            logger.info(f"Switched {node_id} to VIRTUAL mode")
            return {"success": True, "node_id": node_id, "mode": "virtual"}
        
        elif new_mode == "real":
            # Virtual → Real: проверяем доступность и синхронизируем
            device_name = get_real_device_for_node(node_id)
            if not device_name:
                return {"success": False, "error": f"No real device mapped to {node_id}"}
            
            if not is_device_available(device_name):
                return {"success": False, "error": f"Real device {device_name} not available"}
            
            # Синхронизируем симулятор с реальностью
            real_data = get_real_device_data(device_name)
            config = REAL_TO_SIM_MAPPING[device_name]
            updates = {}
            for real_field, sim_field in config["mappings"].items():
                if sim_field and real_field in real_data:
                    value = convert_real_value(device_name, real_field, real_data[real_field])
                    updates[sim_field] = value
            
            if updates:
                updates["control_override_source"] = "real"
                simulator.update(node_id, **updates)
            
            node_modes[node_id] = "real"
            logger.info(f"Switched {node_id} to REAL mode")
            return {"success": True, "node_id": node_id, "mode": "real"}
    
    except Exception as e:
        logger.exception(f"Mode switch failed for {node_id}")
        return {"success": False, "error": str(e)}
    
    return {"success": False, "error": "Unknown error"}


async def handle_trigger_crisis(data: dict) -> dict:
    """Запускает кризисный сценарий."""
    crisis_name = data.get("crisis_name")
    params = data.get("params")
    
    if not crisis_name:
        return {"success": False, "error": "Missing crisis_name"}
    
    try:
        simulator.trigger_crisis(crisis_name, params)
        logger.info(f"Triggered crisis: {crisis_name}")
        return {"success": True, "crisis_name": crisis_name}
    except Exception as e:
        logger.exception(f"Failed to trigger crisis {crisis_name}")
        return {"success": False, "error": str(e)}


async def handle_stop_crisis(data: dict) -> dict:
    """Останавливает кризисный сценарий."""
    crisis_name = data.get("crisis_name")
    
    if not crisis_name:
        return {"success": False, "error": "Missing crisis_name"}
    
    try:
        simulator.stop_crisis(crisis_name)
        logger.info(f"Stopped crisis: {crisis_name}")
        return {"success": True, "crisis_name": crisis_name}
    except Exception as e:
        logger.exception(f"Failed to stop crisis {crisis_name}")
        return {"success": False, "error": str(e)}


async def handle_time_control(data: dict) -> dict:
    """Управление временем симуляции."""
    action = data.get("action")
    value = data.get("value")
    
    try:
        if action == "set_scale":
            simulator.set_time_scale(float(value))
        elif action == "pause":
            simulator.pause()
        elif action == "resume":
            simulator.resume()
        else:
            return {"success": False, "error": f"Unknown action: {action}"}
        
        return {"success": True, "action": action}
    except Exception as e:
        logger.exception(f"Time control failed: {action}")
        return {"success": False, "error": str(e)}


# ================= ОБРАБОТКА КЛИЕНТОВ =================

async def handle_client(websocket):
    """Обрабатываем подключение клиента."""
    try:
        logger.info(f"Client connected: {websocket.remote_address}")
        
        # Отправляем текущее состояние сразу при подключении
        snapshot = simulator.snapshot()
        telemetry_msg = {
            "type": "telemetry",
            "timestamp": snapshot["timestamp"],
            "data": {
                "nodes": snapshot["state"],
                "active_crises": simulator.active_crises(),
                "sim_time": simulator.time_controller.sim_time,
                "time_scale": simulator.time_controller.time_scale,
                "node_modes": node_modes,  # Добавляем информацию о режимах
            }
        }
        await websocket.send(json.dumps(telemetry_msg))

        while True:
            # 1. Синхронизируем real данные с симулятором
            sync_real_to_simulator()
            
            # 2. Получаем snapshot симулятора
            snapshot = simulator.snapshot()
            
            # 3. Отправляем телеметрию
            telemetry_msg = {
                "type": "telemetry",
                "timestamp": snapshot["timestamp"],
                "data": {
                    "nodes": snapshot["state"],
                    "active_crises": simulator.active_crises(),
                    "sim_time": simulator.time_controller.sim_time,
                    "time_scale": simulator.time_controller.time_scale,
                    "node_modes": node_modes,
                }
            }
            await websocket.send(json.dumps(telemetry_msg))

            # 4. Ждем команду от клиента (с таймаутом)
            try:
                message = await asyncio.wait_for(websocket.recv(), timeout=3.9)
                data = json.loads(message)
                msg_type = data.get("type")

                response = None
                if msg_type == "control":
                    response = await handle_control_command(data)
                elif msg_type == "switch_mode":
                    response = await handle_switch_mode(data)
                elif msg_type == "trigger_crisis":
                    response = await handle_trigger_crisis(data)
                elif msg_type == "stop_crisis":
                    response = await handle_stop_crisis(data)
                elif msg_type == "time_control":
                    response = await handle_time_control(data)
                else:
                    response = {"success": False, "error": f"Unknown message type: {msg_type}"}
                
                if response:
                    response["type"] = f"{msg_type}_response"
                    await websocket.send(json.dumps(response))

            except asyncio.TimeoutError:
                continue  # Просто идем на новый круг отправки телеметрии
            except json.JSONDecodeError:
                logger.warning("Invalid JSON received")
            except Exception as e:
                logger.error(f"Error processing message: {e}")

    except Exception as e:
        logger.info(f"Client disconnected: {e}")


# ================= ЗАПУСК И ЖИЗНЕННЫЙ ЦИКЛ =================

async def main():
    """Запускаем сервер, симулятор и фоновые потоки опроса."""
    global simulator, inverter_monitor, smart_manager
    
    # 1. Инициализация и запуск симулятора
    logger.info("Initializing dome simulator...")
    simulator = create_dome_simulator(tick_interval=4.0, time_scale=1.0)
    simulator.start()
    logger.info("Dome simulator started")
    
    # 2. Инициализация режимов узлов
    init_default_modes()
    logger.info(f"Node modes initialized: {len(node_modes)} real nodes")
    
    # 3. Инициализация и запуск монитора инвертора (если доступен)
    if REAL_HARDWARE_AVAILABLE:
        try:
            inverter_monitor = InverterMonitor(poll_interval=2.0)
            inverter_monitor.start()
            logger.info("InverterMonitor started")
        except Exception as e:
            logger.error(f"Failed to start InverterMonitor: {e}")
        
        # 4. Инициализация и запуск менеджера умных устройств (если доступен)
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
    else:
        logger.warning("Real hardware not available - running in simulation-only mode")
    
    # 5. Запуск WebSocket сервера
    try:
        async with serve(handle_client, "0.0.0.0", 8765):
            logger.info("WebSocket server started on ws://0.0.0.0:8765")
            logger.info("Press Ctrl+C to stop the server gracefully.")
            
            await asyncio.Future()  # Бесконечное ожидание
            
    except KeyboardInterrupt:
        logger.info("Shutdown signal received (Ctrl+C)...")
    except asyncio.CancelledError:
        logger.info("Server task cancelled.")
    finally:
        # 6. Корректная остановка всех компонентов
        logger.info("Stopping all components...")
        
        if simulator:
            simulator.stop()
            logger.info("Simulator stopped")
        
        if inverter_monitor:
            inverter_monitor.stop()
            logger.info("InverterMonitor stopped")
        
        if smart_manager:
            smart_manager.stop()
            logger.info("SmartDeviceManager stopped")
        
        logger.info("Server stopped gracefully")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
