# tests/test_nodes.py
"""
Тесты узлов песочницы (реестр dome_sandbox_nodes.md).

Используют stepped_simulator: без фонового потока, с фиксированным seed,
время двигается синхронно через sim.step(). Один шаг = 4 игровые минуты.
"""
import pytest

from dome_simulator import SANDBOX_NODES, ControlError

STEPS_PER_HOUR = 15


def steps(sim, n):
    for _ in range(n):
        sim.step()


def run_until_hour(sim, hour):
    """Двигает симуляцию до ближайшего наступления игрового часа hour."""
    for _ in range(STEPS_PER_HOUR * 25):
        if abs(sim.environment.outdoor.hour - hour) < 0.05:
            return
        sim.step()
    raise AssertionError(f"не дошли до {hour}:00")


# ---------------------------------------------------------------------------
# Общее
# ---------------------------------------------------------------------------

def test_all_registry_nodes_present(stepped_simulator):
    ids = set(stepped_simulator.node_ids())
    expected = {node_id for _, node_id, _ in SANDBOX_NODES}
    assert ids == expected
    assert len(ids) == 35  # + smoke_detector_02, network_link_01, dome_automation_01


def test_common_fields(stepped_simulator):
    for node_id, params in stepped_simulator.get_all().items():
        assert params["online"] is True, node_id
        assert "control_override_source" in params, node_id


def test_catalog_lists_controls_from_registry(stepped_simulator):
    catalog = {n["node_id"]: n for n in stepped_simulator.describe_nodes()}
    assert catalog["solar_inverter_01"]["code_name"] == "solar_inverter"
    assert "start_cell_balancing" in catalog["solar_inverter_01"]["controls"]
    assert catalog["battery_01"]["controls"] == []
    assert "feed_hold" in catalog["cnc_01"]["controls"]
    assert "feedhold" in catalog["cnc_01"]["control_aliases"]["feed_hold"]


def test_unknown_action_rejected(stepped_simulator):
    with pytest.raises(ControlError):
        stepped_simulator.control("battery_01", "explode")


def test_action_names_case_insensitive(stepped_simulator):
    stepped_simulator.control("solar_inverter_01", "START_CELL_BALANCING")
    assert stepped_simulator.get("solar_inverter_01", "balancing_state") == "BALANCING"


def test_same_seed_same_data():
    from dome_simulator import create_dome_simulator
    a = create_dome_simulator(seed=99)
    b = create_dome_simulator(seed=99)
    steps(a, 40)
    steps(b, 40)
    assert a.get_all() == b.get_all()


def test_real_override_freezes_virtual_generation(stepped_simulator):
    sim = stepped_simulator
    steps(sim, 3)
    sim.update("climate_sensor_01", control_override_source="real", temperature_c=30.0)
    steps(sim, 5)
    assert sim.get("climate_sensor_01", "temperature_c") == 30.0


def test_sensor_fault_input(stepped_simulator):
    sim = stepped_simulator
    sim.set("weather_station_01", "control_fault", "CABLE_CUT")
    steps(sim, 2)
    node = sim.get_node("weather_station_01")
    assert node["sensor_state"] == "FAULT"
    assert node["confidence"] < 0.1


# ---------------------------------------------------------------------------
# Энергетика
# ---------------------------------------------------------------------------

def test_solar_day_night(stepped_simulator):
    sim = stepped_simulator
    run_until_hour(sim, 13)
    assert sim.get("solar_panels_01", "power_w") > 100
    assert sim.get("solar_inverter_01", "pv_power_w") > 100
    run_until_hour(sim, 1)
    assert sim.get("solar_panels_01", "power_w") == 0.0


def test_solar_mppt_channels(stepped_simulator):
    sim = stepped_simulator
    run_until_hour(sim, 12)
    sim.control("solar_panels_01", "disable_mppt_1")
    node = sim.get_node("solar_panels_01")
    assert node["mppt_1_enabled"] is False
    assert node["active_mppt_count"] == 1
    assert node["all_mppt_enabled"] is False

    sim.control("solar_panels_01", "disable_all_mppt")
    sim.step()
    node = sim.get_node("solar_panels_01")
    assert node["power_w"] == 0.0
    assert node["active_mppt_count"] == 0

    sim.control("solar_panels_01", "enable_mppt", 2)
    assert sim.get("solar_panels_01", "mppt_channels_enabled") == [False, True]


