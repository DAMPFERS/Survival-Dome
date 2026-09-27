# tests/test_crises.py
"""
Кризисы dome_crises.md: для каждого — прогон «агент бездействует» и «агент
реагирует так, как требует карточка». Проверяется исход (RESOLVED / FAILED /
EXPIRED / MANUAL) в отчёте кризиса.

Команды агента — origin="agent" (связь, запрет физических действий),
просьбы к Оператору — origin="operator".
"""
from __future__ import annotations

import pytest

from dome_simulator import ControlError, create_dome_simulator

AUTO = "dome_automation_01"
A, OP = "agent", "operator"


def trust(node, param, **extra):
    return (AUTO, "set_data_trust", {"node": node, "param": param, "trust": "UNRELIABLE", **extra}, A)


def make(start_hour=12.0, jobs=True, solder=True, soc=None, grid=True, fuel_water=None, cloud=None, seed=21):
    sim = create_dome_simulator(seed=seed, start_hour=start_hour)
    sim.set_random_faults(False)
    for node_id in ("printer_3d_01", "cnc_01"):
        sim.registry.get(node_id).auto_jobs = False
    if soc is not None:
        sim.registry.get("solar_inverter_01").set_soc(soc)
    if not grid:
        sim.environment.force("external.grid_available", False)
    if cloud is not None:
        sim.environment.force("outdoor.cloud_cover", cloud)
    if fuel_water is not None:
        sim.update("fuel_tank_01", water_content_ppm=fuel_water, volume_l=90.0)
    if jobs:
        sim.control("cnc_01", "start_job", {"file_name": "board.nc", "duration_s": 7200})
        sim.control("printer_3d_01", "start_job", {"file_name": "case.gcode", "duration_s": 7200, "filament_g": 80})
    sim.update("smart_panel_01", control_workstation="SOLDERING" if solder else "IDLE")
    for _ in range(45):  # 3 минуты: нагрев стола и сопла
        sim.step()
    return sim


def play(sim, name, params=None, actions=(), run_s=300.0, stop=True, expect_errors=False):
    """Запускает кризис, выполняет actions [(t_s, node, action, value, origin)], крутит run_s."""
    sim.trigger_crisis(name, params or {})
    crisis = sim.scenario_engine.get(name)
    pending = sorted(actions, key=lambda a: a[0])
    t = 0.0
    while t < run_s:
        while pending and pending[0][0] <= t:
            _, node, action, value, origin = pending.pop(0)
            try:
                sim.control(node, action, value, origin=origin)
            except ControlError:
                if not expect_errors:
                    raise
        sim.step()
        t += 4.0
    if stop and sim.scenario_engine.resolve_name(name) in sim.active_crises():
        sim.stop_crisis(name)
    return crisis.report


def at(t, node, action, value=None, origin=A):
    return (t, node, action, value, origin)


def trust_at(t, node, param, **extra):
    return (t, *trust(node, param, **extra))


# ---------------------------------------------------------------------------
# Табличные случаи: (кризис, make-kwargs, params, действия, run_s, исход)
# ---------------------------------------------------------------------------

