"""
Генератор ХЭШ-ключей авторизации
Генерирует 4 ключа для команд + 1 ключ админа (32 символа, строки)
Сохраняет SHA-256 хэши в keys_config.json
Исходные ключи показывает один раз в консоли - их нужно раздать пользователям
"""

import secrets
import string
import hashlib
import json
import os
from datetime import datetime


# Константы
KEY_LENGTH = 32
ALPHABET = string.ascii_letters + string.digits  # a-zA-Z0-9 (62 символа)
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "keys_config.json")


def generateRandomString(length: int = KEY_LENGTH) -> str:
    """Генерирует криптографически стойкую случайную строку."""
    return ''.join(secrets.choice(ALPHABET) for _ in range(length))


def hashKey(key: str) -> str:
    """Хэширует ключ алгоритмом SHA-256. Возвращает hex-строку."""
    return hashlib.sha256(key.encode('utf-8')).hexdigest()


def generateAllKeys():
    """Генерирует 4 командных ключа и 1 админский, сохраняет хэши."""
    # Генерация исходных строк
    teamKeys = [generateRandomString(KEY_LENGTH) for _ in range(4)]
    adminKey = generateRandomString(KEY_LENGTH)

    # Хэширование
    teamHashes = [hashKey(k) for k in teamKeys]
    adminHash = hashKey(adminKey)

    # Формирование конфига
    config = {
        "teamKeys": teamHashes,
        "adminKey": adminHash,
        "generatedAt": datetime.now().isoformat(),
        "keyLength": KEY_LENGTH,
        "hashAlgorithm": "SHA-256"
    }

    # Сохранение на диск
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    # Вывод исходных ключей (единственный момент когда их можно увидеть)
    print("=" * 60)
    print("КЛЮЧИ СГЕНЕРИРОВАНЫ. СОХРАНИТЕ ИХ - ОНИ БОЛЬШЕ НЕ ОТОБРАЗЯТСЯ!")
    print("=" * 60)
    for i, key in enumerate(teamKeys, 1):
        print(f"Команда {i}: {key}  (хэш: {teamHashes[i-1][:16]}...)")
    print(f"АДМИН   : {adminKey}  (хэш: {adminHash[:16]}...)")
    print("=" * 60)
    print(f"Хэши сохранены в: {CONFIG_FILE}")
    print("=" * 60)

    return teamKeys, adminKey, config


if __name__ == "__main__":
    generateAllKeys()
