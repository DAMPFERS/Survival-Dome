import sqlite3
import uuid
import time
import os
from app.config import settings


def _connect():
    os.makedirs(os.path.dirname(settings.DB_PATH), exist_ok=True)
    conn = sqlite3.connect(settings.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS game (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            session_id TEXT NOT NULL,
            stage INTEGER NOT NULL DEFAULT 1,
            started_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            ts REAL NOT NULL
        )
        """
    )
    # если строки состояния ещё нет — создаём стартовую сессию
    row = conn.execute("SELECT * FROM game WHERE id = 1").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO game (id, session_id, stage, started_at) VALUES (1, ?, 1, ?)",
            (str(uuid.uuid4()), time.time()),
        )
    conn.commit()
    conn.close()


class GameState:
    def get_stage(self) -> int:
        conn = _connect()
        row = conn.execute("SELECT stage FROM game WHERE id = 1").fetchone()
        conn.close()
        return row["stage"]

    def get_session_id(self) -> str:
        conn = _connect()
        row = conn.execute("SELECT session_id FROM game WHERE id = 1").fetchone()
        conn.close()
        return row["session_id"]

    def set_stage(self, stage: int) -> None:
        stage = max(1, min(stage, settings.MAX_STAGE))
        conn = _connect()
        conn.execute("UPDATE game SET stage = ? WHERE id = 1", (stage,))
        conn.commit()
        conn.close()

    def reset_session(self) -> str:
        """Новый забег для следующей команды: новый session_id, этап 1, история не трогается (остаётся в логах по старому session_id)."""
        new_session = str(uuid.uuid4())
        conn = _connect()
        conn.execute(
            "UPDATE game SET session_id = ?, stage = 1, started_at = ? WHERE id = 1",
            (new_session, time.time()),
        )
        conn.commit()
        conn.close()
        return new_session

    def log_message(self, session_id: str, role: str, content: str) -> None:
        conn = _connect()
        conn.execute(
            "INSERT INTO messages (session_id, role, content, ts) VALUES (?, ?, ?, ?)",
            (session_id, role, content, time.time()),
        )
        conn.commit()
        conn.close()

    def get_recent_history(self, session_id: str, limit: int) -> list[dict]:
        conn = _connect()
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        conn.close()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def get_full_log(self, session_id: str) -> list[dict]:
        conn = _connect()
        rows = conn.execute(
            "SELECT role, content, ts FROM messages WHERE session_id = ? ORDER BY id ASC",
            (session_id,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]


game_state = GameState()