def test_inverter_modes_and_balancing(stepped_simulator):
    sim = stepped_simulator
    sim.control("solar_inverter_01", "set_mode_grid_only")
    assert sim.get("solar_inverter_01", "mode") == "GRID_ONLY"
    with pytest.raises(ControlError):
        sim.control("solar_inverter_01", "set_mode", "TURBO")

    sim.control("solar_inverter_01", "start_cell_balancing")
    with pytest.raises(ControlError):
        sim.control("solar_inverter_01", "start_cell_balancing")
    steps(sim, 12)  # 48 игровых минут
    assert sim.get("solar_inverter_01", "balancing_state") == "COMPLETED"


def test_inverter_overheat_error_and_reset(stepped_simulator):
    sim = stepped_simulator
    sim.set("solar_inverter_01", "inverter_temp_c", 95.0)
    sim.step()
    node = sim.get_node("solar_inverter_01")
    assert node["error"] is True and node["error_code"] == "E02"
    assert node["output_voltage_v"] == 0.0
    steps(sim, 20)  # остывает при заблокированном выходе
    sim.control("solar_inverter_01", "reset_error")
    sim.step()
    assert sim.get("solar_inverter_01", "error") is False


def test_battery_telemetry_consistent(stepped_simulator):
    sim = stepped_simulator
    for _ in range(STEPS_PER_HOUR * 24):
        sim.step()
        b = sim.get_node("battery_01")
        assert 0.0 <= b["soc_pct"] <= 100.0
        assert b["state"] in ("CHARGING", "DISCHARGING", "IDLE")
        if b["state"] == "CHARGING":
            assert b["current_a"] > 0
        if b["state"] == "DISCHARGING":
            assert b["current_a"] < 0
        assert 22.0 < b["voltage_v"] < 29.5


def test_battery_follows_control_power(stepped_simulator):
    sim = stepped_simulator
    sim.set("battery_01", "control_power_w", -1500.0)
    before = sim.get("battery_01", "soc_pct")
    steps(sim, 5)
    assert sim.get("battery_01", "state") == "DISCHARGING"
    assert sim.get("battery_01", "soc_pct") < before


def test_wind_turbine_on_off(stepped_simulator):
    sim = stepped_simulator
    sim.control("wind_turbine_01", "turn_off")
    steps(sim, 2)
    node = sim.get_node("wind_turbine_01")
    assert node["state"] == "OFF" and node["power_w"] == 0.0 and node["rpm"] == 0.0
    sim.control("wind_turbine_01", "turn_on")
    assert sim.get("wind_turbine_01", "state") == "ON"


def test_wind_turbine_storm_protection(stepped_simulator):
    sim = stepped_simulator
    sim.environment._wind.value = 30.0  # шторм
    sim.environment._wind.mean = 30.0
    steps(sim, 1)
    node = sim.get_node("wind_turbine_01")
    assert node["state"] == "FAULT" and node["fault_code"] == "OVERSPEED"
    with pytest.raises(ControlError):
        sim.control("wind_turbine_01", "turn_on")


def test_dizel_start_and_fuel_loss(stepped_simulator):
    sim = stepped_simulator
    for _ in range(5):  # запуск может не удаться (START_FAILURE) — пробуем снова
        sim.control("dizel_1", "turn_on")
        if sim.get("dizel_1", "state") == "ON":
            break
        sim.control("dizel_1", "turn_off")
    assert sim.get("dizel_1", "state") == "ON"
    steps(sim, 5)
    assert sim.get("dizel_1", "power_w") > 0
    assert sim.get("dizel_1", "runtime_h") > 0

    sim.set("dizel_1", "control_fuel_available", False)
    sim.step()
    assert sim.get("dizel_1", "state") == "FAULT"
    assert sim.get("dizel_1", "fault_code") == "NO_FUEL"


