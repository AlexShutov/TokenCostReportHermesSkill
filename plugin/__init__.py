"""Плагин: /token-cost-daily — отчёт сразу, без LLM.

Контракт обработчика плагин-команд: ``fn(raw_args) -> str | None``. Gateway доставляет
возвращённую строку как ответ в чат; ``None`` — значит в чат ничего не отправляется.
Это и есть нужное поведение: сообщение уже отправлено одним сообщением через Bot API
(scripts/single_message.py, прежнее трэкированное сообщение удаляется), и второй копии
быть не должно — правило навыка «ровно одно сообщение на навык».

Чат берётся из сессионного контекста гейтвея (HERMES_SESSION_CHAT_ID), чтобы команда
работала в том чате, где её набрали; на чужой машине — TOKEN_COST_CHAT_ID, иначе
значение по умолчанию.

Расчёт и доставка — только Python-скрипт: если модель, на которой работает Hermes,
отключена, команда всё равно срабатывает.
"""
import os
import subprocess
import sys

HOME = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
SCRIPT = os.path.join(HOME, "scripts", "token_cost_commit.py")
DEFAULT_CHAT = "362122320"
TIMEOUT = 60
SPAWN = None   # подменяемая точка вызова: тест обязан иметь mock, иначе проверка
               # сводится к чтению исходника и проходит, когда логика сломана


def _default_spawn(env):
    return subprocess.run([sys.executable, SCRIPT], env=env,
                          capture_output=True, text=True, timeout=TIMEOUT)


def resolve_chat():
    """Чат, где набрали команду; вне гейтвея — переменная, иначе значение по умолчанию."""
    try:
        from gateway.session_context import get_session_env
        chat = get_session_env("HERMES_SESSION_CHAT_ID") or ""
    except Exception:
        chat = os.environ.get("HERMES_SESSION_CHAT_ID") or ""
    return chat or os.environ.get("TOKEN_COST_CHAT_ID") or DEFAULT_CHAT


def run(raw_args="", chat=None, spawn=None):
    """Запуск скрипта. ``spawn`` — подменяемый вызов (mock в тесте): без него это
    реальный subprocess, и тест не мог бы проверить, какой чат подставлен."""
    chat = chat or resolve_chat()
    env = dict(os.environ)
    env["TOKEN_COST_CHAT_ID"] = chat
    env["TOKEN_COST_CRON_JOB"] = "token-cost-daily"
    if spawn is None:
        spawn = SPAWN or _default_spawn
    try:
        proc = spawn(env)
    except Exception as exc:
        return "не запустилось: %s" % exc
    if getattr(proc, "returncode", 0) != 0:
        return "ОШКА: %s" % (getattr(proc, "stderr", "") or "").strip()
    # stdout пуст по правилам навыка: сообщение уже отправлено через Bot API,
    # обработчик возвращает None — гейтвей ничего не добавляет в чат
    return None


def register(ctx):
    ctx.register_command(
        "token-cost-daily",
        lambda args: run(args),
        description="Отчёт token-cost сразу, без LLM (тот же скрипт, что и крон-задача).",
    )
