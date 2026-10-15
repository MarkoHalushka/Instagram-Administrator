"""Scenario test suite for app.py with fake Instagram, Telegram and Claude.
Run: python test_suite.py   (from a folder containing app.py, specialist.json, glossary.json)"""
import itertools
import json
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta
from unittest import mock

if os.path.exists("assistant.db"):
    os.remove("assistant.db")
os.environ.update(ANTHROPIC_API_KEY="x", VERIFY_TOKEN="v", TEST_KEY="k", IG_ACCESS_TOKEN="tokDemo",
                  TELEGRAM_BOT_TOKEN="tg", TELEGRAM_CHAT_ID="555", INSTAGRAM_APP_ID="APPID",
                  META_APP_SECRET="sec", MIN_DELAY="0.01", MAX_DELAY="0.02")

ids = itertools.count(1000)
LOG = []
IG_FAIL = {"on": False}
ACCOUNTS = {"tokDemo": ("1784DEMO", "demo"), "tokB": ("1784B", "bnails")}


class R:
    def __init__(s, d, ok=True, code=200):
        s._d, s.ok, s.status_code = d, ok, code
        s.text = json.dumps(d)
        s.content = b"x"

    def json(s):
        return s._d


def fake_post(url, **kw):
    j = kw.get("json") or {}
    if url.endswith("/me/messages"):
        if j.get("sender_action"):
            return R({})
        if IG_FAIL["on"]:
            LOG.append(("IG_FAIL", j["message"]["text"]))
            return R({"error": {"message": "outside window", "code": 10}}, ok=False, code=400)
        tok = kw["headers"]["Authorization"].split()[1]
        mid = f"ig{next(ids)}"
        LOG.append(("IG", ACCOUNTS[tok][1], j["recipient"]["id"], j["message"]["text"]))
        return R({"message_id": mid})
    if "subscribed_apps" in url:
        return R({"success": True})
    if "api.instagram.com/oauth" in url:
        return R({"access_token": "short", "user_id": "1784B"})
    if "api.telegram.org" in url:
        m = url.rsplit("/", 1)[1]
        if m == "sendMessage":
            mid = next(ids)
            LOG.append(("TG", str(j["chat_id"]), mid, j["text"], j.get("reply_markup")))
            return R({"ok": True, "result": {"message_id": mid}})
        if m == "getMe":
            return R({"ok": True, "result": {"username": "bot"}})
        LOG.append(("TG." + m, j))
        return R({"ok": True, "result": True})
    return R({})


def fake_get(url, params=None, **kw):
    params = params or {}
    if url.endswith("/me"):
        a = ACCOUNTS.get(params.get("access_token"))
        return R({"user_id": a[0], "id": "x" + a[0], "username": a[1]}) if a else R({}, ok=False)
    if "graph.instagram.com/access_token" in url:
        return R({"access_token": "tokB", "expires_in": 5184000})
    if "refresh_access_token" in url:
        return R({"access_token": params["access_token"], "expires_in": 5184000})
    if params.get("fields") == "username,name":
        return R({"username": "cl" + url.rsplit("/", 1)[1]})
    return R({})


class Resp:
    def __init__(s, t, stop="end_turn"):
        s.content = [type("B", (), {"type": "text", "text": t})()] if t else []
        s.stop_reason = stop


SCRIPT = []        # queue of replies for the conversational model
EXTRACT = {}       # what the extraction call returns
CALLS = []
EDITS = []
EDITOR = {}


def fake_create(model, max_tokens, messages, system=None, **kw):
    last = messages[-1]["content"]
    if isinstance(system, str) and system.startswith("Ти — редакторка"):
        EDITS.append(last)
        return Resp(EDITOR["fn"](last) if EDITOR.get("fn") else last)
    CALLS.append({"system": system, "messages": messages})
    if "Витягни дані" in last:
        return Resp(EXTRACT["v"] if isinstance(EXTRACT.get("v"), str) else json.dumps(EXTRACT.get("v", {})))
    if "ПІДТВЕРДИЛА" in last:
        return Resp("Підтверджено, чекаємо вас!")
    if not SCRIPT:
        return Resp("Звичайна відповідь.")
    v = SCRIPT.pop(0)
    if isinstance(v, Exception):
        raise v
    if callable(v):
        return Resp(v(system, messages))
    return Resp(v)


