#!/usr/bin/env python3
"""Тесты CLI-обёртки блока плана (plan_usage.py).

Логика ядра (формула кредитов, пик, каскад, окно, расщепление переполнения)
тестируется поведенчески в test_plan_core.py. Здесь — поведение обёртки:
аргументы, состояние, деградация, запись перехода плана, доставляемый блок.
Только stdlib, offline, Python 3.9+. Запуск: python3 tests/test_plan_usage.py
Каждый тест — в отдельном временном HOME; инкременты сеются прямыми INSERT.
"""
import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(SKILL, "scripts")
sys.path.insert(0, SCRIPTS)

PEAK_TZ = timezone(timedelta(hours=8))
INC_COLS = ("run_id", "run_at", "run_epoch", "window_start", "window_end", "session_id",
            "model", "billing_provider", "billing_base_url", "billing_mode", "task",
            "plan_class", "input_delta", "cache_delta", "output_delta", "attribution", "note")


def ts(y, m, d, h, mi, tz=PEAK_TZ):
    return datetime(y, m, d, h, mi, tzinfo=tz).timestamp()


class Case(unittest.TestCase):
    def setUp(self):
        # обёртка решает пути при импорте — тест обязан выгружать модули из
        # sys.modules, иначе они читают HOME предыдущего теста
        for name in ("token_cost", "token_cost_db", "plan_core", "plan_usage"):
            sys.modules.pop(name, None)
        self.home = tempfile.mkdtemp(prefix="tcr-plan-")
        os.environ["HOME"] = self.home
        import token_cost_db as T
        import plan_core as C
        import plan_usage as U
        self.T, self.C, self.U = T, C, U
        self.con = self.T.connect()
        self.plan_file = os.path.join(self.home, ".hermes", "state", "token_cost_plan.json")

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.home, ignore_errors=True)

    def seed(self, rows):
        """rows: (window_start, window_end, model, plan_class, i, c, o, attribution)."""
        for r in rows:
            rid = self.con.execute("SELECT COUNT(*) FROM runs").fetchone()[0] + 1
            self.con.execute("INSERT INTO runs(run_id, run_at, run_epoch, job, cron_job, profile, "
                             "session_id, session_started_at, session_seconds, prices_json, rate, "
                             "rate_source, ledger_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (rid, datetime.now(PEAK_TZ).isoformat(), r[1], "test", None, "default",
                              "s1", None, None, "{}", 0, "test", "{}"))
            self.con.execute("INSERT INTO increments(" + ", ".join(INC_COLS) + ") VALUES(" +
                             ", ".join("?" * len(INC_COLS)) + ")",
                             (rid, datetime.now(PEAK_TZ).isoformat(), r[1], r[0], r[1], "s1", r[2],
                              "zai", "https://api.z.ai", "", "", r[3], r[4], r[5], r[6], r[7], ""))
        self.con.commit()

    def capture(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = self.U.main(argv)
        return code, buf.getvalue()

    def state(self):
        if not os.path.exists(self.plan_file):
            return {}
        with open(self.plan_file) as f:
            return json.load(f)


# --- только python3 ------------------------------------------------------------
class Python3Only(Case):
    def test_no_requests_and_shebang_is_python3(self):
        bad = []
        for f in sorted(os.listdir(SCRIPTS)):
            if not f.endswith(".py"):
                continue
            with open(os.path.join(SCRIPTS, f)) as fh:
                text = fh.read()
            if re.search(r"^\s*import requests\b", text, re.M):
                bad.append(f)
            for line in text.splitlines():
                if line.strip().startswith("#!") and "python3" not in line:
                    bad.append(f)
        self.assertEqual(bad, [])

    def test_core_has_no_third_party_imports(self):
        with open(os.path.join(SCRIPTS, "plan_core.py")) as fh:
            text = fh.read()
        for line in text.splitlines():
            m = re.match(r"\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)", line)
            if m:
                name = m.group(1)
                self.assertIn(name, {"json", "os", "sqlite3", "sys", "datetime", "calendar",
                                     "abc", "typing", "token_cost", "token_cost_db", "plan_core"},
                                "импорт не из stdlib: %s" % name)


# --- якорь месяца ---------------------------------------------------------------
class Anchor(Case):
    def test_default_anchor_day_is_19(self):
        # правило пользователя: при установке навыка с нуля предлагается 19-е число,
        # чтобы данные, уже имеющиеся в базе, попали в расчёт
        self.assertEqual(self.U.DEFAULT_ANCHOR_DAY, 19)

    def test_default_anchor_used_when_nothing_saved(self):
        code, out = self.capture(["--month", "2026-10"])
        self.assertEqual(code, 0)
        self.assertIn("день начала отсчёта — 19 (default_derived)", out)

    def test_set_anchor_persists_and_wins_over_default(self):
        code, out = self.capture(["--set-anchor", "15"])
        self.assertEqual(code, 0)
        self.assertIn("сохранён: 15", out)
        self.assertEqual(self.state()["anchor_date"], "2026-10-15")
        code, out = self.capture(["--month", "2026-10"])
        self.assertIn("день начала отсчёта — 15 (saved)", out)

    def test_date_anchor(self):
        d = self.C.parse_anchor("2026-10-15")
        self.assertEqual(d.day, 15)
        self.assertEqual(d.isoformat(), "2026-10-15")

    def test_anchor_day_missing_in_month_clamps(self):
        # якорь 31, февраль: дня 31 нет — берётся последний день месяца, это называется
        code, out = self.capture(["--set-anchor", "31", "--month", "2026-02"])
        self.assertIn("ограничение метода", out)

    def test_dry_run_writes_nothing(self):
        code, out = self.capture(["--dry-run", "--month", "2026-10"])
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.plan_file))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM increments").fetchone()[0], 0)

    def test_invalid_plan_and_model_return_2(self):
        self.assertEqual(self.capture(["--plan", "ultra"])[0], 2)
        self.assertEqual(self.capture(["--model", "glm-9"])[0], 2)
        self.assertEqual(self.capture(["--peak", "moon"])[0], 2)


