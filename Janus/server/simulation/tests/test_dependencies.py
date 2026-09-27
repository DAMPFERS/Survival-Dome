# tests/test_dependencies.py
"""Правила зависимостей, автоматика купола, слой телеметрии и гибридное время."""
import pytest

from dome_simulator import ControlError, create_dome_simulator


def run(sim, seconds):
    for _ in range(int(seconds / 4)):
        sim.step()


@pytest.fixture
def quiet():
    sim = create_dome_simulator(seed=11, start_hour=12.0)
    sim.set_random_faults(False)
    for node_id in ("printer_3d_01", "cnc_01"):
        sim.registry.get(node_id).auto_jobs = False
    sim.step()
    return sim


# ---------------------------------------------------------------------------
# Время
# ---------------------------------------------------------------------------

def test_hybrid_time_model():
    sim = create_dome_simulator(seed=1, start_hour=12.0)
    assert sim.environment.time_factor == pytest.approx(8.0)       # сутки — 3 часа
    assert sim.environment.device_factor == 1.0                    # физика — 1:1
    run(sim, 450)                                                  # 7.5 минут
    assert sim.environment.outdoor.hour == pytest.approx(13.0, abs=0.02)
    assert sim.environment.sim_time_s == pytest.approx(448, abs=4)


def test_legacy_time_model():
    sim = create_dome_simulator(seed=1, time_model="legacy", rules=False)
    assert sim.environment.device_factor == sim.environment.time_factor == 60.0


# ---------------------------------------------------------------------------
# Энергия
# ---------------------------------------------------------------------------

def test_panels_feed_inverter_and_battery_mirrors(quiet):
    sim = quiet
    run(sim, 60)
    inv = sim.get_node("solar_inverter_01")
    assert inv["control_pv_power_w"] == pytest.approx(sim.get("solar_panels_01", "power_w"), abs=1.0)
    assert sim.get("battery_01", "soc_pct") == pytest.approx(inv["battery_soc_pct"], abs=0.05)
    # нагрузка инвертора = щит + службы купола
    assert inv["control_load_w"] > sim.get("smart_panel_01", "total_power_w")


def test_blackout_cuts_lines_and_devices(quiet):
    sim = quiet
    sim.control("cnc_01", "start_job", {"file_name": "a.nc", "duration_s": 3600})
    run(sim, 20)
    sim.environment.force("external.grid_available", False)
    sim.registry.get("solar_inverter_01").set_soc(20.0)   # АКБ на отсечке, сети нет
    sim.environment.force("outdoor.cloud_cover", 1.0)
    sim.control("solar_panels_01", "disable_all_mppt")
    run(sim, 40)
    assert sim.get("solar_inverter_01", "error_code") == "E04"
    panel = sim.get_node("smart_panel_01")
    assert panel["bus_powered"] is False and panel["total_power_w"] == 0.0
    cnc = sim.get_node("cnc_01")
    assert cnc["online"] is False and cnc["alarm_code"] == "ALARM:3"   # позиция потеряна
    assert sim.get("edge_compute_01", "state") == "OFFLINE"


def test_diesel_feeds_inverter_and_burns_fuel(quiet):
    sim = quiet
    sim.environment.force("external.grid_available", False)
    sim.control("dizel_1", "turn_on")
    assert sim.get("dizel_1", "state") == "ON"
    fuel0 = sim.get("fuel_tank_01", "volume_l")
    run(sim, 120)
    assert sim.get("solar_inverter_01", "ac_source") == "GENERATOR"
    assert sim.get("solar_inverter_01", "generator_power_w") > 0
    assert sim.get("fuel_tank_01", "volume_l") < fuel0


def test_diesel_water_in_fuel_and_transfer(quiet):
    sim = quiet
    sim.update("fuel_tank_01", water_content_ppm=650.0, volume_l=90.0)
    run(sim, 8)
    sim.control("dizel_1", "turn_on")
    assert sim.get("dizel_1", "fault_reason") == "WATER_IN_FUEL"
    sim.control("fuel_tank_01", "transfer_fuel")
    assert sim.get("fuel_tank_01", "fuel_quality") == "GOOD"
    run(sim, 8)
    sim.control("dizel_1", "turn_off")
    sim.control("dizel_1", "turn_on")
    assert sim.get("dizel_1", "state") == "ON"