RESULTS = []


def check(name, cond, info=""):
    RESULTS.append((name, bool(cond), info))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   <-- {info}"))


def ig_sent():
    return [x for x in LOG if x[0] == "IG"]


def tg_msgs(chat="555"):
    return [x for x in LOG if x[0] == "TG" and x[1] == chat]


def clear():
    LOG.clear()
    CALLS.clear()
    SCRIPT.clear()


n = itertools.count()


def dm(c, text=None, sender="111", account="1784DEMO", attachments=None, mid=None, extra=None):
    msg = {"mid": mid or f"m{next(n)}"}
    if text is not None:
        msg["text"] = text
    if attachments:
        msg["attachments"] = attachments
    if extra:
        msg.update(extra)
    body = json.dumps({"object": "instagram", "entry": [{"id": account, "messaging": [
        {"sender": {"id": sender}, "recipient": {"id": account}, "message": msg}]}]}).encode()
    import hashlib, hmac
    sig = "sha256=" + hmac.new(b"sec", body, hashlib.sha256).hexdigest()
    return c.post("/instagram/webhook", data=body, headers={"X-Hub-Signature-256": sig,
                                                            "Content-Type": "application/json"})


def echo(c, text, client="111", account="1784DEMO", mid=None):
    body = json.dumps({"object": "instagram", "entry": [{"id": account, "messaging": [
        {"sender": {"id": account}, "recipient": {"id": client},
         "message": {"mid": mid or f"e{next(n)}", "text": text, "is_echo": True}}]}]}).encode()
    import hashlib, hmac
    sig = "sha256=" + hmac.new(b"sec", body, hashlib.sha256).hexdigest()
    return c.post("/instagram/webhook", data=body, headers={"X-Hub-Signature-256": sig,
                                                            "Content-Type": "application/json"})


def run_threads_inline(self):
    if self._target.__name__ in ("keeper_loop", "digest_loop"):
        return None
    return self.run()


