import requests
import logging
from pathlib import Path
from config import API_KEY, API_URL, OUTPUT_DIR, SUPPORTED_FORMATS

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def generate_3d_model(prompt: str, output_format: str = "obj") -> str:
    """
    Отправляет запрос на генерацию 3D-модели и сохраняет результат в файл.

    Args:
        prompt: Текстовый промт для генерации.
        output_format: Формат выходного файла (obj, stl, glb).

    Returns:
        Путь до сохранённого файла.

    Raises:
        ValueError: Если формат не поддерживается или ошибка API.
    """
    if output_format not in SUPPORTED_FORMATS:
        raise ValueError(f"Неподдерживаемый формат: {output_format}. Доступные: {SUPPORTED_FORMATS}")

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "prompt": prompt,
        "format": output_format
    }

    try:
        response = requests.post(API_URL, headers=headers, json=payload, timeout=30)
        response.raise_for_status()  # Вызывает ошибку, если статус не 200-299

        # Предположим, что API возвращает файл в ответе
        if response.headers.get("Content-Type") == "application/octet-stream":
            output_path = Path(OUTPUT_DIR) / f"{prompt[:20]}_{output_format}.{output_format}"
            output_path.parent.mkdir(exist_ok=True)  # Создаём папку, если её нет

            with open(output_path, "wb") as f:
                f.write(response.content)

            logger.info(f"Модель сохранена в {output_path}")
            return str(output_path)
        else:
            # Если API возвращает JSON с ссылкой на файл
            result = response.json()
            file_url = result.get("file_url")
            if not file_url:
                raise ValueError("API не вернул файл или ссылку на него")

            # Скачиваем файл по ссылке
            file_response = requests.get(file_url, timeout=30)
            file_response.raise_for_status()

            output_path = Path(OUTPUT_DIR) / f"{prompt[:20]}_{output_format}.{output_format}"
            output_path.parent.mkdir(exist_ok=True)

            with open(output_path, "wb") as f:
                f.write(file_response.content)

            logger.info(f"Модель сохранена в {output_path}")
            return str(output_path)

    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка при запросе к API: {e}")
        raise ValueError(f"Ошибка API: {e}")