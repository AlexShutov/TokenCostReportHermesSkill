#!/usr/bin/env python3
"""Сравнительная цена токенов: сколько сэкономлено на локальных LLM.

Считаются ТОЛЬКО токены, обработанные локальными моделями (strata, LM Studio,
ollama и любой провайдер, кроме z.ai). Токены z.ai coding plan не учитываются —
они идут по лимиту подписки, а не за деньги. Экономия = сколько стоили бы
локальные токены по ценам z.ai (GLM-5.3-Flash), если бы их пришлось покупать.

Правило пользователя (2026-10-18): при переключении на z.ai / z.ai coding plan
токены не считаются; статистика копится в «копилке» (ledger) и не зависит от
смены модели и удаления сессий.

Режимы:
  (без флагов)  печать отчёта; копилка обновляется (это безопасно)
  --dry-run     печать вообще без записей (ручной тест)
  --commit      автоматический запуск 21:00: живой курс «Плати по миру»,
                запись базы суточной дельты. Другие запуски базу не двигают.
  --tick        фоновый тик крона (10 мин, no-agent): обновить копилку,
                в stdout ничего не печатать (пустой stdout = тихо).
"""
import json, os, re, sqlite3, sys, time, urllib.request
from datetime import datetime, timezone

import token_cost_db as T          # append-only база: runs / usage_snapshot / increments

CHAT_ID = "362122320"
DB = os.path.expanduser("~/.hermes/state.db")
SNAP = os.path.expanduser("~/.hermes/state/token_cost_snapshots.json")
LEDGER = os.path.expanduser("~/.hermes/state/token_cost_ledger.json")
PRICE_CACHE = os.path.expanduser("~/.hermes/state/token_cost_prices.json")
RATE_CACHE = os.path.expanduser("~/.hermes/state/token_cost_rate.json")
LABEL = "GLM-5.3-Flash"
URL = "https://docs.z.ai/guides/overview/pricing"
RATE_URL = "https://platipomiru.com"          # курс «Плати по миру», не курс ЦБ
TARIFF = "Для подписок"                        # тариф, по которому считает рубли
DEFAULT_RATE = 95.35                           # последний известный курс — рубли печатаются всегда
DEFAULTS = {"input": 0.15, "cached": 0.03, "output": 0.50}   # только если нет ни сайта, ни кэша
UA = "Mozilla/5.0 (Hermes token-cost-report; contact: local agent)"
KEYS = ("input", "cache", "output")

# локальный = всё, что НЕ z.ai (strata/custom, LM Studio, ollama, bionic и т.д.)
ZAI_WHERE = "(billing_provider='zai' OR billing_base_url LIKE '%api.z.ai%')"


def conn():
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)


def resolve_session(c):
    row = c.execute("SELECT entry_json FROM gateway_routing WHERE session_key=?",
                    (f"agent:main:telegram:dm:{CHAT_ID}",)).fetchone()
    if row:
        try:
            sid = json.loads(row[0]).get("session_id")
            if sid:
                return sid
        except Exception:
            pass
    r = c.execute("SELECT id FROM sessions WHERE chat_id=? ORDER BY started_at DESC LIMIT 1",
                  (CHAT_ID,)).fetchone()
    return r[0] if r else None


def totals(c, sid=None):
    """Локальные токены (не z.ai): по всем сессиям или по одной, все модели."""
    q = ("SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(cache_read_tokens),0), "
         "COALESCE(SUM(output_tokens),0) FROM session_model_usage WHERE NOT " + ZAI_WHERE)
    args = []
    if sid:
        q += " AND session_id=?"
        args.append(sid)
    return c.execute(q, args).fetchone()


def zai_total(c):
    """Сколько токенов ушло на z.ai — в отчёт не входят, но показать стоит."""
    q = ("SELECT COALESCE(SUM(input_tokens+cache_read_tokens+output_tokens),0) "
         f"FROM session_model_usage WHERE {ZAI_WHERE}")
    return c.execute(q).fetchone()[0]


