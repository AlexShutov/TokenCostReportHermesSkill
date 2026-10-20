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

import attributions as A          # одни общие определения атрибуций (база и ядро плана)

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
PAID_CLASS = "zai_paid"

# Дефолт дня начала отсчёта месяца при установке навыка с нуля (запрос пользователя,
# 2026-10-20): 19-е число — чтобы данные, уже имеющиеся в базе, попали в расчёт.
DEFAULT_ANCHOR_DAY = 19


def as_tuple(tokens):
    """(input, cached, output) — порядок, которого ждёт прежний расчёт токенов."""
    return (tokens["input"], tokens["cached"], tokens["output"])


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

    def describe(self):
        return ("интегральная оценка по дню (модель %s): будний день 4/24 часа пика, суббота и "
                "воскресенье — весь день вне пика; момент конкретного вызова не используется" % self.name)


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

    def describe(self):
        return ("доля временного окна в пике (модель %s) — это приближение: z.ai списывает по моменту "
                "каждого запроса, а база хранит окна между наблюдениями, поэтому пик делится по времени "
                "окна пропорционально, а не поштучно" % self.name)

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

    def known_models(self):
        return sorted(self._table)

    def fallback_model(self):
        return self._fallback


# --- лимиты планов -------------------------------------------------------------
class PlanRegistry:
    def __init__(self, plans=None):
        self._plans = dict(plans or PLANS)

    def order(self):
        return list(self._plans.keys())

    def known(self, plan):
        return plan in self._plans

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
        """Строки окна: dict(session_id, model, plan_class, attribution, window_start,
        window_end, tokens)."""

    def anomalies(self, window) -> int:
        """Anomaly-строки окна: они не входят в суммы, но остаются в базе."""
        return 0

    def unknown_count(self, window) -> int:
        """Строки окна с незаписанным провайдером (plan unknown) — в оценку не входят."""
        return 0

    def before_window(self, window) -> tuple:
        """Строки базы ДО начала окна: (число, токены). Они относятся к предыдущему
        месячному окну и в оценку не входят — блок обязан это назвать."""
        return (0, 0)

    def gaps(self, window) -> tuple:
        """Выбрасываемые интервалы окна: (число, токены). Токены за них реальны, но
        время не атрибутируется, поэтому в оценку они не входят — блок обязан это
        назвать, иначе цифра выглядит как полная."""
        return (0, 0)


class InMemoryIncrementSource(IncrementSource):
    def __init__(self, rows):
        self._rows = list(rows)

    def rows(self, window):
        return [r for r in self._rows if window.contains(r["window_end"])
                and r["attribution"] not in A.EXCLUDED_ATTRIBUTION]

    def anomalies(self, window):
        return sum(1 for r in self._rows if r["attribution"] == "anomaly"
                   and window.contains(r["window_end"]))

    def unknown_count(self, window):
        return sum(1 for r in self._rows if r["plan_class"] == "unknown"
                   and window.contains(r["window_end"]))

    def before_window(self, window):
        rows = [r for r in self._rows if r["window_end"] < window.start.timestamp()]
        tot = zero_tokens()
        for r in rows:
            add_tokens(tot, r["tokens"])
        return (len(rows), sum(tot.values()))

    def gaps(self, window):
        rows = [r for r in self._rows if r["attribution"] == "gap"
                and window.contains(r["window_end"])]
        tot = zero_tokens()
        for r in rows:
            add_tokens(tot, r["tokens"])
        return (len(rows), sum(tot.values()))


