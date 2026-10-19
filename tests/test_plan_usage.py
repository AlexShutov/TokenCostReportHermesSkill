#!/usr/bin/env python3
"""Тесты блока эквивалентного кодового плана z.ai (stdlib, offline, Python 3.9+).

Запуск: python3 tests/test_plan_usage.py
Каждый тест — в отдельном временном HOME; база строится прямыми INSERT в
increments (временные окна задаёт тест), сеть не трогается.
"""
import contextlib
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(SKILL, "scripts")
sys.path.insert(0, SCRIPTS)

PEAK_TZ = timezone(timedelta(hours=8))
SRC_COLS = ("session_id", "model", "billing_provider", "billing_base_url", "billing_mode",
            "task", "input_tokens", "cache_read_tokens", "output_tokens", "actual_cost_usd",
            "first_seen", "last_seen")
INC_COLS = ("run_id", "run_at", "run_epoch", "window_start", "window_end", "session_id",
            "model", "billing_provider", "billing_base_url", "billing_mode", "task",
            "plan_class", "input_delta", "cache_delta", "output_delta", "attribution", "note")


def ts(y, m, d, h, mi, tz=PEAK_TZ):
    return datetime(y, m, d, h, mi, tzinfo=tz).timestamp()


class Case(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="tcr-plan-")
        os.environ["HOME"] = self.home
        for name in ("token_cost_db", "plan_usage"):
            sys.modules.pop(name, None)
        import token_cost_db as T
        import plan_usage as P
        self.T, self.P = T, P
        self.con = self.T.connect()

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
                             ",".join("?" * len(INC_COLS)) + ")",
                             (rid, datetime.now(PEAK_TZ).isoformat(), r[1], r[0], r[1], "s1", r[2],
                              "zai", "https://api.z.ai", "", "", r[3], r[4], r[5], r[6], r[7], ""))
        self.con.commit()

    def capture(self, argv):
        sys.argv = ["plan_usage.py"] + argv
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.P.main()
        return buf.getvalue()


# --- формула кредитов ---------------------------------------------------------
class CreditFormula(Case):
    def test_worked_example_peak(self):
        # 100 000 input (95 000 из них кэш) + 2 000 output на Flash, всё в пике
        toks = {"input": 5000, "cached": 95000, "output": 2000}
        self.assertAlmostEqual(self.P.credits_for(toks, self.P.MULTIPLIERS["glm-5.3-flash"], 1.0), 8.07, places=6)

    def test_offpeak_is_half(self):
        toks = {"input": 5000, "cached": 95000, "output": 2000}
        f = self.P.credits_for(toks, self.P.MULTIPLIERS["glm-5.3-flash"], 0.0)
        self.assertAlmostEqual(f, 4.035, places=6)
        self.assertAlmostEqual(f, self.P.credits_for(toks, self.P.MULTIPLIERS["glm-5.3-flash"], 1.0) * 0.5, places=6)

    def test_blended_window(self):
        # половина окна в пике -> коэффициент 0.75: 2.3 * 0.75 = 1.725
        toks = {"input": 10000, "cached": 0, "output": 0}
        self.assertAlmostEqual(self.P.credits_for(toks, self.P.MULTIPLIERS["glm-5.3-flash"], 0.5), 1.725, places=6)

    def test_glm53_multipliers(self):
        toks = {"input": 10000, "cached": 0, "output": 0}
        self.assertAlmostEqual(self.P.credits_for(toks, self.P.MULTIPLIERS["glm-5.3"], 1.0), 6.9, places=6)

    def test_by_actual_model(self):
        # строка glm-5.3 считается мультипликаторами glm-5.3, а не эквивалентной модели
        self.seed([(ts(2026, 10, 19, 15, 0), ts(2026, 10, 19, 15, 30), "glm-5.3",
                   "zai_plan_by_rule", 10000, 0, 0, "delta")])
        out = self.capture(["--month", "2026-10", "--dry-run", "--by-actual-model"])
        self.assertIn("6.9", out)
        # 6.9 кредитов (в пике), против 2.3 по эквивалентной модели
        self.assertIn("6.9", out.split("Кредиты за окно:")[1].split("\n")[0])
        out2 = self.capture(["--month", "2026-10", "--dry-run"])
        self.assertIn("2.3", out2)


