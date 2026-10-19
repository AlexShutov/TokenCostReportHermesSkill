#!/usr/bin/env python3
"""Ядро блока эквивалентного кодового плана z.ai.

Только stdlib, Python 3.9+ (stock python3 на macOS — 3.9). Сеть не трогается.

Правила — дословно из docs.z.ai/devpack/overview (проверено чтением страницы):
  кредиты модели = (input x Input_mult + cached x Cached_mult + output x Output_mult) / 10000
  мультипликаторы: GLM-5.3 = 6.9/1.7/24 ; GLM-5.3-Flash = 2.3/0.56/8
  вне пика модель списывается по 50% стандартной ставки
  пик: пн-пт 14:00-18:00 Singapore Standard Time (UTC+8); выходные — внепик весь день
  MCP-инструменты: вызов x Output_mult — не отслеживаются, в расчёт не входят

Месячного лимита на сайте нет: публикуется только 5-часовой и недельный.
Месячный лимит выводится: недельный сбрасывается каждые 7 дней от активации,
месячное окно — от дня начала отсчёта (00:00) до того же числа следующего месяца,
лимит = weekly x (дней_в_окне / 7). Это вывод, а не опубликованное число, и блок
обязан это называть.

Разделение ответственности:
  PeakModel            — как считать долю пика (интегральная по дню / по окну)
  CreditCalculator     — формула кредитов
  MultiplierRegistry   — мультипликаторы по модели
  PlanRegistry         — лимиты планов и вывод месячного лимита
  MonthWindow          — месячное окно от дня начала отсчёта
  IncrementSource      — чтение инкрементов базы (абстракция, не конкретная БД)
  PlanCascade          — распределение кредитов по планам Light -> Pro -> Max
  OverflowLedger       — переполнение Max: только излишек токенов считается деньгами
  PlanStateStore       — сохранённый день начала отсчёта и текущий план
  TransitionNotifier   — сообщение о переходе плана
  PlanReport           — доставляемый текст (первая строка «Равно X% плана Y на Z.ai»)
"""
import calendar
import json
import os
import sqlite3
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

PEAK_TZ = timezone(timedelta(hours=8))          # Singapore Standard Time
PEAK_DAYS = (0, 1, 2, 3, 4)                     # пн-пт
PEAK_START, PEAK_END = 14, 18
OFFPEAK_FACTOR = 0.5

MULTIPLIERS = {
    "glm-5.3": {"input": 6.9, "cached": 1.7, "output": 24.0},
    "glm-5.3-flash": {"input": 2.3, "cached": 0.56, "output": 8.0},
}
EQUIV_MODEL = "glm-5.3-flash"

PLANS = {
    "light": {"5h": 2000, "weekly": 10000},
    "pro": {"5h": 12000, "weekly": 60000},
    "max": {"5h": 28000, "weekly": 140000},
}
PLAN_ORDER = ("light", "pro", "max")
MAX_PLAN = "max"
ZAI_PLANS = ("zai_plan", "zai_plan_by_rule", "zai_paid")


def zero_tokens():
    return {"input": 0.0, "cached": 0.0, "output": 0.0}


def add_tokens(acc, tokens, factor=1.0):
    for k in acc:
        acc[k] += (tokens[k] or 0) * factor
    return acc


# --- пик / офф-пик -------------------------------------------------------------
class PeakModel(ABC):
    """Как считать долю пика. implementations принимают один и тот же интерфейс."""

    name = "abstract"

    @abstractmethod
    def share(self, day, window_start=None, window_end=None) -> Optional[float]:
        """Доля пика для дня (или для окна, если реализация её использует)."""


class DayIntegralPeakModel(PeakModel):
    """Интегральная оценка по дню: доля пиковых часов в дне.

    Пн-пт: 4 часа пика из 24 = 1/6. Выходной: 0 — внепик весь день.
    Момент конкретного вызова не используется: база хранит окна между
    наблюдениями, и поштучный расчёт по моменту невозможен.
    """

    name = "day-integral"

    def share(self, day, window_start=None, window_end=None):
        if day.weekday() in PEAK_DAYS:
            return (PEAK_END - PEAK_START) / 24.0
        return 0.0


