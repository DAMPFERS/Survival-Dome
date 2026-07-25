#!/usr/bin/env python3
"""
JSON Netlist → SPICE converter
Специально адаптирован под Netlist Viewer (требует .SUBCKT)
"""

import json
import re
from pathlib import Path

# ====================== НАСТРОЙКИ ПУТЕЙ ======================
INPUT_JSON   = "output/schematic_20260725_190540_aaf800.netlist.json"       # ← откуда читать JSON
OUTPUT_SPICE = "output/circuit.cir"        # ← куда писать SPICE
SUBCKT_NAME  = "circuit"            # имя subcircuit
# =============================================================


def normalize_net(name: str) -> str:
    if name.upper() in ("GND", "0", "GND1"):
        return "0"
    return name.upper()


def parse_voltage(value: str) -> str:
    m = re.search(r"([+-]?\d*\.?\d+)", value)
    return m.group(1) if m else "0"


def get_net(pin_to_net: dict, ref: str, pin: str, default: str = "0") -> str:
    return pin_to_net.get(f"{ref}.{pin}".upper(), 
                          pin_to_net.get(f"{ref}.{pin}", default))


def convert(netlist: dict) -> str:
    components = netlist.get("components", [])
    nets = netlist.get("nets", [])

    # pin → net (всё в верхнем регистре)
    pin_to_net = {}
    for net in nets:
        net_name = normalize_net(net["name"])
        for pin in net["pins"]:
            pin_to_net[pin.upper()] = net_name
            pin_to_net[pin] = net_name          # на всякий случай

    lines = []
    lines.append(f".SUBCKT {SUBCKT_NAME.upper()}")
    lines.append("")

    models_needed = set()

    for comp in components:
        ref   = comp["ref"].upper()
        typ   = comp["type"].lower()
        value = str(comp.get("value", "")).upper()

        if typ == "resistor":
            n1 = get_net(pin_to_net, ref, "1")
            n2 = get_net(pin_to_net, ref, "2")
            lines.append(f"{ref} {n1} {n2} {value}")

        elif typ == "capacitor":
            n1 = get_net(pin_to_net, ref, "1")
            n2 = get_net(pin_to_net, ref, "2")
            cref = ref if ref.startswith("C") else f"C{ref}"
            lines.append(f"{cref} {n1} {n2} {value}")

        elif typ == "inductor":
            n1 = get_net(pin_to_net, ref, "1")
            n2 = get_net(pin_to_net, ref, "2")
            lref = ref if ref.startswith("L") else f"L{ref}"
            lines.append(f"{lref} {n1} {n2} {value}")

        elif typ == "diode":
            n_a = get_net(pin_to_net, ref, "1", get_net(pin_to_net, ref, "A"))
            n_k = get_net(pin_to_net, ref, "2", get_net(pin_to_net, ref, "K"))
            model = value if value else "DDEFAULT"
            dref = ref if ref.startswith("D") else f"D{ref}"
            lines.append(f"{dref} {n_a} {n_k} {model}")
            models_needed.add(("diode", model))

        elif typ in ("transistor", "bjt", "npn", "pnp"):
            nc = get_net(pin_to_net, ref, "C", get_net(pin_to_net, ref, "1"))
            nb = get_net(pin_to_net, ref, "B", get_net(pin_to_net, ref, "2"))
            ne = get_net(pin_to_net, ref, "E", get_net(pin_to_net, ref, "3"))

            if typ == "pnp" or value == "PNP":
                model = "PDEFAULT"
                models_needed.add(("pnp", model))
            else:
                model = "NDEFAULT"
                models_needed.add(("npn", model))

            if value and value not in ("NPN", "PNP"):
                model = value

            qref = ref if ref.startswith("Q") else f"Q{ref}"
            lines.append(f"{qref} {nc} {nb} {ne} {model}")

        elif typ == "power_flag":
            net = get_net(pin_to_net, ref, "PWR", normalize_net(value))
            volt = parse_voltage(value)
            lines.append(f"V{ref} {net} 0 DC {volt}")

    # Модели
    if models_needed:
        lines.append("")
        for kind, name in sorted(models_needed):
            if kind == "diode":
                lines.append(f".MODEL {name} D")
            elif kind == "npn":
                lines.append(f".MODEL {name} NPN")
            elif kind == "pnp":
                lines.append(f".MODEL {name} PNP")

    lines.append("")
    lines.append(".ENDS")
    return "\n".join(lines)


def main():
    input_path  = Path(INPUT_JSON)
    output_path = Path(OUTPUT_SPICE)

    if not input_path.exists():
        raise FileNotFoundError(f"Не найден входной файл: {input_path}")

    data = json.loads(input_path.read_text(encoding="utf-8"))
    spice = convert(data)

    output_path.write_text(spice, encoding="utf-8")
    print(f"Готово → {output_path}")
    print("Файл обёрнут в .SUBCKT — должен открываться в Netlist Viewer")


if __name__ == "__main__":
    main()