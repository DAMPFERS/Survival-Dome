"""
Детерминированная сборка .kicad_sch из структурированного netlist (JSON),
полученного от LLM. Вся геометрия (позиции пинов, провода, метки) считается
кодом через простую тригонометрию — а не "на глаз" языковой моделью, что и
было главным источником ошибок в предыдущей версии.

Схема соединений через (global_label) вместо точной трассировки проводов
между произвольными компонентами: провод рисуется только коротким "хвостиком"
от каждого пина до метки с именем цепи. Все пины с одинаковым именем цепи
электрически объединяются в KiCad автоматически — не нужно совмещать
координаты проводов разных компонентов друг с другом.
"""

import math
import uuid as uuid_lib
from kiutils.schematic import Schematic
from kiutils.symbol import Symbol
from kiutils.utils.sexpr import parse_sexp
from kiutils.items.common import Position, Effects, Font
from kiutils.items.schitems import (
    SchematicSymbol,
    Property,
    Connection as Wire,
    GlobalLabel,
    SymbolInstance,
)

from components_library import COMPONENT_TYPES

GRID_STEP_X = 40.0
GRID_STEP_Y = 35.0
GRID_COLUMNS = 5
GRID_ORIGIN_X = 50.0
GRID_ORIGIN_Y = 50.0
STUB_LENGTH = 2.54


class NetlistError(Exception):
    """Ошибка в структуре/содержимом netlist, полученного от LLM."""


def _new_uuid() -> str:
    return str(uuid_lib.uuid4())


def _load_symbol(lib_id: str) -> Symbol:
    meta = None
    for m in COMPONENT_TYPES.values():
        if m["lib_id"] == lib_id:
            meta = m
            break
    if meta is None:
        raise NetlistError(f"Неизвестный lib_id: {lib_id}")
    return Symbol.from_sexpr(parse_sexp(meta["sexpr"]))


def _make_property(key: str, value: str, x: float, y: float, hide: bool = False) -> Property:
    prop = Property(key=key, value=value)
    prop.position = Position(X=x, Y=y, angle=0)
    prop.effects = Effects(font=Font(height=1.27, width=1.27), hide=hide)
    return prop


def _grid_position(index: int) -> tuple[float, float]:
    col = index % GRID_COLUMNS
    row = index // GRID_COLUMNS
    return (GRID_ORIGIN_X + col * GRID_STEP_X, GRID_ORIGIN_Y + row * GRID_STEP_Y)


def _pin_by_number(symbol: Symbol, pin_number: str):
    for unit in symbol.units:
        for pin in unit.pins:
            if pin.number == pin_number:
                return pin
    return None


