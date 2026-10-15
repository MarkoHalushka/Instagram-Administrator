"""
INSTAGRAM BOOKING ASSISTANT (v1)
Answers a beauty specialist's Instagram DMs with Claude, using ONLY her real
data from specialist.json. Collects booking requests, hands unclear cases to
her in Telegram.

Routes:
  GET  /                    health check
  POST /test                try the AI without Instagram (needs TEST_KEY)
  GET  /instagram/webhook   Meta's one-time verification
  POST /instagram/webhook   real incoming DMs from Meta

Env vars (set on Render, never in code):
  ANTHROPIC_API_KEY   required
  VERIFY_TOKEN        required  any secret word; you type the same word in Meta's webhook settings
  TEST_KEY            required  secret word for the /test route
  IG_ACCESS_TOKEN     for real Instagram replies (from Meta app dashboard)
  META_APP_SECRET     for real Instagram (checks messages really come from Meta)
  TELEGRAM_BOT_TOKEN  optional  for handoff to the specialist
  TELEGRAM_CHAT_ID    optional  the specialist's chat id with your bot
  CLAUDE_MODEL        optional  default claude-haiku-5-5 (cheap); claude-sonnet-5-5 for smarter replies
"""

import hashlib
import hmac
import json
import os
import sqlite3
import sys
import time

import requests
from anthropic import Anthropic
from flask import Flask, abort, jsonify, request

# ---------------------------------------------------------------- config
REQUIRED = ["ANTHROPIC_API_KEY", "VERIFY_TOKEN", "TEST_KEY"]
missing = [v for v in REQUIRED if not os.environ.get(v)]
if missing:
    sys.exit(f"Missing environment variables: {', '.join(missing)}")

VERIFY_TOKEN = os.environ["VERIFY_TOKEN"]
TEST_KEY = os.environ["TEST_KEY"]
IG_ACCESS_TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")
META_APP_SECRET = os.environ.get("META_APP_SECRET", "")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")
GRAPH_URL = os.environ.get("GRAPH_URL", "https://graph.instagram.com/v21.0/me/messages")
HISTORY_TURNS = 12
DB_PATH = "/data/assistant.db" if os.path.isdir("/data") else "assistant.db"
HANDOFF_TAG = "[MASTER]"

with open(os.path.join(os.path.dirname(__file__), "specialist.json"), encoding="utf-8") as f:
    SPECIALIST = json.load(f)

client = Anthropic()
app = Flask(__name__)


