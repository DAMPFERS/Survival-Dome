"""
Библиотека компонентов для генератора схем.

КЛЮЧЕВОЕ АРХИТЕКТУРНОЕ РЕШЕНИЕ: символы здесь — полностью самодостаточные
(своя собственная минималистичная графика), а НЕ копии официальных символов
KiCad (Device:R, power:GND и т.д.). Это осознанный компромисс:

  - Официальные символы могут отличаться между версиями KiCad, и без
    доступа к вашей реальной установке 9.0 нет гарантии побайтового
    совпадения → риск, что файл не откроется или будет неполным.
  - Наши собственные символы полностью контролируются этим кодом:
    мы точно знаем их геометрию и гарантируем, что она внутренне
    непротиворечива (координаты пинов согласованы с графикой).
    Выглядят проще штатных (просто прямоугольник для резистора и т.п.),
    но открываются надёжно.

Каждый тип компонента описан:
  - lib_id: идентификатор в (lib_id "...") схемы
  - sexpr: сырое S-expression определение символа (кладётся в lib_symbols)
  - pin_map: словарь "логическое имя пина, которое использует LLM" -> "номер пина в symbol"
  - ref_prefix: префикс позиционного обозначения (R, C, D, J, U, #PWR)
  - default_footprint: футпринт по умолчанию (можно переопределить в JSON от LLM)
"""

# --- Резистор -----------------------------------------------------------
_R_SEXPR = '''(symbol "AIGen:R" (in_bom yes) (on_board yes)
  (property "Reference" "R" (at 2.54 1.27 0) (effects (font (size 1.27 1.27))))
  (property "Value" "R" (at 2.54 -1.27 0) (effects (font (size 1.27 1.27))))
  (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
  (symbol "R_0_1"
    (rectangle (start -1.016 2.54) (end 1.016 -2.54)
      (stroke (width 0.254) (type default)) (fill (type none)))
  )
  (symbol "R_1_1"
    (pin passive line (at 0 3.81 270) (length 1.27)
      (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
    (pin passive line (at 0 -3.81 90) (length 1.27)
      (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
  )
)'''

# --- Конденсатор (неполярный) --------------------------------------------
_C_SEXPR = '''(symbol "AIGen:C" (in_bom yes) (on_board yes)
  (property "Reference" "C" (at 2.54 1.27 0) (effects (font (size 1.27 1.27))))
  (property "Value" "C" (at 2.54 -1.27 0) (effects (font (size 1.27 1.27))))
  (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
  (symbol "C_0_1"
    (polyline (pts (xy -1.524 0.508) (xy 1.524 0.508)) (stroke (width 0.3810) (type default)) (fill (type none)))
    (polyline (pts (xy -1.524 -0.508) (xy 1.524 -0.508)) (stroke (width 0.3810) (type default)) (fill (type none)))
  )
  (symbol "C_1_1"
    (pin passive line (at 0 2.54 270) (length 2.032)
      (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
    (pin passive line (at 0 -2.54 90) (length 2.032)
      (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
  )
)'''

# --- Диод (пин 1 = анод, пин 2 = катод) ----------------------------------
_D_SEXPR = '''(symbol "AIGen:D" (in_bom yes) (on_board yes)
  (property "Reference" "D" (at 2.54 1.27 0) (effects (font (size 1.27 1.27))))
  (property "Value" "D" (at 2.54 -1.27 0) (effects (font (size 1.27 1.27))))
  (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
  (symbol "D_0_1"
    (polyline (pts (xy -1.27 1.27) (xy -1.27 -1.27) (xy 1.27 0) (xy -1.27 1.27)) (stroke (width 0.254) (type default)) (fill (type outline)))
    (polyline (pts (xy 1.27 1.27) (xy 1.27 -1.27)) (stroke (width 0.254) (type default)) (fill (type none)))
  )
  (symbol "D_1_1"
    (pin passive line (at 0 2.54 270) (length 1.27)
      (name "A" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
    (pin passive line (at 0 -2.54 90) (length 1.27)
      (name "K" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
  )
)'''

# --- Разъём на 2 контакта -------------------------------------------------
_CONN2_SEXPR = '''(symbol "AIGen:CONN2" (in_bom yes) (on_board yes)
  (property "Reference" "J" (at 0 3.556 0) (effects (font (size 1.27 1.27))))
  (property "Value" "CONN2" (at 0 -3.556 0) (effects (font (size 1.27 1.27))))
  (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
  (symbol "CONN2_0_1"
    (rectangle (start -2.54 2.032) (end 2.54 -2.032) (stroke (width 0.254) (type default)) (fill (type background)))
  )
  (symbol "CONN2_1_1"
    (pin passive line (at 5.08 1.27 180) (length 2.54)
      (name "1" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
    (pin passive line (at 5.08 -1.27 180) (length 2.54)
      (name "2" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
  )
)'''

