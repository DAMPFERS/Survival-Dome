"""
Сканер регистров Modbus для поиска Grid Voltage (и других "живых" AC-параметров)
на инверторе MUST PH1800 / Ph18Series.

Идея: Grid voltage на экране показан ~240.2V СЕРЫМ ЦВЕТОМ - то есть это "всегда живое"
значение, которое не обнуляется в режиме SelfTest. Значит его можно поймать
прямо сейчас, не создавая нагрузку - нужно просто найти правильный адрес.

Скрипт:
1. Читает регистры широкими диапазонами (безопасными кусками по CHUNK_SIZE),
   продолжая работу даже если какой-то диапазон вернул ошибку (нет смысла падать
   из-за одного "пустого" куска).
2. Для каждого ненулевого регистра проверяет, похоже ли значение на переменное
   напряжение сети 200-260В при разных вероятных масштабах (x1, x0.1, x0.01),
   и отдельно помечает кандидатов на "частоту сети" (45-65 Гц) и "ток" (0-100А).
3. В конце печатает отсортированный список кандидатов по правдоподобности.

Как использовать:
- Просто запустите как есть - разница с прошлым скриптом только в том, что
  сканируются гораздо более широкие диапазоны и есть автоматическое распознавание.
- Если хотите поймать и другие "живые под нагрузкой" параметры (PGrid, PLoad,
  BattCurrent, InverterCurrent) - включите инвертор в реальную работу
  (не SelfTest), пока скрипт работает.
"""

from pymodbus.client import ModbusSerialClient
import logging
import time

# === НАСТРОЙКИ ===
PORT = "COM16"
BAUDRATE = 19200
SLAVE_ID = 4
TIMEOUT = 1

# Насколько большими кусками читаем регистры за один запрос.
# Слишком большой кусок может не поместиться в один Modbus-ответ (max ~125 регистров
# для функции 03), слишком маленький - будет долго. 60 - безопасный компромисс.
CHUNK_SIZE = 60

# Широкие диапазоны для сканирования - основано на том, что уже подтверждено
# рабочим (15xxx и 25xxx), плюс соседние "вероятные" блоки (10xxx, 16xxx, 20xxx, 30xxx).
# Можно смело добавлять/убирать диапазоны.
SCAN_RANGES = [
    (10000, 10300),
    (15201, 15450),
    (16000, 16300),
    (20000, 20300),
    (25200, 25600),   # расширенный блок "Inverter Display Messages"
    (30000, 30300),
]

# Пороговые значения-кандидаты для распознавания "похоже на сетевое напряжение"
VOLTAGE_RAW_MIN, VOLTAGE_RAW_MAX = 180, 280          # если регистр хранит вольты как есть
VOLTAGE_X10_MIN, VOLTAGE_X10_MAX = 1800, 2800        # если множитель 0.1 (типично для MUST)
VOLTAGE_X100_MIN, VOLTAGE_X100_MAX = 18000, 28000    # если множитель 0.01

FREQ_X100_MIN, FREQ_X100_MAX = 4500, 6500            # частота сети x0.01 (45.00-65.00 Гц)

CURRENT_X10_MAX = 1000                                # ток x0.1 (0-100A) - просто для пометки

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def classify(value):
    """Возвращает список подсказок о том, чем может быть это значение."""
    hints = []
    if VOLTAGE_RAW_MIN <= value <= VOLTAGE_RAW_MAX:
        hints.append(f"~напряжение как есть: {value} В")
    if VOLTAGE_X10_MIN <= value <= VOLTAGE_X10_MAX:
        hints.append(f"~напряжение x0.1: {value/10:.1f} В  <-- ПОХОЖЕ НА GRID VOLTAGE (240.2V)")
    if VOLTAGE_X100_MIN <= value <= VOLTAGE_X100_MAX:
        hints.append(f"~напряжение x0.01: {value/100:.2f} В")
    if FREQ_X100_MIN <= value <= FREQ_X100_MAX:
        hints.append(f"~частота x0.01: {value/100:.2f} Гц")
    if 0 < value <= CURRENT_X10_MAX:
        hints.append(f"~ток x0.1: {value/10:.1f} А (если это ток)")
    return hints


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

    all_nonzero = {}       # address -> value
    candidates = []        # (address, value, [hints])

    try:
        logger.info(f"✅ Подключено к {PORT} (Slave ID: {SLAVE_ID})")

        for range_start, range_end in SCAN_RANGES:
            addr = range_start
            while addr < range_end:
                count = min(CHUNK_SIZE, range_end - addr)
                try:
                    response = client.read_holding_registers(
                        address=addr,
                        count=count,
                        device_id=SLAVE_ID,
                    )
                except Exception as e:
                    logger.warning(f"Исключение при чтении {addr}-{addr+count-1}: {e}")
                    addr += count
                    time.sleep(0.05)
                    continue

                if response is None or response.isError():
                    # Не падаем - просто пропускаем этот кусок и едем дальше
                    logger.info(f"  (диапазон {addr}-{addr+count-1} недоступен, пропускаю)")
                    addr += count
                    time.sleep(0.05)
                    continue

                for i, value in enumerate(response.registers):
                    reg_address = addr + i
                    if value != 0:
                        all_nonzero[reg_address] = value
                        hints = classify(value)
                        if hints:
                            candidates.append((reg_address, value, hints))

                addr += count
                time.sleep(0.02)  # небольшая пауза, чтобы не заваливать шину запросами

        # --- Вывод результатов ---
        logger.info(f"Всего найдено ненулевых регистров: {len(all_nonzero)}")

        print("\n=== ВСЕ НЕНУЛЕВЫЕ РЕГИСТРЫ ===")
        for addr in sorted(all_nonzero):
            print(f"REG_{addr}: {all_nonzero[addr]}")

        print("\n=== КАНДИДАТЫ (похожи на напряжение/частоту/ток сети) ===")
        if not candidates:
            print("Ничего не найдено в просканированных диапазонах.")
            print("Попробуйте расширить SCAN_RANGES или включить инвертор в реальную работу.")
        else:
            for addr, value, hints in sorted(candidates, key=lambda x: x[0]):
                print(f"REG_{addr} = {value}")
                for h in hints:
                    print(f"    -> {h}")

    finally:
        client.close()
        logger.info("🔌 Подключение закрыто")


if __name__ == "__main__":
    main()
