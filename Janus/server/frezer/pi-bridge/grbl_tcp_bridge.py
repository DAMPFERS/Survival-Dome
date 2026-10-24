#!/usr/bin/env python3
"""Прозрачный TCP <-> USB-serial мост для GRBL 1.1 (замена ESP32).

Поведение как прошивка bridge_frez 1.20-GRBL:
  TCP :8881  <->  COM 115200 8N1, сырые байты, без парсинга.
  Новый TCP-клиент вытесняет старого. TCP_NODELAY.
  Realtime-байты (?, !, ~, 0x18, 0x9E) проходят как есть, без \\n.

USB-специфика: DTR/RTS выключены, HUPCL снят, иначе GRBL на Arduino
сбросится при открытии порта (ALARM:3, позиция потеряна).
"""

from __future__ import annotations

import argparse
import glob
import logging
import os
import select
import signal
import socket
import subprocess
import sys
import termios
import time
from typing import Optional

import serial

log = logging.getLogger("grbl-bridge")

BUFFER_SIZE = 1024
DEFAULT_TCP_PORT = 8881
DEFAULT_BAUD = 115200

TIOCEXCL = getattr(termios, "TIOCEXCL", 0x540C)


class BridgeError(Exception):
    pass


def find_serial_port(explicit: Optional[str]) -> Optional[str]:
    if explicit and explicit not in ("auto", ""):
        return explicit
    by_id = sorted(glob.glob("/dev/serial/by-id/*"))
    if by_id:
        return by_id[0]
    for pattern in ("/dev/ttyUSB*", "/dev/ttyACM*"):
        ports = sorted(glob.glob(pattern))
        if ports:
            return ports[0]
    return None


def _disable_hupcl(fd: int) -> None:
    attrs = termios.tcgetattr(fd)
    attrs[2] &= ~termios.HUPCL
    termios.tcsetattr(fd, termios.TCSANOW, attrs)


def _stty_no_hupcl(port: str) -> None:
    try:
        subprocess.run(
            ["stty", "-F", port, "-hupcl"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def open_serial(port: str, baud: int) -> serial.Serial:
    real = os.path.realpath(port)
    if not os.path.exists(real):
        raise BridgeError(f"порт не найден: {port}")

    _stty_no_hupcl(real)

    ser = serial.Serial()
    ser.port = real
    ser.baudrate = baud
    ser.bytesize = serial.EIGHTBITS
    ser.parity = serial.PARITY_NONE
    ser.stopbits = serial.STOPBITS_ONE
    ser.timeout = 0
    ser.write_timeout = 1
    ser.xonxoff = False
    ser.rtscts = False
    ser.dsrdtr = False
    ser.exclusive = True
    ser.dtr = False
    ser.rts = False
    ser.open()

    fd = ser.fileno()
    _disable_hupcl(fd)
    try:
        import fcntl

        fcntl.ioctl(fd, TIOCEXCL)
    except OSError:
        pass
    ser.dtr = False
    ser.rts = False
    ser.reset_input_buffer()
    ser.reset_output_buffer()
    return ser


def wait_serial(explicit: Optional[str], baud: int, stop) -> Optional[serial.Serial]:
    logged_missing = False
    while not stop[0]:
        port = find_serial_port(explicit)
        if port:
            try:
                ser = open_serial(port, baud)
                log.info("serial open %s (%s) %d 8N1", port, ser.port, baud)
                return ser
            except Exception as exc:
                log.warning("не удалось открыть %s: %s", port, exc)
        elif not logged_missing:
            log.warning("USB-serial не найден, жду /dev/ttyUSB* или /dev/ttyACM*")
            logged_missing = True
        time.sleep(1.0)
    return None


def _set_keepalive(sock: socket.socket) -> None:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    if hasattr(socket, "TCP_KEEPIDLE"):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 10)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 3)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)


def _close(sock: Optional[socket.socket]) -> None:
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def run_bridge(args: argparse.Namespace) -> int:
    stop = [False]

    def _on_signal(signum, _frame):
        log.info("signal %s, выход", signum)
        stop[0] = True

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    server.bind((args.listen, args.tcp_port))
    server.listen(1)
    server.setblocking(False)
    log.info("TCP listen %s:%d", args.listen, args.tcp_port)

    ser: Optional[serial.Serial] = None
    client: Optional[socket.socket] = None
    client_addr = None
    buf_size = args.buffer

    try:
        ser = wait_serial(args.serial, args.baud, stop)
        while not stop[0]:
            if ser is None or not ser.is_open:
                _close(client)
                client = None
                client_addr = None
                ser = wait_serial(args.serial, args.baud, stop)
                if ser is None:
                    break

            readers = [server]
            if client is not None:
                readers.append(client)
            readers.append(ser)

            try:
                ready, _, _ = select.select(readers, [], [], 0.5)
            except InterruptedError:
                continue
            except (ValueError, OSError) as exc:
                log.warning("select failed: %s", exc)
                if ser is not None:
                    try:
                        ser.close()
                    except Exception:
                        pass
                    ser = None
                continue

            if server in ready:
                try:
                    new_client, addr = server.accept()
                except BlockingIOError:
                    new_client = None
                    addr = None
                if new_client is not None:
                    new_client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    _set_keepalive(new_client)
                    new_client.setblocking(False)
                    if client is not None:
                        log.info("old client replaced %s", client_addr)
                        _close(client)
                    client = new_client
                    client_addr = addr
                    log.info("new client %s:%s", addr[0], addr[1])

            if client is not None and client in ready:
                try:
                    data = client.recv(buf_size)
                except (ConnectionError, OSError) as exc:
                    log.info("client read error: %s", exc)
                    data = b""
                if not data:
                    log.info("client disconnected %s", client_addr)
                    _close(client)
                    client = None
                    client_addr = None
                else:
                    try:
                        ser.write(data)
                    except Exception as exc:
                        log.warning("serial write failed: %s", exc)
                        try:
                            ser.close()
                        except Exception:
                            pass
                        ser = None
                        continue

            if ser is not None and ser in ready:
                try:
                    waiting = ser.in_waiting or 1
                    data = ser.read(min(waiting, buf_size))
                except Exception as exc:
                    log.warning("serial read failed: %s", exc)
                    try:
                        ser.close()
                    except Exception:
                        pass
                    ser = None
                    continue
                if data and client is not None:
                    try:
                        client.sendall(data)
                    except (ConnectionError, OSError) as exc:
                        log.info("client write error: %s", exc)
                        _close(client)
                        client = None
                        client_addr = None
        return 0
    finally:
        _close(client)
        _close(server)
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Прозрачный TCP <-> USB-serial мост для GRBL (как ESP32 :8881)."
    )
    p.add_argument("--listen", default="0.0.0.0", help="адрес TCP (по умолчанию 0.0.0.0)")
    p.add_argument("--tcp-port", type=int, default=DEFAULT_TCP_PORT, help="TCP-порт (8881)")
    p.add_argument(
        "--serial",
        default="auto",
        help="COM-порт, например /dev/ttyUSB0. auto — первый USB-serial",
    )
    p.add_argument("--baud", type=int, default=DEFAULT_BAUD, help="скорость (115200)")
    p.add_argument("--buffer", type=int, default=BUFFER_SIZE, help="размер блока копирования")
    return p.parse_args(argv)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    if sys.platform == "win32":
        log.error("этот скрипт только для Raspberry Pi / Linux")
        return 2
    return run_bridge(parse_args())


if __name__ == "__main__":
    sys.exit(main())
