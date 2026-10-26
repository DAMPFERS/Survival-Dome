#!/usr/bin/env python3
"""
Пошаговая проверка фрезера (GRBL через TCP-мост ESP32 / Raspberry Pi).

Запуск:
    python test_milling.py                      # хост из CNC_HOST или 192.168.8.71
    python test_milling.py 192.168.8.71 --debug # с логом обмена (>> / <<)

Есть два режима:
  * меню - любой шаг можно запустить отдельно и в любом порядке;
  * сценарий (пункт 0) - все этапы по очереди, перед каждым спрашивает:
    Enter - выполнить, s - пропустить, q - выйти в меню.

Этапы: статус -> снятие Alarm -> подвод Z -> проверка пробника ->
Z-probe -> (сухая проверка файла $C) -> запуск -> пауза -> продолжение ->
остановка.

Ctrl+C в любой момент - безопасная остановка (feed hold + reset,
позиция сохраняется). Держите руку у аварийной кнопки.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cnc_controller import (  # noqa: E402
    CNCController, CNCError, CNCResetError, CNCState, GCodeFile, Vec3,
)


DEFAULT_HOST = os.getenv("CNC_HOST", "192.168.8.15")
DEFAULT_GCODE_DIR = Path(__file__).resolve().parent / "gcode"

HOLD_WAIT_S = 5.0            # торможение до Hold:0 (как в real_devices.CNCAdapter)
HOLD_SPINDLE_WAIT_S = 3.0    # остановка шпинделя после 0x9E
ABORT_WAIT_S = 40.0          # feed hold + reset


# ============================================================
# Вывод
# ============================================================

def fmt_vec(v: Optional[Vec3]) -> str:
    return "?" if v is None else f"{v[0]:8.3f} {v[1]:8.3f} {v[2]:8.3f}"


def status_line(s: CNCState) -> str:
    job = ""
    if s.milling:
        job = (f" | {'ПАУЗА ' if s.paused else ''}{s.current_line}/{s.total_lines}"
               f" {s.progress:5.1f}%")
    return (f"{s.machine_state:8} | WPos {fmt_vec(s.wpos)} | MPos {fmt_vec(s.mpos)}"
            f" | F={s.feed_rate:.0f} S={s.spindle_rpm:.0f} | Pn:{s.pins or '-'}{job}")


def print_state(s: CNCState) -> None:
    print(f"  Связь:        {'есть' if s.online else 'НЕТ'}")
    print(f"  Состояние:    {s.machine_state}")
    print(f"  WPos (раб.):  {fmt_vec(s.wpos)}")
    print(f"  MPos (маш.):  {fmt_vec(s.mpos)}")
    print(f"  WCO:          {fmt_vec(s.wco)}")
    print(f"  Подача/обор.: F={s.feed_rate:.0f} мм/мин, S={s.spindle_rpm:.0f}")
    print(f"  Пины Pn:      {s.pins or '-'} (пробник {'ЗАМКНУТ' if s.probe_pin else 'разомкнут'})")
    print(f"  Аксессуары A: {s.accessories if s.accessories is not None else '?'}")
    print(f"  Буфер Bf:     planner={s.planner_free} rx={s.rx_free}")
    print(f"  Задание:      {'идёт' if s.milling else 'нет'}"
          f"{' (пауза)' if s.paused else ''} {s.current_file or ''}")
    if s.milling or s.total_lines:
        print(f"  Прогресс:     {s.current_line}/{s.total_lines} ({s.progress:.1f}%)")
    if s.last_probe is not None:
        p = s.last_probe
        print(f"  Посл. probe:  MPos Z={p.z:.3f} ({'успех' if p.success else 'НЕУДАЧА'})")
    if s.alarm is not None:
        print(f"  ALARM:        {s.alarm}")
    if s.error:
        print(f"  Ошибка:       {s.error}")


def header(title: str) -> None:
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def ask(prompt: str, default: Optional[str] = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer if answer else (default or "")


def ask_float(prompt: str, default: float, lo: float, hi: float) -> float:
    while True:
        raw = ask(prompt, f"{default:g}").replace(",", ".")
        try:
            value = float(raw)
        except ValueError:
            print("  Нужно число")
            continue
        if lo <= value <= hi:
            return value
        print(f"  Допустимо от {lo:g} до {hi:g}")


def confirm(prompt: str) -> bool:
    return ask(f"{prompt} (y/n)", "n").lower() in ("y", "yes", "д", "да")


# ============================================================
# Ожидания
# ============================================================

def wait_state(
    cnc: CNCController,
    pred: Callable[[CNCState], bool],
    timeout: float,
    show: bool = True,
) -> bool:
    """Ждёт pred(state), печатая строку статуса. True - дождались."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            cnc.refresh_status(timeout=0.5)
        except CNCError:
            pass
        s = cnc.get_state()
        if show:
            print("\r  " + status_line(s), end="", flush=True)
        if pred(s):
            if show:
                print()
            return True
        time.sleep(0.2)
    if show:
        print()
    return False


