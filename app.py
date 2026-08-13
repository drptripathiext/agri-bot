#!/usr/bin/env python3
"""
AgriBot — Telegram Q&A bot for agriculture-extension exam aspirants.
Answers from your own notes / PDFs / books (RAG), 100% free stack.

Required env vars:
    TELEGRAM_BOT_TOKEN   from @BotFather
    GEMINI_API_KEY       from https://aistudio.google.com/apikey
Optional:
    GROQ_API_KEY, OPENROUTER_API_KEY   (extra free fallbacks)
    BOT_NAME, ADMIN_ID, RATE_PER_HOUR, ALLOW_OUTSIDE_NOTES
"""
import os, re, io, json, time, html, sqlite3, hashlib, threading, traceback
from concurrent.futures import ThreadPoolExecutor
import requests

import llm
import syllabus
import interview
import special
import mcq
from kb_search import KnowledgeBase

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}"
BOT_NAME = os.environ.get("BOT_NAME", "AgriBot")
ADMIN_ID = os.environ.get("ADMIN_ID", "").strip()
RATE_PER_HOUR = int(os.environ.get("RATE_PER_HOUR", "25"))
ALLOW_OUTSIDE = os.environ.get("ALLOW_OUTSIDE_NOTES", "1") == "1"
# Jawab ke neeche 📚 book ka naam dikhana hai ya nahi. Default: nahi.
SHOW_SOURCES = os.environ.get("SHOW_SOURCES", "0") == "1"
DB_PATH = os.environ.get("DB_PATH", "/tmp/agribot.db")

WORKERS = int(os.environ.get("WORKERS", "24"))
TOP_K = int(os.environ.get("TOP_K", "6"))            # kitne passages LLM ko bheje
CTX_CHARS = int(os.environ.get("CTX_CHARS", "5500"))  # kam context = tez jawab
ASK_NAME = os.environ.get("ASK_NAME", "1") == "1"

KB = None
ME = {}
BUSY = set()                      # jin users ka sawaal abhi chal raha hai
_busy_lock = threading.Lock()

# --------------------------------------------------------------------- storage

SCHEMA = """
CREATE TABLE IF NOT EXISTS cache(k TEXT PRIMARY KEY, q TEXT, a TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS usage(uid TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS settings(chat TEXT PRIMARY KEY, lang TEXT);
CREATE TABLE IF NOT EXISTS stats(k TEXT PRIMARY KEY, v INTEGER);
CREATE TABLE IF NOT EXISTS weird(uid TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS ucount(uid TEXT PRIMARY KEY, n INTEGER);
CREATE TABLE IF NOT EXISTS people(uid TEXT PRIMARY KEY, name TEXT, state TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS convo(uid TEXT, ts INTEGER, q TEXT, a TEXT);
CREATE INDEX IF NOT EXISTS ix_convo ON convo(uid, ts);
CREATE TABLE IF NOT EXISTS qlog(ts INTEGER, uid TEXT, name TEXT, uname TEXT,
                                chat TEXT, ctitle TEXT, ctype TEXT, q TEXT, kind TEXT);
CREATE INDEX IF NOT EXISTS ix_usage ON usage(uid, ts);
CREATE INDEX IF NOT EXISTS ix_weird ON weird(uid, ts);
CREATE INDEX IF NOT EXISTS ix_qlog ON qlog(ts);
"""

_local = threading.local()


def init_db():
    """Tables ek hi baar banao — har message par nahi."""
    c = sqlite3.connect(DB_PATH, timeout=30)
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    c.executescript(SCHEMA)
    c.commit()
    c.close()


def db():
    """Har thread ki apni connection — bar-bar connect karne ka time bachta hai."""
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        c.execute("PRAGMA busy_timeout=15000")
        c.execute("PRAGMA synchronous=NORMAL")
        _local.conn = c
    return c


def bump(key, n=1):
    with db() as c:
        c.execute("INSERT INTO stats(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=v+?",
                  (key, n, n))


def get_stat(key):
    with db() as c:
        r = c.execute("SELECT v FROM stats WHERE k=?", (key,)).fetchone()
    return r[0] if r else 0


# --------------------------------------------------------------- admin logging

KIND_ICON = {"q": "❓", "weird": "🤨", "abuse": "🚫", "info": "ℹ️", "quiz": "📝"}


def who(msg):
    """Message bhejne wale ka naam nikalo."""
    f = msg.get("from", {}) or {}
    name = " ".join(x for x in [f.get("first_name"), f.get("last_name")] if x) or "Unknown"
    ch = msg.get("chat", {}) or {}
    return {
        "uid": str(f.get("id", 0)),
        "name": name,
        "uname": f.get("username", ""),
        "chat": str(ch.get("id", 0)),
        "ctitle": ch.get("title", "") or "Private chat",
        "ctype": ch.get("type", ""),
    }


_trim = [0]


def log_q(w, question, kind="q"):
    with db() as c:
        c.execute("INSERT INTO qlog VALUES(?,?,?,?,?,?,?,?,?)",
                  (int(time.time()), w["uid"], w["name"], w["uname"],
                   w["chat"], w["ctitle"], w["ctype"], question[:500], kind))
        _trim[0] += 1
        if _trim[0] % 200 == 0:          # har message par nahi — kabhi-kabhi
            c.execute("DELETE FROM qlog WHERE rowid NOT IN "
                      "(SELECT rowid FROM qlog ORDER BY ts DESC LIMIT 3000)")


def watch_on():
    with db() as c:
        r = c.execute("SELECT lang FROM settings WHERE chat='__watch__'").fetchone()
    return (r[0] if r else "on") == "on"


def notify_admin(w, question, kind="q"):
    """Admin ke DM me live copy bhejo — ye permanent record hai."""
    if not ADMIN_ID or w["uid"] == ADMIN_ID:
        return
    if kind != "abuse" and not watch_on():
        return
    uname = f" (@{w['uname']})" if w["uname"] else ""
    place = ("💬 Private" if w["ctype"] == "private"
             else f"👥 {html.escape(w['ctitle'][:40])}")
    txt = (f"{KIND_ICON.get(kind, '❓')} <b>{html.escape(w['name'][:40])}</b>"
           f"{html.escape(uname)}\n"
           f"<code>{w['uid']}</code> · {place}\n\n"
           f"{html.escape(question[:800])}")
    tg("sendMessage", chat_id=ADMIN_ID, text=txt, parse_mode="HTML",
       disable_web_page_preview=True)


# ------------------------------------------------- pehli baar: naam poochho

ASK_NAME_MSG = ("👋 Welcome! Before we begin — <b>what should I call you?</b>\n\n"
                "<i>Just type your name.</i>")

_NOT_A_NAME = re.compile(r"(?i)\?|\b(what|why|how|when|where|which|who|kya|kaise|kyu|"
                         r"kaun|kab|kahan|explain|define|tell|batao|samjhao)\b")


def get_person(uid):
    with db() as c:
        r = c.execute("SELECT name, state FROM people WHERE uid=?",
                      (str(uid),)).fetchone()
    return r if r else (None, None)


def set_person(uid, name=None, state=None):
    with db() as c:
        c.execute("INSERT INTO people(uid,name,state,ts) VALUES(?,?,?,?) "
                  "ON CONFLICT(uid) DO UPDATE SET "
                  "name=COALESCE(?,name), state=?, ts=?",
                  (str(uid), name, state, int(time.time()), name, state,
                   int(time.time())))


def clean_name(t):
    t = re.sub(r"(?i)^(my name is|mera naam|main|mai|i am|i'm|this is|naam)\s+", "", t.strip())
    t = re.sub(r"(?i)\s+(hai|hu|hoon|h)\.?$", "", t).strip(" .!,​")
    t = re.sub(r"\s+", " ", t)
    return t[:40]


# ------------------------------------------------- baat ka silsila (follow-up)

CONVO_TTL = int(os.environ.get("CONVO_TTL", "2700"))     # 45 minute
CONVO_TURNS = int(os.environ.get("CONVO_TURNS", "3"))    # kitne purane sawaal yaad


def save_turn(uid, q, a):
    now = int(time.time())
    with db() as c:
        c.execute("INSERT INTO convo VALUES(?,?,?,?)",
                  (str(uid), now, q[:400], a[:1500]))
        c.execute("DELETE FROM convo WHERE ts < ?", (now - CONVO_TTL,))
        c.execute("DELETE FROM convo WHERE uid=? AND rowid NOT IN "
                  "(SELECT rowid FROM convo WHERE uid=? ORDER BY ts DESC LIMIT ?)",
                  (str(uid), str(uid), CONVO_TURNS))


def history(uid):
    cut = int(time.time()) - CONVO_TTL
    with db() as c:
        rows = c.execute("SELECT q, a FROM convo WHERE uid=? AND ts>=? "
                         "ORDER BY ts DESC LIMIT ?",
                         (str(uid), cut, CONVO_TURNS)).fetchall()
    return list(reversed(rows))


def clear_convo(uid):
    with db() as c:
        c.execute("DELETE FROM convo WHERE uid=?", (str(uid),))


# "iska matlab?", "aur detail do", "why?", "example do" — ye pichle sawaal se jude hain
_FOLLOWUP = re.compile(r"""(?ix)
    ^\s*(
        (aur|और|and)\b | (iska|isaka|uska|usaka|iske|uske|isme|usme|isko|usko|inka|unka)\b
      | (ye|yeh|wo|woh|this|that|it|they|these|those)\b
      | (why|how|when|where|which|who)\s*[?.]?\s*$
      | (matlab|meaning|arth|samjhao|samjha|explain|elaborate|detail|details|
         example|examples|udaharan|difference|compare|briefly|shortly|short\s*me)\b
      | (more|zyada|thoda|aage|phir|next|continue|go\s*on|ok|okay|haan|han|hmm)\b
      | (kyu|kyun|kyon|kaise|kab|kahan|kaun)\s*[?.]?\s*$
      | (source|proof|reference|kaha\s*likha)\b
      | (uske|iske)\s*(alawa|baad|pehle|bare)
      | (repeat|dobara|dubara|firse|fir\s*se)\b
    )
  | ^\s*[a-zऀ-ॿ ]{1,18}\s*\?\s*$
""")


def is_followup(text, hist):
    """0 = naya sawaal · 1 = shayad juda hua · 2 = pakka follow-up"""
    if not hist:
        return 0
    t = text.strip()
    if len(t) <= 3:
        return 0
    if _FOLLOWUP.search(t) and not re.search(r"\b[A-Z]{2,}\b", t):
        return 2                       # "iska matlab?", "aur detail do", "why?"
    if len(t) < 60:
        return 1                       # chhota sawaal — context saath rakho, par
    return 0                           # dhoondhne me use mat karo


def norm_q(q):
    q = re.sub(r"[^a-z0-9ऀ-ॿ]+", " ", q.lower())
    return " ".join(q.split())


def cache_get(q, lang):
    k = hashlib.sha1((lang + "|" + norm_q(q)).encode()).hexdigest()
    with db() as c:
        r = c.execute("SELECT a FROM cache WHERE k=?", (k,)).fetchone()
    return r[0] if r else None


def cache_put(q, lang, a):
    k = hashlib.sha1((lang + "|" + norm_q(q)).encode()).hexdigest()
    with db() as c:
        c.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?,?)", (k, q, a, int(time.time())))


def rate_ok(uid):
    now = int(time.time())
    with db() as c:
        c.execute("DELETE FROM usage WHERE ts < ?", (now - 3600,))
        n = c.execute("SELECT COUNT(*) FROM usage WHERE uid=?", (str(uid),)).fetchone()[0]
        if n >= RATE_PER_HOUR and str(uid) != ADMIN_ID:
            return False
        c.execute("INSERT INTO usage VALUES(?,?)", (str(uid), now))
    return True


def chat_lang(chat_id, set_to=None):
    with db() as c:
        if set_to:
            c.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (str(chat_id), set_to))
            return set_to
        r = c.execute("SELECT lang FROM settings WHERE chat=?", (str(chat_id),)).fetchone()
    return r[0] if r else "auto"

# --------------------------------------------------------------------- telegram

TG = requests.Session()
TG.mount("https://", requests.adapters.HTTPAdapter(
    pool_connections=32, pool_maxsize=64, max_retries=0))


