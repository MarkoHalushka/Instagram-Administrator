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
  GET  /telegram/setup      open once (?key=TEST_KEY) to switch on the Telegram buttons
  POST /telegram/webhook    button taps and replies from the specialist's Telegram
  GET  /subscribe, /debug, /renew-token   maintenance (?key=TEST_KEY)

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
import random
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
MIN_DELAY = float(os.environ.get("MIN_DELAY", 2))    # seconds before a short reply
MAX_DELAY = float(os.environ.get("MAX_DELAY", 10))   # seconds before a long reply
MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")
GRAPH_URL = os.environ.get("GRAPH_URL", "https://graph.instagram.com/v21.0/me/messages")
HISTORY_TURNS = 12
DB_PATH = "/data/assistant.db" if os.path.isdir("/data") else "assistant.db"
HANDOFF_TAG = "[MASTER]"     # AI can't answer -> ask the specialist
BOOKING_TAG = "[BOOKING]"    # all booking details collected -> specialist confirms
NOTE_TAG = "[ДЛЯ МАЙСТРА]"   # anything after it goes only to the specialist's Telegram
SHOW_TYPING = os.environ.get("SHOW_TYPING", "0") == "1"   # "seen" + "typing…" in Instagram

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
def system_prompt(ongoing=False):
    s = SPECIALIST
    portfolio = s.get("portfolio") or "приклади робіт є в нашому профілі Instagram"
    greeting = ("Розмова вже триває і ви вже віталися: НЕ вітайся знову, одразу відповідай по суті."
                if ongoing else "Це початок розмови: можна коротко привітатися.")
    services = "\n".join(f"- {x['name']}: {x['price']}, {x['duration']}" for x in s["services"])
    extras = "\n".join(f"- {x['name']}: {x['price']}" for x in s["extras"])
    faq = "\n".join(f"- {x['q']} -> {x['a']}" for x in s["faq"])
    return f"""Ти — адміністраторка майстра манікюру {s['name']} ({s['brand']}, {s['city']}) і відповідаєш у Instagram Direct.
Ти жінка: завжди говори про себе в жіночому роді («записала», «передала», «уточню»).
Стиль: {s['tone']}. Пиши мовою клієнта (за замовчуванням українською). Відповіді короткі: 1–2 речення, максимум 3.
{greeting}

ЯК ПИСАТИ:
- Пиши як жива людина в Instagram, просто і тепло, без канцеляриту.
- Не використовуй шаблонних фраз: «Дякую за звернення», «Чим можу допомогти?», «Звертайтеся», «З повагою», «Бажаєте записатися?». Замість цього — природно: «Добрий день!», «Так, звісно», «На який день вам зручно?».
- Не перепитуй те, що клієнт уже сказав (послугу, день, час, ім'я). Подивись на всю розмову і питай лише те, чого бракує.
- Не повторюй усі деталі запису щоразу; підсумуй один раз, коли все зібрано.
- Не пропонуй додаткові послуги, якщо про них не питали.
- Твоя відповідь — це повідомлення, яке клієнт побачить у Direct. Звертайся тільки до клієнта, на «ви».
  Ніколи не пиши в ній нотаток, підсумків чи інструкцій для майстра і не говори про клієнта в третій особі.
- Не вгадуй стать клієнта за іменем; якщо невідомо — пиши нейтрально.

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
   Якщо просять фото робіт — {portfolio}. Не кажи, що в тебе «немає фото під рукою».
4. Якщо питання не покривається даними, клієнт незадоволений, скарга, щось медичне, або клієнт просить живу людину — ввічливо скажи, що передаєш питання майстру, і почни відповідь з тегу {HANDOFF_TAG}.
5. Коли зібрано всі дані для запису (послуга, день/час, ім'я, телефон) — коротко скажи, що передала заявку і майстер підтвердить час, і почни відповідь з тегу {BOOKING_TAG}.
6. Не кажи, що ти штучний інтелект, якщо не питають; якщо питають прямо — чесно скажи, що ти онлайн-помічник майстра.
7. Теги {HANDOFF_TAG} і {BOOKING_TAG} клієнт не побачить, вони лише для системи.
8. Коли ставиш тег, після тексту для клієнта можеш з нового рядка написати {NOTE_TAG} і ОДИН короткий рядок-підсумок для майстра (що хоче клієнт). Цей рядок клієнт не побачить."""


