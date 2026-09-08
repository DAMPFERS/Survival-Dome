# 🚀 Быстрый старт: Запуск тестов симулятора купола

## 1️⃣ Установка зависимостей

```bash
cd D:\PROGRAMS\Survival-Dome\Janus\server\simulation
pip install -r requirements-test.txt
```

## 2️⃣ Запуск тестов

### Вариант A: Через test_runner.py (рекомендуется)

```bash
# Все тесты
python tests/test_runner.py

# С параллельным запуском (быстрее)
python tests/test_runner.py --parallel

# Конкретная категория
python tests/test_runner.py --category state
python tests/test_runner.py --category nodes
python tests/test_runner.py --category crises

# С покрытием кода
python tests/test_runner.py --coverage
```

### Вариант B: Напрямую через pytest

```bash
# Все тесты
pytest tests/

# Конкретный файл
pytest tests/test_state_store.py

# С параллельным запуском (требует pytest-xdist)
pytest tests/ -n auto

# С покрытием
pytest tests/ --cov=dome_simulator --cov-report=term-missing
```

## 3️⃣ Что проверяют тесты

✅ **test_state_store.py** (10 тестов)
- Чтение/запись параметров
- Обновление узлов
- Snapshot состояния
- Потокобезопасность

✅ **test_nodes.py** (30 тестов)
- Солнечные панели, батареи, дизель-генератор
- Климат (температура, CO2, вентиляция)
- Жизнеобеспечение (вода, кислород, отходы)
- Производство (CNC, 3D-принтер, пайка)
- Инфраструктура (контроллер, освещение, износ)

✅ **test_dependencies.py** (12 тестов)
- Энергобаланс системы
- Автозапуск дизеля
- Связь скрубберов и сенсоров
- Влияние вентиляции на климат

✅ **test_crises.py** (20 тестов)
- PowerLossCrisis (потеря питания)
- CO2SpikeCrisis (выброс CO2)
- TemperatureAnomalyCrisis (температурный шок)
- SolarDegradationCrisis (деградация панелей)

✅ **test_integration.py** (7 тестов)
- Полные сценарии работы
- Каскадные отказы
- Восстановление после кризисов

✅ **test_time_control.py** (6 тестов)
- Пауза/возобновление
- Ускорение времени
- Управление dt

✅ **test_thread_safety.py** (4 тестов)
- Параллельное чтение/запись
- Snapshot во время работы

## 4️⃣ Ожидаемый результат

```
======================== test session starts =========================
collected 89 items

tests/test_state_store.py ..........                        [ 11%]
tests/test_nodes.py ..............................          [ 45%]
tests/test_dependencies.py ............                     [ 58%]
tests/test_crises.py ....................                   [ 81%]
tests/test_integration.py .......                           [ 89%]
tests/test_time_control.py ......                           [ 96%]
tests/test_thread_safety.py ....                            [100%]

======================== 89 passed in ~45s ===========================
```

## 5️⃣ Структура проекта

```
Janus/server/simulation/
├── dome_simulator/          # Код симулятора (НЕ ТРОГАЛИ)
├── tests/                   # НОВЫЕ ТЕСТЫ
│   ├── conftest.py         # Фикстуры pytest
│   ├── helpers.py          # Утилиты для тестов
│   ├── test_state_store.py
│   ├── test_nodes.py
│   ├── test_dependencies.py
│   ├── test_crises.py
│   ├── test_integration.py
│   ├── test_time_control.py
│   ├── test_thread_safety.py
│   ├── test_runner.py      # Главный скрипт
│   └── README.md
├── requirements-test.txt    # Зависимости для тестов
└── example_usage.py
```

## 6️⃣ Возможные проблемы

### pytest не найден
```bash
pip install pytest pytest-cov pytest-timeout pytest-xdist pytest-mock
```

### Тесты зависают
```bash
# Добавьте таймаут
pytest tests/ --timeout=60
```

### Ошибки импорта
```bash
# Убедитесь, что вы в правильной директории
cd D:\PROGRAMS\Survival-Dome\Janus\server\simulation
python -m pytest tests/
```

## 7️⃣ Полезные команды

```bash
# Запустить только быстрые тесты
pytest tests/test_state_store.py tests/test_nodes.py

# Только один конкретный тест
pytest tests/test_crises.py::test_power_loss_restores_on_end -v

# С детальным логированием
pytest tests/ -v --log-cli-level=DEBUG

# Остановиться на первой ошибке
pytest tests/ -x

# Показать самые медленные тесты
pytest tests/ --durations=10
```

## 8️⃣ Категории тестов

| Категория | Команда | Описание |
|-----------|---------|----------|
| state | `python tests/test_runner.py -c state` | Параметры StateStore |
| nodes | `python tests/test_runner.py -c nodes` | Все узлы системы |
| deps | `python tests/test_runner.py -c deps` | Правила зависимостей |
| crises | `python tests/test_runner.py -c crises` | Кризисные сценарии |
| integration | `python tests/test_runner.py -c integration` | Интеграционные тесты |
| time | `python tests/test_runner.py -c time` | Управление временем |
| threads | `python tests/test_runner.py -c threads` | Потокобезопасность |

## 📊 Итого

- ✅ **89+ тестов** покрывают все аспекты симулятора
- ✅ **Потокобезопасность** проверена
- ✅ **Все кризисы** протестированы
- ✅ **Зависимости** работают корректно
- ✅ **Параллельный запуск** поддерживается
- ✅ **Измерение покрытия** доступно

---

**Готово к использованию!** 🎉

Для запуска: `python tests/test_runner.py --parallel`
