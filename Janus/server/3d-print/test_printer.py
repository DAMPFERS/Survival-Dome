import time

from klipper_printer import KlipperPrinter


PRINTER_IP = "192.168.8.69"


def test_monitoring():
    """Тест мониторинга статуса принтера."""
    print("=" * 60)
    print("ТЕСТ 1: Мониторинг статуса принтера")
    print("=" * 60)
    
    printer = KlipperPrinter(
        host=PRINTER_IP,
        poll_interval=1.0,
        request_timeout=3.0,
    )

    printer.start()

    try:
        for i in range(5):
            status = printer.get_status()

            print(f"\n--- Обновление {i+1}/5 ---")
            print(f"В сети: {status.online}")
            print(f"Klipper: {status.klippy_state}")
            print(f"Печать: {status.printing}")
            print(f"Пауза: {status.paused}")
            print(f"Состояние: {status.print_state}")
            print(f"Прогресс: {status.progress:.1%}")
            print(f"Файл: {status.filename}")
            
            print(f"Сопло: {status.extruder_temperature} °C "
                  f"(цель: {status.extruder_target} °C)")
            
            print(f"Стол: {status.bed_temperature} °C "
                  f"(цель: {status.bed_target} °C)")
            
            # Новые поля
            print(f"Хоуминг: {status.homing}")
            print(f"Статус toolhead: {status.toolhead_status}")
            print(f"Позиция: X={status.position_x}, Y={status.position_y}, "
                  f"Z={status.position_z}, E={status.position_e}")
            print(f"Скорость печати: {status.speed_factor:.0%}")

            if status.error:
                print(f"Ошибка: {status.error}")

            time.sleep(2)

    except KeyboardInterrupt:
        print("\nОстановка...")

    finally:
        printer.stop()


def test_homing():
    """Тест команды home() с отслеживанием процесса."""
    print("\n" + "=" * 60)
    print("ТЕСТ 2: Команда HOME с отслеживанием")
    print("=" * 60)
    
    printer = KlipperPrinter(
        host=PRINTER_IP,
        poll_interval=0.5,  # Чаще опрашивать для теста
        request_timeout=3.0,
    )

    printer.start()
    
    try:
        # Ждем подключения
        print("\nОжидание подключения к принтеру...")
        for _ in range(10):
            status = printer.get_status()
            if status.online and status.klippy_connected:
                print(f"✓ Принтер подключен (состояние: {status.klippy_state})")
                break
            time.sleep(1)
        else:
            print("✗ Не удалось подключиться к принтеру")
            return
        
        # Проверяем готовность
        status = printer.get_status()
        if status.klippy_state != "ready":
            print(f"✗ Принтер не готов: {status.klippy_state}")
            return
        
        print("\nНачальная позиция:")
        print(f"  X={status.position_x}, Y={status.position_y}, "
              f"Z={status.position_z}")
        
        # Отправляем команду home
        print("\nОтправка команды G28 (home всех осей)...")
        success = printer.home()
        
        if not success:
            print("✗ Не удалось отправить команду home")
            return
        
        print("✓ Команда отправлена, отслеживание процесса...")
        
        # Отслеживаем процесс хоуминга
        homing_detected = False
        start_time = time.time()
        
        while time.time() - start_time < 60:  # Максимум 60 секунд
            status = printer.get_status()
            
            if status.homing and not homing_detected:
                homing_detected = True
                print(f"\n⟳ Хоуминг начался (статус: {status.toolhead_status})")
            
            if homing_detected and not status.homing:
                print(f"✓ Хоуминг завершен (статус: {status.toolhead_status})")
                print(f"\nКонечная позиция:")
                print(f"  X={status.position_x}, Y={status.position_y}, "
                      f"Z={status.position_z}")
                break
            
            if status.homing:
                print(".", end="", flush=True)
            
            time.sleep(0.5)
        else:
            print("\n⚠ Истекло время ожидания завершения хоуминга")
        
    except KeyboardInterrupt:
        print("\nОстановка...")
    
    finally:
        printer.stop()


