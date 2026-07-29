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
import requests

import llm
import syllabus
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

KB = None
ME = {}

# --------------------------------------------------------------------- storage

def db():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.execute("""CREATE TABLE IF NOT EXISTS cache(
                    k TEXT PRIMARY KEY, q TEXT, a TEXT, ts INTEGER)""")
    c.execute("""CREATE TABLE IF NOT EXISTS usage(
                    uid TEXT, ts INTEGER)""")
    c.execute("""CREATE TABLE IF NOT EXISTS settings(
                    chat TEXT PRIMARY KEY, lang TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS stats(
                    k TEXT PRIMARY KEY, v INTEGER)""")
    c.execute("""CREATE TABLE IF NOT EXISTS qlog(
                    ts INTEGER, uid TEXT, name TEXT, uname TEXT,
                    chat TEXT, ctitle TEXT, ctype TEXT, q TEXT, kind TEXT)""")
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


def log_q(w, question, kind="q"):
    with db() as c:
        c.execute("INSERT INTO qlog VALUES(?,?,?,?,?,?,?,?,?)",
                  (int(time.time()), w["uid"], w["name"], w["uname"],
                   w["chat"], w["ctitle"], w["ctype"], question[:500], kind))
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

def tg(method, **params):
    for attempt in range(3):
        try:
            r = requests.post(f"{API}/{method}", json=params, timeout=70)
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
    tg("sendChatAction", chat_id=chat_id, action="typing")

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
3. Keep it exam-focused and compact: short paragraphs or bullets, bold the key terms.
   Aim for under 200 words unless the question needs more.
4. If the STUDY MATERIAL genuinely does not contain the answer, reply with EXACTLY
   this one token and nothing else — do not attempt a partial answer:
   {needweb}
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
   2 months of hard work for the aspirants, and invite them to join {glink}.
   If asked how to contact Him, give {link} and {phone}.
   Never say you are Gemini, Google, an LLM, or that you read PDFs or notes.
"""

SENTINEL = "OFF_TOPIC_Q"

# ===================== YAHAN SE APNE HISAB SE BADAL SAKTE HO =====================

OWNER_NAME = "Dr. P. Tripathi"
OWNER_GROUP = "@agriextprep"
GROUP_LINK = "https://t.me/AgriExtPrep"
OWNER_LINK = "https://t.me/asktripathii"
OWNER_PHONE = "+91 85779 16450"
PROMO_EVERY = int(os.environ.get("PROMO_EVERY", "10"))   # har N-ve sawaal par group promo

# Ajeeb / bedhanga sawaal — pehli aur doosri baar
WEIRD_HI = "iska answer to sirf Tripathi Sir de payenge, mai nahi 🙏"
WEIRD_EN = "Ask Tripathi Sir, only He can help you now 🙏"

# Baar-baar pareshan kare to (teesri baar se)
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
          f"👥 Group join karo: {GROUP_LINK}\n"
          f"💬 Sir se baat: {OWNER_LINK}")
WHO_EN = (f"<b>{OWNER_NAME}</b> — Admin of the {OWNER_GROUP} group 🌾\n\n"
          "He designed me and built me with <b>2 months of hard work</b>, "
          "just for you.\n"
          "And you people do nothing for Him 😌\n\n"
          f"👥 Join the group: {GROUP_LINK}\n"
          f"💬 Reach Sir: {OWNER_LINK}")

# "Sir se baat karni hai / contact"
CONTACT_MSG = (f"{OWNER_NAME} se seedhe baat karo 👇\n\n"
               f"💬 {OWNER_LINK}\n"
               f"📞 {OWNER_PHONE}\n"
               f"👥 Group: {GROUP_LINK}")

# Har 10 sawaal ke baad jawab ke neeche ye jud jaayega
PROMO_HI = f"———\n📣 Roz ke notes, doubts aur updates ke liye group join karo 👉 {GROUP_LINK}"
PROMO_EN = f"———\n📣 For daily notes, doubts and updates, join the group 👉 {GROUP_LINK}"

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
3. Keep it compact and exam-focused — bullets or short paragraphs, bold the key terms,
   under 220 words unless more is genuinely needed.
4. NEVER name your sources inside the answer. No "according to PIB", no citations,
   no URLs, no "as per the website". Just state the facts plainly.
5. Do not say the notes did not have it. Do not apologise. Start with the answer.
6. If you are genuinely unsure of a fact, say so in one short line rather than guessing.
7. NEVER say you are Gemini, Google, an AI model, or that you searched the internet.
   Your knowledge comes from {owner}."""

USER_TMPL = """STUDY MATERIAL:
=====
{context}
=====

QUESTION: {question}"""

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