class WindowPeakModel(PeakModel):
    """Прежняя логика: доля конкретного окна в пике. Точечное окно решается по моменту."""

    name = "window"

    def _is_peak(self, epoch):
        dt = datetime.fromtimestamp(epoch, PEAK_TZ)
        return dt.weekday() in PEAK_DAYS and PEAK_START <= dt.hour < PEAK_END

    def share(self, day, window_start=None, window_end=None):
        if window_start is None or window_end is None:
            return DayIntegralPeakModel().share(day)
        if window_end <= window_start:
            return 1.0 if self._is_peak(window_start) else 0.0
        total = float(window_end - window_start)
        peak = 0.0
        guard = 0
        cur = float(window_start)
        while cur < window_end and guard < 4000:
            guard += 1
            dt = datetime.fromtimestamp(cur, PEAK_TZ)
            day_start = datetime(dt.year, dt.month, dt.day, tzinfo=PEAK_TZ).timestamp()
            day_end = day_start + 86400.0
            if dt.weekday() in PEAK_DAYS:
                ps = day_start + PEAK_START * 3600.0
                pe = day_start + PEAK_END * 3600.0
                peak += max(0.0, min(window_end, pe) - max(cur, ps))
            cur = min(window_end, day_end)
        if guard >= 4000:
            return None            # окно длиннее 4000 дней — не считаем
        return peak / total


# --- формула кредитов ----------------------------------------------------------
class CreditCalculator(ABC):
    @abstractmethod
    def credits(self, tokens, mult, share):
        """Кредиты за порцию токенов при доле пика share."""


class StandardCreditCalculator(CreditCalculator):
    def credits(self, tokens, mult, share):
        base = (tokens["input"] * mult["input"] + tokens["cached"] * mult["cached"]
                + tokens["output"] * mult["output"]) / 10000.0
        if share is None:
            return None
        return base * (share * 1.0 + (1.0 - share) * OFFPEAK_FACTOR)


class MultiplierRegistry:
    def __init__(self, table=None, fallback_model=EQUIV_MODEL):
        self._table = dict(table or MULTIPLIERS)
        self._fallback = fallback_model

    def for_model(self, model):
        return self._table.get((model or "").lower(), self._table[self._fallback])

    def known(self, model):
        return (model or "").lower() in self._table

    def fallback_model(self):
        return self._fallback


# --- лимиты планов -------------------------------------------------------------
class PlanRegistry:
    def __init__(self, plans=None):
        self._plans = dict(plans or PLANS)

    def order(self):
        return list(self._plans.keys())

    def weekly(self, plan):
        return self._plans[plan]["weekly"]

    def five_hour(self, plan):
        return self._plans[plan]["5h"]

    def monthly_cap(self, plan, days):
        """Месячный лимит — вывод из недельного: на сайте месячного лимита нет."""
        return self.weekly(plan) * days / 7.0


class MonthWindow:
    """Месячное окно: от дня начала отсчёта (00:00) до того же числа следующего месяца."""

    def __init__(self, anchor_date, tz=PEAK_TZ):
        if isinstance(anchor_date, datetime):
            anchor_date = anchor_date.date()
        self.anchor = anchor_date
        self.tz = tz
        self.start = datetime(anchor_date.year, anchor_date.month, anchor_date.day, 0, 0, tzinfo=tz)
        ny, nm = (anchor_date.year + 1, 1) if anchor_date.month == 12 else (anchor_date.year, anchor_date.month + 1)
        day = min(anchor_date.day, calendar.monthrange(ny, nm)[1])
        self.clamped = day != anchor_date.day
        self.end = datetime(ny, nm, day, 0, 0, tzinfo=tz)

    def days(self):
        return (self.end - self.start).days

    def contains(self, epoch):
        return self.start.timestamp() <= epoch < self.end.timestamp()

    def day_of(self, epoch):
        return datetime.fromtimestamp(epoch, self.tz).date()


# --- источник инкрементов ------------------------------------------------------
class IncrementSource(ABC):
    @abstractmethod
    def rows(self, window) -> List[dict]:
        """Строки окна: dict(model, plan_class, attribution, window_start, window_end, tokens)."""


class InMemoryIncrementSource(IncrementSource):
    def __init__(self, rows):
        self._rows = list(rows)

    def rows(self, window):
        return [r for r in self._rows if window.contains(r["window_end"])]