def bump_ledger(cur):
    """Копилка: суммирует только прирост с прошлого раза; удаления сессий
    (просадка cur) не обнуляют накопленное и не считаются минусом. Idempotent:
    повторный запуск между тиками просто не добавит лишнего."""
    d = {}
    if os.path.exists(LEDGER):
        try:
            d = json.load(open(LEDGER))
        except Exception:
            d = {}
    last = [int(d.get("last_seen", {}).get(k, 0) or 0) for k in KEYS]
    cum = [int(d.get("cumulative", {}).get(k, 0) or 0) for k in KEYS]
    cum = [cum[i] + max(0, cur[i] - last[i]) for i in range(3)]
    json.dump({"last_seen": dict(zip(KEYS, cur)),
               "cumulative": dict(zip(KEYS, cum)),
               "updated_at": datetime.now(timezone.utc).isoformat()},
              open(LEDGER, "w"), ensure_ascii=False)
    return cum, not d


def parse_price(cell):
    cell = cell.strip()
    if not cell or cell == "-":
        return None
    if "free" in cell.lower():
        return 0.0
    m = re.search(r"\$?([0-9]+(?:\.[0-9]+)?)", cell)
    return float(m.group(1)) if m else None


def fetch_prices():
    """Строка таблицы цен z.ai для GLM-5.3-Flash; колонки определяем по заголовку таблицы."""
    try:
        req = urllib.request.Request(URL, headers={"User-Agent": UA})
        html = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "replace")
    except Exception:
        return None, "сайт не ответил"
    for table in re.findall(r"<table[^>]*>.*?</table>", html, re.S):
        header = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<th[^>]*>(.*?)</th>", table, re.S)]
        if "Input" not in header or "Output" not in header or "Cached Input" not in header:
            continue
        idx = {name: i for i, name in enumerate(header)}
        for tr in re.findall(r"<tr>.*?</tr>", table, re.S):
            cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
            if not cells or cells[0] != LABEL:
                continue
            p = {"input": parse_price(cells[idx["Input"]]),
                 "cached": parse_price(cells[idx["Cached Input"]]),
                 "output": parse_price(cells[idx["Output"]])}
            if all(v is not None for v in p.values()):
                return p, None
    return None, "модель не найдена на странице"


def load_cache():
    try:
        d = json.load(open(PRICE_CACHE))
        pk = ("input", "cached", "output")          # ключи цен, не путать с KEYS (ключи токенов)
        return {k: float(v) for k, v in d.items() if k in pk} or None
    except Exception:
        return None


def fetch_rate():
    """Курс «Плати по миру» по тарифу TARIFF (курс сервиса, не ЦБ)."""
    try:
        req = urllib.request.Request(RATE_URL, headers={"User-Agent": UA})
        html = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "replace")
    except Exception:
        return None, "сайт не ответил"
    text = re.sub(r"<[^>]+>", " ", html)
    # курс ищем только рядом со словом «курс» — иначе можно схватить цену самой карты
    pat = re.compile(r"курс[^\d]{0,20}(\d+[.,]\d{1,2})\s*(?:₽|руб)")
    for m in re.finditer(re.escape(TARIFF), text):
        hit = pat.search(text[m.start():m.start() + 300])
        if hit:
            return float(hit.group(1).replace(",", ".")), None
    return None, f"курс тарифа «{TARIFF}» не найден на странице"


def load_rate_cache():
    try:
        d = json.load(open(RATE_CACHE))
        v = d.get("rate")
        return float(v) if v else None
    except Exception:
        return None


def money(x, rate):
    # рубли печатаются всегда: курс есть всегда (живой, сохранённый или DEFAULT_RATE)
    return f"${x:.2f} (₽{x * rate:.2f})"


def plural(n, forms):
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return forms[0]
    if 2 <= n10 <= 4 and n100 not in (12, 13, 14):
        return forms[1]
    return forms[2]


def duration(seconds):
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d} {plural(d, ('день', 'дня', 'дней'))}")
    if h:
        parts.append(f"{h} {plural(h, ('час', 'часа', 'часов'))}")
    if m:
        parts.append(f"{m} {plural(m, ('минута', 'минуты', 'минут'))}")
    return " ".join(parts) if parts else "0 минут"


def cost(toks, p):
    return toks[0] / 1e6 * p["input"] + toks[1] / 1e6 * p["cached"] + toks[2] / 1e6 * p["output"]


def block(toks, p, rate):
    return "\n".join([
        f"- вход: {toks[0]/1e6:.2f}M × ${p['input']:.2f} = {money(toks[0]/1e6*p['input'], rate)}",
        f"- кэш-вход: {toks[1]/1e6:.2f}M × ${p['cached']:.2f} = {money(toks[1]/1e6*p['cached'], rate)}",
        f"- выход: {toks[2]/1e6:.2f}M × ${p['output']:.2f} = {money(toks[2]/1e6*p['output'], rate)}",
        f"- итого {money(cost(toks, p), rate)}",
    ])