CASES = [
    # №1 фантомное потребление
    ("n01_phantom_load", {}, {}, [], 660, "FAILED"),
    ("n01_phantom_load", {}, {}, [at(30, "smart_panel_01", "line_off", 8)], 120, "RESOLVED"),
    # №2 ложная балансировка
    ("n02_false_balancing", {}, {"minutes": 3}, [], 240, "FAILED"),
    ("n02_false_balancing", {}, {}, [at(20, "solar_inverter_01", "cancel_balancing")], 60, "RESOLVED"),
    # №3 призрачное напряжение линии 2
    ("n03_ghost_voltage_line2", {}, {}, [], 200, "FAILED"),
    ("n03_ghost_voltage_line2", {}, {}, [trust_at(20, "smart_panel_01", "voltage_v", line=2)], 200, "RESOLVED"),
    # №4 MPPT-канал 1
    ("n04_mppt1_failure", {}, {}, [], 120, "EXPIRED"),
    ("n04_mppt1_failure", {}, {}, [at(20, "solar_panels_01", "enable_mppt_1")], 60, "RESOLVED"),
    # №5 Feed Hold
    ("n05_cnc_feed_hold", {}, {}, [], 200, "FAILED"),
    ("n05_cnc_feed_hold", {}, {}, [at(20, "cnc_01", "resume")], 60, "RESOLVED"),
    # №6 фантомный CO₂
    ("n06_phantom_co2", {}, {}, [], 260, "FAILED"),
    ("n06_phantom_co2", {}, {}, [trust_at(20, "climate_sensor_01", "co2_ppm")], 260, "RESOLVED"),
    # №7 ложный конденсат
    ("n07_false_condensate", {}, {}, [], 260, "FAILED"),
    ("n07_false_condensate", {}, {}, [trust_at(20, "climate_sensor_01", "humidity_pct")], 260, "RESOLVED"),
    # №8 ложное срабатывание защиты линии 1
    ("n08_false_protection_trip", {}, {}, [], 100, "FAILED"),
    ("n08_false_protection_trip", {}, {}, [at(12, "smart_panel_01", "reset_protection", 1)], 40, "RESOLVED"),
    # №9 ложная деградация ёмкости (нет внешнего резерва: сеть отключена, в топливе вода)
    ("n09_false_capacity_loss", dict(start_hour=21.0, soc=70.0, grid=False, fuel_water=650.0), {}, [], 300, "FAILED"),
    ("n09_false_capacity_loss", dict(start_hour=21.0, soc=70.0, grid=False, fuel_water=650.0), {},
     [trust_at(20, "battery_01", "estimated_capacity_kwh")], 300, "RESOLVED"),
    # №10 GRID_ONLY и провалы сети
    ("n10_grid_only_mode", {}, {}, [], 330, "FAILED"),
    ("n10_grid_only_mode", {}, {}, [at(20, "solar_inverter_01", "set_mode_hybrid")], 330, "RESOLVED"),
    # №11 ошибка изоляции E09
    ("n11_isolation_fault", {}, {"fog_s": 60}, [], 420, "EXPIRED"),
    ("n11_isolation_fault", {}, {"fog_s": 60}, [at(80, "solar_inverter_01", "reset_error")], 420, "RESOLVED"),
    # №12 ложная температура инвертора
    ("n12_false_inverter_temp", {}, {}, [], 260, "FAILED"),
    ("n12_false_inverter_temp", {}, {}, [trust_at(20, "solar_inverter_01", "inverter_temp_c")], 260, "RESOLVED"),
    # №13 все MPPT
    ("n13_all_mppt_off", {}, {}, [], 120, "EXPIRED"),
    ("n13_all_mppt_off", {}, {}, [at(20, "solar_panels_01", "enable_all_mppt")], 60, "RESOLVED"),
    # №14 ложный концевик Z
    ("n14_false_z_limit", {}, {}, [], 200, "EXPIRED"),
    ("n14_false_z_limit", {}, {}, [at(20, "cnc_01", "reset_alarm")], 60, "RESOLVED"),
    # №16 ложное завершение печати
    ("n16_false_print_complete", {}, {}, [at(20, "printer_3d_01", "abort")], 60, "FAILED"),
    ("n16_false_print_complete", {}, {}, [], 620, "RESOLVED"),
    # №18 обледенение анемометра
    ("n18_wind_overestimate", {}, {}, [], 400, "FAILED"),
    ("n18_wind_overestimate", {}, {}, [trust_at(10, "weather_station_01", "wind_speed_ms")], 400, "RESOLVED"),
    # №20 рассинхрон времени
    ("n20_time_desync", {}, {}, [], 620, "FAILED"),
    ("n20_time_desync", {}, {}, [at(20, "edge_compute_01", "sync_time")], 40, "RESOLVED"),
    # №21 потеря телеметрии узла
    ("n21_telemetry_loss", {}, {}, [], 200, "FAILED"),
    ("n21_telemetry_loss", {}, {}, [at(20, "water_filter_01", "reconnect")], 60, "RESOLVED"),
    # №22 коллизия шины МС-ТЮК
    ("n22_mstyuk_bus_collision", {}, {}, [], 200, "EXPIRED"),
    ("n22_mstyuk_bus_collision", {}, {}, [at(20, "thermal_insulation_01", "set_poll_interval", 500)], 200, "RESOLVED"),
    # №23 деградация SFP
    ("n23_sfp_degradation", {}, {}, [], 1220, "FAILED"),
    ("n23_sfp_degradation", {}, {}, [at(200, "network_link_01", "switch_path", "RESERVE", OP)], 260, "RESOLVED"),
    # №26 утечка на линии 2
    ("n26_leakage_line2", {}, {}, [], 420, "FAILED"),
    ("n26_leakage_line2", {}, {}, [at(60, "cnc_01", "feed_hold"), at(90, "smart_panel_01", "inspect_line", 2, OP),
                                   at(100, "cnc_01", "resume")], 160, "RESOLVED"),
    # №27 ложная пожарная тревога
    ("n27_false_fire_alarm", {}, {}, [], 90, "FAILED"),
    ("n27_false_fire_alarm", {}, {}, [at(10, AUTO, "acknowledge", "all"), at(20, "smoke_detector_01", "reset_alarm"),
                                      at(24, "cnc_01", "resume"), at(24, "printer_3d_01", "resume"),
                                      at(24, "smart_panel_01", "line_on", 1)], 200, "RESOLVED"),
    # №28 ложная перегрузка инвертора
    ("n28_false_inverter_overload", {}, {}, [], 150, "FAILED"),
    ("n28_false_inverter_overload", {}, {}, [trust_at(10, "solar_inverter_01", "error_code")], 150, "RESOLVED"),
    # №29 мерцание освещения
    ("n29_lighting_flicker", {}, {}, [], 320, "FAILED"),
    ("n29_lighting_flicker", {}, {}, [at(20, "smart_panel_01", "line_off", 4), at(20, "smart_panel_01", "line_on", 5)],
     60, "RESOLVED"),
    # №30 обрыв связи со щитом
    ("n30_panel_link_loss", {}, {}, [], 120, "MANUAL"),
    ("n30_panel_link_loss", {}, {}, [at(30, "network_link_01", "replace_patchcord", None, OP)], 120, "RESOLVED"),
    # №32 ложное занижение SOC
    ("n32_false_low_soc", {}, {}, [], 200, "FAILED"),
    ("n32_false_low_soc", {}, {}, [trust_at(10, "battery_01", "soc_pct"), trust_at(10, "solar_inverter_01", "battery_soc_pct")],
     200, "RESOLVED"),
    # №33 ложное завышение SOC (истинный SOC у отсечки, сети нет)
    ("n33_false_high_soc", dict(start_hour=21.0, grid=False), {"true_soc_pct": 23.0}, [], 600, "FAILED"),
    ("n33_false_high_soc", dict(start_hour=21.0, grid=False), {"true_soc_pct": 23.0},
     [at(20, "dizel_1", "turn_on")], 600, "RESOLVED"),
    # №36 ложная температура снаружи
    ("n36_false_outdoor_temp", {}, {}, [], 60, "EXPIRED"),
    ("n36_false_outdoor_temp", {}, {}, [trust_at(10, "weather_station_01", "temperature_c")], 60, "RESOLVED"),
    # №37 залипание флюгера
    ("n37_stuck_wind_vane", {}, {}, [trust_at(10, "weather_station_01", "wind_direction_deg")], 60, "RESOLVED"),
    # №38 дрейф влажности
    ("n38_humidity_drift", {}, {}, [], 900, "FAILED"),
    ("n38_humidity_drift", {}, {}, [trust_at(300, "climate_sensor_01", "humidity_pct")], 900, "RESOLVED"),
    # №40 сброс контроллера ЧПУ
    ("n40_cnc_controller_reset", {}, {}, [at(10, "cnc_01", "reset_alarm"),
                                          at(20, "cnc_01", "start_job", {"file_name": "b.nc", "duration_s": 600})],
     60, "FAILED"),
    ("n40_cnc_controller_reset", {}, {}, [at(10, "cnc_01", "reset_alarm"), at(20, "cnc_01", "home")], 60, "RESOLVED"),
    # №42 ложный перегрев сопла
    ("n42_false_nozzle_overheat", {}, {}, [at(20, "printer_3d_01", "pause")], 60, "FAILED"),
    ("n42_false_nozzle_overheat", {}, {}, [], 200, "RESOLVED"),
    # №43 ложный отказ вентилятора
    ("n43_false_fan_failure", {}, {}, [at(20, "solar_inverter_01", "reset_error")], 60, "RESOLVED"),
    # №44 ложная нестабильность сети — провал на закате
    ("n44_false_grid_instability", dict(start_hour=19.4), {}, [], 200, "FAILED"),
    ("n44_false_grid_instability", dict(start_hour=19.4), {}, [at(20, "solar_inverter_01", "reset_error")], 60, "RESOLVED"),
    # №46 ложная облачность (нет внешнего резерва)
    ("n46_false_cloudiness", dict(soc=50.0, grid=False, fuel_water=650.0, cloud=0.05), {}, [], 400, "FAILED"),
    ("n46_false_cloudiness", dict(soc=50.0, grid=False, fuel_water=650.0, cloud=0.05), {},
     [trust_at(10, "weather_station_01", "illuminance_lux")], 400, "RESOLVED"),
    # №47 ложное УЗО освещения
    ("n47_false_rcd_lighting", {}, {}, [], 60, "FAILED"),
    ("n47_false_rcd_lighting", {}, {}, [at(8, "smart_panel_01", "line_on", 5)], 30, "RESOLVED"),
    # №48 занижение PV
    ("n48_false_low_pv_voltage", {}, {}, [], 60, "EXPIRED"),
    ("n48_false_low_pv_voltage", {}, {}, [at(20, "solar_inverter_01", "reset_error")], 40, "RESOLVED"),
    # №49 дрейф датчика тока линии 1
    ("n49_current_drift_line1", {}, {}, [], 1000, "FAILED"),
    ("n49_current_drift_line1", {}, {}, [trust_at(60, "smart_panel_01", "current_a", line=1)], 1000, "RESOLVED"),
    # К3 цепная перегрузка (инвертор с пониженной способностью)
    ("k03_chain_overload", dict(solder=False), {"inverter_limit_pct": 60}, [], 240, "FAILED"),
    ("k03_chain_overload", dict(solder=False), {"inverter_limit_pct": 60},
     [at(5, "smart_panel_01", "set_workstation", "IDLE", OP), at(40, "cnc_01", "feed_hold"),
      at(5, "smart_panel_01", "line_off", 8), at(5, "smart_panel_01", "line_off", 7)], 240, "RESOLVED"),
    # К4 пожар на складе
    ("k04_storage_fire", {}, {}, [at(30, AUTO, "block_interlock", {"id": "IL_SMOKE", "minutes": 30})], 600, "FAILED"),
    ("k04_storage_fire", {}, {}, [at(20, "access_control_01", "lock_door", "STORAGE")], 900, "RESOLVED"),
    # К7 отказ охлаждения
    ("k07_cooling_failure", {}, {}, [], 1500, "FAILED"),
    ("k07_cooling_failure", {}, {}, [at(60, "equipment_cooling_01", "set_speed", 100),
                                     at(120, "equipment_cooling_01", "bleed_loop", None, OP)], 900, "RESOLVED"),
    # К8 утечка памяти edge
    ("k08_edge_memory_leak", {}, {}, [], 1500, "FAILED"),
    ("k08_edge_memory_leak", {}, {}, [at(300, "edge_compute_01", "restart_service", "telemetry_collector")], 400, "RESOLVED"),
    ("k08_edge_memory_leak", {}, {}, [at(300, "edge_compute_01", "reboot")], 400, "FAILED"),
    # К9 солнечная вспышка
    ("k09_solar_flare", {}, {}, [], 620, "FAILED"),
    ("k09_solar_flare", {}, {}, [at(30, "radio_01", "set_protocol", "LORA"),
                                 at(30, "backup_comms_01", "switch_channel", "MESH")], 120, "RESOLVED"),
    # К10 землетрясение
    ("k10_weak_earthquake", {}, {}, [at(30, "cnc_01", "feed_hold")], 60, "FAILED"),
    ("k10_weak_earthquake", {}, {}, [], 900, "RESOLVED"),
    # К12 двойная ложь
    ("k12_double_lie", {}, {}, [], 260, "FAILED"),
    ("k12_double_lie", {}, {}, [trust_at(20, "climate_sensor_01", "co2_ppm"), trust_at(20, "climate_sensor_03", "co2_ppm")],
     260, "RESOLVED"),
    # К13 филамента не хватит
    ("k13_filament_shortage", {}, {"spool_g": 1.0}, [], 300, "FAILED"),
    ("k13_filament_shortage", {}, {"spool_g": 5.0}, [at(10, "material_inventory_01", "replace_spool", None, OP)],
     60, "RESOLVED"),
]


