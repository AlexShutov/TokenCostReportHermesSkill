#!/usr/bin/env python3
"""Эквивалентное использование кодового плана z.ai (месячное окно).

Логика кредитов — дословно из docs.z.ai/devpack/overview (проверено 2026-10-20):
  кредиты модели = (input x Input_mult + cached x Cached_mult + output x Output_mult) / 10000
  мультипликаторы: GLM-5.3 6.9 / 1.7 / 24 ; GLM-5.3-Flash 2.3 / 0.56 / 8
  во внепиковые часы модель списывается по 50% стандартной ставки
  пик: пн-пт 14:00-18:00 Singapore Standard Time (UTC+8); выходные — внепик весь день
  MCP-инструменты: число вызовов x Output_mult (у нас не отслеживается — ограничение)

Лимиты, которые z.ai публикует: только 5-часовой и недельный
(Light 2000/10000, Pro 12000/60000, Max 28000/140000). Месячного лимита на сайте НЕТ,
поэтому месячный лимит — ВЫВОД: недельный сбрасывается каждые 7 дней от активации,
значит месячное окно = от сохранённого дня начала отсчёта до того же числа
следующего месяца, и месячный лимит = weekly x (дней_в_окне / 7).

Каскад по правилу пользователя: когда месячный лимит Light исчерпан — считаем, что
перешли на Pro (считаем по максимальному лимиту Pro); когда Pro исчерпан — по Max.

Режимы:
  (без флагов)   печать блока плана; файл состояния создаётся при первом запуске
  --set-anchor   сохранить день начала отсчёта месяца (число 1-31 или дата YYYY-MM-DD)
  --plan         текущий план (light|pro|max)
  --model        эквивалентная модель (по умолчанию GLM-5.3-Flash)
  --by-actual-model  считать мультипликаторами фактической модели каждой строки
  --month        месяц отсчёта (YYYY-MM), по умолчанию текущий
  --dry-run      ничего не писать
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import token_cost_db as T

STATE = os.path.expanduser("~/.hermes/state")
PLAN_FILE = os.path.join(STATE, "token_cost_plan.json")

PLANS = {
    "light": {"5h": 2000, "weekly": 10000},
    "pro": {"5h": 12000, "weekly": 60000},
    "max": {"5h": 28000, "weekly": 140000},
}
ORDER = ("light", "pro", "max")

# мультипликаторы по модели (z.ai, таблица Credit Calculation)
MULTIPLIERS = {
    "glm-5.3": {"input": 6.9, "cached": 1.7, "output": 24.0},
    "glm-5.3-flash": {"input": 2.3, "cached": 0.56, "output": 8.0},
}
EQUIV_MODEL = "glm-5.3-flash"          # эквивалентная модель по умолчанию

PEAK_TZ = timezone(timedelta(hours=8))   # Singapore Standard Time
PEAK_DAYS = (0, 1, 2, 3, 4)              # пн-пт
PEAK_START, PEAK_END = 14, 18
OFFPEAK_FACTOR = 0.5

ZAI_PLANS = ("zai_plan", "zai_plan_by_rule", "zai_paid")


def load_state():
    try:
        with open(PLAN_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    os.makedirs(STATE, exist_ok=True)
    st["saved_at"] = T.now_iso()
    with open(PLAN_FILE, "w") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def default_state():
    st = load_state()
    if "anchor_day" not in st:
        st["anchor_day"] = 1          # дефолт: первое число месяца; меняется через --set-anchor
        st["anchor_source"] = "default"
    if "plan" not in st:
        st["plan"] = "light"
    if "model" not in st:
        st["model"] = EQUIV_MODEL
    return st


def anchor_day_from(value):
    """Число 1-31 или дата YYYY-MM-DD -> (день месяца, полная дата якоря)."""
    if isinstance(value, int):
        d = value
    else:
        v = str(value).strip()
        if len(v) == 10 and v[4] == "-" and v[7] == "-":
            dt = datetime.strptime(v, "%Y-%m-%d")
            return dt.day, v
        d = int(v)
    if not 1 <= d <= 31:
        raise ValueError("день якоря — число 1-31 или дата YYYY-MM-DD")
    return d, None


def month_window(anchor_day, ref):
    """Месячное окно: [день якоря ref-месяца, день якоря следующего месяца).

    Если дня якоря нет в ref-месяце (например якорь 31, а в феврале 28 дней),
    берётся последний день месяца — это ограничение, оно называется в блоке.
    """
    start = datetime(ref.year, ref.month, min(anchor_day, days_in_month(ref.year, ref.month)))
    nxt = ref.year + (1 if ref.month == 12 else 0), 1 if ref.month == 12 else ref.month + 1
    end = datetime(nxt[0], nxt[1], min(anchor_day, days_in_month(nxt[0], nxt[1])))
    return start, end, (end - start).days


def days_in_month(year, month):
    import calendar
    return calendar.monthrange(year, month)[1]


def peak_fraction(start, end):
    """Доля окна, приходящаяся на пиковые часы (пн-пт 14:00-18:00 UTC+8).

    Выходные дают 0.0 — они внепик весь день. Точечное окно (start == end,
    backfill-строки) решается по состоянию момента.
    """
    def is_peak(dt):
        return dt.weekday() in PEAK_DAYS and PEAK_START <= dt.hour < PEAK_END

    if end <= start:
        return 1.0 if is_peak(datetime.fromtimestamp(start, PEAK_TZ)) else 0.0
    total = float(end - start)
    peak = 0.0
    guard = 0
    cur = float(start)
    while cur < end and guard < 4000:
        guard += 1
        dt = datetime.fromtimestamp(cur, PEAK_TZ)
        day_start = datetime(dt.year, dt.month, dt.day, tzinfo=PEAK_TZ).timestamp()
        day_end = day_start + 86400.0
        if dt.weekday() in PEAK_DAYS:
            ps = day_start + PEAK_START * 3600.0
            pe = day_start + PEAK_END * 3600.0
            peak += max(0.0, min(end, pe) - max(cur, ps))
        cur = min(end, day_end)
    if guard >= 4000:
        return None            # окно длиннее 4000 дней — не считаем, блок это назовёт
    return peak / total


def credits_for(tokens, mult, frac):
    base = (tokens["input"] * mult["input"] + tokens["cached"] * mult["cached"]
            + tokens["output"] * mult["output"]) / 10000.0
    if frac is None:
        return None
    return base * (frac * 1.0 + (1.0 - frac) * OFFPEAK_FACTOR)


def month_usage(con, anchor_day, ref, model, by_actual=False):
    """Кредиты за месячное окно по инкрементам базы (anomaly в sums не входит)."""
    start, end, days = month_window(anchor_day, ref)
    lo, hi = start.timestamp(), end.timestamp()
    rows = con.execute(
        "SELECT model, plan_class, attribution, window_start, window_end, "
        "input_delta, cache_delta, output_delta FROM increments "
        "WHERE attribution<>'anomaly' AND plan_class IN (" + ",".join("?" * len(ZAI_PLANS)) + ") "
        "AND window_end>=? AND window_start<=?", list(ZAI_PLANS) + [lo, hi]).fetchall()
    tot = {"input": 0, "cached": 0, "output": 0}
    peak_tok = {"input": 0.0, "cached": 0.0, "output": 0.0}
    cr = 0.0
    n = 0
    unknown_model = set()
    point_only = 0
    for r in rows:
        m = MULTIPLIERS.get((r[0] or "").lower(), MULTIPLIERS[model.lower()])
        if not by_actual:
            m = MULTIPLIERS[model.lower()]
        if (r[0] or "").lower() not in MULTIPLIERS:
            unknown_model.add(r[0])
        toks = {"input": r[5] or 0, "cached": r[6] or 0, "output": r[7] or 0}
        for k in tot:
            tot[k] += toks[k]
        f = peak_fraction(r[3], r[4])
        if r[3] == r[4]:
            point_only += 1
        if f is not None:
            for k in peak_tok:
                peak_tok[k] += toks[k] * f
        c = credits_for(toks, m, f)
        if c is None:
            continue
        cr += c
        n += 1
    return {"window": (start, end, days), "rows": n, "tokens": tot,
            "peak_tokens": peak_tok, "credits": cr, "point_rows": point_only,
            "unknown_models": sorted(unknown_model)}


def monthly_cap(plan, days):
    return PLANS[plan]["weekly"] * days / 7.0


def cascade(credits, anchor_day, ref):
    """Light -> Pro -> Max: излишек переносится на следующий план."""
    start, end, days = month_window(anchor_day, ref)
    out = []
    rem = credits
    for name in ORDER:
        cap = monthly_cap(name, days)
        used = min(rem, cap)
        out.append({"plan": name, "cap": cap, "used": used,
                    "pct": 100.0 * used / cap if cap else 0.0})
        rem -= used
        if rem <= 1e-9:
            break
    return out, days


def block(anchor_day, ref, model, by_actual=False, plan=None, con=None):
    """Блок плана. Заголовок и оговорка о выводе печатаются всегда — даже когда
    база недоступна (тогда кредиты не считаются, и блок это называет)."""
    start, end, days = month_window(anchor_day, ref)
    lines = ["Эквивалентный кодовый план z.ai (эквивалентная модель %s)" % model,
             "Месячное окно: %s -> %s (%d дней), день начала отсчёта — %d"
             % (start.date(), end.date(), days, anchor_day)]
    own = None
    try:
        if con is None:
            own = sqlite3.connect(f"file:{T.DB}?mode=ro", uri=True, timeout=10)
            u = month_usage(own, anchor_day, ref, model, by_actual)
        else:
            u = month_usage(con, anchor_day, ref, model, by_actual)
    except Exception as e:
        lines.append("Блок плана z.ai не посчитан: %s" % e)
        lines.append("Месячный лимит выведен из недельного: %d x (%d/7) — на сайте публикуется "
                     "только 5-часовой и недельный, месячного нет" % (PLANS[plan or "light"]["weekly"], days))
        lines.append("MCP-вызовы (Web Search / Reader / Zread) не отслеживаются — кредиты по инструментам "
                     "в блок не включены")
        return "\n".join(lines)
    finally:
        if own is not None:
            own.close()

    m = MULTIPLIERS[model.lower()]
    lines.append("Кредиты за окно: %.1f (вход %.2fM x %.1f, кэш-вход %.2fM x %.2f, выход %.2fM x %.1f)"
                 % (u["credits"], u["tokens"]["input"] / 1e6, m["input"],
                    u["tokens"]["cached"] / 1e6, m["cached"],
                    u["tokens"]["output"] / 1e6, m["output"]))
    res, days2 = cascade(u["credits"], anchor_day, ref)
    for r in res:
        lines.append("использовано %.1f%% %s плана (%.1f кредитов из месячного лимита %.1f)"
                     % (r["pct"], r["plan"].capitalize(), r["used"], r["cap"]))
    lines.append("Месячный лимит выведен из недельного: %d x (%d/7) — на сайте публикуется "
                 "только 5-часовой и недельный, месячного нет"
                 % (PLANS[res[0]["plan"]]["weekly"], days2))
    pk, tot = u["peak_tokens"], u["tokens"]
    if tot["input"] + tot["cached"] + tot["output"] > 0:
        share = (pk["input"] + pk["cached"] + pk["output"]) / (tot["input"] + tot["cached"] + tot["output"])
        lines.append("Пик/офф-пик: %.1f%% токенов окна пришлось на пик (x1.0), остальное — вне пика (x0.5); "
                     "выходные — внепик весь день" % (100.0 * share))
    if u["point_rows"]:
        lines.append("Строк с точечной атрибуцией (backfill/first_seen): %d — их пик определяется по моменту "
                     "last_seen, это оценка" % u["point_rows"])
    if u["unknown_models"]:
        lines.append("Модели без опубликованных мультипликаторов: %s — посчитаны мультипликаторами "
                     "эквивалентной модели" % ", ".join(u["unknown_models"]))
    lines.append("MCP-вызовы (Web Search / Reader / Zread) не отслеживаются — кредиты по инструментам "
                 "в блок не включены")
    return "\n".join(lines)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    dry = "--dry-run" in argv
    st = default_state()
    by_actual = "--by-actual-model" in argv
    ref = datetime.now(PEAK_TZ)

    if "--set-anchor" in argv:
        val = argv[argv.index("--set-anchor") + 1]
        d, full = anchor_day_from(val)
        st["anchor_day"] = d
        if full:
            st["anchor_date"] = full
        st["anchor_source"] = "user"
        if not dry:
            save_state(st)
        print("день начала отсчёта месяца сохранён: %d%s" % (d, " (дата якоря %s)" % full if full else ""))

    if "--plan" in argv:
        p = argv[argv.index("--plan") + 1].lower()
        if p not in PLANS:
            print("план: light|pro|max", file=sys.stderr)
            return 2
        st["plan"] = p
        if not dry:
            save_state(st)

    if "--model" in argv:
        m = argv[argv.index("--model") + 1].lower()
        if m not in MULTIPLIERS:
            print("модель: %s" % ", ".join(MULTIPLIERS), file=sys.stderr)
            return 2
        st["model"] = m
        if not dry:
            save_state(st)

    if "--month" in argv:
        ref = datetime.strptime(argv[argv.index("--month") + 1], "%Y-%m")
        ref = ref.replace(tzinfo=PEAK_TZ)

    if dry:
        print(block(st["anchor_day"], ref, st["model"], by_actual, st["plan"]))
        return 0

    save_state(st)
    print(block(st["anchor_day"], ref, st["model"], by_actual, st["plan"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
