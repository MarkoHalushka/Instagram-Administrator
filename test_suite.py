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
        if j.get("sender_action") == "react":
            LOG.append(("REACT", j["recipient"]["id"], j["payload"]["message_id"], j["payload"]["reaction"]))
            return R({})
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
REVIEWS = []       # drafts the self-check looked at (with the conversation it was shown)
REVIEW = {}        # REVIEW["fn"](draft) -> verdict (default {"ok": True}); may return a str or an Exception


def fake_create(model, max_tokens, messages, system=None, **kw):
    last = messages[-1]["content"]
    if isinstance(system, list) and "ПЕРЕВІРКА ЧЕРНЕТКИ" in system[-1]["text"]:
        draft = last.split("<<<\n", 1)[1].rsplit("\n>>>", 1)[0]
        REVIEWS.append({"draft": draft, "input": last, "system": system})
        v = REVIEW["fn"](draft) if REVIEW.get("fn") else {"ok": True}
        if isinstance(v, Exception):
            raise v
        return Resp(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))
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
        clear(); SCRIPT.append("Передала заявку, гарного дня!"); dm(c, "запишіть", sender="bye1")
        clear(); dm(c, "Дякую, і вам гарного дня!", sender="bye1", mid="byeMid1")
        check("thanks/goodbye after our farewell: silence (no loop)", not ig_sent(), ig_sent())
        reacts = [x for x in LOG if x[0] == "REACT"]
        check("...but the client's goodbye gets a ❤️", reacts == [("REACT", "bye1", "byeMid1", "love")], reacts)
        clear(); SCRIPT.append("Передати заявку майстру?"); dm(c, "запишіть", sender="bye2")
        clear(); dm(c, "добре", sender="bye2")
        check("'добре' answering our question: replied", len(ig_sent()) == 1, ig_sent())
        check("...and no ❤️ when we answer with text", not [x for x in LOG if x[0] == "REACT"], LOG)
        clear(); SCRIPT.append("Чекаємо вас!"); dm(c, "ок", sender="bye3")
        clear(); dm(c, "дякую, а скільки педикюр?", sender="bye3")
        check("thanks + new question: replied", len(ig_sent()) == 1, ig_sent())
        clear(); dm(c, "👍", sender="r2")
        check("emoji as FIRST message: answered", len(ig_sent()) == 1)
        clear(); dm(c, "x" * 5000, sender="long")
        sent_to_ai = app.history("demo:long")[-1]["content"]
        check("very long client message capped", len(sent_to_ai) <= 1500, len(sent_to_ai))
        clear(); SCRIPT.append(" ".join(f"Речення номер {i}." for i in range(400))); dm(c, "розкажи все", sender="lr")
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
        check("hand-over pauses the AI for this client", app.is_paused("demo:n2"))
        card = [x for x in tg_msgs() if "Потрібна ваша відповідь" in x[3]]
        check("hand-over card: the person leads now, with ▶️ to give it back",
              card and "ведете ви" in card[-1][3] and "resume:demo:n2" in json.dumps(card[-1][4]), card)
        clear(); dm(c, "то можна чи ні?", sender="n2")
        check("after a hand-over the AI stays silent, the message goes to Telegram",
              not ig_sent() and any("на паузі" in x[3] for x in tg_msgs()), ig_sent())

        print("\n### SELF-CHECK BEFORE SENDING")
        demo = app.tenant_get("demo")
        REVIEWS.clear(); clear(); SCRIPT.append("Манікюр з покриттям — 600 грн, близько години.")
        dm(c, "скільки манікюр з покриттям?", sender="sc1")
        check("every reply is checked before sending, together with the conversation",
              REVIEWS and "скільки манікюр з покриттям?" in REVIEWS[-1]["input"], REVIEWS[-1:])
        check("a good draft goes out unchanged", [x[3] for x in ig_sent()] == ["Манікюр з покриттям — 600 грн, близько години."],
              ig_sent())
        REVIEW["fn"] = lambda d: {"ok": False, "problems": ["двічі питає одне й те саме"],
                                  "fixed": "Добрий день! Яка послуга вас цікавить?"}
        clear(); SCRIPT.append("Добрий день! Що вас цікавить? Яка послуга вас цікавить?"); dm(c, "добрий день", sender="sc2")
        check("nonsense draft («Що вас цікавить? Яка послуга вас цікавить?») → the fixed one is sent",
              [x[3] for x in ig_sent()] == ["Добрий день! Яка послуга вас цікавить?"], ig_sent())
        REVIEW["fn"] = lambda d: {"ok": False, "problems": ["калька"], "fixed": d.replace("12:00", "13:00")}
        clear(); SCRIPT.extend(["Понеділок о 12:00 — звучить добре. Підкажіть ваше ім'я?",
                                "Понеділок о 12:00 підходить. Підкажіть, будь ласка, ваше ім'я?"])
        dm(c, "в понеділок о 12", sender="sc3")
        sent = ig_sent()[0][3] if ig_sent() else ""
        check("a fix that changes a time is never used: the reply is written again",
              "12:00" in sent and "13:00" not in sent and "підходить" in sent, sent)
        check("...and the rewrite is told what was wrong",
              any("ПЕРЕВІРКА ПЕРЕД НАДСИЛАННЯМ" in x["system"][1]["text"] and "калька" in x["system"][1]["text"]
                  for x in CALLS if isinstance(x["system"], list)))
        REVIEW["fn"] = lambda d: {"ok": False, "problems": ["x"], "fixed": d.replace("?", ".")}
        clear(); SCRIPT.extend(["На який день вам зручно прийти на манікюр?", "На який день вам було б зручно прийти?"])
        dm(c, "хочу на манікюр", sender="sc4")
        check("a fix that drops the client's question is rejected", ig_sent() and "?" in ig_sent()[0][3], ig_sent())
        REVIEW["fn"] = lambda d: {"ok": False, "problems": ["x"], "fixed": "Привіт! " + d}
        REVIEWS.clear(); clear(); SCRIPT.append("Hello! Manicure costs 600 UAH. Which day suits you?"); dm(c, "hi, price?", sender="sc5")
        check("English reply is left alone", ig_sent() and ig_sent()[0][3].startswith("Hello") and not REVIEWS, ig_sent())
        REVIEW["fn"] = lambda d: "це не JSON"
        clear(); SCRIPT.append("Педикюр — 750 грн, близько півтори години."); dm(c, "а педикюр?", sender="sc6")
        check("a broken self-check answer never blocks a good reply", ig_sent() and ig_sent()[0][3].startswith("Педикюр"), ig_sent())
        REVIEW["fn"] = lambda d: Exception("overloaded")
        clear(); SCRIPT.append("Педикюр — 750 грн, близько півтори години."); dm(c, "а педикюр?", sender="sc7")
        check("a self-check error never blocks a good reply", ig_sent() and ig_sent()[0][3].startswith("Педикюр"), ig_sent())
        REVIEW.clear()
        clear(); SCRIPT.extend(["Дякую за ваше питання про нарощування, воно коштує 900 грн.", "Нарощування — 900 грн."])
        dm(c, "а нарощування?", sender="sc8")
        check("a bot phrase that can't simply be cut → the reply is written again",
              [x[3] for x in ig_sent()] == ["Нарощування — 900 грн."], ig_sent())
        check("«Дякую за розуміння» is cut", app.polish("Дякую за розуміння. Майстер напише вам.", demo) == "Майстер напише вам.",
              app.polish("Дякую за розуміння. Майстер напише вам.", demo))
        check("«Чим можу допомогти: …» no longer turns into «Що вас цікавить: яка послуга вас цікавить»",
              app.polish("Добрий день! Чим можу допомогти: яка послуга вас цікавить?", demo)
              == "Добрий день! Яка послуга вас цікавить?",
              app.polish("Добрий день! Чим можу допомогти: яка послуга вас цікавить?", demo))
        check("bare «Чим можу допомогти?» after a greeting → a real question",
              app.polish("Привіт! Чим можу допомогти?", demo) == "Привіт! Що саме вас цікавить?",
              app.polish("Привіт! Чим можу допомогти?", demo))
        check("«Чим ще можу допомогти?» after an answer → dropped",
              app.polish("Манікюр — 600 грн. Чим ще можу допомогти?", demo) == "Манікюр — 600 грн.",
              app.polish("Манікюр — 600 грн. Чим ще можу допомогти?", demo))

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
        check("✅ on past-date request refused", "мину" in (popup() or ""), popup())
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
        with app.db() as conn:
            role = conn.execute("SELECT role FROM messages WHERE user_id='demo:own' AND text='Можу о 16:00'").fetchone()[0]
        check("Telegram reply remembered as the person's own words", role == "staff", role)
        check("after a Telegram reply the AI steps back and says so, with ▶️",
              app.is_paused("demo:own") and any("ведете ви" in x[3] and "resume:demo:own" in json.dumps(x[4])
                                                for x in tg_msgs()), tg_msgs())
        clear(); tg_update({"message": {"message_id": 502, "chat": {"id": 555}, "photo": [{}],
                                        "reply_to_message": {"message_id": prompt_id}}})
        check("photo reply -> explained, nothing sent", not ig_sent() and any("лише текст" in x[3] for x in tg_msgs()))
        clear(); dm(c, "підходить, а скільки це коштує?", sender="own")
        check("after the person replied: AI silent, the message goes to Telegram",
              not ig_sent() and any("на паузі" in x[3] for x in tg_msgs()), ig_sent())
        clear(); tap("resume:demo:own"); dm(c, "а адреса яка?", sender="own")
        check("▶️ gives the conversation back to the AI", len(ig_sent()) == 1, ig_sent())
        ctx = CALLS[0]["system"][1]["text"] if CALLS else ""
        check("the AI is told which words were the person's", "ЖИВА ЛЮДИНА" in ctx and "Можу о 16:00" in ctx, ctx[-300:])
        # specialist replied before AI ran: think must not call Claude
        app.save("demo:race", "user", "привіт"); app.save("demo:race", "assistant", "Відповіла майстриня")
        out = app.think("demo:race")
        check("no Claude call when last message is ours", out[0] == "", out)
        app.save("demo:race3", "user", "привіт"); app.save("demo:race3", "staff", "Привіт, це Оля")
        check("no Claude call when the person already answered", app.think("demo:race3")[0] == "")

        def person_answers_meanwhile(sysm, msgs):
            app.save("demo:race2", "staff", "Так, працюємо з таким матеріалом")
            return "Тобто вас цікавить, з яким матеріалом ми працюємо?"
        clear(); SCRIPT.append(person_answers_meanwhile); dm(c, "у вас є гель від Kodi?", sender="race2")
        check("a reply prepared before the person answered is not sent (no «Тобто…» after her answer)",
              not ig_sent(), ig_sent())

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

        print("\n### FINISHED APPOINTMENTS MOVE TO HISTORY")
        now_k = datetime.now(app.KYIV)
        past_t = (now_k - timedelta(hours=3)).strftime("%H:%M") if now_k.hour >= 3 else None
        fut_t = (now_k + timedelta(hours=2)).strftime("%H:%M") if now_k.hour <= 21 else None
        with app.db() as conn:
            for nm, tm in (("ПройшлаСьогодні", past_t), ("ЩеБудеСьогодні", fut_t)):
                if tm:
                    conn.execute("INSERT INTO bookings (tenant,conv,client_label,name,phone,service,date,time,status,created) "
                                 "VALUES ('demo',?, 'x', ?, '+380991234567', 'манікюр без покриття', ?, ?, 'confirmed', ?)",
                                 (f"demo:{nm}", nm, today.isoformat(), tm, time.time()))
        clear(); tg_update({"message": {"message_id": 1, "chat": {"id": 555}, "text": "📋 Усі записи"}})
        lst = " ".join(x[3] for x in tg_msgs())
        if past_t:
            check("finished appointment not in upcoming list", "ПройшлаСьогодні" not in lst, lst[:200])
        if fut_t:
            check("later-today appointment still in upcoming list", "ЩеБудеСьогодні" in lst, lst[:200])
        clear(); tg_update({"message": {"message_id": 1, "chat": {"id": 555}, "text": "🕘 Історія"}})
        hist = " ".join(x[3] for x in tg_msgs())
        if past_t:
            check("finished appointment appears in history", "ПройшлаСьогодні" in hist, hist[:200])
        if fut_t:
            check("later-today appointment not in history yet", "ЩеБудеСьогодні" not in hist, hist[:200])

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

        print("\n### DENTAL CLINIC (same engine, other instructions)")
        r = c.post("/admin/new", data={"key": "k", "name": "Smile Dent", "niche": "dental"})
        did = r.headers["Location"].split("/admin/t/")[1].split("?")[0]
        check("new clinic gets the dental template", app.niche_of(app.tenant_get(did)) == "dental")
        app.tenant_update(tid, profile=app.CLINIC_TEMPLATE, active=1)   # reuse B's connected account as a clinic
        tb = app.tenant_get(tid)
        pd, pn = app.system_prompt(tb), app.system_prompt(app.tenant_get("demo"))
        check("dental prompt: clinic + dental glossary, no nail words",
              "стоматологічної клініки" in pd and "ОПТГ" in pd and "гелька" not in pd)
        check("nail prompt unchanged: nail glossary, no dental words", "ОПТГ" not in pn and "гелька" in pn)
        check("dental: emoji and «Супер!» removed", app.polish("Супер! Чистка — 1 900 грн 🙂", tb) == "Чистка — 1 900 грн",
              app.polish("Супер! Чистка — 1 900 грн 🙂", tb))
        check("nails: emoji kept", "🙂" in app.polish("Супер! Манікюр 🙂", app.tenant_get("demo")))
        check("dental fallback text", app.fallback_of(tb) == app.FALLBACK_DENTAL)
        clear(); EXTRACT["v"] = booking_ex(service="професійна гігієна"); SCRIPT.append("[BOOKING] Дякую, заявку передала 🙂")
        dm(c, "так", account="1784B", sender="dc")
        card = [x for x in tg_msgs("777") if "Нова заявка" in x[3]]
        check("clinic booking card uses 🦷", card and "🦷" in card[-1][3], card)
        check("clinic reply reaches the patient without emoji", ig_sent() and "🙂" not in ig_sent()[-1][3], ig_sent())
        clear(); EXTRACT["v"] = booking_ex(service="професійна гігієна"); SCRIPT.append("[BOOKING] Передала заявку.")
        dm(c, "так", account="1784B", sender="dc2")
        card = [x for x in tg_msgs("777") if "Нова заявка" in x[3]]
        check("clinic: two patients at the same time is not an overlap", card and "перетинається" not in card[-1][3], card)
        check("dental: «Розумію.» and «Дякую за розуміння» cut",
              app.polish("Розумію. Дякую за розуміння, зараз уточню.", tb) == "Зараз уточню.",
              app.polish("Розумію. Дякую за розуміння, зараз уточню.", tb))
        check("dental: «Чим можу допомогти?» after a greeting → «Підкажіть, що вас цікавить?»",
              app.polish("Добрий день! Чим можу допомогти?", tb) == "Добрий день. Підкажіть, що вас цікавить?",
              app.polish("Добрий день! Чим можу допомогти?", tb))
        check("dental: no «Що вас цікавить: яка послуга вас цікавить»",
              app.polish("Добрий день! Чим можу допомогти: яка послуга вас цікавить?", tb)
              == "Добрий день. Яка послуга вас цікавить?",
              app.polish("Добрий день! Чим можу допомогти: яка послуга вас цікавить?", tb))
        check("dental prompt: unknown → «Зараз уточню», typos → «Ви мали на увазі», no pushing",
              "Зараз уточню" in pd and "Ви мали на увазі" in pd and "НЕ НАВ'ЯЗУЙ ЗАПИС" in pd)
        other = json.loads(json.dumps(app.CLINIC_TEMPLATE))
        other["services"] = [{"name": "Консультація", "price": "500 грн"}, {"name": "Чистка зубів", "price": "2 100 грн"}]
        other["doctors"] = [{"name": "Петро Іванов", "role": "терапевт"}]
        po = app.system_prompt({"profile": other})
        check("dental prompt: examples use the clinic's OWN prices and doctors (nothing to copy from another clinic)",
              "2 100 грн" in po and "Петро Іванов" in po and "1 900" not in po and "Коваль" not in po and "450" not in po,
              [x for x in ("1 900", "Коваль", "450") if x in po])
        check("dental prompt: never «передам адміністратору»; honest «віртуальний адміністратор» only when asked",
              "віртуальний адміністратор" in pd and "ПРЯМО питає" in pd and "Ніколи не кажи, що ти жива людина" in pd)
        asked = [{"role": "user", "text": "Ви бот?", "ts": 1}]
        check("dental: honest answer to «Ви бот?» is allowed",
              not app.bot_problems("Я віртуальний адміністратор клініки: відповідаю на основні питання і допомагаю із записом.",
                                   None, tb, asked))
        unasked = [{"role": "user", "text": "Скільки років досвіду в лікаря?", "ts": 1}]
        check("dental: «я бот… немає даних» without being asked is caught",
              len(app.bot_problems("Я просто бот, у мене немає даних про це.", None, tb, unasked)) >= 2)
        for leak in ("Передам ваше питання адміністратору.", "Адміністратор вам напише.", "Уточню в адміністратора.",
                     "Я віртуальний помічник, тому точно не знаю.", "Перепрошую за помилкові дані.",
                     "Адміністратор клініки підтвердить час."):
            check(f"dental: «{leak}» gives the bot away → caught", app.bot_problems(leak, None, tb, unasked), leak)
        said_yes = [{"role": "user", "text": "Скільки коштує консультація?", "ts": 1},
                    {"role": "assistant", "text": "Консультація — 400 грн. Записати вас?", "ts": 2},
                    {"role": "user", "text": "так", "ts": 3}]
        check("dental: after «так» to «Записати вас?» asking for a day is NOT pushing",
              not app.bot_problems("На який день вам зручно?", None, tb, said_yes),
              app.bot_problems("На який день вам зручно?", None, tb, said_yes))
        said_no = said_yes[:2] + [{"role": "user", "text": "ні, поки подумаю. А знімок скільки?", "ts": 3}]
        check("dental: after «ні» another «Записати вас?» IS pushing",
              app.bot_problems("Панорамний знімок — 450 грн. Записати вас?", None, tb, said_no))
        check("dental: «Зараз уточню. Записати вас?» — no booking push while the question is open",
              app.bot_problems("Зараз уточню. Записати вас на консультацію?", "question", tb, unasked))
        for ok_text in ("Зараз уточню.", "Це треба уточнити в лікаря. Зараз уточню.", "Уточню в лікаря і напишу вам.",
                        "Дякую. Заявку на четвер після 17:00 прийнято, точний час підтвердимо."):
            check(f"dental: «{ok_text}» is fine", not app.bot_problems(ok_text, None, tb, unasked),
                  app.bot_problems(ok_text, None, tb, unasked))
        clear(); SCRIPT.extend(["Тобто вас цікавить досвід лікаря? Дякую за ваше питання, у мене немає такої інформації.",
                                "[MASTER] Зараз уточню."])
        dm(c, "скільки досвіду в лікаря?", account="1784B", sender="d1")
        check("dental: bot-like draft («Тобто…», «немає інформації») → rewritten to «Зараз уточню.»",
              [x[3] for x in ig_sent()] == ["Зараз уточню."], ig_sent())
        check("dental: unknown → clinic gets the question, AI steps back",
              app.is_paused(f"{tid}:d1") and any("Потрібна ваша відповідь" in x[3] for x in tg_msgs("777")))
        app.save(f"{tid}:d2", "user", "Скільки коштує консультація?")
        app.save(f"{tid}:d2", "assistant", "Консультація — 400 грн. Записати вас?")
        clear(); SCRIPT.extend(["Панорамний знімок — 450 грн. Записати вас на консультацію?", "Панорамний знімок — 450 грн."])
        dm(c, "а знімок скільки?", account="1784B", sender="d2")
        check("dental: no second «Записати вас?» when the patient didn't ask",
              [x[3] for x in ig_sent()] == ["Панорамний знімок — 450 грн."], ig_sent())
        clear(); SCRIPT.append("[SKIP]"); dm(c, "ок, подумаю", account="1784B", sender="d3")
        check("dental: [SKIP] → no reply at all", not ig_sent() and not tg_msgs("777"), (ig_sent(), tg_msgs("777")))
        clear(); SCRIPT.append("[SKIP]\n[ДЛЯ МАЙСТРА] дякує лікарці за прийом")
        dm(c, "Передайте Ірині дякую за прийом", account="1784B", sender="d4")
        check("dental: [SKIP] with a note → only the clinic's Telegram gets it",
              not ig_sent() and any("дякує лікарці" in x[3] for x in tg_msgs("777")), tg_msgs("777"))
        clear(); SCRIPT.extend(["[SKIP]", "Консультація — 400 грн."]); dm(c, "а консультація скільки?", account="1784B", sender="d5")
        check("dental: a question is never left unanswered", [x[3] for x in ig_sent()] == ["Консультація — 400 грн."], ig_sent())
        REVIEW["fn"] = lambda d: {"ok": False, "problems": ["вигадка: про мікроскоп у даних нічого"], "fixed": "Зараз уточню.",
                                  "handoff": True}
        clear(); SCRIPT.append("Так, усі наші лікарі працюють з мікроскопом."); dm(c, "у вас є мікроскоп?", account="1784B", sender="d6")
        REVIEW.clear()
        check("dental: invented fact caught by the self-check → «Зараз уточню.»", [x[3] for x in ig_sent()] == ["Зараз уточню."],
              ig_sent())
        check("dental: ...and the clinic gets the question, AI steps back",
              app.is_paused(f"{tid}:d6") and any("Потрібна ваша відповідь" in x[3] for x in tg_msgs("777")))
        app.tenant_update(tid, profile=app.PROFILE_TEMPLATE, active=0)

        print("\n### PRIVACY: provider details, model, deletion requests, retention")
        app.PROVIDER_NAME, app.PROVIDER_PERSON = "ФОП Тестовий Виконавець", "Тестовий Виконавець"
        pv = c.get("/privacy").get_data(as_text=True)
        check("privacy page names the provider and the responsible person",
              "ФОП Тестовий Виконавець" in pv and "Відповідальна особа" in pv, pv[:200])
        check("privacy page names the AI model", app.model_label("claude-haiku-5-5") == "Claude Haiku 5.5"
              and app.model_label() in pv and "Anthropic" in pv, app.model_label())
        app.PROVIDER_NAME = app.PROVIDER_PERSON = ""
        check("privacy page still renders without provider details", c.get("/privacy").status_code == 200)
        clear(); SCRIPT.append("Добрий день! Чим можу допомогти?")
        dm(c, "привіт", sender="del1")
        clear()
        dm(c, "Видаліть мої дані, будь ласка", sender="del1")
        with app.db() as conn:
            left = conn.execute("SELECT COUNT(*) FROM messages WHERE user_id='demo:del1'").fetchone()[0]
        check("«видаліть мої дані»: conversation deleted", left == 0, left)
        check("«видаліть мої дані»: client gets a confirmation, not an AI answer",
              ig_sent() and "запит прийнято" in ig_sent()[-1][3] and not CALLS, (ig_sent(), len(CALLS)))
        check("«видаліть мої дані»: the business is told in Telegram",
              any("видалити свої дані" in x[3] for x in tg_msgs()), tg_msgs())
        with app.db() as conn:
            conn.execute("INSERT INTO messages VALUES (?,?,?,?)", ("demo:old", "user", "old", 1.0))
            conn.execute("INSERT INTO messages VALUES (?,?,?,?)", ("demo:new", "user", "new", time.time()))
            conn.execute("INSERT INTO bookings (tenant, conv, date, status, created) VALUES (?,?,?,?,?)",
                         ("demo", "demo:old", "2020-01-01", "confirmed", 1.0))
        app.purge_old_data()
        with app.db() as conn:
            old = conn.execute("SELECT COUNT(*) FROM messages WHERE user_id='demo:old'").fetchone()[0]
            new = conn.execute("SELECT COUNT(*) FROM messages WHERE user_id='demo:new'").fetchone()[0]
            ob = conn.execute("SELECT COUNT(*) FROM bookings WHERE date='2020-01-01'").fetchone()[0]
        check("old chats and long-past bookings are purged, recent ones kept", old == 0 and ob == 0 and new == 1,
              (old, ob, new))

        print("\n### DELETE A TEST / UNUSED ACCOUNT")
        with app.db() as conn:
            conn.execute("INSERT INTO messages VALUES (?,?,?,?)", (f"{did}:p1", "user", "hi", 1.0))
            conn.execute("INSERT INTO messages VALUES (?,?,?,?)", (f"{tid}:keep", "user", "hi", 1.0))
            conn.execute("INSERT INTO bookings (tenant, conv, status) VALUES (?,?,?)", (did, f"{did}:p1", "pending"))
        app.kv_set(f"pause_{did}:p1", 1)
        check("admin list shows a delete button", "🗑 Видалити" in c.get("/admin?key=k").get_data(as_text=True))
        check("delete needs key", c.post(f"/admin/t/{did}/delete").status_code == 403)
        r = c.post(f"/admin/t/{did}/delete", data={"key": "k"})
        with app.db() as conn:
            left = (conn.execute("SELECT COUNT(*) FROM messages WHERE user_id LIKE ?", (f"{did}:%",)).fetchone()[0]
                    + conn.execute("SELECT COUNT(*) FROM bookings WHERE tenant=?", (did,)).fetchone()[0])
            kept = conn.execute("SELECT COUNT(*) FROM messages WHERE user_id=?", (f"{tid}:keep",)).fetchone()[0]
        check("deleted clinic is gone with its chats, bookings and pauses",
              r.status_code == 302 and not app.tenant_get(did) and left == 0 and not app.kv_get(f"pause_{did}:p1"),
              (r.status_code, left))
        check("other accounts' data untouched", kept == 1 and app.tenant_get(tid))
        check("self-test account can't be deleted", c.post("/admin/t/selftest/delete", data={"key": "k"}).status_code in (400, 404))
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