@pytest.mark.parametrize("name,setup,params,actions,run_s,expected", CASES,
                         ids=[f"{c[0]}-{'act' if c[3] else 'idle'}-{c[5]}" for c in CASES])
def test_crisis_outcome(name, setup, params, actions, run_s, expected):
    sim = make(**setup)
    report = play(sim, name, params, actions, run_s)
    assert report.outcome == expected, (report.outcome, report.notes)


# ---------------------------------------------------------------------------
# Отдельные проверки механики
# ---------------------------------------------------------------------------

def test_catalog_and_aliases():
    sim = create_dome_simulator(seed=1)
    names = {c["name"] for c in sim.crisis_catalog()}
    assert len([n for n in names if n.startswith("n")]) == 43
    assert len([n for n in names if n.startswith("k")]) == 13
    engine = sim.scenario_engine
    assert engine.resolve_name("6") == "n06_phantom_co2"
    assert engine.resolve_name("№27") == "n27_false_fire_alarm"
    assert engine.resolve_name("К4") == "k04_storage_fire"
    assert engine.resolve_name("k1") == "k01_night_without_reserve"
    with pytest.raises(KeyError):
        engine.resolve_name("24")   # исключён из списка


def test_distortion_visible_to_agent_but_truth_intact():
    sim = make()
    sim.trigger_crisis("6")
    sim.step()
    assert sim.snapshot()["nodes"]["climate_sensor_01"]["co2_ppm"] > 2500
    assert sim.get("climate_sensor_01", "co2_ppm") < 1500
    assert sim.snapshot(view="truth")["nodes"]["climate_sensor_01"]["co2_ppm"] < 1500
    sim.stop_crisis("6")
    sim.step()
    assert sim.snapshot()["nodes"]["climate_sensor_01"]["co2_ppm"] < 1500


