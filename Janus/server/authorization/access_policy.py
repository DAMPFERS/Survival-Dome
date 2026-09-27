"""
Политика доступа участников к узлам купола.

Админ видит и может всё. Для участника (роль "team") политика определяет:
    - какие поля телеметрии верхнего уровня он получает (telemetry_fields);
    - какие узлы видит (visible) и какие параметры узлов скрыты (hidden_params, glob);
    - какие управляющие воздействия ему разрешены (actions: список основных имён или "*").

Политика хранится в access_policy.json и перечитывается автоматически при
изменении файла — поменять права участника можно прямо во время игры.
Ошибка в файле не ломает сервер: остаётся предыдущая корректная версия.
Если корректной версии нет вообще — участнику запрещено всё (fail closed).

Формат (секция "participant"):
    {
      "telemetry_fields": ["game_time"],
      "hidden_params": ["control_*", "control_override_source"],
      "default": {"visible": true, "actions": []},
      "nodes": {
        "smart_panel_01": {"actions": ["line_on", "line_off"]},
        "thermal_insulation_01": {"actions": ["set_fan_pwm"], "hidden_params": ["pwm_distortion"]},
        "edge_compute_01": {"visible": false}
      }
    }
"""
from __future__ import annotations

import copy
import json
import logging
import os
import threading
from fnmatch import fnmatchcase
from typing import Any, Iterable, Optional

logger = logging.getLogger("access_policy")

POLICY_FILE = os.path.join(os.path.dirname(__file__), "access_policy.json")

ROLE_ADMIN = "admin"
ROLE_TEAM = "team"

_NODE_KEYS = {"visible", "actions", "hidden_params"}


class PolicyError(ValueError):
    """Некорректная политика доступа."""


def _validate(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or not isinstance(raw.get("participant"), dict):
        raise PolicyError('ожидается объект с секцией "participant"')
    p = raw["participant"]
    for key in ("telemetry_fields", "hidden_params"):
        if not isinstance(p.get(key, []), list):
            raise PolicyError(f'"{key}" должен быть списком')
    if not isinstance(p.get("default", {}), dict):
        raise PolicyError('"default" должен быть объектом')
    nodes = p.get("nodes", {})
    if not isinstance(nodes, dict):
        raise PolicyError('"nodes" должен быть объектом')
    for node_id, rule in [("default", p.get("default", {})), *nodes.items()]:
        if not isinstance(rule, dict):
            raise PolicyError(f"{node_id}: правило должно быть объектом")
        unknown = set(rule) - _NODE_KEYS - {"_comment"}
        if unknown:
            raise PolicyError(f"{node_id}: неизвестные ключи {sorted(unknown)}")
        actions = rule.get("actions", [])
        if actions != "*" and not (isinstance(actions, list) and all(isinstance(a, str) for a in actions)):
            raise PolicyError(f'{node_id}: "actions" — список строк или "*"')
        if not isinstance(rule.get("hidden_params", []), list):
            raise PolicyError(f'{node_id}: "hidden_params" должен быть списком')
        if not isinstance(rule.get("visible", True), bool):
            raise PolicyError(f'{node_id}: "visible" должен быть true/false')
    return raw


class AccessPolicy:
    def __init__(self, path: str = POLICY_FILE) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._raw: Optional[dict[str, Any]] = None
        self._mtime: Optional[float] = None
        self.reload()

    # ---------------- загрузка / сохранение ----------------

    def reload(self) -> bool:
        """Перечитывает файл. True — применена новая версия."""
        with self._lock:
            try:
                mtime = os.path.getmtime(self.path)
                self._mtime = mtime  # эту версию файла больше не перечитываем, даже если она битая
                with open(self.path, "r", encoding="utf-8") as f:
                    raw = _validate(json.load(f))
            except (OSError, json.JSONDecodeError, PolicyError) as e:
                logger.error("Политика доступа %s не загружена: %s", self.path, e)
                if self._raw is None:
                    logger.error("Нет корректной политики — участникам запрещено всё")
                return False
            self._raw = raw
            logger.info("Политика доступа загружена: %s", self.path)
            return True

    def _refresh(self) -> None:
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return
        if mtime != self._mtime:
            self.reload()

    def _participant(self) -> dict[str, Any]:
        self._refresh()
        return (self._raw or {}).get("participant", {})

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._raw or {})

    def update_node(self, node_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        """Меняет правило узла для участника и сохраняет файл. Возвращает новое правило."""
        with self._lock:
            if self._raw is None:
                raise PolicyError("Политика не загружена — исправьте файл вручную")
            raw = copy.deepcopy(self._raw)
            nodes = raw["participant"].setdefault("nodes", {})
            rule = dict(nodes.get(node_id, {}))
            rule.update({k: v for k, v in changes.items() if k in _NODE_KEYS})
            nodes[node_id] = rule
            _validate(raw)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
            self._raw, self._mtime = raw, os.path.getmtime(self.path)
            return rule

    # ---------------- проверки ----------------

    def _rule(self, node_id: str) -> dict[str, Any]:
        p = self._participant()
        rule = dict(p.get("default", {}))
        rule.update(p.get("nodes", {}).get(node_id, {}))
        return rule

    def node_visible(self, role: str, node_id: str) -> bool:
        if role == ROLE_ADMIN:
            return True
        with self._lock:
            if self._raw is None:
                return False
            return bool(self._rule(node_id).get("visible", True))

    def can_control(self, role: str, node_id: str, action: str) -> bool:
        """action — основное имя действия (после разрешения алиасов)."""
        if role == ROLE_ADMIN:
            return True
        with self._lock:
            if self._raw is None or not self.node_visible(role, node_id):
                return False
            actions = self._rule(node_id).get("actions", [])
            return actions == "*" or action.lower() in {a.lower() for a in actions}

    def allowed_actions(self, role: str, node_id: str, all_actions: Iterable[str]) -> list[str]:
        return [a for a in all_actions if self.can_control(role, node_id, a)]

    def telemetry_fields(self, role: str) -> Optional[set[str]]:
        """None — без ограничений (админ)."""
        if role == ROLE_ADMIN:
            return None
        with self._lock:
            return set(self._participant().get("telemetry_fields", [])) if self._raw else set()

    def filter_node_params(self, role: str, node_id: str, params: dict[str, Any]) -> dict[str, Any]:
        if role == ROLE_ADMIN:
            return params
        with self._lock:
            patterns = list(self._participant().get("hidden_params", []))
            patterns += self._rule(node_id).get("hidden_params", [])
        return {k: v for k, v in params.items() if not any(fnmatchcase(k, p) for p in patterns)}

    def filter_nodes(self, role: str, nodes: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        if role == ROLE_ADMIN:
            return nodes
        return {nid: self.filter_node_params(role, nid, params)
                for nid, params in nodes.items() if self.node_visible(role, nid)}
