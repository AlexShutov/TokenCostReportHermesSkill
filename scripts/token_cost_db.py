#!/usr/bin/env python3
"""Append-only база статистики токенов для token-cost-report.

Каждый запуск (тик крона, полный запуск 21:00, ручной) пишет три таблицы:
  runs           — таймстамп запуска, job, cron-задача, сессия, цены, курс;
  usage_snapshot — каждое значение source-строки на момент запуска;
  increments     — прирост каждой строки между двумя наблюдениями, с временным окном.

Ничего не удаляется: таблицы INSERT-only. Суточные и сессионные значения считаются
СUMМИРОВАНИЕМ инкрементов по временным окнам, а не дельтой от базы снимка — поэтому
короткая сессия не обнуляет «последние сутки».

Класс плана (plan_class) различает:
  zai_plan / zai_plan_by_rule — z.ai coding plan (подписка, токены не считаются в экономию)
  zai_paid                    — z.ai, но реально списаны деньги (платный маршрут)
  local                       — любой не-z.ai провайдер (strata, LM Studio, ollama, bionic)
  unknown                     — провайдер не записан
"""
import json, os, sqlite3, time
from datetime import datetime, timezone

STATE = os.path.expanduser("~/.hermes/state")
DB = os.path.join(STATE, "token_cost.db")
SRC = os.path.expanduser("~/.hermes/state.db")

KEYS = ("input", "cache", "output")          # input_tokens, cache_read_tokens, output_tokens
ZAI_WHERE = "(billing_provider='zai' OR billing_base_url LIKE '%api.z.ai%')"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
  run_id INTEGER PRIMARY KEY,
  run_at TEXT NOT NULL,
  run_epoch REAL NOT NULL,
  job TEXT NOT NULL,
  cron_job TEXT,
  profile TEXT,
  session_id TEXT,
  session_started_at REAL,
  session_seconds REAL,
  prices_json TEXT,
  rate REAL,
  rate_source TEXT,
  ledger_json TEXT
);
CREATE TABLE IF NOT EXISTS usage_snapshot(
  snap_id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES runs(run_id),
  run_at TEXT NOT NULL,
  run_epoch REAL NOT NULL,
  session_id TEXT NOT NULL,
  model TEXT NOT NULL,
  billing_provider TEXT NOT NULL,
  billing_base_url TEXT NOT NULL,
  billing_mode TEXT NOT NULL,
  task TEXT NOT NULL,
  plan_class TEXT NOT NULL,
  input_tokens INTEGER NOT NULL,
  cache_read_tokens INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL,
  actual_cost_usd REAL NOT NULL,
  first_seen REAL,
  last_seen REAL
);
CREATE TABLE IF NOT EXISTS increments(
  inc_id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES runs(run_id),
  run_at TEXT NOT NULL,
  run_epoch REAL NOT NULL,
  window_start REAL NOT NULL,
  window_end REAL NOT NULL,
  session_id TEXT NOT NULL,
  model TEXT NOT NULL,
  billing_provider TEXT NOT NULL,
  billing_base_url TEXT NOT NULL,
  billing_mode TEXT NOT NULL,
  task TEXT NOT NULL,
  plan_class TEXT NOT NULL,
  input_delta INTEGER NOT NULL,
  cache_delta INTEGER NOT NULL,
  output_delta INTEGER NOT NULL,
  attribution TEXT NOT NULL,
  note TEXT
);
CREATE INDEX IF NOT EXISTS idx_inc_window ON increments(window_end);
CREATE INDEX IF NOT EXISTS idx_inc_session ON increments(session_id);
CREATE INDEX IF NOT EXISTS idx_inc_plan ON increments(plan_class);
CREATE INDEX IF NOT EXISTS idx_snap_key ON usage_snapshot(session_id, model, billing_provider,
                                                          billing_base_url, billing_mode, task);