def test_z_limit_intercepts_reset_on_real_cnc():
    sim = make()
    sim.update("cnc_01", control_override_source="real")   # данные с реального GRBL
    sim.update("cnc_01", state="RUNNING")
    sim.trigger_crisis("14")
    assert ("cnc_01", "feed_hold", None) in sim.drain_real_commands()
    sim.step()
    assert sim.snapshot()["nodes"]["cnc_01"]["alarm_code"] == "ALARM:1"
    actions = sim.prepare_real_command("cnc_01", "reset_alarm")
    assert actions == [("resume", None)]                       # безопасный реальный эквивалент (Д7)
    sim.step()
    assert sim.snapshot()["nodes"]["cnc_01"]["alarm_code"] is None


def test_panel_link_loss_blocks_agent_commands():
    sim = make()
    sim.trigger_crisis("30")
    sim.step()
    with pytest.raises(ControlError):
        sim.control("smart_panel_01", "line_off", 8, origin="agent")
    assert sim.snapshot()["nodes"]["smart_panel_01"]["online"] is False
    assert sim.get("smart_panel_01", "lines")[7]["state"] == "ON"   # реле держат состояние


def test_mstyuk_nulls_until_poll_interval_fixed():
    sim = make()
    sim.trigger_crisis("22")
    nulls = 0
    for _ in range(15):
        sim.step()
        nulls += sum(1 for v in sim.snapshot()["nodes"]["thermal_insulation_01"].values() if v is None)
    assert nulls > 0
    sim.control("thermal_insulation_01", "set_poll_interval", 500, origin="agent")
    sim.step()
    nulls = 0
    for _ in range(15):
        sim.step()
        nulls += sum(1 for v in sim.snapshot()["nodes"]["thermal_insulation_01"].values() if v is None)
    assert nulls == 0


