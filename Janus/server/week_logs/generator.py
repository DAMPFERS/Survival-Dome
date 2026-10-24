# week_logs/generator.py
"""Прогон недели: распорядок + кризисы + бортовой журнал, журнал участника по дням."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .logbook import Logbook
from .plan import WeekPlan, logbook_times, schedule
from .world import DAY_S, Wait, World

logger = logging.getLogger("week_logs")


def _logbook_task(w: World, book: Logbook, at_s: float, author: str, summary: bool):
    yield Wait(max(0.0, at_s - w.t))
    entry = book.write(w, author, summary=summary)
    logger.info("Бортовой журнал %s: %s (%d символов)", entry["ts"], author, len(entry["text"]))


def generate(out_dir: str | Path, start: datetime, days: int = 7, seed: int = 2026, snapshot_s: float = 60.0,
             report_path: Optional[str | Path] = None, plan: Optional[WeekPlan] = None) -> dict[str, Any]:
    """Генерирует журнал участника за days суток в out_dir. Возвращает отчёт организаторов
    (исходы кризисов, факты дня) — он пишется в report_path, не в журнал участника."""
    out_dir = Path(out_dir)
    plan = plan or WeekPlan(days=days)
    plan.days = days
    w = World(out_dir, start, seed=seed, snapshot_s=snapshot_s)
    w.plan = plan
    book = Logbook(out_dir, seed)
    w.stats_hooks.append(book.stats)
    schedule(w, plan)
    for at_s, author, summary in logbook_times(plan):
        w.spawn(f"logbook_{at_s:.0f}", _logbook_task(w, book, at_s, author, summary))

    t0 = time.monotonic()

    def progress(world: World) -> None:
        if int(world.t) % (6 * 3600) < 3600:
            elapsed = time.monotonic() - t0
            done = world.t / (days * DAY_S)
            logger.info("%s — %.0f %% (%.0f с, осталось ~%.0f с)", world.now.strftime("%a %d.%m %H:%M"),
                        100 * done, elapsed, elapsed / max(done, 1e-6) * (1 - done))

    try:
        w.run_until(days * DAY_S, progress)
    finally:
        w.log.close()

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "start": start.isoformat(), "days": days, "seed": seed, "snapshot_s": snapshot_s,
        "participant_log": str(out_dir),
        "crises": w.sim.crisis_reports(),
        "facts": [{"day": d, "at": f.at, "key": f.key, "variant": f.variant, "belief": f.belief, "data": f.data}
                  for d, facts in sorted(w.facts.items()) for f in facts],
        "commands": w.commands,
    }
    if report_path is not None:
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return report