def tg(method, **params):
    for attempt in range(3):
        try:
            r = TG.post(f"{API}/{method}", json=params, timeout=70)
            d = r.json()
            if d.get("ok"):
                return d["result"]
            if d.get("error_code") == 429:
                time.sleep(d.get("parameters", {}).get("retry_after", 3))
                continue
            print(f"[tg] {method} -> {d}", flush=True)
            return None
        except Exception as e:
            if attempt == 2:
                print(f"[tg] {method} error {e}", flush=True)
            time.sleep(2)
    return None


MD_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
MD_ITAL = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", re.S)
MD_CODE = re.compile(r"`([^`]+)`")


def to_html(text):
    """Markdown-ish -> Telegram HTML, safely escaped."""
    t = html.escape(text)
    t = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", t, flags=re.M)
    t = MD_BOLD.sub(r"<b>\1</b>", t)
    t = MD_CODE.sub(r"<code>\1</code>", t)
    t = MD_ITAL.sub(r"<i>\1</i>", t)
    return t


def send(chat_id, text, reply_to=None, html_mode=True):
    chunks = []
    while len(text) > 3800:
        cut = text.rfind("\n", 0, 3800)
        cut = cut if cut > 2000 else 3800
        chunks.append(text[:cut]); text = text[cut:]
    chunks.append(text)
    for i, ch in enumerate(chunks):
        p = {"chat_id": chat_id, "text": to_html(ch) if html_mode else ch,
             "disable_web_page_preview": True}
        if html_mode:
            p["parse_mode"] = "HTML"
        if reply_to and i == 0:
            p["reply_to_message_id"] = reply_to
        res = tg("sendMessage", **p)
        if res is None and html_mode:              # HTML parse failed -> plain
            p.pop("parse_mode", None)
            p["text"] = ch
            tg("sendMessage", **p)


def typing(chat_id):
    """Background me bhejo — jawab ka rasta block na ho."""
    threading.Thread(target=tg, args=("sendChatAction",),
                     kwargs={"chat_id": chat_id, "action": "typing"},
                     daemon=True).start()


def bg(fn, *a, **kw):
    """Logging waghairah jawab bhejne ke baad, background me."""
    threading.Thread(target=lambda: fn(*a, **kw), daemon=True).start()

# --------------------------------------------------------------------- prompts

LANG_RULE = {
    "auto": ("DEFAULT TO ENGLISH. Answer in clear, exam-style English unless the user "
             "clearly wrote otherwise. Only two exceptions: (a) the question is in "
             "Roman-script Hindi / Hinglish -> reply in Hinglish; (b) the question is in "
             "Devanagari script -> reply in Hindi. Anything else, including short or "
             "one-word questions, gets an ENGLISH answer."),
    "en": "Always reply in clear English.",
    "hi": "Hamesha Hindi (Devanagari script) me jawab do. Technical terms English me rakh sakte ho.",
    "hinglish": ("Hamesha Hinglish me jawab do (Roman script Hindi + English technical terms). "
                 "Simple, friendly, jaise ek senior aspirant samjha raha ho."),
}

SYSTEM = """You are {name}, a study assistant for Indian agriculture competitive exams
(ICAR NET / ARS / SRF / JRF / ASRB, Agricultural Extension & allied subjects).
You answer strictly from the STUDY MATERIAL given to you.

{lang_rule}

RULES:
1. Answer the question directly first, then give supporting detail.
2. Use the STUDY MATERIAL as your primary source. Quote exact facts, years, names,
   numbers and definitions from it — aspirants need exam-accurate detail.
3. BE BRIEF BY DEFAULT. Answer in under 120 words. Lead with the direct answer in one
   line, then 3-5 short bullets of exam-critical detail (years, full forms, names,
   figures). Bold the key terms. No introductions, no summaries, no "in conclusion".
   EXCEPTION: if the student asks to explain in detail / descriptive / "vistar se" /
   full explanation / short note / discuss — then write a long, well-structured,
   teaching-style answer instead (see the DETAIL instructions if they are given).
4. If the STUDY MATERIAL genuinely does not contain the answer, reply with EXACTLY
   this one token and nothing else — do not attempt a partial answer:
   {needweb}
   ALSO use this token when the question asks about anything CURRENT or RECENT —
   the latest scheme, a new programme, current affairs, this year's budget or
   figures, a recent launch, renaming or merger, "latest", "new", "recent",
   "current status", or any year from 2024 onward — unless the STUDY MATERIAL
   clearly and explicitly contains that up-to-date fact. Outdated information is
   worse than no information for an aspirant.
5. NEVER invent citations, years, scheme names or statistics.
6. NEVER reveal or name your sources. Do not write the name of any book, PDF, notes,
   chapter, unit, author or file. Do not write "according to the notes", "as per the
   material", "source", "context", "document" or anything similar. Just state the fact
   plainly, as if you already knew it.
7. No preamble like "Sure" or "Great question". Start with the answer.
8. OFF-TOPIC / NONSENSE FILTER — this overrides every other rule.
   If the message is NOT a genuine study question, reply with EXACTLY this one token
   and nothing else:
   {sentinel}
   Use it when the message is: gibberish or random characters; a joke, meme, flirting,
   or time-pass; personal questions about the user, you, or anyone's private life;
   abuse, insults or adult content; politics, cricket, movies, relationships, money
   advice; asking you to write code, essays or do unrelated tasks; or any topic with
   no connection to agriculture, extension, rural development, or competitive exams.
   ALWAYS GENUINE (never use the token for these): questions about the exam itself —
   syllabus, exam pattern, marking scheme, PYQ / previous year questions, cut-off,
   eligibility, how to prepare, book recommendations, subject codes, ASRB / ICAR NET /
   ARS / SRF / JRF / SMS / STO exam details.
   BE GENEROUS to real students: badly worded, short, spelling-mistake, one-word or
   Hinglish questions about ANY academic or agriculture/exam topic are GENUINE —
   answer those normally. Greetings like "hi", "thanks", "good morning" are also fine
   to answer briefly and warmly. Only use the token for clearly off-topic or weird messages.
9. ABOUT YOURSELF. If asked where your knowledge comes from, say it comes from
   {owner}'s knowledge — whatever He taught you. If asked who {owner} is or who made
   you, say He is the Admin of the {group} group who designed and built you with
   2 months of hard work for the aspirants, and invite them to join {glink} and to
   follow Him on LinkedIn at {linkedin}.
   If asked how to contact Him, give {link}, {phone} and {linkedin}.
   Never say you are Gemini, Google, an LLM, or that you read PDFs or notes.
10. OUR WEBSITE — {website}. It has the full syllabus, notes, free notes, mock tests,
   mini quizzes, test series, PYQs and exam updates. Mention it (with the full link)
   whenever the student asks about syllabus, exam pattern, mock test, test series,
   quiz, notes, study material or where to practise — and whenever you are not fully
   sure about an exam / syllabus / pattern detail, tell them to check {website}
   instead of guessing. Do not repeat the link in every answer, only where it helps.
"""

SENTINEL = "OFF_TOPIC_Q"

# ===================== YAHAN SE APNE HISAB SE BADAL SAKTE HO =====================

OWNER_NAME = "Dr. P. Tripathi"
OWNER_GROUP = "@agriextprep"
GROUP_LINK = "https://t.me/AgriExtPrep"
OWNER_LINK = "https://t.me/asktripathii"
OWNER_LINKEDIN = "https://www.linkedin.com/in/pramod-tripathii/"
OWNER_PHONE = "+91 85779 16450"
WEBSITE = os.environ.get("WEBSITE", "https://www.agriextprep.co.in")
WEBSITE_SHORT = "agriextprep.co.in"
PROMO_EVERY = int(os.environ.get("PROMO_EVERY", "5"))   # har N-ve sawaal par promo
# Website par kya-kya hai — jawab me isi bhasha me batana hai
WEBSITE_HAS = ("syllabus, notes, free notes, mock test, mini quiz, test series, "
               "PYQs and exam updates")

# --- Off-topic sawaal: 3 step escalation ---
# 1st baar — bilkul polite
POLITE_HI = ("Ye mera area nahi hai 😊 Main Agricultural Extension aur ASRB/ARS exam ki "
             "taiyari me help karta hoon.\nSyllabus se kuch bhi poochho — main hoon yahan!\n"
             f"🌐 Notes, mock test aur quiz: {WEBSITE}")
POLITE_EN = ("That is outside my area 😊 I help with Agricultural Extension and "
             "ASRB / ARS exam preparation.\nAsk me anything from the syllabus — I am here!")

# 2nd baar — halka sa taunt
WEIRD_HI = "iska answer to sirf Tripathi Sir de payenge, mai nahi 🙏"
WEIRD_EN = "Ask Tripathi Sir, only He can help you now 🙏"

# 3rd baar se — warning
ANGRY_HI = ("Bas karo ab 😤 Yahi karte rahoge to exam nahi niklega.\n"
            "Padhai par dhyan do — sawaal poochho, main jawab dunga.")
ANGRY_EN = ("Enough now 😤 Keep doing this and you will never clear the exam.\n"
            "Focus on your studies — ask me a real question and I will answer.")

# "Tum answer kahan se dete ho?"
FROM_HI = (f"{OWNER_NAME} ke knowledge se 🙏\n"
           "Jo unhone mujhe padhaya aur sikhaya, bas wahi se batata hoon.")
FROM_EN = (f"From {OWNER_NAME}'s knowledge 🙏\n"
           "Whatever He taught me — that is all I answer from.")

# "Tripathi Sir kaun hain?" / "tumhe kisne banaya?"
WHO_HI = (f"<b>{OWNER_NAME}</b> — Admin, {OWNER_GROUP} group 🌾\n\n"
          "Unhone hi mujhe design kiya aur <b>2 mahine ki mehnat</b> se banaya — "
          "sirf aap logon ke liye.\n"
          "Aur aap log unke liye kuch nahi karte 😌\n\n"
          f"🌐 Website: {WEBSITE}\n"
          f"👥 Group join karo: {GROUP_LINK}\n"
          f"💬 Sir se baat: {OWNER_LINK}\n"
          f"🔗 LinkedIn par follow karo: {OWNER_LINKEDIN}")
WHO_EN = (f"<b>{OWNER_NAME}</b> — Admin of the {OWNER_GROUP} group 🌾\n\n"
          "He designed me and built me with <b>2 months of hard work</b>, "
          "just for you.\n"
          "And you people do nothing for Him 😌\n\n"
          f"🌐 Website: {WEBSITE}\n"
          f"👥 Join the group: {GROUP_LINK}\n"
          f"💬 Reach Sir: {OWNER_LINK}\n"
          f"🔗 Follow Him on LinkedIn: {OWNER_LINKEDIN}")

# "Sir se baat karni hai / contact"
CONTACT_MSG = (f"{OWNER_NAME} se seedhe baat karo 👇\n\n"
               f"💬 Telegram: {OWNER_LINK}\n"
               f"📞 Phone: {OWNER_PHONE}\n"
               f"🔗 LinkedIn: {OWNER_LINKEDIN}\n"
               f"👥 Group: {GROUP_LINK}\n"
               f"🌐 Website: {WEBSITE}")

# Har kuch sawaal ke baad jawab ke neeche ye jud jaayega (group aur website baari-baari)
PROMO_HI = f"———\n📣 Roz ke notes, doubts aur updates ke liye group join karo 👉 {GROUP_LINK}"
PROMO_EN = f"———\n📣 For daily notes, doubts and updates, join the group 👉 {GROUP_LINK}"

PROMO_WEB_HI = (f"———\n🌐 Syllabus, notes, free notes, mock test, mini quiz, test series "
                f"aur PYQ — sab kuch yahan milega 👉 {WEBSITE}")
PROMO_WEB_EN = (f"———\n🌐 Syllabus, unit-wise notes, PYQs, timed mock tests, mini quizzes "
                f"and ARS Mains answer writing — all here 👉 {WEBSITE}")