def follow_future(cnc: CNCController, future, timeout: float):
    """Показывает статус, пока выполняется команда контроллера; возвращает результат."""
    deadline = time.monotonic() + timeout
    while not future.done():
        if time.monotonic() > deadline:
            print()
            raise TimeoutError(f"Команда не завершилась за {timeout:.0f} с")
        print("\r  " + status_line(cnc.get_state()), end="", flush=True)
        time.sleep(0.25)
    print("\r  " + status_line(cnc.get_state()))
    return future.result()


def monitor(cnc: CNCController, stop_when: Optional[Callable[[CNCState], bool]] = None) -> None:
    """Живой статус до Enter (или до stop_when). Задание при этом продолжается."""
    done = threading.Event()
    finished = threading.Event()

    def loop() -> None:
        while not done.is_set():
            s = cnc.get_state()
            print("\r  " + status_line(s) + "   ", end="", flush=True)
            if stop_when is not None and stop_when(s):
                finished.set()
                print("\n  Условие выполнено - нажмите Enter")
                return
            time.sleep(0.25)

    th = threading.Thread(target=loop, daemon=True)
    print("  (Enter - вернуться; станок продолжит работу)")
    th.start()
    try:
        input()
    finally:
        done.set()
        th.join(timeout=1.0)
    if not finished.is_set():
        print()


# ============================================================
# Шаги
# ============================================================

