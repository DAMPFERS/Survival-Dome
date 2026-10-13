# -*- coding: utf-8 -*-
"""
HIKVISION RTSP -> MP4/RTMP Streamer (v3.2 - Fixed Decode Order)
Исправление: Декодирование для предпросмотра теперь выполняется ДО изменения packet.stream,
что исключает ошибку avcodec_send_packet() returned 22.
"""

import av
import cv2
import time
import sys

# Логирование только ошибок, чтобы не засорять консоль
av.logging.set_level(av.logging.ERROR)

# --- НАСТРОЙКИ ---
RTSP_USER = "admin"
RTSP_PASS = "!Dezmant2805"
RTSP_IP = "192.168.8.65"
RTSP_PORT = "554"
RTSP_PATH = "/Streaming/Channels/101"

RTSP_URL = f"rtsp://{RTSP_USER}:{RTSP_PASS}@{RTSP_IP}:{RTSP_PORT}{RTSP_PATH}"

# Для теста используем локальный файл. 
# Позже заменим на: "rtmp://ваш-сервер/live/stream" или "srt://..."
OUTPUT_URL = "test_output.mp4"

# ⚠️ ВАЖНО: Для финальной работы через туннель установите False!
# Это превратит скрипт в сверхбыстрый "прозрачный канал" (0% нагрузки на CPU).
# True используйте только для локальной отладки.
SHOW_LOCAL_VIDEO = True 

def main():
    print("=" * 60)
    print("HIKVISION Streamer (v3.2 - Fixed Decode Order)")
    print("=" * 60)
    print(f"📹 Вход:  {RTSP_URL}")
    print(f"📁 Выход: {OUTPUT_URL}")
    print("-" * 60)
    
    options = {
        'rtsp_transport': 'tcp',
        'stimeout': '10000000',  # 10 секунд
    }
    
    try:
        print("Подключение к камере...")
        input_container = av.open(RTSP_URL, options=options)
        print("✓ УСПЕХ! Камера подключена!")
    except Exception as e:
        print(f"✗ Ошибка подключения: {e}")
        sys.exit(1)
    
    # Находим потоки
    video_in = next((s for s in input_container.streams if s.type == 'video'), None)
    audio_in = next((s for s in input_container.streams if s.type == 'audio'), None)
    
    if not video_in:
        print("✗ Не найден видео поток!")
        sys.exit(1)
        
    print(f"✓ Видео: {video_in.codec_context.name}, {video_in.average_rate} fps")
    if audio_in:
        print(f"✓ Аудио: {audio_in.codec_context.name}, {audio_in.rate} Hz, {audio_in.layout.name}")
    else:
        print("⚠ Аудио поток не найден. Будет записано только видео.")

    # Инициализация выходного контейнера
    try:
        output_container = av.open(OUTPUT_URL, mode='w')
    except Exception as e:
        print(f"✗ Не удалось создать выходной файл: {e}")
        sys.exit(1)

    # --- ДВОЙНОЕ КОПИРОВАНИЕ ПОТОКА (STREAM COPY) ---
    try:
        video_out = output_container.add_stream_from_template(video_in)
        
        audio_out = None
        if audio_in:
            audio_out = output_container.add_stream_from_template(audio_in)
            print("✓ Настроен режим Stream Copy для видео и аудио (без перекодирования)")
            
    except AttributeError:
        print("⚠ add_stream_from_template не найден, используем старый синтаксис...")
        video_out = output_container.add_stream(video_in.codec_context.name)
        video_out.time_base = video_in.time_base
        audio_out = None
        if audio_in:
            audio_out = output_container.add_stream(audio_in.codec_context.name)
            audio_out.time_base = audio_in.time_base

    if SHOW_LOCAL_VIDEO:
        cv2.namedWindow('Local Preview', cv2.WINDOW_NORMAL)
        print("Откройте окно предпросмотра. Нажмите 'q' для выхода.")

    # Счетчики
    v_packets = 0
    a_packets = 0
    last_log = time.time()

    try:
        streams_to_demux = [video_in]
        if audio_in:
            streams_to_demux.append(audio_in)
            
        for packet in input_container.demux(*streams_to_demux):
            if packet is None or packet.dts is None:
                continue 
            
            # 1. Обработка ВИДЕО
            if packet.stream == video_in:
                
                # ШАГ А: Сначала декодируем для предпросмотра (пока packet привязан к video_in)
                if SHOW_LOCAL_VIDEO:
                    try:
                        # Явно вызываем decode у исходного потока, чтобы избежать конфликтов контекстов
                        for frame in video_in.decode(packet):
                            img = frame.to_ndarray(format='bgr24')
                            cv2.imshow('Local Preview', img)
                            if cv2.waitKey(1) & 0xFF == ord('q'):
                                raise KeyboardInterrupt
                    except Exception:
                        # Игнорируем ошибки декодирования отдельных кадров, чтобы не ронять поток записи
                        pass

                # ШАГ Б: Затем меняем поток и отправляем на запись (Stream Copy)
                packet.stream = video_out
                try:
                    output_container.mux(packet)
                    v_packets += 1
                except Exception as e:
                    print(f"✗ Ошибка mux видео: {e}")
                    continue

            # 2. Обработка АУДИО
            elif packet.stream == audio_in and audio_out:
                packet.stream = audio_out
                try:
                    output_container.mux(packet)
                    a_packets += 1
                except Exception as e:
                    print(f"✗ Ошибка mux аудио: {e}")
                    continue

            # Лог каждые 3 секунды
            if time.time() - last_log > 3.0:
                print(f"📊 Поток идет... Видео: {v_packets} пак., Аудио: {a_packets} пак.")
                last_log = time.time()

    except KeyboardInterrupt:
        print("\n⏹ Остановлено пользователем (Ctrl+C или 'q')")
    except Exception as e:
        print(f"\n✗ Критическая ошибка: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("\nЗавершение и сохранение файла...")
        if SHOW_LOCAL_VIDEO:
            cv2.destroyAllWindows()
            
        output_container.close()
        input_container.close()
        print(f"✅ Готово! Записано: {v_packets} видео и {a_packets} аудио пакетов.")
        print(f"📁 Файл: {OUTPUT_URL}")
        print("💡 Откройте его в VLC, чтобы проверить синхронизацию.")

if __name__ == "__main__":
    main()