def test_fuel_tank_consumption_and_transfer(stepped_simulator):
    sim = stepped_simulator
    start = sim.get("fuel_tank_01", "volume_l")
    sim.set("fuel_tank_01", "control_consumption_l_h", 30.0)
    steps(sim, STEPS_PER_HOUR)  # час
    after = sim.get("fuel_tank_01", "volume_l")
    assert after == pytest.approx(start - 30.0, abs=0.5)

    sim.control("fuel_tank_01", "transfer_fuel", 20)
    assert sim.get("fuel_tank_01", "volume_l") == pytest.approx(after + 20.0, abs=0.01)
    assert sim.get("fuel_tank_01", "reserve_l") == pytest.approx(130.0)

    sim.set("fuel_tank_01", "control_consumption_l_h", 200.0)
    steps(sim, STEPS_PER_HOUR)
    assert sim.get("fuel_tank_01", "state") == "CRITICAL"


def test_smart_panel_lines(stepped_simulator):
    sim = stepped_simulator
    sim.step()
    lines = sim.get("smart_panel_01", "lines")
    assert [l["line"] for l in lines] == list(range(1, 9))
    assert lines[1]["name"] == "ЧПУ-фрезер"

    sim.control("smart_panel_01", "line_off", 4)
    sim.step()
    line4 = sim.get("smart_panel_01", "lines")[3]
    assert line4["state"] == "OFF" and line4["power_w"] == 0.0 and line4["voltage_v"] == 0.0
    sim.control("smart_panel_01", "line_on", {"line": 4})
    assert sim.get("smart_panel_01", "lines")[3]["state"] == "ON"


def test_smart_panel_protection(stepped_simulator):
    sim = stepped_simulator
    sim.control("smart_panel_01", "emulate_protection_trip", {"line": 5, "type": "RCD_TRIP"})
    line5 = sim.get("smart_panel_01", "lines")[4]
    assert line5["state"] == "RCD_TRIP" and line5["alarm"] is True
    with pytest.raises(ControlError):
        sim.control("smart_panel_01", "line_on", 5)
    sim.control("smart_panel_01", "reset_protection", 5)
    sim.control("smart_panel_01", "reset_line_error", 5)
    line5 = sim.get("smart_panel_01", "lines")[4]
    assert line5["state"] == "ON" and line5["alarm"] is False

    # Перегрузка: нагрузка на линии 2 выше номинала автомата (10 А)
    sim.set("smart_panel_01", "control_line_loads_w", {"2": 4000.0})
    sim.step()
    line2 = sim.get("smart_panel_01", "lines")[1]
    assert line2["state"] == "TRIPPED" and line2["alarm_code"] == "OVERCURRENT"


# ---------------------------------------------------------------------------
# Производство
# ---------------------------------------------------------------------------

def _quiet_production(sim):
    """Отключает автозадания и случайные аварии, чтобы тест управлял сам."""
    for node_id in ("printer_3d_01", "cnc_01"):
        node = sim.registry.get(node_id)
        node.auto_jobs = False
        if hasattr(node, "error_rate_per_hour"):
            node.error_rate_per_hour = 0.0
        if hasattr(node, "alarm_rate_per_hour"):
            node.alarm_rate_per_hour = 0.0


def test_printer_job_lifecycle(stepped_simulator):
    sim = stepped_simulator
    _quiet_production(sim)
    sim.control("printer_3d_01", "start_job", {"file_name": "part.gcode", "duration_s": 1800})
    sim.step()
    p = sim.get_node("printer_3d_01")
    assert p["state"] == "PRINTING" and p["file_name"] == "part.gcode"
    assert p["nozzle_temp_c"] > 150

    sim.control("printer_3d_01", "pause")
    assert sim.get("printer_3d_01", "paused") is True
    progress = sim.get("printer_3d_01", "progress_pct")
    steps(sim, 3)
    assert sim.get("printer_3d_01", "progress_pct") == progress
    sim.control("printer_3d_01", "resume")

    steps(sim, 12)
    p = sim.get_node("printer_3d_01")
    assert p["state"] == "COMPLETED" and p["progress_pct"] == 100.0
    assert p["real_state"] == "COMPLETED"