FALLBACK = "Дякую за повідомлення! Майстер відповість вам найближчим часом 🙏"


def ask_claude(user_id, text):
    """Instant version (used by /test): save, think, finish."""
    save(user_id, "user", text)
    clean, kind, client_text, note = think(user_id)
    finish(user_id, clean, kind, client_text, note)
    return clean, bool(kind)


def finish(user_id, clean, kind, client_text, note=""):
    save(user_id, "assistant", clean)
    if kind:
        notify_master(user_id, client_text, clean, kind, note)


def think(user_id):
    """Asks Claude about the whole conversation. Saves nothing.
    Returns (reply without tags, kind, what the client wrote since our last reply).
    kind: None = normal answer, "booking" = all booking details collected,
    "question" = needs the specialist."""
    msgs = history(user_id)
    client_text = msgs[-1]["content"] if msgs and msgs[-1]["role"] == "user" else ""
    with db() as conn:
        last_ours = conn.execute("SELECT MAX(ts) FROM messages WHERE user_id=? AND role='assistant'",
                                 (user_id,)).fetchone()[0]
    ongoing = bool(last_ours) and time.time() - last_ours < 12 * 3600
    try:
        resp = client.messages.create(
            model=MODEL, max_tokens=400, system=system_prompt(ongoing), messages=msgs)
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

    kind = "booking" if BOOKING_TAG in reply else "question" if HANDOFF_TAG in reply else None
    clean = reply.replace(BOOKING_TAG, "").replace(HANDOFF_TAG, "").strip()
    clean, _, note = clean.partition(NOTE_TAG)
    clean, note = clean.strip(), note.strip(" :\n")
    # safety net: a paragraph talking ABOUT the client is a note for the specialist, not for the client
    parts = [p.strip() for p in clean.split("\n\n") if p.strip()]
    if len(parts) > 1:
        about = [p for p in parts[1:] if p.lower().startswith(("клієнт", "клиент", "client", "уточнити", "майстру"))]
        if about:
            note = " ".join([note] + about).strip()
            clean = "\n\n".join(p for p in parts if p not in about)
    return clean, kind, client_text, note


# ---------------------------------------------------------------- human-like timing
def human_delay(reply):
    """Seconds between the client's message and our reply, counted from when it arrived.
    ~30 chars -> ~3 s, ~100 -> ~5 s, ~200 -> ~9 s, longer -> 10 s, plus a little randomness."""
    return max(MIN_DELAY, min(MAX_DELAY, 1.5 + len(reply) / 25 + random.uniform(-0.5, 0.8)))


def answer_later(sender, my_id, received_at, text=""):
    """Runs in the background so Meta gets its 'ok' at once. If the client sends
    another message before we reply, this answer is dropped and the newest one
    answers everything together (like a person reading all messages first)."""
    try:
        if is_paused(sender):   # the specialist took over this client: just forward to Telegram
            tg_to_master(f"💬 {client_label(sender)} (помічник на паузі):\n{text}", sender,
                         [[btn("✍️ Відповісти", "rep", sender), btn("▶️ Увімкнути помічника", "resume", sender)]])
            return

        def superseded():
            return kv_get(f"latest_{sender}") != my_id

        clean, kind, client_text, note = think(sender)
        if superseded():
            return
        wait = human_delay(clean) - (time.time() - received_at)
        if wait > 1 and SHOW_TYPING:
            sender_action(sender, "mark_seen")
            time.sleep(min(1.0, wait / 3))
            sender_action(sender, "typing_on")
            time.sleep(max(0, human_delay(clean) - (time.time() - received_at)))
        elif wait > 0:
            time.sleep(wait)
        if superseded():
            return
        if send_instagram(sender, clean):
            finish(sender, clean, kind, client_text, note)
    except Exception as e:  # a background error must never crash the server
        print(f"Reply error for {sender}: {e}", flush=True)