def test_edge_memory_leak_freezes_served_telemetry():
    sim = make()
    sim.trigger_crisis("К8", {"leak_pct_min": 12.0})
    for _ in range(90):
        sim.step()
    assert sim.get("edge_compute_01", "services")["telemetry_collector"] == "FAILED"
    assert sim.snapshot()["nodes"]["water_tank_01"]["status"] == "UNKNOWN"
    assert sim.snapshot()["nodes"]["battery_01"]["status"] == "OK"   # реальные драйверы не через edge


def test_k1_diesel_fails_on_water_until_fuel_transferred():
    sim = make(start_hour=20.5, soc=26.0)
    report = play(sim, "К1", {}, [], 240, stop=False)
    assert sim.get("dizel_1", "fault_reason") == "WATER_IN_FUEL"   # автозапуск по IL_LOAD_SHED
    sim.control("fuel_tank_01", "transfer_fuel", origin="agent")
    sim.step()  # качество топлива доходит до дизеля через правило
    sim.control("dizel_1", "turn_off", origin="agent")
    sim.control("dizel_1", "turn_on", origin="agent")
    assert sim.get("dizel_1", "state") == "ON"
    sim.stop_crisis("К1")
    assert report.outcome in ("FAILED", "RESOLVED")


def test_k2_dust_storm_trips_storm_mode_and_soils_panels():
    sim = make()
    report = play(sim, "К2", {}, [at(10, "supply_ventilation_01", "set_mode", "RECIRCULATION")], 900, stop=False)
    assert sim.get("dome_automation_01", "interlocks")["IL_STORM"]["trips"] >= 1
    assert sim.get("solar_panels_01", "soiling_loss_pct") > 5.0
    assert sim.get("supply_ventilation_01", "filter_clog_pct") < 98.0
    sim.stop_crisis("К2")
    assert report.outcome == "RESOLVED"


