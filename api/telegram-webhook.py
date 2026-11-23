#!/usr/bin/env python3
"""Telegram webhook for @WCU_Assistantbot (Wazirganj College Updates).

Vercel Serverless Function (Python, stdlib only — no dependencies).
Replaces the polling bot (group-bot.py), which died on every VM
replacement because its systemd unit lives in ephemeral /etc.

- Telegram POSTs updates to /api/telegram-webhook; this answers them
  with the same knowledge base and routing as the polling bot.
- Token comes from the TELEGRAM_GROUP_BOT_TOKEN env var (never in repo).
- Optional shared secret: if TELEGRAM_WEBHOOK_SECRET is set, Telegram
  must send it as X-Telegram-Bot-Api-Secret-Token, else 403.
- GET returns a health JSON (no token needed) for monitoring.
- AI TONE (Gemini, all replies): every college answer is rephrased by
  Gemini in a natural AI tone — but the FACTS always come from the canned
  answer, and a fact check rejects any rephrase that drops or changes a
  date, fee, link or number (then the canned text is sent as-is).
  Link-only answers (teacher number) and refusals (roll/password) are
  never rephrased. Questions the rules cannot answer go to Gemini
  grounded ONLY on the live website's notices + past Q&A (fetched at
  runtime); ungrounded -> NAHI_PATA -> A_FALLBACK. No key
  (GEMINI_API_KEY) = no AI, canned behaviour unchanged.
- Replies ONLY in the discussion group (GROUP_CHAT_ID). Never in
  private chats or other groups. Same rules as the polling bot:
  short formal Devanagari Hindi, only what was asked, no link previews,
  teacher numbers = group-message link only, never private data.
"""

import json
import os
import re
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

TOKEN = os.environ.get("TELEGRAM_GROUP_BOT_TOKEN", "")
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GROUP_CHAT_ID = int(os.environ.get("GROUP_CHAT_ID", "-1002003808705"))
API = "https://api.telegram.org/bot" + TOKEN

AI_SYSTEM = (
    "You answer students' questions about Wazirganj College (Magadh "
    "University) in the Wazirganj College Updates discussion group. "
    "RULES (strict): answer ONLY from the NOTICES given below — never "
    "from general knowledge, never invent dates, fees, links or numbers. "
    "Reply in short formal Devanagari Hindi (1-3 lines), English terms "
    "in Latin script (Exam Form, Admit Card, Last Date). Say ONLY what "
    "was asked, no extra information, no join/channel links. If the "
    "question asks for a sir/teacher/principal phone number, reply ONLY "
    "with this line: 'Sir/Teacher के number इस message में दिए गए हैं: "
    "https://t.me/c/2003808705/20695'. Never give a magadhonline link or "
    "a Telegram link. The Exam Form link, if asked, is only "
    "wazirganjcollege.vercel.app. If the notices do not contain the "
    "answer, reply with exactly: NAHI_PATA"
)

PHONE_RE = re.compile(r"[6-9]\d{9}")

WEBSITE = "wazirganjcollege.vercel.app"
WA_CHANNEL = "https://whatsapp.com/channel/0029VbAOPnRFsn0gSrR6By2Q"


