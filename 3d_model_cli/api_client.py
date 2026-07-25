# api_client.py
import requests
import time
import logging
from pathlib import Path
from config import API_BASE_URL, API_KEY, OUTPUT_DIR, SUPPORTED_FORMATS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def submit_generation_task(prompt: str, output_format: str = "glb", enable_pbr: bool = True) -> str:
    """
    Отправляет запрос на генерацию 3D-модели и возвращает task_id.
    """
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "prompt": prompt,
        "enable_pbr": enable_pbr,
    }

    try:
        logger.info(f"Отправка запроса на генерацию для промта: {prompt}")
        response = requests.post(
            f"{API_BASE_URL}/v1/3d-models/tencent/generate/rapid/",
            headers=headers,
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        task_id = response.json().get("task_id")
        if not task_id:
            raise ValueError("API не вернул task_id.")
        logger.info(f"Получен task_id: {task_id}")
        return task_id

    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка при отправке запроса: {e}")
        raise ValueError(f"Не удалось отправить запрос на генерацию: {e}")

def check_task_status(task_id: str, max_attempts: int = 20, delay: int = 10) -> dict:
    """
    Опрашивает статус задачи, пока она не будет готова.
    """
    headers = {
        "Authorization": f"Bearer {API_KEY}",
    }

    for attempt in range(max_attempts):
        try:
            logger.info(f"Проверка статуса задачи {task_id} (попытка {attempt + 1}/{max_attempts})")
            response = requests.get(
                f"{API_BASE_URL}/v1/generation-request/{task_id}/status/",
                headers=headers,
                timeout=30,
            )
            response.raise_for_status()
            status_data = response.json()
            logger.info(f"СЫРОЙ ОТВЕТ API: {status_data}")
            status = status_data.get("status")

            if status == "FINISHED":
                logger.info(f"Задача {task_id} завершена.")
                return status_data
            elif status == "FAILED":
                raise ValueError(f"Задача не удалась: {status_data.get('failure_reason', 'Неизвестная ошибка')}")
            elif status in ["PENDING", "IN_PROGRESS"]:
                logger.info(f"Задача ещё выполняется (статус: {status}). Ожидание {delay} секунд...")
                time.sleep(delay)
            else:
                logger.warning(f"Неизвестный статус задачи: {status}")
                time.sleep(delay)

        except requests.exceptions.RequestException as e:
            logger.error(f"Ошибка при проверке статуса: {e}")
            raise ValueError(f"Не удалось проверить статус задачи: {e}")

    raise ValueError(f"Превышено количество попыток для задачи {task_id}.")

def get_download_url(status_data: dict) -> str:
    """
    Извлекает URL для скачивания 3D-модели из ответа API.
    """
    results = status_data.get("results", [])
    if not results:
        raise ValueError("Нет результатов в ответе API.")

    # Берём первый asset из результатов (должен быть 3D_MODEL)
    asset_data = results[0]
    asset_url = asset_data.get("asset")
    if not asset_url:
        raise ValueError("Нет URL для скачивания в результатах.")

    asset_type = asset_data.get("asset_type")
    if asset_type != "3D_MODEL":
        raise ValueError(f"Ожидался 3D_MODEL, получен {asset_type}.")

    return asset_url

def download_file(url: str, output_path: Path) -> Path:
    """
    Скачивает файл по URL и сохраняет его в output_path.
    """
    try:
        logger.info(f"Скачивание файла по URL: {url}")
        response = requests.get(url, timeout=60)
        response.raise_for_status()

        with open(output_path, "wb") as f:
            f.write(response.content)
        logger.info(f"Файл сохранён в: {output_path}")
        return output_path

    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка при скачивании файла: {e}")
        raise ValueError(f"Не удалось скачать файл: {e}")

def generate_3d_model(prompt: str, output_format: str = "glb", output_name: str = None) -> Path:
    """
    Полный цикл генерации 3D-модели.
    """
    if output_format not in SUPPORTED_FORMATS:
        raise ValueError(f"Неподдерживаемый формат: {output_format}. Доступные: {SUPPORTED_FORMATS}")

    if not output_name:
        output_name = prompt[:50].replace(" ", "_")
    output_path = OUTPUT_DIR / f"{output_name}.{output_format}"

    try:
        # Шаг 1: Отправка запроса на генерацию
        task_id = submit_generation_task(prompt, output_format)

        # Шаг 2: Ожидание завершения задачи
        status_data = check_task_status(task_id)

        # Шаг 3: Извлечение URL для скачивания
        download_url = get_download_url(status_data)
        logger.info(f"Получен URL для скачивания: {download_url}")

        # Шаг 4: Скачивание файла
        download_file(download_url, output_path)
        return output_path

    except ValueError as e:
        logger.error(f"Ошибка при генерации модели: {e}")
        raise