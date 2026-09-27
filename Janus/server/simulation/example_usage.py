# example_usage.py
import logging
import time

from dome_simulator import ControlError, create_dome_simulator

logging.basicConfig(level=logging.INFO)

# 2 с реального времени = 8 с симуляции = 8 игровых минут (сутки купола — 1440 с симуляции)
sim = create_dome_simulator(tick_interval=2.0, time_scale=4.0)

# Подписка на критические события — например, для алертов хакатон-дашборда
sim.subscribe_all(lambda e: print(f"[EVENT] {e}") if e.severity.value != "info" else None)

# Каталог узлов и их управляющих воздействий
for node in sim.describe_nodes():
    print(f"{node['node_id']:24} {node['title']}: {', '.join(node['controls']) or '—'}")

sim.start()

time.sleep(5)
print("SOC АКБ, %:", sim.get("battery_01", "soc_pct"))
print("Погода:", sim.get_node("weather_station_01"))

# Запуск задания на 3D-принтере (duration_s — игровые секунды).
# В рабочее время принтер сам берёт задания — если занят, прерываем текущее.
if sim.get("printer_3d_01", "state") in ("PRINTING", "PAUSED", "ERROR"):
    sim.control("printer_3d_01", "abort")
sim.control("printer_3d_01", "start_job", {"file_name": "bracket_v2.gcode", "duration_s": 3600})

# Управление линией умного щитка
sim.control("smart_panel_01", "line_off", 7)

# Недопустимая команда — понятная ошибка с перечнем вариантов
try:
    sim.control("solar_inverter_01", "set_mode", "TURBO")
except ControlError as e:
    print("Отклонено:", e)

time.sleep(10)
print("Принтер:", sim.get_node("printer_3d_01"))
print("Окружение (истина, только для отладки):", sim.environment_snapshot()["outdoor"])

sim.set_time_scale(0.0)  # пауза
time.sleep(2)
sim.set_time_scale(1.0)  # резюме в реальном времени

sim.save_snapshot("dome_state.json")

sim.stop()