def test_frost_blocks_battery_charge(quiet):
    sim = quiet
    sim.environment.force("outdoor.temperature_c", -15.0)
    sim.control("supply_ventilation_01", "set_mode", "FRESH_AIR")
    sim.update("battery_01", temperature_c=1.0)
    run(sim, 900)
    assert sim.get("battery_01", "charge_allowed") is False
    assert sim.get("solar_inverter_01", "battery_charge_blocked") is True
    sim.control("supply_ventilation_01", "set_mode", "RECIRCULATION")
    run(sim, 2400)
    assert sim.get("battery_01", "charge_allowed") is True


def test_inverter_overload_restarts_after_30s(quiet):
    sim = quiet
    sim.update("solar_inverter_01", control_output_limit_pct=20.0)  # 360 Вт
    run(sim, 48)
    assert sim.get("solar_inverter_01", "error_code") == "E07"
    sim.update("solar_inverter_01", control_output_limit_pct=None)
    run(sim, 40)
    assert sim.get("solar_inverter_01", "error_code") is None


def test_grid_only_transfer_gap(quiet):
    sim = quiet
    sim.control("solar_inverter_01", "set_mode_grid_only")
    run(sim, 8)
    sim.environment.force("external.grid_available", False)
    sim.step()
    assert sim.get("solar_inverter_01", "output_voltage_v") == 0.0     # 10–20 с без выхода (Д9)
    run(sim, 24)
    assert sim.get("solar_inverter_01", "output_voltage_v") > 200.0


def test_fan_failure_derates_and_overheats():
    sim = create_dome_simulator(seed=5, start_hour=19.0)
    sim.set_random_faults(False)
    sim.update("solar_inverter_01", control_fan_fault="STOPPED", inverter_temp_c=55.0)
    sim.update("smart_panel_01", control_workstation="SOLDERING")
    temps = []
    for _ in range(20 * 15):
        sim.step()
        temps.append(sim.get("solar_inverter_01", "inverter_temp_c"))
        if sim.get("solar_inverter_01", "error_code") in ("E02", "E07"):
            break
    assert sim.get("solar_inverter_01", "fan_state") == "FAULT"
    assert max(temps) > 65.0
    assert sim.get("solar_inverter_01", "output_limit_pct") < 100.0


def test_leakage_trips_rcd(quiet):
    sim = quiet
    sim.update("smart_panel_01", control_leakage_ma={"2": 31.0})
    run(sim, 8)
    line = sim.get("smart_panel_01", "lines")[1]
    assert line["state"] == "RCD_TRIP" and line["leakage_ma"] == 0.0


def test_restore_after_blackout_has_inrush():
    from dome_simulator.nodes.power import INRUSH_K
    assert INRUSH_K[6] > 1 and INRUSH_K[4] > 1


# ---------------------------------------------------------------------------
# Производство
# ---------------------------------------------------------------------------

def test_printer_consumes_spool_and_runs_out(quiet):
    sim = quiet
    sim.registry.get("material_inventory_01").set_spool(20.0)
    sim.control("printer_3d_01", "start_job", {"file_name": "x.gcode", "duration_s": 600, "filament_g": 100})
    run(sim, 600)
    p = sim.get_node("printer_3d_01")
    assert p["state"] == "PAUSED" and p["error_code"] == "FILAMENT_RUNOUT"
    with pytest.raises(ControlError):
        sim.control("printer_3d_01", "resume")
    sim.control("material_inventory_01", "replace_spool")
    sim.step()
    sim.control("printer_3d_01", "resume")
    assert sim.get("printer_3d_01", "state") == "PRINTING"


def test_printer_long_pause_layer_adhesion(quiet):
    sim = quiet
    sim.control("printer_3d_01", "start_job", {"file_name": "x.gcode", "duration_s": 3600})
    run(sim, 120)
    sim.control("printer_3d_01", "pause")
    run(sim, 620)
    assert sim.get("printer_3d_01", "error_code") == "LAYER_ADHESION"
    assert sim.get("printer_3d_01", "part_defect") == "LAYER_ADHESION"


def test_cooling_air_in_loop_heats_spindle(quiet):
    sim = quiet
    sim.control("cnc_01", "start_job", {"file_name": "a.nc", "duration_s": 3600})
    sim.update("equipment_cooling_01", control_air_in_loop=True)
    run(sim, 1500)
    assert sim.get("equipment_cooling_01", "coolant_temp_c") > 40.0
    assert sim.get("cnc_01", "quality_state") in ("DEGRADED", "OVERHEAT")
    sim.control("equipment_cooling_01", "bleed_loop")
    run(sim, 900)
    assert sim.get("equipment_cooling_01", "control_air_in_loop") is False
    assert sim.get("equipment_cooling_01", "coolant_temp_c") < 36.0


