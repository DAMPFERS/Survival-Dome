"""
Финальный скрипт чтения данных с MUST PH1800 / Ph18Series по Modbus RTU.
Исправлена проблема со знаковыми значениями (мощность, ток).
"""
from pymodbus.client import ModbusSerialClient
import logging
import struct

PORT = "COM3"  
BAUDRATE = 19200
SLAVE_ID = 4
TIMEOUT = 1

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

def to_signed(value):
    """Преобразует 16-битное беззнаковое значение в знаковое (int16)."""
    if value > 32767:
        return value - 65536
    return value

# (адрес, имя, множитель, единица, signed)
# signed=True означает, что нужно применить to_signed() перед умножением
CHARGER_REGISTERS = [
    (15201, "ChargerWorkstate", None, " ", False),
    (15202, "MpptState", None, " ", False),
    (15203, "ChargingState", None, " ", False),
    (15205, "PvVoltage", 0.1, "V", False),
    (15206, "BatteryVoltage_Charger", 0.1, "V", False),
    (15207, "ChargerCurrent", 0.1, "A", True),      # Ток может быть отрицательным
    (15208, "ChargerPower", None, "W", True),       # Мощность заряда
    (15209, "RadiatorTemperature", None, "°C", False),
    (15210, "ExternalTemperature", None, "°C", False),
    (15211, "BatteryRelay", None, "(0/1)", False),
    (15212, "PvRelay", None, "(0/1)", False),
    (15215, "BattVolGrade", None, "V", False),
    (15216, "RatedCurrent", 0.1, "A", False),
]

INVERTER_REGISTERS = [
    (25201, "WorkState", None, " ", False),
    (25202, "AcVoltageGrade", None, "V", False),
    (25203, "RatedPower", None, "VA", False),
    (25205, "InverterBatteryVoltage", 0.1, "V", False),
    (25206, "InverterVoltage", 0.1, "V", False),
    (25207, "GridVoltage", 0.1, "V", False),
    (25208, "BusVoltage", 0.1, "V", False),
    (25209, "ControlCurrent", 0.1, "A", True),     # Знаковый
    (25210, "InverterCurrent", 0.1, "A", True),     # Знаковый
    (25211, "GridCurrent", 0.1, "A", True),         # Знаковый
    (25212, "LoadCurrent", 0.1, "A", True),         # Знаковый
    (25213, "PInverter", None, "W", True),          # Знаковая мощность!
    (25214, "PGrid", None, "W", True),              # Знаковая мощность!
    (25215, "PLoad", None, "W", True),              # Знаковая мощность!
    (25216, "LoadPercent", None, "%", False),
    (25225, "InverterFrequency", 0.01, "Hz", False),
    (25226, "GridFrequency", 0.01, "Hz", False),
    (25233, "AcRadiatorTemperature", None, "°C", False),
    (25234, "TransformerTemperature", None, "°C", False),
    (25235, "DcRadiatorTemperature", None, "°C", False),
    (25273, "BattPower", None, "W", True),          # Знаковая мощность батареи!
    (25274, "BattCurrent", None, "A", True),        # Знаковый ток батареи!
    (25277, "RatedPowerW", None, "W", False),
]

ACCUM_32BIT_REGISTERS = [
    (25245, "AccumulatedChargerPower", 0.1, "kWh"),
    (25247, "AccumulatedDischargerPower", 0.1, "kWh"),
    (25249, "AccumulatedBuyPower", 0.1, "kWh"),
    (25251, "AccumulatedSellPower", 0.1, "kWh"),
    (25253, "AccumulatedLoadPower", 0.1, "kWh"),
    (25255, "AccumulatedSelfUsePower", 0.1, "kWh"),
    (25257, "AccumulatedPvSellPower", 0.1, "kWh"),
    (25259, "AccumulatedGridChargerPower", 0.1, "kWh"),
]

def read_single(client, addr, count):
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
    for item in registers:
        addr, name, mult, unit, signed = item
        regs = read_single(client, addr, 1)
        if regs is None:
            print(f"  {name:30s} REG_{addr}: ОШИБКА ЧТЕНИЯ")
            continue
        
        raw = regs[0]
        
        # Применяем преобразование знака, если необходимо
        if signed:
            value = to_signed(raw)
        else:
            value = raw
            
        # Применяем множитель
        if mult is not None:
            value = value * mult
            
        print(f"  {name:30s} REG_{addr}: raw={raw:<8} -> {value} {unit}")

def print_accum(client, title, registers):
    print(f"\n=== {title} ===")
    for addr, name, mult, unit in registers:
        regs = read_single(client, addr, 2)
        if regs is None:
            print(f"  {name:30s} REG_{addr}-{addr+1}: ОШИБКА ЧТЕНИЯ")
            continue
        high, low = regs
        # Для 32-битных счетчиков обычно используется unsigned long
        raw = (high << 16) | low
        value = raw * mult
        print(f"  {name:30s} REG_{addr}-{addr+1}: raw={raw:<8} -> {value:.1f} {unit}")

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
        logger.info(" Подключение закрыто")

if __name__ == "__main__":
    main()