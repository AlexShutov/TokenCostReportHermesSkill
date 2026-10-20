#!/usr/bin/env python3
"""Тесты плагин-команды /token-cost-daily — offline, mock-spawn, без сети и без LLM.

Запуск: python3 tests/test_plugin_command.py
Проверяются правила навыка:
- обработчик возвращает None, когда сообщение уже отправлено скриптом: иначе в чат
  уйдёт вторая копия (правило — ровно одно сообщение на навык);
- чат берётся из сессионного контекста гейтвея, а не hardcoded;
- скрипт вызывается без LLM и без аргументов модели;
- ошибка запуска сообщается строкой, а не молчит.
"""
import os
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import importlib.util
_spec = importlib.util.spec_from_file_location("tcr_plugin", os.path.join(BASE, "plugin", "__init__.py"))
P = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(P)


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class Case(unittest.TestCase):
    def setUp(self):
        self.saved = dict(os.environ)
        self.calls = []

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)

    def fake_spawn(self, proc):
        def _spawn(env):
            self.calls.append(dict(env))
            return proc
        return _spawn


class CommandContract(Case):
    def test_handler_returns_none_when_script_already_delivered(self):
        """None — гейтвей ничего не добавляет в чат; сообщение уже отправлено скриптом."""
        out = P.run("", chat="362122320", spawn=self.fake_spawn(FakeProc()))
        self.assertIsNone(out, f"обработчик вернул текст — в чате будет вторая копия: {out!r}")

    def test_chat_comes_from_session_env(self):
        os.environ["HERMES_SESSION_CHAT_ID"] = "-100999"
        P.run("", spawn=self.fake_spawn(FakeProc()))
        self.assertEqual(self.calls[0]["TOKEN_COST_CHAT_ID"], "-100999",
                         f"чат не из сессионного контекста: {self.calls[0]}")

    def test_fallback_to_variable_then_default(self):
        os.environ.pop("HERMES_SESSION_CHAT_ID", None)
        os.environ["TOKEN_COST_CHAT_ID"] = "777"
        P.run("", spawn=self.fake_spawn(FakeProc()))
        self.assertEqual(self.calls[0]["TOKEN_COST_CHAT_ID"], "777")
        os.environ.pop("TOKEN_COST_CHAT_ID", None)
        P.run("", spawn=self.fake_spawn(FakeProc()))
        self.assertEqual(self.calls[-1]["TOKEN_COST_CHAT_ID"], P.DEFAULT_CHAT)

    def test_launch_failure_reports_instead_of_silence(self):
        out = P.run("", chat="1", spawn=self.fake_spawn(FakeProc(returncode=1, stderr="база заблокирована")))
        self.assertIsInstance(out, str)
        self.assertIn("база заблокирована", out, out)

    def test_spawn_failure_reports(self):
        def boom(env):
            raise OSError("нет python")
        out = P.run("", chat="1", spawn=boom)
        self.assertIn("не запустилось", out, out)

    def test_no_llm_and_no_model_arguments(self):
        """Команда обязана вызывать только Python-скрипт: никакой модели в пути нет."""
        P.run("", chat="1", spawn=self.fake_spawn(FakeProc()))
        argv = self.calls[0]
        self.assertTrue(argv.get("TOKEN_COST_CRON_JOB") == "token-cost-daily", argv)
        self.assertNotIn("HERMES_MODEL", argv, f"путь тянет модель: {argv}")


class Manifest(Case):
    def test_manifest_declares_the_command_name(self):
        with open(os.path.join(BASE, "plugin", "plugin.yaml"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("token-cost-daily", text)
        self.assertIn("provides_commands", text)

    def test_command_name_does_not_conflict_with_builtin(self):
        try:
            sys.path.insert(0, os.path.expanduser("~/.hermes/hermes-agent"))
            from hermes_cli.commands import resolve_command
        except Exception:
            self.skipTest("исходники Hermes недоступны — конфликт проверить нечем")
        self.assertIsNone(resolve_command("token-cost-daily"),
                         "имя конфликтует со встроенной командой — плагин не загрузится")


if __name__ == "__main__":
    unittest.main()
