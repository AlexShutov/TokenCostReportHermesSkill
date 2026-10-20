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
        con.execute("INSERT INTO increments(run_id, run_at, run_epoch, window_start, window_end, "
                    "session_id, model, billing_provider, billing_base_url, billing_mode, task, "
                    "plan_class, input_delta, cache_delta, output_delta, attribution, note) "
                    "VALUES(?, 'x', ?, ?, ?, 's1', ?, 'zai', 'https://api.z.ai', '', 'chat', "
                    "?, ?, 0, 0, ?, ?)",
                    (rid, now, now - 100, now, model, plan_class, tokens, attribution, note))
        con.commit()
        PC.PlanStateStore(os.path.join(self.T.STATE, "token_cost_plan.json")).save(
            {"anchor_date": datetime.now(PC.PEAK_TZ).date().isoformat()})
        return now - 100, now

    def seed_window(self, tokens=1000000, model="glm-5.3-flash", plan_class="zai_plan_by_rule"):
        """Одна строка с окном, попадающим в текущее месячное окно, и якорь = сегодня."""
        self.seed_row(tokens, model, plan_class)

    def test_shape_plan_first(self):
        self.seed_window()
        out = self.capture([])
        first = out.splitlines()[0]
        self.assertTrue(first.startswith("Равно"), f"первая строка не про план: {first!r}")
        self.assertIn("на Z.ai", first)
        for line in ("Эквивалентный кодовый план z.ai", "курс взят из «Плати по миру»"):
            self.assertIn(line, out)
        # расчёт токенов — частный случай: план не переполнен, поэтому он не печатается
        self.assertIn("Расчёт токенов не применяется", out)
        self.assertNotIn("Сэкономлено за последние сутки", out)

    def test_dry_run_writes_nothing(self):
        out = self.capture(["--dry-run"])
        self.assertIn("тест без сети", out)
        self.assertFalse(os.path.exists(self.C.LEDGER), "dry-run записал копилку")
        self.assertFalse(os.path.exists(self.C.SNAP), "dry-run записал снимок")
        self.assertFalse(os.path.exists(self.T.DB), "dry-run создал базу")

    def test_tick_is_silent(self):
        out = self.capture(["--tick"])
        self.assertEqual(out, "", f"tick печатает: {out!r}")
        self.assertTrue(os.path.exists(self.T.DB), "tick не записал запуск в базу")
        con = sqlite3.connect(self.T.DB)
        self.assertEqual(con.execute("SELECT job FROM runs").fetchone()[0], "tick")

    def test_zai_block_separates_plan_and_paid(self):
        # строка переполняет каскад — включается частный случай с расчётом токенов
        self.seed_row(tokens=7000000000)
        rows = [("s1", "glm", "zai", "https://api.z.ai", "", "chat", 7000000000, 0, 0, 0, 100, 100),
                ("s2", "glm", "zai", "https://api.z.ai", "", "chat", 1200000, 0, 0, 0.42, 100, 100)]
        out = self.capture([], rows=rows)
        self.assertIn("оплаченные", out, f"блок z.ai не различает план и оплату: {out}")
        self.assertIn("Не учтено (z.ai coding plan): 7000.00M", out, out)
        self.assertIn("1.20M", out, out)

    def test_max_overflow_prints_token_calculation_on_overflow_only(self):
        """Частный случай: когда каскад переполнен, прежний расчёт токенов включается,
        но показывает только число переполнения (вычет суммарного лимита Light+Pro+Max),
        а не общее количество токенов."""
        import plan_core as PC
        start, end = self.seed_row(tokens=7000000000)
        out = self.capture([])
        self.assertIn("План Max переполнен, переходим на пересчет токенов.", out)
        self.assertIn("Пересчёт токенов — только переполнение Max", out)
        # ожидание считается тем же модельным рядом на том же окне строки:
        # доля пика зависит от того, когда запущен тест
        day = datetime.fromtimestamp(end, PC.PEAK_TZ).date()
        share = PC.DayIntegralPeakModel().share(day, start, end)
        base = 7000000000 * 2.3 / 10000
        credits = base * (share + (1.0 - share) * PC.OFFPEAK_FACTOR)
        frac = (credits - 930000) / credits
        expected = 7000000000 * frac
        self.assertIn(f"{expected/1e6:.2f}M", out,
                      f"переполнение не равно ожидаемому {expected/1e6:.2f}M (день {day}, доля {share}): {out}")
        # общее количество токенов показывать нельзя — только переполнение
        # (блок плана называет полный объём окна — это его норма; проверяем
        # именно блок пересчёта токенов)
        tail = out.split("Пересчёт токенов", 1)[1]
        self.assertNotIn("7000.00M", tail,
                         f"блок пересчёта показывает общее количество токенов: {tail}")

    def test_paid_rows_do_not_enter_the_overflow_split(self):
        """Баг M1 в частном случае: оплаченные z.ai не участвуют в расщеплении
        по лимитам каскада — переполнение считается только по плановым строкам."""
        import plan_core as PC
        start, end = self.seed_row(tokens=7000000000)
        self.seed_row(tokens=2000000, plan_class="zai_paid")
        out = self.capture([])
        day = datetime.fromtimestamp(end, PC.PEAK_TZ).date()
        share = PC.DayIntegralPeakModel().share(day, start, end)
        base = 7000000000 * 2.3 / 10000
        credits = base * (share + (1.0 - share) * PC.OFFPEAK_FACTOR)
        expected = 7000000000 * (credits - 930000) / credits
        tail = out.split("Пересчёт токенов", 1)[-1]
        self.assertIn(f"{expected/1e6:.2f}M", tail,
                      f"оплаченная строка попала в расщепление: {tail[:200]}")
        self.assertIn("Оплаченные z.ai в окне", out,
                      f"оплаченные не названы отдельной строкой: {out[:300]}")

    def test_plan_transition_message_printed_once(self):
        """Переход Light -> Pro сообщается один раз: состояние плана обновляется
        и в основной ветке (не только в ветке переполнения Max)."""
        # строка, которая переполняет месячный лимит Light (~44285.7 кредитов)
        self.seed_row(tokens=400000000)
        out1 = self.capture([])
        self.assertIn("Превышен план Light, перешли на Pro", out1, out1)
        st = json.load(open(os.path.join(self.T.STATE, "token_cost_plan.json")))
        self.assertEqual(st.get("plan"), "pro", f"состояние плана не обновлено: {st}")
        out2 = self.capture([])
        self.assertNotIn("Превышен план", out2,
                         f"переход сообщается повторно на каждом запуске: {out2}")

    def test_report_includes_plan_block(self):
        self.seed_window()
        out = self.capture([])
        self.assertIn("Эквивалентный кодовый план z.ai", out, out)
        self.assertIn("использовано", out, out)
        self.assertIn("Light плана", out, out)
        self.assertIn("месячного лимита", out, out)
        # месячного лимита на сайте нет — блок обязан называть, что он выведен
        self.assertIn("только 5-часовой и недельный", out, out)
        self.assertIn("MCP-вызовы", out, out)

    def test_dry_run_plan_block_writes_no_state(self):
        out = self.capture(["--dry-run"])
        self.assertIn("Эквивалентный кодовый план z.ai", out, out)
        # dry-run не создаёт базу: блок обязан честно назвать, что кредиты не считались,
        # и всё равно печатать заголовок и оговорку о выводе месячного лимита
        self.assertIn("не посчитан", out, out)
        self.assertIn("только 5-часовой и недельный", out, out)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".hermes", "state",
                                                     "token_cost_plan.json")),
                         "dry-run создал файл состояния плана")

    def test_hours_line_not_mislabelled_without_commit_run(self):
        # строка 'с 21:00' существует только в частном случае (переполнение Max)
        self.seed_row(tokens=7000000000)
        out = self.capture([])
        self.assertIn("запуска 21:00 ещё нет", out,
                      f"строка 'с 21:00' печатается без запуска 21:00 — метка врёт: {out}")

    def test_anomalies_reported(self):
        self.seed_row(model="m", plan_class="local", tokens=0, attribution="anomaly",
                      note="строка уменьшилась")
        out = self.capture([])
        self.assertIn("Anomaly-строки", out, f"anomaly не показаны в отчёте: {out}")

    def test_unknown_plan_noted(self):
        self.seed_row(model="m", plan_class="unknown")
        out = self.capture([])
        self.assertIn("plan unknown", out, f"незаписанный провайдер не отражён: {out}")

    def test_db_failure_degrades_honestly(self):
        """База заблокирована/недоступна — отчёт всё равно должен выйти и честно
        сказать, что план не посчитан, а не упасть."""
        def boom(*a, **k):
            raise sqlite3.OperationalError("database is locked")
        self.T.connect = boom
        out = self.capture([])
        self.assertIn("не посчитан", out, f"деградация базы не показана: {out}")
        self.assertIn("database is locked", out, out)
        self.assertIn("Эквивалентный кодовый план z.ai", out,
                      f"отчёт без базы должен начинаться с блока плана: {out}")

    def test_fresh_install_creates_state_dir(self):
        """Чистая установка: каталога ~/.hermes/state может не быть — первый запуск
        не должен падать с FileNotFoundError."""
        out = self.capture([])
        self.assertIn("Эквивалентный кодовый план z.ai", out)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
