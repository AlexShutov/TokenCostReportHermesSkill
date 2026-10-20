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

CHAT_ID = os.environ.get("TOKEN_COST_CHAT_ID") or "362122320"
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
    """Сессия для блока «За текущую сессию». Возвращает (session_id, найдена через
    routing) — если routing-записи нет, это чужой чат или другой профиль: в отчёте
    это называется, а не выдаётся за факт."""
    row = c.execute("SELECT entry_json FROM gateway_routing WHERE session_key=?",
                    (f"agent:main:telegram:dm:{CHAT_ID}",)).fetchone()
    if row:
        try:
            sid = json.loads(row[0]).get("session_id")
            if sid:
                return sid, True
        except Exception:
            pass
    r = c.execute("SELECT id FROM sessions WHERE chat_id=? ORDER BY started_at DESC LIMIT 1",
                  (CHAT_ID,)).fetchone()
    return (r[0] if r else None), False


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
    """Сколько токенов ушло на z.ai — в отчёт не входят, но показать стоит.
    Возвращает (всего, оплаченные): оплаченные — это НЕ coding plan, по ним
    реально списаны деньги, и называть их «подпиской» нельзя."""
    q = ("SELECT COALESCE(SUM(input_tokens+cache_read_tokens+output_tokens),0), "
         "COALESCE(SUM(CASE WHEN actual_cost_usd > 0 THEN input_tokens+cache_read_tokens+output_tokens "
         "ELSE 0 END),0) FROM session_model_usage WHERE " + ZAI_WHERE)
    return c.execute(q).fetchone()


def resolve_profile():
    """Профиль запуска. Cron НЕ устанавливает HERMES_PROFILE (его ставит только
    kanban-диспетчер), поэтому профиль берём из записи задачи в jobs.json —
    cron наследует профиль создателя, и на мультипрофильной машине это важно."""
    job = os.environ.get("TOKEN_COST_CRON_JOB")
    if job:
        try:
            d = json.load(open(os.path.expanduser("~/.hermes/cron/jobs.json")))
            jobs = d if isinstance(d, list) else (d.get("jobs") or [])
            for j in jobs:
                if isinstance(j, dict) and j.get("name") == job:
                    return j.get("profile") or os.environ.get("HERMES_PROFILE") or "default"
        except Exception:
            pass
    return os.environ.get("HERMES_PROFILE") or "default"


def bump_ledger(cur):
    """Копилка: суммирует только прирост с прошлого раза; удаления сессий
    (просадка cur) не обнуляют накопленное и не считаются минусом. Idempotent:
    повторный запуск между тиками просто не добавит лишнего."""
    d = {}
    if os.path.exists(LEDGER):
        try:
            with open(LEDGER) as f:
                d = json.load(f)
        except Exception:
            d = {}
    last = [int(d.get("last_seen", {}).get(k, 0) or 0) for k in KEYS]
    cum = [int(d.get("cumulative", {}).get(k, 0) or 0) for k in KEYS]
    cum = [cum[i] + max(0, cur[i] - last[i]) for i in range(3)]
    # fresh-установка: каталога state/ может ещё не быть — без makedirs первый
    # запуск падает с FileNotFoundError и отчёт не формируется вообще
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    with open(LEDGER, "w") as f:
        json.dump({"last_seen": dict(zip(KEYS, cur)),
                   "cumulative": dict(zip(KEYS, cum)),
                   "fresh_install": not d,
                   "updated_at": datetime.now(timezone.utc).isoformat()},
                  f, ensure_ascii=False)
    return cum, not d


def parse_price(cell):
    cell = cell.strip()
    if not cell or cell == "-":
        return None
    if "free" in cell.lower():
        return 0.0
    m = re.search(r"\$?([0-9]+(?:\.[0-9]+)?)", cell)
    return float(m.group(1)) if m else None


def parse_price_table(html):
    """Строка таблицы цен z.ai для GLM-5.3-Flash; колонки определяем по заголовку таблицы."""
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


def fetch_prices():
    try:
        req = urllib.request.Request(URL, headers={"User-Agent": UA})
        html = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "replace")
    except Exception:
        return None, "сайт не ответил"
    return parse_price_table(html)


def load_cache():
    try:
        d = json.load(open(PRICE_CACHE))
        pk = ("input", "cached", "output")          # ключи цен, не путать с KEYS (ключи токенов)
        return {k: float(v) for k, v in d.items() if k in pk} or None
    except Exception:
        return None


# курс ищем только рядом со словом «курс» — иначе можно схватить цену самой карты;
# обязательное условие — дробное число (цена карты «2 990₽» дроби не имеет и не пройдёт)
RATE_PAT = re.compile(r"курс[^\d]{0,20}(\d+[.,]\d{1,2})\s*(?:₽|руб)?")