# --- доставляемый блок ----------------------------------------------------------
class Block(Case):
    def test_first_line_is_plan_equivalence(self):
        # 7 453 416 входных токенов = 1000 кредитов (2.3 x 7/12 на 10000 токенов)
        # -> 1000 / 44285.7 = 2.258% -> "Равно 2.3%"
        self.seed([(ts(2026, 10, 19, 15, 0), ts(2026, 10, 19, 15, 30), "glm-5.3-flash",
                   "zai_plan_by_rule", 7453416, 0, 0, "delta")])
        code, out = self.capture(["--dry-run", "--month", "2026-10"])
        self.assertEqual(out.splitlines()[0], "Равно 2.3% плана Light на Z.ai")
        self.assertIn("использовано", out)
        self.assertIn("Light плана", out)
        self.assertIn("месячного лимита", out)
        self.assertIn("только 5-часовой и недельный", out)

    def test_block_names_the_derivation(self):
        code, out = self.capture(["--dry-run", "--month", "2026-10"])
        self.assertIn("выведен из недельного", out)

    def test_anomaly_rows_not_counted(self):
        self.seed([(ts(2026, 10, 19, 15, 0), ts(2026, 10, 19, 15, 30), "glm-5.3-flash",
                   "zai_plan_by_rule", 10000, 0, 0, "delta"),
                   (ts(2026, 10, 19, 15, 30), ts(2026, 10, 19, 16, 0), "glm-5.3-flash",
                    "zai_plan_by_rule", -5000, 0, 0, "anomaly")])
        code, out = self.capture(["--dry-run", "--month", "2026-10"])
        self.assertIn("0.01M", out)          # только 10 000, а не 5 000

    def test_local_rows_not_counted_in_plan(self):
        self.seed([(ts(2026, 10, 19, 15, 0), ts(2026, 10, 19, 15, 30), "qwen",
                   "local", 100000, 0, 0, "delta")])
        code, out = self.capture(["--dry-run", "--month", "2026-10"])
        self.assertIn("Строк плана в окне нет", out)

    def test_rows_outside_month_window_excluded(self):
        self.seed([(ts(2026, 9, 30, 15, 0), ts(2026, 9, 30, 15, 30), "glm-5.3-flash",
                   "zai_plan_by_rule", 100000, 0, 0, "delta")])
        code, out = self.capture(["--dry-run", "--month", "2026-10"])
        self.assertIn("Строк плана в окне нет", out)

    def test_unknown_model_named(self):
        self.seed([(ts(2026, 10, 19, 15, 0), ts(2026, 10, 19, 15, 30), "glm-4.7",
                   "zai_plan_by_rule", 10000, 0, 0, "delta")])
        code, out = self.capture(["--dry-run", "--month", "2026-10"])
        self.assertIn("без опубликованных мультипликаторов", out)
        self.assertIn("glm-4.7", out)

    def test_mcp_limitation_stated(self):
        code, out = self.capture(["--dry-run", "--month", "2026-10"])
        self.assertIn("MCP-вызовы", out)

    def test_block_names_degradation_when_base_missing(self):
        self.T.DB = os.path.join(self.home, "no-such.db")
        code, out = self.capture(["--dry-run", "--month", "2026-10"])
        self.assertIn("Эквивалентный кодовый план z.ai", out)
        self.assertIn("не посчитан", out)
        self.assertIn("только 5-часовой и недельный", out)
        self.assertNotIn("использовано", out)

    def test_point_rows_flagged_as_estimate(self):
        self.seed([(ts(2026, 10, 19, 15, 0), ts(2026, 10, 19, 15, 0), "glm-5.3-flash",
                   "zai_plan_by_rule", 10000, 0, 0, "backfill")])
        code, out = self.capture(["--dry-run", "--month", "2026-10"])
        self.assertIn("оценка", out)

    def test_by_actual_model_uses_row_multipliers(self):
        # 10000 токенов glm-5.3: по мультипликаторам строки = 6.9 x 7/12 = 4.025 кредита,
        # по эквивалентной модели = 2.3 x 7/12 = 1.342
        self.seed([(ts(2026, 10, 19, 15, 0), ts(2026, 10, 19, 15, 30), "glm-5.3",
                   "zai_plan_by_rule", 10000, 0, 0, "delta")])
        code, out = self.capture(["--dry-run", "--month", "2026-10", "--by-actual-model"])
        self.assertIn("по мультипликаторам фактической модели", out)
        self.assertIn("Кредиты за окно: 4.0", out)
        code, out = self.capture(["--dry-run", "--month", "2026-10"])
        self.assertIn("Кредиты за окно: 1.3", out)

    def test_default_peak_names_day_integral(self):
        code, out = self.capture(["--dry-run", "--month", "2026-10"])
        self.assertIn("интегральная оценка по дню", out)

    def test_window_peak_names_the_approximation(self):
        code, out = self.capture(["--dry-run", "--month", "2026-10", "--peak", "window"])
        self.assertIn("приближение", out)
        self.assertIn("по моменту каждого запроса", out)


