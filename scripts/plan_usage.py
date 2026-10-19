#!/usr/bin/env python3
"""CLI-обёртка блока плана. Вся логика — в plan_core.py (SOLID), здесь только аргументы.

Логика кредитов — дословно из docs.z.ai/devpack/overview (проверено 2026-10-20):
  кредиты = (input x Input_mult + cached x Cached_mult + output x Output_mult) / 10000
  GLM-5.3 6.9/1.7/24, GLM-5.3-Flash 2.3/0.56/8; вне пика — 50% стандартной ставки;
  пик — пн-пт 14:00-18:00 Singapore (UTC+8); выходные — внепик весь день.
Месячного лимита на сайте нет — он выводится из недельного (plan_core.PlanRegistry).
Каскад Light -> Pro -> Max — plan_core.PlanCascade.

Режимы:
  (без флагов)   печать блока плана; файл состояния создаётся при первом запуске
  --set-anchor   сохранить день начала отсчёта месяца (число 1-31 или дата YYYY-MM-DD)
  --plan         текущий план (light|pro|max)
  --model        эквивалентная модель (по умолчанию GLM-5.3-Flash)
  --by-actual-model  считать мультипликаторами фактической модели каждой строки
  --month        месяц отсчёта (YYYY-MM), по умолчанию текущий
  --peak day|window  путь пика: интегральная оценка по дню (дефолт) или по временному окну
  --dry-run      ничего не писать

Только python3 и stdlib.
"""
import datetime as dt
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plan_core as P
import token_cost_db as T

# Дефолт дня начала отсчёта месяца при установке навыка с нуля — в plan_core
# (значение 19-е число, чтобы данные, уже имеющиеся в базе, попали в расчёт).
DEFAULT_ANCHOR_DAY = P.DEFAULT_ANCHOR_DAY

PEAK_TZ = P.PEAK_TZ
PLAN_FILE = os.path.join(T.STATE, "token_cost_plan.json")


def ro_connect():
    """Читательское подключение: на «свежем Hermes» без базы блок деградирует честно,
    а не создаёт пустую базу."""
    return sqlite3.connect("file:%s?mode=ro" % T.DB, uri=True, timeout=10)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    dry = "--dry-run" in argv
    store = P.PlanStateStore(PLAN_FILE)
    state = store.load()
    ref = dt.datetime.now(PEAK_TZ)
    if "--month" in argv:
        ref = dt.datetime.strptime(argv[argv.index("--month") + 1], "%Y-%m").replace(tzinfo=PEAK_TZ)

    if "--set-anchor" in argv:
        anchor = P.parse_anchor(argv[argv.index("--set-anchor") + 1])
        state["anchor_date"] = anchor.isoformat()
        state["anchor_source"] = "saved"
        if not dry:
            store.save(state)
        print("день начала отсчёта месяца сохранён: %d (дата якоря %s)" % (anchor.day, anchor.isoformat()))

    if "--plan" in argv:
        plan = argv[argv.index("--plan") + 1].lower()
        if not P.PlanRegistry().known(plan):
            print("план: %s" % " | ".join(P.PlanRegistry().order()), file=sys.stderr)
            return 2
        state["plan"] = plan
        if not dry:
            store.save(state)

    if "--model" in argv:
        model = argv[argv.index("--model") + 1].lower()
        if not P.MultiplierRegistry().known(model):
            print("модель: %s" % ", ".join(P.MultiplierRegistry().known_models()), file=sys.stderr)
            return 2
        state["model"] = model
        if not dry:
            store.save(state)

    by_actual = "--by-actual-model" in argv
    peak_name = argv[argv.index("--peak") + 1] if "--peak" in argv else "day"
    if peak_name not in ("day", "window"):
        print("путь пика: day (интегральная оценка по дню) или window (по временному окну)", file=sys.stderr)
        return 2
    peak_model = P.WindowPeakModel() if peak_name == "window" else P.DayIntegralPeakModel()

    anchor, source = P.resolve_anchor(state, ref.date(), DEFAULT_ANCHOR_DAY)
    window = P.MonthWindow(anchor)
    report = P.PlanReport(window, P.PlanCascade(P.PlanRegistry(), window),
                          P.StandardCreditCalculator(), P.MultiplierRegistry(),
                          P.PlanRegistry(), peak_model,
                          source=P.SqliteIncrementSource(ro_connect),
                          anchor_source=source, notifier=P.TransitionNotifier(P.PlanRegistry()),
                          state=state, store=store, by_actual=by_actual,
                          mcp_note="MCP-вызовы (Web Search / Reader / Zread) не отслеживаются — "
                                   "кредиты по инструментам в блок не включены")

    rows = report.rows()
    credits = report.credits(rows)
    print("\n".join(report.build(rows, credits)))

    # переход плана фиксируется только при недrom-запуске (тесты не пишут)
    if not dry and rows:
        prev = state.get("plan")
        new_plan = report.cascade.current_plan(credits)
        if new_plan != prev:
            state["plan"] = new_plan
            store.save(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