def tg(method, payload):
    """Call one Bot API method. Returns True on ok. Never logs the token."""
    if not TOKEN:
        print("telegram-webhook: no TELEGRAM_GROUP_BOT_TOKEN set")
        return False
    try:
        req = urllib.request.Request(
            API + "/" + method,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8")).get("ok", False)
    except Exception as e:  # noqa: BLE001 - log type only, never body/URL
        print("telegram-webhook: %s failed: %s" % (method, type(e).__name__))
        return False


def send_message(chat_id, text, reply_to=None, markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    if markup:
        payload["reply_markup"] = markup
    return tg("sendMessage", payload)


def edit_message(chat_id, message_id, text, markup=None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if markup:
        payload["reply_markup"] = markup
    return tg("editMessageText", payload)


def answer_callback(callback_query_id):
    return tg("answerCallbackQuery",
              {"callback_query_id": callback_query_id})


def fetch_notices():
    """Newest notices from the live website (grounding for the AI)."""
    try:
        req = urllib.request.Request(
            "https://wazirganjcollege.vercel.app/", method="GET")
        with urllib.request.urlopen(req, timeout=6) as r:
            html = r.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        print("telegram-webhook: site fetch failed: %s" % type(e).__name__)
        return []
    i = html.find("const UPDATES = ")
    if i == -1:
        return []
    try:
        updates, _ = json.JSONDecoder().raw_decode(
            html, i + len("const UPDATES = "))
    except Exception:  # noqa: BLE001
        return []
    lines = []
    for n in updates[:25]:
        line = "- %s | %s" % (n.get("title", ""), n.get("summary", ""))
        if n.get("lastDate"):
            line += " | Last Date: %s" % n.get("lastDate")
        lines.append(line)
    return lines


def ai_answer(question):
    """Gemini fallback for college questions the rules cannot answer.
    Returns a grounded short answer, or None (caller falls back)."""
    if not GEMINI_API_KEY:
        return None
    notices = fetch_notices()
    if not notices:
        return None
    parts = ["NOTICES:\n" + "\n".join(notices)]
    past = past_qa_lines()
    if past:
        parts.append("PAST Q&A (how similar questions were answered):\n"
                     + "\n".join(past))
    parts.append("QUESTION: " + question)
    text = gemini_text(AI_SYSTEM, "\n\n".join(parts))
    if not text or text == "NAHI_PATA" or len(text) > 600:
        return None
    if PHONE_RE.search(text):
        return None
    return text


REPHRASE_SYSTEM = (
    "Rephrase the FACT below in a natural, friendly AI-assistant tone, "
    "in short formal Devanagari Hindi (1-3 lines), English terms in "
    "Latin script (Exam Form, Admit Card, Last Date). Keep EVERY date, "
    "fee, amount, link, name and number EXACTLY as in the FACT — do not "
    "change, drop or add any of them, do not add extra information. "
    "Reply with the rephrased text only."
)

FACT_RE = re.compile(
    r"\d{2}/\d{2}/\d{4}|Rs\.\s*\d+|https?://\S+|"
    r"\b[\w-]+\.(?:gov\.in|vercel\.app|in|com)\b")


def fact_tokens(text):
    """Dates, fees, links and domains that must survive a rephrase."""
    return [t.rstrip(".,)") for t in FACT_RE.findall(text)]


def facts_preserved(fact, text):
    return all(tok in text for tok in fact_tokens(fact))


def should_rephrase(answer):
    """Never rephrase link-only answers or private-data refusals."""
    if "http" in answer:
        return False
    return answer is not A_ROLL


def gemini_text(system, prompt):
    """One Gemini call. Returns the reply text, or None on any failure.
    Fast-fail (6s): a slow AI call must never delay the reply — the
    caller sends the canned fact instead, so the bot always answers."""
    if not GEMINI_API_KEY:
        return None
    body = json.dumps({
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 300},
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            + GEMINI_MODEL + ":generateContent",
            data=body,
            headers={"Content-Type": "application/json",
                     "x-goog-api-key": GEMINI_API_KEY},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001 - never log key/URL/body
        print("telegram-webhook: gemini call failed: %s" % type(e).__name__)
        return None
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError, TypeError):
        return None


_REPHRASE_CACHE = {}


def ai_rephrase(question, fact):
    """Rephrase a canned fact in AI tone. Falls back to the fact itself.
    A validated rephrase is cached per fact, so repeat questions (the
    same last-date questions all day) answer instantly and use no
    extra Gemini quota."""
    if not should_rephrase(fact):
        return fact
    hit = _REPHRASE_CACHE.get(fact)
    if hit is not None:
        return hit
    text = gemini_text(
        REPHRASE_SYSTEM,
        "QUESTION: " + question + "\n\nFACT: " + fact)
    if not text or len(text) > 600:
        return fact
    if PHONE_RE.search(text):
        return fact
    if not facts_preserved(fact, text):
        return fact
    if len(_REPHRASE_CACHE) < 200:
        _REPHRASE_CACHE[fact] = text
    return text


def past_qa_lines():
    """Past Q&A pairs (from sample questions) — the bot's
    memory of how questions were answered before. Auto-train input."""
    lines = []
    for q in PAST_QA_QUESTIONS:
        a = find_answer(q)
        if a and a is not A_FALLBACK:
            lines.append("Q: %s | A: %s" % (q, a))
    return lines


def final_answer(question, canned):
    """The reply to send: canned fact in AI tone, or the AI-grounded
    answer for questions the rules cannot answer."""
    if canned is None:
        return None
    if canned == A_FALLBACK:
        ai = ai_answer(question)
        return ai if ai else A_FALLBACK
    return ai_rephrase(question, canned)


def tg_result(method, payload):
    """Call one Bot API method, return its result dict, or None."""
    if not TOKEN:
        return None
    try:
        req = urllib.request.Request(
            API + "/" + method,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8")).get("result")
    except Exception as e:  # noqa: BLE001 - log type only, never body/URL
        print("telegram-webhook: %s failed: %s" % (method, type(e).__name__))
        return None


def is_admin(user_id):
    """True if the user is the group's creator or an administrator."""
    if not user_id:
        return False
    member = tg_result("getChatMember",
                       {"chat_id": GROUP_CHAT_ID, "user_id": user_id})
    return bool(member) and member.get("status") in (
        "creator", "administrator")


# /stop pause (21 Nov 2026, Veeru's word). In-memory ONLY — the bot
# cannot pin messages (can_pin_messages is False), so there is no
# durable store for the pause; a Vercel cold start ends it early.
# Best-effort 5 minutes; the group stays active, so the warm instance
# normally carries the full pause.
PAUSED_UNTIL = 0.0
PAUSE_SECONDS = 300


def is_paused():
    return time.time() < PAUSED_UNTIL


def handle_admin_command(msg):
    """Admin-only commands /stop and /text. Returns True if the message
    was one of them (handled or silently ignored), else False.

    /stop — stops the bot's replies for 5 minutes; sends one
    confirmation, then silence. /stop again restarts the 5 minutes.
    /text — only when an admin replies to (tags) a message with
    "/text <message>": the bot sends that message as its own reply to
    the tagged message, then deletes the admin's /text message (so
    students see only the bot's reply). Both admin-only; anyone else
    is silently ignored.
    """
    global PAUSED_UNTIL
    chat = msg.get("chat", {})
    sender = msg.get("from", {})
    text = msg.get("text") or ""
    cmd = text.strip().split()[0].lower() if text.strip() else ""
    if cmd not in ("/stop", "/stop@wcu_assistantbot",
                   "/text", "/text@wcu_assistantbot"):
        return False
    if not is_admin(sender.get("id")):
        return True
    if cmd in ("/stop", "/stop@wcu_assistantbot"):
        PAUSED_UNTIL = time.time() + PAUSE_SECONDS
        until_ist = time.strftime("%H:%M",
                                  time.gmtime(PAUSED_UNTIL + 19800))
        send_message(chat["id"],
                     "⏸️ Bot 5 minute ke liye band hai — %s IST tak "
                     "koi jawab nahi dega।" % until_ist,
                     reply_to=msg.get("message_id"))
        print("telegram-webhook: paused by admin until %s IST" % until_ist)
        return True
    target = msg.get("reply_to_message") or {}
    parts = text.strip().split(None, 1)
    body = parts[1].strip() if len(parts) > 1 else ""
    if not target or not body:
        return True
    ok = send_message(chat["id"], body,
                      reply_to=target.get("message_id"))
    if ok:
        tg("deleteMessage", {"chat_id": chat["id"],
                             "message_id": msg.get("message_id")})
        print("telegram-webhook: relayed /text to msg %s; command deleted"
              % target.get("message_id"))
    return True


def send_menu(chat_id, reply_to=None):
    return send_message(chat_id, JOIN_TEXT, reply_to=reply_to)


def handle_callback(cq):
    # No panels anymore — but messages already sent keep their buttons.
    # Any tap on one retires that message to the Join text.
    sender = cq.get("from", {})
    if sender.get("is_bot"):
        return
    msg = cq.get("message") or {}
    chat = msg.get("chat", {})
    if chat.get("id") != GROUP_CHAT_ID:
        return
    answer_callback(cq["id"])
    edit_message(chat["id"], msg.get("message_id"), JOIN_TEXT)


def handle_message(msg):
    chat = msg.get("chat", {})
    if chat.get("type") not in ("group", "supergroup"):
        return
    if chat.get("id") != GROUP_CHAT_ID:
        return
    # Channel posts auto-forwarded to the group (sender_chat = channel,
    # is_automatic_forward) must NEVER get a reply — Veeru's rule.
    if msg.get("is_automatic_forward"):
        return
    if msg.get("sender_chat", {}).get("type") == "channel":
        return
    sender = msg.get("from", {})
    if sender.get("is_bot"):
        return
    text = msg.get("text")
    if not text:
        return
    cmd = text.strip().split()[0].lower() if text.strip() else ""
    # Admin commands work even while paused; everything else stops
    # while the bot is paused.
    if handle_admin_command(msg):
        return
    if is_paused():
        return
    if cmd in ("/menu", "/menu@wcu_assistantbot"):
        send_menu(chat["id"], reply_to=msg.get("message_id"))
        return
    answer = final_answer(text, find_answer(text))
    if answer is None:
        return
    send_message(chat["id"], answer, reply_to=msg.get("message_id"))
    print("telegram-webhook: replied to msg %s: %r"
          % (msg.get("message_id"), text[:60]))


A_TEACHER_NUMBER = (
    "Sir/Teacher के number इस message में दिए गए हैं: "
    "https://t.me/c/2003808705/20695"
)

A_ROLL = (
    "क्षमा करें, roll number, password या कोई personal जानकारी chat पर share नहीं की जा सकती। इसके लिए college office से संपर्क करें।"
)

A_SEM7 = (
    "Session 2023-27 (Sem-VII) ka Exam Form portal khula hai — form online "
    "bharna hai, User ID aur Password se, fee ₹800। Koi nayi Last Date "
    "ghoshit nahi hai।"
)
# NOTE: A_SEM7 me koi date NAHI hai (jaan-bujhkar) — CEO/1119/2026 (21/11/2026)
# ke anusar portal khula hai aur form online bharna hai, lekin koi nayi Last
# Date ghoshit nahi hai. Isliye sync-bot.py is constant ko skip karega
# (date-count mismatch -> UNVERIFIED) — nayi date aate hi daalni hai.

A_SEM5 = (
    "Session 2024-28 (Sem-V) के Exam Form की Last Date 05/12/2026 है।"
)

A_SEM3_FORM = (
    "Session 2025-29 (Sem-III) के Exam Form का Portal आज, 23/11/2026 को दोपहर 3:00 बजे बंद हो गया है। अब form नहीं भरा जा सकता। Admit Card University ने जारी कर दिया है।"
)

A_PROGRAMME = (
    "Sem-III (Session 2025-29) का Exam Programme 26/11/2026 से 02/12/2026 तक है। Details: https://t.me/Wazirganj_Collegeinfo/2179"
)

# Sem-VII programme (channel post 2054 + website /exam, Memo CEO/971/2026).
A_PROGRAMME_VII = (
    "Sem-VII (Session 2023-27) का Exam Programme 26/11/2026 से 30/11/2026 तक है — 26/11 MJC-13, 27/11 MJC-14, 28/11 MJC-15, 30/11 MIC-9। Practical Exam 18/11/2026 से 23/11/2026 तक है। Details: https://t.me/Wazirganj_Collegeinfo/2054"
)

# Sem-V programme (Veeru's word, 21 Nov 2026: "Sem v ka new exam
# programme aata hai"). Purana programme (10/12–15/12/2026, Memo
# CEO/969/2026) QUOTE NAHI karna — naya programme aane wala hai. Ye
# canned jawab isliye hai taaki fallback AI website se purana programme
# utha kar na bata de.
A_PROGRAMME_V = (
    "Sem-V (Session 2024-28) का new Exam Programme अभी नहीं आया है। जैसे ही आएगा, channel पर बता दिया जाएगा।"
)

A_ADMIT = (
    "Sem-III (Session 2025-29) का Admit Card University ने जारी कर दिया है। College ने सूचना जारी की है — Admit Card 24/11/2026 को दोपहर 12:00 बजे के बाद से College कार्यालय से प्राप्त कर लें। Admit Card के लिए 75% attendance और Internal Exam में pass होना ज़रूरी है।"
)

A_FEE = "Exam Form की fee Rs. 800 है (सभी sessions और semesters के लिए)।"

A_OFFICE = (
    "Exam Form के लिए college office प्रतिदिन सुबह 10:00 बजे से दोपहर 3:00 बजे तक खुला रहता है।"
)

A_PAYMENT = (
    "अगर payment नहीं कट रहा है, तो college ने अभी आपका Form Validate नहीं किया है। College के Validate करते ही payment कटने लगेगा। धैर्य रखें।"
)

A_EXSTUDENT = (
    "हाँ। Session 2023-27 के students Sem-V का Exam Form Ex-Student के रूप में भर सकते हैं। Session 2024-28 और 2023-27 के Ex-Student Sem-III का Exam Form भी भर सकते हैं।"
)

A_PMS = (
    "PMS Scholarship 2026-27 का portal 12/11/2026 से 30/11/2026 तक खुला है। BC-EBC: pmsonline.bihar.gov.in, SC-ST: scstpmsonline.bihar.gov.in। Defective application हो तो 15 दिन के अंदर सुधार करें (अधिकतम 3 बार)। Aadhaar Seeding ज़रूर check करें।"
)

A_ADMISSION_LASTDATE = (
    "Admission की कोई fixed Last Date घोषित नहीं है। जब तक Magadh "
    "University की ओर से Exam Fee जमा करने की सूचना नहीं आती, तब तक कई "
    "मामलों में Admission चलता रहता है — फिर भी जितनी जल्दी हो सके अपना "
    "Admission करा लें। यह जानकारी official notification पर नहीं, अनुभव "
    "पर आधारित है। पूरी जानकारी: https://t.me/Wazirganj_Collegeinfo/2097"
)

A_ADMISSION = (
    "Admission के लिए चाहिए: Offer Letter, Aadhaar Card, Photo, 10th/12th Marksheet, TC, Caste Certificate, APAAR ID, Bank Passbook, माता-पिता का Aadhaar। Details: https://wazirganjcollege.vercel.app/admission"
)

A_BACK = (
    "अगर आपने सारे paper और practical दिए थे, फिर भी marksheet में ABSENT या Back in Paper लिखा है, तो तुरंत college से संपर्क करें।"
)

A_SPECIAL = (
    "जिन students का exam छूट गया है, उनका Special Exam लिया जाता है। Special Exam की date के लिए college से संपर्क करें।"
)

A_PMW = (
    "अगर आपका result PMW या PENDING है, तो एक application लिखकर पहले के semesters की marksheet और admit card लगाकर college में जमा करें। Details: https://wazirganjcollege.vercel.app/result"
)

A_DEADLINES = (
    "Exam Form की Last Dates: Sem-III (2025-29) — 23/11/2026 (Portal बंद हो गया है), Sem-V (2024-28) — 05/12/2026, Sem-VII (2023-27) — portal खुला है, कोई नयी Last Date घोषित नहीं है। Sem-I (2026-30) की कोई Last Date अभी घोषित नहीं है। PMS Scholarship — 30/11/2026।"
)
# NOTE: A_DEADLINES me Sem-VII ki koi date NAHI hai (portal khula hai, nayi
# date ghoshit nahi hai) — isliye sync-bot.py is constant ko skip karega
# (date-count mismatch -> UNVERIFIED). Nayi date aate hi daalni hai.

A_FORM_GENERIC = (
    "Sem-V (Session 2024-28) के Exam Form की Last Date 05/12/2026 है, Sem-III (Session 2025-29) का Portal 23/11/2026 को दोपहर 3:00 बजे बंद हो गया है (अब form नहीं भरा जा सकता), और Sem-VII (Session 2023-27) का portal खुला है (koi nayi Last Date ghoshit nahi hai)।"
)
# NOTE: A_FORM_GENERIC me bhi Sem-VII ki koi date NAHI hai (portal khula hai,
# nayi date ghoshit nahi hai) — sync-bot.py ise bhi skip karega jab tak
# university ki nayi notice nahi aati; tab date wapas daalni hai.

# NEW (21 Nov 2026, intent-first fix): "form kaise bharen" ka jawab — pehle
# ye sawaal session ki last date me kho jata tha. Sirf itna hi kehte hain
# jo pakka pata hai: form ONLINE bhara jata hai. (Veeru's word, 21 Nov:
# exam form ki documents list mange to specific page ka link de do —
# isliye yahan /exam-form ka link hai. Login format isme NAHI hai — bot
# password wale sawalon ko refuse karta hai, aur Veeru ne login format
# dene ki confirmation abhi nahi di hai.)
A_FORM_HOW = (
    "Exam Form ONLINE bhara jata hai। Form के लिए documents की list: https://wazirganjcollege.vercel.app/exam-form"
)

A_FALLBACK = (
    "इस विषय में अभी तक college से कोई सूचना नहीं आई है। जैसे ही सूचना आएगी, channel पर बता दिया जाएगा — कृपया wait करें।"
)

# ---------------------------------------------------------------------------
# NO PANELS (21 Nov 2026, Veeru's word: "Panel htake sare" — nobody uses
# the Menu button). There are no inline keyboards anymore: /menu sends a
# plain text message, "Join On WhatsApp" with the WhatsApp channel link.
# The bot only answers typed questions now.
# ---------------------------------------------------------------------------

JOIN_TEXT = (
    "Wazirganj College Updates\n\n"
    "Join On WhatsApp 👇\n"
    "https://whatsapp.com/channel/0029VbAOPnRFsn0gSrR6By2Q\n\n"
    "🌐 Website: https://wazirganjcollege.vercel.app\n\n"
    "Managed by @veeruverma"
)

# Sample questions (routed through find_answer) — past Q&A memory for the
# webhook's AI grounding. Replaces the old CALLBACK_QUESTIONS, which died
# with the panels.
PAST_QA_QUESTIONS = [
    "exam form ki last date kya hai",
    "sem 3 ka exam programme",
    "admit card kab aayega",
    "exam form ki fee kitni hai",
    "pms scholarship ki last date kya hai",
    "admission ke liye documents kya chahiye",
    "admission ki last date kya hai",
    "sir ka number chahiye",
    "college office kab khulta hai",
    "saari last dates batao",
]

# Intent keyword groups (21 Nov 2026 intent-first fix). find_answer checks
# the INTENT of the question first (admit? fee? payment? programme?) and
# only then applies the session — the old first-match RULES list let a
# session word ("Sem-V") hijack any question into a last-date answer.
ADMIT_KW = ["admit", "एडमिट"]
FEE_KW = ["fee", "fees", "फीस", "शुल्क"]
PAYMENT_KW = ["payment", "पेमेंट", "भुगतान", "deduct", "kat gaya",
              "कट गया", "kat nahi", "कट नहीं", "pay nahi", "payment nahi"]
# Payment is "not deducted" only with a negative word; "payment kat gaya"
# (it WAS deducted) is a different question — it must NOT get the
# "payment nahi kat raha" answer.
PAYMENT_NEG_KW = ["nahi", "नहीं", "nhi", "nahin", "fail", "failed",
                  "atka", "अटका", "ruka", "रुका"]
OFFICE_KW = ["office", "कार्यालय", "college open", "कॉलेज खुला",
             "office kab", "कार्यालय कब"]
# NOTE: bare "college kab" is deliberately NOT an office keyword — it
# usually means "college kab khulega / chhutti hai kya", not office timing.
LASTDATE_KW = ["last date", "लास्ट डेट", "अंतिम तिथि", "deadline",
               "kab tak", "कब तक", "antim", "अंतिम", "aakhri", "आखिरी",
               "band kab", "बंद कब"]
FORM_KW = ["exam form", "परीक्षा फॉर्म", "examform", "form bhar",
           "फॉर्म भर", "form kaise", "form kab"]
HOW_KW = ["kaise", "कैसे", "how", "tarika", "तरीका", "steps", "process",
          "प्रक्रिया"]
BACK_KW = ["back in paper", "back paper", "backpaper", "बैक"]

SESSION_7 = ["2023-27", "2023 27", "sem-vii", "sem vii", "sem-7", "sem 7",
             "sem7", "semester 7", "vii", "7th sem", "7th semester"]
SESSION_5 = ["2024-28", "2024 28", "sem-v", "sem v", "sem-5", "sem 5",
             "sem5", "semester 5", "5th sem", "5th semester"]
SESSION_3 = ["2025-29", "2025 29", "sem-iii", "sem iii", "sem-3", "sem 3",
             "sem3", "semester 3", "3rd sem", "3rd semester"]
# Sem-I (2026-30): newest session. No exam-form / programme / admit-card
# fact is known for it yet — those questions fall through to the AI
# fallback instead of borrowing another session's fact. ("sem i" is
# deliberately NOT listed: it is a substring of "sem iii".)
SESSION_1 = ["2026-30", "2026 30", "sem-1", "sem 1",
             "sem1", "semester 1", "1st sem", "1st semester"]
PROGRAMME_KW = ["programme", "program", "कार्यक्रम", "date sheet",
                "datesheet", "time table", "टाइम टेबल", "परीक्षा तिथि",
                "exam date"]

# Broad college-question keywords: if none of these match, the message is
# not a college question at all — ignore it silently.
COLLEGE_KEYWORDS = [
    "exam", "परीक्षा", "form", "फॉर्म", "college", "कॉलेज", "session",
    "सत्र", "sem", "सेमेस्टर", "result", "रिजल्ट", "marksheet", "मार्कशीट",
    "practical", "प्रैक्टिकल", "internal", "इंटरनल", "attendance",
    "उपस्थिति", "university", "विश्वविद्यालय", "magadh", "मगध", "notice",
    "सूचना", "update", "अपडेट", "last date", "तिथि", "programme",
    "कार्यक्रम", "admit", "एडमिट", "pms", "scholarship", "छात्रवृत्ति",
    "back", "बैक", "fee", "फीस", "payment", "भुगतान", "office",
    "कार्यालय", "admission", "नामांकन", "document", "दस्तावेज", "roll",
    "रोल", "pending", "पेंडिंग", "pmw", "special", "विशेष",
    "chhutti", "छुट्टी", "chutti", "holiday", "band", "बंद",
]


def _session_of(t):
    """Which session the question names: 7, 5, 3, 1, or None. Sem-VII is
    checked first because "sem vii" contains "sem v" as a substring, and
    Sem-III before Sem-I because "sem iii" contains "sem i"."""
    if any(k in t for k in SESSION_7):
        return 7
    if any(k in t for k in SESSION_5):
        return 5
    if any(k in t for k in SESSION_3):
        return 3
    if any(k in t for k in SESSION_1):
        return 1
    return None


def _session_form_answer(session):
    """Exam-form answer for one named session, or None."""
    if session == 7:
        return A_SEM7
    if session == 5:
        return A_SEM5
    if session == 3:
        return A_SEM3_FORM
    return None


def find_answer(text):
    """Return the answer for a message, or None if it is not a college question.

    INTENT FIRST (21 Nov 2026 fix): the question's intent is decided
    before its session. A session word must never hijack a fee / payment /
    admit-card / programme question into a last-date answer. Where the
    knowledge base only knows one session's fact (admit card is
    Sem-III-only), a Sem-V / Sem-VII question returns A_FALLBACK so the
    webhook's Gemini fallback can try the live website notices instead of
    answering with the wrong session's fact. Programme is per-session:
    Sem-III (link), Sem-VII (dates + link), Sem-V (new programme coming —
    the old one must NOT be quoted, and it must NOT fall back to the AI,
    which would quote the old programme from the live website).
    """
    t = text.lower()
    session = _session_of(t)
    # 1. Private data: refuse first, whatever else the message says.
    if any(k in t for k in ["roll", "रोल", "password", "पासवर्ड", "user id",
                            "userid", "user-id"]):
        return A_ROLL
    # 2. Teacher/sir number: give ONLY the link to the group message
    # that lists the numbers (Veeru's word: "ye link hi de dena").
    # The numbers themselves are never stored in the bot.
    _num = ("number" in t or "नंबर" in t or "no." in t
            or "contact" in t or "संपर्क" in t)
    _teacher = any(k in t for k in ["sir", "teacher", "principal",
                                    "madam", "professor", "सर", "शिक्षक",
                                    "प्राचार्य", "अध्यापक", "मैडम"])
    if _num and _teacher:
        return A_TEACHER_NUMBER
    # 3. Admission last date (Veeru's post 2097, 18 Nov 2026): no fixed
    # last date is announced. Word-level match (not exact phrases) so
    # Devanagari "लास्ट डेट" and other phrasings work too; but an
    # "exam form" question that merely mentions admission must NOT match.
    _adm = "admission" in t or "एडमिशन" in t
    _ld = any(k in t for k in LASTDATE_KW)
    _examform = any(k in t for k in FORM_KW)
    if _adm and _ld and not _examform:
        return A_ADMISSION_LASTDATE
    # 4. Last date (any topic): PMS -> its own window; a named session ->
    # that session's last date; otherwise all last dates.
    if _ld:
        if any(k in t for k in ["pms", "scholarship", "छात्रवृत्ति",
                                "छात्रवृति", "scholar"]):
            return A_PMS
        ans = _session_form_answer(session)
        if ans is not None:
            return ans
        if session == 1:
            # Sem-I (2026-30): no exam-form date announced yet — AI tries
            # the live notices instead of listing other sessions' dates.
            return A_FALLBACK
        return A_DEADLINES
    # 5. Admit Card: only Sem-III's status is known. Sem-I / Sem-V /
    # Sem-VII -> fallback (AI tries the live notices) instead of the
    # Sem-III answer.
    if any(k in t for k in ADMIT_KW):
        if session in (1, 5, 7):
            return A_FALLBACK
        return A_ADMIT
    # 6. Programme: per session. Sem-VII -> dates + channel link;
    # Sem-V -> new programme coming (canned, never the old dates, never
    # the AI fallback — the live website still lists the old programme).
    # Sem-I -> fallback; no session named -> Sem-III (the only one with
    # a posted programme).
    if any(k in t for k in PROGRAMME_KW):
        if session == 7:
            return A_PROGRAMME_VII
        if session == 5:
            return A_PROGRAMME_V
        if session == 1:
            return A_FALLBACK
        return A_PROGRAMME
    # 7. Fee: Rs. 800 for ALL sessions/semesters (Veeru's word, 21 Nov 2026).
    if any(k in t for k in FEE_KW):
        return A_FEE
    # 8. Payment: "nahi kat raha" -> validate-wait answer; "kat gaya" or
    # any other payment question -> fallback (AI), never the opposite answer.
    if any(k in t for k in PAYMENT_KW):
        if any(k in t for k in PAYMENT_NEG_KW):
            return A_PAYMENT
        return A_FALLBACK
    # 9. Ex-Student.
    if any(k in t for k in ["ex-student", "ex student", "exstudent",
                            "एक्स-स्टूडेंट"]):
        return A_EXSTUDENT
    # 10. PMS Scholarship.
    if any(k in t for k in ["pms", "scholarship", "छात्रवृत्ति",
                            "छात्रवृति", "scholar"]):
        return A_PMS
    # 11. Back paper: the canned answer is ONLY for "marksheet me ABSENT /
    # Back likha hai". A back-paper FORM / date question goes to the AI.
    # Bare "back" counts too when the marksheet context is there.
    _back = (any(k in t for k in BACK_KW) or "back" in t or "बैक" in t)
    if _back:
        if any(k in t for k in ["marksheet", "मार्कशीट", "absent",
                                "likha", "लिखा"]):
            return A_BACK
        if any(k in t for k in BACK_KW):
            return A_FALLBACK
    # 12. Special Exam.
    if any(k in t for k in ["special exam", "विशेष परीक्षा", "exam chhoot",
                            "exam chhut", "missed exam", "chhoot gaya",
                            "छूट गया", "chhut gaya"]):
        return A_SPECIAL
    # 13. PMW / Pending result.
    if any(k in t for k in ["pmw", "pending", "पेंडिंग"]):
        return A_PMW
    # 14. Office timing (narrow: "college kab" alone is NOT office timing).
    if any(k in t for k in OFFICE_KW):
        return A_OFFICE
    # 15. Admission documents — must not hijack a question that merely
    # mentions admission while asking about the exam form / last dates.
    if _adm and not _examform and not _ld:
        return A_ADMISSION
    # 16. How to fill the exam form (online; steps + link on the website).
    if any(k in t for k in HOW_KW) and _examform:
        return A_FORM_HOW
    # 17. Exam form, generic: named session -> its answer, else all sessions.
    if _examform:
        ans = _session_form_answer(session)
        if ans is not None:
            return ans
        if session == 1:
            return A_FALLBACK
        return A_FORM_GENERIC
    if any(k in t for k in COLLEGE_KEYWORDS):
        return A_FALLBACK
    return None


class handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - Vercel handler name
        self._json(200, {"ok": True,
                         "service": "wazirganj-group-bot-webhook"})

    def do_POST(self):  # noqa: N802 - Vercel handler name
        if WEBHOOK_SECRET:
            if self.headers.get("X-Telegram-Bot-Api-Secret-Token") \
                    != WEBHOOK_SECRET:
                self._json(403, {"ok": False, "error": "bad secret"})
                return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            update = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:  # noqa: BLE001
            self._json(400, {"ok": False, "error": "bad json"})
            return
        # Answer Telegram immediately, then do the work: the AI fallback
        # can take seconds, and a slow 200 makes Telegram retry the
        # update (duplicate replies). The handler still runs to completion
        # after the response is flushed.
        self._json(200, {"ok": True})
        try:
            if update.get("callback_query"):
                handle_callback(update["callback_query"])
            elif update.get("message"):
                handle_message(update["message"])
        except Exception as e:  # noqa: BLE001 - never fail the webhook
            print("telegram-webhook: handler error: %s" % type(e).__name__)

    def log_message(self, *args):  # keep Vercel logs clean
        pass