class SqliteIncrementSource(IncrementSource):
    def __init__(self, conn_factory, plan_classes=ZAI_PLANS):
        self._conn_factory = conn_factory
        self._plan_classes = tuple(plan_classes)

    def rows(self, window):
        con = self._conn_factory()
        try:
            ph = ",".join("?" * len(self._plan_classes))
            cur = con.execute(
                "SELECT model, plan_class, attribution, window_start, window_end, "
                "input_delta, cache_delta, output_delta FROM increments "
                "WHERE attribution<>'anomaly' AND plan_class IN (" + ph + ") "
                "AND window_end>=? AND window_start<=?",
                list(self._plan_classes) + [window.start.timestamp(), window.end.timestamp()])
            rows = []
            for r in cur.fetchall():
                rows.append({"model": r[0], "plan_class": r[1], "attribution": r[2],
                             "window_start": r[3], "window_end": r[4],
                             "tokens": {"input": r[5] or 0, "cached": r[6] or 0, "output": r[7] or 0}})
            return rows
        finally:
            con.close()


# --- каскад планов и переполнение ---------------------------------------------
class Step:
    def __init__(self, plan, cap, used, pct):
        self.plan, self.cap, self.used, self.pct = plan, cap, used, pct


class PlanCascade:
    def __init__(self, registry, window):
        self._registry = registry
        self._window = window

    def evaluate(self, credits):
        steps = []
        rem = credits
        for plan in self._registry.order():
            cap = self._registry.monthly_cap(plan, self._window.days())
            used = min(rem, cap)
            pct = 100.0 * used / cap if cap else 0.0
            steps.append(Step(plan, cap, used, pct))
            rem -= used
            if rem <= 1e-9:
                break
        return steps

    def current_plan(self, credits):
        """План, на котором сейчас находится использование.

        Если кредиты переполнили Max, плана больше нет — дальше идёт денежный
        пересчёт токенов, и current_plan возвращает None.
        """
        steps = self.evaluate(credits)
        if len(steps) < len(self._registry.order()):
            return steps[-1].plan
        # все три плана исчерпаны
        last = steps[-1]
        return None if last.pct >= 100.0 - 1e-9 else last.plan


class OverflowLedger:
    """Переполнение Max: строки в хронологическом порядке, кредиты накапливаются.

    Всё, что пришло после месячного лимита Max, — переполнение. Деньги считаются
    только по этому излишку, а не по общему количеству токенов.
    """

    def __init__(self, calculator, multipliers, peak_model):
        self._calc = calculator
        self._mult = multipliers
        self._peak = peak_model

    def split(self, rows, max_cap):
        within, overflow = zero_tokens(), zero_tokens()
        acc = 0.0
        for r in sorted(rows, key=lambda x: x["window_end"]):
            share = self._peak.share(self._peak_day(r), r["window_start"], r["window_end"])
            c = self._calc.credits(r["tokens"], self._mult.for_model(r["model"]), share)
            if c is None:
                continue
            if acc + c <= max_cap:
                add_tokens(within, r["tokens"])
            else:
                # room обязан быть неотрицательным: когда лимит уже исчерпан,
                # отрицательный room даёт отрицательную долю и «внутрь» попадают
                # отрицательные токены
                room = max(0.0, max_cap - acc)
                frac = room / c if c > 0 else 1.0
                add_tokens(within, r["tokens"], frac)
                add_tokens(overflow, r["tokens"], 1.0 - frac)
            acc += c
        return within, overflow, acc

    def _peak_day(self, row):
        # день строки — по времени окна (окно может быть точечным)
        return datetime.fromtimestamp(row["window_end"], PEAK_TZ).date()


# --- состояние ---------------------------------------------------------------
class PlanStateStore:
    def __init__(self, path):
        self._path = path

    def load(self):
        try:
            with open(self._path) as f:
                return json.load(f)
        except Exception:
            return {}

    def save(self, state):
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        state["saved_at"] = datetime.now(timezone.utc).isoformat()
        with open(self._path, "w") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

    def path(self):
        return self._path


def parse_anchor(value):
    """Число 1-31 или дата YYYY-MM-DD -> date (день начала отсчёта месяца)."""
    if isinstance(value, (date, datetime)):
        return value if isinstance(value, date) else value.date()
    v = str(value).strip()
    if len(v) == 10 and v[4] == "-" and v[7] == "-":
        return datetime.strptime(v, "%Y-%m-%d").date()
    d = int(v)
    if not 1 <= d <= 31:
        raise ValueError("день начала отсчёта — число 1-31 или дата YYYY-MM-DD")
    today = datetime.now(PEAK_TZ).date()
    return date(today.year, today.month, min(d, calendar.monthrange(today.year, today.month)[1]))