def test_list_files():
    """Тест получения списка файлов."""
    print("\n" + "=" * 60)
    print("ТЕСТ 3: Список файлов на принтере")
    print("=" * 60)
    
    printer = KlipperPrinter(
        host=PRINTER_IP,
        poll_interval=1.0,
        request_timeout=3.0,
    )

    printer.start()
    
    try:
        # Ждем подключения
        print("\nОжидание подключения к принтеру...")
        for _ in range(10):
            status = printer.get_status()
            if status.online:
                print("✓ Принтер подключен")
                break
            time.sleep(1)
        else:
            print("✗ Не удалось подключиться к принтеру")
            return
        
        # Получаем список файлов
        print("\nПолучение списка файлов...")
        files = printer.list_files()
        
        if files is None:
            print("✗ Ошибка получения списка файлов")
            return
        
        print(f"✓ Найдено файлов: {len(files)}")
        print("\nПервые 10 файлов:")
        print("-" * 60)
        
        for i, file_info in enumerate(files[:10], 1):
            path = file_info.get("path", "?")
            size = file_info.get("size", 0)
            size_mb = size / (1024 * 1024)
            print(f"{i}. {path}")
            print(f"   Размер: {size_mb:.2f} MB")
        
        if len(files) > 10:
            print(f"\n... и еще {len(files) - 10} файлов")
        
    except KeyboardInterrupt:
        print("\nОстановка...")
    
    finally:
        printer.stop()