_W = WEBSITE.rstrip("/")
WEBSITE_MSG = (f"🌐 <b>AgriExtPrep</b> — {_W}\n"
               "ASRB NET / ARS / SMS / ICAR AICE JRF-SRF · Agricultural Extension\n\n"
               "Sab kuch ek jagah 👇\n"
               f"📋 <b>Syllabus &amp; exam pattern</b> (free) — {_W}/asrb-extension-syllabus\n"
               f"📘 <b>Unit-wise notes</b> — {_W}/asrb-net-extension-notes\n"
               f"📄 <b>PYQs</b> (unit-wise) — {_W}/asrb-extension-pyqs\n"
               f"⏱️ <b>Mock tests</b> (+3 / −1, real pattern) — {_W}/asrb-extension-mock-tests\n"
               f"🖊️ <b>ARS Mains answer writing</b> (scientists se checked) — "
               f"{_W}/ars-mains-extension-education\n"
               f"📝 <b>Online tests &amp; mini quiz</b> — {_W}/pages/tests.html\n"
               f"🎁 <b>Free notes &amp; free tests</b> — {_W}/free-agricultural-extension-study-material\n\n"
               f"👉 Free account banao: {_W}/pages/register.html\n"
               f"👥 Group: {GROUP_LINK}")

# Jab syllabus / pattern / exam ka pura detail bot ke paas na ho
WEB_TIP_HI = (f"\n\n🌐 Poora syllabus, mock test, mini quiz, test series aur free notes "
              f"hamari website par hain 👉 {WEBSITE}")
WEB_TIP_EN = (f"\n\n🌐 The full syllabus, mock tests, mini quizzes, test series and free "
              f"notes are on our website 👉 {WEBSITE}")

# Gaali / adult bhasha par
ABUSE_HI = ("⚠️ Aisi bhasha yahan bilkul nahi chalegi.\n\n"
            "Aapke saare messages <b>record ho rahe hain</b> aur admin sab dekh rahe hain 👁️\n"
            "Ye pehli aur aakhri warning hai. Padhai par dhyan do.")
ABUSE_EN = ("⚠️ This language is not allowed here.\n\n"
            "All your messages are being <b>recorded</b> and the admins are watching 👁️\n"
            "Treat this as your first and final warning. Focus on your studies.")

# ================================================================================

NEEDWEB = "NEED_WEB_LOOKUP"

# Stage 2 — jab notes me jawab na mile: Google Search grounding ke saath
WEB_SYSTEM = """You are {name}, a study assistant for Indian agriculture competitive exams
(ICAR NET / ASRB NET / ARS / SRF / JRF / SMS / STO, Agricultural Extension & allied subjects).

{lang_rule}

The question was not covered in the student's own notes, so answer it now using
Google Search and your own knowledge.

RULES:
1. Prefer authoritative Indian sources: ICAR (icar.org.in), ASRB, PIB (pib.gov.in),
   Ministry of Agriculture & Farmers Welfare (agricoop.gov.in), MANAGE (manage.gov.in),
   NAARM, eGyanKosh / IGNOU, NIRD&PR, TNAU Agritech Portal, KVK portal, ICAR institute
   websites, and peer-reviewed extension literature.
2. Be exam-accurate: give exact years, full forms, names of committees, scheme names,
   ministries and figures. If a scheme has been renamed or merged, say the current status.
3. BE BRIEF — under 120 words. Direct answer first, then a few tight bullets with
   exact years, full forms, names and figures. Bold key terms. No preamble, no summary.
4. NEVER name your sources inside the answer. No "according to PIB", no citations,
   no URLs, no "as per the website". Just state the facts plainly.
5. Do not say the notes did not have it. Do not apologise. Start with the answer.
6. If you are genuinely unsure of a fact, say so in one short line rather than guessing.
7. NEVER say you are Gemini, Google, an AI model, or that you searched the internet.
   Your knowledge comes from {owner}.
8. OFF-TOPIC FILTER — overrides everything above. If the message is not a genuine
   study/exam question (gibberish, jokes, flirting, personal questions, abuse,
   politics, cricket, movies, relationships, money advice, unrelated tasks), reply
   with EXACTLY this one token and nothing else:
   {sentinel}
   Exam-related questions — syllabus, pattern, PYQ, cut-off, eligibility, schemes,
   current affairs in agriculture — are always GENUINE.
9. If the question is about syllabus, exam pattern, mock tests, test series, quizzes,
   notes or study material — or if you are not fully certain of an exam detail — point
   the student to {website} (write the full link) instead of guessing."""

USER_TMPL = """STUDY MATERIAL:
=====
{context}
=====
{history}
QUESTION: {question}"""

FOLLOWUP_NOTE = """
The question below may be a FOLLOW-UP to the conversation above. Words like "it",
"this", "ye", "iska", "uska", "why", "aur", "example", "matlab" refer to what was
just discussed — resolve them from the conversation and build on your previous
answer instead of repeating it.
If the question is clearly self-contained and about a NEW topic, ignore the
conversation above and answer it fresh.
"""


def convo_block(hist):
    """Pichli baat-cheet prompt ke liye."""
    if not hist:
        return ""
    lines = ["EARLIER IN THIS CONVERSATION:"]
    for q, a in hist:
        lines.append(f"Student: {q}")
        lines.append(f"You: {a[:700]}")
    return "=====\n" + "\n".join(lines) + "\n=====\n" + FOLLOWUP_NOTE

# --------------------------------------------------------------------- answering

# LLM kabhi-kabhi source ka naam likh deta hai — usko saaf kar do
_SRC_LINE = re.compile(
    r"(?im)^\s*(?:[📚📖🔖]\s*)?(?:source|sources|src|ref|reference|references|"
    r"citation|from|as per|according to|based on|study material|material|notes?|"
    r"scrot|srot|स्रोत|संदर्भ)\s*[:\-–—]\s*.+$")
_SRC_INLINE = re.compile(
    r"(?i)\s*\((?:source|ref|reference|from|as per|according to)\s*[:\-]?[^)]{0,80}\)")
_SRC_PHRASE = re.compile(
    r"(?i)\b(?:as per|according to|as (?:given|mentioned|stated|described) in|"
    r"from)\s+(?:the\s+)?(?:study\s+)?(?:material|notes?|book|chapter|unit|pdf|"
    r"document|context|handbook)[a-z ]{0,25}[,:]?\s*")
_EMOJI_LINE = re.compile(r"(?m)^\s*[📚📖🔖]\s*.*$")

# "Notes me nahi mila" type disclaimers — sirf phrase hatao, baaki jawab rakho
_D = (r"(?:notes?\s*(?:me|mein|m)\s*(?:ye|yeh|is\w*)?\s*nahi[n]?\s*(?:mila|hai|h)\b"
      r"|(?:ye|yeh|is\w*)\s*notes?\s*(?:me|mein|m)\s*nahi[n]?\s*(?:hai|h|mila)?\b"
      r"|(?:this|it|that|the\s+topic|this\s+topic)\s+is\s+not\s+"
      r"(?:in|mentioned\s+in|covered\s+in|found\s+in|available\s+in|present\s+in)\s+"
      r"the\s+(?:notes?|material|study\s*material|document|context)"
      r"|(?:the\s+)?(?:study\s+)?(?:material|notes?|context)\s+does\s+not\s+"
      r"(?:mention|contain|cover|include|have)\s*(?:this|it)?"
      r"|(?:based\s+on|from|as\s+per)\s+(?:my\s+)?general\s+knowledge"
      r"|general\s+knowledge\s+se)")

_DISCLAIM_LINE = re.compile(r"(?im)^\s*(?:⚠️\s*)?" + _D +
                            r"\s*[—\-–:,.]*\s*(?:general\s+knowledge\s+se)?\s*[:\-–—.]?\s*$")
_DISCLAIM_INLINE = re.compile(r"(?i)(?:⚠️\s*)?" + _D +
                              r"\s*[—\-–:,.]*\s*"
                              r"(?:however|but|phir\s*bhi|lekin|magar|still)?\s*[,.:—\-–]?\s*"
                              r"(?:[a-z ]{0,25}?[:])?\s*")


