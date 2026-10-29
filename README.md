# Instagram booking assistant — how to run it

An AI "адміністраторка" that answers a beauty specialist's Instagram DMs (prices, slang, bookings),
sends booking requests to her Telegram with buttons, and keeps her appointment list.

## Files (all go to GitHub)
| File | What it is |
|---|---|
| `app.py` | the whole server |
| `specialist.json` | demo specialist data and the template for new ones (prices, schedule, rules) |
| `glossary.json` | nail terms and slang the assistant understands |
| `clinic.json` | demo dental clinic and the template for new clinics (`"niche": "dental"`) |
| `glossary_dental.json` | dental terms and slang (пломба, вісімка, ОПТГ, елайнери…) |
| `requirements.txt` | Python packages |
| `test_chat.py` | optional: chat with the AI from your computer |
| `test_suite.py` | optional: 163 offline tests of the server logic (`python test_suite.py`) |

## Render → Environment
Required: `ANTHROPIC_API_KEY`, `VERIFY_TOKEN`, `TEST_KEY`, `META_APP_SECRET`, `TELEGRAM_BOT_TOKEN`.
For connecting new specialists: `INSTAGRAM_APP_ID`.
Recommended: `ADMIN_TELEGRAM_CHAT_ID` = your own chat id (technical alerts go there), `CONTACT_EMAIL`, `PYTHONUNBUFFERED=1`.
Privacy page (/privacy): `PROVIDER_NAME` (e.g. «ФОП Прізвище Імʼя По батькові»), `PROVIDER_PERSON` (responsible for data), `CONTACT_EMAIL`, optional `CONTACT_PHONE`, `PROVIDER_CODE`, `PROVIDER_ADDRESS`, `HOSTING_REGION` (as shown in Render). The AI model name is shown automatically. Old chats are deleted after `MESSAGE_KEEP_DAYS` (90), bookings `BOOKING_KEEP_DAYS` (365) after the visit; a client writing «видаліть мої дані» in Direct gets the chat deleted at once.
Old demo setup (keeps working): `IG_ACCESS_TOKEN`, `TELEGRAM_CHAT_ID`.
Optional: `CLAUDE_MODEL` (default `claude-haiku-5-5`), `UA_EDITOR=0` to switch off the self-check before sending (not recommended),
`SESSION_GAP_HOURS` (6), `PAUSE_HOURS` (12), `MIN_DELAY`/`MAX_DELAY` (2/10 s).

## Your pages (add `?key=YOUR_TEST_KEY`)
- `/admin` — specialists, their data, links to send them; «🗑 Видалити» removes a test or unused account with all its chats and bookings
- `/selftest` — real conversations through the AI (25 nails / 31 dental), pass/fail report (nothing is sent anywhere)
- `/debug` — status, last AI error, last webhook visits
- `/reset` — wipe test conversations and bookings
- `/telegram/setup` — open once after deploying (buttons + menu)
- `/subscribe` — re-subscribe Instagram if DMs stop arriving

## Connecting a real specialist
1. Once: Meta app → Instagram → API setup with Instagram login → Business login settings →
   OAuth redirect URI `https://instagram-administrator.onrender.com/instagram/callback`. Put the Instagram app ID into
   Render as `INSTAGRAM_APP_ID`.
2. `/admin` → «Новий майстер» → fill her data: services with prices and durations, `schedule`
   (e.g. `"Пн": "09:00-19:00"`, `"Нд": "вихідний"`), `closed_dates`, address, rules.
3. Until Meta App Review: Meta app → App roles → Instagram Testers → add her Instagram username.
4. Send her the two links from her admin page: Instagram connect link, then the Telegram link.
   Her account must be Professional. She accepts the tester invite in Instagram → Settings → Website permissions →
   Apps and websites → Tester invites, then taps «Підключити Instagram».
5. Ask her to switch OFF instant replies / away messages in Meta Business Suite (otherwise clients get two replies).
6. Test: DM her account from another account and book something.

## Costs (Haiku)
About $2–4 per specialist per month at ~10 conversations a day. Render Starter is a fixed monthly fee.
To give one specialist the stronger model, add `"model": "claude-sonnet-5-5"` to her data (≈ $15–30/month).

## Two niches: nails and dental clinics
One server serves both. Each client's data has `"niche"`: `"nails"` (default) or `"dental"`.
- `/admin` → «Новий» → choose 💅 Манікюр or 🦷 Стоматологія. The clinic starts from `clinic.json`.
- Dental mode: short, human, reserved tone (no emoji, no «супер»), never diagnoses or suggests medicines,
  treatment prices only after examination, emergencies → clinic phone / 103, Telegram cards with 🦷.
- The patient talks to «the administrator»: the bot never says it is a bot or that it «passes the question to the
  administrator». Only when asked directly («ви бот?») it answers honestly: «Я віртуальний адміністратор клініки…».
- `"parallel_bookings": true` (set in clinic.json): several doctors work at once, so bookings at the same time
  are not treated as overlaps.
- `/selftest` has two buttons: 💅 nails scenarios and 🦷 dental scenarios.

## How a reply is made (since 29 Oct)
1. The AI writes a draft from the clinic's data only. Anything not in the data (a doctor's experience, equipment,
   guarantees, a service that isn't listed) → «Зараз уточню.» / «Уточню в лікаря і напишу вам.» — never a guess,
   never «у мене немає інформації». A misspelled or unknown word → «Ви мали на увазі брекети?» / «Що саме ви маєте на увазі?»
2. **Self-check before sending.** The draft is read again together with the conversation, the way the patient will read it:
   nonsense or repeats («Що вас цікавить? Яка послуга вас цікавить?»), bot phrases («Дякую за розуміння»,
   «найближчим часом», «передам адміністратору»), invented facts, a second «Записати вас?» the patient didn't ask for,
   contradicting what was already said. A bad draft is fixed or written again; an invented fact becomes «Зараз уточню.»
3. **Hand-over means hand-over.** When the bot sends a question to Telegram («📩 Потрібна ваша відповідь»), or someone from
   the clinic answers the patient (✍️ in Telegram or straight in Instagram), the bot stops writing to that patient for
   `PAUSE_HOURS` (12 h). New messages from the patient go to Telegram. ▶️ «Увімкнути помічника» gives the chat back.
   What the person wrote is remembered as the clinic's words, so the bot never «corrects» or contradicts it.
4. A reply that was being prepared while a person answered is thrown away (no late «Тобто…» after the admin's answer).
5. Messages that need no answer («ок, подумаю», spam) get none.

After deploying: open `/selftest?key=…` → 🦷 and check the report.