# ---------------------------------------------------------------- memory
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS messages (
        user_id TEXT, role TEXT, text TEXT, ts REAL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS seen (mid TEXT PRIMARY KEY)""")
    return conn


def already_handled(mid):
    """Meta can deliver the same message twice; answer each only once."""
    if not mid:
        return False
    with db() as conn:
        try:
            conn.execute("INSERT INTO seen (mid) VALUES (?)", (mid,))
            return False
        except sqlite3.IntegrityError:
            return True


def save(user_id, role, text):
    with db() as conn:
        conn.execute("INSERT INTO messages VALUES (?,?,?,?)", (user_id, role, text, time.time()))


def history(user_id):
    with db() as conn:
        rows = conn.execute(
            "SELECT role, text FROM messages WHERE user_id=? ORDER BY ts DESC LIMIT ?",
            (user_id, HISTORY_TURNS)).fetchall()
    msgs = [{"role": r, "content": t} for r, t in reversed(rows)]
    # Claude needs the conversation to start with the user and alternate roles
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    merged = []
    for m in msgs:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1]["content"] += "\n" + m["content"]
        else:
            merged.append(m)
    return merged


# ---------------------------------------------------------------- the AI
def system_prompt():
    s = SPECIALIST
    services = "\n".join(f"- {x['name']}: {x['price']}, {x['duration']}" for x in s["services"])
    extras = "\n".join(f"- {x['name']}: {x['price']}" for x in s["extras"])
    faq = "\n".join(f"- {x['q']} -> {x['a']}" for x in s["faq"])
    return f"""Ти — адміністраторка майстра манікюру {s['name']} ({s['brand']}, {s['city']}) і відповідаєш у Instagram Direct.
Ти жінка: завжди говори про себе в жіночому роді («записала», «передала», «уточню»).
Стиль: {s['tone']}. Пиши мовою клієнта (за замовчуванням українською). Відповіді короткі: 1–3 речення.

ЯК ПИСАТИ:
- Пиши як жива людина в Instagram, просто і тепло, без канцеляриту.
- Не використовуй шаблонних фраз: «Дякую за звернення», «Чим можу допомогти?», «Звертайтеся», «З повагою», «Бажаєте записатися?». Замість цього — природно: «Добрий день!», «Так, звісно», «На який день вам зручно?».
- Не перепитуй те, що клієнт уже сказав (послугу, день, час, ім'я). Подивись на всю розмову і питай лише те, чого бракує.
- Не повторюй усі деталі запису щоразу; підсумуй один раз, коли все зібрано.
- Не пропонуй додаткові послуги, якщо про них не питали.

Приклади тону:
Клієнт: «Скільки коштує манікюр з покриттям?» → «Добрий день! Манікюр з покриттям — 600 грн, займає приблизно 1 год 45 хв. На який день вам зручно?»
Клієнт: «Хочу на суботу на 12» → «Супер, субота о 12:00. Підкажіть, будь ласка, ваше ім'я і номер телефону?»

ДАНІ МАЙСТРА (єдине джерело правди):
Адреса: {s['address']}
Графік: {s['hours']}
Послуги:
{services}
Додатково:
{extras}
Запис: {s['booking_rules']}
Передоплата: {s['deposit_rule']}
Скасування: {s['cancel_rule']}
Часті питання:
{faq}

ПРАВИЛА:
1. Ніколи не вигадуй ціни, послуги, знижки чи вільні вікна. Якщо чогось немає в даних — не вгадуй.
2. Ти НЕ знаєш вільних вікон. Збери бажаний день і час, ім'я, телефон і напиши, що майстер підтвердить час найближчим часом.
3. Якщо клієнт надіслав фото дизайну — скажи, що майстер подивиться і назве точну ціну.
4. Якщо питання не покривається даними, клієнт незадоволений, скарга, щось медичне, або клієнт просить живу людину — ввічливо скажи, що передаєш питання майстру, і почни відповідь з тегу {HANDOFF_TAG}.
5. Коли зібрано всі дані для запису (послуга, день/час, ім'я, телефон) — підтверди коротко і почни відповідь з тегу {HANDOFF_TAG}.
6. Не кажи, що ти штучний інтелект, якщо не питають; якщо питають прямо — чесно скажи, що ти онлайн-помічник майстра.
7. Тег {HANDOFF_TAG} клієнт не побачить, він лише для системи."""


FALLBACK = "Дякую за повідомлення! Майстер відповість вам найближчим часом 🙏"


def ask_claude(user_id, text):
    save(user_id, "user", text)
    try:
        resp = client.messages.create(
            model=MODEL, max_tokens=500, system=system_prompt(), messages=history(user_id))
        reply = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    except Exception as e:
        print(f"Claude error: {e}")
        reply = HANDOFF_TAG + " " + FALLBACK
    if not reply:
        reply = HANDOFF_TAG + " " + FALLBACK

    handoff = reply.startswith(HANDOFF_TAG)
    clean = reply.replace(HANDOFF_TAG, "").strip()
    save(user_id, "assistant", clean)
    if handoff:
        notify_master(user_id, text, clean)
    return clean, handoff


# ---------------------------------------------------------------- outside world
def notify_master(user_id, client_text, ai_reply):
    msg = (f"📩 Потрібна ваша увага (клієнт {user_id})\n\n"
           f"Клієнт: {client_text}\n\nПомічник відповів: {ai_reply}")
    print("HANDOFF:", msg.replace("\n", " | "))
    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          json={"chat_id": TG_CHAT, "text": msg}, timeout=10)
        except requests.RequestException as e:
            print(f"Telegram error: {e}")


def send_instagram(recipient_id, text):
    if not IG_ACCESS_TOKEN:
        print("No IG_ACCESS_TOKEN set; not sending:", text)
        return False
    r = requests.post(GRAPH_URL,
                      headers={"Authorization": f"Bearer {IG_ACCESS_TOKEN}"},
                      json={"recipient": {"id": recipient_id}, "message": {"text": text}},
                      timeout=15)
    if not r.ok:
        print(f"Instagram send failed {r.status_code}: {r.text[:300]}")
    return r.ok


def signature_ok(raw_body):
    if not META_APP_SECRET:
        return True  # allowed only while testing; set META_APP_SECRET before real clients
    sent = request.headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + hmac.new(META_APP_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sent, expected)


# ---------------------------------------------------------------- routes
@app.route("/")
def health():
    return jsonify(status="running", model=MODEL, specialist=SPECIALIST.get("brand"),
                   instagram_ready=bool(IG_ACCESS_TOKEN), telegram_ready=bool(TG_TOKEN and TG_CHAT))


@app.route("/test", methods=["POST"])
def test():
    if request.headers.get("X-Test-Key") != TEST_KEY:
        abort(403)
    data = request.get_json(silent=True) or {}
    user = data.get("user", "test-user")
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify(error="send JSON like {\"text\": \"Скільки коштує манікюр?\"}"), 400
    reply, handoff = ask_claude(user, text)
    return jsonify(reply=reply, handed_to_master=handoff)


@app.route("/instagram/webhook", methods=["GET"])
def verify():
    if (request.args.get("hub.mode") == "subscribe"
            and request.args.get("hub.verify_token") == VERIFY_TOKEN):
        return request.args.get("hub.challenge", ""), 200
    return "Verification failed", 403


@app.route("/instagram/webhook", methods=["POST"])
def incoming():
    raw = request.get_data()
    if not signature_ok(raw):
        abort(403)
    payload = json.loads(raw or b"{}")
    for entry in payload.get("entry", []):
        for event in entry.get("messaging", []):
            msg = event.get("message") or {}
            if msg.get("is_echo") or msg.get("is_deleted"):
                continue  # our own replies / deletions
            if already_handled(msg.get("mid")):
                continue
            sender = (event.get("sender") or {}).get("id")
            if not sender:
                continue
            text = (msg.get("text") or "").strip()
            if not text and msg.get("attachments"):
                text = "[Клієнт надіслав фото/вкладення]"
            if not text:
                continue
            reply, _ = ask_claude(sender, text)
            send_instagram(sender, reply)
    return "ok", 200  # always 200 quickly, or Meta retries


if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 5000)))
