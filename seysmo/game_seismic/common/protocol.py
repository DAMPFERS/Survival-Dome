"""
protocol.py — бинарный протокол передачи сейсмических данных.

Формат чанка:
  Заголовок (36 байт):
    - magic:        4 байта  b'SEIS'
    - sensor_id:    16 байт  (ASCII, null-padded)
    - timestamp:    8 байт   (double, абсолютное время в секундах)
    - n_samples:    4 байта  (uint32, число отсчётов)
    - payload_len:  4 байта  (uint32, длина тела в байтах)
  
  Тело (payload_len байт):
    - Z: n_samples x float64
    - N: n_samples x float64
    - E: n_samples x float64
"""
import struct
import numpy as np

MAGIC = b'SEIS'
HEADER_FORMAT = '!4s16sdII'  # network byte order (big-endian)
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)  # 36 байта


def pack_chunk(sensor_id: str, timestamp: float, n_samples: int,
               z: np.ndarray, n: np.ndarray, e: np.ndarray) -> bytes:
    """
    Упаковывает чанк данных в бинарный формат.
    
    Args:
        sensor_id: ID датчика (до 16 ASCII символов)
        timestamp: абсолютное время первого отсчёта (сек)
        n_samples: число отсчётов в чанке
        z, n, e: массивы ускорений по осям (float64, длина = n_samples)
    
    Returns:
        bytes: готовый чанк для отправки по сети
    """
    # Кодируем sensor_id в 16 байт (null-padded)
    sensor_id_bytes = sensor_id.encode('ascii')[:16].ljust(16, b'\x00')
    
    # Собираем тело: Z + N + E (каждое — n_samples × 8 байт)
    payload = z.tobytes() + n.tobytes() + e.tobytes()
    
    # Упаковываем заголовок
    header = struct.pack(HEADER_FORMAT, MAGIC, sensor_id_bytes, 
                         timestamp, n_samples, len(payload))
    
    return header + payload


def unpack_header(header_bytes: bytes) -> dict:
    """
    Распаковывает заголовок чанка.
    
    Returns:
        dict: {'sensor_id': str, 'timestamp': float, 
               'n_samples': int, 'payload_len': int}
    
    Raises:
        ValueError: если magic не совпадает
    """
    if len(header_bytes) != HEADER_SIZE:
        raise ValueError(f"Invalid header size: {len(header_bytes)} != {HEADER_SIZE}")
    
    magic, sensor_id_bytes, timestamp, n_samples, payload_len = \
        struct.unpack(HEADER_FORMAT, header_bytes)
    
    if magic != MAGIC:
        raise ValueError(f"Invalid magic: {magic!r} != {MAGIC!r}")
    
    sensor_id = sensor_id_bytes.rstrip(b'\x00').decode('ascii')
    
    return {
        'sensor_id': sensor_id,
        'timestamp': timestamp,
        'n_samples': n_samples,
        'payload_len': payload_len,
    }


def unpack_payload(payload_bytes: bytes, n_samples: int) -> dict:
    """
    Распаковывает тело чанка в массивы numpy.
    
    Returns:
        dict: {'Z': np.ndarray, 'N': np.ndarray, 'E': np.ndarray}
    """
    expected_len = n_samples * 8 * 3  # 3 оси × 8 байт (float64)
    if len(payload_bytes) != expected_len:
        raise ValueError(f"Invalid payload size: {len(payload_bytes)} != {expected_len}")
    
    # Разбираем на три массива
    offset = n_samples * 8
    z = np.frombuffer(payload_bytes[:offset], dtype=np.float64).copy()
    n = np.frombuffer(payload_bytes[offset:2*offset], dtype=np.float64).copy()
    e = np.frombuffer(payload_bytes[2*offset:], dtype=np.float64).copy()
    
    return {'Z': z, 'N': n, 'E': e}