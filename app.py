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
  ADMIN_TELEGRAM_CHAT_ID optional  where YOUR technical alerts go (default: TELEGRAM_CHAT_ID)

The Instagram token renews itself every 5 days (it would otherwise die after
60 days). The fresh token is kept in the database on the /data disk. If you
ever paste a new IG_ACCESS_TOKEN on Render, the server switches to it.
"""

import hashlib
import hmac
import json
import os
import sqlite3
import sys
import threading
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
IG_ENV_TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")
META_APP_SECRET = os.environ.get("META_APP_SECRET", "")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
ADMIN_CHAT = os.environ.get("ADMIN_TELEGRAM_CHAT_ID", "") or TG_CHAT
TOKEN_RENEW_EVERY = 5 * 86400   # renew the Instagram token every 5 days
TOKEN_CHECK_EVERY = 6 * 3600    # look at it every 6 hours
MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")
GRAPH_URL = os.environ.get("GRAPH_URL", "https://graph.instagram.com/v21.0/me/messages")
HISTORY_TURNS = 12
DB_PATH = "/data/assistant.db" if os.path.isdir("/data") else "assistant.db"
HANDOFF_TAG = "[MASTER]"

with open(os.path.join(os.path.dirname(__file__), "specialist.json"), encoding="utf-8") as f:
    SPECIALIST = json.load(f)

client = Anthropic()
app = Flask(__name__)

# Last webhook visits, viewable at /debug?key=TEST_KEY (helps when logs are empty)
RECENT = []


def remember(event):
    RECENT.append({"time": time.strftime("%H:%M:%S", time.gmtime()) + " UTC", **event})
    del RECENT[:-30]
    print("WEBHOOK:", event, flush=True)


# ---------------------------------------------------------------- memory
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS messages (
        user_id TEXT, role TEXT, text TEXT, ts REAL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS seen (mid TEXT PRIMARY KEY)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)""")
    return conn


def kv_get(key, default=None):
    with db() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def kv_set(key, value):
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, str(value)))


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
        print(f"Claude error: {e}", flush=True)
        err = str(e).lower()
        if "credit balance" in err or "billing" in err:
            alert_admin("💳 Закінчився баланс Anthropic — помічник не може відповідати. "
                        "Поповніть: https://console.anthropic.com/settings/billing", "claude", every=3600)
        elif "authentication" in err or "api key" in err or "401" in err:
            alert_admin("🔑 Anthropic не приймає ключ. Перевірте ANTHROPIC_API_KEY на Render.", "claude", every=3600)
        else:
            alert_admin(f"⚠️ Помилка Claude: {str(e)[:300]}", "claude", every=3600)
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


def alert_admin(text, key, every=86400):
    """Technical problems go to YOU (not the specialist), at most once per `every` seconds."""
    print("ALERT:", text, flush=True)
    last = float(kv_get(f"alert_{key}", 0))
    if time.time() - last < every or not (TG_TOKEN and ADMIN_CHAT):
        return
    kv_set(f"alert_{key}", time.time())
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": ADMIN_CHAT, "text": "[Технічне] " + text}, timeout=10)
    except requests.RequestException as e:
        print(f"Telegram error: {e}")


# ------------------------------------------------- Instagram token, renews itself
def ig_token():
    """The current Instagram token. Starts from IG_ACCESS_TOKEN on Render, then the
    renewed copies saved in the database are used. Pasting a new token on Render wins."""
    if IG_ENV_TOKEN:
        env_hash = hashlib.sha256(IG_ENV_TOKEN.encode()).hexdigest()
        if kv_get("ig_env_hash") != env_hash:      # first start, or you pasted a new token
            now = time.time()
            kv_set("ig_env_hash", env_hash)
            kv_set("ig_token", IG_ENV_TOKEN)
            kv_set("ig_renewed_at", now)
            kv_set("ig_expires_at", now + 60 * 86400)
            kv_set("ig_last_error", "")
    return kv_get("ig_token") or IG_ENV_TOKEN


