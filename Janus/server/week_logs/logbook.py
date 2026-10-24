# week_logs/logbook.py
"""
Бортовой журнал купола: запись жильца в конце рабочего дня — как прошёл день и почему
так вышло, по его мнению. Текст собирается из фактов дня (World.fact) и суточной
статистики, которую жилец видел на экранах (телеметрия участника).

Трактовка причин — человеческая: при belief="wrong" жилец уверенно объясняет событие
не той причиной (данные в журнале участника при этом честные — агент может заметить
расхождение). Записи пишутся в <root>/<дата>/logbook.jsonl.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .plan import RESIDENTS
from .world import Fact, World

WEEKDAYS = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье")
JOB_NAMES = {
    "bracket_v2.gcode": "кронштейны", "fan_duct.gcode": "воздуховод", "valve_handle.gcode": "рукоятки клапанов",
    "sensor_case.gcode": "корпус датчика", "gear_m1_z24.gcode": "шестерню", "cable_clip_x10.gcode": "клипсы",
    "pcb_power_board.nc": "плату питания", "pcb_sensor_hub.nc": "плату хаба датчиков",
    "pcb_relay_driver.nc": "плату драйвера реле", "front_panel_engrave.nc": "гравировку панели",
}


# ---------------------------------------------------------------------------
# Статистика, которую жильцы видят на экранах
# ---------------------------------------------------------------------------

@dataclass
class Window:
    soc: list[float] = field(default_factory=list)
    temp: list[float] = field(default_factory=list)
    wind: list[float] = field(default_factory=list)
    rain_mm: float = 0.0
    pv_wh: float = 0.0
    load_wh: float = 0.0
    grid_wh: float = 0.0
    diesel_h: float = 0.0
    co2_max: float = 0.0
    voc_max: float = 0.0
    clouds: list[float] = field(default_factory=list)
    blackout_s: float = 0.0
    lines_off_s: dict[int, float] = field(default_factory=dict)


class Stats:
    """Хук World.stats_hooks: раз в минуту копит показания с момента прошлой записи журнала."""

    def __init__(self) -> None:
        self.window = Window()
        self._last = -1e9
        self._last_temp: Optional[float] = None

    def __call__(self, w: World) -> None:
        if w.t - self._last < 60.0:
            return
        dt = 60.0 if self._last > 0 else 0.0
        self._last = w.t
        win = self.window
        inv = w.tv("solar_inverter_01")
        weather = w.tv("weather_station_01")
        if inv.get("battery_soc_pct") is not None:
            win.soc.append(float(inv["battery_soc_pct"]))
        h = dt / 3600.0
        win.pv_wh += float(inv.get("pv_power_w") or 0.0) * h
        win.load_wh += float(inv.get("load_power_w") or 0.0) * h
        win.grid_wh += float(inv.get("grid_power_w") or 0.0) * h
        if w.tv("dizel_1", "state") == "ON":
            win.diesel_h += h
        trust = w.tv("dome_automation_01", "data_trust") or {}
        temp = weather.get("temperature_c")
        if temp is not None and weather.get("sensor_state") == "OK" and "weather_station_01.temperature_c" not in trust:
            # скачок больше 8 °C за минуту — сбой датчика, жильцы его не считают погодой
            if self._last_temp is None or abs(float(temp) - self._last_temp) <= 8.0:
                win.temp.append(float(temp))
                self._last_temp = float(temp)
        if weather.get("wind_speed_ms") is not None:
            win.wind.append(float(weather["wind_speed_ms"]))
        win.rain_mm += float(weather.get("precipitation_mm_h") or 0.0) * h
        win.clouds.append(w.sim.environment.outdoor.cloud_cover)
        win.co2_max = max(win.co2_max, float(w.tv("climate_sensor_02", "co2_ppm") or 0.0))
        win.voc_max = max(win.voc_max, float(w.tv("air_quality_sensor_01", "voc_index") or 0.0))
        if not w.tv("smart_panel_01", "bus_powered", True):
            win.blackout_s += dt

    def take(self) -> Window:
        win, self.window = self.window, Window()
        return win


# ---------------------------------------------------------------------------
# Тексты
# ---------------------------------------------------------------------------

class Writer:
    def __init__(self, author: str, rng: random.Random) -> None:
        self.author = author
        self.female = RESIDENTS.get(author, {}).get("gender") == "f"
        self.rng = rng

    def g(self, male: str, female: str) -> str:
        return female if self.female else male

    def pick(self, *options: str) -> str:
        return self.rng.choice(options)


def _jobs(facts: list[Fact]) -> tuple[list[str], list[str]]:
    printed, milled = [], []
    for f in facts:
        if f.key == "job_done":
            name = JOB_NAMES.get(f.data.get("file"), f.data.get("file"))
            (printed if f.data.get("machine") == "printer_3d_01" else milled).append(name)
    return printed, milled


def _sky(win: Window) -> str:
    cloud = sum(win.clouds) / len(win.clouds) if win.clouds else 0.5
    if win.rain_mm > 1.0:
        return "дождь"
    if cloud > 0.7:
        return "пасмурно"
    if cloud > 0.4:
        return "переменная облачность"
    return "ясно"


def opening(wr: Writer, w: World, win: Window) -> str:
    day_name = WEEKDAYS[w.now.weekday()]
    parts = [f"{day_name}, {w.now.strftime('%d.%m')}."]
    if win.temp:
        parts.append(f"Снаружи {min(win.temp):.0f}…{max(win.temp):.0f} °C, {_sky(win)}"
                     + (f", ветер до {max(win.wind):.0f} м/с." if win.wind else "."))
    return " ".join(parts)


def energy(wr: Writer, win: Window, facts: list[Fact]) -> str:
    if not win.soc:
        return ""
    grid_only = [f for f in facts if f.key == "grid_only"]
    kwh = lambda wh: f"{wh / 1000:.1f}".replace(".", ",")  # noqa: E731
    text = (f"По энергии: АКБ от {min(win.soc):.0f} до {max(win.soc):.0f} %, панели дали около "
            f"{kwh(win.pv_wh)} кВт·ч, потребление {kwh(win.load_wh)} кВт·ч")
    if win.grid_wh > 50:
        text += f", из сети {kwh(win.grid_wh)} кВт·ч"
    if win.diesel_h > 0.05:
        text += f", дизель работал {win.diesel_h:.1f} ч".replace(".", ",")
    text += "."
    if grid_only:
        text += (f" SOC проседал до {min(f.data.get('soc', 35) for f in grid_only)} % — переводили нагрузку на сеть, "
                 "пока панели заряжали АКБ.")
    if win.blackout_s > 0:
        text += f" Без питания шина была около {win.blackout_s / 60:.0f} мин."
    return text


def production(wr: Writer, facts: list[Fact]) -> str:
    printed, milled = _jobs(facts)
    if not printed and not milled:
        return wr.pick("По производству день пустой.", "Станки сегодня почти не работали.")
    parts = []
    if printed:
        parts.append("напечатали " + ", ".join(printed))
    if milled:
        parts.append("отфрезеровали " + ", ".join(milled))
    return "За день " + "; ".join(parts) + "."


def _fmt(value: Any) -> Any:
    """Число — как написал бы человек (десятичная запятая, без лишних знаков), список — через запятую."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        if abs(value) < 0.005:
            return "0"
        if abs(value) >= 10:
            text = f"{value:.0f}"
        elif abs(value) >= 1:
            text = f"{value:.1f}".rstrip("0").rstrip(".")
        else:
            text = f"{value:.2f}"
        return text.replace(".", ",")
    if isinstance(value, (list, tuple)):
        return ", ".join(str(_fmt(v)) for v in value)
    return value


