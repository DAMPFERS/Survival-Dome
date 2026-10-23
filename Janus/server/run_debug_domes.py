#!/usr/bin/env python3
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
POLICY_SRC = SERVER_DIR / "authorization" / "access_policy.json"
RUNTIME_DIR = SERVER_DIR / "debug_domes"
BASE_PORT = int(os.getenv("DOME_DEBUG_BASE_PORT", "8765"))
TEAMS = (1, 2, 3, 4)


def _copy_policy(dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(POLICY_SRC, dest)


def _env_for(team: int, port: int, workdir: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
        "DOME_HOST": os.getenv("DOME_HOST", "0.0.0.0"),
        "DOME_PORT": str(port),
        "DOME_ACTIVE_TEAM": str(team),
        "DOME_AUTO_REAL": "0",
        "DOME_REAL_DEVICES": "none",
        "DOME_ENERGY_MODE": "virtual",
        "DOME_MEETING_FILE": str(workdir / "history.jsonl"),
        "DOME_POLICY_FILE": str(workdir / "access_policy.json"),
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


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(errors="replace")

    if not SERVER_FILE.is_file():
        print(f"Нет файла сервера: {SERVER_FILE}", file=sys.stderr)
        return 1
    if not POLICY_SRC.is_file():
        print(f"Нет политики доступа: {POLICY_SRC}", file=sys.stderr)
        return 1

    processes: list[subprocess.Popen] = []

    def stop_all() -> None:
        for proc in processes:
            if proc.poll() is None:
                proc.terminate()

    atexit.register(stop_all)

    print("Виртуальные копии купола:")
    for team in TEAMS:
        port = BASE_PORT + team - 1
        workdir = RUNTIME_DIR / f"team_{team}"
        workdir.mkdir(parents=True, exist_ok=True)
        _copy_policy(workdir / "access_policy.json")
        proc = subprocess.Popen(
            [sys.executable, str(SERVER_FILE)],
            cwd=str(SERVER_DIR),
            env=_env_for(team, port, workdir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        processes.append(proc)
        threading.Thread(target=_pipe, args=(f"[team {team}]", proc.stdout), daemon=True).start()
        print(f"  команда {team}: ws://0.0.0.0:{port}  pid={proc.pid}")

    print("Ctrl+C — остановить все 4 сервера")
    try:
        while True:
            alive = [p for p in processes if p.poll() is None]
            if not alive:
                break
            if len(alive) < len(processes):
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

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