# ---------------------------------------------------------------------------
# Воздух и вода
# ---------------------------------------------------------------------------

def test_recirculation_raises_co2(quiet):
    sim = quiet
    co2_0 = sim.environment.indoor.co2_ppm
    sim.control("dome_automation_01", "set_ventilation_auto", False)
    sim.control("supply_ventilation_01", "set_mode", "RECIRCULATION")
    sim.control("dome_sealing_01", "close_valves")
    run(sim, 1800)
    assert sim.environment.indoor.co2_ppm > co2_0 + 400


def test_soldering_without_extraction_raises_voc(quiet):
    sim = quiet
    sim.update("smart_panel_01", control_workstation="SOLDERING")
    run(sim, 600)
    with_fume = sim.environment.indoor.voc_index
    sim.control("smart_panel_01", "line_off", 6)
    run(sim, 600)
    assert with_fume < 200 < sim.environment.indoor.voc_index


def test_chemicals_enter_through_fresh_air(quiet):
    sim = quiet
    sim.environment.force("external.chem_substance", "NH3")
    sim.environment.force("external.chem_ppm", 40.0)
    sim.control("supply_ventilation_01", "set_mode", "FRESH_AIR")
    run(sim, 600)
    assert sim.get("air_quality_sensor_01", "chem_ppm") > 5.0


def test_water_chain_and_fire_suppression_draws_tank(quiet):
    sim = quiet
    run(sim, 40)
    v0 = sim.get("water_tank_01", "volume_l")
    sim.control("fire_suppression_01", "manual_start_zone", "STORAGE")
    run(sim, 300)
    assert sim.get("water_tank_01", "volume_l") < v0 - 80    # ~100 л за цикл
    assert sim.get("fire_suppression_01", "state") == "READY"  # цикл 5 минут завершён


def test_fablab_suppression_floods_line1(quiet):
    sim = quiet
    sim.control("fire_suppression_01", "manual_start_zone", "FABLAB")
    run(sim, 140)
    assert sim.get("smart_panel_01", "lines")[0]["state"] == "RCD_TRIP"


# ---------------------------------------------------------------------------
# Автоматика
# ---------------------------------------------------------------------------

def test_interlock_trips_on_telemetry_and_trust_disables_it(quiet):
    sim = quiet
    sim.control("cnc_01", "start_job", {"file_name": "a.nc", "duration_s": 3600})
    did = sim.telemetry.add_distortion("climate_sensor_01", "co2_ppm", "const", value=3000)
    run(sim, 72)
    assert sim.get("dome_automation_01", "interlocks")["IL_CO2"]["state"] == "TRIPPED"
    assert sim.get("cnc_01", "state") == "PAUSED"
    assert sim.environment.indoor.co2_ppm < 1500            # истина в норме
    sim.control("dome_automation_01", "set_data_trust",
                {"node": "climate_sensor_01", "param": "co2_ppm", "trust": "UNRELIABLE"})
    run(sim, 8)
    assert sim.get("dome_automation_01", "interlocks")["IL_CO2"]["state"] == "ARMED"
    sim.telemetry.remove_distortion(did)


def test_block_interlock_limits(quiet):
    sim = quiet
    with pytest.raises(ControlError):
        sim.control("dome_automation_01", "block_interlock", {"id": "IL_SMOKE", "minutes": 45})
    sim.control("dome_automation_01", "block_interlock", {"id": "IL_SMOKE", "minutes": 1})
    assert sim.get("dome_automation_01", "interlocks")["IL_SMOKE"]["state"] == "BLOCKED"
    run(sim, 64)
    assert sim.get("dome_automation_01", "interlocks")["IL_SMOKE"]["state"] == "ARMED"


def test_smoke_interlock_starts_suppression_unless_acknowledged(quiet):
    sim = quiet
    sim.environment.zones["STORAGE"].smoke_density = 0.5
    sim.environment.drivers.fire_intensity = {"STORAGE": 0.8}
    run(sim, 12)
    ev = [e for e in sim.get("dome_automation_01", "events") if e["type"] == "TRIP"]
    assert ev and ev[-1]["zone"] == "STORAGE"
    run(sim, 64)
    fire = sim.get_node("fire_suppression_01")
    assert fire["state"] == "ACTIVE" and fire["active_zone"] == "STORAGE"