"""

ROW_KEY = ("session_id", "model", "billing_provider", "billing_base_url", "billing_mode", "task")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def connect():
    os.makedirs(STATE, exist_ok=True)
    con = sqlite3.connect(DB, timeout=10)
    con.executescript(SCHEMA)
    return con


def src_connect():
    return sqlite3.connect(f"file:{SRC}?mode=ro", uri=True, timeout=10)


def plan_class(billing_provider, billing_mode, actual_cost):
    """План по записанному маршруту; реально списанные деньги — сильный сигнал."""
    if not billing_provider:
        return "unknown"
    if billing_provider == "zai":
        if actual_cost and actual_cost > 0:
            return "zai_paid"
        if billing_mode in ("subscription", "subscription_included"):
            return "zai_plan"
        return "zai_plan_by_rule"      # подписка по правилу пользователя; Hermes не различает
    return "local"


def db_start(con):
    row = con.execute("SELECT MIN(run_epoch) FROM runs").fetchone()
    return row[0] if row and row[0] is not None else time.time()


def record_run(con, job, cron_job, profile, session_id, prices, rate, rate_source,
               ledger_cum, session_started=None):
    """Один запуск: ряд в runs, ряд в usage_snapshot на каждую source-строку,
    ряд в increments на каждую строку с приростом с прошлого наблюдения."""
    t = time.time()
    first_run = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    cur = con.cursor()
    cur.execute("INSERT INTO runs(run_at, run_epoch, job, cron_job, profile, session_id, "
                "session_started_at, session_seconds, prices_json, rate, rate_source, ledger_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (now_iso(), t, job, cron_job, profile, session_id, session_started,
                 (t - session_started) if session_started else None,
                 json.dumps(prices, ensure_ascii=False), rate, rate_source,
                 json.dumps(ledger_cum, ensure_ascii=False)))
    run_id = cur.lastrowid

    s = src_connect()
    rows = s.execute("SELECT session_id, model, billing_provider, billing_base_url, billing_mode, "
                     "task, input_tokens, cache_read_tokens, output_tokens, actual_cost_usd, "
                     "first_seen, last_seen FROM session_model_usage").fetchall()

    added = 0
    for r in rows:
        key = r[:6]
        prev = cur.execute("SELECT input_tokens, cache_read_tokens, output_tokens, run_epoch "
                           "FROM usage_snapshot WHERE " +
                           " AND ".join(f"{c}=?" for c in ROW_KEY) +
                           " AND run_id<>? ORDER BY run_epoch DESC LIMIT 1",
                           (*key, run_id)).fetchone()
        pc = plan_class(key[2], key[4], r[9])
        cur.execute("INSERT INTO usage_snapshot(run_id, run_at, run_epoch, " + ", ".join(ROW_KEY) +
                    ", plan_class, input_tokens, cache_read_tokens, output_tokens, actual_cost_usd, "
                    "first_seen, last_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, now_iso(), t, *key, pc, r[6], r[7], r[8], r[9], r[10], r[11]))

        if prev is None:
            if first_run:
                # Первый запуск базы: строки уже существовали, время внутри них не восстановить.
                # Вся сумма атрибутируется точкой last_seen — это оценка, и она так помечена.
                a, note, d = "backfill", "оценка: атрибуция по last_seen строки (база создана позже)", (r[6], r[7], r[8])
                w0 = w1 = r[11] or r[10] or t
            elif r[10] is not None:
                a, note, d = "first_seen", "первое наблюдение строки", (r[6], r[7], r[8])
                w0, w1 = r[10], r[11] or t
            else:
                a, note, d = "anomaly", "строка без first_seen — время не восстанавливается", (0, 0, 0)
                w0, w1 = t, t
        else:
            d = (r[6] - prev[0], r[7] - prev[1], r[8] - prev[2])
            w0, w1 = prev[3], t
            if any(x < 0 for x in d):
                a, note, d = "anomaly", "строка уменьшилась (сессия удалена) — не входит в суммы", (0, 0, 0)
            else:
                a, note = "delta", None

        if a != "anomaly" and sum(d) == 0:
            continue
        added += 1
        cur.execute("INSERT INTO increments(run_id, run_at, run_epoch, window_start, window_end, "
                    + ", ".join(ROW_KEY) + ", plan_class, input_delta, cache_delta, output_delta, "
                    "attribution, note) VALUES(" + ",".join("?" * 17) + ")",
                    (run_id, now_iso(), t, w0, w1, *key, pc, d[0], d[1], d[2], a, note))
    con.commit()
    s.close()
    return run_id, added


def window_sum(con, plan, start, end):
    """Сумма инкрементов за временное окно (anomaly-строки не входят)."""
    q = ("SELECT COALESCE(SUM(input_delta),0), COALESCE(SUM(cache_delta),0), "
         "COALESCE(SUM(output_delta),0) FROM increments WHERE attribution<>'anomaly' "
         "AND plan_class=? AND window_end>=? AND window_start<=?")
    return con.execute(q, (plan, start, end)).fetchone()


def session_sum(con, session_id, plan=None):
    q = ("SELECT COALESCE(SUM(input_delta),0), COALESCE(SUM(cache_delta),0), "
         "COALESCE(SUM(output_delta),0) FROM increments WHERE attribution<>'anomaly' "
         "AND session_id=?" + (" AND plan_class=?" if plan else ""))
    args = (session_id,) + ((plan,) if plan else ())
    return con.execute(q, args).fetchone()


def daily(con, plan, limit=4):
    q = ("SELECT date(window_end,'unixepoch','localtime') d, SUM(input_delta), SUM(cache_delta), "
         "SUM(output_delta) FROM increments WHERE attribution<>'anomaly' AND plan_class=? "
         "GROUP BY d ORDER BY d DESC LIMIT ?")
    return con.execute(q, (plan, limit)).fetchall()


def anomalies(con, limit=10):
    return con.execute("SELECT run_at, session_id, model, plan_class, note FROM increments "
                       "WHERE attribution='anomaly' ORDER BY run_epoch DESC LIMIT ?", (limit,)).fetchall()


if __name__ == "__main__":
    import sys
    con = connect()
    print("база:", DB)
    print("запусков:", con.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
          "| инкрементов:", con.execute("SELECT COUNT(*) FROM increments").fetchone()[0],
          "| снимков:", con.execute("SELECT COUNT(*) FROM usage_snapshot").fetchone()[0])
    if "--record" in sys.argv:
        rid, added = record_run(con, "manual", None, "default", None, {}, None, None, {})
        print(f"run {rid}, строк increments добавлено: {added}")
