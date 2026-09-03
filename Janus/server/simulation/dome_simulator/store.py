# dome_simulator/store.py
"""
Потокобезопасное хранилище состояния купола.

Внутри: dict[node_id, dict[param, value]]. Единственный регулярный
писатель — SimulatorThread, но чтение/точечная запись возможны из
любого потока (например, API Gateway дергает set() для control_*
параметров) — отсюда RLock на все операции.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional


class NodeNotFoundError(KeyError):
    pass


class StateStore:
    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    # ---------- регистрация узлов ----------

    def register_node(self, node_id: str, initial_state: dict[str, Any]) -> None:
        """Создаёт запись под узел. Идемпотентно перезатирает при повторной регистрации."""
        with self._lock:
            self._data[node_id] = dict(initial_state)

    def remove_node(self, node_id: str) -> None:
        with self._lock:
            self._data.pop(node_id, None)

    # ---------- чтение ----------

    def get(self, node_id: str, param: str, default: Any = None) -> Any:
        with self._lock:
            node = self._data.get(node_id)
            if node is None:
                raise NodeNotFoundError(node_id)
            return node.get(param, default)

    def get_node(self, node_id: str) -> dict[str, Any]:
        """Возвращает КОПИЮ состояния узла — наружу мутабельные ссылки не отдаём."""
        with self._lock:
            node = self._data.get(node_id)
            if node is None:
                raise NodeNotFoundError(node_id)
            return dict(node)

    def get_all(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {nid: dict(params) for nid, params in self._data.items()}

    def node_ids(self) -> list[str]:
        with self._lock:
            return list(self._data.keys())

    def snapshot(self) -> dict[str, Any]:
        """Атомарный слепок всего состояния + метка времени."""
        with self._lock:
            return {
                "timestamp": time.time(),
                "nodes": {nid: dict(params) for nid, params in self._data.items()},
            }

    # ---------- запись ----------

    def set(self, node_id: str, param: str, value: Any) -> None:
        with self._lock:
            node = self._data.get(node_id)
            if node is None:
                raise NodeNotFoundError(node_id)
            node[param] = value

    def update(self, node_id: str, **kwargs: Any) -> None:
        with self._lock:
            node = self._data.get(node_id)
            if node is None:
                raise NodeNotFoundError(node_id)
            node.update(kwargs)

    def bulk_update(self, updates: dict[str, dict[str, Any]]) -> None:
        """Атомарно применяет несколько узлов сразу (например, результат тика)."""
        with self._lock:
            for node_id, params in updates.items():
                node = self._data.setdefault(node_id, {})
                node.update(params)

    # ---------- persistence (простой JSON snapshot) ----------

    def dump_json(self, path: str | Path) -> None:
        with self._lock:
            payload = self.snapshot()
        Path(path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    def load_json(self, path: str | Path) -> None:
        """
        Загружает snapshot из файла и ПОЛНОСТЬЮ заменяет текущее состояние.
        Используется при рестарте процесса, чтобы не терять состояние купола.
        """
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        nodes = payload.get("nodes", {})
        with self._lock:
            self._data = {nid: dict(params) for nid, params in nodes.items()}