#!/usr/bin/env python3
"""Тесты навыка token-cost-report (stdlib, Python 3.9+, без сети).

Запуск: python3 tests/test_token_cost.py
Каждый тест работает в отдельном временном HOME; source-таблица подменяется
in-memory копией session_model_usage; сеть не трогается.
"""
import contextlib
from datetime import datetime
from fractions import Fraction
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

SRC_COLS = ("session_id", "model", "billing_provider", "billing_base_url", "billing_mode",
            "task", "input_tokens", "cache_read_tokens", "output_tokens", "actual_cost_usd",
            "first_seen", "last_seen")


def fake_source(rows):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE session_model_usage (" + ", ".join(SRC_COLS) + ")")
    con.executemany("INSERT INTO session_model_usage VALUES(" + ",".join("?" * 12) + ")", rows)
    con.execute("CREATE TABLE sessions (id TEXT, model TEXT, started_at REAL)")
    con.execute("INSERT INTO sessions VALUES('s1', 'qwen', 100)")
    return con


class Case(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="tcr-test-")
        os.environ["HOME"] = self.home
        # пути вычислены при импорте модулей — перезагружаем их в новом HOME
        for name in ("token_cost_db", "token_cost", "plan_usage", "plan_core"):
            sys.modules.pop(name, None)
        import token_cost_db as T
        import token_cost as C
        self.T, self.C = T, C

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def run_record(self, rows, job="manual"):
        con = self.T.connect()
        self.T.src_connect = lambda: fake_source(rows)
        rid, added = self.T.record_run(con, job, None, "default", "s1", {}, None, None, {}, None)
        return (rid, added), con

    def capture(self, argv, rows=None):
        rows = rows if rows is not None else self.ROWS
        self.C.conn = lambda: fake_source(rows)
        self.T.src_connect = lambda: fake_source(rows)
        self.C.resolve_session = lambda c: "s1"
        self.C.fetch_prices = lambda: (None, "тест без сети")
        self.C.fetch_rate = lambda: (None, "тест без сети")
        sys.argv = ["token_cost.py"] + argv
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.C.main()
        return buf.getvalue()


# --- план-класс ---------------------------------------------------------------
class PlanClass(Case):
    def test_local_vs_zai(self):
        T = self.T
        self.assertEqual(T.plan_class("custom", "http://192.168.1.107:8080", "", 0), "local")
        self.assertEqual(T.plan_class("lmstudio", "http://localhost:1234", "", 0), "local")
        self.assertEqual(T.plan_class("ollama", "", "", 0), "local")
        self.assertEqual(T.plan_class("", "", "", 0), "unknown")
        self.assertEqual(T.plan_class("zai", "https://api.z.ai", "", 0), "zai_plan_by_rule")
        self.assertEqual(T.plan_class("zai", "https://api.z.ai", "subscription", 0), "zai_plan")
        self.assertEqual(T.plan_class("zai", "https://api.z.ai", "chat_completions", 0.42), "zai_paid")

    def test_base_url_route_is_zai_even_if_provider_says_otherwise(self):
        """Баг был: SQL-фильтр навыка ловит api.z.ai по base_url, а plan_class —
        только по provider. Строка provider='custom' + api.z.ai считалась локальной
        в суточном блоке и z.ai в блоке «не учтено» — одни и те же токены в двух
        разных категориях."""
        T = self.T
        self.assertEqual(T.plan_class("custom", "https://api.z.ai/v1", "", 0), "zai_plan_by_rule")
        self.assertEqual(T.plan_class("custom", "https://api.z.ai/v1", "", 0.30), "zai_paid")
        self.assertEqual(T.plan_class("custom", "http://192.168.1.107:8080", "", 0), "local")

    def test_plan_class_and_sql_filter_agree(self):
        rows = [("s1", "m", "custom", "https://api.z.ai/v1", "", "chat", 10, 20, 30, 0, 1, 2),
                ("s1", "m", "custom", "http://192.168.1.107:8080", "", "chat", 40, 50, 60, 0, 1, 2)]
        s = fake_source(rows)
        zai_sql = s.execute("SELECT SUM(input_tokens+cache_read_tokens+output_tokens) FROM session_model_usage "
                            "WHERE (billing_provider='zai' OR billing_base_url LIKE '%api.z.ai%')").fetchone()[0]
        zai_code = sum(r[6] + r[7] + r[8] for r in rows
                       if self.T.plan_class(r[2], r[3], r[4], r[9]) != "local")
        self.assertEqual(zai_sql, 60)
        self.assertEqual(zai_code, 60)


