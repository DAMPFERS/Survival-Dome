# cli.py
import argparse
from api_client import generate_3d_model
from config import SUPPORTED_FORMATS

def parse_args():
    parser = argparse.ArgumentParser(description="Генерация 3D-моделей по промту.")
    parser.add_argument(
        "--prompt",
        type=str,
        required=True,
        help="Текстовый промт для генерации 3D-модели.",
    )
    parser.add_argument(
        "--format",
        type=str,
        default="glb",
        choices=SUPPORTED_FORMATS,
        help=f"Формат выходного файла. Доступные: {SUPPORTED_FORMATS}",
    )
    parser.add_argument(
        "--output-name",
        type=str,
        default=None,
        help="Имя файла (без расширения). Если не указано, используется часть промта.",
    )
    return parser.parse_args()

def main():
    args = parse_args()
    try:
        output_path = generate_3d_model(
            prompt=args.prompt,
            output_format=args.format,
            output_name=args.output_name,
        )
        print(f"Успех! Модель сохранена в: {output_path}")
    except ValueError as e:
        print(f"Ошибка: {e}")

if __name__ == "__main__":
    main()