def test_file_metadata():
    """Тест получения метаданных файла."""
    print("\n" + "=" * 60)
    print("ТЕСТ 4: Метаданные файла")
    print("=" * 60)
    
    printer = KlipperPrinter(
        host=PRINTER_IP,
        poll_interval=1.0,
        request_timeout=3.0,
    )

    printer.start()
    
    try:
        # Ждем подключения
        print("\nОжидание подключения к принтеру...")
        for _ in range(10):
            status = printer.get_status()
            if status.online:
                print("✓ Принтер подключен")
                break
            time.sleep(1)
        else:
            print("✗ Не удалось подключиться к принтеру")
            return
        
        # Получаем список файлов
        print("\nПолучение списка файлов...")
        files = printer.list_files()
        
        if not files:
            print("✗ Нет доступных файлов")
            return
        
        # Берем первый файл для теста
        test_file = files[0]["path"]
        print(f"\nТестовый файл: {test_file}")
        
        # Получаем метаданные
        print("\nПолучение метаданных...")
        metadata = printer.get_file_metadata(test_file)
        
        if metadata is None:
            print("✗ Ошибка получения метаданных")
            return
        
        print("✓ Метаданные получены:")
        print("-" * 60)
        
        # Основные параметры
        if "slicer" in metadata:
            print(f"Слайсер: {metadata['slicer']}")
        if "slicer_version" in metadata:
            print(f"Версия: {metadata['slicer_version']}")
        
        if "estimated_time" in metadata:
            est_time = metadata["estimated_time"]
            hours = int(est_time // 3600)
            minutes = int((est_time % 3600) // 60)
            print(f"Время печати: {hours}ч {minutes}м")
        
        if "filament_total" in metadata:
            filament_mm = metadata["filament_total"]
            filament_m = filament_mm / 1000
            print(f"Расход пластика: {filament_m:.2f} м")
        
        if "filament_weight_total" in metadata:
            print(f"Вес пластика: {metadata['filament_weight_total']:.2f} г")
        
        if "layer_count" in metadata:
            print(f"Количество слоев: {metadata['layer_count']}")
        
        if "layer_height" in metadata:
            print(f"Высота слоя: {metadata['layer_height']} мм")
        
        if "first_layer_extr_temp" in metadata:
            print(f"Температура сопла: {metadata['first_layer_extr_temp']}°C")
        
        if "first_layer_bed_temp" in metadata:
            print(f"Температура стола: {metadata['first_layer_bed_temp']}°C")
        
    except KeyboardInterrupt:
        print("\nОстановка...")
    
    finally:
        printer.stop()


def B_PCB1_file():
    """Тест загрузки файла."""
    print("\n" + "=" * 60)
    print("ТЕСТ 5: Загрузка файла на принтер")
    print("=" * 60)
    
    printer = KlipperPrinter(
        host=PRINTER_IP,
        poll_interval=1.0,
        request_timeout=3.0,
    )

    printer.start()
    
    try:
        # Ждем подключения
        print("\nОжидание подключения к принтеру...")
        for _ in range(10):
            status = printer.get_status()
            if status.online:
                print("✓ Принтер подключен")
                break
            time.sleep(1)
        else:
            print("✗ Не удалось подключиться к принтеру")
            return
        
        # Путь к тестовому файлу
        test_file = "gcode/B_PCB1.gcode"
        
        print(f"\nЗагрузка файла: {test_file}")
        print("Ожидание... (это может занять некоторое время)")
        
        success = printer.upload_file(test_file, filename="B_PCB1.gcode")
        
        if success:
            print("✓ Файл успешно загружен на принтер как 'B_PCB1.gcode'")
        else:
            print("✗ Ошибка загрузки файла")
            status = printer.get_status()
            if status.error:
                print(f"   {status.error}")
        
    except KeyboardInterrupt:
        print("\nОстановка...")
    
    finally:
        printer.stop()


def test_delete_file():
    """Тест удаления файла."""
    print("\n" + "=" * 60)
    print("ТЕСТ 6: Удаление файла с принтера")
    print("=" * 60)
    
    printer = KlipperPrinter(
        host=PRINTER_IP,
        poll_interval=1.0,
        request_timeout=3.0,
    )

    printer.start()
    
    try:
        # Ждем подключения
        print("\nОжидание подключения к принтеру...")
        for _ in range(10):
            status = printer.get_status()
            if status.online:
                print("✓ Принтер подключен")
                break
            time.sleep(1)
        else:
            print("✗ Не удалось подключиться к принтеру")
            return
        
        # Удаляем тестовый файл
        test_file = "B_PCB1.gcode"
        
        print(f"\nУдаление файла: {test_file}")
        
        success = printer.delete_file(test_file)
        
        if success:
            print("✓ Файл успешно удален")
        else:
            print("✗ Ошибка удаления файла")
            status = printer.get_status()
            if status.error:
                print(f"   {status.error}")
        
    except KeyboardInterrupt:
        print("\nОстановка...")
    
    finally:
        printer.stop()


def test_start_print():
    """Тест запуска печати (НЕ ЗАПУСКАЕТ РЕАЛЬНО!)."""
    print("\n" + "=" * 60)
    print("ТЕСТ 7: Проверка возможности запуска печати")
    print("=" * 60)
    print("\n⚠ ВНИМАНИЕ: Этот тест только проверяет условия,")
    print("   но НЕ запускает печать реально!")
    
    printer = KlipperPrinter(
        host=PRINTER_IP,
        poll_interval=1.0,
        request_timeout=3.0,
    )

    printer.start()
    
    try:
        # Ждем подключения
        print("\nОжидание подключения к принтеру...")
        for _ in range(10):
            status = printer.get_status()
            if status.online:
                print("✓ Принтер подключен")
                break
            time.sleep(1)
        else:
            print("✗ Не удалось подключиться к принтеру")
            return
        
        # Проверяем состояние
        status = printer.get_status()
        
        print("\nПроверка готовности принтера:")
        print(f"  В сети: {status.online}")
        print(f"  Klipper подключен: {status.klippy_connected}")
        print(f"  Состояние Klipper: {status.klippy_state}")
        print(f"  Печатает: {status.printing}")
        
        if not status.online:
            print("\n✗ Принтер не в сети")
        elif not status.klippy_connected:
            print("\n✗ Klipper не подключен")
        elif status.klippy_state != "ready":
            print(f"\n✗ Принтер не готов: {status.klippy_state}")
        elif status.printing:
            print("\n✗ Печать уже выполняется")
        else:
            print("\n✓ Принтер готов к запуску печати")
            print("\n(Реальный запуск не выполняется в тестовом режиме)")
        
    except KeyboardInterrupt:
        print("\nОстановка...")
    
    finally:
        printer.stop()


def test_real_start_print():
    """РЕАЛЬНЫЙ запуск печати с выбором файла."""
    print("\n" + "=" * 60)
    print("ТЕСТ 9: РЕАЛЬНЫЙ ЗАПУСК ПЕЧАТИ")
    print("=" * 60)
    print("\n⚠⚠⚠ ВНИМАНИЕ! ⚠⚠⚠")
    print("Этот тест ЗАПУСТИТ ПЕЧАТЬ НА РЕАЛЬНОМ ПРИНТЕРЕ!")
    print("Убедитесь, что принтер готов к работе.")
    
    confirm = input("\nПродолжить? (введите 'ДА' для продолжения): ").strip().upper()
    
    if confirm != "ДА":
        print("Отменено.")
        return
    
    printer = KlipperPrinter(
        host=PRINTER_IP,
        poll_interval=1.0,
        request_timeout=3.0,
    )

    printer.start()
    
    try:
        # Ждем подключения
        print("\nОжидание подключения к принтеру...")
        for _ in range(10):
            status = printer.get_status()
            if status.online:
                print("✓ Принтер подключен")
                break
            time.sleep(1)
        else:
            print("✗ Не удалось подключиться к принтеру")
            return
        
        # Проверяем готовность
        status = printer.get_status()
        
        print("\nПроверка готовности принтера:")
        print(f"  В сети: {status.online}")
        print(f"  Klipper подключен: {status.klippy_connected}")
        print(f"  Состояние Klipper: {status.klippy_state}")
        print(f"  Печатает: {status.printing}")
        
        if not status.online:
            print("\n✗ Принтер не в сети")
            return
        elif not status.klippy_connected:
            print("\n✗ Klipper не подключен")
            return
        elif status.klippy_state != "ready":
            print(f"\n✗ Принтер не готов: {status.klippy_state}")
            return
        elif status.printing:
            print("\n✗ Печать уже выполняется")
            return
        
        print("\n✓ Принтер готов к запуску печати")
        
        # Получаем список файлов
        print("\nПолучение списка файлов...")
        files = printer.list_files()
        
        if not files:
            print("✗ Нет доступных файлов")
            return
        
        print(f"\nДоступно файлов: {len(files)}")
        print("-" * 60)
        
        # Показываем первые 20 файлов
        display_count = min(20, len(files))
        for i, file_info in enumerate(files[:display_count], 1):
            path = file_info.get("path", "?")
            size = file_info.get("size", 0)
            size_mb = size / (1024 * 1024)
            print(f"{i:2}. {path} ({size_mb:.2f} MB)")
        
        if len(files) > display_count:
            print(f"\n... и еще {len(files) - display_count} файлов")
        
        # Выбор файла
        print("\n" + "=" * 60)
        choice = input("\nВведите номер файла (1-{}) или путь к файлу: ".format(display_count)).strip()
        
        # Определяем выбранный файл
        selected_file = None
        
        try:
            file_index = int(choice) - 1
            if 0 <= file_index < display_count:
                selected_file = files[file_index]["path"]
            else:
                print("✗ Неверный номер файла")
                return
        except ValueError:
            # Пользователь ввел путь к файлу
            selected_file = choice
        
        print(f"\nВыбран файл: {selected_file}")
        
        # Получаем метаданные для информации
        print("\nПолучение метаданных...")
        metadata = printer.get_file_metadata(selected_file)
        
        if metadata:
            print("\nИнформация о файле:")
            if "estimated_time" in metadata:
                est_time = metadata["estimated_time"]
                hours = int(est_time // 3600)
                minutes = int((est_time % 3600) // 60)
                print(f"  Время печати: {hours}ч {minutes}м")
            
            if "filament_weight_total" in metadata:
                print(f"  Вес пластика: {metadata['filament_weight_total']:.2f} г")
            
            if "layer_count" in metadata:
                print(f"  Количество слоев: {metadata['layer_count']}")
        
        # Финальное подтверждение
        print("\n" + "=" * 60)
        print("⚠⚠⚠ ПОСЛЕДНЕЕ ПОДТВЕРЖДЕНИЕ ⚠⚠⚠")
        print(f"Запустить печать файла: {selected_file}?")
        
        final_confirm = input("\nВведите 'СТАРТ' для запуска печати: ").strip().upper()
        
        if final_confirm != "СТАРТ":
            print("Отменено.")
            return
        
        # ЗАПУСКАЕМ ПЕЧАТЬ
        print("\n🚀 Запуск печати...")
        success = printer.start_print(selected_file)
        
        if success:
            print("✓ Команда запуска печати отправлена!")
            
            # Ждем и проверяем начало печати
            print("\nОжидание начала печати...")
            for _ in range(10):
                time.sleep(1)
                status = printer.get_status()
                
                if status.printing:
                    print(f"✓ Печать начата!")
                    print(f"  Файл: {status.filename}")
                    print(f"  Прогресс: {status.progress:.1%}")
                    print(f"  Сопло: {status.extruder_temperature}°C")
                    print(f"  Стол: {status.bed_temperature}°C")
                    break
                
                print(".", end="", flush=True)
            else:
                print("\n⚠ Печать не началась в течение 10 секунд")
                print("  Проверьте статус принтера вручную")
        else:
            print("✗ Ошибка запуска печати")
            if status.error:
                print(f"   {status.error}")
        
    except KeyboardInterrupt:
        print("\nОтменено пользователем")
    
    finally:
        printer.stop()


def _wait_online(printer, timeout=10):
    print("\nОжидание подключения к принтеру...")
    for _ in range(timeout):
        status = printer.get_status()
        if status.online and status.klippy_connected:
            print(f"✓ Принтер подключен (состояние: {status.klippy_state})")
            return True
        time.sleep(1)
    print("✗ Не удалось подключиться к принтеру")
    return False


def test_print_speed():
    """Тест изменения скорости печати."""
    print("\n" + "=" * 60)
    print("ТЕСТ 10: Скорость печати")
    print("=" * 60)

    printer = KlipperPrinter(
        host=PRINTER_IP,
        poll_interval=0.5,
        request_timeout=3.0,
    )
    printer.start()

    try:
        if not _wait_online(printer):
            return

        current = printer.get_print_speed()
        print(f"\nТекущая скорость: {current:.0%}")

        print("\nПроверка отклонения 300% (должно быть отклонено)...")
        if printer.set_print_speed(3.0):
            print("✗ Опасное значение принято")
        else:
            print("✓ 300% отклонено")
            status = printer.get_status()
            if status.error:
                print(f"  {status.error}")

        print("\nУстановка 50%...")
        if not printer.set_print_speed(0.5):
            print("✗ Не удалось установить 50%")
            return

        time.sleep(1.5)
        speed = printer.get_print_speed()
        print(f"✓ Скорость: {speed:.0%}")

        print("\nВозврат к 100%...")
        if not printer.set_print_speed(1.0):
            print("✗ Не удалось вернуть 100%")
            return

        time.sleep(1.5)
        speed = printer.get_print_speed()
        print(f"✓ Скорость: {speed:.0%}")

    except KeyboardInterrupt:
        print("\nОстановка...")
    finally:
        printer.stop()


def test_move_relative():
    """Тест относительного перемещения ±10 мм по X."""
    print("\n" + "=" * 60)
    print("ТЕСТ 11: Относительное движение")
    print("=" * 60)

    printer = KlipperPrinter(
        host=PRINTER_IP,
        poll_interval=0.5,
        request_timeout=3.0,
    )
    printer.start()

    try:
        if not _wait_online(printer):
            return

        status = printer.get_status()
        if status.printing:
            print("✗ Принтер печатает — движение запрещено")
            return

        pos = printer.get_position()
        if pos is None:
            print("✗ Позиция недоступна (возможно оси не homed)")
            return

        print(f"\nНачальная позиция: X={pos['x']:.2f} Y={pos['y']:.2f} Z={pos['z']:.2f}")

        print("\nПроверка лимита 100 мм (должно быть отклонено)...")
        if printer.move_relative(x=100):
            print("✗ Перемещение 100 мм принято")
        else:
            print("✓ 100 мм отклонено")
            err = printer.get_status().error
            if err:
                print(f"  {err}")

        print("\nПеремещение X+10 мм...")
        if not printer.move_relative(x=10, feedrate=3000):
            print("✗ Не удалось отправить движение")
            err = printer.get_status().error
            if err:
                print(f"  {err}")
            return

        time.sleep(3)
        pos2 = printer.get_position()
        if pos2:
            print(f"✓ Позиция: X={pos2['x']:.2f} Y={pos2['y']:.2f} Z={pos2['z']:.2f}")

        print("\nВозврат X-10 мм...")
        if not printer.move_relative(x=-10, feedrate=3000):
            print("✗ Не удалось вернуть ось")
            return

        time.sleep(3)
        pos3 = printer.get_position()
        if pos3:
            print(f"✓ Позиция: X={pos3['x']:.2f} Y={pos3['y']:.2f} Z={pos3['z']:.2f}")

    except KeyboardInterrupt:
        print("\nОстановка...")
    finally:
        printer.stop()


def main():
    """Главное меню тестов."""
    print("\n" + "=" * 60)
    print("ТЕСТИРОВАНИЕ МОДУЛЯ 3D-ПРИНТЕРА")
    print("=" * 60)
    print("\nДоступные тесты:")
    print("\n  ФАЗА 1 - Базовый функционал:")
    print("    1 - Мониторинг статуса (5 обновлений)")
    print("    2 - Тест команды HOME с отслеживанием")
    print("\n  ФАЗА 2 - Работа с файлами:")
    print("    3 - Список файлов на принтере")
    print("    4 - Метаданные файла")
    print("    5 - Загрузка файла на принтер")
    print("    6 - Удаление файла с принтера")
    print("    7 - Проверка возможности запуска печати")
    print("    8 - Все тесты Фазы 2 последовательно")
    print("    9 - ЗАПУСК ПЕЧАТИ (РЕАЛЬНО!) ⚠")
    print("\n  ФАЗА 3 - Скорость и движение:")
    print("    10 - Скорость печати (50% / 100%)")
    print("    11 - Относительное движение X+10 / X-10")
    print("\n    0 - Выход")
    
    choice = input("\nВыберите тест (1-11, 0 для выхода): ").strip()
    
    if choice == "1":
        test_monitoring()
    elif choice == "2":
        test_homing()
    elif choice == "3":
        test_list_files()
    elif choice == "4":
        test_file_metadata()
    elif choice == "5":
        B_PCB1_file()
    elif choice == "6":
        test_delete_file()
    elif choice == "7":
        test_start_print()
    elif choice == "8":
        test_list_files()
        test_file_metadata()
        B_PCB1_file()
        test_delete_file()
        test_start_print()
    elif choice == "9":
        test_real_start_print()
    elif choice == "10":
        test_print_speed()
    elif choice == "11":
        test_move_relative()
    elif choice == "0":
        print("Выход...")
    else:
        print("Неверный выбор")


if __name__ == "__main__":
    main()