def manual_reply_check(client_id, mid, text):
    """Instagram echoes every message the account sends. If the echo is NOT one we
    sent, the specialist typed it herself in Instagram: step back for PAUSE_HOURS."""
    try:
        time.sleep(3)  # give send_instagram time to record its own message ids
        if not mid or kv_get(f"sent_{mid}"):
            return
        if text:
            save(client_id, "assistant", text)   # so the AI knows what she said
        was_paused = is_paused(client_id)
        set_pause(client_id, True)
        if not was_paused:
            tg_to_master(f"⏸ Ви відповіли {client_label(client_id)} в Instagram самі — "
                         f"помічник не втручається в цю розмову {PAUSE_HOURS:g} год.", client_id,
                         [[btn("▶️ Увімкнути помічника", "resume", client_id)]])
    except Exception as e:
        print(f"Echo check error: {e}", flush=True)


# ---------------------------------------------------------------- Telegram: the specialist's remote control
TG_SECRET = hashlib.sha256(("tg-webhook" + TG_TOKEN).encode()).hexdigest()[:40] if TG_TOKEN else ""
PAUSE_HOURS = float(os.environ.get("PAUSE_HOURS", 12))
TG_HELP = ("Привіт! Це пульт вашої онлайн-адміністраторки 💅\n\n"
           "Сюди приходять нові записи і питання, на які вона не знає відповіді.\n"
           "• ✅ Підтвердити — клієнт отримає підтвердження запису\n"
           "• ✍️ Відповісти — ваш текст піде клієнту в Instagram від вашого імені\n"
           "• ⏸ Відповім сама — помічник замовкає з цим клієнтом на "
           f"{PAUSE_HOURS:g} год (▶️ — увімкнути знову)\n\n"
           "Також можна просто відповісти (свайп вліво) на будь-яке повідомлення про клієнта. "
           "Якщо ви самі напишете клієнту в Instagram, помічник теж автоматично відступить.")


def tg(method, **payload):
    if not TG_TOKEN:
        return {}
    try:
        return requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", json=payload, timeout=15).json()
    except (requests.RequestException, ValueError) as e:
        print(f"Telegram {method} error: {e}", flush=True)
        return {}


def btn(text, action, uid):
    return {"text": text, "callback_data": f"{action}:{uid}"}


def tg_to_master(text, uid=None, buttons=None, markup=None):
    """Message to the specialist. Remembers which client it is about, so replying to it reaches that client."""
    if not TG_CHAT:
        return None
    payload = {"chat_id": TG_CHAT, "text": text[:4000]}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    if markup:
        payload["reply_markup"] = markup
    msg_id = ((tg("sendMessage", **payload) or {}).get("result") or {}).get("message_id")
    if msg_id and uid:
        kv_set(f"tgmap_{msg_id}", uid)
    return msg_id


def client_label(uid):
    """@username (Name) if Instagram tells us, otherwise a short id."""
    cached = kv_get(f"name_{uid}")
    if cached:
        return cached
    token = ig_token()
    if token and str(uid).isdigit():
        try:
            r = requests.get(f"{GRAPH_BASE}/{uid}", params={"fields": "username,name", "access_token": token}, timeout=10)
            d = r.json() if r.ok else {}
            if d.get("username"):
                label = "@" + d["username"] + (f" ({d['name']})" if d.get("name") else "")
                kv_set(f"name_{uid}", label)
                return label
        except (requests.RequestException, ValueError):
            pass
    return f"клієнт …{str(uid)[-4:]}"


def is_paused(uid):
    return float(kv_get(f"pause_{uid}", 0)) > time.time()


def set_pause(uid, on):
    kv_set(f"pause_{uid}", time.time() + PAUSE_HOURS * 3600 if on else 0)


def notify_master(user_id, client_text, ai_reply, kind="question", note=""):
    head = "🗓 Нова заявка на запис" if kind == "booking" else "📩 Потрібна ваша відповідь"
    msg = f"{head} — {client_label(user_id)}\n\n"
    if note:
        msg += f"📝 {note}\n\n"
    msg += f"Клієнт: {client_text}\n\nПомічник відповів: {ai_reply}"
    print("HANDOFF:", msg.replace("\n", " | "), flush=True)
    rows = [[btn("✅ Підтвердити", "ok", user_id)]] if kind == "booking" else []
    rows.append([btn("✍️ Відповісти", "rep", user_id), btn("⏸ Відповім сама", "pause", user_id)])
    tg_to_master(msg, user_id, rows)


