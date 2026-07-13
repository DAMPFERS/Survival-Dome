from pymodbus.client import ModbusSerialClient
import logging

# === НАСТРОЙКИ (измените под себя) ===
PORT = "COM16"          # COM-порт (Windows) или "/dev/ttyUSB0" (Linux)
BAUDRATE = 19200       # Скорость (проверьте в документации инвертора)
SLAVE_ID = 4          # Modbus ID инвертора (обычно 1)
TIMEOUT = 2           # Таймаут подключения (секунды)

# Диапазоны регистров для сканирования (из вашего кода)
REGISTER_RANGES = [
    (25269, 25274),    # Основной диапазон (статусы, RatedPowerW)
    (15201, 15217),
    (15218, 15342)

]

# Настройка логгирования
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

def main():
    # Инициализация Modbus RTU клиента
    client = ModbusSerialClient(
        # method="rtu",
        port=PORT,
        baudrate=BAUDRATE,
        stopbits=1,
        bytesize=8,
        parity="N",
        timeout=TIMEOUT,
    )

    if not client.connect():
        logger.error("❌ Не удалось подключиться к инвертору. Проверьте PORT, BAUDRATE или подключение кабеля.")
        return

    try:
        logger.info(f"✅ Подключено к {PORT} (Slave ID: {SLAVE_ID})")

        for start, end in REGISTER_RANGES:
            count = end - start + 1
            logger.info(f"Сканирую диапазон {start}-{end}...")

            response = client.read_holding_registers(
                address=start,
                count=count,
                device_id=SLAVE_ID
            )
            # response = client.read_input_registers(
            #     address=start,
            #     count=count,
            #     device_id=SLAVE_ID
            # )

            if response.isError():
                logger.error(f"Ошибка чтения диапазона {start}-{end}: {response}")
                continue

            # Вывод ненулевых значений
            for i, value in enumerate(response.registers):
                reg_address = start + i
                if value != 0:
                    print(f"REG_{reg_address}: {value}")

    finally:
        client.close()
        logger.info("🔌 Подключение закрыто")

if __name__ == "__main__":
    main()