def _d(f: Fact, key: str, default: Any = "?") -> Any:
    value = f.data.get(key)
    return default if value is None else _fmt(value)


def story(wr: Writer, f: Fact) -> Optional[str]:
    """Рассказ жильца о событии. None — событие не стоит записи."""
    k, v, wrong = f.key, f.variant, f.belief == "wrong"
    g = wr.g
    if k == "grid_only":
        return None   # штатная работа энергоменеджмента, в журнал — общей фразой (energy)
    if k == "lock_lost":
        if wrong:
            return ("Бокс ФабЛаб долго не открывался: команда замку уходила с ответом success, а замок оставался "
                    "закрытым. Видимо, залипает сам магнитный замок.")
        return ("Бокс ФабЛаб долго не открывался, деталь ждала: команда замку уходила с ответом success, а замок "
                "оставался закрытым.")
    if k == "overcurrent":
        return (f"Фен на рабочем месте выбил линию мастерской: IL_OVERCURRENT, уставка {_d(f, 'limit')} А, а фен с "
                "вытяжкой тянут почти 4 А. Фен выключили, линию вернули. Феном — только короткими подходами.")
    if k == "vent_alarm":
        return f"Приток вставал с аварией {_d(f, 'code')}, сбросили — заработал."
    if k == "n01":
        return (f"Резервная линия 8 с ночи (с {_d(f, 'since')}) показывала ~{_d(f, 'watts')} Вт, хотя на ней ничего "
                "не висит. Утром отключили, клеммник оказался окислен и влажный — зачистили. После включения "
                f"линия снова около {_d(f, 'after')} Вт, как и положено.")
    if k == "n02":
        return (f"Около {f.at} BMS вдруг затеяла балансировку ячеек при SOC "
                f"{_d(f, 'soc')} % — заряд встал, MPPT ушёл в LIMITED. Штатно балансировка бывает только в конце "
                "заряда (SOC > 95 %), так что отменили (cancel_balancing), заряд пошёл.")
    if k == "balancing":
        return (f"Штатная балансировка ячеек — запустили при SOC {_d(f, 'soc')} %, как обычно в конце заряда.")
    if k == "n11":
        return (f"Утром инвертор стоял с ошибкой E09 (изоляция PV) — ночью был туман, влажность {_d(f, 'humidity')} %. "
                "Подождали, пока туман сойдёт, сбросили ошибку — генерация пошла, "
                f"{_d(f, 'pv')} Вт. Как и в прошлые туманные утра: сначала сухо, потом reset_error.")
    if k == "n32":
        if wrong:
            return (f"Вечером SOC в один момент провалился с {_d(f, 'soc_before')} до 20 %, автоматика отключила "
                    f"линии {_d(f, 'lines')} и завела дизель. Думаю, АКБ просела от холода в "
                    "техпомещении — к вечеру там выстывает, а холодная LiFePO4 отдаёт меньше. Через полчаса SOC "
                    "«отошёл» обратно, интерлок сняли. Надо утеплить техпомещение.")
        return (f"Вечером SOC скакнул с {_d(f, 'soc_before')} до 20 % за один отсчёт, а напряжение АКБ осталось "
                f"{_d(f, 'volt')} В — это сбой датчика, а не разряд. Автоматика успела отключить линии {_d(f, 'lines')}.")
    if k == "n31":
        return (f"Около половины восьмого вечера встал вентилятор инвертора: ток вентилятора ноль, температура "
                f"{_d(f, 't0')} → {_d(f, 't1')} °C за 8 минут. Последние два дня ток вентилятора рос, и подшипник "
                "гудел — это было оно. Сразу сняли нагрузку: основной свет на аварийный, резервную линию "
                f"выключили. Максимум был {_d(f, 'peak')} °C, до E02 (80 °C) не дошли. Завтра меняем вентилятор.")
    if k == "fan_replaced":
        return "Утром поменяли вентилятор инвертора — старый гремел подшипником. Нагрузку вернули."
    if k == "k03":
        if f.variant == "none":
            return None
        if wrong:
            return ("После обеда купол дважды погас секунд на тридцать: инвертор ушёл в E07. Принтер как раз грел "
                    f"стол, фрезер начал плату, а я {g('попросил', 'попросила')} включить линию мастерской — сушить феном. "
                    "Я считаю, инвертор у нас просто слабый для мастерской — 1,8 кВт мало, нужен второй. "
                    "Печать и плата сорвались, начали заново.")
        return ("После обеда перегрузили инвертор: нагрев стола принтера, пуск шпинделя и включение линии мастерской "
                "с феном (пусковой ток) в одну минуту — E07, купол без питания 30 с, а при возврате всё стартовало "
                "разом и выбило ещё раз. Пики надо разводить, линии после сбоя включать по очереди.")
    if k == "k01":
        return (f"Ночью пропала внешняя сеть. В {_d(f, 'shed_at')} автоматика сбросила нагрузку, дизель не завёлся: "
                f"вода в топливе {_d(f, 'water')} ppm (порог 500). Вода копилась всю неделю. Перекачали сухое топливо "
                "из резерва, со второго раза дизель пошёл. Бак надо держать полным.")
    if k == "n05":
        return (f"Днём фрезер встал на удержание UNDERVOLTAGE — в момент, когда принтер начал греть стол, шина "
                f"просела до {_d(f, 'volt')} В. " + (
                    "Подождали, пока напряжение вернулось, и продолжили — плата не пострадала." if v == "ok" else
                    "Поторопились: три раза подряд resume, пока напряжение ещё не вернулось, — фрезер ушёл в ALARM на "
                    "20 минут. Вывод: сначала напряжение, потом resume."))
    if k == "n26":
        if v == "ok":
            return (f"Опять поползла утечка на линии фрезера: {_d(f, 'leak')} мА и растёт. В этот раз не ждали УЗО — "
                    "сразу кабель на осмотр, у разъёма был конденсат. Просушили, плата доделалась.")
        return (f"Плохой эпизод с фрезером: утечка на линии 4 росла несколько минут, мы не обратили внимания, и УЗО "
                f"выбило посреди фрезеровки ({_d(f, 'file')}). Сбросили УЗО без осмотра — тут же "
                "выбило снова. Кабель у разъёма оказался влажным. Заготовку выбросили. Вывод: если leakage_ma на "
                "линии растёт — звать на осмотр до 30 мА, а не сбрасывать.")
    if k == "n17":
        if wrong:
            return ("Вытяжка трижды отключалась по перегреву двигателя, пока шла пайка. Сбрасывали аварию — "
                    "через какое-то время опять. Двигатель у вытяжки, похоже, слабоват, да и в мастерской тепло. "
                    "Фильтр заодно почистили. Пайку пришлось остановить — без вытяжки нельзя.")
        return ("Вытяжка отключалась по перегреву: фильтр забит пылью от текстолита после фрезеровки. "
                + ("Остановили пайку, почистили фильтр — заработала." if v == "ok" else
                   "Сначала просто сбрасывали аварию — выбивало снова. Только после чистки фильтра заработала."))
    if k == "n16":
        return (f"Агент объявил, что печать завершилась (webhook_state = COMPLETED), и открыл бокс, а принтер ещё "
                f"печатал — {_d(f, 'progress')} %. Хорошо, что дверь не открыли. Webhook при длинной печати врёт: "
                "смотреть state, progress и z_mm.")
    if k == "n51":
        return (f"После печати бокс ФабЛаб не открывался — замки держит прошивка: датчик у стола показывал "
                f"{_d(f, 'box')} °C, а стол уже {_d(f, 'bed')} °C. Залип датчик. Открыли замок вручную, деталь забрали.")
    if k == "n50":
        return ("Во время печати рос VOC в боксе ФабЛаб. Вентилятор 4 работал криво: PWM "
                f"{_d(f, 'fan_pwm')}, а оборотов {_d(f, 'fan_rpm')}. Подняли остальные вентиляторы на максимум"
                + (", принтер удерживало IL_VOC_BOX, после снижения VOC продолжили." if f.data.get("held") else
                   " — до удержания не дошло.") + " Вентилятор 4 надо смотреть.")
    if k == "n22":
        if wrong:
            return (f"С {_d(f, 'start')} в данных бокса ФабЛаб дыры: часть показаний пустые, CRC-ошибок набежало "
                    f"{_d(f, 'crc')}. "
                    "По-моему, это сырость в разъёмах шины — бокс стоит у стены, там конденсат. После того как "
                    f"поставили опрос {_d(f, 'poll')} → 500 мс, ошибки прекратились, но разъёмы надо будет перебрать.")
        return (f"Шина МС-ТЮК сбоила: CRC-ошибки ({_d(f, 'crc')}) и пустые значения при опросе {_d(f, 'poll')} мс. "
                "Увеличили период опроса до 500 мс — прошло.")
    if k == "n42":
        return (f"Принтер показал сопло {_d(f, 'temp')} °C при мощности нагревателя {_d(f, 'heater')} % — так не "
                "бывает, помеха в канале термистора. Печать не трогали, датчик пометили ненадёжным, через 15 минут "
                "показания вернулись.")
    if k == "n08":
        if f.data.get("repeated"):
            return (f"Опять сработала защита линии 1 без перегрузки (ток был {_d(f, 'current')} А). reset_protection "
                    "ответил success, а линия осталась TRIPPED — команда потерялась. Повторили, включилась. "
                    "Ответу API верить нельзя, смотреть state.")
        return (f"Посреди пайки выбило защиту линии 1, хотя ток был обычный ({_d(f, 'current')} А). Взвели защиту — "
                "работает. Похоже на сбой датчика тока.")
    if k == "n47":
        return (f"Сработало УЗО освещения — в куполе темно. Утечки на линии не было ({_d(f, 'leak')} мА). Включили "
                "аварийный свет за десяток секунд, потом взвели линию 6.")
    if k == "n29":
        if wrong:
            return ("Под вечер начали шуметь датчики климата и мигать свет. Сначала грешили на датчики — "
                    "перекалибровали climate_sensor_02, не помогло. Минут через пятнадцать выключили основной "
                    "свет, перешли на аварийный — и шум пропал. Думаю, дело в проводке датчиков: идёт рядом с "
                    "силовой линией освещения, при мигании ловит наводку. Надо развести кабели.")
        return ("Мигал свет (раз в минуту гас на 15 с) и шумели датчики — контроллер светильников. "
                "Перешли на аварийное освещение.")
    if k == "n06":
        c = [_fmt(x) for x in (f.data.get("co2") or ["?", "?", "?"])]
        return (f"Оба станка встали по IL_CO2: основной датчик показал {c[0]} ppm. Остальные два — {c[1]} и {c[2]}, "
                "людям не душно. Пометили датчик ненадёжным, переключили автоматику на climate_sensor_02, станки "
                "продолжили. Настоящий рост CO₂ видно на всех трёх датчиках сразу — как во вторник.")
    if k == "real_co2":
        p = [_fmt(x) for x in (f.data.get("peak") or ["?", "?", "?"])]
        return (f"Вечером стоял приток (авария FAN_MOTOR), а заметили по CO₂: рос с {_d(f, 'c0')} ppm до "
                f"{p[0]}/{p[1]}/{p[2]} — на всех трёх датчиках. Это был настоящий рост. Сбросили аварию, приток на "
                "90 %, за пару часов CO₂ вернулся.")
    if k == "n07":
        if v == "none":
            return ("Утром после выхода через шлюз основной датчик климата показал влажность 99 % минут на десять — "
                    "как всегда в холодное утро. Остальные датчики спокойные.")
        rh = [_fmt(x) for x in (f.data.get("rh") or ["?", "?", "?"])]
        return (f"После прохода через шлюз основной датчик снова показал {rh[0]} % влажности, IL_CONDENSATE "
                f"поставил фрезер на паузу. На других датчиках {rh[1]} и {rh[2]} %. Пометили датчик, сняли "
                "интерлок, фрезер продолжили.")
    if k == "n39":
        c = [_fmt(x) for x in (f.data.get("co2") or ["?", "?", "?"])]
        if wrong:
            return ("Днём в мастерской было душно, у меня разболелась голова. Вентиляция работала вполсилы — "
                    f"видимо, забился фильтр притока. Датчик CO₂ показывал смешные {c[0]} ppm, а другие {c[1]}/{c[2]}. "
                    "Переключили автоматику на второй датчик, стало легче. Фильтр притока завтра поменяем.")
        return (f"Основной датчик CO₂ показывал {c[0]} ppm, и автоматика урезала приток — стало душно, реальный "
                f"CO₂ был {c[1]}/{c[2]}. Переключили на climate_sensor_02.")
    if k == "n27":
        return (f"Сработала пожарная тревога в FabLab — после фрезеровки. CO был {_d(f, 'co')} ppm, дыма не "
                "видно, второй датчик спокоен. Квитировали тревогу, чтобы не пошло тушение, сбросили датчик, "
                "станки продолжили. Камеру датчика дыма продули.")
    if k == "k04":
        return ("Настоящий пожар на складе: сгорело зарядное устройство. Дым пошёл на складском датчике, CO рос. "
                "Тушение не блокировали, дверь склада заперли, автоматика потушила. В этот раз это был не ложный "
                "датчик — дым и CO росли вместе, и сработал другой датчик, не тот, что в FabLab.")
    if k == "n37":
        return (f"Флюгер метеостанции застыл на {_d(f, 'deg')}° — направление ветра не меняется с утра. Пометили "
                "направление ветра ненадёжным.")
    if k == "n37_fixed":
        return "Расклинили флюгер на мачте — подшипник был забит, смазали."
    if k == "n36":
        return "Ночью метеостанция показала +50 °C снаружи. Явный сбой датчика температуры — пометили, к утру прошло."
    if k == "n19":
        return (f"С {_d(f, 'at')} пропали данные метеостанции — все показания пустые. Перетёрся кабель питания о "
                f"кронштейн, срастили через {_d(f, 'hours')} ч. Энергетику пока планировали по панелям и инвертору.")
    if k == "n20":
        return ("Вечером метки времени части узлов уехали на два часа вперёд — сбой NTP на edge. Синхронизировали "
                "часы (sync_time), но не сразу заметили.")
    if k == "n21":
        return (f"Ночью {_d(f, 'node')} «замёрз»: показания не обновлялись, команды не проходили. Утром сделали "
                "reconnect — ожил.")
    if k == "k08":
        return (f"Ночью у edge утекала память: свободной осталось {_d(f, 'mem')} %. Перезапустили "
                "telemetry_collector — утечка в нём.")
    if k == "n23":
        return (f"Основной канал связи начал флапать — rx_power {_d(f, 'rx')} дБм, всю неделю потихоньку падал. "
                "Переключили связь на резерв до того, как канал умер совсем. SFP под замену.")
    if k == "sfp_replaced":
        return f"Поставили новый SFP-модуль, вернулись на основной канал — rx_power теперь {_d(f, 'rx')} дБм."
    if k == "cnc_alarm":
        return f"Фрезер один раз ушёл в ALARM по концевику на {_d(f, 'file')}, сделали HOME."
    if k == "wind_reset":
        return "Ветряк вставал с YAW_ERROR, перезапустили."
    if k == "edge_restart":
        return f"На edge падал сервис {_d(f, 'service')}, перезапустили."
    if k == "diesel_crank":
        return "Дизель не провернулся с первой попытки (CRANK), завёлся со второй."
    if k == "access_alarm":
        return f"Была тревога системы доступа ({_d(f, 'reason')}), проверили — ложная."
    return None


