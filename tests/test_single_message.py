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


if __name__ == "__main__":
    unittest.main(verbosity=2)