# --- Разъём на 3 контакта -------------------------------------------------
_CONN3_SEXPR = '''(symbol "AIGen:CONN3" (in_bom yes) (on_board yes)
  (property "Reference" "J" (at 0 4.826 0) (effects (font (size 1.27 1.27))))
  (property "Value" "CONN3" (at 0 -4.826 0) (effects (font (size 1.27 1.27))))
  (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
  (symbol "CONN3_0_1"
    (rectangle (start -2.54 3.302) (end 2.54 -3.302) (stroke (width 0.254) (type default)) (fill (type background)))
  )
  (symbol "CONN3_1_1"
    (pin passive line (at 5.08 2.54 180) (length 2.54)
      (name "1" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
    (pin passive line (at 5.08 0 180) (length 2.54)
      (name "2" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
    (pin passive line (at 5.08 -2.54 180) (length 2.54)
      (name "3" (effects (font (size 1.27 1.27)))) (number "3" (effects (font (size 1.27 1.27)))))
  )
)'''

# --- "Земля" (один пин сверху) -------------------------------------------
_GND_SEXPR = '''(symbol "AIGen:GND" (power) (pin_names (offset 0) hide) (in_bom yes) (on_board yes)
  (property "Reference" "#PWR" (at 0 -3.81 0) (effects (font (size 1.27 1.27)) hide))
  (property "Value" "GND" (at 0 -2.54 0) (effects (font (size 1.27 1.27))))
  (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
  (symbol "GND_0_1"
    (polyline (pts (xy 0 0) (xy 0 -1.27) (xy -1.27 -1.27) (xy 1.27 -1.27) (xy 0 0)) (stroke (width 0.254) (type default)) (fill (type none)))
  )
  (symbol "GND_1_1"
    (pin power_in line (at 0 0 270) (length 0)
      (name "GND" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
  )
)'''

# --- Обобщённый флаг питания (+5V/+12V/+3V3/VCC — текст задаётся value) --
_PWR_SEXPR = '''(symbol "AIGen:PWR_FLAG" (power) (pin_names (offset 0) hide) (in_bom yes) (on_board yes)
  (property "Reference" "#PWR" (at 0 3.81 0) (effects (font (size 1.27 1.27)) hide))
  (property "Value" "PWR" (at 0 2.286 0) (effects (font (size 1.27 1.27))))
  (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
  (symbol "PWR_FLAG_0_1"
    (polyline (pts (xy 0 0) (xy 0 1.27) (xy -0.635 0.635) (xy 0 1.27) (xy 0.635 0.635)) (stroke (width 0.254) (type default)) (fill (type none)))
  )
  (symbol "PWR_FLAG_1_1"
    (pin power_in line (at 0 0 90) (length 0)
      (name "PWR" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
  )
)'''


# type -> метаданные компонента
COMPONENT_TYPES = {
    "resistor": {
        "lib_id": "AIGen:R",
        "sexpr": _R_SEXPR,
        "ref_prefix": "R",
        "pin_map": {"1": "1", "2": "2"},
        "default_footprint": "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal",
    },
    "capacitor": {
        "lib_id": "AIGen:C",
        "sexpr": _C_SEXPR,
        "ref_prefix": "C",
        "pin_map": {"1": "1", "2": "2"},
        "default_footprint": "Capacitor_THT:C_Disc_D3.0mm_W1.6mm_P2.50mm",
    },
    "diode": {
        "lib_id": "AIGen:D",
        "sexpr": _D_SEXPR,
        "ref_prefix": "D",
        # A = анод (пин 1), K = катод (пин 2)
        "pin_map": {"A": "1", "K": "2"},
        "default_footprint": "Diode_THT:D_DO-35_SOD27_P7.62mm_Horizontal",
    },
    "connector2": {
        "lib_id": "AIGen:CONN2",
        "sexpr": _CONN2_SEXPR,
        "ref_prefix": "J",
        "pin_map": {"1": "1", "2": "2"},
        "default_footprint": "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
    },
    "connector3": {
        "lib_id": "AIGen:CONN3",
        "sexpr": _CONN3_SEXPR,
        "ref_prefix": "J",
        "pin_map": {"1": "1", "2": "2", "3": "3"},
        "default_footprint": "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical",
    },
    "gnd": {
        "lib_id": "AIGen:GND",
        "sexpr": _GND_SEXPR,
        "ref_prefix": "#PWR",
        "pin_map": {"GND": "1"},
        "default_footprint": "",
    },
    "power_flag": {
        "lib_id": "AIGen:PWR_FLAG",
        "sexpr": _PWR_SEXPR,
        "ref_prefix": "#PWR",
        "pin_map": {"PWR": "1"},
        "default_footprint": "",
    },
}

# Список допустимых значений "type" — используется в промпте для LLM
ALLOWED_COMPONENT_TYPES = list(COMPONENT_TYPES.keys())
