import sounddevice as sd
import logging

logger = logging.getLogger(__name__)


def find_microphone(
    preferred_name: str | None = None,
    min_channels: int = 1,
) -> int | None:
    """
    Автоматически найти подходящий микрофон.

    Приоритет поиска:
    1. Устройство, в имени которого есть preferred_name (если передан)
    2. Системное устройство ввода по умолчанию
    3. Первое найденное устройство с входными каналами

    :param preferred_name: Часть имени устройства для поиска (например "USB", "Blue")
    :param min_channels:   Минимальное число входных каналов
    :return:               Индекс устройства или None если ничего не найдено
    """
    devices = sd.query_devices()

    print("\n📋 Доступные аудио-устройства:")
    print("-" * 60)
    for i, dev in enumerate(devices):
        if dev["max_input_channels"] >= min_channels:
            marker = "🎤"
        else:
            marker = "  "
        print(f"  [{i:2d}] {marker} {dev['name']}"
              f"  (вход: {dev['max_input_channels']}ch"
              f"  | {int(dev['default_samplerate'])} Гц)")
    print("-" * 60)

    # --- 1. Поиск по имени ---
    if preferred_name:
        for i, dev in enumerate(devices):
            if (preferred_name.lower() in dev["name"].lower()
                    and dev["max_input_channels"] >= min_channels):
                logger.info("Найден микрофон по имени '%s': [%d] %s",
                            preferred_name, i, dev["name"])
                print(f"✅ Выбран по имени: [{i}] {dev['name']}")
                return i

        logger.warning("Микрофон с именем '%s' не найден, ищем дальше...",
                       preferred_name)

    # --- 2. Системное устройство по умолчанию ---
    try:
        default = sd.query_devices(kind="input")
        default_idx = sd.default.device[0]  # индекс устройства ввода
        if (default_idx is not None
                and devices[default_idx]["max_input_channels"] >= min_channels):
            logger.info("Используем системный микрофон по умолчанию: [%d] %s",
                        default_idx, default["name"])
            print(f"✅ Выбран по умолчанию: [{default_idx}] {default['name']}")
            return default_idx
    except Exception as e:
        logger.warning("Не удалось получить устройство по умолчанию: %s", e)

    # --- 3. Первый доступный микрофон ---
    for i, dev in enumerate(devices):
        if dev["max_input_channels"] >= min_channels:
            logger.info("Выбран первый доступный микрофон: [%d] %s",
                        i, dev["name"])
            print(f"✅ Выбран первый доступный: [{i}] {dev['name']}")
            return i

    logger.error("Микрофоны не найдены!")
    print("❌ Микрофоны не найдены!")
    return None


def verify_microphone(device_idx: int, sample_rate: int = 16000) -> bool:
    """
    Проверить, что микрофон реально работает — записать 0.5 сек тишины.
    Возвращает True если всё ок.
    """
    try:
        sd.check_input_settings(
            device=device_idx,
            channels=1,
            dtype="float32",
            samplerate=sample_rate,
        )
        logger.info("Микрофон [%d] прошёл проверку", device_idx)
        return True
    except Exception as e:
        logger.error("Микрофон [%d] не прошёл проверку: %s", device_idx, e)
        return False