# --- increments: backfill, отсутствие двойного счёта, anomaly ---------------
class Increments(Case):
    def test_backfill_then_no_double_count(self):
        rows = [("s1", "qwen", "custom", "http://x", "", "chat", 1000, 2000, 300, 0, 100, 100)]
        (rid, added), con = self.run_record(rows)
        self.assertEqual(added, 1, "backfill: один инкремент-ряд на source-строку")
        self.assertEqual(con.execute("SELECT SUM(input_delta),SUM(cache_delta),SUM(output_delta) "
                                     "FROM increments").fetchone(), (1000, 2000, 300))
        # повторный запуск с теми же значениями не должен добавить ни токена
        (rid2, added2), con = self.run_record(rows)
        self.assertEqual(added2, 0, "двойной счёт: повторный запуск добавил токены")
        self.assertEqual(con.execute("SELECT SUM(input_delta) FROM increments").fetchone()[0], 1000)

    def test_growth_window(self):
        rows = [("s1", "qwen", "custom", "http://x", "", "chat", 1000, 2000, 300, 0, 100, 100)]
        self.run_record(rows)
        rows2 = [("s1", "qwen", "custom", "http://x", "", "chat", 1100, 2100, 350, 0, 100, 900)]
        (rid, added), con = self.run_record(rows2)
        self.assertEqual(added, 1)
        self.assertEqual(con.execute("SELECT SUM(input_delta),SUM(cache_delta),SUM(output_delta) "
                                     "FROM increments").fetchone(), (1100, 2100, 350))
        w = con.execute("SELECT window_start, window_end FROM increments WHERE input_delta=100").fetchone()
        self.assertLess(w[0], w[1], "окно прироста не должно быть точкой")

    def test_negative_growth_is_anomaly_and_excluded(self):
        rows = [("s1", "qwen", "custom", "http://x", "", "chat", 1000, 0, 0, 0, 100, 100)]
        self.run_record(rows)
        rows2 = [("s1", "qwen", "custom", "http://x", "", "chat", 400, 0, 0, 0, 100, 900)]
        (rid, added), con = self.run_record(rows2)
        a = con.execute("SELECT attribution FROM increments WHERE run_id=?", (rid,)).fetchone()[0]
        self.assertEqual(a, "anomaly")
        self.assertEqual(con.execute("SELECT SUM(input_delta) FROM increments").fetchone()[0], 1000,
                         "отрицательный прирост попал в суммы")

    def test_window_over_threshold_is_gap_and_excluded(self):
        """Баг: длинный интервал между наблюдениями (крон не сработал, машина спала,
        агент завис) атрибутировался целиком днём window_end и искажал «по дням».
        Теперь такой интервал выбрасывается из сумм, но строка остаётся в базе."""
        rows = [("s1", "qwen", "custom", "http://x", "", "chat", 1000, 0, 0, 0, 100, 100)]
        self.run_record(rows)
        con = self.T.connect()
        con.execute("UPDATE usage_snapshot SET run_epoch = run_epoch - 2400")   # 40 минут назад
        con.commit()
        con.close()
        rows2 = [("s1", "qwen", "custom", "http://x", "", "chat", 1100, 0, 0, 0, 100, 2500)]
        (rid, added), con = self.run_record(rows2)
        a = con.execute("SELECT attribution FROM increments WHERE run_id=?", (rid,)).fetchone()[0]
        self.assertEqual(a, "gap", f"интервал длиннее порога не выброшен: {a}")
        now = time.time() + 10
        self.assertEqual(self.T.window_sum(con, "local", 0, now)[0], 1000,
                         "выбрасываемый интервал попал в суточное окно")
        self.assertEqual(self.T.session_sum(con, "s1", "local")[0], 1000,
                         "выбрасываемый интервал попал в сессионную сумму")
        day = self.T.daily(con, "local")[0]
        self.assertEqual(day[1], 1000, "выбрасываемый интервал попал в «по дням»")
        self.assertEqual(con.execute("SELECT input_delta FROM increments WHERE run_id=?",
                                     (rid,)).fetchone()[0],
                         100, "append-only: строка выбрасывается из сумм, но обязана остаться в базе")
        con.close()

    def test_window_under_threshold_stays_delta(self):
        rows = [("s1", "qwen", "custom", "http://x", "", "chat", 1000, 0, 0, 0, 100, 100)]
        self.run_record(rows)
        rows2 = [("s1", "qwen", "custom", "http://x", "", "chat", 1100, 0, 0, 0, 100, 101)]
        (rid, added), con = self.run_record(rows2)
        a = con.execute("SELECT attribution FROM increments WHERE run_id=?", (rid,)).fetchone()[0]
        self.assertEqual(a, "delta", f"нормальный интервал ошибочно выброшен: {a}")
        self.assertEqual(self.T.window_sum(con, "local", 0, time.time() + 10)[0], 1100,
                         "нормальный интервал не попал в суммы")
        con.close()

    def test_threshold_is_configurable_by_env(self):
        import importlib, attributions as A
        os.environ["TOKEN_COST_MAX_WINDOW"] = "5"
        try:
            A2 = importlib.reload(A)
            self.assertEqual(A2.MAX_WINDOW_SECONDS, 5, "порог не читается из переменной")
            rows = [("s1", "qwen", "custom", "http://x", "", "chat", 1000, 0, 0, 0, 100, 100)]
            self.run_record(rows)
            con = self.T.connect()
            con.execute("UPDATE usage_snapshot SET run_epoch = run_epoch - 60")
            con.commit()
            con.close()
            (rid, added), con = self.run_record(
                [("s1", "qwen", "custom", "http://x", "", "chat", 1100, 0, 0, 0, 100, 160)])
            a = con.execute("SELECT attribution FROM increments WHERE run_id=?", (rid,)).fetchone()[0]
            self.assertEqual(a, "gap", f"порог из переменной не применён: {a}")
            self.assertEqual(self.T.window_sum(con, "local", 0, time.time() + 10)[0], 1000,
                         "порог из переменной не применяется к суммам")
            con.close()
        finally:
            os.environ.pop("TOKEN_COST_MAX_WINDOW", None)
            importlib.reload(A)

    def test_disappeared_row_recorded_as_anomaly(self):
        """Строка, которая исчезла между запусками (сессия удалена), должна попасть
        в базу как anomaly — иначе база никогда не видит удаление, а SKILL.md обещает."""
        rows = [("s1", "qwen", "custom", "http://x", "", "chat", 1000, 0, 0, 0, 100, 100),
                ("s2", "glm", "zai", "https://api.z.ai", "", "chat", 500, 0, 0, 0, 100, 100)]
        self.run_record(rows)
        (rid, added), con = self.run_record([rows[0]])          # s2 исчезла
        got = con.execute("SELECT session_id, attribution, input_delta FROM increments WHERE run_id=?",
                          (rid,)).fetchall()
        self.assertTrue(any(g[0] == "s2" and g[1] == "anomaly" and g[2] == 0 for g in got),
                        f"исчезнувшая строка не записана как anomaly: {got}")
        # anomaly-ряд не должен ничего добавлять: суммы остаются как после backfill
        self.assertEqual(con.execute("SELECT SUM(input_delta) FROM increments").fetchone()[0], 1500)

    def test_row_without_first_seen(self):
        rows = [("s1", "qwen", "custom", "http://x", "", "chat", 1000, 0, 0, 0, None, 100)]
        (rid, added), con = self.run_record(rows)
        self.assertEqual(con.execute("SELECT attribution FROM increments WHERE run_id=?", (rid,)).fetchone()[0],
                         "backfill")

    def test_window_sum_excludes_outside_window(self):
        rows = [("s1", "qwen", "custom", "http://x", "", "chat", 1000, 0, 0, 0, 100, 100)]
        con = self.T.connect()
        self.T.src_connect = lambda: fake_source(rows)
        self.T.record_run(con, "manual", None, "default", "s1", {}, None, None, {}, None)
        now = time.time()
        self.assertEqual(self.T.window_sum(con, "local", now - 10, now), (0, 0, 0))
        self.assertEqual(self.T.window_sum(con, "local", 90, 110), (1000, 0, 0))

    def test_window_sum_excludes_anomaly(self):
        rows = [("s1", "qwen", "custom", "http://x", "", "chat", 1000, 0, 0, 0, 100, 100)]
        self.run_record(rows)
        con = self.T.connect()
        con.execute("INSERT INTO increments(run_id, run_at, run_epoch, window_start, window_end, "
                    "session_id, model, billing_provider, billing_base_url, billing_mode, task, "
                    "plan_class, input_delta, cache_delta, output_delta, attribution, note) "
                    "VALUES(1,'x',1,1,1,'s1','qwen','custom','http://x','','chat','local',500,0,0,"
                    "'anomaly','проверка')")
        self.assertEqual(self.T.window_sum(con, "local", 0, 200), (1000, 0, 0))

    def test_plan_filter(self):
        rows = [("s1", "glm", "zai", "https://api.z.ai", "", "chat", 500, 0, 0, 0, 100, 100),
                ("s1", "qwen", "custom", "http://x", "", "chat", 1000, 0, 0, 0, 100, 100)]
        self.run_record(rows)
        con = self.T.connect()
        self.assertEqual(self.T.window_sum(con, "local", 0, 200), (1000, 0, 0))
        self.assertEqual(self.T.window_sum(con, "zai_plan_by_rule", 0, 200), (500, 0, 0))

    def test_boundary_straddling_documents_the_limitation(self):
        """Ограничение (не баг, но должно называться в SKILL.md): инкремент, окно
        которого пересекает границу суточного окна, считается ЦЕЛИКОМ — при длинном
        интервале между запусками (крон не работал, машина спала) прирост целиком
        попадает в день window_end, даже если большая часть роста случилась раньше."""
        rows = [("s1", "qwen", "custom", "http://x", "", "chat", 1000, 0, 0, 0, 100, 100)]
        con = self.T.connect()
        self.T.src_connect = lambda: fake_source(rows)
        self.T.record_run(con, "manual", None, "default", "s1", {}, None, None, {}, None)
        rows2 = [("s1", "qwen", "custom", "http://x", "", "chat", 2000, 0, 0, 0, 100, 900)]
        self.T.src_connect = lambda: fake_source(rows2)
        self.T.record_run(con, "manual", None, "default", "s1", {}, None, None, {}, None)
        w = con.execute("SELECT window_start, window_end FROM increments WHERE attribution='delta'").fetchone()
        # окно, которое накрывает только хвост инкремента, — и всё равно получает его целиком
        self.assertGreater(self.T.window_sum(con, "local", w[1] - 1, w[1]), (0, 0, 0))
        self.assertEqual(self.T.window_sum(con, "local", w[1] - 1, w[1]), (1000, 0, 0))
        # окно до инкремента — ничего не получает
        self.assertEqual(self.T.window_sum(con, "local", 101, w[0] - 1), (0, 0, 0))


