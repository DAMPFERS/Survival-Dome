#!/usr/bin/env python3
# tests/test_runner.py
"""
Главный скрипт запуска тестов симулятора купола.

Использование:
    python test_runner.py                    # все тесты
    python test_runner.py --category state   # только тесты параметров
    python test_runner.py --category nodes   # только тесты узлов
    python test_runner.py --category deps    # только зависимости
    python test_runner.py --category crises  # только кризисы
    python test_runner.py --category integration  # интеграционные
    python test_runner.py --category time    # управление временем
    python test_runner.py --category threads # потокобезопасность
    python test_runner.py --coverage         # с измерением покрытия
    python test_runner.py --parallel         # параллельный запуск
    python test_runner.py --verbose          # подробный вывод
"""
import sys
import argparse
import subprocess
from pathlib import Path


# Категории тестов
CATEGORIES = {
    "state": "test_state_store.py",
    "nodes": "test_nodes.py",
    "deps": "test_dependencies.py",
    "crises": "test_crises.py",
    "integration": "test_integration.py",
    "time": "test_time_control.py",
    "threads": "test_thread_safety.py",
}


def run_tests(args):
    """Запуск тестов с заданными параметрами."""
    # Базовая команда pytest
    cmd = ["pytest"]
    
    # Определяем директорию тестов
    tests_dir = Path(__file__).parent
    
    # Фильтрация по категории
    if args.category:
        if args.category not in CATEGORIES:
            print(f"❌ Неизвестная категория: {args.category}")
            print(f"   Доступные: {', '.join(CATEGORIES.keys())}")
            return 1
        
        test_file = tests_dir / CATEGORIES[args.category]
        cmd.append(str(test_file))
        print(f"🔍 Запуск категории: {args.category}")
    else:
        cmd.append(str(tests_dir))
        print("🔍 Запуск всех тестов")
    
    # Покрытие кода
    if args.coverage:
        cmd.extend([
            "--cov=dome_simulator",
            "--cov-report=term-missing",
            "--cov-report=term:skip-covered"
        ])
        print("📊 Измерение покрытия кода включено")
    
    # Параллельный запуск
    if args.parallel:
        import os
        cpu_count = os.cpu_count() or 2
        cmd.extend(["-n", str(cpu_count)])
        print(f"⚡ Параллельный запуск на {cpu_count} ядрах")
    
    # Уровень детализации
    if args.verbose:
        cmd.append("-v")
    else:
        cmd.append("-q")
    
    # Показывать локальные переменные при ошибках
    cmd.append("--tb=short")
    
    # Таймаут для тестов (избегаем зависаний)
    cmd.extend(["--timeout=60"])
    
    # Цветной вывод
    cmd.append("--color=yes")
    
    print(f"📋 Команда: {' '.join(cmd)}\n")
    print("=" * 70)
    
    # Запускаем pytest
    try:
        result = subprocess.run(cmd, cwd=tests_dir.parent)
        return result.returncode
    except KeyboardInterrupt:
        print("\n⚠️  Тесты прерваны пользователем")
        return 130
    except Exception as e:
        print(f"\n❌ Ошибка запуска тестов: {e}")
        return 1


def main():
    parser = argparse.ArgumentParser(
        description="Запуск тестов симулятора купола",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    
    parser.add_argument(
        "--category", "-c",
        choices=list(CATEGORIES.keys()),
        help="Запустить тесты только одной категории"
    )
    
    parser.add_argument(
        "--coverage",
        action="store_true",
        help="Измерить покрытие кода тестами"
    )
    
    parser.add_argument(
        "--parallel", "-p",
        action="store_true",
        help="Параллельный запуск тестов (быстрее)"
    )
    
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Подробный вывод результатов"
    )
    
    args = parser.parse_args()
    
    # Проверяем наличие pytest
    try:
        subprocess.run(["pytest", "--version"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("❌ pytest не установлен!")
        print("   Установите зависимости: pip install -r requirements-test.txt")
        return 1
    
    # Запускаем тесты
    exit_code = run_tests(args)
    
    print("\n" + "=" * 70)
    if exit_code == 0:
        print("✅ Все тесты пройдены успешно!")
    else:
        print(f"❌ Тесты завершились с кодом: {exit_code}")
    
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
