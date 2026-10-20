#!/usr/bin/env python3
"""Тесты доставки одного сообщения (single_message.py) — offline, mock-transport.

Запуск: python3 tests/test_single_message.py
Сеть не трогается: api() вызывает подменяемый transport (mock в тесте), dry-run
печатает, что отправил бы. Без mock-transport сетевой скрипт проверял бы только
текст исходника и проходил, когда логика сломана (правило навыка).
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, BASE)
import single_message as S


class Case(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="tcr-sm-")
        self.env = os.path.join(self.home, ".env")
        with open(self.env, "w", encoding="utf-8") as f:
            f.write("TELEGRAM_BOT_TOKEN=TESTTOKEN\n")
        S.ENV = self.env
        S.STATE = os.path.join(self.home, "state", "single_message.json")
        self.calls = []

        def transport(url, data):
            payload = json.loads(data)
            method = url.rsplit("/", 1)[-1]
            self.calls.append((method, payload))
            if method == "sendMessage":
                return {"ok": True, "result": {"message_id": 1000 + len(self.calls)}}
            return {"ok": True, "result": None}

        self.transport = transport

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)


class SplitParts(Case):
    def test_short_text_is_one_part(self):
        self.assertEqual(S.split_parts("привет"), ["привет"])

    def test_long_text_split_under_platform_limit(self):
        text = "\n".join("строка %04d %s" % (i, "x" * 60) for i in range(80))
        parts = S.split_parts(text)
        self.assertGreater(len(parts), 1, "длинный текст обязан делиться до отправки")
        self.assertTrue(all(len(p) <= 4090 for p in parts),
                        f"часть превышает лимит Bot API: {max(len(p) for p in parts)}")
        self.assertEqual("\n".join(parts), text, "при делении потерялось содержимое")

    def test_split_happens_before_sending_not_after_failure(self):
        """Правило навыка: sendMessage вернёт 400 Bad Request, если текст больше
        4096 — делать надо ДО отправки. Проверяется по записи transport."""
        text = "\n".join("y" * 200 for _ in range(25))
        r = S.send("token-cost", "362122320", text, transport=self.transport)
        self.assertTrue(r["ok"], r)
        sent = [c for c in self.calls if c[0] == "sendMessage"]
        self.assertGreater(len(sent), 1)
        self.assertTrue(all(len(c[1]["text"]) <= 4096 for c in sent),
                        "отправлена часть длиннее лимита")

    def test_dry_run_prints_what_it_would_send_without_network(self):
        r = S.send("token-cost", "362122320", "текст", transport=self.transport, dry=True)
        self.assertTrue(r.get("dry"))
        self.assertFalse(self.calls, "dry-run не должен ничего отправлять")
        self.assertEqual(r["parts"], ["текст"])


class Markdown(Case):
    def test_html_conversion_and_escaping(self):
        out = S.to_html("**жир** *кур* `код` [ссылка](https://example.com) & <x>")
        self.assertIn("<b>жир</b>", out)
        self.assertIn("<i>кур</i>", out)
        self.assertIn("<code>код</code>", out)
        self.assertIn('<a href="https://example.com">ссылка</a>', out)
        self.assertIn("&amp;", out)
        self.assertIn("&lt;x&gt;", out)


class StateAndDeletion(Case):
    def test_previous_messages_of_the_same_key_are_deleted(self):
        os.makedirs(os.path.dirname(S.STATE), exist_ok=True)
        with open(S.STATE, "w", encoding="utf-8") as f:
            json.dump({"token-cost": [11, 12]}, f)
        r = S.send("token-cost", "362122320", "новое", transport=self.transport)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["deleted"], 2, "прежние сообщения этого ключа не удалены")
        dels = [c for c in self.calls if c[0] == "deleteMessage"]
        self.assertEqual([d[1]["message_id"] for d in dels], [11, 12])
        with open(S.STATE, encoding="utf-8") as f:
            st = json.load(f)
        self.assertEqual(len(st["token-cost"]), 1, "состояние должно хранить только новые id")

    def test_other_keys_are_not_touched(self):
        os.makedirs(os.path.dirname(S.STATE), exist_ok=True)
        with open(S.STATE, "w", encoding="utf-8") as f:
            json.dump({"weather": [7]}, f)
        S.send("token-cost", "362122320", "новое", transport=self.transport)
        with open(S.STATE, encoding="utf-8") as f:
            st = json.load(f)
        self.assertEqual(st.get("weather"), [7], "чужой ключ трогать нельзя")

    def test_missing_token_reports_honestly(self):
        S.ENV = os.path.join(self.home, "нет-файла.env")
        r = S.send("token-cost", "362122320", "текст", transport=self.transport)
        self.assertFalse(r["ok"])
        self.assertIn("TELEGRAM_BOT_TOKEN", r["error"])



# --- доставка без LLM (запуск по расписанию) --------------------------------
class Delivery(Case):
    """Правило пользователя: навык обязан работать, даже если модель отключена —
    расчёт, формат и отправка делаются одним Python3-скриптом, без агента."""

    ROWS = [("s1", "qwen", "custom", "http://x", "", "chat", 1000, 2000, 300, 0, 100, 100),
            ("s1", "glm", "zai", "https://api.z.ai", "", "chat", 2500000, 0, 0, 0, 100, 100)]

    def setUp(self):
        super().setUp()
        import importlib
        import token_cost as C
        # пути решаются при импорте: без перезагрузки модуль остаётся с HOME предыдущего
        # теста, и патч main() из одного теста утекает в следующий
        importlib.reload(C)
        self.C = C
        C.conn = lambda: self.fake_source()
        C.resolve_session = lambda c: "s1"
        C.fetch_prices = lambda: (None, "тест без сети")
        C.fetch_rate = lambda: (None, "тест без сети")
        os.environ.pop("TOKEN_COST_CHAT_ID", None)

    def fake_source(self):
        import sqlite3
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE session_model_usage (" +
                    ", ".join(("session_id", "model", "billing_provider", "billing_base_url",
                              "billing_mode", "task", "input_tokens", "cache_read_tokens",
                              "output_tokens", "actual_cost_usd", "first_seen", "last_seen")) + ")")
        con.executemany("INSERT INTO session_model_usage VALUES(" + ",".join("?" * 12) + ")",
                        self.ROWS)
        con.execute("CREATE TABLE sessions (id TEXT, model TEXT, started_at REAL)")
        con.execute("CREATE TABLE gateway_routing (chat_id TEXT, profile TEXT)")
        return con

    def sent_texts(self):
        return [c[1].get("text") for c in self.calls if c[0] == "sendMessage"]

    def test_delivery_happens_without_an_agent(self):
        import token_cost_commit as W
        r = W.run(transport=self.transport)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["sent"], 1, f"сообщение не отправлено: {r}")
        self.assertEqual(len(self.calls), 1, f"неожиданный вызов API: {self.calls}")

    def test_sent_message_is_exactly_two_lines(self):
        import token_cost_commit as W
        W.run(transport=self.transport)
        text = self.sent_texts()[0]
        plain = text.replace("<b>", "").replace("</b>", "").strip()
        lines = [l for l in plain.split("\n") if l]
        self.assertEqual(len(lines), 2, f"в сообщении не две строки: {plain!r}")
        self.assertTrue(lines[0].startswith("Равно") and "на Z.ai" in lines[0], lines[0])
        self.assertIn("Эквивалентный кодовый план z.ai", lines[1], lines[1])

    def test_no_reasoning_reaches_the_chat(self):
        import token_cost_commit as W
        W.run(transport=self.transport)
        text = self.sent_texts()[0]
        for phrase in ("Суточное окно", "Месячный лимит", "MCP-вызовы", "Пик/офф-пик"):
            self.assertNotIn(phrase, text, f"размышление ушло в чат: {text!r}")

    def test_empty_output_sends_nothing(self):
        import token_cost_commit as W
        self.C.main = lambda: ""
        r = W.run(transport=self.transport)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["sent"], 0, f"пустой вывод ушёл в чат: {r}")
        self.assertEqual(self.calls, [], "пустой вывод — отправки быть не должно")

    def test_chat_comes_from_env_on_other_machines(self):
        import token_cost_commit as W
        os.environ["TOKEN_COST_CHAT_ID"] = "-100999"
        W.run(transport=self.transport)
        self.assertEqual(self.calls[0][1]["chat_id"], "-100999",
                         "чат берётся не из переменной — на чужой машине доставка уйдёт автору")
        os.environ.pop("TOKEN_COST_CHAT_ID", None)
        W.run(transport=self.transport)
        self.assertEqual(self.calls[1][1]["chat_id"], "362122320",
                         "без переменной должен оставаться чат автора")

    def test_long_message_is_split_before_sending(self):
        """Ветка переполнения Max — сообщение длинное: деление происходит до отправки."""
        import token_cost_commit as W
        self.C.main = lambda: "\n".join("з" * 200 for _ in range(25))
        W.run(transport=self.transport)
        texts = self.sent_texts()
        self.assertGreater(len(texts), 1, "длинное сообщение отправлено одним куском")
        self.assertTrue(all(len(t) <= 4090 for t in texts),
                        f"часть превышает лимит Bot API: {max(len(t) for t in texts)}")

    def test_previous_messages_of_the_same_key_are_deleted(self):
        import token_cost_commit as W
        os.makedirs(os.path.dirname(S.STATE), exist_ok=True)
        with open(S.STATE, "w", encoding="utf-8") as f:
            json.dump({"token-cost": [11]}, f)
        W.run(transport=self.transport)
        dels = [c for c in self.calls if c[0] == "deleteMessage"]
        self.assertEqual([d[1]["message_id"] for d in dels], [11],
                         "прежнее сообщение навыка не удалено")


if __name__ == "__main__":
    unittest.main(verbosity=2)