def renew_token(force=False):
    token = ig_token()
    if not token:
        return "no token"
    age = time.time() - float(kv_get("ig_renewed_at", 0))
    if not force and age < TOKEN_RENEW_EVERY:
        return "not due yet"
    ok, data = False, {}
    try:
        r = requests.get("https://graph.instagram.com/refresh_access_token",
                         params={"grant_type": "ig_refresh_token", "access_token": token}, timeout=20)
        data = r.json()
        ok = r.ok and bool(data.get("access_token"))
    except (requests.RequestException, ValueError) as e:
        data = {"error": str(e)}
    now = time.time()
    if ok:
        kv_set("ig_token", data["access_token"])
        kv_set("ig_renewed_at", now)
        kv_set("ig_expires_at", now + int(data.get("expires_in") or 60 * 86400))
        kv_set("ig_last_error", "")
        print("Instagram token renewed", flush=True)
        return "renewed"
    err = json.dumps(data.get("error", data), ensure_ascii=False)[:300]
    kv_set("ig_last_error", time.strftime("%Y-%m-%d %H:%M UTC ", time.gmtime()) + err)
    days_left = (float(kv_get("ig_expires_at", 0)) - now) / 86400
    alert_admin(f"⚠️ Не вдалося оновити токен Instagram (лишилось ~{days_left:.0f} дн.). "
                f"Сервер повторить сам через 6 год. Помилка: {err}", "ig_token")
    return "failed"


def token_status():
    exp = float(kv_get("ig_expires_at", 0))
    renewed = float(kv_get("ig_renewed_at", 0))
    return {"has_token": bool(ig_token()),
            "last_renewed": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(renewed)) if renewed else None,
            "days_left": round((exp - time.time()) / 86400, 1) if exp else None,
            "last_error": kv_get("ig_last_error", "") or None}


def token_keeper():
    time.sleep(60)  # let the server start first
    while True:
        try:
            renew_token()
        except Exception as e:  # never let this thread die
            print(f"Token keeper error: {e}", flush=True)
        time.sleep(TOKEN_CHECK_EVERY)


def send_instagram(recipient_id, text):
    token = ig_token()
    if not token:
        print("No IG_ACCESS_TOKEN set; not sending:", text)
        return False
    r = requests.post(GRAPH_URL,
                      headers={"Authorization": f"Bearer {token}"},
                      json={"recipient": {"id": recipient_id}, "message": {"text": text}},
                      timeout=15)
    if not r.ok:
        print(f"Instagram send failed {r.status_code}: {r.text[:300]}", flush=True)
        if '"code":190' in r.text.replace(" ", ""):
            alert_admin("🔑 Токен Instagram недійсний — відповіді не надсилаються. Згенеруйте новий у Meta "
                        "(API setup with Instagram login → Generate token) і вставте в IG_ACCESS_TOKEN на Render.",
                        "ig_dead")
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
                   instagram_ready=bool(ig_token()), telegram_ready=bool(TG_TOKEN and TG_CHAT))


CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "")
PAGE_STYLE = ("<style>body{font-family:system-ui,Arial,sans-serif;max-width:720px;margin:40px auto;"
              "padding:0 16px;line-height:1.6;color:#1f2937}h1{font-size:24px}h2{font-size:18px;margin-top:28px}</style>")


@app.route("/privacy")
def privacy():
    return f"""<!doctype html><html lang="uk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Privacy Policy</title>{PAGE_STYLE}</head><body>
<h1>Privacy Policy / Політика конфіденційності</h1>
<p>This service is an online assistant that answers Instagram Direct messages on behalf of beauty specialists who connected their Instagram professional account to it.</p>
<h2>What data we process</h2>
<p>The text of messages sent to the connected Instagram account, the sender's Instagram user ID, and booking details the sender chooses to share (name, phone number, preferred date and time).</p>
<h2>Why</h2>
<p>Only to reply to the sender, to pass booking requests to the specialist, and to keep conversation context. We do not sell data, show ads, or use it for any other purpose.</p>
<h2>Who receives it</h2>
<p>The specialist who owns the connected account (via Telegram notifications), and the AI provider Anthropic, which generates reply text. Data is stored on our server hosted by Render.</p>
<h2>How long</h2>
<p>Conversation history is kept up to 90 days, then deleted. You can ask for earlier deletion at any time.</p>
<h2>Your rights / Видалення даних</h2>
<p>To delete your data, follow the instructions at <a href="/data-deletion">/data-deletion</a>.</p>
<h2>Contact</h2>
<p>{CONTACT_EMAIL or "Contact the specialist via Instagram Direct."}</p>
</body></html>"""