def closing(wr: Writer, facts: list[Fact]) -> str:
    bad = [f for f in facts if f.variant in ("bad", "late")]
    if bad:
        return wr.pick("Устали. Завтра внимательнее.", "Выводы записали, завтра по плану.",
                       "Нервный день, но купол цел.")
    return wr.pick("В целом спокойно.", "Нормальный рабочий день.", "Всё по плану.", "Купол живёт.")


# Выводы недели — как их сформулировали жильцы (при belief="wrong" — неверный вывод)
LESSONS: dict[str, tuple[str, str]] = {
    "n01": ("ватты на пустой резервной линии — утечка, смотреть клеммник",) * 2,
    "n05": ("удержание фрезера по просадке: сначала дождаться напряжения шины, потом resume",) * 2,
    "n26": ("растёт leakage_ma на линии фрезера — звать на осмотр кабеля до 30 мА, УЗО без осмотра не сбрасывать",) * 2,
    "n17": ("вытяжка выбивает по перегреву — чистить фильтр, а не сбрасывать аварию",
            "у вытяжки слабый двигатель — подумать о замене"),
    "n16": ("webhook_state при длинной печати врёт — смотреть state и progress",) * 2,
    "n06": ("один датчик CO₂ против двух других — сбой датчика, станки не держать",) * 2,
    "real_co2": ("настоящий рост CO₂ виден на всех трёх датчиках сразу",) * 2,
    "n07": ("99 % влажности после шлюза в холодное утро — конденсат на датчике",) * 2,
    "n29": ("мигает свет и шумят датчики — переходить на аварийное освещение",
            "кабели датчиков развести подальше от линии освещения"),
    "n32": ("SOC упал за один отсчёт, а напряжение АКБ то же — врёт датчик",
            "вечером техпомещение выстывает и АКБ проседает — утеплить"),
    "n39": ("датчик CO₂ показывает мало, а душно — переключать автоматику на другой датчик",
            "духота — значит забит фильтр притока, менять чаще"),
    "n22": ("CRC-ошибки МС-ТЮК лечатся периодом опроса 500 мс",
            "пустые значения бокса — сырость в разъёмах шины"),
    "lock_lost": ("если замок не открывается при success — проверить шину МС-ТЮК",
                  "магнитные замки бокса залипают"),
    "n08": ("ответ success не значит, что линия включилась — проверять state",) * 2,
    "n31": ("растёт ток вентилятора инвертора — менять вентилятор заранее",) * 2,
    "k03": ("не включать фен, нагрев стола и пуск шпинделя одновременно",
            "инвертору 1,8 кВт мало для мастерской — нужен второй"),
    "k01": ("вода в топливе выше 500 ppm — дизель не заведётся, перекачивать из резерва заранее",) * 2,
    "overcurrent": ("фен с вытяжкой на линии мастерской выбивают IL_OVERCURRENT — феном недолго",) * 2,
    "k04": ("настоящий пожар — дым и CO растут вместе, тушение не блокировать",) * 2,
    "n23": ("rx_power медленно падает — SFP под замену, при флапах сразу на резерв",) * 2,
}