class SqliteIncrementSource(IncrementSource):
    def __init__(self, conn_factory, plan_classes=ZAI_PLANS):
        self._conn_factory = conn_factory
        self._plan_classes = tuple(plan_classes)

    def rows(self, window):
        con = self._conn_factory()
        try:
            ph = ",".join("?" * len(self._plan_classes))
            cur = con.execute(
                "SELECT session_id, model, plan_class, attribution, window_start, window_end, "
                "input_delta, cache_delta, output_delta FROM increments "
                "WHERE " + A.excluded_sql() + " AND plan_class IN (" + ph + ") "
                "AND window_end>=? AND window_start<=?",
                list(A.EXCLUDED_ATTRIBUTION) + list(self._plan_classes)
                + [window.start.timestamp(), window.end.timestamp()])
            rows = []
            for r in cur.fetchall():
                rows.append({"session_id": r[0], "model": r[1], "plan_class": r[2],
                             "attribution": r[3], "window_start": r[4], "window_end": r[5],
                             "tokens": {"input": r[6] or 0, "cached": r[7] or 0,
                                        "output": r[8] or 0}})
            return rows
        finally:
            con.close()

    def anomalies(self, window):
        con = self._conn_factory()
        try:
            return con.execute(
                "SELECT COUNT(*) FROM increments WHERE attribution='anomaly' "
                "AND window_end>=? AND window_start<=?",
                (window.start.timestamp(), window.end.timestamp())).fetchone()[0]
        finally:
            con.close()

    def gaps(self, window):
        con = self._conn_factory()
        try:
            ph = ",".join("?" * len(self._plan_classes))
            cur = con.execute(
                "SELECT COUNT(*), COALESCE(SUM(input_delta),0), COALESCE(SUM(cache_delta),0), "
                "COALESCE(SUM(output_delta),0) FROM increments WHERE attribution='gap' "
                "AND plan_class IN (" + ph + ") AND window_end>=? AND window_start<=?",
                list(self._plan_classes) + [window.start.timestamp(), window.end.timestamp()])
            r = cur.fetchone()
            # форма возврата обязана совпадать с контрактом и с in-memory-путём:
            # раньше один путь отдавал число, другой словарь, и отчёт ломался на втором
            return (r[0], (r[1] or 0) + (r[2] or 0) + (r[3] or 0))
        finally:
            con.close()

    def unknown_count(self, window):
        con = self._conn_factory()
        try:
            return con.execute(
                "SELECT COUNT(*) FROM increments WHERE plan_class='unknown' "
                "AND window_end>=? AND window_start<=?",
                (window.start.timestamp(), window.end.timestamp())).fetchone()[0]
        finally:
            con.close()

    def before_window(self, window):
        con = self._conn_factory()
        try:
            ph = ",".join("?" * len(self._plan_classes))
            row = con.execute(
                "SELECT COUNT(*), COALESCE(SUM(input_delta+cache_delta+output_delta),0) "
                "FROM increments WHERE plan_class IN (" + ph + ") AND window_end < ?",
                list(self._plan_classes) + [window.start.timestamp()]).fetchone()
            return (row[0], row[1])
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

    def split(self, rows, caps):
        """caps — [(план, лимит)] по порядку каскада.

        Возвращает (within по планам, overflow, details), где details —
        (строка, доля переполнения): из неё группируют дни и сессии прежнего
        расчёта токенов. Строки идут хронологически: более ранняя строка не может
        «съесть» лимит более поздней.
        """
        within = [zero_tokens() for _ in caps]
        accs = [0.0] * len(caps)
        overflow = zero_tokens()
        details = []
        for r in sorted(rows, key=lambda x: x["window_end"]):
            c = self._row_credits(r)
            if c is None:
                details.append((r, None, None))
                continue
            rem = c
            for i, (_plan, cap) in enumerate(caps):
                # room обязан быть неотрицательным: когда лимит уже исчерпан,
                # отрицательный room даёт отрицательную долю и «внутрь» попадают
                # отрицательные токены
                room = max(0.0, cap - accs[i])
                if rem <= room + 1e-12:
                    add_tokens(within[i], r["tokens"], (rem / c) if c > 0 else 1.0)
                    accs[i] += rem
                    rem = 0.0
                    break
                if room > 0:
                    add_tokens(within[i], r["tokens"], room / c)
                    accs[i] += room
                    rem -= room
            if rem > 1e-12:
                add_tokens(overflow, r["tokens"], rem / c)
            details.append((r, (rem / c) if c > 0 else 0.0, c))
        return within, overflow, details

    def _peak_day(self, row):
        # день строки — по времени окна (окно может быть точечным)
        return datetime.fromtimestamp(row["window_end"], PEAK_TZ).date()

    def _row_credits(self, row):
        return self._calc.credits(row["tokens"], self._mult.for_model(row["model"]),
                                  self._peak.share(self._peak_day(row),
                                                   row["window_start"], row["window_end"]))


