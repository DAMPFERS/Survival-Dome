"""
Модуль авторизации с классом Protector
Управляет проверкой ключей доступа команд и администратора
"""

import hashlib
import json
import os
from typing import Optional


CONFIG_FILE = os.path.join(os.path.dirname(__file__), "keys_config.json")


class Protector:
    """
    Класс для управления авторизацией команд.
    
    Возможности:
    - Установка активного ключа команды (индекс 0-3)
    - Проверка введенного ключа (админский ключ пропускается всегда)
    """
    
    def __init__(self, configPath: str = CONFIG_FILE):
        """
        Инициализация Protector.
        
        Args:
            configPath: путь к файлу конфигурации с хэшами ключей
        """
        self.configPath = configPath
        self._activeTeamIndex: Optional[int] = None
        self._loadConfig()
    
    def _loadConfig(self):
        """Загружает конфигурацию с хэшами из JSON файла."""
        if not os.path.exists(self.configPath):
            raise FileNotFoundError(
                f"Файл конфигурации не найден: {self.configPath}\n"
                f"Запустите generate_keys.py для создания ключей."
            )
        
        with open(self.configPath, 'r', encoding='utf-8') as f:
            config = json.load(f)
        
        self.teamHashes = config.get("teamKeys", [])
        self.adminHash = config.get("adminKey")
        
        if len(self.teamHashes) != 4:
            raise ValueError(f"Ожидается 4 командных ключа, найдено: {len(self.teamHashes)}")
        if not self.adminHash:
            raise ValueError("Админский ключ отсутствует в конфигурации")
    
    def _hashKey(self, key: str) -> str:
        """Хэширует ключ алгоритмом SHA-256."""
        return hashlib.sha256(key.encode('utf-8')).hexdigest()
    
    def setActiveTeamKey(self, teamIndex: int) -> None:
        """
        Устанавливает активный ключ команды.
        
        Args:
            teamIndex: индекс команды (0-3)
        
        Raises:
            ValueError: если индекс вне диапазона 0-3
        """
        if not (0 <= teamIndex <= 3):
            raise ValueError(f"Индекс команды должен быть от 0 до 3, получено: {teamIndex}")
        
        self._activeTeamIndex = teamIndex
        print(f"[Protector] Активный ключ установлен: Команда {teamIndex + 1}")
    
    def verifyKey(self, inputKey: str) -> bool:
        """
        Проверяет введенный ключ.
        
        Логика проверки:
        1. Хэширует введенный ключ
        2. Если хэш совпадает с админским - возвращает True (админ всегда проходит)
        3. Если активный ключ команды установлен и хэш совпадает - возвращает True
        4. Иначе возвращает False
        
        Args:
            inputKey: строка ключа для проверки
        
        Returns:
            True если ключ валиден, False иначе
        """
        inputHash = self._hashKey(inputKey)
        
        # Проверка админского ключа (приоритет)
        if inputHash == self.adminHash:
            print("[Protector] Доступ разрешен: АДМИН")
            return True
        
        # Проверка активного командного ключа
        if self._activeTeamIndex is not None:
            activeHash = self.teamHashes[self._activeTeamIndex]
            if inputHash == activeHash:
                print(f"[Protector] Доступ разрешен: Команда {self._activeTeamIndex + 1}")
                return True
        
        print("[Protector] Доступ запрещен: неверный ключ")
        return False
    
    def getActiveTeamIndex(self) -> Optional[int]:
        """Возвращает индекс активной команды или None если не установлен."""
        return self._activeTeamIndex


# Пример использования
if __name__ == "__main__":
    # Демонстрация работы класса
    print("=== Демонстрация работы Protector ===\n")
    
    try:
        protector = Protector()
        
        # Установка активной команды
        protector.setActiveTeamKey(0)  # Команда 1
        
        # Симуляция проверки ключей
        print("\n--- Тестовые проверки ---")
        print("Проверка случайного ключа:")
        protector.verifyKey("wrongkey12345")
        
        print("\nИнформация:")
        print(f"Активная команда: {protector.getActiveTeamIndex()}")
        print(f"Загружено командных хэшей: {len(protector.teamHashes)}")
        print(f"Админский хэш загружен: {'Да' if protector.adminHash else 'Нет'}")
        
    except FileNotFoundError as e:
        print(f"ОШИБКА: {e}")
        print("Сначала запустите: python generate_keys.py")