# --- переход плана -------------------------------------------------------------
class TransitionNotifier:
    def __init__(self, registry):
        self._registry = registry

    def message(self, previous, current):
        """None — перехода нет. Строка — сообщение о переполнении."""
        if previous == current:
            return None
        if current is None:
            return "План Max переполнен, переходим на пересчет токенов."
        if previous is None:
            return None
        return "Превышен план %s, перешли на %s" % (previous.capitalize(), current.capitalize())


# --- доставляемый блок ---------------------------------------------------------
class PlanReport:
    def __init__(self, window, cascade, calculator, multipliers, registry, peak_model,
                 state=None, source=None):
        self.window = window
        self.cascade = cascade
        self.calculator = calculator
        self.multipliers = multipliers
        self.registry = registry
        self.peak_model = peak_model
        self.state = state or {}
        self.source = source

    def first_line(self, credits):
        plan = self.cascade.current_plan(credits)
        if plan is None:
            return "Равно 100.0%% плана %s на Z.ai" % MAX_PLAN.capitalize()
        steps = self.cascade.evaluate(credits)
        step = steps[-1]
        return "Равно %.1f%% плана %s на Z.ai" % (step.pct, step.plan.capitalize())

    def rows(self):
        return self.source.rows(self.window) if self.source is not None else []

    def credits(self, rows):
        total = 0.0
        for r in rows:
            day = datetime.fromtimestamp(r["window_end"], PEAK_TZ).date()
            c = self.calculator.credits(r["tokens"], self.multipliers.for_model(r["model"]),
                                        self.peak_model.share(day, r["window_start"], r["window_end"]))
            if c is not None:
                total += c
        return total

    def build(self, rows=None, credits=None):
        rows = self.rows() if rows is None else rows
        # кредиты считаются ДО использования: иначе ветка «строк нет» падает,
        # потому что credits остаётся None
        credits = self.credits(rows) if credits is None else credits
        lines = [self.first_line(credits)]
        lines.append("Эквивалентный кодовый план z.ai (эквивалентная модель %s)"
                     % self.multipliers.fallback_model())
        lines.append("Месячное окно: %s 00:00 -> %s 00:00 (%d дней), день начала отсчёта — %s"
                     % (self.window.start.date(), self.window.end.date(), self.window.days(),
                        self.window.anchor.day))
        if self.window.clamped:
            lines.append("Якорь %d-го числа в этом месяце нет — взято последнее число месяца "
                         "(это ограничение метода)" % self.window.anchor.day)
        if rows:
            tot = zero_tokens()
            for r in rows:
                add_tokens(tot, r["tokens"])
            m = self.multipliers.for_model(self.multipliers.fallback_model())
            if credits is None:
                credits = self.credits(rows)
            lines.append("Кредиты за окно: %.1f (вход %.2fM x %.1f, кэш-вход %.2fM x %.2f, "
                         "выход %.2fM x %.1f)" % (credits, tot["input"] / 1e6, m["input"],
                                                   tot["cached"] / 1e6, m["cached"],
                                                   tot["output"] / 1e6, m["output"]))
            for step in self.cascade.evaluate(credits):
                lines.append("использовано %.1f%% %s плана (%.1f кредитов из месячного лимита %.1f)"
                             % (step.pct, step.plan.capitalize(), step.used, step.cap))
        else:
            lines.append("Строк плана в окне нет — кредиты не считались")
        lines.append("Месячный лимит выведен из недельного: %d x (%d/7) — на сайте публикуется "
                     "только 5-часовой и недельный, месячного нет"
                     % (self.registry.weekly(self.cascade.current_plan(credits) or MAX_PLAN),
                        self.window.days()))
        lines.append("Пик/офф-пик — интегральная оценка по дню (модель %s): пн-пт 4/24 часа, "
                     "выходной — весь день вне пика; момент конкретного вызова не используется"
                     % self.peak_model.name)
        point = sum(1 for r in rows if r["attribution"] in ("backfill", "first_seen"))
        if point:
            lines.append("Строк с точечной атрибуцией (backfill/first_seen): %d — их день определяется "
                         "по моменту last_seen, это оценка" % point)
        unknown = sorted({r["model"] for r in rows if not self.multipliers.known(r["model"])})
        if unknown:
            lines.append("Модели без опубликованных мультипликаторов: %s — посчитаны мультипликаторами "
                         "эквивалентной модели" % ", ".join(unknown))
        lines.append("MCP-вызовы (Web Search / Reader / Zread) не отслеживаются — кредиты по "
                     "инструментам в блок не включены")
        return lines
