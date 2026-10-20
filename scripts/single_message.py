#!/usr/bin/env python3
"""Одно сообщение на навык в Telegram: отправляет новое и удаляет все прежние этого же ключа.

Ключи: "weather", "token-cost". Состояние: ~/.hermes/state/single_message.json
Текст читается из stdin (или из третьего аргумента). Markdown переводится в HTML Bot API.
Печатает короткий отчёт для агента (sent/deleted) — в чат ничего не идёт.

Файл обязан лежать в репо навыка: SKILL.md ссылается на него как на шаг доставки,
и на машине коллеги без него доставка одного сообщения не работает.
"""
import json, os, re, sys, urllib.request

ENV = os.path.expanduser("~/.hermes/.env")
STATE = os.path.expanduser("~/.hermes/state/single_message.json")


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def token():
    try:
        with open(ENV, encoding="utf-8") as f:
            for line in f:
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return None


def http_post(url, data):
    req = urllib.request.Request(url, data=data.encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def api(method, chat_id, transport=None, **kw):
    """transport — подменяемая отправка (mock в тесте); без неё это сетевой скрипт,
    который нельзя проверить offline."""
    t = token()
    if not t:
        return {"ok": False, "error": "нет TELEGRAM_BOT_TOKEN"}
    send = transport or http_post
    try:
        r = send("https://api.telegram.org/bot%s/%s" % (t, method),
                 json.dumps({"chat_id": chat_id, **kw}))
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if not r.get("ok"):
        return {"ok": False, "error": r.get("description", "ошибка API")}
    res = r.get("result")
    return {"ok": True, "message_id": res.get("message_id") if isinstance(res, dict) else None}


def to_html(text):
    s = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<i>\1</i>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r'<a href="\2">\1</a>', s)
    return s


def split_parts(text, limit=4090):
    """Bot API принимает не больше 4096 символов — длинный текст режется по
    границам строк ДО отправки, а не после отказа."""
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = line
        else:
            cur = (cur + "\n" + line).strip("\n")
    if cur:
        parts.append(cur)
    return parts


def send(key, chat_id, text, transport=None, dry=False):
    try:
        state = read_json(STATE)
    except Exception:
        state = {}
    old = state.get(key, [])
    parts = split_parts(text)
    if dry:
        return {"ok": True, "dry": True, "parts": parts, "would_delete": list(old), "sent": len(parts)}

    deleted = 0
    sent_ids = []
    for part in parts:
        res = api("sendMessage", chat_id, transport=transport, text=to_html(part),
                  parse_mode="HTML", disable_web_page_preview=True)
        if not res["ok"]:
            return {"ok": False, "error": res["error"], "deleted": deleted, "sent": len(sent_ids)}
        sent_ids.append(res["message_id"])

    for mid in old:
        r = api("deleteMessage", chat_id, transport=transport, message_id=mid)
        if r["ok"]:
            deleted += 1

    state[key] = sent_ids
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    write_json(STATE, state)
    return {"ok": True, "message_id": sent_ids[0], "deleted": deleted, "sent": len(sent_ids)}


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: single_message.py <key> <chat_id> [text] [--dry-run]", file=sys.stderr)
        sys.exit(1)
    key, chat = sys.argv[1], sys.argv[2]
    dry = "--dry-run" in sys.argv
    text = sys.stdin.read().strip() if not sys.stdin.isatty() else (sys.argv[3] if len(sys.argv) > 3 else "")
    if not text:
        print("пустой текст — ничего не отправлено")
        sys.exit(0)
    r = send(key, chat, text, dry=dry)
    if r.get("dry"):
        print("dry-run: частей %d (максимум %d символов), удалил бы: %s"
              % (r["sent"], max(len(p) for p in r["parts"]), r["would_delete"]))
    elif r["ok"]:
        print("sent %s (частей: %d), удалено старых: %d" % (r["message_id"], r["sent"], r["deleted"]))
    else:
        print("ОШКА: %s" % r["error"])
        sys.exit(1)