def fetch_rate_from_text(text):
    for m in re.finditer(re.escape(TARIFF), text):
        hit = RATE_PAT.search(text[m.start():m.start() + 300])
        if hit:
            return float(hit.group(1).replace(",", "."))
    return None


def fetch_rate():
    """Курс «Плати по миру» по тарифу TARIFF (курс сервиса, не ЦБ)."""
    try:
        req = urllib.request.Request(RATE_URL, headers={"User-Agent": UA})
        html = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "replace")
    except Exception:
        return None, "сайт не ответил"
    text = re.sub(r"<[^>]+>", " ", html)
    rate = fetch_rate_from_text(text)
    if rate:
        return rate, None
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
    sid, via_routing = resolve_session(c)

    cur_all = totals(c)                       # локальные токены по всем сессиям
    if dry:
        cumulative = [int(d) for d in cur_all]
        ledger_fresh = False
    else:
        cumulative, ledger_fresh = bump_ledger(cur_all)

    if tick:
        # тик: без сети, значения из кэша; запись в базу и пустой stdout (тихо).
        # Ошибка записи не должна ронять крон-задачу — тик молчит и выходит.
        try:
            con_db = T.connect()
            T.record_run(con_db, "tick", os.environ.get("TOKEN_COST_CRON_JOB"),
                         resolve_profile(), sid,
                         load_cache() or DEFAULTS, load_rate_cache() or DEFAULT_RATE,
                         "cache", cumulative, None)
        except Exception:
            pass
        return

    cur_sess = totals(c, sid) if sid else (0, 0, 0)
    zai, zai_paid = zai_total(c)
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
    db_note = None
    if dry:
        if os.path.exists(T.DB):
            try:
                con_db = sqlite3.connect(f"file:{T.DB}?mode=ro", uri=True, timeout=10)
            except Exception as e:
                db_note = f"База есть, но открыть её не удалось ({e}) — сутки посчитаны старым путём."
    else:
        try:
            con_db = T.connect()
            T.record_run(con_db, "tick" if tick else ("commit" if commit else "manual"),
                         os.environ.get("TOKEN_COST_CRON_JOB"),
                         resolve_profile(),
                         sid, p, rate, rate_source, cumulative, started)
        except Exception as e:
            # locked / битая база / любая ошибка записи — отчёт должен выйти и без базы
            con_db = None
            db_note = f"База недоступна ({e}) — сутки посчитаны дельтой от снимка 21:00 (старый путь)."

    now = time.time()
    prev_commit = None
    if con_db is not None:
        day = T.window_sum(con_db, "local", now - 86400, now)
        prow = con_db.execute("SELECT run_epoch FROM runs WHERE job='commit' ORDER BY run_epoch DESC LIMIT 1").fetchone()
        prev_commit = prow[0] if prow else None
        since_commit = T.window_sum(con_db, "local", prev_commit, now) if prev_commit else day
        if prev_commit:
            hours = (now - prev_commit) / 3600
        else:
            lt = time.localtime()
            hours = (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec) / 3600
        notes = []
        if T.db_start(con_db) >= now - 60:
            notes.append("База создана этим запуском: суточное окно и «по дням» — только по строкам, "
                         "записанным сейчас (backfill, оценка по last_seen).")
        if not prev_commit:
            notes.append("Запусков 21:00 в базе ещё нет — строка «с 21:00» показывает то же суточное окно, "
                         "среднее за час считается от полуночи.")
        notes.append("Суточное окно = сумма инкрементов по временным окнам из базы (append-only, "
                     "ничего не удаляется).")
        n_anom = con_db.execute("SELECT COUNT(*) FROM increments WHERE attribution='anomaly'").fetchone()[0]
        if n_anom:
            notes.append(f"Anomaly-строки: {n_anom} (уменьшение или исчезновение строки — в суммы не входят).")
        n_unknown = con_db.execute("SELECT COUNT(*) FROM increments WHERE plan_class='unknown'").fetchone()[0]
        if n_unknown:
            notes.append(f"Строки с незаписанным провайдером (plan unknown): {n_unknown} — в суточное окно не входят.")
        db_note = ((db_note + " ") if db_note else "") + " ".join(notes)
    else:
        day = delta(snap.get("cumulative"), cumulative)   # старый путь: дельта от базы снимка
        since_commit = day
        if not db_note:
            db_note = "База недоступна — сутки посчитаны дельтой от снимка 21:00 (старый путь)."
        hours = None
        if snap.get("saved_at"):
            try:
                ts = datetime.fromisoformat(snap["saved_at"]).timestamp()
                hours = (time.time() - ts) / 3600
                prev_commit = ts
            except Exception:
                pass
        if hours is None:
            db_note += " Среднее за час — от полуночи."
            lt = time.localtime()
            hours = (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec) / 3600

    # --- план: основной путь; расчёт токенов — частный случай только после Max ---
    import plan_core as C
    store = C.PlanStateStore(os.path.join(T.STATE, "token_cost_plan.json"))
    state = store.load()
    registry, mult, calc = C.PlanRegistry(), C.MultiplierRegistry(), C.StandardCreditCalculator()
    peak = C.DayIntegralPeakModel()
    anchor, anchor_src = C.resolve_anchor(state, datetime.now(C.PEAK_TZ).date(), C.DEFAULT_ANCHOR_DAY)
    window = C.MonthWindow(anchor)
    report = C.PlanReport(window, C.PlanCascade(registry, window), calc, mult, registry, peak,
                          state=state,
                          source=(C.SqliteIncrementSource(T.connect) if not dry else None),
                          anchor_source=anchor_src, notifier=C.TransitionNotifier(registry), store=store)
    plan_rows = report.rows()
    credits = report.credits(plan_rows)
    current = report.cascade.current_plan(credits)
    lines = report.build(plan_rows, credits)

    if current is not None:
        # основной путь: только план. Прежний расчёт токенов — частный случай,
        # он включается только когда переполнен Max.
        lines += ["", "Расчёт токенов не применяется — это частный случай, он включается "
                      "только после переполнения плана Max."]
    else:
        max_cap = registry.monthly_cap("max", window.days())
        caps = [(p, registry.monthly_cap(p, window.days())) for p in registry.order()]
        # оплаченные z.ai — не план: они не участвуют в расщеплении по лимитам
        split_rows = [r for r in plan_rows if r["plan_class"] != C.PAID_CLASS]
        within, ov, details = C.OverflowLedger(calc, mult, peak).split(split_rows, caps)
        by_day = C.group_overflow(details, lambda r: datetime.fromtimestamp(r["window_end"], C.PEAK_TZ).date())
        by_sess = C.group_overflow(details, lambda r: r["session_id"])
        today_pk = datetime.now(C.PEAK_TZ).date()
        day_ov = C.zero_tokens()
        for d, t in by_day.items():
            if 0 <= (today_pk - d).days <= 1:
                for k in day_ov:
                    day_ov[k] += t[k]
        sess_ov = by_sess.get(sid, C.zero_tokens())
        lines += ["", f"Пересчёт токенов — только переполнение Max ({sum(ov.values())/1e6:.2f}M токенов "
                      f"сверх месячного лимита Max ({max_cap:.1f} кредитов):", block(C.as_tuple(ov), p, rate)]
        lines += [f"Не учтено (z.ai coding plan): {(zai - zai_paid)/1e6:.2f}M токенов"]
        if zai_paid:
            lines.append(f"  отдельно — оплаченные z.ai (реально списанные деньги, это не план): "
                         f"{zai_paid/1e6:.2f}M токенов")
        lines += ["", "За последние сутки (переполнение Max):", block(C.as_tuple(day_ov), p, rate)]
        if prev_commit:
            since_ov = C.zero_tokens()
            for r, frac, _c in details:
                if frac and r["window_end"] >= prev_commit:
                    C.add_tokens(since_ov, r["tokens"], frac)
            lines.append(f"- с 21:00 предыдущего запуска: {money(cost(C.as_tuple(since_ov), p), rate)}")
        else:
            since_ov = day_ov
            lines.append(f"- с 21:00 предыдущего запуска: запуска 21:00 ещё нет — это то же суточное "
                         f"окно: {money(cost(C.as_tuple(since_ov), p), rate)}")
        if hours and hours >= 1 / 60:
            lines.append(f"- в среднем за час: {money(cost(C.as_tuple(since_ov), p) / hours, rate)}")
        if sid:
            lines += ["", "За текущую сессию (переполнение Max):", block(C.as_tuple(sess_ov), p, rate)]
            if started:
                lines.append(f"- длительность сессии: {duration(time.time() - started)}")
        if by_day:
            day_lines = [f"- {d}: {sum(t.values())/1e6:.2f}M = {money(cost(C.as_tuple(t), p), rate)}"
                         for d, t in sorted(by_day.items(), reverse=True)[:4]]
            lines += ["", "По дням (переполнение Max):"] + day_lines

    # состояние плана обновляется в обеих ветвях: иначе переход Light->Pro
    # сообщался бы заново на каждом запуске
    new_plan = current or "max"
    if not dry and new_plan != state.get("plan"):
        state["plan"] = new_plan
        store.save(state)

    if ledger_fresh:
        lines += ["", "Копилка создана только что — «за всё время» считается с этого момента "
                      "(ранее удалённые сессии восстановить нельзя)."]
    if not via_routing:
        lines += ["", f"Сессия взята не через gateway_routing (chat {CHAT_ID}) — на другой машине "
                      f"задай TOKEN_COST_CHAT_ID своим чатом, иначе блок «За текущую сессию» "
                      f"может относиться не к тому пользователю."]
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
