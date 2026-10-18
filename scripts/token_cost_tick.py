#!/usr/bin/env python3
"""Тик копилки токенов для крона token-counter (10 мин, --no-agent):
обновляет ledger локальных токенов и молчит (пустой stdout = ничего не доставлять).
"""
import runpy, sys

sys.argv = ["token_cost.py", "--tick"]
runpy.run_path("/Users/alex/.hermes/scripts/token_cost.py", run_name="__main__")