def fixed_intent(q, lang):
    """Kuch sawaalon ka jawab pehle se tay hai — AI ko poochne ki zaroorat nahi."""
    en = is_english(q, lang)
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
_ABUSE = re.compile(r"""(?ix)\b(
    m[ae]d[ae]r?ch[o0]+d\w* | b[ae]h?[ae]nch[o0]+d\w* | bhench[o0]+d\w* | bsdk | bhsdk
  | bh[o0]+sd[ia]\w* | bh[o0]+sda\w* | ch[u+]t[iy]+a\w* | ch[u+]t[iy]e\w* | chutya\w*
  | g[a@]nd[u+]\w* | gaandu\w* | g[a@]{2}nd | ch[o0]+d[uo]? | ch[o0]+dn?a
  | l[a@]nd | l[a@]ud[ae] | l[a@]wd[ae] | jh[a@]{1,2}t\w* | r[a@]nd[iy]\w*
  | h[a@]r[a@]m[iy]\w* | k[a@]m[iy]n[ae]\w* | b[a@]kch[o0]d\w* | t[a@]tt[iy]
  | ch[u+]tt?[ae]d | m[uy]th\w* | b[o0]{2}bs? | p[e3]nis | v[a@]gin[a@] | p[o0]rn\w*
  | nud[e3]s? | xxx | s[e3]xy | h[o0]rny | fuck\w* | f[u\*]ck | fck | shit
  | b[i1]tch\w* | b[a@]st[a@]rd | [a@]ssh[o0]l[e3] | d[i1]ckh?[e3]?[a@]?d?
  | pu[s\$]{2}y | wh[o0]r[e3] | slut | cunt | m[o0]th[e3]rfuck\w*
)\b""")


def is_abusive(text):
    return bool(_ABUSE.search(text))


def user_qcount(uid, add=True):
    """User ne ab tak kitne sawaal poochhe (promo ke liye)."""
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS ucount(uid TEXT PRIMARY KEY, n INTEGER)")
        if add:
            c.execute("INSERT INTO ucount(uid,n) VALUES(?,1) "
                      "ON CONFLICT(uid) DO UPDATE SET n=n+1", (str(uid),))
        r = c.execute("SELECT n FROM ucount WHERE uid=?", (str(uid),)).fetchone()
    return r[0] if r else 0


def maybe_promo(answer, uid, en):
    """Har PROMO_EVERY-ve sawaal par group ka link jod do."""
    n = user_qcount(uid)
    if PROMO_EVERY > 0 and n % PROMO_EVERY == 0:
        return answer + "\n\n" + (PROMO_EN if en else PROMO_HI)
    return answer


def weird_count(uid, add=False):
    """Ek ghante me user ne kitni baar bakwaas ki."""
    now = int(time.time())
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS weird(uid TEXT, ts INTEGER)""")
        c.execute("DELETE FROM weird WHERE ts < ?", (now - 3600,))
        if add:
            c.execute("INSERT INTO weird VALUES(?,?)", (str(uid), now))
        return c.execute("SELECT COUNT(*) FROM weird WHERE uid=?", (str(uid),)).fetchone()[0]


def weird_reply(q, lang, uid):
    en = is_english(q, lang)
    n = weird_count(uid, add=True)
    if n >= 3:                                   # baar-baar pareshan kar raha hai
        return ANGRY_EN if en else ANGRY_HI
    return WEIRD_EN if en else WEIRD_HI


_SYL_RE = re.compile(r"(?i)\b(syllabus|syllabi|silabus|sylabus|paathyakram|"
                     r"exam\s*pattern|course\s*content|unit\s*[-–]?\s*\d{1,2})\b")


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
            "Send /syllabus 5 to get the full text of any unit.")


def answer_question(q, lang):
    """Stage 1: notes se. Stage 2: web/Google se. Ya SENTINEL agar sawaal bakwaas hai."""
    hits = KB.search(q, top_k=8)

    # syllabus ka sawaal ho to official syllabus sabse upar, aur notes ka hissa chhota
    if _SYL_RE.search(q):
        ctx = syllabus.context_for(q) + "\n\n---\n\n" + KB.build_context(hits, 3500)
    else:
        ctx = KB.build_context(hits)

    sysmsg = SYSTEM.format(name=BOT_NAME,
                           lang_rule=LANG_RULE.get(lang, LANG_RULE["auto"]),
                           needweb=NEEDWEB,
                           sentinel=SENTINEL, owner=OWNER_NAME, group=OWNER_GROUP,
                           link=OWNER_LINK, phone=OWNER_PHONE, glink=GROUP_LINK)
    if not ctx:
        ctx = "(no relevant material found)"
    out = llm.complete(sysmsg, USER_TMPL.format(context=ctx, question=q))
    flat = out.upper().replace(" ", "_")

    if SENTINEL in flat:
        return SENTINEL

    # ---- Stage 2: notes me nahi mila -> Google Search + apni knowledge
    if NEEDWEB in flat or not strip_sources(out):
        if not ALLOW_OUTSIDE:
            return ("This topic is not covered in our material yet. "
                    "Try asking about a related Extension topic 🙏")
        websys = WEB_SYSTEM.format(name=BOT_NAME, owner=OWNER_NAME,
                                   lang_rule=LANG_RULE.get(lang, LANG_RULE["auto"]))
        print(f"[web] falling back to search for: {q[:70]}", flush=True)
        try:
            out = llm.complete(websys, q, temperature=0.3, use_search=True)
            bump("web")
        except Exception as e:
            print(f"[web] grounded call failed ({e}) — trying plain", flush=True)
            out = llm.complete(websys, q, temperature=0.3, use_search=False)
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


QUIZ_SYS = """You are a question setter for Indian agriculture competitive exams
(ICAR NET / ARS / SRF). From the STUDY MATERIAL, create {n} multiple-choice questions.
Format each exactly like:

