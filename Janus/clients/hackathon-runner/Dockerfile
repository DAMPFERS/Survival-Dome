FROM python:3.12-slim

# Устанавливаем Git
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Рабочая директория runner
WORKDIR /runner

# Копируем runner
COPY runner.py .

# Запускаем runner при старте контейнера
CMD ["python", "-u", "runner.py"]