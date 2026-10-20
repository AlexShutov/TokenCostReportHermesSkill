#!/usr/bin/env bash
# Подготовка навыка token-cost-report на новой машине. Зависимостей нет — только
# stdlib Python. Проверяет версию, создаёт каталог state/ и печатает, что нужно
# создать в кроне и какую переменную задать под своего пользователя.
set -e
PY=${PYTHON:-python3}
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1
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

# M3: существующие скрипты не перезаписываются молча — крон исполняет именно
# копию в ~/.hermes/scripts/, и тихая перезапись меняет поведение живого крона
TARGET_DIR="$HOME/.hermes/scripts"
mkdir -p "$TARGET_DIR"
DIFF=0
for f in token_cost.py token_cost_db.py plan_core.py plan_usage.py token_cost_tick.py token_cost_commit.py single_message.py; do
  [ -f "$SKILL_DIR/scripts/$f" ] || continue
  if [ -f "$TARGET_DIR/$f" ] && ! cmp -s "$SKILL_DIR/scripts/$f" "$TARGET_DIR/$f"; then
    echo "ВНИМАНИЕ: $TARGET_DIR/$f уже существует и отличается от версии навыка — не перезаписываю."
    DIFF=1
  fi
done
if [ "$DIFF" = 1 ] && [ "$FORCE" = 0 ]; then
  echo "  Перезапись — только осознанно: bash $SKILL_DIR/scripts/setup.sh --force"
else
  for f in token_cost.py token_cost_db.py plan_core.py plan_usage.py token_cost_tick.py token_cost_commit.py single_message.py; do
    if [ -f "$SKILL_DIR/scripts/$f" ]; then
      cp "$SKILL_DIR/scripts/$f" "$TARGET_DIR/$f"
    fi
  done
  echo "Скрипты скопированы в $TARGET_DIR"
fi

# выбор дня начала подписки — предлагается при установке с нуля
ANSWER="${1:-}"
if [ "$ANSWER" = "--force" ]; then
  ANSWER=""
fi
if [ -z "$ANSWER" ] && [ -t 0 ]; then
  printf 'День начала отсчёта месячного плана z.ai: число 1-31 или дата YYYY-MM-DD.\n'
  printf 'Enter — вчера (%s): так все данные, уже имеющиеся в базе, попадают в расчёт.\n' "$(date -v-1d +%Y-%m-%d 2>/dev/null || date -d yesterday +%Y-%m-%d)"
  read -r ANSWER
fi
"$PY" - "$SKILL_DIR" "$ANSWER" <<'EOF'
import json, os, subprocess, sys
from datetime import date, timedelta

state = os.path.expanduser("~/.hermes/state")
os.makedirs(state, exist_ok=True)
path = os.path.join(state, "token_cost_plan.json")
yesterday = (date.today() - timedelta(days=1)).isoformat()
raw = sys.argv[2].strip()
value = yesterday
if raw:
    # M4: значение проверяется тем же валидатором, что и CLI — иначе навык
    # принимает nonsense вроде 'abc' или '2026-02-31' и молча пишет его в состояние
    validator = os.path.join(sys.argv[1], "scripts", "plan_usage.py")
    rc = subprocess.call([sys.executable, validator, "--set-anchor", raw, "--dry-run"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if rc != 0:
        print("не понял: нужен день 1-31 или дата YYYY-MM-DD; значение '%s' не принято" % raw)
        print("  использован день по умолчанию:", yesterday)
    else:
        value = raw
st = {"anchor_date": value, "anchor_source": "saved", "plan": "light", "model": "glm-5.3-flash"}
with open(path, "w", encoding="utf-8") as f:
    json.dump(st, f, ensure_ascii=False, indent=2)
print("записано:", path, "->", st["anchor_date"])
print("  свой день меняется так: python3 ~/.hermes/scripts/plan_usage.py --set-anchor <день или YYYY-MM-DD>")
EOF