def _light_strip(text):
    t = _SRC_LINE.sub("", text)
    t = _EMOJI_LINE.sub("", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def strip_sources(text):
    light = _light_strip(text)
    t = _DISCLAIM_LINE.sub("", light)
    t = _DISCLAIM_INLINE.sub("", t)
    t = _SRC_INLINE.sub("", t)
    t = _SRC_PHRASE.sub("", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    # safety: agar safai me poora jawab hi mit gaya to halka wala rakho
    if len(t) < 15 <= len(light):
        t = light
    return (t[0].upper() + t[1:]) if t and t[0].islower() else t


# NOTE: yahan sirf wo shabd rakhe hain jo English me nahi hote
# ("me", "main", "sir", "form", "full" jaan-boojh kar nahi hain — wo English bhi hain)
_HINGLISH = set("""kya kyu kyun kyon kaise kaun kab kahan kitna kitne kitni batao
bataiye bataye samjhao samjhaiye samjha hai hain haan nahi nhi mujhe muje mera meri
tumhara tumhari aapka aapki tum aap ki ke ko mein aur bhi yeh woh iska uska unka
kro karo kre kare karna karke karta karti wala wali bhai bhaiya didi acha accha
achha thik theek matlab kripya kripaya arth paribhasha prakar labh mahatva antar
chahiye hota hoti hote raha rahi rahe diya diye gaya gayi liye kuch kuchh sab sabhi
padhna padhai taiyari sawaal jawab jaankari jankari tumhe tumko tujhe tera teri
tere mera meri mere hamara humara hamari humein hume kisne kisko kiska kisi banaya
banaye banane banata bataye bataiye dijiye nahin zyada jyada thoda bahut kaisa
kaisi kyunki kyuki lekin magar abhi phir jaise waise unka unko dena deta deti""".split())


def is_english(text, lang="auto"):
    """English reply chahiye ya Hindi/Hinglish wala — decide karo."""
    if lang == "en":
        return True
    if lang in ("hi", "hinglish"):
        return False
    if re.search(r"[ऀ-ॿ]", text):
        return False
    words = set(re.findall(r"[a-z]+", text.lower()))
    return not (words & _HINGLISH)


# ---- fixed-answer intents (LLM call ki zaroorat hi nahi — instant aur free) ----

_RE_FROM = re.compile(r"""(?ix)
    ( (kahan|kaha|kahaan|kidhar) \s* se \s* (ye|yeh|tu|tum|aap|bot|inf|jaankari|jankari)?
        \s* (answer|jawab|jwab|batat|bata|jaankari|jankari|padha|seekh|laat|late|deta|dete)
    | (answer|jawab|jwab|data|info|information) \s* (tum|aap|ye|yeh)? \s*
        (kahan|kaha|kahaan|kidhar) \s* se
    | where \s+ (do|did|does) \s+ (you|u) \s+ (get|find|learn|take|source)
    | how \s+ (do|did) \s+ (you|u) \s+ (know|learn)
    | (what|whats|what's) \s+ (is|are)? \s* (your|ur) \s+ (source|sources|data|knowledge)
    | (your|ur|tumhara|tumhare|aapka|apka) \s+ (source|sources|knowledge) \s+ (kya|what|hai)
    | (kis|kiske|kisse|kiski) \s* se? \s* (notes|book|knowledge|help) \s* se
    | trained \s+ on \s+ (what|kya)
    )""")

_RE_WHO = re.compile(r"""(?ix)
    tripathi (?! [^?.\n]{0,25} \b (reddy|management|principles) \b )
    [^?.\n]{0,30} \b (kaun|kon|koun|who|about|kya\s*karte|introduce|intro) \b
  | \b (kaun|kon|who) \b [^?.\n]{0,30} tripathi
  | (tumhe|tumko|tujhe|aapko) \s* (kisne|kaun\s*ne) \s* banaya
  | who \s+ (made|created|built|designed) \s+ (you|u|this\s*bot)
  | (tumhara|tumhare|aapka|apka) \s+ (owner|malik|admin|creator|banane\s*wala)
  | (bot|tum|aap) \s* (ko)? \s* kisne \s* banaya""")

_RE_CONTACT = re.compile(r"""(?ix)
    ( (contact|sampark|number|whatsapp|mobile|phone|call|baat|message|msg|milna|milna\s*hai|reach|dm)
      [^?.\n]{0,35} \b (tripathi|sir|owner|admin|creator|malik|aapse|tumse) \b
    | \b (tripathi|sir|owner|admin) \b [^?.\n]{0,35}
      (contact|sampark|number|whatsapp|mobile|phone|call|se\s*baat|message|msg|reach|dm)
    | how \s+ (can|do) \s+ i \s+ (contact|reach|message)
    )""")


# "website kya hai", "notes kahan milenge", "mock test / test series kahan hai"
_RE_SITE = re.compile(r"""(?ix)
    \b(your\s*website|our\s*website|apk?i\s*website|tumhari\s*website|
       website|web\s*site|agriextprep)\b
  | (mock\s*test s?|test\s*series|mini\s*quiz|free\s*notes|study\s*material|
     notes|pdf s?|material)
      [^?.\n]{0,25}
      \b(kahan|kaha|kahaan|kidhar|kaise|kese|milenge|milega|milti|milte|mil\s*sakte|
         where|how|link|chahiye|do|de\s*do|bhejo|send|available)\b
  | \b(kahan|kaha|kahaan|kidhar|where)\b [^?.\n]{0,25}
      \b(mock\s*test s?|test\s*series|mini\s*quiz|free\s*notes|notes|material|
         practice|quiz)\b
""")


def fixed_intent(q, lang):
    """Kuch sawaalon ka jawab pehle se tay hai — AI ko poochne ki zaroorat nahi."""
    en = is_english(q, lang)
    # sirf chhote, seedhe sawaal — "ATMA ke notes detail me do" jaisa sawaal
    # yahan nahi fansna chahiye
    if len(q) <= 75 and not _DETAIL_RE.search(q) and _RE_SITE.search(q):
        return WEBSITE_MSG
    if _RE_CONTACT.search(q):
        return CONTACT_MSG
    if _RE_WHO.search(q):
        return WHO_EN if en else WHO_HI
    if _RE_FROM.search(q):
        return FROM_EN if en else FROM_HI
    return None


# ---- gaali / adult bhasha ----
# NOTE: "sex" (sex ratio), "rape" (rapeseed), "bc" (backward class) jaan-boojh kar
# nahi rakhe — wo asli exam topics me aate hain.
# Isi tarah "land" (land grant college, land reform, land holding), "myth",
# "discuss", "asset" bhi kabhi gaali nahi maane jaayenge.
_ABUSE = re.compile(r"""(?ix)\b(
    m[ae]d[ae]r?ch[o0]+d\w* | b[ae]h?[ae]nch[o0]+d\w* | bhench[o0]+d\w* | bsdk | bhsdk
  | bh[o0]+sd[ia]\w* | bh[o0]+sda\w* | ch[u+]t[iy]+a\w* | ch[u+]t[iy]e\w* | chutya\w*
  | g[a@]nd[u+]\w* | gaandu\w* | g[a@]{2}nd | ch[o0]+d[uo]? | ch[o0]+dn?a
  | l[u+]nd | l[a@]ud[ae] | l[a@]wd[ae] | jh[a@]{1,2}t\w* | r[a@]nd[iy]\w*
  | h[a@]r[a@]m[iy]\w* | k[a@]m[iy]n[ae]\w* | b[a@]kch[o0]d\w* | t[a@]tt[iy]
  | ch[u+]tt?[ae]d | muth[hi]\w* | b[o0]{2}bs? | p[e3]nis | v[a@]gin[a@] | p[o0]rn\w*
  | nud[e3]s? | xxx | s[e3]xy | h[o0]rny | fuck\w* | f[u\*]ck | fck | shit
  | b[i1]tch\w* | b[a@]st[a@]rd | [a@]ssh[o0]l[e3] | d[i1]ckh?[e3]?[a@]?d?
  | pu[s\$]{2}y | wh[o0]r[e3] | slut | cunt | m[o0]th[e3]rfuck\w*
)\b""")


# Padhai ke shabd — inme se koi bhi ho to gaali ka shak khatam
_SAFE_WORDS = re.compile(r"""(?ix)\b(
    land\s*(grant|reform|reforms|holding|holdings|ceiling|use|tenure|army|records?)
  | (grant|college|university|act|reform|tenure|acre|hectare|farm|farmer|soil|
     crop|agri\w*|extension|rural|village|kisan|scheme|policy|exam|syllabus|
     unit|chapter|theory|model|concept|definition|myth|mythology)
)\b""")


def is_abusive(text):
    if not _ABUSE.search(text):
        return False
    # koi bhi padhai wala shabd mila -> ye gaali nahi, sawaal hai
    if _SAFE_WORDS.search(text):
        return False
    return True


def user_qcount(uid, add=True):
    """User ne ab tak kitne sawaal poochhe (promo ke liye)."""
    with db() as c:
        if add:
            c.execute("INSERT INTO ucount(uid,n) VALUES(?,1) "
                      "ON CONFLICT(uid) DO UPDATE SET n=n+1", (str(uid),))
        r = c.execute("SELECT n FROM ucount WHERE uid=?", (str(uid),)).fetchone()
    return r[0] if r else 0


def maybe_promo(answer, uid, en):
    """Har PROMO_EVERY-ve sawaal par promo — group aur website baari-baari."""
    n = user_qcount(uid)
    if PROMO_EVERY <= 0 or n % PROMO_EVERY:
        return answer
    if WEBSITE and WEBSITE.rstrip("/").lower() in answer.lower():
        return answer                       # jawab me pehle se website hai
    turn = (n // PROMO_EVERY) % 2           # 1st -> website, 2nd -> group, ...
    if turn == 1:
        promo = PROMO_WEB_EN if en else PROMO_WEB_HI
    else:
        promo = PROMO_EN if en else PROMO_HI
    return answer + "\n\n" + promo


def weird_count(uid, add=False):
    """Ek ghante me user ne kitni baar bakwaas ki."""
    now = int(time.time())
    with db() as c:
        c.execute("DELETE FROM weird WHERE ts < ?", (now - 3600,))
        if add:
            c.execute("INSERT INTO weird VALUES(?,?)", (str(uid), now))
        return c.execute("SELECT COUNT(*) FROM weird WHERE uid=?", (str(uid),)).fetchone()[0]


def weird_reply(q, lang, uid):
    """Pehle polite, phir taunt, phir warning."""
    en = is_english(q, lang)
    n = weird_count(uid, add=True)
    if n <= 1:
        return POLITE_EN if en else POLITE_HI
    if n == 2:
        return WEIRD_EN if en else WEIRD_HI
    return ANGRY_EN if en else ANGRY_HI


_SYL_RE = re.compile(r"(?i)\b(syllabus|syllabi|silabus|sylabus|paathyakram|"
                     r"exam\s*pattern|course\s*content|unit\s*[-–]?\s*\d{1,2})\b")

# "Latest / current" wale sawaal — inme notes purane ho sakte hain.
# Inke liye ek hi call me notes + Google dono bhej do (do call ka time bachta hai).
_CURRENT_RE = re.compile(r"""(?ix)
    \b(latest|newest|recent|recently|current|currently|today|
       aaj|abhi|naya|nayi|naye|taza|filhal|
       updated?|update|news|announced?|announcement|launched?|
       this\s*year|is\s*saal|budget|allocation)\b
  | \b20(2[4-9]|[3-9]\d)\b
  | \b(kaun|who)\s+(hai|is|hain|are)\b[^?.\n]{0,30}\b(dg|director|minister|chairman|secretary)\b
""")


def local_fallback(q):
    """AI fail ho jaye to bhi syllabus wale sawaal ka jawab file se de do."""
    if not _SYL_RE.search(q) or not syllabus.UNITS:
        return None
    m = re.search(r"unit\s*[-–]?\s*(\d{1,2})", q, re.I)
    if m:
        u = syllabus.get_unit(int(m.group(1)))
        if u:
            return f"📘 Unit {u[0]}: {u[1]}\n\n{u[2]}"
    # generic shabd hata do, warna "exam pattern" jaisa sawaal galat unit utha leta hai
    topic = re.sub(r"(?i)\b(tell|give|show|send|please|what|which|whats|about|"
                   r"exam|pattern|paper|marks|marking|scheme|syllabus|syllabi|"
                   r"course|content|full|detail|details|list|asrb|icar|net|ars|"
                   r"srf|jrf|sms|sto|batao|bata|chahiye|kya|hai|mujhe)\b", " ", q)
    if len(re.findall(r"[a-z]{4,}", topic.lower())) >= 1:
        hits = syllabus.find_units(topic, limit=1)
        if hits:
            n, t, b = hits[0]
            return f"📘 Unit {n}: {t}\n\n{b}"
    units = "\n".join(f"Unit {n} — {t}" for n, t, _ in syllabus.UNITS)
    return (f"📘 {syllabus.HEADER}\n\n{units}\n\n"
            "Send /syllabus 5 to get the full text of any unit."
            + WEB_TIP_EN)


def answer_interview(q, lang):
    """ARS/ASRB interview ke sawaal — scientist-mentor ki tarah jawab do."""
    sysmsg = interview.SYSTEM.format(
        name=BOT_NAME, blueprint=interview.BLUEPRINT,
        lang_rule=LANG_RULE.get(lang, LANG_RULE["auto"]))
    hits = KB.search(q, top_k=4)
    ctx = KB.build_context(hits, max_chars=3000)
    user = (f"REFERENCE MATERIAL (use only if relevant):\n{ctx}\n\n"
            f"STUDENT'S QUESTION: {q}") if ctx else q
    bump("interview")
    return strip_sources(llm.complete(sysmsg, user, temperature=0.4))


def answer_special(q, lang):
    """Uske liye alag andaaz — sawaal ka sahi jawab, apne tareeke se."""
    sysmsg = special.SYSTEM.format(
        name=special.NAME, first=special.FIRST, owner=OWNER_NAME,
        lang_rule=LANG_RULE.get(lang, LANG_RULE["auto"]))
    if special.is_birthday():
        sysmsg += special.BIRTHDAY_NOTE
    hits = KB.search(q, top_k=5)
    ctx = KB.build_context(hits, max_chars=4000)
    user = (f"REFERENCE MATERIAL (use it if her question is academic):\n{ctx}\n\n"
            f"SHE SAID: {q}") if ctx else f"SHE SAID: {q}"
    return strip_sources(llm.complete(sysmsg, user, temperature=0.75))


def answer_pyq(q, lang):
    """PYQ / MCQ ke sawaal — pehle asli bank se, na mile to question-bank files se."""
    # asli PYQ bank — turant, bina API call ke, asli answer key ke saath
    direct = mcq.search(q, limit=6)
    if len(direct) >= 3:
        bump("pyq")
        return (mcq.as_text(direct)
                + "\n\n———\n📝 To attempt these, send  `/quiz`")

    hits = KB.search(q, top_k=10, only_pyq=True) or KB.search(q, top_k=8)
    ctx = KB.build_context(hits, max_chars=7000)
    sysmsg = PYQ_SYS.format(name=BOT_NAME, website=WEBSITE,
                            lang_rule=LANG_RULE.get(lang, LANG_RULE["auto"]))
    bump("pyq")
    return strip_sources(llm.complete(
        sysmsg, f"QUESTION BANK:\n=====\n{ctx}\n=====\n\nSTUDENT ASKED: {q}",
        temperature=0.3))


def answer_strategy(q, lang):
    """Preparation / strategy / motivation — professor ki tarah."""
    sysmsg = STRATEGY_SYS.format(name=BOT_NAME, website=WEBSITE, glink=GROUP_LINK,
                                 lang_rule=LANG_RULE.get(lang, LANG_RULE["auto"]))
    units = "\n".join(f"Unit {n}: {t}" for n, t, _ in syllabus.UNITS)
    user = (f"OFFICIAL SYLLABUS UNITS:\n{units}\n\nSTUDENT ASKED: {q}"
            if units else q)
    bump("strategy")
    return strip_sources(llm.complete(sysmsg, user, temperature=0.5,
                                      max_tokens=1600))


def answer_question(q, lang, uid=None, w=None, chat_type="private", hist=None,
                    strong=False):
    """Stage 1: notes se. Stage 2: web/Google se. Ya SENTINEL agar sawaal bakwaas hai."""
    if interview.is_interview(q):
        return answer_interview(q, lang)
    if _MAINS_RE.search(q):
        return answer_mains(q, lang)
    if _PYQ_RE.search(q):
        return answer_pyq(q, lang)
    if is_strategy(q):
        return answer_strategy(q, lang)
    if _DETAIL_RE.search(q):
        return answer_detail(q, lang, hist)

    # PAKKA follow-up ho tabhi dhoondhne ke liye pichla sawaal bhi jodo
    hq = (hist[-1][0] + " " + q) if (hist and strong) else q
    hits = KB.search(hq, top_k=TOP_K)
    hblock = convo_block(hist)

    # syllabus ka sawaal ho to official syllabus sabse upar, aur notes ka hissa chhota
    if _SYL_RE.search(q):
        ctx = syllabus.context_for(q, 4000) + "\n\n---\n\n" + KB.build_context(hits, 2500)
    else:
        ctx = KB.build_context(hits, CTX_CHARS)

    # ---- "latest/current" sawaal: do call ki jagah EK call — notes + Google saath me
    weak = (not hits) or hits[0]["score"] < 15
    if ALLOW_OUTSIDE and not _SYL_RE.search(q) and (_CURRENT_RE.search(q) or weak):
        websys = WEB_SYSTEM.format(name=BOT_NAME, owner=OWNER_NAME, sentinel=SENTINEL,
                                   website=WEBSITE,
                                   lang_rule=LANG_RULE.get(lang, LANG_RULE["auto"]))
        user = (f"YOUR OWN NOTES — READ THESE FIRST. If they answer the question and "
                f"are still accurate, answer from them and do NOT search. Search only "
                f"if they are missing this fact or clearly out of date.\n{ctx}\n\n"
                f"{hblock}\nQUESTION: {q}") if ctx else (hblock + "\nQUESTION: " + q)
        print(f"[web] direct (current/weak): {q[:60]}", flush=True)
        bump("web")
        try:
            w1 = llm.complete(websys, user, temperature=0.3, use_search=True)
        except Exception as e:
            print(f"[web] grounded failed ({e}) — plain", flush=True)
            w1 = llm.complete(websys, user, temperature=0.3)
        if SENTINEL in w1.upper().replace(" ", "_"):
            return SENTINEL
        return strip_sources(w1) or SENTINEL

    sysmsg = SYSTEM.format(name=BOT_NAME,
                           lang_rule=LANG_RULE.get(lang, LANG_RULE["auto"]),
                           needweb=NEEDWEB,
                           sentinel=SENTINEL, owner=OWNER_NAME, group=OWNER_GROUP,
                           link=OWNER_LINK, phone=OWNER_PHONE, glink=GROUP_LINK,
                           linkedin=OWNER_LINKEDIN, website=WEBSITE)
    if not ctx:
        ctx = "(no relevant material found)"
    out = llm.complete(sysmsg, USER_TMPL.format(context=ctx, question=q,
                                                history=hblock))
    flat = out.upper().replace(" ", "_")

    if SENTINEL in flat:
        return SENTINEL

    # ---- Stage 2: notes me nahi mila -> Google Search + apni knowledge
    if NEEDWEB in flat or not strip_sources(out):
        if not ALLOW_OUTSIDE:
            return ("This topic is not covered in our material yet. "
                    "Try asking about a related Extension topic 🙏" + WEB_TIP_EN)
        websys = WEB_SYSTEM.format(name=BOT_NAME, owner=OWNER_NAME, sentinel=SENTINEL,
                                   website=WEBSITE,
                                   lang_rule=LANG_RULE.get(lang, LANG_RULE["auto"]))
        print(f"[web] falling back to search for: {q[:70]}", flush=True)
        try:
            out = llm.complete(websys, hblock + "\nQUESTION: " + q,
                               temperature=0.3, use_search=True)
            bump("web")
        except Exception as e:
            print(f"[web] grounded call failed ({e}) — trying plain", flush=True)
            out = llm.complete(websys, hblock + "\nQUESTION: " + q,
                               temperature=0.3, use_search=False)
            bump("web")

    out = strip_sources(out)
    if not out:
        return SENTINEL
    if SHOW_SOURCES:
        srcs = []
        for h in hits[:3]:
            if h["src"] not in srcs:
                srcs.append(h["src"])
        if srcs:
            out += "\n\n📚 " + ", ".join(s[:55] for s in srcs)
    return out


# ------------------------------------------------- descriptive / detail mode

# "detail me batao", "descriptive", "vistar se", "short note", "discuss"
_DETAIL_RE = re.compile(r"""(?ix)
    \b(in\s*detail|detailed|detail\s*(me|mein|se)|details?\s*me[ei]n?)\b
  | \bdescriptive(ly)?\b
  | \b(full|complete|comprehensive|elaborate|exhaustive)\s*
      (explanation|explain|answer|note|notes|detail|details)\b
  | \bexplain\s+(it\s+)?(fully|completely|properly|thoroughly|in\s+depth)\b
  | \bin\s*[-]?\s*depth\b | \bindepth\b
  | \b(long|big|bada|lamba)\s*(answer|jawab|note)\b
  | \bshort\s*note s?\b | \bwrite\s+(a\s+|short\s+)?note s?\b
  | \b(discuss|elucidate|expound|critically\s*(examine|analyse|analyze))\b
  | \b(vistar|vistaar|vistrit|vistritt)\b
  | \b(pur[ia]|poor[ia])\s*(tarah|detail|jankari|explanation)\b
  | \b(achhe|acche|ache)\s*se\s*(samjh|batao|bata|explain)\w*
  | \bsamjh[aa]?\s*(do|dijiye|deejiye|kar\s*do)\b
  | \bnotes?\s*(bana|banao|bnao|de\s*do|chahiye)\b
""")

DETAIL_TOP_K = int(os.environ.get("DETAIL_TOP_K", "12"))
DETAIL_CTX = int(os.environ.get("DETAIL_CTX", "12000"))
DETAIL_TOKENS = int(os.environ.get("DETAIL_TOKENS", "3000"))

DETAIL_SYS = """You are {name}, a senior professor of Agricultural Extension who has
taught ICAR NET / ASRB / ARS / SRF / JRF aspirants for years.

{lang_rule}

The student has asked for a DETAILED / DESCRIPTIVE explanation. Give them a full,
teaching-style answer — the kind of notes they could revise from and write in a
descriptive paper.

HOW TO WRITE:
1. Length: 500-800 words. Never a short answer. Never cut it off midway.
2. Structure it with clear bold headings, in this order (skip a heading only if it
   truly does not apply):
   **Meaning / Definition** — the standard definition, with the person who gave it
   and the year if known.
   **Background / Origin** — when, where, why it came, by whom, under which
   committee / Act / scheme.
   **Key features / Components / Elements** — numbered points, each explained in
   one or two lines, not just named.
   **Types / Classification / Stages / Steps** — with a one-line note on each.
   **Indian context / Example** — how it works in India, real institutes, schemes,
   states or KVK/ATMA-level examples.
   **Importance / Advantages** and **Limitations / Criticism** — both sides.
   **Exam pointers** — 4-6 crisp one-liners: exact years, full forms, names,
   figures, "who gave what", the facts that actually get asked.
3. Use the STUDY MATERIAL as your primary source. Quote its exact years, names,
   numbers and definitions. Add your own subject knowledge to fill genuine gaps,
   but never invent a citation, year or statistic.
4. Bold every key term the first time it appears. Use short paragraphs and bullets —
   this is read on a phone.
5. Teach, do not just list. After each technical point add a plain-language line so a
   weak student also understands.
6. NEVER name your sources — no book, PDF, notes, chapter, unit, author or file name.
   No "according to the material". Just state the facts as if you know them.
7. No preamble like "Sure" or "Great question". Start with the topic heading.
8. End with ONE line: the single thing they must remember for the exam.
9. If the topic is about syllabus, exam pattern, mock tests, test series or study
   material, also point them to {website}.
10. Never say you are Gemini, Google or an AI. Your knowledge comes from {owner}.
11. If the message is not a genuine study question at all (gibberish, abuse, jokes,
   personal or off-topic chat), reply with EXACTLY this one token and nothing else:
   {sentinel}"""


def answer_detail(q, lang, hist=None):
    """Lamba, professor-style descriptive jawab."""
    hits = KB.search(q, top_k=DETAIL_TOP_K)
    ctx = KB.build_context(hits, DETAIL_CTX)
    if _SYL_RE.search(q) and syllabus.UNITS:
        ctx = syllabus.context_for(q, 3500) + "\n\n---\n\n" + ctx
    sysmsg = DETAIL_SYS.format(name=BOT_NAME, owner=OWNER_NAME, website=WEBSITE,
                               sentinel=SENTINEL,
                               lang_rule=LANG_RULE.get(lang, LANG_RULE["auto"]))
    hblock = convo_block(hist)
    user = USER_TMPL.format(context=ctx or "(no relevant material found)",
                            question=q, history=hblock)
    weak = (not hits) or hits[0]["score"] < 12
    bump("detail")
    try:
        out = llm.complete(sysmsg, user, temperature=0.35,
                           use_search=bool(weak and ALLOW_OUTSIDE),
                           max_tokens=DETAIL_TOKENS)
    except Exception as e:
        print(f"[detail] failed ({e}) — plain retry", flush=True)
        out = llm.complete(sysmsg, user, temperature=0.35,
                           max_tokens=DETAIL_TOKENS)
    if SENTINEL in out.upper().replace(" ", "_"):
        return SENTINEL
    return strip_sources(out) or SENTINEL


# ------------------------------------------------------------- ARS/ASRB MAINS

_MAINS_RE = re.compile(r"""(?ix)
    \bmains\b
  | \bmain\s*(exam|paper|examination)\b
  | \bdescriptive\s*(paper|exam|examination)\b
  | \banswer\s*writing\b
  | \bsubjective\s*(paper|exam|question)s?\b
""")

# Jin files me mains ka material hai
_MAINS_SRC = re.compile(r"(?i)(mains|model\s*paper|plan\s*with\s*pyq|question\s*bank|"
                        r"fully\s*syllabus)")

MAINS_SYS = """You are {name}, a senior ICAR scientist who evaluates ARS / ASRB MAINS
(descriptive) answer scripts in Agricultural Extension.

{lang_rule}

The QUESTION BANK below contains real ARS/ASRB Mains previous-year questions, unit-wise
mains question banks and model papers.

DECIDE WHAT THE STUDENT WANTS:
A) If they are ASKING FOR QUESTIONS (mains PYQs, mains questions on a topic, model
   paper, unit-wise mains questions):
   - Give a clean numbered list of 8-12 questions taken from the QUESTION BANK.
   - Keep each question exactly as it is asked in the paper, with its marks if shown.
   - Group them by unit or sub-topic with bold mini-headings where that helps.
   - Do NOT invent a previous-year question. If the bank is thin, say so in one short
     line and add well-framed questions clearly marked as **Practice**.
   - End with one line inviting them to ask for a model answer to any of them.

B) If they PASTED A MAINS QUESTION and want the answer:
   Write a FULL MODEL ANSWER, the way a topper writes it:
   - **Introduction / Definition** (2-3 lines, with the authority and year).
   - **Main body** under 3-5 bold sub-headings, each with crisp numbered points.
   - Include exact years, full forms, committee names, scheme names, Acts, figures,
     scientists and their contributions — this is what fetches marks.
   - Add an Indian example (ICAR institute, KVK, ATMA, a state, a scheme) wherever
     it fits, and a flow/diagram described in words if the topic allows.
   - **Conclusion** (2-3 lines, forward-looking).
   - Length by marks: 10 marks -> ~250 words, 15 marks -> ~400 words,
     20 marks -> ~600 words. If marks are not given, write ~450 words.
   - End with **Value addition:** — 3-4 one-line facts that lift the answer above average.

RULES:
1. Never name the source file, book, paper or notes.
2. No preamble. Start with the first question or the answer heading.
3. Never invent citations, years or statistics.
4. Bold key terms and headings. Short paragraphs — this is read on a phone.
5. Never say you are Gemini, Google or an AI.
6. Mention {website} once at the end if the student would benefit from the mock tests,
   test series or free notes there."""


def answer_mains(q, lang):
    """ARS / ASRB Mains — PYQ list ya pura model answer."""
    hits = KB.search(q, top_k=14)
    mains_hits = [h for h in hits if _MAINS_SRC.search(h.get("src", ""))]
    rest = [h for h in hits if not _MAINS_SRC.search(h.get("src", ""))]
    if len(mains_hits) < 4:
        extra = KB.search(q + " mains descriptive question", top_k=10)
        for h in extra:
            if _MAINS_SRC.search(h.get("src", "")) and h not in mains_hits:
                mains_hits.append(h)
    ctx = KB.build_context(mains_hits + rest, max_chars=11000)
    sysmsg = MAINS_SYS.format(name=BOT_NAME, website=WEBSITE,
                              lang_rule=LANG_RULE.get(lang, LANG_RULE["auto"]))
    bump("mains")
    out = llm.complete(
        sysmsg,
        f"QUESTION BANK:\n=====\n{ctx}\n=====\n\nSTUDENT ASKED: {q}",
        temperature=0.35, max_tokens=DETAIL_TOKENS)
    return strip_sources(out)


# ------------------------------------------------------------ PYQ / MCQ mode

_PYQ_RE = re.compile(r"""(?ix)
    \b(pyq|pyqs|p\.?y\.?q)\b
  | \bmcq s?\b | \bmcqs?\b
  | previous\s*(year|yr)s?\s*(question|paper|q)
  | \bpast\s*(paper|question)s?\b
  | (question|q)\s*(bank|paper|set)
  | \bobjective\s*question
  | \bone\s*liner s?\b
  | (asrb|icar|ars|srf|jrf|net)\s*(net\s*)?20\d\d\s*(paper|question|pyq)
""")

PYQ_SYS = """You are {name}, an examiner for Indian agriculture competitive exams
(ASRB NET / ARS / SRF / JRF / SMS / STO — Agricultural Extension).

{lang_rule}

The QUESTION BANK below contains real previous-year questions, mock tests and
one-liners. Use it to answer.

RULES:
1. If the student asks for previous-year questions on a topic, give them as a clean
   numbered list. For each: the question, then **Ans:** the correct answer in bold,
   and a one-line reason if it is not obvious.
2. If the student pastes a question and asks for the answer, give the correct option
   in bold first, then one or two lines of justification. If the material shows a
   different answer than you would give, trust the material's answer key.
3. Give 5-8 questions unless the student asks for a specific number.
4. Prefer questions that actually appear in the QUESTION BANK. Do not invent a
   previous-year question and label it as one. If the bank is thin on that topic,
   say so in one short line and add well-framed practice questions clearly marked
   as **Practice**.
5. Never name the source file, book or paper.
6. No preamble. Start with the first question.
7. End with one line telling them to use /quiz for an attemptable version, and mention
   that full mock tests, mini quizzes and test series are on {website}."""


# ------------------------------------------------------------ strategy mode

# Pakke signals — inhe akela hi kaafi maana jaata hai
_STRAT_STRONG = re.compile(r"""(?ix)
    \b(strategy|strategies|stratergy|startegy)\b
  | how\s+(to|do\s+i|should\s+i|can\s+i)\s+
      (prepare|study|crack|clear|pass|begin|approach|score|qualify)
  | (preparation|padhai|taiyari|tayari)\s*(kaise|kese|kaisi|plan|tips?|strategy)
  | \b(study|preparation|revision|time\s*table|timetable)\s*plan\b
  | (crack|clear|qualify|pass)\s+(the\s+)?(asrb|icar|ars|net|srf|jrf|exam)
  | \b(kitna|kitne)\s*(time|ghante|hours?|months?|mahine|din)\b
  | \b(book|books|source|sources)\s*(list|recommend|suggest|batao|kaun|konsi|kaunsi)
  | \bwhere\s+(do\s+i\s+)?start\b
  | \b(motivat|demotivat|give\s*up|giveup|nahi\s*ho\s*pa|dar\s*lag|confidence|
       hopeless|frustrat|burnout|thak\s*gaya|man\s*nahi\s*lag)\w*
""")

# Dheele signals — sirf tab jab exam/padhai ka context bhi ho
_STRAT_LOOSE = re.compile(r"""(?ix)
    kaise\b[^?.\n]{0,22}\b(kare|karu|karun|kru|karein|karna|padhu|padhe|padhna|
                            nikale|nikalu|nikalna|crack|clear|prepare|start|shuru)
  | \b(prepare|padhna|padhai|taiyari)\b
""")
_STRAT_CTX = re.compile(r"""(?ix)
    \b(exam|exams|asrb|icar|ars|net|srf|jrf|sms|sto|paper|syllabus|
       padhai|taiyari|tayari|preparation|study|selection|qualify|
       tayyari|revision|mock|pyq)\b
""")


def is_strategy(q):
    if _STRAT_STRONG.search(q):
        return True
    return bool(_STRAT_LOOSE.search(q) and _STRAT_CTX.search(q))

STRATEGY_SYS = """You are {name}, speaking as a senior ICAR Agricultural Scientist and
mentor who has cleared ASRB NET and ARS himself and has guided many students through it.

{lang_rule}

The student is asking about preparation, strategy, or is feeling low about the exam.
Respond like a professor who genuinely wants them to succeed — not like a website.

HOW TO ANSWER:
1. Be CONCRETE. Real numbers, real sequence, real time-splits. "Study hard" is useless;
   "Unit 1 and 9 carry the most questions — give them your first three weeks" is useful.
2. Structure it: where to start → what order → how much time → how to revise →
   how to test yourself. Use short bullets, bold the key actions.
3. Ground it in the actual ASRB Agricultural Extension syllabus (10 units) and in what
   the paper actually rewards: exact years, full forms, committee names, scheme details,
   Rogers, Bennett, research methodology, statistics.
4. Push them toward ACTIVE study — writing one-liners, solving PYQs, timed mocks,
   revision cycles — not passive reading.
5. Tell them what usually goes wrong: starting with the fattest book, no revision cycle,
   never attempting a full timed paper, ignoring statistics, and chasing new material
   instead of finishing what they have.
6. MOTIVATE — honestly, not cheaply. Acknowledge that this exam is hard and slow, that
   plateaus are normal, and that consistency beats intensity. If they sound low or
   defeated, address that FIRST, warmly, before any plan.
7. If they ask about the exam itself — pattern, marks, negative marking, eligibility,
   number of papers, Prelims vs Mains, cut-off, how the Mains descriptive paper is
   evaluated — answer as an insider: exact structure, exact marks, what the paper
   really tests, and where students lose marks. If you are not fully certain of a
   current official detail, say the current pattern should be confirmed on
   {website} rather than stating a wrong number.
8. Close with ONE thing they can do today. Just one.
9. Length: 250-400 words — enough to be genuinely useful, never a wall of text.
   No preamble, no "great question". Never name books, files or sources.
10. Point them to {website} for the full syllabus, free notes, mock tests, mini
   quizzes and test series, and to {glink} for daily doubts — naturally, in one line,
   at the end.
11. Never say you are an AI, Gemini or Google. You are their teacher."""


QUIZ_SYS = """You are a question setter for Indian agriculture competitive exams
(ASRB NET / ARS / SRF — Agricultural Extension). Using the STUDY MATERIAL, write
{n} exam-standard multiple-choice questions.

Reply with ONLY a JSON array. No markdown fence, no text before or after:

[{{"q":"question text","o":["option A","option B","option C","option D"],
  "a":0,"e":"one-line reason"}}]

RULES
- "a" is the 0-based index of the correct option.
- "q" max 250 characters. Each option max 90 characters. "e" max 180 characters.
- Exactly 4 options. Exactly one correct.
- Questions must be answerable from the STUDY MATERIAL — real facts, years, names,
  full forms, theorists, schemes. Wrong options must be plausible, not silly.
- English only. Valid JSON only."""


def make_quiz(topic, n=5):
    """Quiz banao. Pehle asli PYQ bank se (instant, sahi), na mile to AI se."""
    real = mcq.pick(topic, n)
    if len(real) >= min(n, 3):
        return [{"q": i["q"], "o": i["o"], "a": i["a"],
                 "e": (f"Asked in: {i['tag']}" if i.get("tag") else "")[:190]}
                for i in real]
    q = topic or "extension education"
    hits = KB.search(q, top_k=10, only_pyq=True) or KB.search(q, top_k=10)
    ctx = KB.build_context(hits, max_chars=7000)
    raw = llm.complete(QUIZ_SYS.format(n=n),
                       USER_TMPL.format(context=ctx, history="",
                                        question=f"Topic: {topic or 'mixed Extension'}"),
                       temperature=0.7)
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        raise ValueError("quiz JSON not found")
    items = json.loads(m.group())
    out = []
    for it in items:
        opts = [str(o)[:95] for o in (it.get("o") or [])][:10]
        try:
            ans = int(it.get("a", 0))
        except Exception:
            ans = 0
        if len(opts) < 2 or not (0 <= ans < len(opts)):
            continue
        out.append({"q": str(it.get("q", ""))[:290],
                    "o": opts, "a": ans, "e": str(it.get("e", ""))[:190]})
    if not out:
        raise ValueError("no valid quiz items")
    return out


def send_quiz(chat, items, reply_to=None):
    """Telegram ke native quiz poll — user tap karke attempt kar sakta hai."""
    sent_n = 0
    for it in items:
        r = tg("sendPoll", chat_id=chat, question=it["q"], options=it["o"],
               type="quiz", correct_option_id=it["a"],
               explanation=it["e"] or None, is_anonymous=False,
               reply_to_message_id=reply_to if sent_n == 0 else None)
        if r:
            sent_n += 1
        time.sleep(0.4)                 # Telegram ko saans lene do
    return sent_n

# --------------------------------------------------------------------- commands

HELP = """<b>{name}</b> — your study partner for ICAR NET / ARS / SRF / JRF 📚

Ask me anything from <b>Agricultural Extension</b> and allied subjects.
I answer in <b>English</b> by default — ask in Hindi or Hinglish and I will reply
in the same language.

<b>How to ask</b>
• <b>Private chat</b> — just type your question
• <b>In a group</b> — use <code>/ask your question</code>, or @mention me,
  or reply to any of my messages

<b>Commands</b>
/ask &lt;question&gt; — get an answer
/syllabus [unit] — official ASRB NET/ARS syllabus
/quiz [n] [topic] — attemptable MCQ practice, e.g. <code>/quiz 5 diffusion</code>
/topics — all PYQ topics you can practise
/interview — ARS / ASRB interview strategy
/mock [topic] — mock interview round (Extension by default)

<b>Just ask normally for</b>
• PYQs — <code>previous year questions on ATMA</code>
• Strategy — <code>how to prepare for ASRB NET?</code>
/website — syllabus, notes, mock tests, mini quiz, test series
/lang auto|en|hi|hinglish — set answer language
/contact — reach Tripathi Sir
/stats — bot usage
/help — this message

<b>Tip:</b> the more specific your question, the sharper the answer. 👍
Want a long answer? Just add <b>"in detail"</b> or <b>"descriptive"</b> to your question.

🌐 Website (syllabus, notes, free notes, mock tests, mini quizzes, test series):
{website}
👥 Join our group: {glink}"""


def cmd_sources(chat, msg, uid):
    """Book ke naam sirf admin ko. Baaki sabko sirf ginti."""
    srcs = KB.meta.get("sources", [])
    if ADMIN_ID and str(uid) == ADMIN_ID:
        txt = (f"📚 <b>{len(srcs)} sources</b> · {KB.meta.get('chunks', 0)} passages\n\n"
               + "\n".join("• " + html.escape(s[:70]) for s in srcs[:80]))
        if len(srcs) > 80:
            txt += f"\n… +{len(srcs) - 80} aur"
    else:
        txt = (f"📚 I have the full content of <b>{len(srcs)}</b> books and notes "
               f"(<b>{KB.meta.get('chunks', 0)}</b> passages).\nJust ask your question!")
    tg("sendMessage", chat_id=chat, text=txt[:4000], parse_mode="HTML",
       reply_to_message_id=msg["message_id"])


def cmd_syllabus(chat, msg, arg):
    m = re.search(r"\d{1,2}", arg or "")
    if m:
        u = syllabus.get_unit(int(m.group()))
        if u:
            txt = (f"📘 <b>{syllabus.HEADER}</b>\n\n"
                   f"<b>Unit {u[0]}: {html.escape(u[1])}</b>\n\n{html.escape(u[2])}")
            for i in range(0, len(txt), 3800):
                tg("sendMessage", chat_id=chat, text=txt[i:i + 3800], parse_mode="HTML")
            return
    txt = (f"📘 <b>{syllabus.HEADER}</b>\n\n{syllabus.unit_list()}\n\n"
           "Send <code>/syllabus 5</code> for the full text of any unit.\n\n"
           f"🌐 Full syllabus, mock tests, mini quizzes, test series and free notes: "
           f"{WEBSITE}")
    tg("sendMessage", chat_id=chat, text=txt, parse_mode="HTML",
       reply_to_message_id=msg["message_id"])


def is_admin(uid):
    return bool(ADMIN_ID) and str(uid) == ADMIN_ID


def _ago(ts):
    d = int(time.time()) - ts
    if d < 60:
        return f"{d}s"
    if d < 3600:
        return f"{d // 60}m"
    if d < 86400:
        return f"{d // 3600}h"
    return f"{d // 86400}d"


def cmd_log(chat, msg, arg):
    n = 15
    m = re.search(r"\d+", arg or "")
    if m:
        n = max(1, min(50, int(m.group())))
    with db() as c:
        rows = c.execute("SELECT ts,name,uname,ctitle,ctype,q,kind FROM qlog "
                         "ORDER BY ts DESC LIMIT ?", (n,)).fetchall()
    if not rows:
        tg("sendMessage", chat_id=chat, text="No questions logged yet."); return
    out = [f"🕘 <b>Last {len(rows)} questions</b>\n"]
    for ts, name, uname, ctitle, ctype, q, kind in rows:
        tag = f"@{uname}" if uname else name[:18]
        place = "DM" if ctype == "private" else ctitle[:16]
        out.append(f"{KIND_ICON.get(kind,'❓')} <b>{html.escape(tag)}</b> "
                   f"<i>{place}</i> · {_ago(ts)} ago\n{html.escape(q[:160])}\n")
    tg("sendMessage", chat_id=chat, text="\n".join(out)[:4000], parse_mode="HTML",
       disable_web_page_preview=True)


def cmd_users(chat, msg):
    with db() as c:
        rows = c.execute("""SELECT uid, MAX(name), MAX(uname), COUNT(*) n,
                                   SUM(kind='abuse'), SUM(kind='weird'), MAX(ts)
                            FROM qlog GROUP BY uid ORDER BY n DESC LIMIT 25""").fetchall()
        tot = c.execute("SELECT COUNT(*), COUNT(DISTINCT uid) FROM qlog").fetchone()
    if not rows:
        tg("sendMessage", chat_id=chat, text="No data yet."); return
    out = [f"👥 <b>{tot[1]} users · {tot[0]} questions</b>\n"]
    for uid, name, uname, n, ab, wd, ts in rows:
        tag = f"@{uname}" if uname else (name or uid)[:20]
        flags = ("  🚫" + str(ab) if ab else "") + ("  🤨" + str(wd) if wd else "")
        out.append(f"<b>{n:>3}</b>  {html.escape(tag)}  <i>{_ago(ts)}</i>{flags}")
    tg("sendMessage", chat_id=chat, text="\n".join(out)[:4000], parse_mode="HTML",
       disable_web_page_preview=True)


def cmd_find(chat, msg, arg):
    if not arg:
        tg("sendMessage", chat_id=chat, text="Usage: /find <word>"); return
    with db() as c:
        rows = c.execute("SELECT ts,name,uname,q,kind FROM qlog WHERE q LIKE ? "
                         "ORDER BY ts DESC LIMIT 20", (f"%{arg}%",)).fetchall()
    if not rows:
        tg("sendMessage", chat_id=chat, text=f"Nothing found for “{arg}”."); return
    out = [f"🔍 <b>{len(rows)} match(es) for “{html.escape(arg)}”</b>\n"]
    for ts, name, uname, q, kind in rows:
        tag = f"@{uname}" if uname else (name or "")[:18]
        out.append(f"{KIND_ICON.get(kind,'❓')} <b>{html.escape(tag)}</b> · "
                   f"{_ago(ts)} ago\n{html.escape(q[:160])}\n")
    tg("sendMessage", chat_id=chat, text="\n".join(out)[:4000], parse_mode="HTML",
       disable_web_page_preview=True)


def cmd_watch(chat, arg):
    a = (arg or "").strip().lower()
    if a in ("on", "off"):
        with db() as c:
            c.execute("INSERT OR REPLACE INTO settings VALUES('__watch__',?)", (a,))
        state = a
    else:
        state = "on" if watch_on() else "off"
    tg("sendMessage", chat_id=chat, parse_mode="HTML",
       text=(f"👁️ Live forwarding is <b>{state}</b>.\n"
             "Use <code>/watch on</code> or <code>/watch off</code>.\n"
             "<i>Abusive messages are always forwarded.</i>"))


ADMIN_HELP = """🔐 <b>Admin commands</b>

/log [n] — last n questions (default 15, max 50)
/users — who is asking, how much, and their flags
/find &lt;word&gt; — search everything ever asked
/watch on|off — live copy of every question in your DM
/diag — live API health check (use this if answers start failing)
/models — which Gemini models your key can use
/stats — totals

<b>Icons</b>
❓ normal · 🤨 off-topic · 🚫 abusive · ℹ️ about-the-bot

<i>Note: this log lives on the server and resets whenever Render restarts
the bot. Your DM copies (/watch on) are permanent — that is your real record.</i>"""


def cmd_stats(chat, msg, uid):
    txt = (f"📊 <b>{BOT_NAME}</b>\n"
           f"Questions answered: <b>{get_stat('answered')}</b>\n"
           f"From cache (free): <b>{get_stat('cached')}</b>\n"
           f"Answered from the web: <b>{get_stat('web')}</b>\n"
           f"Knowledge: <b>{KB.meta.get('chunks',0)}</b> passages from "
           f"<b>{len(KB.meta.get('sources',[]))}</b> sources")
    if is_admin(uid):
        with db() as c:
            n, u, ab, wd = c.execute(
                "SELECT COUNT(*), COUNT(DISTINCT uid), SUM(kind='abuse'), "
                "SUM(kind='weird') FROM qlog").fetchone()
        txt += (f"\n\n🔐 <b>Admin</b>\nLogged: <b>{n or 0}</b> questions from "
                f"<b>{u or 0}</b> users\nOff-topic: <b>{wd or 0}</b> · "
                f"Abusive: <b>{ab or 0}</b>\nLive forwarding: "
                f"<b>{'on' if watch_on() else 'off'}</b>  ·  /adminhelp")
    tg("sendMessage", chat_id=chat, text=txt, parse_mode="HTML",
       reply_to_message_id=msg["message_id"])

# --------------------------------------------------------------------- routing

def wants_reply(msg, text):
    """Group me spam na ho — sirf tabhi jawab do jab clearly poocha gaya ho."""
    chat_type = msg["chat"]["type"]
    if chat_type == "private":
        return True, text
    uname = ME.get("username", "")
    low = text.lower()
    if uname and f"@{uname.lower()}" in low:
        return True, re.sub(f"@{re.escape(uname)}", "", text, flags=re.I).strip()
    rt = msg.get("reply_to_message") or {}
    if rt.get("from", {}).get("id") == ME.get("id"):
        return True, text
    return False, text


def handle(msg):
    chat = msg["chat"]["id"]
    uid = msg.get("from", {}).get("id", 0)
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if not text:
        return

    w = who(msg)
    private = msg["chat"]["type"] == "private"

    # ---- pehli baar: sirf ek baar naam poochho (sirf private chat me)
    if ASK_NAME and private and not special.is_special(
            uid, w["name"], get_person(uid)[0] or "", "private"):
        name, state = get_person(uid)
        if state == "await_name" and not text.startswith("/"):
            nm = clean_name(text)
            if nm and len(nm) >= 2 and not _NOT_A_NAME.search(text) and len(text) <= 60:
                set_person(uid, name=nm, state="ok")
                tg("sendMessage", chat_id=chat, parse_mode="HTML",
                   disable_web_page_preview=True,
                   text=(f"Nice to meet you, <b>{html.escape(nm)}</b>! 🌾\n\n"
                         "Now ask me anything from Agricultural Extension — "
                         "concepts, schemes, previous-year topics, syllabus or "
                         "interview preparation.\n\n"
                         "Try: <code>What is ATMA?</code>  ·  <code>/syllabus</code>"
                         "  ·  <code>/interview</code>"))
                return
            # sawaal poochh liya naam ki jagah — Telegram wala naam le lo, aage badho
            set_person(uid, name=w["name"], state="ok")
        elif not name and not state:
            # commands ko mat roko — sirf /start par naam poochho
            if text.startswith("/") and not re.match(r"^/start\b", text, re.I):
                set_person(uid, name=w["name"], state="ok")
            else:
                set_person(uid, state="await_name")
                tg("sendMessage", chat_id=chat, text=ASK_NAME_MSG,
                   parse_mode="HTML", reply_to_message_id=msg["message_id"])
                return

    # ---- commands
    m = re.match(r"^/(\w+)(?:@[\w_]+)?\s*(.*)$", text, re.S)
    if m:
        cmd, arg = m.group(1).lower(), m.group(2).strip()

        # ---- admin-only (sirf tumhe dikhenge, baaki ke liye chup)
        if cmd in ("log", "users", "find", "watch", "adminhelp", "diag", "models"):
            if not is_admin(uid):
                return
            if cmd == "models":
                names = llm.list_models() or ["(could not fetch)"]
                tg("sendMessage", chat_id=chat, parse_mode="HTML",
                   text=("🧩 <b>Models on your key</b>\nCurrent: <code>"
                         + html.escape(llm.gemini_model()) + "</code>\n\n<pre>"
                         + html.escape("\n".join(names[:60]))
                         + "</pre>\n\nSet <code>GEMINI_MODEL</code> in Render "
                           "to force one."))
            elif cmd == "diag":
                typing(chat)
                try:
                    rep = llm.diagnose()
                except Exception as ex:
                    rep = f"diagnose crashed: {ex}"
                tg("sendMessage", chat_id=chat, parse_mode="HTML",
                   text=f"🩺 <b>API check</b>\n<pre>{html.escape(rep[:3500])}</pre>")
            elif cmd == "log":
                cmd_log(chat, msg, arg)
            elif cmd == "users":
                cmd_users(chat, msg)
            elif cmd == "find":
                cmd_find(chat, msg, arg)
            elif cmd == "watch":
                cmd_watch(chat, arg)
            else:
                tg("sendMessage", chat_id=chat, text=ADMIN_HELP, parse_mode="HTML")
            return

        if cmd in ("start", "help"):
            if special.is_special(uid, w["name"], get_person(uid)[0] or "",
                                  msg["chat"]["type"]):
                tg("sendMessage", chat_id=chat,
                   text=special.GREETING.format(first=special.FIRST))
                return
            tg("sendMessage", chat_id=chat,
               text=HELP.format(name=BOT_NAME, glink=GROUP_LINK, website=WEBSITE),
               parse_mode="HTML", disable_web_page_preview=True); return
        if cmd == "sources":
            cmd_sources(chat, msg, uid); return
        if cmd in ("syllabus", "syl"):
            cmd_syllabus(chat, msg, arg); return
        if cmd in ("topics", "topic"):
            rows = mcq.topic_list()
            body = "\n".join(f"<b>{n:>4}</b>  {html.escape(t)}" for t, n in rows)
            tg("sendMessage", chat_id=chat, parse_mode="HTML",
               reply_to_message_id=msg["message_id"],
               text=(f"📚 <b>{sum(n for _, n in rows)} previous-year questions</b>\n\n"
                     f"{body}\n\nPractice: <code>/quiz 5 gender</code>"))
            return
        if cmd in ("interview", "viva"):
            tg("sendMessage", chat_id=chat, text=interview.blueprint_summary(),
               parse_mode="HTML", disable_web_page_preview=True,
               reply_to_message_id=msg["message_id"])
            return
        if cmd == "mock":
            if not rate_ok(uid):
                send(chat, "⏳ Hourly limit reached. Please try again in a while.",
                     reply_to=msg["message_id"]); return
            typing(chat)
            try:
                topic = ("The candidate is from Agricultural Extension. "
                         + (f"Focus the questions on: {arg}." if arg else
                            "Cover the breadth of the Extension syllabus."))
                out = llm.complete(interview.MOCK_SYSTEM.format(topic_line=topic),
                                   "Conduct the mock interview now.", temperature=0.7)
                send(chat, strip_sources(out), reply_to=msg["message_id"])
                bump("answered"); bump("interview")
            except Exception:
                traceback.print_exc()
                send(chat, "😕 Could not build the mock round. Please try again.",
                     reply_to=msg["message_id"])
            return
        if cmd in ("website", "site", "web", "notes", "mocktest", "testseries"):
            tg("sendMessage", chat_id=chat, text=WEBSITE_MSG, parse_mode="HTML",
               disable_web_page_preview=False, reply_to_message_id=msg["message_id"])
            return
        if cmd in ("contact", "admin", "owner"):
            tg("sendMessage", chat_id=chat, text=CONTACT_MSG, parse_mode="HTML",
               disable_web_page_preview=True, reply_to_message_id=msg["message_id"])
            return
        if cmd == "stats":
            cmd_stats(chat, msg, uid); return
        if cmd == "lang":
            a = arg.lower()
            if a in LANG_RULE:
                chat_lang(chat, a)
                tg("sendMessage", chat_id=chat, text=f"✅ Language set to <b>{a}</b>",
                   parse_mode="HTML", reply_to_message_id=msg["message_id"])
            else:
                tg("sendMessage", chat_id=chat,
                   text="Usage: /lang auto | en | hi | hinglish")
            return
        if cmd in ("quiz", "practice", "test"):
            if not rate_ok(uid):
                send(chat, "⏳ Hourly limit reached. Please try again in a while.",
                     reply_to=msg["message_id"]); return
            typing(chat)
            m2 = re.match(r"^(\d+)\s*(.*)$", arg or "")
            n = max(1, min(10, int(m2.group(1)))) if m2 else 5
            topic = (m2.group(2) if m2 else arg).strip()
            if msg["chat"]["type"] != "private":
                n = min(n, 5)          # group me Telegram rate limit lag jaati hai
            try:
                items = make_quiz(topic, n)
                tg("sendMessage", chat_id=chat, parse_mode="HTML",
                   reply_to_message_id=msg["message_id"],
                   text=(f"📝 <b>{len(items)} questions</b>"
                         + (f" · {html.escape(topic[:60])}" if topic else "")
                         + "\nTap your answer — you will see the result instantly."))
                ok = send_quiz(chat, items)
                if not ok:
                    raise RuntimeError("polls not delivered")
                bump("answered"); bump("quiz")
            except Exception:
                traceback.print_exc()
                send(chat, "😕 Could not build the quiz right now. Please try again.",
                     reply_to=msg["message_id"])
            return
        if cmd in ("ask", "q", "p", "poochho"):
            text = arg
            if not text:
                tg("sendMessage", chat_id=chat,
                   text="Write it like this:  /ask What is ATMA?"); return
        else:
            return                              # unknown command -> ignore

    else:
        ok, text = wants_reply(msg, text)
        if not ok:
            return

    if len(text) < 3:
        return
    if len(text) > 900:
        text = text[:900]

    lang = chat_lang(chat)

    # ---- uske liye alag rasta: koi filter nahi, koi cache nahi, koi promo nahi
    her = special.is_special(uid, w["name"], get_person(uid)[0] or "",
                             msg["chat"]["type"])
    if her:
        typing(chat)
        try:
            send(chat, answer_special(text, lang), reply_to=msg["message_id"])
            bump("answered")
        except Exception:
            traceback.print_exc()
            send(chat, "Give me a moment and say that again? 🌸",
                 reply_to=msg["message_id"])
        return

    # ---- gaali / adult bhasha — sabse pehle, koi jawab nahi, sirf warning
    if is_abusive(text):
        weird_count(uid, add=True)
        bg(log_q, w, text, "abuse"); bg(notify_admin, w, text, "abuse")
        tg("sendMessage", chat_id=chat,
           text=ABUSE_EN if is_english(text, lang) else ABUSE_HI,
           parse_mode="HTML", reply_to_message_id=msg["message_id"])
        return

    # ---- pehle se tay jawab (owner, contact, "kahan se batate ho") — 0 API call
    fx = fixed_intent(text, lang)
    if fx:
        bump("answered")
        bg(log_q, w, text, "info"); bg(notify_admin, w, text, "info")
        tg("sendMessage", chat_id=chat, text=fx, parse_mode="HTML",
           disable_web_page_preview=True, reply_to_message_id=msg["message_id"])
        return

    en = is_english(text, lang)

    # ---- pichli baat yaad rakho: "iska matlab?", "aur detail do", "why?"
    hist = history(uid)
    rt = msg.get("reply_to_message") or {}
    if rt.get("from", {}).get("id") == ME.get("id") and rt.get("text"):
        # bot ke message par reply = pakka follow-up
        hist = (hist or []) + [("(earlier)", rt["text"][:900])]
    if rt.get("from", {}).get("id") == ME.get("id") and rt.get("text"):
        follow = 2                      # bot ko reply kiya = pakka follow-up
    else:
        follow = is_followup(text, hist)
    if not follow:
        hist = []                       # naya sawaal — purani baat bhool jao

    cached = None if follow else cache_get(text, lang)
    if cached:
        bump("answered"); bump("cached")
        kind = "weird" if cached == SENTINEL else "q"
        bg(log_q, w, text, kind); bg(notify_admin, w, text, kind)
        if cached == SENTINEL:
            send(chat, weird_reply(text, lang, uid), reply_to=msg["message_id"]); return
        ans = strip_sources(cached)
        save_turn(uid, text, ans)
        send(chat, maybe_promo(ans, uid, en), reply_to=msg["message_id"]); return

    if not rate_ok(uid):
        send(chat, "⏳ Only {} questions per hour. Please ask again a bit later 🙏"
             .format(RATE_PER_HOUR), reply_to=msg["message_id"]); return

    typing(chat)
    t0 = time.time()
    try:
        ans = answer_question(text, lang, uid, w, msg["chat"]["type"], hist,
                              follow >= 2)
        print(f"[time] {time.time()-t0:.1f}s{' [follow-up]' if follow else ''} "
              f"— {text[:50]}", flush=True)
        if not follow:                       # follow-up ka jawab cache mat karo
            cache_put(text, lang, ans)
        bump("answered")
        kind = "weird" if ans == SENTINEL else "q"
        bg(log_q, w, text, kind); bg(notify_admin, w, text, kind)
        if ans == SENTINEL:
            ans = weird_reply(text, lang, uid)
        else:
            save_turn(uid, text, ans)
            ans = maybe_promo(ans, uid, en)
        send(chat, ans, reply_to=msg["message_id"])
    except Exception as e:
        traceback.print_exc()
        # AI fail — phir bhi syllabus jaisa jawab local file se de do
        lf = local_fallback(text)
        if lf:
            send(chat, lf, reply_to=msg["message_id"])
        else:
            send(chat, "😕 Could not answer right now. Please try again in a minute.",
                 reply_to=msg["message_id"])
        # asli error admin ke DM me — debugging ke liye
        if ADMIN_ID:
            tg("sendMessage", chat_id=ADMIN_ID, parse_mode="HTML",
               text=("⚠️ <b>Answer failed</b>\n"
                     f"<b>Q:</b> {html.escape(text[:200])}\n"
                     f"<b>Error:</b> <code>{html.escape(str(e)[:600])}</code>"))

# --------------------------------------------------------------------- health server

def keep_awake():
    """
    Render ka free instance 15 min traffic na aaye to so jaata hai, aur phir
    agla message ~60 second lagta hai. Bot khud ko ping karta rahega.
    """
    url = (os.environ.get("SELF_URL")
           or os.environ.get("RENDER_EXTERNAL_URL")
           or "").strip().rstrip("/")
    if not url:
        print("[wake] SELF_URL not set — relying on external ping", flush=True)
        return
    print(f"[wake] self-ping every 8 min -> {url}", flush=True)
    while True:
        time.sleep(480)
        try:
            TG.get(url, timeout=20)
        except Exception as e:
            print("[wake]", e, flush=True)


def health_server():
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if KB is None:
                body = f"<h2>{BOT_NAME} starting…</h2>".encode()
            else:
                body = (f"<h2>{BOT_NAME} is running ✅</h2>"
                        f"<p>{KB.meta.get('chunks',0)} passages from "
                        f"{len(KB.meta.get('sources',[]))} sources</p>"
                        f"<p>Answered: {get_stat('answered')}</p>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    port = int(os.environ.get("PORT", "7860"))
    HTTPServer(("0.0.0.0", port), H).serve_forever()

# --------------------------------------------------------------------- main loop

def main():
    global KB, ME
    if not TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN missing")
    if not llm.have_any_key():
        raise SystemExit("No LLM key: set GEMINI_API_KEY (or GROQ_API_KEY / OPENROUTER_API_KEY)")

    # port pehle bind karo — warna Render/Koyeb jaise hosts deploy fail bata dete hain
    threading.Thread(target=health_server, daemon=True).start()
    init_db()
    threading.Thread(target=keep_awake, daemon=True).start()
    threading.Thread(target=llm.warmup, daemon=True).start()   # providers warm-up

    print("[kb] loading knowledge base…", flush=True)
    KB = KnowledgeBase()
    print(f"[kb] {KB.meta.get('chunks',0)} passages, "
          f"{len(KB.meta.get('sources',[]))} sources", flush=True)

    me = tg("getMe")
    if not me:
        raise SystemExit("Bad TELEGRAM_BOT_TOKEN")
    ME.update(me)
    print(f"[tg] connected as @{me.get('username')}", flush=True)

    tg("setMyCommands", commands=[
        {"command": "ask", "description": "Ask a question"},
        {"command": "syllabus", "description": "Official ASRB NET / ARS syllabus"},
        {"command": "interview", "description": "ARS / ASRB interview strategy"},
        {"command": "mock", "description": "Mock interview round (Extension)"},
        {"command": "quiz", "description": "Attemptable MCQ practice"},
        {"command": "topics", "description": "PYQ topics you can practise"},
        {"command": "lang", "description": "auto | en | hi | hinglish"},
        {"command": "website", "description": "Syllabus, notes, mock tests, test series"},
        {"command": "contact", "description": "Reach Tripathi Sir"},
        {"command": "stats", "description": "Bot usage"},
        {"command": "help", "description": "How to use this bot"},
    ])
    tg("deleteWebhook", drop_pending_updates=True)

    # ---- 20-30 log ek saath: har sawaal alag thread me, main loop kabhi ruke nahi
    pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="q")
    print(f"[bot] {WORKERS} worker threads ready", flush=True)

    def run(msg):
        uid = str((msg.get("from") or {}).get("id", 0))
        try:
            handle(msg)
        except Exception:
            traceback.print_exc()
        finally:
            with _busy_lock:
                BUSY.discard(uid)

    offset = None
    while True:
        try:
            r = requests.get(f"{API}/getUpdates",
                             params={"timeout": 50, "offset": offset,
                                     "allowed_updates": json.dumps(["message"])},
                             timeout=70)
            d = r.json()
            if not d.get("ok"):
                time.sleep(3); continue
            for upd in d["result"]:
                offset = upd["update_id"] + 1
                msg = upd.get("message")
                if not msg:
                    continue
                uid = str((msg.get("from") or {}).get("id", 0))
                text = (msg.get("text") or "").strip()
                # ek user ek waqt me ek hi sawaal — queue flood na ho
                with _busy_lock:
                    if uid in BUSY and not text.startswith("/"):
                        tg("sendMessage", chat_id=msg["chat"]["id"],
                           reply_to_message_id=msg["message_id"],
                           text="⏳ Still working on your previous question…")
                        continue
                    BUSY.add(uid)
                pool.submit(run, msg)
        except requests.exceptions.ReadTimeout:
            continue
        except Exception as e:
            print("[loop]", e, flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