# --- пик / офф-пик ------------------------------------------------------------
class PeakOffpeak(Case):
    def test_peak_window(self):
        self.assertAlmostEqual(self.P.peak_fraction(ts(2026, 10, 19, 15, 0), ts(2026, 10, 19, 16, 0)), 1.0)

    def test_weekend_all_day_offpeak(self):
        # воскресенье 15:00-16:00 SGT — внепик весь день
        self.assertAlmostEqual(self.P.peak_fraction(ts(2026, 10, 18, 15, 0), ts(2026, 10, 18, 16, 0)), 0.0)

    def test_partial_peak(self):
        # пн 13:00 -> пн 15:00 SGT: пик только 14:00-15:00 = 1/2 окна
        self.assertAlmostEqual(self.P.peak_fraction(ts(2026, 10, 19, 13, 0), ts(2026, 10, 19, 15, 0)), 0.5)

    def test_multi_day_window(self):
        # пн 17:00 -> вт 15:00 SGT: пик 17-18 (1 ч) + 14-15 (1 ч) = 2 ч из 22 ч
        f = self.P.peak_fraction(ts(2026, 10, 19, 17, 0), ts(2026, 10, 20, 15, 0))
        self.assertAlmostEqual(f, 2.0 / 22.0, places=6)

    def test_point_window_uses_moment(self):
        # backfill: window_start == window_end, решение по моменту last_seen
        self.assertAlmostEqual(self.P.peak_fraction(ts(2026, 10, 19, 15, 0), ts(2026, 10, 19, 15, 0)), 1.0)
        self.assertAlmostEqual(self.P.peak_fraction(ts(2026, 10, 18, 15, 0), ts(2026, 10, 18, 15, 0)), 0.0)

    def test_saturday_point(self):
        self.assertAlmostEqual(self.P.peak_fraction(ts(2026, 10, 17, 15, 0), ts(2026, 10, 17, 15, 0)), 0.0)


# --- якорь месяца -------------------------------------------------------------
class Anchor(Case):
    def test_default_anchor_is_first_day(self):
        st = self.P.default_state()
        self.assertEqual(st["anchor_day"], 1)
        self.assertEqual(st["anchor_source"], "default")

    def test_set_anchor_persists(self):
        out = self.capture(["--set-anchor", "15"])
        self.assertIn("сохранён: 15", out)
        st = self.P.load_state()
        self.assertEqual(st["anchor_day"], 15)
        self.assertEqual(st["anchor_source"], "user")
        # месяц отсчёта начинается с 15-го
        s, e, days = self.P.month_window(15, datetime(2026, 10, 20, tzinfo=PEAK_TZ))
        self.assertEqual(s.day, 15)
        self.assertEqual(days, 31)          # 15 октября -> 15 ноября

    def test_date_anchor(self):
        d, full = self.P.anchor_day_from("2026-10-15")
        self.assertEqual(d, 15)
        self.assertEqual(full, "2026-10-15")

    def test_anchor_day_missing_in_month_clamps(self):
        # якорь 31, февраль: дня 31 нет -> берётся последний день месяца (ограничение)
        s, e, days = self.P.month_window(31, datetime(2026, 2, 10, tzinfo=PEAK_TZ))
        self.assertEqual(s.day, 28)
        self.assertEqual(days, 31)          # 28 февраля -> 31 марта

    def test_dry_run_writes_nothing(self):
        self.capture(["--dry-run", "--month", "2026-10"])
        self.assertFalse(os.path.exists(self.P.PLAN_FILE))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM increments").fetchone()[0], 0)


