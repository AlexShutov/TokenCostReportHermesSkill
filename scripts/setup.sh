#!/usr/bin/env bash
# Подготовка навыка token-cost-report на новой машине. Зависимостей нет — только
# stdlib Python. Проверяет версию, создаёт каталог state/ и печатает, что нужно
# создать в кроне и какую переменную задать под своего пользователя.
set -e
PY=${PYTHON:-python3}
"$PY" - <<'EOF'
import os, sys
if sys.version_info < (3, 9):
    sys.exit("нужен Python 3.9+ (stock python3 на macOS — 3.9, подходит)")
state = os.path.expanduser("~/.hermes/state")
os.makedirs(state, exist_ok=True)
print("python", "%d.%d" % sys.version_info[:2], "->", state)
EOF

echo "cron-задачи, которые нужно создать (имена важны — по ним скрипт определяет задачу):"
echo "  token-counter    каждые 10 мин, --no-agent, script=token_cost_tick.py, deliver=local"
echo "  token-cost-daily 0 21 * * *,                script=token_cost_commit.py, deliver=origin"
echo "переменная TOKEN_COST_CHAT_ID — твой Telegram chat id (без неё блок «За текущую сессию»"
echo "может относиться к чату автора навыка)."
echo "Скрипты должны лежать в ~/.hermes/scripts/ (крон исполняет только оттуда)."
echo "Блок плана z.ai: день начала отсчёта месяца по умолчанию — 1-е число; свой день"
echo "  сохраняется в ~/.hermes/state/token_cost_plan.json:"
echo "  python3 ~/.hermes/scripts/plan_usage.py --set-anchor <день или YYYY-MM-DD>"