# --- отчёт: формат, часы, z.ai, dry-run, tick, деградация -------------------
class Report(Case):
    ROWS = [("s1", "qwen", "custom", "http://x", "", "chat", 1000, 2000, 300, 0, 100, 100),
            ("s1", "glm", "zai", "https://api.z.ai", "", "chat", 2500000, 0, 0, 0, 100, 100),
            ("s2", "glm", "zai", "https://api.z.ai", "", "chat", 1200000, 0, 0, 0.42, 100, 100)]

    def seed_row(self, tokens=1000000, model="glm-5.3-flash", plan_class="zai_plan_by_rule",
                 attribution="delta", note=""):
        import plan_core as PC
        now = int(time.time())
        con = self.T.connect()
        con.execute("INSERT INTO runs(run_at, run_epoch, job, cron_job, profile, session_id, "
                    "session_started_at, session_seconds, prices_json, rate, rate_source, ledger_json) "
                    "VALUES('x', ?, 'manual', NULL, 'default', 's1', NULL, NULL, '{}', 0, 'default', '{}')",
                    (now,))
        rid = con.execute("SELECT MAX(run_id) FROM runs").fetchone()[0]
        # провайдер зависит от класса: локальный ряд не может идти через z.ai
        prov, url = ("custom", "http://192.168.1.107:8080/v1") if plan_class == "local" else ("zai", "https://api.z.ai")
        con.execute("INSERT INTO increments(run_id, run_at, run_epoch, window_start, window_end, "
                    "session_id, model, billing_provider, billing_base_url, billing_mode, task, "
                    "plan_class, input_delta, cache_delta, output_delta, attribution, note) "
                    "VALUES(?, 'x', ?, ?, ?, 's1', ?, ?, ?, '', 'chat', "
                    "?, ?, 0, 0, ?, ?)",
                    (rid, now, now - 100, now, model, prov, url, plan_class, tokens, attribution, note))
        con.commit()
        PC.PlanStateStore(os.path.join(self.T.STATE, "token_cost_plan.json")).save(
            {"anchor_date": datetime.now(PC.PEAK_TZ).date().isoformat()})
        return now - 100, now

    def seed_window(self, tokens=1000000, model="glm-5.3-flash", plan_class="zai_plan_by_rule"):
        """Одна строка с окном, попадающим в текущее месячное окно, и якорь = сегодня."""
        self.seed_row(tokens, model, plan_class)

    def capture(self, argv, rows=None):
        """Возвращает (доставляемое сообщение, лог). В сообщение уходит только то, что
        просил пользователь; оговорки пишутся в stderr и остаются в базе."""
        rows = rows if rows is not None else self.ROWS
        self.C.conn = lambda: fake_source(rows)
        self.T.src_connect = lambda: fake_source(rows)
        self.C.resolve_session = lambda c: "s1"
        self.C.fetch_prices = lambda: (None, "тест без сети")
        self.C.fetch_rate = lambda: (None, "тест без сети")
        sys.argv = ["token_cost.py"] + argv
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            delivered = self.C.main()
        return delivered, err.getvalue()

    def test_delivered_is_exactly_two_lines(self):
        """Правило пользователя: из всего вывода нужны ровно две строки."""
        out, log = self.capture([])
        lines = out.splitlines()
        self.assertEqual(len(lines), 2, f"доставляется не две строки: {out!r}")
        self.assertTrue(lines[0].startswith("Равно") and "на Z.ai" in lines[0], lines[0])
        self.assertEqual(lines[1],
                         "Эквивалентный кодовый план z.ai (эквивалентная модель glm-5.3-flash)",
                         lines[1])

    def test_caveats_are_not_delivered_but_are_in_the_log(self):
        out, log = self.capture([])
        for phrase in ("Суточное окно", "Месячный лимит", "MCP-вызовы", "Пик/офф-пик"):
            self.assertNotIn(phrase, out, f"размышление попало в сообщение: {out!r}")
            self.assertIn(phrase, log, f"оговорка пропала из лога: {log!r}")

    def test_unknown_plan_noted_in_the_log(self):
        self.seed_row(model="m", plan_class="unknown")
        out, log = self.capture([])
        self.assertIn("plan unknown", log, log)
        self.assertNotIn("plan unknown", out, out)

    def test_anomalies_reported_in_the_log(self):
        self.seed_row(model="m", plan_class="local", tokens=0, attribution="anomaly",
                      note="строка уменьшилась")
        out, log = self.capture([])
        self.assertIn("Anomaly-строки", log, log)
        self.assertNotIn("Anomaly", out, out)

    def test_dropped_intervals_are_named_in_the_log(self):
        self.seed_row(tokens=5000000, attribution="gap", note="интервал 2400 с")
        out, log = self.capture([])
        self.assertIn("Выбрасываемые интервалы", log, log)
        self.assertNotIn("Выбрасываемые интервалы", out, out)

    def test_no_dropped_interval_line_when_there_are_none(self):
        self.seed_row(tokens=1000000, attribution="delta")
        out, log = self.capture([])
        self.assertNotIn("Выбрасываемые интервалы", log, log)

    def test_dropped_interval_not_counted_in_plan_credits(self):
        self.seed_row(tokens=5000000, attribution="gap", note="интервал 2400 с")
        out, log = self.capture([])
        self.assertTrue(out.splitlines()[0].startswith("Равно 0.0%"),
                        f"выбрасываемый интервал попал в кредиты плана: {out!r}")

    def test_max_overflow_delivers_token_version(self):
        """Частный случай: каскад переполнен — доставляется прежняя версия про токены,
        и только переполнение, а не общее количество токенов."""
        import plan_core as PC
        start, end = self.seed_row(plan_class="local", tokens=7000000000)
        out, log = self.capture([])
        self.assertIn("Сравнительная цена токенов — только локальные LLM", out, out)
        self.assertIn("Работа аналогичного агента на GLM-5.3-Flash за последние сутки:", out, out)
        self.assertIn("переполнение Max", out, out)
        self.assertIn("План Max переполнен, переходим на пересчет токенов.", out,
                      f"строка о переходе не доставляется: {out[:200]}")
        day = datetime.fromtimestamp(end, PC.PEAK_TZ).date()
        share = PC.DayIntegralPeakModel().share(day, start, end)
        base = 7000000000 * 2.3 / 10000
        credits = base * (share + (1.0 - share) * PC.OFFPEAK_FACTOR)
        expected = 7000000000 * (credits - 930000) / credits
        self.assertIn(f"{expected/1e6:.2f}M", out,
                      f"переполнение не равно ожидаемому {expected/1e6:.2f}M: {out}")
        tail = out.split("переполнение Max", 1)[1]
        self.assertNotIn("7000.00M", tail,
                         f"блок пересчёта показывает общее количество токенов: {tail}")

    def test_paid_rows_do_not_enter_the_overflow_split(self):
        import plan_core as PC
        start, end = self.seed_row(plan_class="local", tokens=7000000000)
        self.seed_row(tokens=2000000, plan_class="zai_paid")
        out, log = self.capture([])
        day = datetime.fromtimestamp(end, PC.PEAK_TZ).date()
        share = PC.DayIntegralPeakModel().share(day, start, end)
        base = 7000000000 * 2.3 / 10000
        credits = base * (share + (1.0 - share) * PC.OFFPEAK_FACTOR)
        expected = 7000000000 * (credits - 930000) / credits
        tail = out.split("переполнение Max", 1)[-1]
        self.assertIn(f"{expected/1e6:.2f}M", tail,
                      f"оплаченная строка попала в расщепление: {tail[:200]}")
        self.assertIn("оплаченные", out, f"оплаченные не названы: {out[:300]}")

    def test_plan_state_updated_once(self):
        """Переход плана не печатается в сообщение: доставляется ровно две строки,
        состояние обновляется один раз."""
        self.seed_row(plan_class="local", tokens=400000000)
        out1, log1 = self.capture([])
        self.assertIn("Pro", out1.splitlines()[0], out1)
        with open(os.path.join(self.T.STATE, "token_cost_plan.json"), encoding="utf-8") as f:
            st = json.load(f)
        self.assertEqual(st.get("plan"), "pro", f"состояние плана не обновлено: {st}")
        out2, log2 = self.capture([])
        self.assertEqual(len(out2.splitlines()), 2, f"доставляется не две строки: {out2!r}")
        self.assertNotIn("Превышен план", out2, f"переход печатается в сообщение: {out2!r}")

    def test_dry_run_writes_nothing(self):
        out, log = self.capture(["--dry-run"])
        self.assertEqual(len(out.splitlines()), 2, out)
        self.assertFalse(os.path.exists(self.C.LEDGER), "dry-run записал копилку")
        self.assertFalse(os.path.exists(self.C.SNAP), "dry-run записал снимок")
        self.assertFalse(os.path.exists(self.T.DB), "dry-run создал базу")

    def test_dry_run_names_uncalculated_credits_in_the_log(self):
        out, log = self.capture(["--dry-run"])
        self.assertIn("не посчитан", log, f"dry-run молчит о том, что кредиты не считались: {out!r}")
        self.assertNotIn("не посчитан", out, out)

    def test_tick_is_silent(self):
        out, log = self.capture(["--tick"])
        self.assertEqual(out, "", f"tick печатает: {out!r}")
        self.assertTrue(os.path.exists(self.T.DB), "tick не записал запуск в базу")
        con = sqlite3.connect(self.T.DB)
        self.assertEqual(con.execute("SELECT job FROM runs").fetchone()[0], "tick")

    def test_hours_line_only_in_overflow_case(self):
        self.seed_row(plan_class="local", tokens=7000000000)
        out, log = self.capture([])
        self.assertIn("запуска 21:00 ещё нет", out, f"строка 'с 21:00' пропала: {out}")

    def test_db_failure_degrades_honestly(self):
        """База заблокирована — сообщение всё равно выходит (две строки), а проблема
        называется в логе, а не в сообщении."""
        def boom(*a, **k):
            raise sqlite3.OperationalError("database is locked")
        self.T.connect = boom
        out, log = self.capture([])
        self.assertEqual(len(out.splitlines()), 2, f"сообщение без базы не вышло: {out!r}")
        self.assertIn("database is locked", log, f"деградация базы не названа в логе: {log!r}")

    def test_manual_run_does_not_touch_the_network(self):
        """Баг был: ручной запуск каждый раз ходил на сайт за ценами (замер 0.619 с) —
        по правилу сеть трогается только на запланированном запуске 21:00, ручной читает
        сохранённые цены. Тест считает фактические обращения, а не читает исходник."""
        net = []
        self.C.conn = lambda: fake_source(self.ROWS)
        self.T.src_connect = lambda: fake_source(self.ROWS)
        self.C.resolve_session = lambda c: "s1"
        self.C.fetch_prices = lambda: (net.append("prices"), (None, "тест без сети"))[1]
        self.C.fetch_rate = lambda: (net.append("rate"), (None, "тест без сети"))[1]
        sys.argv = ["token_cost.py"]
        with contextlib.redirect_stderr(io.StringIO()):
            self.C.main()
        self.assertEqual(net, [], f"ручной запуск обратился к сети: {net}")
        sys.argv = ["token_cost.py", "--commit"]
        with contextlib.redirect_stderr(io.StringIO()):
            self.C.main()
        self.assertEqual(net, ["prices", "rate"],
                         f"запуск 21:00 обязан обновить цены и курс: {net}")

    def test_dry_run_does_not_touch_the_network(self):
        """Баг был: dry-run тоже ходил на сайт за ценами и курсом (замер 3.184 с) —
        сеть трогается только на запланированном запуске 21:00. Тест считает фактические
        обращения: dry-run обязан быть offline и ничего не писать."""
        net = []
        self.C.conn = lambda: fake_source(self.ROWS)
        self.T.src_connect = lambda: fake_source(self.ROWS)
        self.C.resolve_session = lambda c: "s1"
        self.C.fetch_prices = lambda: (net.append("prices"), (None, "тест без сети"))[1]
        self.C.fetch_rate = lambda: (net.append("rate"), (None, "тест без сети"))[1]
        sys.argv = ["token_cost.py", "--commit", "--dry-run"]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.C.main()
        self.assertEqual(net, [], f"dry-run обратился к сети: {net}")

    def test_tick_does_not_touch_the_network(self):
        net = []
        self.C.conn = lambda: fake_source(self.ROWS)
        self.T.src_connect = lambda: fake_source(self.ROWS)
        self.C.resolve_session = lambda c: "s1"
        self.C.fetch_prices = lambda: (net.append("prices"), (None, ""))[1]
        self.C.fetch_rate = lambda: (net.append("rate"), (None, ""))[1]
        sys.argv = ["token_cost.py", "--tick"]
        with contextlib.redirect_stderr(io.StringIO()):
            self.C.main()
        self.assertEqual(net, [], f"тик обратился к сети: {net}")

    def test_calculation_is_a_fraction_of_a_second(self):
        """Правило пользователя: расчёт — доля секунды. Замер без сети и без LLM."""
        self.C.conn = lambda: fake_source(self.ROWS)
        self.T.src_connect = lambda: fake_source(self.ROWS)
        self.C.resolve_session = lambda c: "s1"
        self.C.fetch_prices = lambda: (None, "тест без сети")
        self.C.fetch_rate = lambda: (None, "тест без сети")
        sys.argv = ["token_cost.py"]
        with contextlib.redirect_stderr(io.StringIO()):
            t0 = time.time()
            self.C.main()
        took = time.time() - t0
        self.assertLess(took, 0.5, f"расчёт занимает {took:.3f} с — правило требует долю секунды")

    def test_commit_writes_snapshot(self):
        """Баг был: блок записи снимка стоял после return в main() — --commit никогда
        не писал файл снимка, и суточная дельта по старому пути не считалась никогда."""
        out, log = self.capture(["--commit"])
        self.assertTrue(os.path.exists(self.C.SNAP), "--commit не написал снимок")
        with open(self.C.SNAP, encoding="utf-8") as f:
            snap = json.load(f)
        # локальные токены из фейковой source-таблицы (строка qwen/custom)
        self.assertEqual(snap["cumulative"], dict(zip(self.C.KEYS, (1000, 2000, 300))), snap)
        self.assertIn("saved_at", snap, snap)

    def test_manual_run_does_not_move_the_snapshot(self):
        self.capture(["--commit"])
        with open(self.C.SNAP, encoding="utf-8") as f:
            before = json.load(f)
        self.capture([])
        with open(self.C.SNAP, encoding="utf-8") as f:
            after = json.load(f)
        self.assertEqual(before["saved_at"], after["saved_at"],
                         "ручной запуск сдвинул базу суточного окна")

    def test_fresh_install_creates_state_dir(self):
        out, log = self.capture([])
        self.assertEqual(len(out.splitlines()), 2, out)
        self.assertTrue(os.path.isdir(os.path.dirname(self.C.LEDGER)), "state/ не создан")
        self.assertTrue(os.path.exists(self.C.LEDGER), "копилка не создана")