def delta(base, cur):
    if base is None:
        return (0, 0, 0)
    return tuple(max(0, cur[i] - int(base.get(k, 0) or 0)) for i, k in enumerate(KEYS))


def main():
    dry = "--dry-run" in sys.argv
    commit = "--commit" in sys.argv          # только автоматический запуск по расписанию
    tick = "--tick" in sys.argv              # фоновый тик: обновить копилку и молчать
    c = conn()
    sid = resolve_session(c)

    cur_all = totals(c)                       # локальные токены по всем сессиям
    if dry:
        cumulative = [int(d) for d in cur_all]
        ledger_fresh = False
    else:
        cumulative, ledger_fresh = bump_ledger(cur_all)

    if tick:
        # тик: без сети, значения из кэша; запись в базу и пустой stdout (тихо)
        con_db = T.connect()
        T.record_run(con_db, "tick", os.environ.get("TOKEN_COST_CRON_JOB"),
                     os.environ.get("HERMES_PROFILE") or "default", sid,
                     load_cache() or DEFAULTS, load_rate_cache() or DEFAULT_RATE,
                     "cache", cumulative, None)
        return

    cur_sess = totals(c, sid) if sid else (0, 0, 0)
    zai = zai_total(c)
    row = c.execute("SELECT model, started_at FROM sessions WHERE id=?",
                    (sid,)).fetchone() if sid else None
    started = row[1] if row else None

    snap = {}
    if os.path.exists(SNAP):
        try:
            snap = json.load(open(SNAP))      # база суточной дельты = значения копилки на 21:00
        except Exception:
            snap = {}

    live, err = fetch_prices()
    note = None
    price_source = "default"
    if live:
        p = live
        price_source = "live"
        try:
            os.makedirs(os.path.dirname(PRICE_CACHE), exist_ok=True)
            json.dump({"model": LABEL, **p, "fetched_at": datetime.now(timezone.utc).isoformat()},
                      open(PRICE_CACHE, "w"), ensure_ascii=False)
        except Exception:
            pass
    else:
        p = load_cache() or DEFAULTS
        price_source = "cache" if load_cache() else "default"
        note = f"Цены с сайта взять не удалось ({err}) — использованы последние сохранённые."

    rate_note = None
    rate_source = "default"
    if commit:
        # курс обновляется только на автоматическом запуске по расписанию
        rate, rate_err = fetch_rate()
        if rate:
            rate_source = "live"
            try:
                os.makedirs(os.path.dirname(RATE_CACHE), exist_ok=True)
                json.dump({"tariff": TARIFF, "rate": rate,
                           "fetched_at": datetime.now(timezone.utc).isoformat()},
                          open(RATE_CACHE, "w"), ensure_ascii=False)
            except Exception:
                pass
        else:
            rate = load_rate_cache() or DEFAULT_RATE
            rate_source = "cache" if load_rate_cache() else "default"
            rate_note = (f"Курс «Плати по миру» обновить не удалось ({rate_err}) — "
                          f"использован сохранённый {rate:.2f}.")
    else:
        # прямой запуск: курс не трогаем, берём сохранённый
        rate = load_rate_cache() or DEFAULT_RATE
        rate_source = "cache" if load_rate_cache() else "default"

    # --- база (append-only): каждое значение каждого запуска + таймстамп + провайдер/модель ---
    con_db = None
    if dry:
        if os.path.exists(T.DB):
            con_db = sqlite3.connect(f"file:{T.DB}?mode=ro", uri=True, timeout=10)
    else:
        con_db = T.connect()
        T.record_run(con_db, "tick" if tick else ("commit" if commit else "manual"),
                     os.environ.get("TOKEN_COST_CRON_JOB"),
                     os.environ.get("HERMES_PROFILE") or "default",
                     sid, p, rate, rate_source, cumulative, started)

    now = time.time()
    db_note = None
    if con_db is not None:
        day = T.window_sum(con_db, "local", now - 86400, now)
        prev = con_db.execute("SELECT run_epoch FROM runs WHERE job='commit' ORDER BY run_epoch DESC LIMIT 1").fetchone()
        prev_commit = prev[0] if prev else None
        since_commit = T.window_sum(con_db, "local", prev_commit, now) if prev_commit else day
        if prev_commit:
            hours = (now - prev_commit) / 3600
        else:
            db_note = ("База ещё не имеет запуска 21:00 — среднее за час считается от полуночи, "
                       "а суточное окно — только по строкам, записанным в базу.")
            lt = time.localtime()
            hours = (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec) / 3600
        db_note = ((db_note + " ") if db_note else "") + (
            "Суточное окно = сумма инкрементов по временным окнам из базы (append-only, ничего не удаляется); "
            "строки, существовавшие до создания базы, атрибутированы точкой last_seen — это оценка.")
    else:
        day = delta(snap.get("cumulative"), cumulative)   # старый путь: дельта от базы снимка
        since_commit = day
        db_note = "База недоступна — сутки посчитаны дельтой от снимка 21:00 (старый путь)."
        hours = None
        if snap.get("saved_at"):
            try:
                ts = datetime.fromisoformat(snap["saved_at"]).timestamp()
                hours = (time.time() - ts) / 3600
            except Exception:
                pass
        if hours is None:
            db_note = "База 21:00 ещё не записана — среднее за час считается от полуночи."
            lt = time.localtime()
            hours = (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec) / 3600

    lines = ["Сравнительная цена токенов — только локальные LLM (z.ai не считается)", "",
             f"Сэкономлено за последние сутки (локальные токены по ценам {LABEL}):",
             block(day, p, rate),
             f"- с 21:00 предыдущего запуска: {money(cost(since_commit, p), rate)}"]
    if hours >= 1 / 60:
        lines.append(f"- в среднем за час: {money(cost(since_commit, p) / hours, rate)}")

    lines += ["", "За текущую сессию (локальные токены):", block(cur_sess, p, rate)]
    if started:
        lines.append(f"- длительность сессии: {duration(time.time() - started)}")
    if con_db is not None and sid:
        ssum = T.session_sum(con_db, sid, "local")
        if ssum != cur_sess:
            lines.append(f"- по базе за эту сессию: {ssum[0]/1e6:.2f}M / {ssum[1]/1e6:.2f}M / {ssum[2]/1e6:.2f}M "
                         f"(база видит меньше source-таблицы: часть строк сессии была до создания базы "
                         f"или имеет незаписанный провайдер — plan unknown)")

    lines += [f"Не учтено (z.ai coding plan): {zai/1e6:.2f}M токенов"]

    # разбивка по дням — сумма инкрементов по временным окнам из базы
    if con_db is not None:
        rows = T.daily(con_db, "local", 4)
    else:
        rows = []
    if rows:
        day_lines = []
        for d, i_, c_, o_ in rows:
            t = (i_ or 0, c_ or 0, o_ or 0)
            day_lines.append(f"- {d}: {sum(t)/1e6:.2f}M = {money(cost(t, p), rate)}")
        lines += ["", "По дням (локальные токены):"] + day_lines
    elif con_db is None:
        try:
            rows = c.execute(
                "SELECT date(last_seen, 'unixepoch', 'localtime') d, "
                "SUM(input_tokens), SUM(cache_read_tokens), SUM(output_tokens) "
                f"FROM session_model_usage WHERE NOT {ZAI_WHERE} GROUP BY d ORDER BY d DESC LIMIT 4"
            ).fetchall()
            day_lines = [f"- {d}: {sum((i_ or 0, c_ or 0, o_ or 0))/1e6:.2f}M = "
                         f"{money(cost((i_ or 0, c_ or 0, o_ or 0), p), rate)}" for d, i_, c_, o_ in rows]
            if day_lines:
                lines += ["", "По дням (локальные токены, старый путь по last_seen):"] + day_lines
        except Exception:
            pass

    if ledger_fresh:
        lines += ["", "Копилка создана только что — «за всё время» считается с этого момента "
                      "(ранее удалённые сессии восстановить нельзя)."]
    if db_note:
        lines += ["", db_note]
    if note:
        lines += ["", note]
    if rate_note:
        lines += ["", rate_note]
    lines += ["", "курс взят из «Плати по миру»"]
    print("\n".join(lines))

    if not dry and commit:
        # база суточного окна = значения копилки на этот запуск в 21:00
        os.makedirs(os.path.dirname(SNAP), exist_ok=True)
        json.dump({"cumulative": dict(zip(KEYS, cumulative)),
                   "prices": p, "saved_at": datetime.now(timezone.utc).isoformat()},
                  open(SNAP, "w"), ensure_ascii=False)
    elif not dry:
        # ручной запуск тоже мог пополнить копилку — снимок не трогаем, но помечаем
        pass


if __name__ == "__main__":
    main()
