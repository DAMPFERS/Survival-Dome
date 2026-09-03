# dome_simulator/nodes/base.py
"""
Базовый контракт узла и реестр узлов.

Модель хранения:
    - Node-инстанс хранит только КОНФИГУРАЦИЮ (константы).
    - Вся ИЗМЕНЯЕМАЯ телеметрия живёт в StateStore под node_id.
    - tick() читает/пишет телеметрию через переданный `state`,
      возвращает список событий, случившихся за тик (не публикует сам).
"""
from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from typing import Any, Callable, Type

from ..events import Event, EventBus
from ..store import StateStore


class BaseNode(ABC):
    """Контракт, обязательный для любого узла купола."""

    category: str = "generic"  # переопределяется в подклассах: "power", "climate", ...

    def __init__(self, node_id: str) -> None:
        self.node_id = node_id

    # ---- обязательные методы (п. 4.4 ТЗ) ----

    @abstractmethod
    def tick(self, dt: float, state: StateStore, event_bus: EventBus) -> list[Event]:
        """
        Выполняется каждый тик симулятора.
        dt уже отмасштабирован TimeController (0.0 при паузе).
        Возвращает события, случившиеся за этот тик (см. конвенцию выше).
        """
        raise NotImplementedError

    @abstractmethod
    def on_event(self, event: Event) -> None:
        """Реакция на событие от другого узла/движка. Не должна тяжело считать —
        только выставлять флаги, которые учтёт следующий tick()."""
        raise NotImplementedError

    @abstractmethod
    def get_state(self) -> dict[str, Any]:
        """Начальные значения телеметрии узла для регистрации в StateStore."""
        raise NotImplementedError

    def apply_control(self, action: str, value: Any = None) -> None:
        """
        Точка входа для внешних управляющих команд.
        По умолчанию — не поддерживается; управляемые узлы переопределяют.
        """
        raise NotImplementedError(
            f"Узел {self.node_id} ({type(self).__name__}) не поддерживает control-действия"
        )


# ---------------------------------------------------------------------------
# Реестр ИНСТАНСОВ узлов (обязателен по ТЗ, п.6: "зарегистрировать в NodeRegistry")
# ---------------------------------------------------------------------------

class NodeRegistry:
    """Хранит живые инстансы узлов и синхронизирует их появление/удаление со StateStore."""

    def __init__(self, store: StateStore) -> None:
        self._store = store
        self._nodes: dict[str, BaseNode] = {}
        self._lock = threading.RLock()

    def register(self, node: BaseNode) -> None:
        with self._lock:
            if node.node_id in self._nodes:
                raise ValueError(f"Узел с id={node.node_id!r} уже зарегистрирован")
            self._nodes[node.node_id] = node
            self._store.register_node(node.node_id, node.get_state())

    def unregister(self, node_id: str) -> None:
        with self._lock:
            self._nodes.pop(node_id, None)
            self._store.remove_node(node_id)

    def get(self, node_id: str) -> BaseNode:
        with self._lock:
            try:
                return self._nodes[node_id]
            except KeyError:
                raise KeyError(f"Узел {node_id!r} не найден в NodeRegistry") from None

    def all_nodes(self) -> list[BaseNode]:
        with self._lock:
            return list(self._nodes.values())

    def node_ids(self) -> list[str]:
        with self._lock:
            return list(self._nodes.keys())

    def nodes_by_category(self, category: str) -> list[BaseNode]:
        with self._lock:
            return [n for n in self._nodes.values() if n.category == category]


# ---------------------------------------------------------------------------
# Реестр КЛАССОВ узлов — опциональная фабрика для массового создания из конфига
# ---------------------------------------------------------------------------

NODE_TYPES: dict[str, Type[BaseNode]] = {}


def register_node_type(type_name: str) -> Callable[[Type[BaseNode]], Type[BaseNode]]:
    """
    Декоратор для класса узла:

        @register_node_type("power_line")
        class PowerLine(BaseNode): ...

    Позволяет позже создавать десятки узлов из конфигурации:
        create_node("power_line", "line_09", capacity_kw=15.0)
    без изменения ядра — прямое следствие п.6 ТЗ ("Расширяемость").
    """
    def decorator(cls: Type[BaseNode]) -> Type[BaseNode]:
        if type_name in NODE_TYPES:
            raise ValueError(f"Тип узла {type_name!r} уже зарегистрирован")
        NODE_TYPES[type_name] = cls
        return cls
    return decorator


def create_node(type_name: str, node_id: str, **config: Any) -> BaseNode:
    """Фабрика инстанса узла по имени зарегистрированного типа."""
    try:
        cls = NODE_TYPES[type_name]
    except KeyError:
        raise KeyError(
            f"Неизвестный тип узла {type_name!r}. Доступно: {sorted(NODE_TYPES)}"
        ) from None
    return cls(node_id, **config)