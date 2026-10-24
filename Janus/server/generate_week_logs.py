#!/usr/bin/env python3
"""
Генерация недельного лога купола на виртуальном куполе (для RAG агента участников).

    python generate_week_logs.py                         # неделя с 12.10.2026, logs/week_2026-10-12
    python generate_week_logs.py --start 2026-11-02 --seed 7 --out logs/week_b
    python generate_week_logs.py --days 1                # быстрая проверка (один день, ~2 мин)

Результат — журнал участника (как пишет сервер): <out>/<дата>/telemetry.jsonl, events.jsonl
и бортовой журнал logbook.jsonl. Отчёт организаторов (исходы кризисов, реакция экипажа)
пишется рядом — <out>.report.json; участникам его не отдают.
Неделя (7 суток, физика 1:1) считается ~15 минут.
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SERVER_DIR))

from week_logs import generate  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", default="2026-10-12", help="дата первого дня (понедельник), ГГГГ-ММ-ДД")
    parser.add_argument("--days", type=int, default=7, help="сколько суток (1..7)")
    parser.add_argument("--seed", type=int, default=2026, help="seed погоды и шумов")
    parser.add_argument("--snapshot-s", type=float, default=60.0, help="период снимков телеметрии, с")
    parser.add_argument("--out", default=None, help="каталог журнала (по умолчанию logs/week_<start>)")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    logging.getLogger("dome_simulator").setLevel(logging.WARNING)

    start = datetime.strptime(args.start, "%Y-%m-%d")
    days = max(1, min(7, args.days))
    out = Path(args.out) if args.out else SERVER_DIR / "logs" / f"week_{args.start}"
    report_path = out.with_name(out.name + ".report.json")
    report = generate(out, start, days=days, seed=args.seed, snapshot_s=args.snapshot_s, report_path=report_path)

    outcomes = Counter(c["outcome"] for c in report["crises"])
    print(f"\nЖурнал участника: {out}")
    print(f"Отчёт организаторов: {report_path}")
    print(f"Команд экипажа: {report['commands']}; кризисов: {len(report['crises'])} — {dict(outcomes)}")
    for c in report["crises"]:
        print(f"  {c['code']:>4} {c['name']:<32} {c['outcome']:<11} {'; '.join(c['notes'])[:110]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
