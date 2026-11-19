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
- HYBRID AI (Gemini): canned answers always win. Only a college
  question the rules cannot answer (A_FALLBACK) goes to Gemini, grounded
  ONLY on the live website's notices (fetched at runtime). Gemini must
  answer in short formal Devanagari Hindi, or reply NAHI_PATA — then the
  bot falls back to A_FALLBACK. No key (GEMINI_API_KEY) = no AI, canned
  behaviour unchanged.
- Replies ONLY in the discussion group (GROUP_CHAT_ID). Never in
  private chats or other groups. Same rules as the polling bot:
  short formal Devanagari Hindi, only what was asked, no link previews,
  teacher numbers = group-message link only, never private data.
"""

import json
import os
import re
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
        with urllib.request.urlopen(req, timeout=20) as r:
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
        with urllib.request.urlopen(req, timeout=10) as r:
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
    prompt = ("NOTICES:\n" + "\n".join(notices)
              + "\n\nQUESTION: " + question)
    body = json.dumps({
        "system_instruction": {"parts": [{"text": AI_SYSTEM}]},
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
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001 - never log key/URL/body
        print("telegram-webhook: gemini call failed: %s" % type(e).__name__)
        return None
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError, TypeError):
        return None
    if not text or text == "NAHI_PATA" or len(text) > 600:
        return None
    if PHONE_RE.search(text):
        return None
    return text


def send_menu(chat_id, reply_to=None):
    return send_message(chat_id, MENU_TEXT, reply_to=reply_to,
                        markup=MENU_KEYBOARD)


def handle_callback(cq):
    sender = cq.get("from", {})
    if sender.get("is_bot"):
        return
    msg = cq.get("message") or {}
    chat = msg.get("chat", {})
    if chat.get("id") != GROUP_CHAT_ID:
        return
    data_cb = cq.get("data", "")
    answer_callback(cq["id"])
    if data_cb == "menu":
        text, markup = MENU_TEXT, MENU_KEYBOARD
    elif data_cb in CALLBACK_QUESTIONS:
        text = find_answer(CALLBACK_QUESTIONS[data_cb]) or A_FALLBACK
        markup = BACK_KEYBOARD
    else:
        return
    edit_message(chat["id"], msg.get("message_id"), text, markup=markup)


def handle_message(msg):
    chat = msg.get("chat", {})
    if chat.get("type") not in ("group", "supergroup"):
        return
    if chat.get("id") != GROUP_CHAT_ID:
        return
    sender = msg.get("from", {})
    if sender.get("is_bot"):
        return
    text = msg.get("text")
    if not text:
        return
    cmd = text.strip().split()[0].lower() if text.strip() else ""
    if cmd in ("/menu", "/menu@wcu_assistantbot"):
        send_menu(chat["id"], reply_to=msg.get("message_id"))
        return
    answer = find_answer(text)
    if answer is None:
        return
    if answer == A_FALLBACK:
        # Hybrid AI: only a college question the rules cannot answer
        # goes to Gemini; anything ungrounded falls back as before.
        ai = ai_answer(text)
        if ai:
            answer = ai
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
    "Session 2023-27 (Sem-VII) का Exam Form portal अभी नहीं खुला है। जैसे ही खुलेगा, सूचित कर दिया जाएगा — अभी college आने की आवश्यकता नहीं है। Last Date 19/11/2026 है।"
)

A_SEM5 = (
    "Session 2024-28 (Sem-V) के Exam Form की Last Date 05/12/2026 है।"
)

A_SEM3_FORM = (
    "Session 2025-29 (Sem-III) के Exam Form की Last Date 21/11/2026 है। इसके बाद कोई form accept नहीं किया जाएगा।"
)

A_PROGRAMME = (
    "Sem-III (Session 2025-29) का Exam Programme 26/11/2026 से 02/12/2026 तक है।"
)

A_ADMIT = (
    "Sem-III (Session 2025-29) का Admit Card अभी जारी नहीं हुआ है। Admit Card के लिए 75% attendance और Internal Exam में pass होना ज़रूरी है। जैसे ही जारी होगा, सूचित कर दिया जाएगा।"
)

A_FEE = "Sem-III (Session 2025-29) के Exam Form की fee Rs. 800 है।"

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
    "Admission के लिए चाहिए: Offer Letter, Aadhaar Card, Photo, 10th/12th Marksheet, TC, Caste Certificate, APAAR ID, Bank Passbook, माता-पिता का Aadhaar।"
)

A_BACK = (
    "अगर आपने सारे paper और practical दिए थे, फिर भी marksheet में ABSENT या Back in Paper लिखा है, तो तुरंत college से संपर्क करें।"
)

A_SPECIAL = (
    "जिन students का exam छूट गया है, उनका Special Exam लिया जाता है। Special Exam की date के लिए college से संपर्क करें।"
)

A_PMW = (
    "अगर आपका result PMW या PENDING है, तो एक application लिखकर पहले के semesters की marksheet और admit card लगाकर college में जमा करें।"
)

A_DEADLINES = (
    "Exam Form की Last Dates: Sem-III (2025-29) — 21/11/2026, Sem-V (2024-28) — 05/12/2026, Sem-VII (2023-27) — 19/11/2026। PMS Scholarship — 30/11/2026।"
)

A_FORM_GENERIC = (
    "Sem-V (Session 2024-28) के Exam Form की Last Date 05/12/2026 है, Sem-III (Session 2025-29) की 21/11/2026 है, और Sem-VII (Session 2023-27) का portal अभी नहीं खुला है (Last Date 19/11/2026)। Exam Form के लिए हमारी website देखें: wazirganjcollege.vercel.app"
)

A_FALLBACK = (
    "इस विषय में अभी जानकारी उपलब्ध नहीं है। College office से संपर्क करें।"
)

# ---------------------------------------------------------------------------
# /menu panel: inline-keyboard buttons in the group. /menu shows the panel;
# tapping a button edits that same message to the answer, with a Back button.
# Button answers reuse find_answer via a sample question, so button and
# text answers always stay identical.
# ---------------------------------------------------------------------------

MENU_TEXT = (
    "Wazirganj College Updates\n\n"
    "किस बारे में जानना है? नीचे से चुनें:\n\n"
    "Admin: Veeru Verma"
)

MENU_KEYBOARD = {"inline_keyboard": [
    [{"text": "📝 Exam Form Last Dates", "callback_data": "exam_dates"},
     {"text": "📅 Exam Programme", "callback_data": "programme"}],
    [{"text": "🎫 Admit Card", "callback_data": "admit"},
     {"text": "💰 Exam Fee", "callback_data": "fee"}],
    [{"text": "🎓 PMS Scholarship", "callback_data": "pms"},
     {"text": "📄 Admission Documents", "callback_data": "admission_docs"}],
    [{"text": "🗓️ Admission Last Date", "callback_data": "admission_last"},
     {"text": "👨‍🏫 Sir/Teacher Number", "callback_data": "teacher"}],
    [{"text": "🕐 Office Timing", "callback_data": "office"},
     {"text": "📋 All Last Dates", "callback_data": "all_dates"}],
]}

BACK_KEYBOARD = {"inline_keyboard": [
    [{"text": "⬅ Menu", "callback_data": "menu"}],
]}

# callback_data -> sample question (routed through find_answer).
CALLBACK_QUESTIONS = {
    "exam_dates": "exam form ki last date kya hai",
    "programme": "sem 3 ka exam programme",
    "admit": "admit card kab aayega",
    "fee": "exam form ki fee kitni hai",
    "pms": "pms scholarship ki last date kya hai",
    "admission_docs": "admission ke liye documents kya chahiye",
    "admission_last": "admission ki last date kya hai",
    "teacher": "sir ka number chahiye",
    "office": "college office kab khulta hai",
    "all_dates": "saari last dates batao",
}

# (keywords, answer) — first matching rule wins.
# Private-data refusal, Admit Card and Programme are handled in find_answer
# before these, because they are session-independent / need session checks.
RULES = [
    (["fee", "fees", "फीस", "शुल्क"], A_FEE),
    (["office", "कार्यालय", "college open", "कॉलेज खुला", "college kab",
      "कॉलेज कब", "office kab", "कार्यालय कब"], A_OFFICE),
    (["payment", "पेमेंट", "भुगतान", "payment nahi", "kat nahi", "कट नहीं",
      "deduct", "pay nahi"], A_PAYMENT),
    (["ex-student", "ex student", "exstudent", "एक्स-स्टूडेंट"], A_EXSTUDENT),
    (["pms", "scholarship", "छात्रवृत्ति", "छात्रवृति", "scholar"], A_PMS),
    (["admission", "एडमिशन", "नामांकन", "document", "दस्तावेज"], A_ADMISSION),
    (["back in paper", "back paper", "backpaper", "बैक"], A_BACK),
    (["special exam", "विशेष परीक्षा", "exam chhoot", "exam chhut",
      "missed exam", "chhoot gaya", "छूट गया", "chhut gaya"], A_SPECIAL),
    (["pmw", "pending", "पेंडिंग"], A_PMW),
    (["deadline", "last date", "अंतिम तिथि", "last dates",
      "saari last", "सारी last"], A_DEADLINES),
    (["exam form", "परीक्षा फॉर्म", "examform", "form bhar", "फॉर्म भर",
      "form kaise", "form kab"], A_FORM_GENERIC),
]

SESSION_7 = ["2023-27", "2023 27", "sem-vii", "sem vii", "sem-7", "sem 7",
             "sem7", "semester 7", "vii", "7th sem", "7th semester"]
SESSION_5 = ["2024-28", "2024 28", "sem-v", "sem v", "sem-5", "sem 5",
             "sem5", "semester 5", "5th sem", "5th semester"]
SESSION_3 = ["2025-29", "2025 29", "sem-iii", "sem iii", "sem-3", "sem 3",
             "sem3", "semester 3", "3rd sem", "3rd semester"]
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
]


def find_answer(text):
    """Return the answer for a message, or None if it is not a college question."""
    t = text.lower()
    # 1. Private data: refuse first, whatever else the message says.
    if any(k in t for k in ["roll", "रोल", "password", "पासवर्ड", "user id",
                            "userid", "user-id"]):
        return A_ROLL
    # 1b. Admission last date (Veeru's post 2097, 18 Nov 2026): no fixed
    # last date is announced. Word-level match (not exact phrases) so
    # Devanagari "लास्ट डेट" and other phrasings work too; but an
    # "exam form" question that merely mentions admission must NOT match.
    _adm = "admission" in t or "एडमिशन" in t
    _ld = any(k in t for k in ["last date", "लास्ट डेट", "kab tak",
                               "कब तक", "antim", "अंतिम", "aakhri",
                               "आखिरी", "band kab", "बंद कब"])
    _examform = "exam form" in t or "परीक्षा फॉर्म" in t
    if _adm and _ld and not _examform:
        return A_ADMISSION_LASTDATE
    # 1c. Teacher/sir number: give ONLY the link to the group message
    # that lists the numbers (Veeru's word: "ye link hi de dena").
    # The numbers themselves are never stored in the bot.
    _num = ("number" in t or "नंबर" in t or "no." in t
            or "contact" in t or "संपर्क" in t)
    _teacher = any(k in t for k in ["sir", "teacher", "principal",
                                    "madam", "professor", "सर", "शिक्षक",
                                    "प्राचार्य", "अध्यापक", "मैडम"])
    if _num and _teacher:
        return A_TEACHER_NUMBER
    # 2. Admit Card is session-independent in the knowledge base.
    if any(k in t for k in ["admit", "एडमिट"]):
        return A_ADMIT
    # 3. Programme: only the Sem-III programme is known. A programme
    # question about Sem-VII / Sem-V honestly falls through to the fallback.
    if any(k in t for k in PROGRAMME_KW):
        if any(k in t for k in SESSION_7 + SESSION_5):
            return A_FALLBACK
        return A_PROGRAMME
    # 4. Session-specific exam-form answers.
    for kws, ans in [(SESSION_7, A_SEM7), (SESSION_5, A_SEM5),
                     (SESSION_3, A_SEM3_FORM)]:
        if any(k in t for k in kws):
            return ans
    # 5. Remaining topic rules, in order. The admission-documents rule
    # must not hijack a question that merely mentions admission while
    # asking about the exam form / last dates.
    admission_docs_ok = ("exam form" not in t and "last date" not in t
                         and "last dates" not in t)
    for keywords, answer in RULES:
        if answer is A_ADMISSION and not admission_docs_ok:
            continue
        if any(k in t for k in keywords):
            return answer
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
