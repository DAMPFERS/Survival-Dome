# Купол — мультиагентный веб-чат

1. `pip install -r requirements.txt`
2. Задайте `DEEPSEEK_API_KEY`, `TELEMETRY_HOST`, при необходимости `TELEMETRY_PORT`.
3. Запустите backend: `python main.py`
4. В другой консоли: `python -m http.server 8080`
5. Откройте `http://localhost:8080`

Backend телеметрии слушается на `8765`, браузерный WebSocket — на `8766`.
