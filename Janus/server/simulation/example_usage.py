# example_usage.py
import logging
import time

from dome_simulator import create_dome_simulator
from dome_simulator.scenario_engine import RandomTrigger

logging.basicConfig(level=logging.INFO)

sim = create_dome_simulator(tick_interval=2.0, time_scale=4.0)  # 2с реального = 8с симуляции

# Подписка на критические события — например, для алертов хакатон-дашборда
sim.subscribe_all(lambda e: print(f"[EVENT] {e}") if e.severity.value != "info" else None)

# Случайный кризис деградации панелей — 1% шанс за тик, не чаще раза в 5 минут
sim.scenario_engine.add_random_trigger(
    RandomTrigger(scenario_name="solar_degradation", probability_per_tick=0.01,
                  cooldown_s=300.0, params={"severity": 0.001})
)

sim.start()

time.sleep(5)
print("Заряд батареи:", sim.get("battery_1", "charge_pct"))

# Запуск задания на 3D-принтере
sim.control("printer_1", "start_job", job_name="bracket_v2", duration_s=20.0)

# Ручной кризис: потеря питания на 30 секунд
sim.trigger_crisis("power_loss", params={"target_node_id": "solar_1", "duration_s": 30.0})

time.sleep(10)
print("Статус купола:", sim.get("main_controller", "overall_status"))
print("Снапшот:", sim.snapshot()["nodes"]["battery_1"])

sim.set_time_scale(0.0)  # пауза
time.sleep(2)
sim.set_time_scale(1.0)  # резюме в реальном времени

sim.save_snapshot("dome_state.json")

sim.stop()