def confirmation_text(uid):
    """Claude writes a short confirmation using the day/time/service from the conversation."""
    transcript = "\n".join(("Клієнт: " if m["role"] == "user" else "Ти: ") + m["content"] for m in history(uid))
    try:
        resp = client.messages.create(model=MODEL, max_tokens=200, system=system_prompt(), messages=[{
            "role": "user",
            "content": f"Розмова з клієнтом:\n{transcript}\n\nМайстер щойно ПІДТВЕРДИЛА цей запис. Напиши клієнту "
                       "одне коротке тепле повідомлення: запис підтверджено, назви послугу, день і час з розмови, "
                       "нагадай адресу. Лише текст повідомлення, без тегів."}])
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        text = text.replace(BOOKING_TAG, "").replace(HANDOFF_TAG, "").strip()
        if text:
            return text
    except Exception as e:
        print(f"Confirmation error: {e}", flush=True)
    return f"Ваш запис підтверджено 🙌 Чекаємо вас за адресою: {SPECIALIST.get('address', '')}"


def swap_buttons(message, remove=(), add=None):
    """Updates the buttons under a Telegram message after a tap."""
    rows = [[b for b in row if b.get("callback_data", "").split(":")[0] not in remove]
            for row in (message.get("reply_markup") or {}).get("inline_keyboard", [])]
    rows = [r for r in rows if r]
    if add:
        rows.append(add)
    tg("editMessageReplyMarkup", chat_id=TG_CHAT, message_id=message["message_id"],
       reply_markup={"inline_keyboard": rows})


WINDOW_HINT = ("❌ Instagram не прийняв повідомлення. Найчастіше це тому, що клієнт писав понад 24 год тому — "
               "тоді відповісти можна лише вручну в Instagram.")


def on_button(action, uid, message):
    label = client_label(uid)
    if action == "ok":
        text = confirmation_text(uid)
        if not send_instagram(uid, text):
            tg_to_master(WINDOW_HINT, uid)
            return "Не вдалося надіслати"
        save(uid, "assistant", text)
        tg("editMessageText", chat_id=TG_CHAT, message_id=message["message_id"],
           text=(message.get("text") or "") + f"\n\n✅ Підтверджено. Клієнту надіслано:\n«{text}»",
           reply_markup={"inline_keyboard": [r for r in (
               [[b for b in row if not b["callback_data"].startswith("ok:")]
                for row in (message.get("reply_markup") or {}).get("inline_keyboard", [])]) if r]})
        return "Підтверджено ✅"
    if action == "rep":
        tg_to_master(f"✍️ Напишіть відповідь для {label} у відповідь на це повідомлення:", uid,
                     markup={"force_reply": True, "input_field_placeholder": "Ваша відповідь клієнту"})
        return "Напишіть відповідь"
    if action == "pause":
        set_pause(uid, True)
        swap_buttons(message, remove=("pause", "resume"), add=[btn("▶️ Увімкнути помічника", "resume", uid)])
        return f"Помічник мовчить з {label} {PAUSE_HOURS:g} год"
    if action == "resume":
        set_pause(uid, False)
        swap_buttons(message, remove=("pause", "resume"), add=[btn("⏸ Відповім сама", "pause", uid)])
        return "Помічник знову відповідає"
    return ""


