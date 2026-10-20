#!/usr/bin/env python3
"""Тесты ядра блока плана (plan_core.py) — все ветви логики.

Только stdlib, offline, Python 3.9+. Запуск: python3 tests/test_plan_core.py
Никакого реального HOME: источник инкрементов — in-memory SQLite, состояние —
временный файл.
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import plan_core as P                                    # noqa: E402

PEAK_TZ = P.PEAK_TZ
INC_COLS = ("run_id", "run_at", "run_epoch", "window_start", "window_end", "session_id",
           "model", "billing_provider", "billing_base_url", "billing_mode", "task",
           "plan_class", "input_delta", "cache_delta", "output_delta", "attribution", "note")


def ts(y, m, d, h, mi=0, tz=PEAK_TZ):
    return datetime(y, m, d, h, mi, tzinfo=tz).timestamp()


def row(model, plan_class, attribution, start, end, i=0, c=0, o=0, session=None):
    return {"session_id": session or model, "model": model, "plan_class": plan_class,
            "attribution": attribution, "window_start": start, "window_end": end,
            "tokens": {"input": i, "cached": c, "output": o}}


def fake_db(rows):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE increments(" + ", ".join(INC_COLS) + ")")
    for n, r in enumerate(rows, 1):
        con.execute("INSERT INTO increments VALUES(" + ",".join("?" * len(INC_COLS)) + ")",
                    (n, "x", r["window_end"], r["window_start"], r["window_end"], "s1",
                     r["model"], "zai", "https://api.z.ai", "", "chat", r["plan_class"],
                     r["tokens"]["input"], r["tokens"]["cached"], r["tokens"]["output"],
                     r["attribution"], ""))
    con.commit()
    return con


# --- PeakModel ---------------------------------------------------------------
class PeakModelBranches(unittest.TestCase):
    def test_day_integral_weekday(self):
        m = P.DayIntegralPeakModel()
        # понедельник 19.10.2026
        self.assertAlmostEqual(m.share(date(2026, 10, 19)), 4.0 / 24.0)

    def test_day_integral_weekend_is_zero(self):
        m = P.DayIntegralPeakModel()
        self.assertEqual(m.share(date(2026, 10, 17)), 0.0)   # суббота
        self.assertEqual(m.share(date(2026, 10, 18)), 0.0)   # воскресенье

    def test_day_integral_ignores_window(self):
        m = P.DayIntegralPeakModel()
        self.assertAlmostEqual(m.share(date(2026, 10, 19), ts(2026, 10, 19, 3, 0), ts(2026, 10, 19, 4, 0)),
                               4.0 / 24.0, places=6)

    def test_window_model_full_peak(self):
        m = P.WindowPeakModel()
        self.assertAlmostEqual(m.share(date(2026, 10, 19), ts(2026, 10, 19, 15, 0), ts(2026, 10, 19, 16, 0)), 1.0)

    def test_window_model_partial_peak(self):
        m = P.WindowPeakModel()
        self.assertAlmostEqual(m.share(date(2026, 10, 19), ts(2026, 10, 19, 13, 0), ts(2026, 10, 19, 15, 0)), 0.5)

    def test_window_model_weekend(self):
        m = P.WindowPeakModel()
        self.assertEqual(m.share(date(2026, 10, 18), ts(2026, 10, 18, 15, 0), ts(2026, 10, 18, 16, 0)), 0.0)

    def test_window_model_point_window_uses_moment(self):
        m = P.WindowPeakModel()
        self.assertEqual(m.share(date(2026, 10, 19), ts(2026, 10, 19, 15, 0), ts(2026, 10, 19, 15, 0)), 1.0)
        self.assertEqual(m.share(date(2026, 10, 18), ts(2026, 10, 18, 15, 0), ts(2026, 10, 18, 15, 0)), 0.0)

    def test_window_model_falls_back_to_day_integral_without_window(self):
        m = P.WindowPeakModel()
        self.assertAlmostEqual(m.share(date(2026, 10, 19)), 4.0 / 24.0)
        self.assertEqual(m.share(date(2026, 10, 17)), 0.0)

    def test_window_model_guard_too_long(self):
        m = P.WindowPeakModel()
        start = ts(2026, 1, 1, 0, 0)
        end = start + 4001 * 86400.0
        self.assertIsNone(m.share(date(2026, 1, 1), start, end))

    def test_interface_is_abstract(self):
        with self.assertRaises(TypeError):
            P.PeakModel()
        with self.assertRaises(TypeError):
            P.CreditCalculator()
        with self.assertRaises(TypeError):
            P.IncrementSource()


# --- CreditCalculator --------------------------------------------------------
class CreditCalculatorBranches(unittest.TestCase):
    def setUp(self):
        self.calc = P.StandardCreditCalculator()
        self.mult = P.MULTIPLIERS["glm-5.3-flash"]

    def test_peak_formula(self):
        toks = {"input": 5000, "cached": 95000, "output": 2000}
        self.assertAlmostEqual(self.calc.credits(toks, self.mult, 1.0), 8.07, places=6)

    def test_offpeak_is_half(self):
        toks = {"input": 5000, "cached": 95000, "output": 2000}
        self.assertAlmostEqual(self.calc.credits(toks, self.mult, 0.0), 4.035, places=6)

    def test_blended_day_integral_share(self):
        # доля пика 1/6 -> коэффициент 1/6 + 5/6 * 0.5 = 7/12
        toks = {"input": 10000, "cached": 0, "output": 0}
        self.assertAlmostEqual(self.calc.credits(toks, self.mult, 1.0 / 6.0), 2.3 * 7.0 / 12.0, places=6)

    def test_none_share_returns_none(self):
        toks = {"input": 10000, "cached": 0, "output": 0}
        self.assertIsNone(self.calc.credits(toks, self.mult, None))

    def test_glm53_multipliers(self):
        toks = {"input": 10000, "cached": 0, "output": 0}
        self.assertAlmostEqual(self.calc.credits(toks, P.MULTIPLIERS["glm-5.3"], 1.0), 6.9, places=6)


# --- MultiplierRegistry ------------------------------------------------------
class MultiplierRegistryBranches(unittest.TestCase):
    def test_known_model(self):
        r = P.MultiplierRegistry()
        self.assertTrue(r.known("glm-5.3-flash"))
        self.assertEqual(r.for_model("glm-5.3")["input"], 6.9)

    def test_unknown_model_uses_fallback_and_is_named(self):
        r = P.MultiplierRegistry()
        self.assertFalse(r.known("glm-4.7"))
        self.assertEqual(r.for_model("glm-4.7"), r.for_model(P.EQUIV_MODEL))
        self.assertEqual(r.fallback_model(), P.EQUIV_MODEL)

    def test_case_insensitive(self):
        r = P.MultiplierRegistry()
        self.assertEqual(r.for_model("GLM-5.3-Flash"), r.for_model("glm-5.3-flash"))

    def test_empty_model_name_uses_fallback(self):
        r = P.MultiplierRegistry()
        self.assertEqual(r.for_model(""), r.for_model(P.EQUIV_MODEL))


# --- PlanRegistry ------------------------------------------------------------
class PlanRegistryBranches(unittest.TestCase):
    def test_order_and_limits(self):
        r = P.PlanRegistry()
        self.assertEqual(r.order(), ["light", "pro", "max"])
        self.assertEqual((r.weekly("light"), r.weekly("pro"), r.weekly("max")), (10000, 60000, 140000))
        self.assertEqual((r.five_hour("light"), r.five_hour("pro"), r.five_hour("max")), (2000, 12000, 28000))

    def test_monthly_cap_is_derived_from_weekly(self):
        r = P.PlanRegistry()
        self.assertAlmostEqual(r.monthly_cap("light", 31), 10000 * 31 / 7.0, places=6)
        self.assertAlmostEqual(r.monthly_cap("pro", 30), 60000 * 30 / 7.0, places=6)
        self.assertAlmostEqual(r.monthly_cap("max", 28), 140000 * 28 / 7.0, places=6)


# --- MonthWindow --------------------------------------------------------------
class MonthWindowBranches(unittest.TestCase):
    def test_normal_window(self):
        w = P.MonthWindow(date(2026, 10, 19))
        self.assertEqual(w.days(), 31)
        self.assertEqual(w.start.date(), date(2026, 10, 19))
        self.assertEqual(w.end.date(), date(2026, 11, 19))
        self.assertFalse(w.clamped)

    def test_window_starts_at_midnight(self):
        w = P.MonthWindow(date(2026, 10, 19))
        self.assertEqual((w.start.hour, w.start.minute), (0, 0))

    def test_clamped_when_anchor_day_missing(self):
        w = P.MonthWindow(date(2026, 1, 31))
        self.assertTrue(w.clamped)
        self.assertEqual(w.end.date(), date(2026, 2, 28))
        self.assertEqual(w.days(), 28)

    def test_contains_boundaries(self):
        w = P.MonthWindow(date(2026, 10, 19))
        self.assertTrue(w.contains(w.start.timestamp()))          # начало включительно
        self.assertFalse(w.contains(w.end.timestamp()))           # конец исключительно
        self.assertFalse(w.contains(w.start.timestamp() - 1))

    def test_day_of_uses_peak_timezone(self):
        w = P.MonthWindow(date(2026, 10, 19))
        # 23:00 UTC того же дня = 07:00 следующего дня SGT
        self.assertEqual(w.day_of(ts(2026, 10, 19, 23, 0, tz=timezone.utc)), date(2026, 10, 20))

    def test_accepts_datetime(self):
        w = P.MonthWindow(datetime(2026, 10, 19, 13, 0, tzinfo=PEAK_TZ))
        self.assertEqual(w.anchor, date(2026, 10, 19))
        self.assertEqual((w.start.hour, w.start.minute), (0, 0))


# --- IncrementSource ---------------------------------------------------------
class IncrementSourceBranches(unittest.TestCase):
    def test_inmemory_source_filters_window(self):
        w = P.MonthWindow(date(2026, 10, 19))
        rows = [row("glm-5.3-flash", "zai_plan_by_rule", "delta", ts(2026, 10, 20, 10), ts(2026, 10, 20, 11)),
                row("glm-5.3-flash", "zai_plan_by_rule", "delta", ts(2026, 10, 18, 10), ts(2026, 10, 18, 11))]
        got = P.InMemoryIncrementSource(rows).rows(w)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["window_end"], ts(2026, 10, 20, 11))

    def test_sqlite_source_excludes_anomaly_and_local(self):
        w = P.MonthWindow(date(2026, 10, 19))
        rows = [row("glm-5.3-flash", "zai_plan_by_rule", "delta", ts(2026, 10, 20, 10), ts(2026, 10, 20, 11), 100),
                row("glm-5.3-flash", "zai_plan_by_rule", "anomaly", ts(2026, 10, 20, 11), ts(2026, 10, 20, 12), -50),
                row("qwen", "local", "delta", ts(2026, 10, 20, 10), ts(2026, 10, 20, 11), 5000),
                row("glm-5.3-flash", "zai_plan_by_rule", "delta", ts(2026, 10, 18, 10), ts(2026, 10, 18, 11), 700)]
        con = fake_db(rows)
        src = P.SqliteIncrementSource(lambda: con)
        got = src.rows(w)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["tokens"]["input"], 100)

    def test_sqlite_source_accepts_zai_paid(self):
        w = P.MonthWindow(date(2026, 10, 19))
        con = fake_db([row("glm-5.3-flash", "zai_paid", "delta", ts(2026, 10, 20, 10), ts(2026, 10, 20, 11), 10)])
        self.assertEqual(len(P.SqliteIncrementSource(lambda: con).rows(w)), 1)

    def test_empty_window_returns_empty(self):
        w = P.MonthWindow(date(2026, 10, 19))
        con = fake_db([row("glm-5.3-flash", "zai_plan_by_rule", "delta", ts(2026, 12, 25, 10), ts(2026, 12, 25, 11))])
        self.assertEqual(P.SqliteIncrementSource(lambda: con).rows(w), [])


# --- PlanCascade --------------------------------------------------------------
class PlanCascadeBranches(unittest.TestCase):
    def setUp(self):
        self.w = P.MonthWindow(date(2026, 10, 1))
        self.c = P.PlanCascade(P.PlanRegistry(), self.w)

    def test_under_light_cap(self):
        steps = self.c.evaluate(1000.0)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].plan, "light")
        self.assertAlmostEqual(steps[0].pct, 1000 / (10000 * 31 / 7.0) * 100.0, places=6)
        self.assertEqual(self.c.current_plan(1000.0), "light")

    def test_zero_credits(self):
        steps = self.c.evaluate(0.0)
        self.assertEqual(len(steps), 1)
        self.assertAlmostEqual(steps[0].pct, 0.0)
        self.assertEqual(self.c.current_plan(0.0), "light")

    def test_exactly_at_light_cap(self):
        cap = 10000 * 31 / 7.0
        steps = self.c.evaluate(cap)
        self.assertEqual(len(steps), 1)
        self.assertAlmostEqual(steps[0].pct, 100.0)
        self.assertEqual(self.c.current_plan(cap), "light")

    def test_overflow_light_to_pro(self):
        steps = self.c.evaluate(45000.0)
        self.assertEqual([s.plan for s in steps], ["light", "pro"])
        self.assertAlmostEqual(steps[0].pct, 100.0)
        self.assertAlmostEqual(steps[1].pct, (45000 - 10000 * 31 / 7.0) / (60000 * 31 / 7.0) * 100.0, places=6)
        self.assertEqual(self.c.current_plan(45000.0), "pro")

    def test_overflow_pro_to_max(self):
        steps = self.c.evaluate(400000.0)
        self.assertEqual([s.plan for s in steps], ["light", "pro", "max"])
        self.assertAlmostEqual(steps[0].pct, 100.0)
        self.assertAlmostEqual(steps[1].pct, 100.0)
        rem = 400000 - 10000 * 31 / 7.0 - 60000 * 31 / 7.0
        self.assertAlmostEqual(steps[2].pct, rem / (140000 * 31 / 7.0) * 100.0, places=6)
        self.assertEqual(self.c.current_plan(400000.0), "max")

    def test_overflow_max_means_no_plan_left(self):
        steps = self.c.evaluate(1000000.0)
        self.assertEqual(len(steps), 3)
        self.assertTrue(all(s.pct >= 100.0 for s in steps))
        self.assertIsNone(self.c.current_plan(1000000.0))


# --- OverflowLedger -----------------------------------------------------------
class OverflowLedgerBranches(unittest.TestCase):
    def setUp(self):
        self.peak = P.DayIntegralPeakModel()
        self.calc = P.StandardCreditCalculator()
        self.mult = P.MultiplierRegistry()
        self.ledger = P.OverflowLedger(self.calc, self.mult, self.peak)

    def test_no_overflow(self):
        rows = [row("glm-5.3-flash", "zai_plan_by_rule", "delta", ts(2026, 10, 19, 10), ts(2026, 10, 19, 11), 10000)]
        within, overflow, details = self.ledger.split(rows, [(None, 100.0)])
        self.assertAlmostEqual(within[0]["input"], 10000)
        self.assertAlmostEqual(overflow["input"], 0.0)
        # понедельник: доля пика 1/6 -> коэффициент 7/12
        self.assertAlmostEqual(sum(d[2] for d in details), 2.3 * 7.0 / 12.0, places=6)

    def test_partial_row_split(self):
        rows = [row("glm-5.3-flash", "zai_plan_by_rule", "delta", ts(2026, 10, 19, 10), ts(2026, 10, 19, 11), 10000),
                row("glm-5.3-flash", "zai_plan_by_rule", "delta", ts(2026, 10, 19, 11), ts(2026, 10, 19, 12), 10000)]
        within, overflow, details = self.ledger.split(rows, [(None, 1.0)])
        # ожидание считается дробями, не вручную: c = 2.3 x 7/12 = 161/120
        c = Fraction(23, 10) * Fraction(7, 12)
        frac_first = Fraction(1, 1) / c            # доля первой строки, которая ещё влезла
        self.assertAlmostEqual(within[0]["input"], float(10000 * frac_first), places=6)
        # вторая строка: лимит уже исчерпан, room = 0 -> вся вторая строка в переполнении
        self.assertAlmostEqual(overflow["input"], float(10000 * (1 - frac_first)) + 10000, places=6)

    def test_room_never_negative_when_cap_already_exceeded(self):
        # если лимит уже исчерпан, «внутрь» не должны попадать отрицательные токены
        rows = [row("glm-5.3-flash", "zai_plan_by_rule", "delta", ts(2026, 10, 19, 10), ts(2026, 10, 19, 11), 10000)]
        within, overflow, _ = self.ledger.split(rows, [(None, 0.5)])
        self.assertGreaterEqual(within[0]["input"], 0.0)
        self.assertAlmostEqual(within[0]["input"] + overflow["input"], 10000, places=6)

    def test_row_spanning_two_caps(self):
        # одна строка может частично попасть в Light и частично в Pro
        rows = [row("glm-5.3-flash", "zai_plan_by_rule", "delta", ts(2026, 10, 19, 10), ts(2026, 10, 19, 11), 10000)]
        within, overflow, _ = self.ledger.split(rows, [("light", 1.0), ("pro", 1.3)])
        c = Fraction(23, 10) * Fraction(7, 12)
        first = float(10000 * (Fraction(1, 1) / c))          # доля строки, которая влезла в Light
        second = float(10000 * ((c - Fraction(1, 1)) / c))  # остаток той же строки — в Pro
        self.assertAlmostEqual(within[0]["input"], first, places=6)
        self.assertAlmostEqual(within[1]["input"], second, places=6)
        self.assertAlmostEqual(overflow["input"], 0.0, places=6)

    def test_chronological_order_matters(self):
        # строка, пришедшая позже по времени, не должна «съедать» лимит у более ранней
        rows = [row("glm-5.3-flash", "zai_plan_by_rule", "delta", ts(2026, 10, 19, 11), ts(2026, 10, 19, 12), 10000),
                row("glm-5.3-flash", "zai_plan_by_rule", "delta", ts(2026, 10, 19, 10), ts(2026, 10, 19, 11), 10000)]
        within, overflow, _ = self.ledger.split(rows, [(None, 1.0)])
        c = Fraction(23, 10) * Fraction(7, 12)
        first_overflow = 10000 * (1 - Fraction(1, 1) / c)
        self.assertAlmostEqual(overflow["input"], float(first_overflow) + 10000, places=6)

    def test_zero_credit_row_does_not_divide_by_zero(self):
        rows = [row("glm-5.3-flash", "zai_plan_by_rule", "delta", ts(2026, 10, 19, 10), ts(2026, 10, 19, 11), 0),
                row("glm-5.3-flash", "zai_plan_by_rule", "delta", ts(2026, 10, 19, 11), ts(2026, 10, 19, 12), 10000)]
        within, overflow, details = self.ledger.split(rows, [(None, 1.0)])
        c = Fraction(23, 10) * Fraction(7, 12)
        self.assertAlmostEqual(overflow["input"], float(10000 * (1 - Fraction(1, 1) / c)), places=6)

    def test_weekend_row_uses_offpeak(self):
        rows = [row("glm-5.3-flash", "zai_plan_by_rule", "delta", ts(2026, 10, 17, 10), ts(2026, 10, 17, 11), 10000)]
        within, _, details = self.ledger.split(rows, [(None, 100.0)])
        self.assertAlmostEqual(sum(d[2] for d in details), 2.3 * 0.5, places=6)


# --- группировка переполнения ------------------------------------------------
class GroupOverflowBranches(unittest.TestCase):
    def test_group_by_day_and_session(self):
        r1 = row("glm-5.3-flash", "zai_plan_by_rule", "delta", ts(2026, 10, 19, 10), ts(2026, 10, 19, 11), 10000)
        r2 = row("glm-5.3-flash", "zai_plan_by_rule", "delta", ts(2026, 10, 19, 11), ts(2026, 10, 19, 12), 10000)
        details = [(r1, 1.0, 100.0), (r2, 0.5, 50.0)]
        day = datetime(2026, 10, 19).date()
        by_day = P.group_overflow(details, lambda r: datetime.fromtimestamp(r["window_end"], P.PEAK_TZ).date())
        self.assertAlmostEqual(by_day[day]["input"], 15000)
        by_sess = P.group_overflow(details, lambda r: r["session_id"])
        self.assertAlmostEqual(by_sess["glm-5.3-flash"]["input"], 15000)

    def test_rows_without_overflow_are_skipped(self):
        r1 = row("glm-5.3-flash", "zai_plan_by_rule", "delta", ts(2026, 10, 19, 10), ts(2026, 10, 19, 11), 10000)
        r2 = row("glm-5.3-flash", "zai_plan_by_rule", "backfill", ts(2026, 10, 19, 11), ts(2026, 10, 19, 12), 10000)
        details = [(r1, 0.0, 10.0), (r2, None, None)]
        self.assertEqual(P.group_overflow(details, lambda r: r["session_id"]), {})


# --- источник строк: оговорки окна ------------------------------------------
class SourceBranches(unittest.TestCase):
    def setUp(self):
        self.window = P.MonthWindow(date(2026, 10, 19))

    def test_gaps_are_counted_and_named(self):
        src = P.InMemoryIncrementSource([row("glm-5.3-flash", "zai_plan_by_rule", "gap",
                                             ts(2026, 10, 20, 10), ts(2026, 10, 20, 15), 50000)])
        n, tok = src.gaps(self.window)
        self.assertEqual((n, tok), (1, 50000), "выбрасываемые интервалы не называются")
        rep = P.PlanReport(self.window, P.PlanCascade(P.PlanRegistry(), self.window),
                           P.StandardCreditCalculator(), P.MultiplierRegistry(), P.PlanRegistry(),
                           P.DayIntegralPeakModel(), state={}, source=src)
        out = rep.build()
        self.assertTrue(any("Выбрасываемые интервалы" in l for l in out), out)
        self.assertTrue(any("TOKEN_COST_MAX_WINDOW" in l for l in out), out)

    def test_no_gap_line_when_there_are_no_gaps(self):
        src = P.InMemoryIncrementSource([row("glm-5.3-flash", "zai_plan_by_rule", "delta",
                                             ts(2026, 10, 20, 10), ts(2026, 10, 20, 11), 10000)])
        rep = P.PlanReport(self.window, P.PlanCascade(P.PlanRegistry(), self.window),
                           P.StandardCreditCalculator(), P.MultiplierRegistry(), P.PlanRegistry(),
                           P.DayIntegralPeakModel(), state={}, source=src)
        out = rep.build()
        self.assertFalse(any("Выбрасываемые интервалы" in l for l in out), out)

    def test_before_window_counts_rows_outside(self):
        before = row("glm-5.3-flash", "zai_plan_by_rule", "delta",
                     ts(2026, 10, 13, 10), ts(2026, 10, 13, 11), 10000)
        inside = row("glm-5.3-flash", "zai_plan_by_rule", "delta",
                     ts(2026, 10, 20, 10), ts(2026, 10, 20, 11), 5000)
        src = P.InMemoryIncrementSource([before, inside])
        n, tok = src.before_window(self.window)
        self.assertEqual(n, 1, "строки до начала окна должны называться")
        self.assertEqual(tok, 10000)

    def test_anomalies_and_unknown_counts(self):
        anom = row("glm-5.3-flash", "zai_plan_by_rule", "anomaly",
                   ts(2026, 10, 20, 10), ts(2026, 10, 20, 11), 0)
        unknown = row("m", "unknown", "delta", ts(2026, 10, 20, 12), ts(2026, 10, 20, 13), 1000)
        src = P.InMemoryIncrementSource([anom, unknown])
        self.assertEqual(src.anomalies(self.window), 1)
        self.assertEqual(src.unknown_count(self.window), 1)

    def test_paid_rows_excluded_from_plan_credits(self):
        """Баг M1: оплаченные z.ai (реально списанные деньги) не план — они не должны
        входить в кредиты плана, но обязаны называться отдельной строкой блока."""
        w = P.MonthWindow(date(2026, 10, 19))
        rows = [row("glm-5.3-flash", "zai_plan_by_rule", "delta",
                    ts(2026, 10, 20, 10), ts(2026, 10, 20, 11), 1000000),
                row("glm-5.3-flash", "zai_paid", "delta",
                    ts(2026, 10, 20, 10), ts(2026, 10, 20, 11), 2000000)]
        src = P.InMemoryIncrementSource(rows)
        rep = P.PlanReport(w, P.PlanCascade(P.PlanRegistry(), w), P.StandardCreditCalculator(),
                           P.MultiplierRegistry(), P.PlanRegistry(), P.DayIntegralPeakModel(),
                           state={}, source=src)
        got = rep.rows()
        self.assertEqual(len(got), 2, "источник обязан отдавать и оплаченные строки")
        self.assertAlmostEqual(rep.credits(got), 134.17, places=2,
                               msg="оплаченные строки вошли в кредиты плана")
        out = rep.build(got, rep.credits(got))
        self.assertTrue(any("Оплаченные z.ai в окне" in l for l in out),
                        f"оплаченные не названы отдельной строкой: {out}")
        self.assertFalse(any("2.00M" in l and "Кредиты за окно" in l for l in out),
                         f"итог токенов окна включает оплаченные: {out}")

    def test_degraded_when_source_missing(self):
        report = P.PlanReport(self.window, P.PlanCascade(P.PlanRegistry(), self.window),
                              P.StandardCreditCalculator(), P.MultiplierRegistry(),
                              P.PlanRegistry(), P.DayIntegralPeakModel(), state={}, source=None)
        lines = report.build()
        self.assertTrue(any("не посчитан" in l for l in lines),
                        f"пустой источник должен называться как деградация: {lines}")
        self.assertFalse(any("Строк плана в окне нет" in l for l in lines),
                         f"деградация подменяется пустым окном: {lines}")


# --- состояние ---------------------------------------------------------------
class PlanStateStoreBranches(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="plan-state-")
        self.store = P.PlanStateStore(os.path.join(self.home, "state", "token_cost_plan.json"))

    def test_load_missing_returns_empty(self):
        self.assertEqual(self.store.load(), {})

    def test_save_creates_dir_and_roundtrips(self):
        self.store.save({"anchor_date": "2026-10-19", "plan": "light"})
        self.assertTrue(os.path.exists(self.store.path()))
        st = self.store.load()
        self.assertEqual(st["anchor_date"], "2026-10-19")
        self.assertIn("saved_at", st)

    def test_broken_json_degrades_to_empty(self):
        os.makedirs(os.path.dirname(self.store.path()), exist_ok=True)
        with open(self.store.path(), "w") as f:
            f.write("{ not json")
        self.assertEqual(self.store.load(), {})


class ParseAnchorBranches(unittest.TestCase):
    def test_int_day(self):
        d = P.parse_anchor("15")
        self.assertEqual(d.day, 15)

    def test_full_date(self):
        self.assertEqual(P.parse_anchor("2026-10-19"), date(2026, 10, 19))

    def test_invalid_day_raises(self):
        with self.assertRaises(ValueError):
            P.parse_anchor("32")
        with self.assertRaises(ValueError):
            P.parse_anchor("0")

    def test_day_clamped_to_current_month_length(self):
        d = P.parse_anchor("31")
        self.assertLessEqual(d.day, 31)

    def test_date_object_passes_through(self):
        self.assertEqual(P.parse_anchor(date(2026, 10, 19)), date(2026, 10, 19))

    def test_numeric_anchor_uses_the_reference_month(self):
        """Баг K1: без опорной даты числовой якорь берётся из фактического сегодня,
        и --month даёт окно другого месяца."""
        d, src = P.resolve_anchor({"anchor_day": "31"}, date(2026, 2, 10), None)
        self.assertEqual(d, date(2026, 2, 28), f"якорь взят из фактического сегодня: {d}")
        self.assertEqual(src, "saved (clamped)", f"корректировка дня не названа в источнике: {src}")

    def test_full_date_with_day_missing_in_that_month_is_clamped(self):
        self.assertEqual(P.parse_anchor("2026-02-31", date(2026, 2, 10)), date(2026, 2, 28))


# --- переход плана -------------------------------------------------------------
class TransitionNotifierBranches(unittest.TestCase):
    def setUp(self):
        self.n = P.TransitionNotifier(P.PlanRegistry())

    def test_no_transition(self):
        self.assertIsNone(self.n.message("light", "light"))

    def test_current_none_always_means_max_overflow(self):
        # current_plan возвращает None только когда кредиты за пределами Max —
        # значит это всегда переполнение, даже если плана в состоянии ещё нет
        self.assertEqual(self.n.message("max", None), "План Max переполнен, переходим на пересчет токенов.")
        self.assertEqual(self.n.message(None, None), "План Max переполнен, переходим на пересчет токенов.")

    def test_light_to_pro(self):
        self.assertEqual(self.n.message("light", "pro"), "Превышен план Light, перешли на Pro")

    def test_pro_to_max(self):
        self.assertEqual(self.n.message("pro", "max"), "Превышен план Pro, перешли на Max")

    def test_max_overflow_message(self):
        self.assertEqual(self.n.message("max", None), "План Max переполнен, переходим на пересчет токенов.")

    def test_first_plan_no_message(self):
        self.assertIsNone(self.n.message(None, "light"))


# --- доставляемый блок ---------------------------------------------------------
class PlanReportBranches(unittest.TestCase):
    def setUp(self):
        self.w = P.MonthWindow(date(2026, 10, 19))
        self.cascade = P.PlanCascade(P.PlanRegistry(), self.w)
        self.report = P.PlanReport(self.w, self.cascade, P.StandardCreditCalculator(),
                                   P.MultiplierRegistry(), P.PlanRegistry(),
                                   P.DayIntegralPeakModel(),
                                   source=P.InMemoryIncrementSource([]))

    def test_first_line_format(self):
        # 1000 / (10000 x 31/7) = 2.258% -> "%.1f" = 2.3
        self.assertEqual(self.report.first_line(1000.0), "Равно 2.3% плана Light на Z.ai")

    def test_first_line_after_overflow_is_next_plan(self):
        self.assertEqual(self.report.first_line(45000.0),
                         "Равно %.1f%% плана Pro на Z.ai" % ((45000 - 10000 * 31 / 7.0) / (60000 * 31 / 7.0) * 100.0))

    def test_first_line_when_max_overflowed(self):
        self.assertEqual(self.report.first_line(1000000.0), "Равно 100.0% плана Max на Z.ai")

    def test_block_without_rows_says_so(self):
        out = "\n".join(self.report.build())
        self.assertIn("Строк плана в окне нет", out)

    def test_block_with_rows(self):
        rows = [row("glm-5.3-flash", "zai_plan_by_rule", "delta", ts(2026, 10, 19, 10), ts(2026, 10, 19, 11), 10000)]
        out = "\n".join(self.report.build(rows))
        self.assertIn("Равно", out.splitlines()[0])
        self.assertIn("использовано", out)
        self.assertIn("Light плана", out)
        self.assertIn("только 5-часовой и недельный", out)
        self.assertIn("интегральная оценка по дню", out)
        self.assertIn("MCP-вызовы", out)

    def test_point_rows_flagged_as_estimate(self):
        rows = [row("glm-5.3-flash", "zai_plan_by_rule", "backfill", ts(2026, 10, 19, 10), ts(2026, 10, 19, 10), 10000)]
        out = "\n".join(self.report.build(rows))
        self.assertIn("точечной атрибуцией", out)
        self.assertIn("оценка", out)

    def test_unknown_model_named(self):
        rows = [row("glm-4.7", "zai_plan_by_rule", "delta", ts(2026, 10, 19, 10), ts(2026, 10, 19, 11), 10000)]
        out = "\n".join(self.report.build(rows))
        self.assertIn("без опубликованных мультипликаторов", out)
        self.assertIn("glm-4.7", out)

    def test_clamped_anchor_named(self):
        w = P.MonthWindow(date(2026, 1, 31))
        r = P.PlanReport(w, P.PlanCascade(P.PlanRegistry(), w), P.StandardCreditCalculator(),
                         P.MultiplierRegistry(), P.PlanRegistry(), P.DayIntegralPeakModel(),
                         source=P.InMemoryIncrementSource([]))
        out = "\n".join(r.build())
        self.assertIn("ограничение метода", out)

    def test_window_line_shows_midnight(self):
        out = "\n".join(self.report.build())
        self.assertIn("2026-10-19 00:00 -> 2026-11-19 00:00", out)
        self.assertIn("день начала отсчёта — 19", out)


class ResolveAnchorBranches(unittest.TestCase):
    def test_saved_anchor_wins_over_default(self):
        d, src = P.resolve_anchor({"anchor_date": "2026-10-15"}, date(2026, 10, 20), default_day=19)
        self.assertEqual(d, date(2026, 10, 15))
        self.assertEqual(src, "saved")

    def test_default_derived_when_nothing_saved(self):
        d, src = P.resolve_anchor({}, date(2026, 10, 20), default_day=19)
        self.assertEqual(d, date(2026, 10, 19))
        self.assertEqual(src, "default_derived")

    def test_plain_default_when_no_default_day(self):
        d, src = P.resolve_anchor({}, date(2026, 10, 20), None)
        self.assertEqual(d, date(2026, 10, 1))
        self.assertEqual(src, "default")

    def test_default_day_clamped_to_month_length(self):
        d, src = P.resolve_anchor({}, date(2026, 2, 10), default_day=31)
        self.assertEqual(d, date(2026, 2, 28))
        self.assertEqual(src, "default_derived")


class PeakDescribeBranches(unittest.TestCase):
    def test_day_model_names_integral_estimate(self):
        self.assertIn("интегральная оценка по дню", P.DayIntegralPeakModel().describe())

    def test_window_model_names_the_approximation(self):
        d = P.WindowPeakModel().describe()
        self.assertIn("приближение", d)
        self.assertIn("по моменту каждого запроса", d)


class PlanReportExtraBranches(unittest.TestCase):
    def setUp(self):
        self.w = P.MonthWindow(date(2026, 10, 19))
        self.cascade = P.PlanCascade(P.PlanRegistry(), self.w)

    def _report(self, **kw):
        return P.PlanReport(self.w, self.cascade, P.StandardCreditCalculator(),
                            P.MultiplierRegistry(), P.PlanRegistry(), P.DayIntegralPeakModel(),
                            source=P.InMemoryIncrementSource([]), **kw)

    def test_anchor_source_is_named(self):
        out = "\n".join(self._report(anchor_source="default_derived").build())
        self.assertIn("(default_derived)", out)

    def test_transition_message_when_plan_changed(self):
        # 400M входных токенов = 53666.7 кредитов — больше месячного лимита Light (44285.7)
        out = "\n".join(self._report(state={"plan": "light"},
                                    notifier=P.TransitionNotifier(P.PlanRegistry())).build(
            [row("glm-5.3-flash", "zai_plan_by_rule", "delta",
                 ts(2026, 10, 19, 10), ts(2026, 10, 19, 11), 400000000)]))
        self.assertIn("Превышен план Light, перешли на Pro", out)

    def test_no_transition_message_when_plan_same(self):
        r = self._report(state={"plan": "light"}, notifier=P.TransitionNotifier(P.PlanRegistry()))
        out = "\n".join(r.build())
        self.assertNotIn("Превышен", out)

    def test_by_actual_uses_row_model_multipliers(self):
        rows = [row("glm-5.3", "zai_plan_by_rule", "delta", ts(2026, 10, 19, 10), ts(2026, 10, 19, 11), 10000)]
        plain = self._report().credits(rows)
        actual = self._report(by_actual=True).credits(rows)
        self.assertAlmostEqual(plain, 2.3 * 7.0 / 12.0, places=6)
        self.assertAlmostEqual(actual, 6.9 * 7.0 / 12.0, places=6)

    def test_custom_mcp_note(self):
        out = "\n".join(self._report(mcp_note="MCP-вызовы: тестовая оговорка").build())
        self.assertIn("тестовая оговорка", out)


if __name__ == "__main__":
    unittest.main()