def group_overflow(details, key_fn):
    """Переполнение по группам (день, сессия): только та часть строк, которая не
    влезла ни в один лимит каскада. Прежний расчёт токенов работает именно такими
    группами, поэтому отчёт по дням и по сессии строится из этого разбора.

    details — (строка, доля переполнения, кредиты) из OverflowLedger.split.
    """
    out = {}
    for r, frac, _credits in details:
        if frac is None or frac <= 0.0:
            continue
        k = key_fn(r)
        out.setdefault(k, zero_tokens())
        add_tokens(out[k], r["tokens"], frac)
    return out


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


def resolve_anchor(state, today, default_day=None):
    """Якорь месяца: сохранённый, иначе дефолт. Возвращает (date, source).

    Три ветви, каждая обязана называться в блоке:
      saved            — пользователь установил свой день
      default_derived  — дефолт, заданный настройкой навыка (DEFAULT_ANCHOR_DAY)
      default          — день 1, когда дефолта нет
    Сохранённый якорь всегда важнее дефолта.
    """
    raw = state.get("anchor_date") or state.get("anchor_day")
    if raw:
        d = parse_anchor(raw, today)
        # исходный запрос сохраняется отдельно: после корректировки дата уже
        # «правильная», и блок не смог бы назвать, что день был скорректирован
        req = state.get("anchor_requested") or raw
        src = "saved" + (" (clamped)" if clamped(req, d) else "")
        return d, src
    if default_day is not None:
        day = min(int(default_day), calendar.monthrange(today.year, today.month)[1])
        return date(today.year, today.month, day), "default_derived"
    return date(today.year, today.month, 1), "default"


def parse_anchor(value, today=None):
    """Число 1-31 или дата YYYY-MM-DD -> date (день начала отсчёта месяца).

    `today` — опорная дата для числового дня: без неё берётся фактическое
    сегодня, и `--month` с сохранённым числом даёт окно не того месяца (баг K1).
    """
    if isinstance(value, (date, datetime)):
        return value if isinstance(value, date) else value.date()
    v = str(value).strip()
    usage = "день начала отсчёта — число 1-31 или дата YYYY-MM-DD"
    if len(v) == 10 and v[4] == "-" and v[7] == "-":
        try:
            y, m, d = int(v[:4]), int(v[5:7]), int(v[8:10])
        except ValueError:
            raise ValueError(usage)
        if not 1 <= m <= 12:
            raise ValueError("месяц начала отсчёта — 1-12")
        return date(y, m, min(d, calendar.monthrange(y, m)[1]))
    try:
        d = int(v)
    except ValueError:
        raise ValueError(usage)
    if not 1 <= d <= 31:
        raise ValueError(usage)
    ref = today or datetime.now(PEAK_TZ).date()
    return date(ref.year, ref.month, min(d, calendar.monthrange(ref.year, ref.month)[1]))


def clamped(value, resolved):
    """Числовой якорь не может указать день, которого в месяце нет — навык берёт
    последний доступный. Это приближение, и оно обязано называться в блоке."""
    if isinstance(value, (date, datetime)):
        return False
    v = str(value).strip()
    if len(v) == 10 and v[4] == "-" and v[7] == "-":
        try:
            return resolved.day != int(v[8:10])
        except ValueError:
            return False
    try:
        return resolved.day != int(v)
    except ValueError:
        return False


