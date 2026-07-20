"""
Финальный скрипт чтения данных с MUST PH1800 / Ph18Series по Modbus RTU.

Карта регистров расшифрована по образцу открытого проекта ha-must-inverter
(github.com/mukaschultze/ha-must-inverter, базовая карта для PV1800) и
подтверждена точным совпадением реальных показаний (Grid Hz 50.01,
ACCUM discharge/buy/load/self_use и т.д.) с экраном "Solar Power Monitor".

Регистры этого конкретного инвертора (Ph18Series) идут БЕЗ смещения
относительно базовой карты PV1800 - то есть номера регистров совпадают
1-в-1 с тем, что описано для PV1800.
"""

from pymodbus.client import ModbusSerialClient
import logging

PORT = "COM4"
BAUDRATE = 19200
SLAVE_ID = 4
TIMEOUT = 1

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# (адрес, имя, множитель, единица)
# multiplier=None означает "как есть, без масштабирования"
CHARGER_REGISTERS = [
    (15201, "ChargerWorkstate",        None,  ""),     # 0=init 1=selftest 2=work 3=stop
    (15202, "MpptState",               None,  ""),     # 0=stop 1=mppt 2=current_limiting
    (15203, "ChargingState",           None,  ""),     # 0=stop 1=absorb 2=float 3=equalization
    (15205, "PvVoltage",               0.1,   "V"),
    (15206, "BatteryVoltage(Charger)", 0.1,   "V"),
    (15207, "ChargerCurrent",          0.1,   "A"),
    (15208, "ChargerPower",            None,  "W"),
    (15209, "RadiatorTemperature",     None,  "°C"),
    (15210, "ExternalTemperature",     None,  "°C"),
    (15211, "BatteryRelay",            None,  "(0/1)"),
    (15212, "PvRelay",                 None,  "(0/1)"),
    (15215, "BattVolGrade",            None,  "V"),
    (15216, "RatedCurrent",            0.1,   "A"),
]

INVERTER_REGISTERS = [
    (25201, "WorkState",               None,  ""),
    (25202, "AcVoltageGrade",          None,  "V"),
    (25203, "RatedPower",              None,  "VA"),
    (25205, "InverterBatteryVoltage",  0.1,   "V"),
    (25206, "InverterVoltage",         0.1,   "V"),
    (25207, "GridVoltage",             0.1,   "V"),
    (25208, "BusVoltage",              0.1,   "V"),
    (25209, "ControlCurrent",          0.1,   "A"),
    (25210, "InverterCurrent",         0.1,   "A"),
    (25211, "GridCurrent",             0.1,   "A"),
    (25212, "LoadCurrent",             0.1,   "A"),
    (25213, "PInverter",               None,  "W"),
    (25214, "PGrid",                   None,  "W"),
    (25215, "PLoad",                   None,  "W"),
    (25216, "LoadPercent",             None,  "%"),
    (25225, "InverterFrequency",       0.01,  "Hz"),
    (25226, "GridFrequency",           0.01,  "Hz"),
    (25233, "AcRadiatorTemperature",   None,  "°C"),
    (25234, "TransformerTemperature",  None,  "°C"),
    (25235, "DcRadiatorTemperature",   None,  "°C"),
    (25273, "BattPower",               None,  "W"),
    (25274, "BattCurrent",             None,  "A"),
    (25277, "RatedPowerW",             None,  "W"),
]

# 32-битные счётчики (два регистра: старшее слово, младшее слово)
ACCUM_32BIT_REGISTERS = [
    (25245, "AccumulatedChargerPower",    0.1, "kWh"),
    (25247, "AccumulatedDischargerPower", 0.1, "kWh"),
    (25249, "AccumulatedBuyPower",        0.1, "kWh"),
    (25251, "AccumulatedSellPower",       0.1, "kWh"),
    (25253, "AccumulatedLoadPower",       0.1, "kWh"),
    (25255, "AccumulatedSelfUsePower",    0.1, "kWh"),
    (25257, "AccumulatedPvSellPower",     0.1, "kWh"),
    (25259, "AccumulatedGridChargerPower",0.1, "kWh"),
]


def read_single(client, addr, count):
    """Читает count регистров начиная с addr, возвращает список или None при ошибке."""
    try:
        response = client.read_holding_registers(address=addr, count=count, device_id=SLAVE_ID)
        if response is None or response.isError():
            return None
        return response.registers
    except Exception as e:
        logger.warning(f"Ошибка чтения {addr} (count={count}): {e}")
        return None


def print_table(client, title, registers):
    print(f"\n=== {title} ===")
    for addr, name, mult, unit in registers:
        regs = read_single(client, addr, 1)
        if regs is None:
            print(f"  {name} (REG_{addr}): ОШИБКА ЧТЕНИЯ")
            continue
        raw = regs[0]
        value = raw if mult is None else raw * mult
        print(f"  {name:28s} REG_{addr}: raw={raw:<8} -> {value} {unit}")


def print_accum(client, title, registers):
    print(f"\n=== {title} ===")
    for addr, name, mult, unit in registers:
        regs = read_single(client, addr, 2)
        if regs is None:
            print(f"  {name} (REG_{addr}-{addr+1}): ОШИБКА ЧТЕНИЯ")
            continue
        high, low = regs
        raw = (high << 16) | low
        value = raw * mult
        print(f"  {name:28s} REG_{addr}-{addr+1}: raw={raw:<8} -> {value:.1f} {unit}")


def main():
    client = ModbusSerialClient(
        port=PORT,
        baudrate=BAUDRATE,
        stopbits=1,
        bytesize=8,
        parity="N",
        timeout=TIMEOUT,
    )

    if not client.connect():
        logger.error("❌ Не удалось подключиться. Проверьте PORT/BAUDRATE/кабель.")
        return

    try:
        logger.info(f"✅ Подключено к {PORT} (Slave ID: {SLAVE_ID})")
        print_table(client, "Charger message", CHARGER_REGISTERS)
        print_table(client, "Inverter message", INVERTER_REGISTERS)
        print_accum(client, "Accumulated energy counters", ACCUM_32BIT_REGISTERS)
    finally:
        client.close()
        logger.info("🔌 Подключение закрыто")


if __name__ == "__main__":
    main()