def handle_telegram(update):
    try:
        cq = update.get("callback_query")
        if cq:
            chat = str(((cq.get("message") or {}).get("chat") or {}).get("id"))
            if chat != str(TG_CHAT):
                tg("answerCallbackQuery", callback_query_id=cq["id"], text="Немає доступу")
                return
            action, _, uid = (cq.get("data") or "").partition(":")
            note = on_button(action, uid, cq.get("message") or {})
            tg("answerCallbackQuery", callback_query_id=cq["id"], text=note[:190])
            return
        m = update.get("message") or {}
        if str((m.get("chat") or {}).get("id")) != str(TG_CHAT):
            return  # only the specialist controls the bot
        text = (m.get("text") or "").strip()
        reply_to = (m.get("reply_to_message") or {}).get("message_id")
        uid = kv_get(f"tgmap_{reply_to}") if reply_to else None
        if uid and text:
            if send_instagram(uid, text):
                save(uid, "assistant", text)
                tg("setMessageReaction", chat_id=TG_CHAT, message_id=m["message_id"],
                   reaction=[{"type": "emoji", "emoji": "👍"}])
            else:
                tg_to_master(WINDOW_HINT, uid)
        elif text.startswith("/"):
            tg_to_master(TG_HELP)
        elif text:
            tg_to_master("Щоб написати клієнту, відповідайте (свайп вліво) на повідомлення про нього "
                         "або натисніть ✍️ Відповісти.")
    except Exception as e:
        print(f"Telegram handling error: {e}", flush=True)


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


def sender_action(recipient_id, action):
    """'mark_seen' / 'typing_on' in the client's Instagram. Nice to have; failures are ignored."""
    token = ig_token()
    if not token:
        return
    try:
        r = requests.post(GRAPH_URL, headers={"Authorization": f"Bearer {token}"},
                          json={"recipient": {"id": recipient_id}, "sender_action": action}, timeout=10)
        if not r.ok:
            print(f"Sender action {action} failed {r.status_code}: {r.text[:200]}", flush=True)
    except requests.RequestException:
        pass


def send_instagram(recipient_id, text):
    token = ig_token()
    if not token:
        print("No IG_ACCESS_TOKEN set; not sending:", text)
        return False
    try:
        r = requests.post(GRAPH_URL,
                          headers={"Authorization": f"Bearer {token}"},
                          json={"recipient": {"id": recipient_id}, "message": {"text": text}},
                          timeout=15)
    except requests.RequestException as e:
        print(f"Instagram send error: {e}", flush=True)
        return False
    if r.ok:
        try:
            kv_set(f"sent_{r.json().get('message_id')}", 1)   # so its echo isn't mistaken for the specialist
        except ValueError:
            pass
    else:
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


@app.route("/telegram/setup")
def telegram_setup():
    """Open once: tells Telegram to send button taps and replies to this server."""
    if request.args.get("key") != TEST_KEY:
        abort(403)
    if not TG_TOKEN:
        return jsonify(error="TELEGRAM_BOT_TOKEN is not set on Render"), 400
    url = "https://" + request.host + "/telegram/webhook"
    result = tg("setWebhook", url=url, secret_token=TG_SECRET,
                allowed_updates=["message", "callback_query"], drop_pending_updates=True)
    info = (tg("getWebhookInfo") or {}).get("result", {})
    if result.get("ok") and TG_CHAT:
        tg_to_master(TG_HELP)
    return jsonify(telegram_says=result, webhook_url=info.get("url"), last_error=info.get("last_error_message"))


@app.route("/telegram/webhook", methods=["POST"])
def telegram_webhook():
    if not TG_SECRET or not hmac.compare_digest(
            request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), TG_SECRET):
        abort(403)
    update = request.get_json(silent=True) or {}
    threading.Thread(target=handle_telegram, args=(update,), daemon=True).start()
    return "ok", 200


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
            if msg.get("is_deleted"):
                continue
            if already_handled(msg.get("mid")):
                continue
            if msg.get("is_echo"):   # a message the account sent: ours, or the specialist typed it herself
                client_id = (event.get("recipient") or {}).get("id")
                if client_id:
                    threading.Thread(target=manual_reply_check, daemon=True,
                                     args=(client_id, msg.get("mid"), (msg.get("text") or "").strip())).start()
                continue
            sender = (event.get("sender") or {}).get("id")
            if not sender:
                continue
            text = (msg.get("text") or "").strip()
            if not text and msg.get("attachments"):
                text = "[Клієнт надіслав фото/вкладення]"
            if not text:
                continue
            received_at = time.time()
            my_id = str(msg.get("mid") or received_at)
            save(sender, "user", text)
            kv_set(f"latest_{sender}", my_id)
            threading.Thread(target=answer_later, args=(sender, my_id, received_at, text), daemon=True).start()
    return "ok", 200  # always 200 quickly, or Meta retries


if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 5000)))
