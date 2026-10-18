#!/usr/bin/env python3
"""Запуск отчёта token-cost-report по расписанию: единственный путь, который
обновляет курс и пишет снимок 21:00 (--commit). Каждый запуск так же пишется
в append-базу token_cost.db (runs / usage_snapshot / increments).
"""
import os, runpy, sys

os.environ.setdefault("TOKEN_COST_CRON_JOB", "token-cost-daily")
sys.argv = ["token_cost.py", "--commit"]
runpy.run_path("/Users/alex/.hermes/scripts/token_cost.py", run_name="__main__")