# --- переход плана ---------------------------------------------------------------
class Transition(Case):
    def test_overflow_message_and_state_write(self):
        # 400M входных токенов = 53666.7 кредитов > месячного лимита Light (44285.7)
        self.seed([(ts(2026, 10, 19, 15, 0), ts(2026, 10, 19, 15, 30), "glm-5.3-flash",
                   "zai_plan_by_rule", 400000000, 0, 0, "delta")])
        code, out = self.capture(["--month", "2026-10"])
        self.assertIn("Превышен план Light, перешли на Pro", out)
        self.assertEqual(self.state()["plan"], "pro")

    def test_no_message_when_plan_unchanged(self):
        self.seed([(ts(2026, 10, 19, 15, 0), ts(2026, 10, 19, 15, 30), "glm-5.3-flash",
                   "zai_plan_by_rule", 10000, 0, 0, "delta")])
        code, out = self.capture(["--month", "2026-10"])
        self.assertNotIn("Превышен", out)
        self.assertEqual(self.state()["plan"], "light")

    def test_dry_run_does_not_write_plan_state(self):
        self.seed([(ts(2026, 10, 19, 15, 0), ts(2026, 10, 19, 15, 30), "glm-5.3-flash",
                   "zai_plan_by_rule", 400000000, 0, 0, "delta")])
        code, out = self.capture(["--dry-run", "--month", "2026-10"])
        self.assertIn("Превышен план Light, перешли на Pro", out)
        self.assertFalse(os.path.exists(self.plan_file))

    def test_max_overflow_message(self):
        # суммарный лимит каскада = 44285.7 + 265714.3 + 620000 = 930000 кредитов;
        # 7 млрд входных токенов = 940833.3 кредитов — за пределами Max
        self.seed([(ts(2026, 10, 19, 15, 0), ts(2026, 10, 19, 15, 30), "glm-5.3-flash",
                   "zai_plan_by_rule", 7000000000, 0, 0, "delta")])
        code, out = self.capture(["--month", "2026-10"])
        self.assertIn("План Max переполнен, переходим на пересчет токенов.", out)


if __name__ == "__main__":
    unittest.main()