def test_printer_webhook_lags_real_state(stepped_simulator):
    sim = stepped_simulator
    _quiet_production(sim)
    sim.control("printer_3d_01", "start_job", "lag.gcode")
    sim.step()
    p = sim.get_node("printer_3d_01")
    assert p["real_state"] == "PRINTING" and p["webhook_state"] == "IDLE"
    sim.step()
    assert sim.get("printer_3d_01", "webhook_state") == "PRINTING"


def test_printer_gcode(stepped_simulator):
    sim = stepped_simulator
    _quiet_production(sim)
    sim.control("printer_3d_01", "send_gcode", "G28\nM104 S200 ; nozzle\nM140 S55")
    p = sim.get_node("printer_3d_01")
    assert p["homed"] is True and p["nozzle_target_c"] == 200 and p["bed_target_c"] == 55
    assert p["gcode_state"] == "OK"

    sim.control("printer_3d_01", "send_gcode", "M999")
    assert sim.get("printer_3d_01", "gcode_state") == "ERROR"

    with pytest.raises(ControlError):
        sim.control("printer_3d_01", "send_gcode", "")


def test_printer_power_loss(stepped_simulator):
    sim = stepped_simulator
    _quiet_production(sim)
    sim.control("printer_3d_01", "start_job", "x.gcode")
    sim.set("printer_3d_01", "control_powered", False)
    sim.step()
    p = sim.get_node("printer_3d_01")
    assert p["online"] is False and p["state"] == "ERROR" and p["error_code"] == "POWER_LOSS"
    with pytest.raises(ControlError):
        sim.control("printer_3d_01", "home")


def test_cnc_job_hold_abort(stepped_simulator):
    sim = stepped_simulator
    _quiet_production(sim)
    sim.control("cnc_01", "start_job", {"file_name": "board.nc", "duration_s": 3600})
    steps(sim, 2)
    c = sim.get_node("cnc_01")
    assert c["state"] == "RUNNING" and c["spindle_rpm"] > 5000
    assert 0 <= c["x_mm"] <= 300 and 0 <= c["y_mm"] <= 180

    sim.control("cnc_01", "feed_hold")
    pos = (sim.get("cnc_01", "x_mm"), sim.get("cnc_01", "y_mm"))
    steps(sim, 2)
    assert (sim.get("cnc_01", "x_mm"), sim.get("cnc_01", "y_mm")) == pos
    sim.control("cnc_01", "~")  # алиас Resume
    assert sim.get("cnc_01", "state") == "RUNNING"

    sim.control("cnc_01", "abort")
    steps(sim, 1)
    c = sim.get_node("cnc_01")
    assert c["state"] == "IDLE" and c["spindle_rpm"] == 0.0


def test_cnc_soft_limit_alarm(stepped_simulator):
    sim = stepped_simulator
    _quiet_production(sim)
    sim.control("cnc_01", "send_gcode", "G0 X500 Y10")
    c = sim.get_node("cnc_01")
    assert c["state"] == "ALARM" and c["alarm_code"] == "ALARM:2"

    sim.control("cnc_01", "send_gcode", "G0 X10")
    assert sim.get("cnc_01", "error_code") == "error:9"  # G-code заблокирован в ALARM

    sim.control("cnc_01", "reset_alarm")
    sim.control("cnc_01", "home")
    c = sim.get_node("cnc_01")
    assert c["state"] == "IDLE" and c["homed"] is True and c["x_mm"] == 0.0


def test_material_inventory_usage(stepped_simulator):
    sim = stepped_simulator
    sim.set("material_inventory_01", "control_filament_usage_g_h", 2000.0)
    steps(sim, STEPS_PER_HOUR)
    inv = sim.get_node("material_inventory_01")
    assert inv["filament_g"] < 450
    assert "FILAMENT_LOW" in inv["low_stock_warnings"]