def test_k2_fresh_air_clogs_filter():
    sim = make()
    report = play(sim, "К2", {}, [at(8, AUTO, "block_interlock", {"id": "IL_STORM", "minutes": 30}),
                                  at(8, "supply_ventilation_01", "set_mode", "FRESH_AIR"),
                                  at(8, AUTO, "set_ventilation_auto", False),
                                  at(8, "supply_ventilation_01", "set_speed", 100)], 900)
    assert report.outcome == "FAILED"


def test_k5_chemical_release():
    sim = make()
    bad = play(sim, "К5", {"duration_s": 900}, [], 900)
    assert bad.outcome == "FAILED"
    sim = make()
    good = play(sim, "К5", {"duration_s": 1500, "leak_at_s": 900},
                [at(20, "dome_sealing_01", "seal_zone", "MAIN"),
                 at(20, "supply_ventilation_01", "set_mode", "RECIRCULATION"),
                 at(960, "dome_sealing_01", "repair_leak", None, OP)], 1500)
    assert good.outcome == "RESOLVED", good.notes


def test_k5_wind_vane_trap():
    sim = make()
    sim.trigger_crisis("К5")
    for _ in range(5):
        sim.step()
    assert sim.snapshot()["nodes"]["weather_station_01"]["wind_direction"] == "SW"   # «ветер от купола»
    assert 0 <= sim.environment.outdoor.wind_direction_deg < 90                        # на самом деле — на купол