class MillingTest:
    def __init__(self, cnc: CNCController, gcode_dir: Path):
        self.cnc = cnc
        self.gcode_dir = gcode_dir
        self.selected: Optional[Path] = None

    # ---------------- 1. статус ----------------

    def step_status(self) -> None:
        header("Статус фрезера")
        try:
            self.cnc.refresh_status(timeout=1.0)
        except CNCError as exc:
            print(f"  Свежий отчёт не получен: {exc}")
        print_state(self.cnc.get_state())
        try:
            self.cnc.send_command("$G").result(timeout=5)
            s = self.cnc.get_state()
            print(f"  Модальное:    {s.coord_system}, {'G90' if s.absolute_mode else 'G91'}, "
                  f"шпиндель {'M%d' % s.spindle_direction if s.spindle_on else 'M5'}")
        except Exception as exc:  # noqa: BLE001 - статус всё равно показан
            print(f"  $G не выполнен: {exc}")

    def step_monitor(self) -> None:
        header("Мониторинг")
        monitor(self.cnc)

    # ---------------- 2. Alarm ----------------

    def step_unlock(self) -> None:
        header("Снятие Alarm ($X)")
        s = self.cnc.get_state()
        if not s.machine_state.startswith("Alarm"):
            print(f"  Станок не в Alarm ({s.machine_state}) - $X не нужен")
            if not confirm("  Всё равно отправить $X?"):
                return
        print("  Внимание: после $X позиция может быть неверной.")
        self.cnc.unlock().result(timeout=10)
        wait_state(self.cnc, lambda st: st.machine_state == "Idle", 3.0)
        print("  Готово")

    def step_home(self) -> None:
        header("Homing ($H)")
        print("  Нужны концевики и $22=1, иначе GRBL ответит error:5.")
        if not confirm("  Запустить $H?"):
            return
        mpos = follow_future(self.cnc, self.cnc.home(), 120)
        print(f"  Homing завершён, MPos {fmt_vec(mpos)}")

    # ---------------- 3. подвод ----------------

    def step_jog(self) -> None:
        header("Ручное перемещение (jog) и нули XY")
        print("  Команды:  z-1  /  x10  /  y-5.5  - смещение по оси, мм")
        print("            f300           - подача jog, мм/мин")
        print("            xy0            - текущая точка = X0 Y0 рабочей СК")
        print("            Enter          - закончить")
        feed = 300.0
        while True:
            s = self.cnc.get_state()
            print("  " + status_line(s))
            cmd = input("  jog> ").strip().lower().replace(",", ".")
            if not cmd:
                return
            try:
                if cmd == "xy0":
                    self.cnc.send_command("G10 L20 P0 X0 Y0").result(timeout=5)
                    print("  X0 Y0 записаны")
                elif cmd[0] == "f":
                    feed = float(cmd[1:])
                elif cmd[0] in "xyz":
                    dist = float(cmd[1:])
                    if abs(dist) > 50 and not confirm(f"  Смещение {dist} мм - уверены?"):
                        continue
                    line = f"$J=G91 G21 {cmd[0].upper()}{dist:.3f} F{feed:g}"
                    self.cnc.send_command(line).result(timeout=5)
                    wait_state(self.cnc, lambda st: st.machine_state == "Idle", 60.0)
                else:
                    print("  Не понял команду")
            except ValueError:
                print("  Неверное число")
            except CNCError as exc:
                print(f"  Ошибка: {exc}")

    # ---------------- 4. пробник ----------------

    def step_probe_check(self) -> None:
        header("Проверка пробника (без движения)")
        s = self.cnc.get_state()
        if s.probe_pin:
            print("  Пробник УЖЕ замкнут (Pn:P) - пробинг сразу даст ALARM:4.")
            print("  Проверьте клипсу/провод и инверсию $6.")
            return
        print("  Пробник разомкнут - хорошо.")
        print("  Коснитесь клипсой пластины (или фрезой пластины) в течение 15 с...")
        if wait_state(self.cnc, lambda st: st.probe_pin, 15.0):
            print("  Контакт виден (Pn:P). Отпустите клипсу...")
            if wait_state(self.cnc, lambda st: not st.probe_pin, 15.0):
                print("  Пробник снова разомкнут. Проверка пройдена.")
            else:
                print("  Пробник так и остался замкнутым - проверьте проводку.")
        else:
            print("  Контакт НЕ обнаружен. Проверьте подключение пробника, "
                  "не запускайте Z-probe!")

    # ---------------- 5. Z-probe ----------------

    def step_probe(self) -> None:
        header("Z-probe: поиск нуля Z (G38.2 + G10 L20)")
        s = self.cnc.get_state()
        if s.machine_state != "Idle" or s.milling:
            print(f"  Станок должен быть в Idle без задания (сейчас {s.machine_state})")
            return
        if s.probe_pin:
            print("  Пробник замкнут (Pn:P) - сначала разомкните")
            return
        print("  Перед запуском: фреза не выше <ход> мм над пластиной,")
        print("  клипса на фрезе, пластина под фрезой, рука у аварийной кнопки.")
        distance = ask_float("  Макс. ход вниз, мм", 10.0, 0.1, 30.0)
        feed = ask_float("  Подача поиска, мм/мин", 50.0, 1.0, 300.0)
        plate = ask_float("  Толщина пластины, мм (0 - касание заготовки)", 0.0, 0.0, 30.0)
        second = ask_float("  Подача 2-го касания, мм/мин (0 - без него)", 20.0, 0.0, 100.0)
        retract = ask_float("  Отъезд вверх после касания, мм", 2.0, 0.0, 20.0)
        if not confirm("  Запустить пробинг?"):
            return

        future = self.cnc.probe_z_zero(
            distance=distance, feed=feed, plate_thickness=plate,
            retract=retract, second_pass_feed=second or None,
        )
        try:
            res = follow_future(self.cnc, future, 180)
        except CNCError as exc:
            print(f"  ПРОБИНГ НЕ УДАЛСЯ: {exc}")
            print("  Для продолжения - пункт «Снять Alarm ($X)».")
            return
        s = self.cnc.get_state()
        print(f"  Касание: MPos Z={res.z:.3f}")
        print(f"  Сейчас рабочий Z = {s.z:.3f} (ожидается ≈ {plate + retract:.3f})"
              if s.z is not None else "  Рабочий Z пока неизвестен")

    # ---------------- 6. файл ----------------

    def select_file(self) -> Optional[Path]:
        try:
            files: list[GCodeFile] = self.cnc.list_gcode_files(self.gcode_dir, validate=True)
        except FileNotFoundError as exc:
            print(f"  {exc}")
            return None
        if not files:
            print(f"  В {self.gcode_dir} нет G-code файлов")
            return None
        print(f"  Файлы в {self.gcode_dir}:")
        for i, f in enumerate(files, 1):
            mark = "ok " if f.valid else "ERR"
            print(f"   {i:2d}. [{mark}] {f.name:30s} {f.size:>9d} B  {time.ctime(f.mtime)}")
            if f.error:
                print(f"         {f.error}")
        default = None
        if self.selected is not None:
            for i, f in enumerate(files, 1):
                if f.path == self.selected:
                    default = str(i)
        raw = ask("  Номер файла (Enter - отмена)", default)
        if not raw:
            return None
        try:
            f = files[int(raw) - 1]
        except (ValueError, IndexError):
            print("  Нет такого номера")
            return None
        if not f.valid:
            print("  Файл не прошёл проверку - GRBL его не примет")
            return None
        self.selected = f.path
        return f.path

    def step_check_file(self) -> None:
        header("Сухая проверка файла ($C - GRBL разбирает G-code, станок НЕ двигается)")
        s = self.cnc.get_state()
        if s.milling or s.machine_state != "Idle":
            print(f"  Нужен Idle без задания (сейчас {s.machine_state})")
            return
        path = self.select_file()
        if path is None:
            return
        self.cnc.send_command("$C").result(timeout=5)
        if not wait_state(self.cnc, lambda st: st.machine_state == "Check", 3.0):
            print("  GRBL не перешёл в Check - проверка отменена")
            self._leave_check_mode()
            return
        try:
            self.cnc.start_milling(path)
            wait_state(self.cnc, lambda st: not st.milling, 3600.0)
            s = self.cnc.get_state()
            if s.error:
                print(f"  ОШИБКА в файле: {s.error}")
            else:
                print(f"  Файл разобран без ошибок: {s.total_lines} строк")
        finally:
            self._leave_check_mode()

    def _leave_check_mode(self) -> None:
        # Выключение $C делает soft reset GRBL - ok может не успеть прийти.
        if not self.cnc.get_state().machine_state.startswith("Check"):
            return
        try:
            self.cnc.send_command("$C").result(timeout=5)
        except CNCResetError:
            pass
        except CNCError as exc:
            print(f"  $C (выключение): {exc}")
        if wait_state(self.cnc, lambda st: not st.machine_state.startswith("Check"), 5.0, show=False):
            print(f"  Режим проверки выключен, станок: {self.cnc.get_state().machine_state}")
        else:
            print("  Режим проверки НЕ выключился - отправьте $C ещё раз")

    # ---------------- 7. запуск ----------------

    def step_start(self) -> None:
        header("Запуск фрезеровки")
        s = self.cnc.get_state()
        if s.milling:
            print("  Задание уже идёт")
            monitor(self.cnc, stop_when=lambda st: not st.milling)
            return
        if s.machine_state != "Idle":
            print(f"  Станок должен быть в Idle (сейчас {s.machine_state})")
            return
        path = self.select_file()
        if path is None:
            return
        if s.z is not None and s.z < 0:
            print(f"  Внимание: рабочий Z = {s.z:.3f} (ниже нуля)")
        print("  Сухой прогон: шпиндель выкл., ноль Z на 20-30 мм выше заготовки.")
        if not confirm(f"  Запустить {path.name}?"):
            return
        self.cnc.start_milling(path)
        print("  Задание запущено. Пауза/остановка - из меню.")
        monitor(self.cnc, stop_when=lambda st: not st.milling)
        self._report_job_end()

    def _report_job_end(self) -> None:
        s = self.cnc.get_state()
        if s.milling:
            return
        if s.error:
            print(f"  Задание завершено с ошибкой: {s.error}")
        else:
            print(f"  Задание завершено: {s.total_lines} строк")

    # ---------------- 8. пауза ----------------

    def step_pause(self) -> None:
        header("Пауза (feed hold + остановка шпинделя)")
        s = self.cnc.get_state()
        if not s.milling:
            print("  Нет выполняемого задания")
            return
        if s.paused:
            print("  Уже на паузе")
            return
        self.cnc.pause()
        print("  '!' отправлен, ждём торможения (Hold:0)...")
        if not wait_state(self.cnc, lambda st: st.machine_state == "Hold:0", HOLD_WAIT_S):
            print(f"  Не дошёл до Hold:0 за {HOLD_WAIT_S:.0f} с")
            return
        if not confirm("  Остановить шпиндель (0x9E)?"):
            print("  Пауза без остановки шпинделя")
            return
        if not self.cnc.spindle_stop():
            print("  Шпиндель уже не вращается (по отчёту A:) - 0x9E не отправлен")
            return

        def spindle_stopped(st: CNCState) -> bool:
            if st.accessories is not None:
                return not any(ch in st.accessories for ch in "SC")
            return st.spindle_rpm <= 0.0

        if wait_state(self.cnc, spindle_stopped, HOLD_SPINDLE_WAIT_S):
            print("  Шпиндель остановлен. На продолжении GRBL сам раскрутит его.")
        else:
            print("  Шпиндель НЕ остановился (сервер в этом случае отменяет задание)")

    def step_resume(self) -> None:
        header("Продолжение (cycle start '~')")
        s = self.cnc.get_state()
        if not s.milling or not s.paused:
            print("  Задание не на паузе")
            return
        self.cnc.resume()
        if wait_state(self.cnc, lambda st: st.machine_state == "Run" or not st.milling, 10.0):
            print("  Станок снова в работе")
        else:
            print("  Станок не вернулся в Run за 10 с")

    # ---------------- 9. остановка ----------------

    def step_abort(self) -> None:
        header("Остановка задания (feed hold -> Hold:0 -> soft reset)")
        s = self.cnc.get_state()
        if not s.milling:
            print("  Нет выполняемого задания")
            return
        if not confirm("  Прервать задание?"):
            return
        self.cnc.abort_milling()
        if wait_state(self.cnc, lambda st: not st.milling, ABORT_WAIT_S):
            s = self.cnc.get_state()
            print(f"  Задание остановлено на строке {s.current_line}/{s.total_lines}")
            wait_state(self.cnc, lambda st: st.machine_state in ("Idle",)
                       or st.machine_state.startswith("Alarm"), 5.0)
            s = self.cnc.get_state()
            if s.machine_state.startswith("Alarm"):
                print("  Станок в Alarm - позиция могла быть потеряна, нужен $X/$H")
            else:
                print(f"  Станок: {s.machine_state}, позиция сохранена: WPos {fmt_vec(s.wpos)}")
        else:
            print(f"  Задание не остановилось за {ABORT_WAIT_S:.0f} с - ЖМИТЕ АВАРИЙНУЮ КНОПКУ")

    # ---------------- сценарий ----------------

    def scenario(self) -> None:
        header("Пошаговый сценарий")
        steps: list[tuple[str, Callable[[], None]]] = [
            ("Статус и связь с GRBL", self.step_status),
            ("Снятие Alarm ($X) - если станок в Alarm", self.step_unlock),
            ("Подвод фрезы к пластине (jog), нули XY", self.step_jog),
            ("Проверка пробника без движения", self.step_probe_check),
            ("Z-probe", self.step_probe),
            ("Сухая проверка файла ($C)", self.step_check_file),
            ("Запуск фрезеровки (Enter в мониторе - перейти к паузе)", self.step_start),
            ("Пауза", self.step_pause),
            ("Продолжение", self.step_resume),
            ("Ещё одна пауза перед остановкой", self.step_pause),
            ("Остановка задания", self.step_abort),
            ("Итоговый статус", self.step_status),
        ]
        for i, (title, fn) in enumerate(steps, 1):
            print(f"\n>>> Шаг {i}/{len(steps)}: {title}")
            choice = ask("    Enter - выполнить, s - пропустить, q - в меню", "").lower()
            if choice == "q":
                return
            if choice == "s":
                continue
            run_step(fn)
        print("\nСценарий завершён")