def test_equipment_cooling(stepped_simulator):
    sim = stepped_simulator
    sim.control("equipment_cooling_01", "set_speed", 100)
    steps(sim, 2)
    full = sim.get("equipment_cooling_01", "pump_power_w")
    sim.control("equipment_cooling_01", "set_speed", 30)
    steps(sim, 2)
    assert sim.get("equipment_cooling_01", "pump_power_w") < full

    sim.control("equipment_cooling_01", "turn_off")
    sim.set("equipment_cooling_01", "control_heat_load_w", 800.0)
    steps(sim, 10)
    node = sim.get_node("equipment_cooling_01")
    assert node["coolant_flow_l_min"] == 0.0
    assert node["alarm_code"] == "OVERHEAT"


# ---------------------------------------------------------------------------
# Климат, вентиляция, герметизация
# ---------------------------------------------------------------------------

def test_climate_sensors_measure_same_air(stepped_simulator):
    sim = stepped_simulator
    steps(sim, 30)
    t1 = sim.get("climate_sensor_01", "temperature_c")
    true_t = sim.environment.indoor.temperature_c
    assert abs(t1 - true_t) < 1.0
    for extra in ("climate_sensor_02", "climate_sensor_03"):
        assert abs(sim.get(extra, "temperature_c") - true_t) < 2.0
    s = sim.get_node("climate_sensor_01")
    assert s["dew_point_c"] < s["temperature_c"]


def test_extra_sensor_drift_and_recalibrate(stepped_simulator):
    sim = stepped_simulator
    steps(sim, STEPS_PER_HOUR * 48)
    assert abs(sim.get("climate_sensor_03", "drift_temp_c")) > 0.2
    assert sim.get("climate_sensor_03", "confidence") < sim.get("climate_sensor_01", "confidence")
    sim.control("climate_sensor_03", "recalibrate")
    sim.step()
    assert abs(sim.get("climate_sensor_03", "drift_temp_c")) < 0.05


def test_smoke_alarm_latches(stepped_simulator):
    sim = stepped_simulator
    sim.registry.get("smoke_detector_01").false_alarm_rate_per_hour = 0.0
    sim.environment.indoor.smoke_density = 0.4
    sim.step()
    assert sim.get("smoke_detector_01", "alarm_state") == "ALARM"
    assert sim.get("smoke_detector_01", "smoke_detected") is True

    sim.environment.indoor.smoke_density = 0.0
    sim.step()
    assert sim.get("smoke_detector_01", "smoke_detected") is False
    assert sim.get("smoke_detector_01", "alarm_state") == "ALARM"  # фиксация до сброса
    sim.control("smoke_detector_01", "RESET_ALARM")
    sim.step()
    assert sim.get("smoke_detector_01", "alarm_state") == "NORMAL"


def test_fume_extraction(stepped_simulator):
    sim = stepped_simulator
    steps(sim, 2)
    assert sim.get("fume_extraction_01", "fan_rpm") > 2000
    sim.control("fume_extraction_01", "turn_off")
    sim.step()
    node = sim.get_node("fume_extraction_01")
    assert node["fan_rpm"] == 0 and node["power_w"] == 0.0


def test_supply_ventilation_modes(stepped_simulator):
    sim = stepped_simulator
    sim.registry.get("supply_ventilation_01").fault_rate_per_hour = 0.0
    sim.control("supply_ventilation_01", "set_mode", "recirculation")
    sim.step()
    indoor = sim.environment.indoor.temperature_c
    assert abs(sim.get("supply_ventilation_01", "supply_temp_c") - indoor) < 0.5
    with pytest.raises(ControlError):
        sim.control("supply_ventilation_01", "set_mode", "TURBO")

    sim.set("supply_ventilation_01", "filter_clog_pct", 99.0)
    sim.step()
    assert sim.get("supply_ventilation_01", "alarm_code") == "FILTER_BLOCKED"
    with pytest.raises(ControlError):
        sim.control("supply_ventilation_01", "reset_alarm")
    sim.control("supply_ventilation_01", "replace_filter")
    sim.control("supply_ventilation_01", "reset_alarm")
    assert sim.get("supply_ventilation_01", "state") == "ON"


