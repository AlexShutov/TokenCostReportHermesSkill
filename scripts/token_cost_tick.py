#!/usr/bin/env python3
"""Тик копилки токенов для крона token-counter (10 мин, --no-agent):
обновляет ledger, пишет запуск в append-базу и молчит (пустой stdout = ничего не доставлять).
"""
import os, runpy, sys

os.environ.setdefault("TOKEN_COST_CRON_JOB", "token-counter")
sys.argv = ["token_cost.py", "--tick"]
runpy.run_path("/Users/alex/.hermes/scripts/token_cost.py", run_name="__main__")
