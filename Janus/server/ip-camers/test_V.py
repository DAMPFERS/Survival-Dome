import cv2
import urllib.parse

# Кодируем спецсимволы в пароле
password = urllib.parse.quote("!Dezmant2805")

# Полный RTSP URL с аутентификацией
rtsp_url = f"rtsp://admin:{password}@192.168.8.65:554/Streaming/Channels/101?transportmode=unicast&profile=Profile_1"

print("Подключаемся к камере...")
print(f"URL: {rtsp_url}")

# Открываем поток с явным указанием FFmpeg
cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 10000)
cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000)

if cap.isOpened():
    print("✓ УСПЕХ! Камера подключена!")
    
    # Показываем видео
    while True:
        ret, frame = cap.read()
        if not ret:
            print("Не удалось получить кадр")
            break
            
        cv2.imshow('Camera Stream', frame)
        
        # Выход по 'q'
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break
    
    cap.release()
    cv2.destroyAllWindows()
else:
    print("✗ Не удалось подключиться")