def test_dome_sealing(stepped_simulator):
    sim = stepped_simulator
    sim.registry.get("dome_sealing_01").stuck_rate_per_hour = 0.0
    sim.step()
    open_dp = sim.get("dome_sealing_01", "pressure_diff_pa")
    sim.control("dome_sealing_01", "seal_zone", "FABLAB")
    sim.step()
    assert sim.get("dome_sealing_01", "state") == "CLOSED"
    assert sim.get("dome_sealing_01", "sealed_zone") == "FABLAB"
    assert sim.get("dome_sealing_01", "pressure_diff_pa") > open_dp + 20

    sim.set("dome_sealing_01", "leak_rate_pct", 40.0)
    sim.step()
    assert sim.get("dome_sealing_01", "leak_detected") is True
    assert sim.get("dome_sealing_01", "seal_ready") is False


def test_thermal_insulation_fans(stepped_simulator):
    sim = stepped_simulator
    sim.control("thermal_insulation_01", "set_fan_pwm", {"fan": 0, "pwm": 255})
    sim.control("thermal_insulation_01", "set_fan_pwm", {"fan": 5, "pwm": 0})
    sim.step()
    node = sim.get_node("thermal_insulation_01")
    assert node["fans_in_rpm"][0] > 2800
    assert node["fans_out_rpm"][1] == 0
    assert len(node["pressure_inside_pa"]) == 2
    with pytest.raises(ControlError):
        sim.control("thermal_insulation_01", "set_fan_pwm", {"fan": 6, "pwm": 10})

    sim.control("thermal_insulation_01", "set_random_rpm_mode", True)
    sim.step()
    assert sim.get("thermal_insulation_01", "fans_in_pwm") != [255, 128, 128, 128]


# ---------------------------------------------------------------------------
# Внешние системы, метео
# ---------------------------------------------------------------------------

def test_weather_station_follows_environment(stepped_simulator):
    sim = stepped_simulator
    sim.registry.get("weather_station_01").dropout_rate_per_hour = 0.0
    steps(sim, 5)
    ws = sim.get_node("weather_station_01")
    o = sim.environment.outdoor
    assert abs(ws["temperature_c"] - o.temperature_c) < 1.5
    assert abs(ws["pressure_hpa"] - o.pressure_hpa) < 2.0
    assert ws["wind_direction"] in ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def test_radiation_and_chem_alarms(stepped_simulator):
    sim = stepped_simulator
    sim.environment.external.radiation_usv_h = 2.5
    sim.environment._radiation.value = 2.5
    sim.environment._radiation.mean = 2.5
    sim.environment.external.chem_ppm = 30.0
    sim.environment.external.chem_substance = "NH3"
    sim.step()
    assert sim.get("radiation_sensor_01", "alarm_state") == "ALARM"
    chem = sim.get_node("chem_sensor_01")
    assert chem["alarm_state"] == "ALARM" and chem["substance"] == "NH3"

    with pytest.raises(ControlError):
        sim.control("chem_sensor_01", "set_thresholds", {"warning": 50, "alarm": 10})
    sim.control("chem_sensor_01", "set_thresholds", {"warning": 40, "alarm": 80})
    sim.control("chem_sensor_01", "reset_alarm")
    sim.step()
    assert sim.get("chem_sensor_01", "alarm_state") == "NORMAL"


def test_seysmo_threshold(stepped_simulator):
    sim = stepped_simulator
    sim.control("seysmo_01", "set_threshold", 0.01)
    sim.step()
    assert sim.get("seysmo_01", "quake_detected") is True
    sim.control("seysmo_01", "set_threshold", 100)
    sim.step()
    assert sim.get("seysmo_01", "quake_detected") is False


def test_radio_and_backup_comms(stepped_simulator):
    sim = stepped_simulator
    with pytest.raises(ControlError):
        sim.control("radio_01", "set_frequency", 900)
    sim.control("radio_01", "set_protocol", "lora")
    sim.step()
    assert sim.get("radio_01", "protocol") == "LORA"
    assert sim.get("radio_01", "reception") is True

    sim.control("backup_comms_01", "switch_channel", "LORA")
    assert sim.get("backup_comms_01", "protocol") == "LORAWAN"
    with pytest.raises(ControlError):
        sim.control("backup_comms_01", "set_protocol", "IRIDIUM_SBD")
    sim.control("backup_comms_01", "set_traffic_priority", "EMERGENCY")
    sim.step()
    assert sim.get("backup_comms_01", "state") in ("AVAILABLE", "DEGRADED", "UNAVAILABLE")


