#!/usr/bin/env python3
"""Чистая установка навыка token-cost-report — как её делает коллега:
другой пользователь, свежий Hermes, клон из ЛОКАЛЬНОГО репо, временный HOME,
setup.sh, полный прогон всех режимов, отчёт.

Запуск: python3 tests/clean_install.py
Ничего реального не трогает: всё происходит во временном HOME.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

REPO = os.path.expanduser("~/.hermes/skills/productivity/token-cost-report")
PY = sys.executable
CHAT = "111111111"          # чат «коллеги», не чат автора навыка
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("PASS  " if ok else "FAIL  ") + name + (("  — " + detail) if detail else ""))


def run(path, *args, env=None):
    e = dict(os.environ)
    e["HOME"] = HOME
    e["TOKEN_COST_CHAT_ID"] = CHAT
    if env:
        e.update(env)
    # setup.sh — это bash, а не python: запускать его интерпретатором python нельзя
    argv = ["bash", path] if path.endswith(".sh") else [PY, path]
    p = subprocess.run(argv + list(args), capture_output=True, text=True, env=e, timeout=120)
    return p.returncode, p.stdout, p.stderr


def make_source_db(path):
    """Минимальный «свежий Hermes» у коллеги: таблицы, которые читает навык."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE session_model_usage (session_id TEXT, model TEXT, billing_provider TEXT,"
                " billing_base_url TEXT, billing_mode TEXT, task TEXT, input_tokens INTEGER,"
                " cache_read_tokens INTEGER, output_tokens INTEGER, actual_cost_usd REAL,"
                " first_seen REAL, last_seen REAL)")
    now = __import__("time").time()
    rows = [
        ("colleague-session", "qwen3.8-flash", "custom", "http://192.168.1.107:8080", "", "chat",
         1500000, 3000000, 400000, 0, now - 7200, now - 3600),
        ("colleague-session", "glm-5.3", "zai", "https://api.z.ai", "", "chat",
         2000000, 0, 0, 0, now - 7200, now - 3600),
        ("colleague-session", "glm-5.3", "custom", "https://api.z.ai/v1", "", "chat",
         500000, 0, 0, 0.21, now - 7200, now - 3600),      # z.ai по маршруту, не по provider
    ]
    con.executemany("INSERT INTO session_model_usage VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.execute("CREATE TABLE sessions (id TEXT, model TEXT, started_at REAL)")
    con.execute("INSERT INTO sessions VALUES('colleague-session', 'glm-5.3', ?)", (now - 9000,))
    con.execute("CREATE TABLE gateway_routing (session_key TEXT, entry_json TEXT)")
    con.execute("INSERT INTO gateway_routing VALUES(?, ?)",
                (f"agent:main:telegram:dm:{CHAT}", json.dumps({"session_id": "colleague-session"})))
    con.commit()
    con.close()


def main():
    global HOME
    HOME = tempfile.mkdtemp(prefix="tcr-clean-")
    skill = os.path.join(HOME, ".hermes", "skills", "productivity", "token-cost-report")
    scripts = os.path.join(HOME, ".hermes", "scripts")
    state = os.path.join(HOME, ".hermes", "state")

    print("=== чистая установка навыка token-cost-report ===")
    print("репо-источник:", REPO)
    print("временный HOME:", HOME)

    # 1. клон из локального репо
    r = subprocess.run(["git", "clone", REPO, skill], capture_output=True, text=True, timeout=120)
    check("клон из локального репо", r.returncode == 0, (r.stderr or "").strip()[:120])
    if r.returncode != 0:
        return 1

    # 2. setup.sh
    rc, out, err = run(os.path.join(skill, "scripts", "setup.sh"))
    check("setup.sh проходит", rc == 0, (err or out).strip()[:160])

    # M3: существующие скрипты не перезаписываются молча — крон исполняет копию
    # в ~/.hermes/scripts/, и тихая перезапись меняет поведение живого крона
    scripts_dir = os.path.join(HOME, ".hermes", "scripts")
    stale = os.path.join(scripts_dir, "token_cost.py")
    with open(stale, "w", encoding="utf-8") as f:
        f.write("# прежняя версия на машине коллеги\n")
    rc, out, err = run(os.path.join(skill, "scripts", "setup.sh"))
    with open(stale, encoding="utf-8") as f:
        kept = f.read().strip()
    check("setup.sh предупреждает и НЕ перезаписывает отличающийся файл",
          "ВНИМАНИЕ" in out and kept == "# прежняя версия на машине коллеги",
          out[:160])
    rc, out, err = run(os.path.join(skill, "scripts", "setup.sh"), "--force")
    with open(stale, encoding="utf-8") as f:
        after_force = f.read().strip()
    check("setup.sh --force перезаписывает осознанно",
          after_force != "# прежняя версия на машине коллеги",
          out[:160])

    # M4: некорректный день не должен молча записываться в состояние
    rc, out, err = run(os.path.join(skill, "scripts", "setup.sh"), "abc")
    with open(os.path.join(state, "token_cost_plan.json"), encoding="utf-8") as f:
        saved = json.load(f)
    check("setup.sh отвергает некорректный день и оставляет значение по умолчанию",
          "не понял" in out and saved["anchor_date"] != "abc", out[:160])
    check("setup.sh создаёт state/", os.path.isdir(state))
    check("setup.sh предлагает выбрать день начала подписки",
          "--set-anchor" in out and "начал" in out.lower(), out.strip()[:160])

    # 3. скрипты в ~/.hermes/scripts (крон исполняет только оттуда)
    os.makedirs(scripts, exist_ok=True)
    for f in os.listdir(os.path.join(skill, "scripts")):
        if f.endswith(".py"):
            shutil.copy(os.path.join(skill, "scripts", f), os.path.join(scripts, f))
    src_scripts = sorted(f for f in os.listdir(os.path.join(skill, "scripts")) if f.endswith(".py"))
    copied = sorted(f for f in os.listdir(scripts) if f.endswith(".py"))
    check("все скрипты скопированы", copied == src_scripts,
          f"{copied} != {src_scripts}")

    # 4. свежий Hermes у коллеги: source-таблица
    make_source_db(os.path.join(HOME, ".hermes", "state.db"))

    # 5. jobs.json с профилем коллеги — проверка, что профиль берётся оттуда
    os.makedirs(os.path.join(HOME, ".hermes", "cron"), exist_ok=True)
    with open(os.path.join(HOME, ".hermes", "cron", "jobs.json"), "w", encoding="utf-8") as f:
        json.dump({"jobs": [{"id": "x", "name": "token-counter", "profile": "colleague",
                             "no_agent": True, "script": "token_cost_tick.py"}]}, f)

    # 6. dry-run: печатает и ничего не пишет
    rc, out, err = run(os.path.join(scripts, "token_cost.py"), "--dry-run")
    check("dry-run прошёл", rc == 0, (err or "").strip()[:160])
    check("dry-run ничего не записал", not os.path.exists(os.path.join(state, "token_cost.db"))
          and not os.path.exists(os.path.join(state, "token_cost_ledger.json")),
          "после dry-run в state/ нет ни базы, ни копилки")
    check("формат отчёта начинается с оценки плана",
          out.splitlines()[0].startswith("Равно") and "на Z.ai" in out
          and "курс взят из «Плати по миру»" in out, out.splitlines()[0])
    check("расчёт токенов — частный случай: при незаполненном плане не печатается",
          "Расчёт токенов не применяется" in out and "Сэкономлено за последние сутки" not in out,
          out.splitlines()[0])

    # 7. тик через обёртку (именно так его зовёт крон): молчит, пишет run
    rc, out, err = run(os.path.join(scripts, "token_cost_tick.py"))
    check("тик через обёртку молчит", rc == 0 and out == "", repr(out[:80]))
    db = os.path.join(state, "token_cost.db")
    check("тик создал базу во временном HOME", os.path.exists(db), db)
    if os.path.exists(db):
        con = sqlite3.connect(db)
        row = con.execute("SELECT job, cron_job, profile FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
        check("тик записал job/cron_job", row and row[0] == "tick" and row[1] == "token-counter", str(row))
        check("профиль взят из jobs.json", row and row[2] == "colleague", str(row))
        con.close()

    # 8. обёртка commit (крон 21:00)
    rc, out, err = run(os.path.join(scripts, "token_cost_commit.py"))
    check("commit-обёртка прошла", rc == 0, (err or "").strip()[:160])
    check("commit пишет снимок", os.path.exists(os.path.join(state, "token_cost_snapshots.json")))
    if os.path.exists(db):
        con = sqlite3.connect(db)
        row = con.execute("SELECT job, cron_job FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
        check("commit записал job/cron_job", row and row[0] == "commit" and row[1] == "token-cost-daily", str(row))
        # суточное окно из базы должно совпасть с source-таблицей (нет двойного счёта)
        import time
        day = con.execute("SELECT SUM(input_delta),SUM(cache_delta),SUM(output_delta) FROM increments "
                          "WHERE attribution<>'anomaly' AND plan_class='local' AND window_end>=?",
                          (time.time() - 86400,)).fetchone()
        src = sqlite3.connect(os.path.join(HOME, ".hermes", "state.db"))
        truth = src.execute("SELECT SUM(input_tokens),SUM(cache_read_tokens),SUM(output_tokens) "
                            "FROM session_model_usage WHERE NOT (billing_provider='zai' OR "
                            "billing_base_url LIKE '%api.z.ai%')").fetchone()
        check("суточное окно = source-таблица (база не пустая)", day == truth, f"база {day}, source {truth}")
        src.close()
        con.close()

    # 9. исчезнувшая строка (сессия удалена) — база должна это увидеть
    con = sqlite3.connect(db)
    before = con.execute("SELECT COUNT(*) FROM increments WHERE attribution='anomaly'").fetchone()[0]
    con.close()
    src = sqlite3.connect(os.path.join(HOME, ".hermes", "state.db"))
    src.execute("DELETE FROM session_model_usage WHERE billing_provider='zai'")
    src.commit()
    src.close()
    rc, out, err = run(os.path.join(scripts, "token_cost.py"))
    con = sqlite3.connect(db)
    after = con.execute("SELECT COUNT(*) FROM increments WHERE attribution='anomaly'").fetchone()[0]
    con.close()
    check("исчезнувшая строка записана как anomaly", after > before, f"{before} -> {after}")
    check("anomaly показана в отчёте", "Anomaly-строки" in out)
    check("блок плана z.ai в отчёте на чистой копии", "Эквивалентный кодовый план z.ai" in out,
          "строка 'использовано X% Y плана' не печатается")
    check("блок плана называет, что месячный лимит выведен", "только 5-часовой и недельный" in out, out[-400:])

    # 10. переполнение Max — частный случай: включается расчёт токенов
    #     и показывает только число переполнения, а не общее количество
    import time as _time
    con = sqlite3.connect(db)
    now = int(_time.time())
    con.execute("INSERT INTO runs(run_at, run_epoch, job, cron_job, profile, session_id, "
                "session_started_at, session_seconds, prices_json, rate, rate_source, ledger_json) "
                "VALUES('x', ?, 'manual', NULL, 'colleague', 's1', NULL, NULL, '{}', 0, 'default', '{}')",
                (now,))
    rid = con.execute("SELECT MAX(run_id) FROM runs").fetchone()[0]
    con.execute("INSERT INTO increments(run_id, run_at, run_epoch, window_start, window_end, session_id, "
                "model, billing_provider, billing_base_url, billing_mode, task, plan_class, input_delta, "
                "cache_delta, output_delta, attribution, note) VALUES(?, 'x', ?, ?, ?, 's1', "
                "'glm-5.3-flash', 'zai', 'https://api.z.ai', '', 'chat', 'zai_plan_by_rule', "
                "7000000000, 0, 0, 'delta', '')", (rid, now, now - 100, now))
    con.commit()
    con.close()
    rc, out, err = run(os.path.join(scripts, "token_cost.py"))
    check("переполнение Max даёт сообщение о переходе",
          "План Max переполнен, переходим на пересчет токенов." in out, out[:200])
    check("частный случай: расчёт токенов включён",
          "Пересчёт токенов — только переполнение Max" in out, out[:200])
    tail = out.split("Пересчёт токенов", 1)[-1]
    check("показывается только переполнение, не общее количество токенов",
          "7000.00M" not in tail, tail[:160])
    check("z.ai разделён на план и оплату", "оплаченные" in tail, tail[:160])

    # 11. тесты навыка на чистой копии
    for tf in ("test_plan_core.py", "test_plan_usage.py", "test_token_cost.py"):
        r = subprocess.run([PY, os.path.join(skill, "tests", tf)],
                           capture_output=True, text=True, env={**os.environ, "HOME": HOME}, timeout=300)
        check(f"тесты навыка на чистой копии ({tf})", r.returncode == 0,
              (r.stderr.strip().splitlines() or [""])[-1][:120])

    print("\n=== отчёт ===")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"{passed}/{len(RESULTS)} проверок пройдено")
    for name, ok, detail in RESULTS:
        if not ok:
            print("  FAIL:", name, "-", detail)
    shutil.rmtree(HOME, ignore_errors=True)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