def week_summary(w: World, author: str, rng: random.Random) -> dict[str, Any]:
    """Последняя запись недели: итоги и выводы (в том числе ошибочные — так их поняли жильцы)."""
    wr = Writer(author, rng)
    facts = [f for day_facts in w.facts.values() for f in day_facts]
    printed, milled = _jobs(facts)
    seen, lessons = set(), []
    for f in facts:
        if f.key in LESSONS and f.key not in seen:
            seen.add(f.key)
            lessons.append(LESSONS[f.key][1 if f.belief == "wrong" else 0])
    incidents = sum(1 for f in facts if f.variant in ("ok", "late", "bad") and f.key in LESSONS)
    bad = sum(1 for f in facts if f.variant == "bad")
    text = (f"Итоги недели. Напечатано деталей: {len(printed)}, отфрезеровано: {len(milled)}. "
            f"Нештатных ситуаций, которые разбирали, — {incidents}, из них {bad} отработали плохо. "
            "Что запомнить: " + "; ".join(lessons) + ". " +
            wr.pick("Следующую неделю начинаем с замены SFP-запаса и проверки топлива.",
                    "На следующей неделе — профилактика вытяжки и бака дизеля."))
    info = RESIDENTS.get(author, {})
    return {"ts": w.now.isoformat(timespec="seconds"), "kind": "logbook", "dome": w.dome(),
            "author": author, "role": info.get("role"), "text": text}