Q1. <question>
(a) ... (b) ... (c) ... (d) ...

After all questions, output a line "ANSWERS: 1-b, 2-d, ..." and nothing else.
Questions must be answerable from the STUDY MATERIAL. Use English."""


def make_quiz(topic, n=5):
    hits = KB.search(topic or "extension education", top_k=10)
    ctx = KB.build_context(hits, max_chars=8000)
    return llm.complete(QUIZ_SYS.format(n=n),
                        USER_TMPL.format(context=ctx, question=f"Topic: {topic or 'mixed'}"),
                        temperature=0.7)

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
/quiz &lt;topic&gt; — 5 MCQ practice questions
/lang auto|en|hi|hinglish — set answer language
/contact — reach Tripathi Sir
/stats — bot usage
/help — this message

<b>Tip:</b> the more specific your question, the sharper the answer. 👍

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
           "Send <code>/syllabus 5</code> for the full text of any unit.")
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

    # ---- commands
    m = re.match(r"^/(\w+)(?:@[\w_]+)?\s*(.*)$", text, re.S)
    if m:
        cmd, arg = m.group(1).lower(), m.group(2).strip()

        # ---- admin-only (sirf tumhe dikhenge, baaki ke liye chup)
        if cmd in ("log", "users", "find", "watch", "adminhelp", "diag"):
            if not is_admin(uid):
                return
            if cmd == "diag":
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
            tg("sendMessage", chat_id=chat,
               text=HELP.format(name=BOT_NAME, glink=GROUP_LINK),
               parse_mode="HTML", disable_web_page_preview=True); return
        if cmd == "sources":
            cmd_sources(chat, msg, uid); return
        if cmd in ("syllabus", "syl"):
            cmd_syllabus(chat, msg, arg); return
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
        if cmd == "quiz":
            if not rate_ok(uid):
                send(chat, "⏳ Hourly limit reached. Please try again in a while.",
                     reply_to=msg["message_id"]); return
            typing(chat)
            try:
                send(chat, make_quiz(arg), reply_to=msg["message_id"])
                bump("answered")
            except Exception as e:
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

    # ---- gaali / adult bhasha — sabse pehle, koi jawab nahi, sirf warning
    if is_abusive(text):
        weird_count(uid, add=True)
        log_q(w, text, "abuse"); notify_admin(w, text, "abuse")
        tg("sendMessage", chat_id=chat,
           text=ABUSE_EN if is_english(text, lang) else ABUSE_HI,
           parse_mode="HTML", reply_to_message_id=msg["message_id"])
        return

    # ---- pehle se tay jawab (owner, contact, "kahan se batate ho") — 0 API call
    fx = fixed_intent(text, lang)
    if fx:
        bump("answered")
        log_q(w, text, "info"); notify_admin(w, text, "info")
        tg("sendMessage", chat_id=chat, text=fx, parse_mode="HTML",
           disable_web_page_preview=True, reply_to_message_id=msg["message_id"])
        return

    en = is_english(text, lang)

    cached = cache_get(text, lang)
    if cached:
        bump("answered"); bump("cached")
        kind = "weird" if cached == SENTINEL else "q"
        log_q(w, text, kind); notify_admin(w, text, kind)
        if cached == SENTINEL:
            send(chat, weird_reply(text, lang, uid), reply_to=msg["message_id"]); return
        send(chat, maybe_promo(strip_sources(cached), uid, en),
             reply_to=msg["message_id"]); return

    if not rate_ok(uid):
        send(chat, "⏳ Only {} questions per hour. Please ask again a bit later 🙏"
             .format(RATE_PER_HOUR), reply_to=msg["message_id"]); return

    typing(chat)
    try:
        ans = answer_question(text, lang)
        cache_put(text, lang, ans)
        bump("answered")
        kind = "weird" if ans == SENTINEL else "q"
        log_q(w, text, kind); notify_admin(w, text, kind)
        if ans == SENTINEL:
            ans = weird_reply(text, lang, uid)
        else:
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
        {"command": "quiz", "description": "5 MCQ practice questions"},
        {"command": "lang", "description": "auto | en | hi | hinglish"},
        {"command": "contact", "description": "Reach Tripathi Sir"},
        {"command": "stats", "description": "Bot usage"},
        {"command": "help", "description": "How to use this bot"},
    ])
    tg("deleteWebhook", drop_pending_updates=True)

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
                if msg:
                    try:
                        handle(msg)
                    except Exception:
                        traceback.print_exc()
        except requests.exceptions.ReadTimeout:
            continue
        except Exception as e:
            print("[loop]", e, flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
