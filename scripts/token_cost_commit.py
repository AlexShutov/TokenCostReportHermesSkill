#!/usr/bin/env python3
"""Запуск отчёта token-cost-report по расписанию: единственный путь, который
обновляет базу суточной дельты (--commit). Прямая команда скрипт не обновляет.
"""
import runpy, sys

sys.argv = ["token_cost.py", "--commit"]
runpy.run_path("/Users/alex/.hermes/scripts/token_cost.py", run_name="__main__")
