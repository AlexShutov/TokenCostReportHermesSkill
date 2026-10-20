#!/usr/bin/env python3
"""Запуск отчёта token-cost-report по расписанию — полностью без LLM.

Скрипт сам считает, сам форматирует и сам отправляет сообщение в чат: в этом ходу нет
агента, и если модель, на которой работает Hermes, отключена, навык всё равно срабатывает.
Расчёт — доли секунды (SQLite + словарь мультипликаторов, без сети на ручном запуске).

Только этот путь обновляет курс и пишет снимок 21:00 (--commit); каждый запуск так же
пишется в append-базу token_cost.db (runs / usage_snapshot / increments).

Путь решается относительно самого файла, а не абсолютным путём автора: на машине коллеги
/Users/alex/... не существует, и крон-задача падает.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import token_cost as C           # путь решается до импорта
import single_message as S

KEY = "token-cost"


def chat_id():
    """Чат автора по умолчанию; на чужой машине задаётся переменной TOKEN_COST_CHAT_ID."""
    return os.environ.get("TOKEN_COST_CHAT_ID") or "362122320"


def run(transport=None, dry=False):
    """Расчёт + отправка одним скриптом. transport — подменяемая отправка (mock в тесте):
    без неё это сетевой путь, который нельзя проверить offline."""
    os.environ.setdefault("TOKEN_COST_CRON_JOB", "token-cost-daily")
    sys.argv = ["token_cost.py", "--commit"]
    text = C.main()
    if not text:
        return {"ok": True, "sent": 0, "note": "пустой вывод — ничего не отправлено"}
    return S.send(KEY, chat_id(), text, transport=transport, dry=dry)


if __name__ == "__main__":
    r = run()
    if not r.get("ok"):
        print("ОШКА: %s" % r.get("error"), file=sys.stderr)
        sys.exit(1)
    if r.get("dry"):
        print("dry-run: частей %d (максимум %d символов), удалил бы: %s"
              % (r["sent"], max(len(p) for p in r["parts"]), r["would_delete"]))
    elif r.get("sent"):
        print("sent %s (частей: %d), удалено старых: %d"
              % (r.get("message_id"), r["sent"], r.get("deleted", 0)))