# --- месячный лимит и каскад -------------------------------------------------
class Caps(Case):
    def test_monthly_cap_is_derived_from_weekly(self):
        # Light: 10000 x (30/7) — на сайте месячного лимита нет, это вывод
        self.assertAlmostEqual(self.P.monthly_cap("light", 30), 10000 * 30 / 7.0, places=6)
        self.assertEqual(self.P.PLANS["light"], {"5h": 2000, "weekly": 10000})
        self.assertEqual(self.P.PLANS["pro"], {"5h": 12000, "weekly": 60000})
        self.assertEqual(self.P.PLANS["max"], {"5h": 28000, "weekly": 140000})

    def test_cascade_light_then_pro(self):
        # окно 1 октября -> 1 ноября = 31 день; Light = 10000 x 31/7 = 44285.7
        # 45 000 кредитов -> Light 100%, излишек переносится на Pro
        res, days = self.P.cascade(45000.0, 1, datetime(2026, 10, 20, tzinfo=PEAK_TZ))
        self.assertEqual(days, 31)
        self.assertEqual(res[0]["plan"], "light")
        self.assertAlmostEqual(res[0]["pct"], 100.0)
        self.assertEqual(res[1]["plan"], "pro")
        self.assertAlmostEqual(res[1]["pct"], (45000 - 10000 * 31 / 7.0) / (60000 * 31 / 7.0) * 100.0, places=6)

    def test_cascade_light_pro_max(self):
        res, days = self.P.cascade(400000.0, 1, datetime(2026, 10, 20, tzinfo=PEAK_TZ))
        self.assertEqual([r["plan"] for r in res], ["light", "pro", "max"])
        self.assertAlmostEqual(res[0]["pct"], 100.0)
        self.assertAlmostEqual(res[1]["pct"], 100.0)
        cap_max = 140000 * 31 / 7.0
        rem = 400000 - 10000 * 31 / 7.0 - 60000 * 31 / 7.0
        self.assertAlmostEqual(res[2]["pct"], rem / cap_max * 100.0, places=6)

    def test_under_light_cap_single_line(self):
        res, days = self.P.cascade(1000.0, 1, datetime(2026, 10, 20, tzinfo=PEAK_TZ))
        self.assertEqual(len(res), 1)
        self.assertAlmostEqual(res[0]["pct"], 1000 / (10000 * 31 / 7.0) * 100.0, places=6)

    def test_block_prints_the_required_phrase(self):
        self.seed([(ts(2026, 10, 19, 15, 0), ts(2026, 10, 19, 15, 30), "glm-5.3-flash",
                   "zai_plan_by_rule", 10000, 0, 0, "delta")])
        out = self.capture(["--dry-run", "--month", "2026-10"])
        self.assertIn("использовано", out)
        self.assertIn("Light плана", out)
        self.assertIn("месячного лимита", out)
        self.assertIn("только 5-часовой и недельный", out)

    def test_block_names_the_derivation(self):
        out = self.capture(["--dry-run", "--month", "2026-10"])
        self.assertIn("выведен из недельного", out)

    def test_anomaly_rows_not_counted(self):
        self.seed([(ts(2026, 10, 19, 15, 0), ts(2026, 10, 19, 15, 30), "glm-5.3-flash",
                   "zai_plan_by_rule", 10000, 0, 0, "delta"),
                   (ts(2026, 10, 19, 15, 30), ts(2026, 10, 19, 16, 0), "glm-5.3-flash",
                    "zai_plan_by_rule", -5000, 0, 0, "anomaly")])
        out = self.capture(["--dry-run", "--month", "2026-10"])
        # только 10 000 входных токенов, а не 5 000
        self.assertIn("0.01M", out)

    def test_local_rows_not_counted_in_plan(self):
        self.seed([(ts(2026, 10, 19, 15, 0), ts(2026, 10, 19, 15, 30), "qwen",
                   "local", 100000, 0, 0, "delta")])
        out = self.capture(["--dry-run", "--month", "2026-10"])
        self.assertIn("Кредиты за окно: 0.0", out)

    def test_rows_outside_month_window_excluded(self):
        # строка в сентябре — вне месячного окна октября
        self.seed([(ts(2026, 9, 30, 15, 0), ts(2026, 9, 30, 15, 30), "glm-5.3-flash",
                   "zai_plan_by_rule", 100000, 0, 0, "delta")])
        out = self.capture(["--dry-run", "--month", "2026-10"])
        self.assertIn("Кредиты за окно: 0.0", out)

    def test_unknown_model_named(self):
        self.seed([(ts(2026, 10, 19, 15, 0), ts(2026, 10, 19, 15, 30), "glm-4.7",
                   "zai_plan_by_rule", 10000, 0, 0, "delta")])
        out = self.capture(["--dry-run", "--month", "2026-10"])
        self.assertIn("без опубликованных мультипликаторов", out)
        self.assertIn("glm-4.7", out)

    def test_mcp_limitation_stated(self):
        out = self.capture(["--dry-run", "--month", "2026-10"])
        self.assertIn("MCP-вызовы", out)

    def test_block_names_degradation_when_base_missing(self):
        # база недоступна — блок печатает заголовок, честную строку и оговорку о выводе
        self.T.DB = os.path.join(self.home, "no-such.db")
        out = self.capture(["--dry-run", "--month", "2026-10"])
        self.assertIn("Эквивалентный кодовый план z.ai", out)
        self.assertIn("не посчитан", out)
        self.assertIn("только 5-часовой и недельный", out)
        self.assertNotIn("использовано", out)

    def test_point_rows_flagged_as_estimate(self):
        self.seed([(ts(2026, 10, 19, 15, 0), ts(2026, 10, 19, 15, 0), "glm-5.3-flash",
                   "zai_plan_by_rule", 10000, 0, 0, "backfill")])
        out = self.capture(["--dry-run", "--month", "2026-10"])
        self.assertIn("оценка", out)


if __name__ == "__main__":
    unittest.main()
