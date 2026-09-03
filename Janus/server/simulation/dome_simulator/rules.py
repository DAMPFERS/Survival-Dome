# dome_simulator/rules.py
"""
Стартовый набор правил зависимостей (п.4.6, "богатая модель" из ТЗ).
Каждая функция — самодостаточное правило, распознающее нужные ему узлы
по ключам StateStore (см. таблицу ролей в пояснении).
"""
from __future__ import annotations

from .events import Event, EventBus, Severity
from .store import StateStore


# ---------------------------------------------------------------------------
# Энергетика
# ---------------------------------------------------------------------------

def _wants_power(params: dict) -> bool:
    """Хочет ли управляемая нагрузка работать в этом тике."""
    if "enabled" in params:
        return bool(params["enabled"])
    if "status" in params:
        return params["status"] == "running"
    return True  # узлы без явного флага (water_recycling, oxygen_generator) всегда пытаются работать


def _shed_priority(params: dict) -> int:
    """Чем меньше число — тем раньше отключаем при дефиците энергии.
    Производство отключаем первым, жизнеобеспечение — последним."""
    if "job_remaining_s" in params:       # производственные узлы (_JobRunnerMixin)
        return 1
    if "clog_pct" in params or "effect_ppm_s" in params:  # климат
        return 2
    return 3  # water_recycling, oxygen_generator и т.п. — жизнеобеспечение, отключаем последними


def energy_balance_rule(store: StateStore, event_bus: EventBus, dt: float) -> None:
    """
    Главное правило сети:
      1. Считает суммарную выработку (производители) и спрос (нагрузки).
      2. Дефицит сначала покрывается батареями (в пределах control_max_discharge_kw).
      3. Если и этого не хватило — публикует crisis.power_loss и последовательно
         отключает управляемые нагрузки (control_powered=False) по приоритету,
         затем "шэдит" базовых потребителей (EnergyConsumer.shed=True) по priority.
      4. При избытке — восстанавливает нагрузки/потребителей и заряжает батареи.
    """
    if dt <= 0:
        return  # пауза — баланс не пересчитываем

    all_nodes = store.get_all()

    producers = {nid: p for nid, p in all_nodes.items()
                 if "output_kw" in p and "control_powered" not in p}
    total_production_kw = sum(p["output_kw"] for p in producers.values())

    controllable = {nid: p for nid, p in all_nodes.items()
                     if "power_draw_kw" in p and "control_powered" in p}
    wanted_loads = {nid: p for nid, p in controllable.items() if _wants_power(p)}
    controllable_demand_kw = sum(p["power_draw_kw"] for p in wanted_loads.values())

    base_consumers = {nid: p for nid, p in all_nodes.items()
                       if "actual_kw" in p and "shed" in p}
    base_demand_kw = sum(p["actual_kw"] for p in base_consumers.values() if not p["shed"])

    total_demand_kw = controllable_demand_kw + base_demand_kw
    deficit_kw = total_demand_kw - total_production_kw

    batteries = {nid: p for nid, p in all_nodes.items() if "charge_kwh" in p}

    if deficit_kw > 1e-6:
        remaining_kw = deficit_kw
        for bid, bp in batteries.items():
            if remaining_kw <= 0:
                break
            max_discharge_kw = bp.get("control_max_discharge_kw", 0.0)
            take_kw = min(max_discharge_kw, remaining_kw)
            energy_kwh = take_kw * (dt / 3600.0)
            energy_kwh = min(energy_kwh, bp["charge_kwh"])
            if energy_kwh <= 0:
                continue
            actual_kw = energy_kwh / (dt / 3600.0)
            store.set(bid, "charge_kwh", round(bp["charge_kwh"] - energy_kwh, 4))
            remaining_kw -= actual_kw

        if remaining_kw > 1e-3:
            event_bus.publish(Event(
                type="crisis.power_loss", source="dependency_engine",
                payload={"deficit_kw": round(remaining_kw, 3)}, severity=Severity.CRITICAL,
            ))
            # отключаем управляемые нагрузки по приоритету (производство первым)
            for nid, p in sorted(wanted_loads.items(), key=lambda kv: _shed_priority(kv[1])):
                if remaining_kw <= 0:
                    break
                store.set(nid, "control_powered", False)
                remaining_kw -= p["power_draw_kw"]
            # затем шэдим базовых потребителей по priority (больше число = отключаем раньше)
            for nid, p in sorted(base_consumers.items(), key=lambda kv: -kv[1].get("priority", 5)):
                if remaining_kw <= 0:
                    break
                if not p["shed"]:
                    store.set(nid, "shed", True)
                    remaining_kw -= p["actual_kw"]
        else:
            for nid in wanted_loads:
                store.set(nid, "control_powered", True)
    else:
        # избыток мощности: восстанавливаем всех и подзаряжаем батареи
        surplus_kw = -deficit_kw
        for nid in controllable:
            store.set(nid, "control_powered", True)
        for nid, p in base_consumers.items():
            if p["shed"]:
                store.set(nid, "shed", False)

        chargeable = [(bid, bp) for bid, bp in batteries.items()
                      if bp["charge_kwh"] < bp.get("capacity_kwh", bp["charge_kwh"])]
        if chargeable and surplus_kw > 0:
            share_kw = surplus_kw / len(chargeable)
            for bid, bp in chargeable:
                energy_kwh = share_kw * (dt / 3600.0)
                cap = bp.get("capacity_kwh", bp["charge_kwh"])
                store.set(bid, "charge_kwh", round(min(cap, bp["charge_kwh"] + energy_kwh), 4))