# --- парсеры (без сети, синтетический HTML) --------------------------------
class Parsers(Case):
    def test_rate_picks_tariff_not_card_price(self):
        html = "<p>Курс карты 2 990₽</p><p>Тариф «Для подписок»: курс 95,35 ₽ за доллар</p>"
        text = " ".join(html.split())
        self.assertEqual(self.C.fetch_rate_from_text(text), 95.35)

    def test_rate_without_currency_symbol(self):
        self.assertEqual(self.C.fetch_rate_from_text("Для подписок курс 95,35 за доллар"), 95.35)

    def test_rate_card_price_never_matches(self):
        # цена карты не имеет дроби — не пройдёт по шаблону
        self.assertIsNone(self.C.fetch_rate_from_text("Для подписок 2 990 ₽"))

    def test_price_row_by_header_and_label(self):
        html = ('<table><tr><th>Model</th><th>Input</th><th>Cached Input</th><th>Output</th></tr>'
                '<tr><td>GLM-5.3-Flash</td><td>$0.15</td><td>$0.03</td><td>$0.50</td></tr></table>')
        p, err = self.C.parse_price_table(html)
        self.assertIsNone(err)
        self.assertEqual(p, {"input": 0.15, "cached": 0.03, "output": 0.50})

    def test_price_other_model_table_ignored(self):
        html = ('<table><tr><th>Model</th><th>Input</th><th>Cached Input</th><th>Output</th></tr>'
                '<tr><td>GLM-4.5</td><td>$0.10</td><td>$0.02</td><td>$0.30</td></tr></table>')
        p, err = self.C.parse_price_table(html)
        self.assertIsNone(p)
        self.assertIn("не найдена", err)

    def test_price_free_cell(self):
        self.assertEqual(self.C.parse_price("Free"), 0.0)
        self.assertEqual(self.C.parse_price("-"), None)
        self.assertEqual(self.C.parse_price("$0.15"), 0.15)