@app.route("/data-deletion")
def data_deletion():
    return f"""<!doctype html><html lang="uk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Data Deletion</title>{PAGE_STYLE}</head><body>
<h1>Data deletion / Видалення даних</h1>
<p>To delete all messages and booking details stored about you, send an email to {CONTACT_EMAIL or "the address on the privacy page"} with the subject "Delete my data" and your Instagram username, or write "видаліть мої дані" in Instagram Direct to the account you contacted.</p>
<p>We delete the data within 30 days and confirm by reply.</p>
<p>Щоб видалити ваші повідомлення та дані запису, напишіть «видаліть мої дані» в Direct акаунту, з яким ви листувалися, або на email вище. Ми видалимо дані протягом 30 днів.</p>
</body></html>"""


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


GRAPH_BASE = os.environ.get("GRAPH_BASE", "https://graph.instagram.com/v21.0")


@app.route("/subscribe")
def subscribe():
    """Subscribes the connected Instagram account to this app's 'messages' webhooks.
    Meta sends NO DMs to the server until this is done (once per account)."""
    if request.args.get("key") != TEST_KEY:
        abort(403)
    token = ig_token()
    if not token:
        return jsonify(error="IG_ACCESS_TOKEN is not set on Render"), 400
    auth = {"access_token": token}
    me = requests.get(f"{GRAPH_BASE}/me", params={**auth, "fields": "user_id,username"}, timeout=15)
    if not me.ok:
        return jsonify(step="read account", status=me.status_code, meta_says=me.json()), 400
    info = me.json()
    ig_id = info.get("user_id") or info.get("id")
    sub = requests.post(f"{GRAPH_BASE}/{ig_id}/subscribed_apps",
                        params={**auth, "subscribed_fields": "messages"}, timeout=15)
    check = requests.get(f"{GRAPH_BASE}/{ig_id}/subscribed_apps", params=auth, timeout=15)
    return jsonify(account=info.get("username"), ig_user_id=ig_id,
                   subscribe_result=sub.json() if sub.content else sub.status_code,
                   current_subscriptions=check.json() if check.content else check.status_code)


@app.route("/debug")
def debug():
    if request.args.get("key") != TEST_KEY:
        abort(403)
    return jsonify(instagram_token=token_status(), recent_webhook_visits=RECENT[::-1])


@app.route("/renew-token")
def renew_now():
    """Renew the Instagram token right now (normally happens by itself)."""
    if request.args.get("key") != TEST_KEY:
        abort(403)
    result = renew_token(force=True)
    return jsonify(result=result, instagram_token=token_status())


threading.Thread(target=token_keeper, daemon=True).start()


@app.route("/instagram/webhook", methods=["GET"])
def verify():
    ok = (request.args.get("hub.mode") == "subscribe"
          and request.args.get("hub.verify_token") == VERIFY_TOKEN)
    remember({"type": "verify", "ok": ok})
    if ok:
        return request.args.get("hub.challenge", ""), 200
    return "Verification failed", 403


@app.route("/instagram/webhook", methods=["POST"])
def incoming():
    raw = request.get_data()
    if not signature_ok(raw):
        remember({"type": "post", "rejected": "bad signature"})
        abort(403)
    payload = json.loads(raw or b"{}")
    remember({"type": "post", "object": payload.get("object"),
              "fields": [list(e.keys()) for e in payload.get("entry", [])],
              "preview": raw[:300].decode("utf-8", "replace")})
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):   # Meta's "Test" button uses this format
            if change.get("field") == "messages" and isinstance(change.get("value"), dict):
                entry.setdefault("messaging", []).append(change["value"])
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
