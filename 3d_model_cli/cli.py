import argparse
from api_client import generate_3d_model
from config import SUPPORTED_FORMATS

def parse_args():
    """Парсит аргументы командной строки."""
    parser = argparse.ArgumentParser(description="Генерация 3D-моделей через AI API.")
    parser.add_argument(
        "--prompt",
        type=str,
        required=True,
        help="Текстовый промт для генерации 3D-модели."
    )
    parser.add_argument(
        "--format",
        type=str,
        default="obj",
        choices=SUPPORTED_FORMATS,
        help=f"Формат выходного файла. Доступные: {SUPPORTED_FORMATS}"
    )
    return parser.parse_args()

def main():
    args = parse_args()
    try:
        output_path = generate_3d_model(args.prompt, args.format)
        print(f"Успех! Модель сохранена в: {output_path}")
    except ValueError as e:
        print(f"Ошибка: {e}")

if __name__ == "__main__":
    main()