def build_schematic(
    components: list[dict],
    nets: list[dict],
    project_name: str = "kicad_ai_project",
) -> Schematic:
    """
    Строит объект kiutils.Schematic из списка компонентов и списка цепей.

    components: [{"ref": "R1", "type": "resistor", "value": "240",
                  "footprint": "..." (опционально)}, ...]
    nets: [{"name": "VOUT", "pins": ["U1.OUT", "R1.1", "J2.1"]}, ...]

    Raises:
        NetlistError: при неизвестном типе компонента, дублирующемся
            reference, ссылке на несуществующий компонент/пин.
    """
    schematic = Schematic(version="20250114", generator="eeschema")
    schematic.uuid = _new_uuid()

    if not components:
        raise NetlistError("Список компонентов пуст.")

    # --- 1. Валидация и размещение компонентов --------------------------
    placed = {}  # ref -> {"position": (x,y), "type": str, "symbol": Symbol}
    seen_refs = set()

    for idx, comp in enumerate(components):
        ref = comp.get("ref")
        comp_type = comp.get("type")
        value = comp.get("value", "")

        if not ref:
            raise NetlistError(f"Компонент №{idx} не содержит поле 'ref'.")
        if ref in seen_refs:
            raise NetlistError(f"Повторяющийся reference: {ref}")
        seen_refs.add(ref)

        if comp_type not in COMPONENT_TYPES:
            allowed = ", ".join(COMPONENT_TYPES.keys())
            raise NetlistError(
                f"Компонент {ref}: неизвестный type '{comp_type}'. "
                f"Допустимые значения: {allowed}"
            )

        meta = COMPONENT_TYPES[comp_type]
        symbol = _load_symbol(meta["lib_id"])
        x, y = _grid_position(idx)
        footprint = comp.get("footprint") or meta["default_footprint"]

        sch_symbol = SchematicSymbol()
        sch_symbol.libId = meta["lib_id"]
        sch_symbol.position = Position(X=x, Y=y, angle=0)
        sch_symbol.unit = 1
        sch_symbol.inBom = True
        sch_symbol.onBoard = True
        sch_symbol.uuid = _new_uuid()
        sch_symbol.properties = [
            _make_property("Reference", ref, x + 2.54, y - 3.81),
            _make_property("Value", str(value), x + 2.54, y - 1.27),
            _make_property("Footprint", footprint, x, y, hide=True),
        ]
        # UUID на каждый физический пин экземпляра (требование формата)
        all_pin_numbers = set()
        for unit in symbol.units:
            for pin in unit.pins:
                all_pin_numbers.add(pin.number)
        sch_symbol.pins = {num: _new_uuid() for num in all_pin_numbers}

        schematic.schematicSymbols.append(sch_symbol)

        placed[ref] = {"position": (x, y), "type": comp_type, "symbol": symbol}

        # Добавляем определение символа в lib_symbols один раз на тип
        if not any(s.libId == meta["lib_id"] for s in schematic.libSymbols):
            schematic.libSymbols.append(_load_symbol(meta["lib_id"]))

    # --- 2. Провода-хвостики + глобальные метки для каждой цепи ---------
    if not nets:
        raise NetlistError("Список цепей (nets) пуст — компоненты ничем не соединены.")

    for net in nets:
        net_name = net.get("name")
        pin_refs = net.get("pins", [])
        if not net_name:
            raise NetlistError("У цепи отсутствует поле 'name'.")
        if len(pin_refs) < 1:
            raise NetlistError(f"Цепь '{net_name}' не содержит ни одного пина.")

        for pin_ref in pin_refs:
            if "." not in pin_ref:
                raise NetlistError(
                    f"Некорректная ссылка на пин '{pin_ref}' в цепи '{net_name}'. "
                    f"Ожидается формат REF.PIN, например 'R1.1'."
                )
            comp_ref, pin_logical = pin_ref.split(".", 1)

            if comp_ref not in placed:
                raise NetlistError(
                    f"Цепь '{net_name}' ссылается на несуществующий компонент '{comp_ref}'."
                )

            info = placed[comp_ref]
            comp_type = info["type"]
            pin_map = COMPONENT_TYPES[comp_type]["pin_map"]
            if pin_logical not in pin_map:
                allowed = ", ".join(pin_map.keys())
                raise NetlistError(
                    f"Цепь '{net_name}': у компонента {comp_ref} (тип {comp_type}) "
                    f"нет пина '{pin_logical}'. Допустимые пины: {allowed}"
                )
            pin_number = pin_map[pin_logical]

            pin = _pin_by_number(info["symbol"], pin_number)
            if pin is None:
                raise NetlistError(
                    f"Внутренняя ошибка: пин {pin_number} не найден в символе {comp_type}."
                )

            cx, cy = info["position"]
            px = cx + pin.position.X
            py = cy + pin.position.Y
            angle = pin.position.angle or 0
            # Направление НАРУЖУ от корпуса (противоположно направлению
            # рисования пина внутрь символа)
            out_rad = math.radians(angle + 180)
            stub_x = px + STUB_LENGTH * math.cos(out_rad)
            stub_y = py + STUB_LENGTH * math.sin(out_rad)

            wire = Wire(type="wire")
            wire.points = [Position(X=px, Y=py), Position(X=stub_x, Y=stub_y)]
            wire.uuid = _new_uuid()
            schematic.graphicalItems.append(wire)

            label = GlobalLabel()
            label.text = net_name
            label.shape = "passive"
            label.position = Position(X=stub_x, Y=stub_y, angle=angle % 360)
            label.effects = Effects(font=Font(height=1.27, width=1.27))
            label.uuid = _new_uuid()
            schematic.globalLabels.append(label)

    # --- 3. symbol_instances (обязательная секция формата) --------------
    for sch_symbol in schematic.schematicSymbols:
        ref_prop = next((p for p in sch_symbol.properties if p.key == "Reference"), None)
        val_prop = next((p for p in sch_symbol.properties if p.key == "Value"), None)
        fp_prop = next((p for p in sch_symbol.properties if p.key == "Footprint"), None)
        schematic.symbolInstances.append(
            SymbolInstance(
                path="/",
                reference=ref_prop.value if ref_prop else "",
                unit=1,
                value=val_prop.value if val_prop else "",
                footprint=fp_prop.value if fp_prop else "",
            )
        )

    return schematic


def schematic_to_kicad9_text(schematic: Schematic) -> str:
    """
    Сериализует Schematic в текст и дописывает то, что установленная версия
    kiutils (1.4.8) не выводит сама, но что реально присутствует в файлах
    KiCad 9 (сверено с пустым файлом, сохранённым из настоящего KiCad 9):
      - (generator_version "9.0") сразу после (generator "eeschema")
      - гарантия, что (symbol_instances ...) присутствует, даже если он
        оказался бы пустым (в нашем случае он не пустой, но проверяем
        на всякий случай для устойчивости)
    """
    text = schematic.to_sexpr()

    if "generator_version" not in text:
        text = text.replace(
            '(generator "eeschema")',
            '(generator "eeschema") (generator_version "9.0")',
            1,
        )
        # kiutils в этой версии не всегда кавычит generator — подстрахуемся
        text = text.replace(
            "(generator eeschema)",
            '(generator "eeschema") (generator_version "9.0")',
            1,
        )

    if "(symbol_instances" not in text:
        # Вставляем пустую секцию перед последней закрывающей скобкой файла
        text = text.rstrip()
        assert text.endswith(")")
        text = text[:-1] + "  (symbol_instances)\n)"

    return text
