#!/usr/bin/env python3
"""
Четыре независимых виртуальных купола — по одному на команду — за одним портом.

    python run_team_domes.py

Купол N — отдельный процесс server_integrated.py: активная команда N, всё
оборудование виртуальное, своя политика доступа, своя песочница и свой журнал
участника (team_domes/team_N). Купола слушают только 127.0.0.1:18765..18768,
снаружи открыт один порт 8765 — маршрутизатор dome_router.py по ключу
отправляет команду в её купол.

    участники:  ws://<host>:8765               (ключ своей команды)
    админ:      ws://<host>:8765/team/N        (админский ключ, N — купол)
                python admin/admin_panel.py --server ws://localhost:8765/team/2 --port 8082

Переменные окружения DOME_* (DOME_TIME_MODE, DOME_START_HOUR, DOME_INTERLOCKS…)
передаются всем куполам. Порты: DOME_ROUTER_PORT (8765), DOME_BACKEND_BASE_PORT (18765).
Политика доступа купола копируется из authorization/access_policy.json при каждом запуске.
"""
from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent
SERVER_FILE = SERVER_DIR / "server_integrated.py"
ROUTER_FILE = SERVER_DIR / "dome_router.py"
POLICY_SRC = SERVER_DIR / "authorization" / "access_policy.json"
RUNTIME_DIR = SERVER_DIR / "team_domes"
ROUTER_PORT = int(os.getenv("DOME_ROUTER_PORT", "8765"))
BACKEND_BASE_PORT = int(os.getenv("DOME_BACKEND_BASE_PORT", "18765"))
BACKEND_HOST = "127.0.0.1"   # купола не видны из сети — только через маршрутизатор
TEAMS = (1, 2, 3, 4)


def _base_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update({"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"})
    return env


def _dome_env(team: int, port: int, workdir: Path) -> dict[str, str]:
    env = _base_env()
    env.update({
        "DOME_HOST": BACKEND_HOST,
        "DOME_PORT": str(port),
        "DOME_ACTIVE_TEAM": str(team),
        "DOME_AUTO_REAL": "0",
        "DOME_REAL_DEVICES": "none",
        "DOME_ENERGY_MODE": "virtual",
        "DOME_MEETING_FILE": str(workdir / "history.jsonl"),
        "DOME_POLICY_FILE": str(workdir / "access_policy.json"),
        "DOME_PARTICIPANT_LOG": str(workdir / "participant_log"),
    })
    return env


def _router_env(routes: dict[int, int]) -> dict[str, str]:
    env = _base_env()
    env.update({
        "DOME_ROUTER_HOST": os.getenv("DOME_ROUTER_HOST", "0.0.0.0"),
        "DOME_ROUTER_PORT": str(ROUTER_PORT),
        "DOME_ROUTES": ",".join(f"{team}=ws://{BACKEND_HOST}:{port}" for team, port in routes.items()),
    })
    return env


def _pipe(prefix: str, stream) -> None:
    try:
        for line in iter(stream.readline, ""):
            try:
                sys.stdout.write(f"{prefix} {line}")
                sys.stdout.flush()
            except UnicodeEncodeError:
                text = f"{prefix} {line}".encode(sys.stdout.encoding or "utf-8", errors="replace")
                sys.stdout.buffer.write(text)
                sys.stdout.flush()
    finally:
        stream.close()


def _spawn(prefix: str, script: Path, env: dict[str, str]) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        cwd=str(SERVER_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    threading.Thread(target=_pipe, args=(prefix, proc.stdout), daemon=True).start()
    return proc


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(errors="replace")

    for path in (SERVER_FILE, ROUTER_FILE, POLICY_SRC):
        if not path.is_file():
            print(f"Нет файла: {path}", file=sys.stderr)
            return 1

    processes: list[subprocess.Popen] = []

    def stop_all() -> None:
        for proc in processes:
            if proc.poll() is None:
                proc.terminate()

    atexit.register(stop_all)

    routes = {team: BACKEND_BASE_PORT + team - 1 for team in TEAMS}
    print("Купола команд:")
    for team, port in routes.items():
        workdir = RUNTIME_DIR / f"team_{team}"
        workdir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(POLICY_SRC, workdir / "access_policy.json")
        proc = _spawn(f"[dome {team}]", SERVER_FILE, _dome_env(team, port, workdir))
        processes.append(proc)
        print(f"  команда {team}: ws://{BACKEND_HOST}:{port}  pid={proc.pid}  "
              f"(админ: ws://<host>:{ROUTER_PORT}/team/{team})")

    router = _spawn("[router]", ROUTER_FILE, _router_env(routes))
    processes.append(router)
    print(f"Маршрутизатор: ws://0.0.0.0:{ROUTER_PORT}  pid={router.pid} — участники подключаются сюда своим ключом")
    print("Ctrl+C — остановить всё")

    rc = 0
    try:
        while True:
            dead = [p for p in processes if p.poll() is not None]
            if dead:
                # купол или маршрутизатор упал — останавливаем всё, чтобы не играть на неполном наборе
                print(f"Процесс pid={dead[0].pid} завершился (код {dead[0].returncode}) — останавливаю остальные",
                      file=sys.stderr)
                rc = 1
                stop_all()
                break
            time.sleep(0.3)
    except KeyboardInterrupt:
        stop_all()

    deadline = time.time() + 5
    for proc in processes:
        remaining = max(0.0, deadline - time.time())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