def diesel_autostart_rule(store: StateStore, event_bus: EventBus, dt: float) -> None:
    """Автозапуск дизеля при просадке батарей ниже 20%, останов при восстановлении выше 60%."""
    all_nodes = store.get_all()
    batteries = [p for p in all_nodes.values() if "charge_pct" in p]
    generators = {nid: p for nid, p in all_nodes.items() if "fuel_l" in p and "running" in p}
    if not generators or not batteries:
        return

    low = any(p["charge_pct"] < 0.20 for p in batteries)
    healthy = all(p["charge_pct"] > 0.60 for p in batteries)

    for gid, gp in generators.items():
        if low and not gp["running"] and gp["fuel_l"] > 0:
            store.set(gid, "running", True)
            store.set(gid, "control_target_output_kw", gp["max_output_kw"])
            event_bus.publish(Event(
                type="node.overload", source=gid,
                payload={"reason": "auto_start_low_battery"}, severity=Severity.WARNING,
            ))
        elif healthy and gp["running"]:
            store.set(gid, "running", False)


# ---------------------------------------------------------------------------
# Климат / атмосфера
# ---------------------------------------------------------------------------

def co2_scrubber_to_sensor_rule(store: StateStore, event_bus: EventBus, dt: float) -> None:
    """Суммирует эффект всех CO2Scrubber и передаёт его во все CO2Sensor."""
    all_nodes = store.get_all()
    total_effect = sum(p["effect_ppm_s"] for p in all_nodes.values() if "effect_ppm_s" in p)
    for nid, p in all_nodes.items():
        if "ppm" in p and "control_scrub_effect_ppm_s" in p:
            store.set(nid, "control_scrub_effect_ppm_s", round(total_effect, 5))


def ventilation_coupling_rule(store: StateStore, event_bus: EventBus, dt: float) -> None:
    """Состояние VentilationSystem определяет:
      - множитель дрейфа температуры в ZoneClimate,
      - множитель генерации CO2 (плохая вентиляция -> CO2 копится быстрее)."""
    all_nodes = store.get_all()
    vents = [p for p in all_nodes.values() if "airflow_m3_h" in p]
    if not vents:
        multiplier = 1.0
    else:
        avg_health = sum(p["health"] for p in vents) / len(vents)
        any_running = any(p["running"] for p in vents)
        if not any_running:
            multiplier = 3.0
        else:
            multiplier = 1.0 + (1.0 - avg_health) * 2.0

    for nid, p in all_nodes.items():
        if "control_drift_multiplier" in p:
            store.set(nid, "control_drift_multiplier", round(multiplier, 3))
        if "control_generation_multiplier" in p:
            store.set(nid, "control_generation_multiplier", round(multiplier, 3))


# ---------------------------------------------------------------------------
# Жизнеобеспечение
# ---------------------------------------------------------------------------

def water_recycling_to_tank_rule(store: StateStore, event_bus: EventBus, dt: float) -> None:
    """Суммирует output_l_s всех WaterRecycling и передаёт в inflow всех WaterTank."""
    all_nodes = store.get_all()
    total_inflow = sum(p["output_l_s"] for p in all_nodes.values() if "output_l_s" in p)
    for nid, p in all_nodes.items():
        if "level_l" in p and "control_inflow_l_s" in p:
            store.set(nid, "control_inflow_l_s", round(total_inflow, 5))


# ---------------------------------------------------------------------------
# Инфраструктура
# ---------------------------------------------------------------------------

def structural_wear_from_crises_rule(store: StateStore, event_bus: EventBus, dt: float) -> None:
    """Чем больше критических событий недавно — тем быстрее растёт износ купола
    (StructuralIntegrity.control_extra_wear_rate)."""
    recent = event_bus.get_recent(n=100)
    critical_count = sum(1 for e in recent if e.severity == Severity.CRITICAL)
    extra_rate = 0.00003 * critical_count

    for nid, p in store.get_all().items():
        if "wear_pct" in p:
            store.set(nid, "control_extra_wear_rate", round(extra_rate, 6))


def emergency_lighting_rule(store: StateStore, event_bus: EventBus, dt: float) -> None:
    """Включает аварийное освещение при активном дефиците энергии
    (см. крит.событие crisis.power_loss в недавней истории), выключает при его отсутствии."""
    recent = event_bus.get_recent(n=20, event_type="crisis.power_loss")
    power_loss_active = len(recent) > 0

    for nid, p in store.get_all().items():
        if "battery_pct" in p and "active" in p:  # EmergencyLighting
            if power_loss_active and not p["active"]:
                store.set(nid, "active", True)
            elif not power_loss_active and p["active"]:
                store.set(nid, "active", False)


# ---------------------------------------------------------------------------
# Сборка стартового набора
# ---------------------------------------------------------------------------

def build_default_rules() -> list[tuple[str, "RuleFunc"]]:
    """Порядок важен: сначала энергобаланс (он двигает control_powered),
    потом всё, что от него зависит (климат, жизнеобеспечение), потом
    производные инфраструктурные эффекты."""
    from .dependency_engine import RuleFunc  # локальный импорт, чтобы избежать цикла типов
    return [
        ("energy_balance", energy_balance_rule),
        ("diesel_autostart", diesel_autostart_rule),
        ("co2_scrubber_to_sensor", co2_scrubber_to_sensor_rule),
        ("ventilation_coupling", ventilation_coupling_rule),
        ("water_recycling_to_tank", water_recycling_to_tank_rule),
        ("structural_wear_from_crises", structural_wear_from_crises_rule),
        ("emergency_lighting", emergency_lighting_rule),
    ]