def test_k6_filter_clog_disarms_fire_suppression():
    sim = make()
    bad = play(sim, "К6", {}, [], 2400)
    assert bad.outcome == "FAILED", bad.notes
    sim = make()
    good = play(sim, "К6", {}, [at(240, "water_filter_01", "switch_to_reserve"),
                               at(1080, "water_filter_01", "force_flush")], 2400)
    assert good.outcome == "RESOLVED", good.notes


def test_k11_frost_blocks_charge_and_gels_fuel():
    sim = make()
    report = play(sim, "К11", {}, [], 540, stop=False)
    assert sim.get("battery_01", "charge_allowed") is False     # MIXED: техпомещение АКБ ниже 0 °C
    for _ in range(180):                                        # до 21-й минуты: топливо ниже −10 °C
        sim.step()
    assert "n18_wind_overestimate" in sim.active_crises()      # обледенение анемометра
    sim.control("dizel_1", "turn_on", origin="agent")
    assert sim.get("dizel_1", "fault_reason") == "FUEL_GEL"
    sim.control("dizel_1", "turn_off", origin="agent")
    sim.control("dizel_1", "preheat", origin="agent")
    for _ in range(31):
        sim.step()
    sim.control("dizel_1", "turn_on", origin="agent")
    assert sim.get("dizel_1", "state") == "ON"
    sim.stop_crisis("К11")
    assert report.outcome == "RESOLVED"


def test_n31_fan_failure_blackout_without_agent():
    sim = make(start_hour=19.0)
    sim.update("solar_inverter_01", inverter_temp_c=55.0)
    report = play(sim, "31", {}, [], 1500)
    assert report.outcome == "FAILED"


def test_n39_false_co2_drop_lets_true_co2_rise():
    sim = make()
    report = play(sim, "39", {}, [], 3600)
    assert report.outcome == "FAILED"
    sim = make()
    report = play(sim, "39", {}, [trust_at(60, "climate_sensor_01", "co2_ppm"),
                                  at(60, AUTO, "set_primary_sensor", "climate_sensor_02")], 3600)
    assert report.outcome == "RESOLVED"


def test_n17_fume_extraction_trip():
    sim = make()
    bad = play(sim, "17", {}, [], 660)
    assert bad.outcome == "FAILED"
    sim = make()
    fume = "fume_extraction_01"
    good = play(sim, "17", {}, [at(20, fume, "clean_filter", None, OP), at(300, fume, "reset_alarm"),
                               at(300, "smart_panel_01", "line_on", 6)], 600)
    assert good.outcome == "RESOLVED", good.notes


def test_n19_weather_station_nulls():
    sim = make()
    report = play(sim, "19", {}, [], 60, stop=False)
    assert sim.snapshot()["nodes"]["weather_station_01"]["illuminance_lux"] is None
    sim.stop_crisis("19")
    assert report.outcome == "MANUAL"