class Guards(unittest.TestCase):
    """Минорные правила, которые кодом не реализованы: закрытый файл и homoglyph."""

    skill = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
    tests = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests")

    def scan(self, dirs, pattern):
        bad = []
        for d in dirs:
            for name in sorted(os.listdir(d)):
                if not name.endswith(".py"):
                    continue
                with open(os.path.join(d, name), encoding="utf-8") as f:
                    for n, line in enumerate(f, 1):
                        for m in pattern.findall(line):
                            bad.append("%s:%d %s" % (name, n, m if isinstance(m, str) else line.strip()[:80]))
        return bad

    def test_scripts_read_files_with_context_manager(self):
        bad = []
        for d in (self.skill, self.tests):
            for name in sorted(os.listdir(d)):
                if not name.endswith(".py"):
                    continue
                with open(os.path.join(d, name), encoding="utf-8") as f:
                    for n, line in enumerate(f, 1):
                        if re.search(r"\bopen\(", line) and "with open" not in line:
                            bad.append("%s:%d %s" % (name, n, line.strip()[:80]))
        self.assertFalse(bad, "файл читается без закрытия: " + "; ".join(bad))

    def test_no_latin_letters_inside_cyrillic_words(self):
        """Homoglyph не даёт синтаксической ошибки и не ловится тестом — слово
        просто никогда не совпадёт (правило навыка)."""
        pat = re.compile(r"[а-яё]+[a-z]+|[a-z]+[а-яё]+")
        bad = self.scan([self.skill, self.tests], pat)
        self.assertFalse(bad, "homoglyph: " + "; ".join(bad))


if __name__ == "__main__":
    unittest.main(verbosity=2)