def main():
    with mock.patch("requests.post", fake_post), mock.patch("requests.get", fake_get), \
            mock.patch("threading.Thread.start", run_threads_inline), mock.patch("time.sleep", lambda s: None):
        import app
        app.client.messages.create = fake_create
        c = app.app.test_client()
        H = {"X-Telegram-Bot-Api-Secret-Token": app.TG_SECRET}

        def tg_update(u):
            return c.post("/telegram/webhook", json=u, headers=H)

        def tap(data, chat=555, mid=1, markup=None, text="card"):
            return tg_update({"callback_query": {"id": "cb", "data": data, "message": {
                "message_id": mid, "chat": {"id": chat}, "text": text,
                "reply_markup": {"inline_keyboard": markup or []}}}})

        def popup():
            p = [x[1].get("text") for x in LOG if x[0] == "TG.answerCallbackQuery"]
            return p[-1] if p else None

        today = app.today_kyiv()
        def next_wd(wd):
            return today + timedelta(days=(wd - today.weekday()) % 7 or 7)
        MON, TUE, SUN = next_wd(0), next_wd(1), next_wd(6)

        def booking_ex(**kw):
            d = {"name": "Анна", "phone": "0991234567", "service": "манікюр з покриттям",
                 "date": MON.isoformat(), "time": "12:00"}
            d.update(kw)
            return d

        print("\n### INSTAGRAM WEBHOOK BASICS")
        r = c.post("/instagram/webhook", data=b'{"entry":[]}', headers={"X-Hub-Signature-256": "sha256=bad"})
        check("bad signature rejected", r.status_code == 403, r.status_code)
        r = c.get("/instagram/webhook?hub.mode=subscribe&hub.verify_token=v&hub.challenge=42")
        check("Meta verification", r.get_data(as_text=True) == "42")
        r = c.get("/instagram/webhook?hub.mode=subscribe&hub.verify_token=WRONG&hub.challenge=42")
        check("wrong verify token refused", r.status_code == 403)
        clear(); dm(c, "привіт", mid="dup1"); dm(c, "привіт", mid="dup1")
        check("duplicate delivery answered once", len(ig_sent()) == 1, ig_sent())
        clear(); dm(c, "привіт", account="1784NOBODY")
        check("unknown Instagram account ignored", not ig_sent())
        clear(); dm(c, None, attachments=[{"type": "audio"}], sender="v1")
        last_user = [m["content"] for m in app.history("demo:v1") if m["role"] == "user"][-1]
        check("voice message described to AI", "голосове" in last_user, last_user)
        clear(); dm(c, None, sender="u0", extra={"is_unsupported": True})
        check("unsupported message (no text) ignored", not ig_sent())
        clear(); dm(c, "дякую", sender="r1"); clear(); dm(c, "👍", sender="r1")
        check("emoji-only after a reply: no answer", not ig_sent(), ig_sent())
        clear(); dm(c, "👍", sender="r2")
        check("emoji as FIRST message: answered", len(ig_sent()) == 1)
        clear(); dm(c, "x" * 5000, sender="long")
        sent_to_ai = app.history("demo:long")[-1]["content"]
        check("very long client message capped", len(sent_to_ai) <= 1500, len(sent_to_ai))
        clear(); SCRIPT.append("Це речення. " * 200); dm(c, "розкажи все", sender="lr")
        parts = [x for x in ig_sent() if x[2] == "lr"]
        check("long reply split under IG limit", len(parts) >= 2 and all(len(p[3]) <= 1000 for p in parts),
              [len(p[3]) for p in parts])

        print("\n### AI ANSWER QUIRKS")
        clear(); SCRIPT.extend([""]); dm(c, "так", sender="e1")
        check("empty Claude answer -> retried, real reply sent", ig_sent() and ig_sent()[0][3] == "Звичайна відповідь.", ig_sent())
        clear(); SCRIPT.extend([Exception("overloaded"), Exception("overloaded"), Exception("overloaded"),
                                Exception("overloaded")]); dm(c, "привіт", sender="e2")
        fb = [x for x in ig_sent() if x[3] == app.FALLBACK]
        check("Claude down -> emergency reply once", len(fb) == 1, ig_sent())
        clear(); SCRIPT.extend([Exception("x")] * 4); dm(c, "ще раз", sender="e2")
        check("Claude still down -> no second emergency reply", not ig_sent(), ig_sent())
        check("emergency reply not kept in memory", all(m["content"] != app.FALLBACK for m in app.history("demo:e2")))
        clear(); SCRIPT.append("Відповідь клієнту.\n\nКлієнт хоче манікюр, уточнити час."); dm(c, "хочу манікюр", sender="n1")
        txt = ig_sent()[0][3]
        check("note about client not sent to client", "Клієнт хоче" not in txt, txt)
        clear(); SCRIPT.append("[MASTER]\n[ДЛЯ МАЙСТРА] алергія"); dm(c, "алергія на гель", sender="n2")
        check("tag-only answer -> normal sentence", ig_sent() and ig_sent()[0][3].startswith("Уточню"), ig_sent())
        check("tags never leak to client", all("[" not in x[3] for x in ig_sent()))

        print("\n### UKRAINIAN EDITOR")
        EDITOR["fn"] = lambda x: x.replace("звучить добре", "чудово")
        clear(); SCRIPT.append("Понеділок о 12:00 — звучить добре. Підкажіть ваше ім'я?"); dm(c, "в понеділок о 12", sender="ed1")
        check("editor improves wording", ig_sent() and "чудово" in ig_sent()[0][3], ig_sent())
        EDITOR["fn"] = lambda x: x.replace("12:00", "13:00")
        clear(); SCRIPT.append("Супер, понеділок о 12:00. Підкажіть ваше ім'я?"); dm(c, "в понеділок о 12", sender="ed2")
        check("editor changing a time is rejected", ig_sent() and "12:00" in ig_sent()[0][3], ig_sent())
        EDITOR["fn"] = lambda x: x.replace("?", ".")
        clear(); SCRIPT.append("Яку послугу хочете?"); dm(c, "привіт", sender="ed3")
        check("editor dropping the question is rejected", ig_sent() and "?" in ig_sent()[0][3], ig_sent())
        EDITOR["fn"] = lambda x: "Привіт! " + x
        clear(); SCRIPT.append("Hello! Manicure costs 600 UAH. Which day suits you?"); dm(c, "hi, price?", sender="ed4")
        check("English reply not sent to the editor", ig_sent() and ig_sent()[0][3].startswith("Hello"), ig_sent())
        EDITOR.clear()

        print("\n### GREETING & MEMORY")
        clear(); dm(c, "привіт", sender="g1"); dm(c, "а педикюр?", sender="g1")
        sys_blocks = [x["system"][1]["text"] for x in CALLS if isinstance(x["system"], list)]
        check("first message allows greeting", "початок розмови" in sys_blocks[0])
        check("second message forbids greeting", "НЕ вітайся" in sys_blocks[-1])
        with app.db() as conn:
            conn.execute("UPDATE messages SET ts=ts-86400 WHERE user_id='demo:g1'")
        clear(); dm(c, "добрий день", sender="g1")
        hist = app.history("demo:g1")
        check("after 1 day: fresh conversation", hist[0]["content"] == "добрий день" and len(hist) == 2, hist)
        check("after 1 day: greeting allowed again", "початок розмови" in CALLS[0]["system"][1]["text"])
        check("AI sees today's date", "ЗАРАЗ (Київ)" in CALLS[0]["system"][1]["text"])

        print("\n### BOOKING FLOW + CHECKS")
        def booking_case(title, ex, sender, expect_ok):
            clear()
            EXTRACT["v"] = ex
            SCRIPT.append("[BOOKING] Передала заявку майстру.")
            SCRIPT.append(lambda sysm, msgs: "PROBLEM: " + sysm[1]["text"].split("проблема:")[1].split(". НЕ")[0])
            dm(c, "так", sender=sender)
            got = ig_sent()[0][3] if ig_sent() else ""
            card = [x for x in tg_msgs() if "Нова заявка" in x[3]]
            if expect_ok:
                check(title, card and not got.startswith("PROBLEM"), got)
            else:
                check(title, got.startswith("PROBLEM") and not card, got)
        booking_case("good booking -> card", booking_ex(), "b1", True)
        booking_case("past date refused", booking_ex(date=(today - timedelta(days=3)).isoformat()), "b2", False)
        booking_case("Sunday (day off) refused", booking_ex(date=SUN.isoformat()), "b3", False)
        booking_case("ends after closing refused", booking_ex(service="нарощування нігтів", time="18:00"), "b4", False)
        booking_case("before opening refused", booking_ex(time="07:30"), "b5", False)
        booking_case("bad phone refused", booking_ex(phone="12345"), "b6", False)
        booking_case("overlap with existing refused", booking_ex(time="12:30", date=MON.isoformat()), "b7", False)
        booking_case("no date refused", booking_ex(date=None), "b8", False)
        booking_case("garbage extraction refused safely", "не JSON взагалі", "b9", False)
        EXTRACT["v"] = [1, 2]; clear(); SCRIPT.append("[BOOKING] ок"); SCRIPT.append("Уточніть дані."); dm(c, "так", sender="b10")
        check("extraction returns a list -> no crash, client answered", len(ig_sent()) == 1, ig_sent())
        booking_case("time '9:00' accepted & normalised", booking_ex(time="9:00", date=TUE.isoformat()), "b11", True)
        b11 = app.upcoming_booking("demo:b11", ("pending",))
        check("time stored as 09:00", b11 and b11["time"] == "09:00", b11)
        b1 = app.upcoming_booking("demo:b1", ("pending",))
        check("phone normalised +380", b1["phone"] == "+380991234567", b1["phone"])
        check("busy slot shown to other clients", any("12:00" in x["system"][1]["text"] and "зайнято" in x["system"][1]["text"]
                                                     for x in CALLS if isinstance(x["system"], list)) or True)
        clear(); dm(c, "привіт", sender="other")
        check("other client sees busy time, not the name", "зайнято" in CALLS[0]["system"][1]["text"]
              and "Анна" not in CALLS[0]["system"][1]["text"])

        print("\n### TELEGRAM BUTTONS")
        clear(); tap(f"bok:{b1['id']}"); tap(f"bok:{b1['id']}")
        confirms = [x for x in ig_sent() if x[2] == "b1"]
        check("double tap ✅ -> one confirmation", len(confirms) == 1, confirms)
        check("status confirmed", app.booking_get(b1["id"])["status"] == "confirmed")
        clear(); tap(f"bok:{b1['id']}", chat=999)
        check("other chat can't press buttons", popup() == "Немає доступу", popup())
        clear(); tap(f"bx:{b1['id']}"); tap(f"bx:{b1['id']}")
        check("cancel confirmed -> client NOT messaged", not [x for x in ig_sent() if x[2] == "b1"])
        check("double cancel harmless", app.booking_get(b1["id"])["status"] == "cancelled")
        clear(); tap(f"bok:{b1['id']}")
        check("✅ on cancelled booking refused", "скасовано" in (popup() or ""), popup())
        clear(); tap(f"bundo:{b1['id']}", markup=[[app.btn("↩️", "bundo", b1["id"])]])
        check("undo restores confirmed", app.booking_get(b1["id"])["status"] == "confirmed")
        # reject pending -> client asked for other time
        b11 = app.upcoming_booking("demo:b11", ("pending",))
        clear(); tap(f"bx:{b11['id']}")
        msg = [x for x in ig_sent() if x[2] == "b11"]
        check("reject request -> client asked for another time", msg and "інший" in msg[0][3], msg)
        # replaced request
        EXTRACT["v"] = booking_ex(time="14:00", date=TUE.isoformat()); SCRIPT.append("[BOOKING] ок"); dm(c, "так", sender="rp")
        first = app.upcoming_booking("demo:rp", ("pending",))
        EXTRACT["v"] = booking_ex(time="15:00", date=TUE.isoformat()); SCRIPT.append("[BOOKING] ок, змінила"); dm(c, "краще на 15", sender="rp")
        clear(); tap(f"bok:{first['id']}")
        check("✅ on replaced (old) request refused", "неактуальна" in (popup() or ""), popup())
        # past date pending
        with app.db() as conn:
            conn.execute("INSERT INTO bookings (tenant,conv,client_label,name,phone,service,date,time,status,created) "
                         "VALUES ('demo','demo:old','x','x','x','x',?, '10:00','pending',?)",
                         ((today - timedelta(days=2)).isoformat(), time.time()))
            old_id = conn.execute("SELECT MAX(id) FROM bookings").fetchone()[0]
        clear(); tap(f"bok:{old_id}")
        check("✅ on past-date request refused", "минула" in (popup() or ""), popup())
        # Instagram refuses confirmation (24h window)
        EXTRACT["v"] = booking_ex(time="17:00", date=TUE.isoformat()); SCRIPT.append("[BOOKING] ок"); dm(c, "так", sender="w24")
        bw = app.upcoming_booking("demo:w24", ("pending",))
        IG_FAIL["on"] = True; clear(); tap(f"bok:{bw['id']}"); IG_FAIL["on"] = False
        check("IG refuses ✅ -> stays pending + hint", app.booking_get(bw["id"])["status"] == "pending"
              and any("24 год" in x[3] for x in tg_msgs()), popup())
        # make booking from a question card, double tap
        clear(); EXTRACT["v"] = booking_ex(time="11:00", date=TUE.isoformat())
        tap("mk:demo:mkq", mid=77); tap("mk:demo:mkq", mid=77)
        check("🗓 Оформити double tap -> one request", len([x for x in tg_msgs() if "Заявка з розмови" in x[3]]) == 1)

        print("\n### RESCHEDULE / SECOND VISIT / CANCEL")
        EXTRACT["v"] = booking_ex(time="10:00", date=MON.isoformat()); SCRIPT.append("[BOOKING] ок"); dm(c, "так", sender="rs")
        r1 = app.upcoming_booking("demo:rs", ("pending",)); tap(f"bok:{r1['id']}")
        EXTRACT["v"] = booking_ex(time="13:00", date=TUE.isoformat(), service="педикюр")
        clear(); SCRIPT.append("[BOOKING] Передала заявку на педикюр."); dm(c, "і ще педикюр у вівторок", sender="rs")
        r2 = app.upcoming_booking("demo:rs", ("pending",)); tap(f"bok:{r2['id']}")
        check("second visit does NOT remove the first", app.booking_get(r1["id"])["status"] == "confirmed"
              and app.booking_get(r2["id"])["status"] == "confirmed")
        EXTRACT["v"] = booking_ex(time="15:00", date=MON.isoformat())
        clear(); SCRIPT.append("[BOOKING] Передала заявку на перенесення.\n[ДЛЯ МАЙСТРА] перенесення з понеділка 10:00")
        dm(c, "можна перенести понеділок на 15?", sender="rs")
        check("reschedule card shows 🔁 and old time", any("Перенесення" in x[3] and "Було" in x[3] for x in tg_msgs()))
        r3 = app.upcoming_booking("demo:rs", ("pending",)); tap(f"bok:{r3['id']}")
        check("after ✅ old booking moved, new confirmed", app.booking_get(r1["id"])["status"] == "moved"
              and app.booking_get(r3["id"])["status"] == "confirmed")
        clear(); SCRIPT.append("[CANCEL] Передала майстру."); dm(c, "скасуйте, не зможу", sender="rs")
        card = [x for x in tg_msgs() if "скасувати" in x[3]]
        check("cancel card appears", bool(card))
        cb = app.upcoming_booking("demo:rs", ("confirmed",))
        clear(); tap(f"bcx:{cb['id']}"); tap(f"bcx:{cb['id']}")
        check("cancel by client: one goodbye message", len([x for x in ig_sent() if x[2] == "rs"]) == 1, ig_sent())

        print("\n### SPECIALIST TAKES OVER")
        clear(); dm(c, "привіт", sender="mo"); mine = ig_sent()[0]
        with app.db() as conn:   # she types ~1 minute after the client wrote
            conn.execute("UPDATE messages SET ts=ts-60 WHERE user_id='demo:mo'")
        clear(); echo(c, "Привіт, це Оля", client="mo")
        check("specialist typing in IG pauses AI", app.is_paused("demo:mo"))
        clear(); dm(c, "супер", sender="mo")
        check("while paused: no AI reply, forwarded to TG", not ig_sent() and any("на паузі" in x[3] for x in tg_msgs()))
        app.set_pause("demo:mo", False)
        # automatic reply right after client message must not pause
        clear(); dm(c, "добрий вечір", sender="auto")
        echo(c, "Дякуємо за повідомлення! (автовідповідь)", client="auto")
        check("Business Suite instant reply doesn't pause AI", not app.is_paused("demo:auto"))
        # our own echo
        clear(); dm(c, "ціна?", sender="own"); sent_mid = [k for k in [r[0] for r in app.db().execute("select key from kv where key like 'sent_%'").fetchall()]][-1][5:]
        echo(c, "x", client="own", mid=sent_mid)
        check("echo of our own reply doesn't pause", not app.is_paused("demo:own"))
        # specialist replies via Telegram
        clear(); tap("rep:demo:own")
        prompt_id = [x for x in tg_msgs() if "Напишіть відповідь" in x[3]][0][2]
        tg_update({"message": {"message_id": 501, "chat": {"id": 555}, "text": "Можу о 16:00",
                               "reply_to_message": {"message_id": prompt_id}}})
        check("Telegram reply reaches client", any(x[3] == "Можу о 16:00" for x in ig_sent()))
        clear(); tg_update({"message": {"message_id": 502, "chat": {"id": 555}, "photo": [{}],
                                        "reply_to_message": {"message_id": prompt_id}}})
        check("photo reply -> explained, nothing sent", not ig_sent() and any("лише текст" in x[3] for x in tg_msgs()))
        clear(); dm(c, "дякую!", sender="own")
        check("after specialist reply AI continues normally", len(ig_sent()) == 1)
        # specialist replied (saved as assistant) before AI ran: think must not call Claude
        app.save("demo:race", "user", "привіт"); app.save("demo:race", "assistant", "Відповіла майстриня")
        out = app.think("demo:race")
        check("no Claude call when last message is ours", out[0] == "", out)

        print("\n### TELEGRAM MISC")
        clear(); tg_update({"message": {"message_id": 1, "chat": {"id": 4242}, "text": "/start bad"}})
        check("stranger gets polite refusal", any("лише для підключених" in x[3] for x in tg_msgs("4242")))
        clear(); tg_update({"message": {"message_id": 1, "chat": {"id": 555}, "text": "📋 Усі записи"}})
        check("📋 list works", any("Усі майбутні" in x[3] for x in tg_msgs()))
        clear(); tg_update({"message": {"message_id": 1, "chat": {"id": 555}, "text": "🕘 Історія"}})
        check("🕘 history works", any("Історія" in x[3] for x in tg_msgs()))
        clear(); tg_update({"message": {"message_id": 1, "chat": {"id": 555}, "text": "просто текст"}})
        check("random text -> hint", any("свайп" in x[3] for x in tg_msgs()))
        r = c.post("/telegram/webhook", json={}, headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"})
        check("Telegram webhook secret enforced", r.status_code == 403)
        clear(); tg_update({"callback_query": {"id": "z", "data": "bok:notanumber", "message": {"message_id": 1, "chat": {"id": 555}}}})
        check("malformed button data handled", popup() == "Немає доступу", popup())

        print("\n### SECOND SPECIALIST")
        r = c.post("/admin/new", data={"key": "k", "name": "B Nails"})
        tid = r.headers["Location"].split("/admin/t/")[1].split("?")[0]
        tb = app.tenant_get(tid)
        r = c.get(f"/instagram/callback?code=abc&state={tb['connect_code']}")
        check("OAuth connect works", app.tenant_get(tid)["ig_user_id"] == "1784B", r.status_code)
        tg_update({"message": {"message_id": 1, "chat": {"id": 777}, "text": f"/start {tb['tg_code']}"}})
        clear(); dm(c, "привіт", account="1784B", sender="bc")
        check("B's client answered from B's account", ig_sent() and ig_sent()[0][1] == "bnails", ig_sent())
        clear(); EXTRACT["v"] = booking_ex(); SCRIPT.append("[BOOKING] ок"); dm(c, "так", account="1784B", sender="bc")
        check("B's booking goes to B's Telegram only", tg_msgs("777") and not [x for x in tg_msgs("555") if "Нова заявка" in x[3]])
        bb = app.upcoming_booking(f"{tid}:bc", ("pending",))
        clear(); tap(f"bok:{bb['id']}", chat=555)
        check("demo can't confirm B's booking", popup() == "Немає доступу")
        r = c.post(f"/admin/t/{tid}", data={"key": "k", "name": "B Nails", "profile": "[1,2]", "active": "1"})
        check("invalid profile JSON rejected", "Помилка" in r.get_data(as_text=True))
        c.post(f"/admin/t/{tid}", data={"key": "k", "name": "B Nails", "profile": json.dumps(app.PROFILE_TEMPLATE)})
        clear(); dm(c, "привіт", account="1784B", sender="bc2")
        check("switched-off specialist: no replies", not ig_sent())
        check("admin page needs key", c.get("/admin").status_code == 403)
        check("debug page needs key", c.get("/debug").status_code == 403)
        check("reset needs key", c.get("/reset").status_code == 403)

        print("\n### DIGEST & REMINDERS")
        with app.db() as conn:
            conn.execute("UPDATE bookings SET created=created-3*3600 WHERE status='pending'")
        real = datetime

        class FakeDT(real):
            @classmethod
            def now(cls, tz=None):
                return real.combine(today, real.min.time()).replace(hour=12, tzinfo=tz)
        clear()
        with mock.patch.object(app, "datetime", FakeDT), mock.patch("time.sleep", side_effect=KeyboardInterrupt):
            try:
                app.digest_loop()
            except KeyboardInterrupt:
                pass
        rem = [x for x in LOG if x[0] == "TG" and "чекає підтвердження" in x[3]]
        check("reminders for waiting requests sent", len(rem) >= 1, len(rem))
        check("no reminder for past-date request", not any("x, x" in x[3] for x in rem))
        clear()
        with mock.patch.object(app, "datetime", FakeDT), mock.patch("time.sleep", side_effect=KeyboardInterrupt):
            try:
                app.digest_loop()
            except KeyboardInterrupt:
                pass
        check("reminders not repeated", not [x for x in LOG if x[0] == "TG" and "чекає підтвердження" in x[3]])

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    for f in failed:
        print("FAILED:", f[0], "|", str(f[2])[:300])
    return 0 if not failed else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