def run_step(fn: Callable[[], None]) -> None:
    """Ошибка шага не должна ронять тест - печатаем и возвращаемся в меню."""
    try:
        fn()
    except CNCError as exc:
        print(f"\n  ОШИБКА контроллера: {exc}")
    except TimeoutError as exc:
        print(f"\n  ТАЙМАУТ: {exc}")
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).exception("Шаг упал")
        print(f"\n  ОШИБКА: {exc}")


# ============================================================
# main
# ============================================================

def main() -> int:
    parser = argparse.ArgumentParser(description="Пошаговая проверка фрезера GRBL")
    parser.add_argument("ip", nargs="?", default=DEFAULT_HOST,
                        help=f"IP моста (по умолчанию {DEFAULT_HOST}, env CNC_HOST)")
    parser.add_argument("--port", type=int, default=int(os.getenv("CNC_PORT", "8881")))
    parser.add_argument("--gcode-dir", type=Path, default=DEFAULT_GCODE_DIR)
    parser.add_argument("--debug", action="store_true", help="лог обмена с GRBL (>> / <<)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(threadName)s: %(message)s",
    )

    cnc = CNCController(ip=args.ip, port=args.port, gcode_dir=args.gcode_dir)
    print(f"Подключение к {args.ip}:{args.port}...")
    try:
        cnc.start()
    except CNCError as exc:
        print(f"Не удалось подключиться: {exc}")
        return 1

    if not wait_state(cnc, lambda s: s.machine_state != "Unknown", 5.0, show=False):
        print("Мост на связи, но GRBL не отвечает на '?'. Проверьте UART/питание станка.")
    test = MillingTest(cnc, args.gcode_dir)

    menu: list[tuple[str, str, Callable[[], None]]] = [
        ("0", "Пошаговый сценарий (все этапы по очереди)", test.scenario),
        ("1", "Статус", test.step_status),
        ("2", "Живой мониторинг", test.step_monitor),
        ("3", "Снять Alarm ($X)", test.step_unlock),
        ("4", "Homing ($H)", test.step_home),
        ("5", "Jog / нули XY", test.step_jog),
        ("6", "Проверка пробника (без движения)", test.step_probe_check),
        ("7", "Z-probe", test.step_probe),
        ("8", "Сухая проверка файла ($C)", test.step_check_file),
        ("9", "Запуск фрезеровки / монитор задания", test.step_start),
        ("p", "Пауза", test.step_pause),
        ("r", "Продолжить", test.step_resume),
        ("x", "Остановить задание", test.step_abort),
    ]
    actions = {key: fn for key, _, fn in menu}

    try:
        while True:
            print("\n" + "-" * 70)
            print("  " + status_line(cnc.get_state()))
            print("-" * 70)
            for key, title, _ in menu:
                print(f"  {key}. {title}")
            print("  q. Выход")
            choice = input("> ").strip().lower()
            if choice == "q":
                if cnc.get_state().milling and not confirm(
                        "Идёт задание - при выходе оно будет остановлено. Выйти?"):
                    continue
                break
            fn = actions.get(choice)
            if fn is None:
                print("Нет такого пункта")
                continue
            run_step(fn)
    except (KeyboardInterrupt, EOFError):
        print("\nCtrl+C - безопасная остановка...")
    finally:
        cnc.stop()   # при идущем задании: feed hold -> reset, позиция сохраняется
    return 0


if __name__ == "__main__":
    sys.exit(main())
