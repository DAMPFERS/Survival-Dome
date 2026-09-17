import time

from klipper_printer import KlipperPrinter


PRINTER_IP = "192.168.3.12"


def main():
    printer = KlipperPrinter(
        host=PRINTER_IP,
        poll_interval=1.0,
        request_timeout=3.0,
    )

    printer.start()

    try:
        while True:
            status = printer.get_status()

            print(
                f"В сети: {status.online}"
            )

            print(
                f"Klipper: {status.klippy_state}"
            )

            print(
                f"Печать: {status.printing}"
            )

            print(
                f"Пауза: {status.paused}"
            )

            print(
                f"Состояние: {status.print_state}"
            )

            print(
                f"Прогресс: {status.progress:.1%}"
            )

            print(
                f"Файл: {status.filename}"
            )

            print(
                f"Сопло: "
                f"{status.extruder_temperature} °C "
                f"(цель: {status.extruder_target} °C)"
            )

            print(
                f"Стол: "
                f"{status.bed_temperature} °C "
                f"(цель: {status.bed_target} °C)"
            )

            if status.error:
                print(
                    f"Ошибка: {status.error}"
                )

            print("-" * 50)

            time.sleep(2)

    except KeyboardInterrupt:
        print("\nОстановка...")

    finally:
        printer.stop()


if __name__ == "__main__":
    main()
    
    
    # printer.home()
    # printer.pause()
    # printer.resume()
    # printer.cancel_print()

    # printer.stop()