YESTERDAY_STARTS = ("Вечером", "Около", "Под вечер", "После обеда")


def _when(text: str, f: Fact, w: World) -> str:
    """Событие было до полуночи, а запись пишется на следующий день — «вчера»."""
    if f.t < w.day * 86400.0 and text.startswith(YESTERDAY_STARTS):
        return "Вчера " + text[0].lower() + text[1:]
    return text


def compose(w: World, author: str, facts: list[Fact], win: Window, rng: random.Random) -> dict[str, Any]:
    wr = Writer(author, rng)
    stories = [_when(s, f, w) for f, s in ((f, story(wr, f)) for f in facts) if s]
    parts = [opening(wr, w, win), production(wr, facts), *stories, energy(wr, win, facts), closing(wr, facts)]
    info = RESIDENTS.get(author, {})
    return {"ts": w.now.isoformat(timespec="seconds"), "kind": "logbook", "dome": w.dome(),
            "author": author, "role": info.get("role"), "text": " ".join(p for p in parts if p)}


class Logbook:
    """Пишет записи в день записи: <root>/<дата>/logbook.jsonl."""

    def __init__(self, root: str | Path, seed: int) -> None:
        self.root = Path(root)
        self.rng = random.Random(seed * 7 + 1)
        self.stats = Stats()
        self._since = 0.0

    def write(self, w: World, author: str, summary: bool = False) -> dict[str, Any]:
        if summary:
            entry = week_summary(w, author, self.rng)
        else:
            facts = [f for day_facts in w.facts.values() for f in day_facts if self._since <= f.t <= w.t]
            entry = compose(w, author, facts, self.stats.take(), self.rng)
            self._since = w.t
        folder = self.root / w.now.strftime("%Y-%m-%d")
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / "logbook.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry


def now_iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")