# --- переход плана -------------------------------------------------------------
class TransitionNotifier:
    def __init__(self, registry):
        self._registry = registry

    def message(self, previous, current):
        """None — перехода нет. Строка — сообщение о переполнении."""
        if current is None:
            return "План Max переполнен, переходим на пересчет токенов."
        # состояние пустое (первый запуск) — отсчёт начинался с первого плана каскада,
        # иначе первое же переполнение молча не сообщается
        if previous is None:
            previous = self._registry.order()[0]
        if previous == current:
            return None
        return "Превышен план %s, перешли на %s" % (previous.capitalize(), current.capitalize())


# --- доставляемый блок ---------------------------------------------------------
class PlanReport:
    def __init__(self, window, cascade, calculator, multipliers, registry, peak_model,
                 state=None, source=None, anchor_source="default", notifier=None,
                 by_actual=False, mcp_note=None, store=None):
        self.window = window
        self.cascade = cascade
        self.calculator = calculator
        self.multipliers = multipliers
        self.registry = registry
        self.peak_model = peak_model
        self.state = state or {}
        self.source = source
        self.anchor_source = anchor_source
        self.notifier = notifier
        self.by_actual = by_actual
        self.mcp_note = mcp_note or ("MCP-вызовы (Web Search / Reader / Zread) не отслеживаются — "
                                     "кредиты по инструментам в блок не включены")
        self.store = store
        self.degraded = None

    def first_line(self, credits):
        plan = self.cascade.current_plan(credits)
        if plan is None:
            return "Равно 100.0%% плана %s на Z.ai" % MAX_PLAN.capitalize()
        steps = self.cascade.evaluate(credits)
        step = steps[-1]
        return "Равно %.1f%% плана %s на Z.ai" % (step.pct, step.plan.capitalize())

    def rows(self):
        if self.source is None:
            self.degraded = "источник строк недоступен (dry-run)"
            return []
        try:
            return self.source.rows(self.window)
        except Exception as e:
            # блок деградирует честно: заголовок и оговорка о выводе печатаются,
            # кредиты не считаются, и блок это называет
            self.degraded = str(e)
            return []

    def credits(self, rows):
        total = 0.0
        for r in rows:
            if r["plan_class"] == PAID_CLASS:
                continue          # оплаченные z.ai — не план, в процент не входит
            if r["attribution"] in A.EXCLUDED_ATTRIBUTION:
                continue          # anomaly и выбрасываемый интервал — не входят в суммы
            day = datetime.fromtimestamp(r["window_end"], PEAK_TZ).date()
            mult = (self.multipliers.for_model(r["model"]) if self.by_actual
                    else self.multipliers.for_model(self.multipliers.fallback_model()))
            c = self.calculator.credits(r["tokens"], mult,
                                        self.peak_model.share(day, r["window_start"], r["window_end"]))
            if c is not None:
                total += c
        return total

    def paid_rows(self, rows):
        return [r for r in rows if r["plan_class"] == PAID_CLASS]

    def build(self, rows=None, credits=None):
        rows = self.rows() if rows is None else rows
        # кредиты считаются ДО использования: иначе ветка «строк нет» падает,
        # потому что credits остаётся None
        credits = self.credits(rows) if credits is None else credits
        lines = [self.first_line(credits)]
        lines.append("Эквивалентный кодовый план z.ai (эквивалентная модель %s)"
                     % self.multipliers.fallback_model())
        lines.append("Месячное окно: %s 00:00 -> %s 00:00 (%d дней), день начала отсчёта — %s (%s)"
                     % (self.window.start.date(), self.window.end.date(), self.window.days(),
                        self.window.anchor.day, self.anchor_source))
        if "clamped" in self.anchor_source:
            lines.append("Якорь скорректирован: запрошенного числа в этом месяце нет, взят последний "
                         "доступный день (%d) — это ограничение метода" % self.window.anchor.day)
        if self.window.clamped:
            lines.append("Якорь %d-го числа в этом месяце нет — взято последнее число месяца "
                         "(это ограничение метода)" % self.window.anchor.day)
        if self.degraded:
            lines.append("Блок плана z.ai не посчитан: %s" % self.degraded)
            lines.append("Месячный лимит выведен из недельного: %d x (%d/7) — на сайте публикуется "
                         "только 5-часовой и недельный, месячного нет"
                         % (self.registry.weekly(self.state.get("plan") or "light"), self.window.days()))
            lines.append(self.mcp_note)
            return lines
        if rows:
            tot = zero_tokens()
            for r in rows:
                if r["plan_class"] == PAID_CLASS:
                    continue
                add_tokens(tot, r["tokens"])
            if self.by_actual:
                lines.append("Кредиты за окно: %.1f (по мультипликаторам фактической модели каждой строки)"
                             % credits)
            else:
                m = self.multipliers.for_model(self.multipliers.fallback_model())
                lines.append("Кредиты за окно: %.1f (вход %.2fM x %.1f, кэш-вход %.2fM x %.2f, "
                             "выход %.2fM x %.1f)" % (credits, tot["input"] / 1e6, m["input"],
                                                       tot["cached"] / 1e6, m["cached"],
                                                       tot["output"] / 1e6, m["output"]))
            paid = self.paid_rows(rows)
            if paid:
                ptok = sum(sum(r["tokens"].values()) for r in paid)
                lines.append("Оплаченные z.ai в окне: %d строк (%.2fM токенов) — списаны деньгами, "
                             "не планом; в процент плана не входят" % (len(paid), ptok / 1e6))
            for step in self.cascade.evaluate(credits):
                lines.append("использовано %.1f%% %s плана (%.1f кредитов из месячного лимита %.1f)"
                             % (step.pct, step.plan.capitalize(), step.used, step.cap))
        else:
            lines.append("Строк плана в окне нет — кредиты не считались")
        lines.append("Месячный лимит выведен из недельного: %d x (%d/7) — на сайте публикуется "
                     "только 5-часовой и недельный, месячного нет"
                     % (self.registry.weekly(self.cascade.current_plan(credits) or MAX_PLAN),
                        self.window.days()))
        lines.append("Пик/офф-пик — " + self.peak_model.describe())
        point = sum(1 for r in rows if r["attribution"] in ("backfill", "first_seen"))
        if point:
            lines.append("Строк с точечной атрибуцией (backfill/first_seen): %d — их день определяется "
                         "по моменту last_seen, это оценка" % point)
        n_unknown = self.source.unknown_count(self.window) if self.source is not None else 0
        if n_unknown:
            lines.append("В окне %d строк с незаписанным провайдером (plan unknown) — они не входят "
                         "в оценку плана" % n_unknown)
        n_anom = self.source.anomalies(self.window) if self.source is not None else 0
        if n_anom:
            lines.append("Anomaly-строки в окне: %d — они не входят в суммы (строка уменьшилась или "
                         "исчезла между наблюдениями), но остаются в базе" % n_anom)
        if self.source is not None:
            n_gap, tok = self.source.gaps(self.window)
            if n_gap:
                lines.append("Выбрасываемые интервалы в окне: %d (%.2fM токенов) — длиннее порога %d мин "
                             "(крон не сработал, машина спала, агент завис): токены реальны, но время "
                             "за них не атрибутируется, поэтому в оценку они не входят; порог — "
                             "переменная TOKEN_COST_MAX_WINDOW" % (n_gap, tok / 1e6, A.MAX_WINDOW_SECONDS // 60))
        if self.source is not None:
            n_before, tok_before = self.source.before_window(self.window)
            if n_before:
                lines.append("Строк базы ДО начала окна: %d (%.2fM токенов) — они относятся к "
                             "предыдущему месячному окну и в оценку не входят" % (n_before, tok_before / 1e6))
        unknown = sorted({r["model"] for r in rows if not self.multipliers.known(r["model"])})
        if unknown:
            lines.append("Модели без опубликованных мультипликаторов: %s — посчитаны мультипликаторами "
                         "эквивалентной модели" % ", ".join(unknown))
        msg = (self.notifier.message(self.state.get("plan"), self.cascade.current_plan(credits))
               if self.notifier is not None else None)
        if msg:
            lines.append(msg)
        lines.append(self.mcp_note)
        return lines