def test_release_interlock_restores_lines(quiet):
    sim = quiet
    sim.telemetry.add_distortion("solar_inverter_01", "inverter_temp_c", "const", value=95.0)
    run(sim, 128)
    lines = sim.get("smart_panel_01", "lines")
    assert lines[0]["state"] == "OFF" and lines[5]["state"] == "OFF"
    sim.control("dome_automation_01", "block_interlock", {"id": "IL_HEAT", "minutes": 10})
    sim.control("dome_automation_01", "reset_interlock", "IL_HEAT")
    run(sim, 8)
    lines = sim.get("smart_panel_01", "lines")
    assert lines[0]["state"] == "ON" and lines[5]["state"] == "ON"


# ---------------------------------------------------------------------------
# Телеметрия, связь, реальные устройства
# ---------------------------------------------------------------------------

def test_fault_sensor_publishes_nulls(quiet):
    sim = quiet
    sim.update("weather_station_01", control_fault="CABLE_CUT")
    run(sim, 8)
    view = sim.telemetry_view("weather_station_01")
    assert view["wind_speed_ms"] is None and view["data_available"] is False
    assert "control_fault" not in view                      # входы кризисов скрыты
    assert sim.get("weather_station_01", "wind_speed_ms") is not None   # истина есть


def test_link_down_freezes_telemetry_and_blocks_agent(quiet):
    sim = quiet
    run(sim, 8)
    before = sim.snapshot()["nodes"]["battery_01"]
    sim.update("network_link_01", control_main_link_down=True)
    run(sim, 20)
    after = sim.snapshot()["nodes"]["battery_01"]
    assert after["status"] == "UNKNOWN" and after["updated_at"] == before["updated_at"] or \
        after["updated_at"] <= sim.environment.now() - 15
    with pytest.raises(ControlError):
        sim.control("smart_panel_01", "line_off", 8, origin="agent")
    sim.control("smart_panel_01", "line_off", 8, origin="admin")    # админ/Оператор — без ограничений
    sim.control("network_link_01", "switch_path", "RESERVE", origin="operator")
    run(sim, 8)
    assert sim.snapshot()["nodes"]["battery_01"]["status"] == "OK"


def test_agent_cannot_do_physical_actions(quiet):
    with pytest.raises(ControlError):
        quiet.control("network_link_01", "replace_patchcord", origin="agent")
    quiet.control("network_link_01", "replace_patchcord", origin="operator")


def test_real_node_commands_are_queued(quiet):
    sim = quiet
    sim.update("smart_panel_01", control_override_source="real")
    actions = sim.prepare_real_command("smart_panel_01", "emulate_protection_trip", {"line": 1}, origin="admin")
    assert actions == [("line_off", 1)]
    assert sim.get("smart_panel_01", "lines")[0]["state"] == "TRIPPED"
    actions = sim.prepare_real_command("smart_panel_01", "reset_protection", 1)
    assert actions == [("line_on", 1)]
    sim.update("printer_3d_01", control_override_source="real")
    sim.ctx.command("printer_3d_01", "pause", origin="automation")
    assert ("printer_3d_01", "pause", None) in sim.drain_real_commands()


def test_blackout_mirrors_to_real_relays(quiet):
    sim = quiet
    sim.update("smart_panel_01", control_override_source="real")
    sim.update("solar_inverter_01", control_output_limit_pct=5.0)
    run(sim, 60)
    cmds = sim.drain_real_commands()
    assert ("smart_panel_01", "line_off", 1) in cmds


def test_demo_energy_mode_uses_real_pv(quiet):
    sim = quiet
    sim.set_energy_mode("demo")
    sim.feed_real_inverter({"pv_power_w": 777.0, "load_power_w": 300.0})
    run(sim, 8)
    assert sim.get("solar_inverter_01", "control_pv_power_w") == 777.0


def test_edge_clock_offset_shifts_timestamps(quiet):
    sim = quiet
    sim.update("edge_compute_01", clock_offset_s=7200.0)
    run(sim, 8)
    nodes = sim.snapshot()["nodes"]
    assert nodes["water_tank_01"]["updated_at"] - nodes["battery_01"]["updated_at"] == pytest.approx(7200, abs=1)
    sim.control("edge_compute_01", "sync_time")
    run(sim, 4)
    nodes = sim.snapshot()["nodes"]
    assert abs(nodes["water_tank_01"]["updated_at"] - nodes["battery_01"]["updated_at"]) < 1
