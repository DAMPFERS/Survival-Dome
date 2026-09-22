#!/usr/bin/env python3
import socket
import time
import sys

# ====== Настройки ======
ESP_IP = "172.31.170.108"      # ← укажите IP вашего ESP32
ESP_PORT = 8881               # порт UART (GRBL)
TIMEOUT = 10                  # таймаут сокета (секунды)

def send_and_receive(sock, data: str, wait=0.3):
    """Отправляет данные и читает ответ"""
    sock.sendall(data.encode('utf-8'))
    time.sleep(wait)
    
    response = b""
    sock.settimeout(0.5)
    try:
        while True:
            chunk = sock.recv(1024)
            if not chunk:
                break
            response += chunk
    except socket.timeout:
        pass
    
    return response.decode('utf-8', errors='ignore')

def check_ok_flag(sock) -> bool:
    """Проверяет флаг ok на ESP32"""
    reply = send_and_receive(sock, "?OK\n", wait=0.2)
    print(f"  [?OK] → {reply.strip()}")
    return "OK:1" in reply

def clear_ok_flag(sock):
    """Сбрасывает флаг ok"""
    reply = send_and_receive(sock, "!CLR\n", wait=0.2)
    print(f"  [!CLR] → {reply.strip()}")

def main():
    print(f"Подключение к {ESP_IP}:{ESP_PORT} ...")
    
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(TIMEOUT)
        sock.connect((ESP_IP, ESP_PORT))
        print("✓ Соединение установлено\n")
    except Exception as e:
        print(f"✗ Ошибка подключения: {e}")
        sys.exit(1)

    try:
        # 1. Сбрасываем флаг на всякий случай
        print("1. Сброс флага ok")
        clear_ok_flag(sock)
        print()

        # 2. Проверяем текущее состояние флага
        print("2. Проверка флага до отправки команды")
        check_ok_flag(sock)
        print()

        # 3. Отправляем команду поиска нуля (homing)
        print("3. Отправка команды $H (поиск нуля по концевикам)")
        response = send_and_receive(sock, "G91 G0 X5\n", wait=0.5)
        if response:
            print(f"  Ответ сразу после отправки:\n{response}")
        else:
            print("  (пока нет ответа)")
        print()

        # 4. Ждём появления ok (с таймаутом)
        print("4. Ожидание подтверждения ok от GRBL...")
        timeout = 30          # максимум 30 секунд на homing
        start = time.time()
        ok_received = False

        while time.time() - start < timeout:
            if check_ok_flag(sock):
                ok_received = True
                break
            time.sleep(0.8)

        print()
        if ok_received:
            print("✓ Получено подтверждение ok!")
            clear_ok_flag(sock)
        else:
            print("✗ Таймаут: ok так и не пришёл")
        
        # 5. Читаем всё, что ещё осталось в буфере
        print("\n5. Дополнительный вывод от GRBL:")
        sock.settimeout(1.0)
        try:
            extra = sock.recv(4096).decode('utf-8', errors='ignore')
            if extra:
                print(extra)
            else:
                print("  (пусто)")
        except socket.timeout:
            print("  (пусто)")

    except KeyboardInterrupt:
        print("\nПрервано пользователем")
    finally:
        sock.close()
        print("\nСоединение закрыто")

if __name__ == "__main__":
    main()