def test_edge_compute_reboot(stepped_simulator):
    sim = stepped_simulator
    sim.control("edge_compute_01", "reboot")
    assert sim.get("edge_compute_01", "state") == "OFFLINE"
    steps(sim, 1)
    assert sim.get("edge_compute_01", "state") == "ONLINE"
    assert set(sim.get("edge_compute_01", "services").values()) == {"RUNNING"}
    sim.control("edge_compute_01", "limit_load", 20)
    steps(sim, 3)
    assert sim.get("edge_compute_01", "cpu_load_pct") <= 20.0


# ---------------------------------------------------------------------------
# Водоснабжение, безопасность
# ---------------------------------------------------------------------------

def test_water_tank_levels(stepped_simulator):
    sim = stepped_simulator
    sim.set("water_tank_01", "control_inflow_l_min", 0.0)
    sim.set("water_tank_01", "control_outflow_l_min", 5.0)
    steps(sim, 60)  # 4 часа: -1200 л
    tank = sim.get_node("water_tank_01")
    assert tank["level_pct"] < 25
    assert tank["state"] in ("LOW", "CRITICAL")


def test_water_pump(stepped_simulator):
    sim = stepped_simulator
    sim.set("water_pump_01", "control_demand_l_min", 20.0)
    steps(sim, 3)
    pump = sim.get_node("water_pump_01")
    assert pump["duty_cycle_pct"] == pytest.approx(50.0, abs=2)
    sim.control("water_pump_01", "set_power_limit", 40)
    sim.control("water_pump_01", "turn_off")
    steps(sim, 2)
    pump = sim.get_node("water_pump_01")
    assert pump["running"] is False and pump["power_w"] == 0.0
    assert pump["pressure_bar"] < 2.0  # без насоса давление падает


def test_water_filter_flush_and_reserve(stepped_simulator):
    sim = stepped_simulator
    wear_before = sim.get("water_filter_01", "wear_pct")["MAIN"]
    sim.control("water_filter_01", "force_flush")
    assert sim.get("water_filter_01", "system_state") == "FLUSHING"
    steps(sim, 5)
    assert sim.get("water_filter_01", "system_state") == "NORMAL"
    assert sim.get("water_filter_01", "wear_pct")["MAIN"] < wear_before

    sim.control("water_filter_01", "switch_to_reserve")
    assert sim.get("water_filter_01", "active_filter") == "RESERVE"


def test_fire_suppression(stepped_simulator):
    sim = stepped_simulator
    sim.control("fire_suppression_01", "manual_start_zone", "FABLAB")
    steps(sim, 1)  # цикл пуска — 5 минут, шаг legacy — 4 игровые минуты
    fs = sim.get_node("fire_suppression_01")
    assert fs["state"] == "ACTIVE" and fs["valves"]["FABLAB"] == "OPEN"
    assert fs["pressure_bar"] < 5.0
    sim.control("fire_suppression_01", "stop")
    sim.control("fire_suppression_01", "block_automation", True)
    assert sim.get("fire_suppression_01", "state") == "DISABLED"

    sim.set("fire_suppression_01", "control_auto_trigger_zone", "LIVING")
    sim.step()
    assert sim.get("fire_suppression_01", "active_zone") is None  # автоматика заблокирована


def test_access_control(stepped_simulator):
    sim = stepped_simulator
    steps(sim, STEPS_PER_HOUR * 3)
    assert len(sim.get("access_control_01", "passage_events")) > 0

    sim.control("access_control_01", "lockdown", True)
    assert sim.get("access_control_01", "system_state") == "LOCKDOWN"
    with pytest.raises(ControlError):
        sim.control("access_control_01", "unlock_door", "FABLAB")
    sim.control("access_control_01", "lockdown", False)
    sim.control("access_control_01", "unlock_door", "FABLAB")
    assert sim.get("access_control_01", "doors")["FABLAB"]["locked"] is False
