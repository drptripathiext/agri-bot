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
from kb_search import KnowledgeBase

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}"
BOT_NAME = os.environ.get("BOT_NAME", "AgriBot")
ADMIN_ID = os.environ.get("ADMIN_ID", "").strip()
RATE_PER_HOUR = int(os.environ.get("RATE_PER_HOUR", "25"))
ALLOW_OUTSIDE = os.environ.get("ALLOW_OUTSIDE_NOTES", "1") == "1"
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
    return c


def bump(key, n=1):
    with db() as c:
        c.execute("INSERT INTO stats(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=v+?",
                  (key, n, n))


def get_stat(key):
    with db() as c:
        r = c.execute("SELECT v FROM stats WHERE k=?", (key,)).fetchone()
    return r[0] if r else 0


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
    "auto": ("Reply in the SAME language and script the user used. "
             "Roman-Hindi (Hinglish) question -> answer in Hinglish. "
             "Devanagari question -> answer in Hindi. English question -> answer in English."),
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
4. If the STUDY MATERIAL does not contain the answer:{outside}
5. NEVER invent citations, years, scheme names or statistics.
6. Do not mention "context", "chunks" or "documents" — just answer naturally.
7. No preamble like "Sure" or "Great question". Start with the answer.
"""

OUTSIDE_YES = """
   start your reply with the line "⚠️ Notes me nahi mila — general knowledge se:" and then
   answer from your own knowledge, clearly and carefully."""

OUTSIDE_NO = """
   say politely that this topic is not in the notes yet, and suggest a related topic
   that IS covered. Do not answer from outside knowledge."""

USER_TMPL = """STUDY MATERIAL:
=====
{context}
=====

QUESTION: {question}"""

# --------------------------------------------------------------------- answering

def answer_question(q, lang):
    hits = KB.search(q, top_k=8)
    ctx = KB.build_context(hits)
    sysmsg = SYSTEM.format(name=BOT_NAME,
                           lang_rule=LANG_RULE.get(lang, LANG_RULE["auto"]),
                           outside=OUTSIDE_YES if ALLOW_OUTSIDE else OUTSIDE_NO)
    if not ctx:
        ctx = "(no relevant material found)"
    out = llm.complete(sysmsg, USER_TMPL.format(context=ctx, question=q))
    srcs = []
    for h in hits[:3]:
        s = h["src"]
        if s not in srcs:
            srcs.append(s)
    if srcs and "Notes me nahi mila" not in out:
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

HELP = """<b>{name}</b> — tumhare notes se padhne wala bot 📚

<b>Kaise poochho:</b>
• Private chat me — seedha sawaal likh do
• Group me — <code>/ask tumhara sawaal</code> ya bot ko @mention karo,
  ya bot ke message par reply karo

<b>Commands</b>
/ask &lt;sawaal&gt; — notes se jawab
/quiz &lt;topic&gt; — 5 MCQ practice questions
/lang auto|hi|en|hinglish — jawab ki bhasha
/sources — kaun kaun si books/notes bot ke paas hain
/stats — bot ka usage
/help — ye message

<b>Tip:</b> sawaal jitna specific hoga, jawab utna accurate. 👍"""


def cmd_sources(chat, msg):
    srcs = KB.meta.get("sources", [])
    txt = (f"📚 <b>{len(srcs)} sources</b> · {KB.meta.get('chunks', 0)} passages\n\n"
           + "\n".join("• " + html.escape(s[:70]) for s in srcs[:80]))
    if len(srcs) > 80:
        txt += f"\n… +{len(srcs) - 80} aur"
    tg("sendMessage", chat_id=chat, text=txt[:4000], parse_mode="HTML",
       reply_to_message_id=msg["message_id"])


def cmd_stats(chat, msg):
    txt = (f"📊 <b>{BOT_NAME}</b>\n"
           f"Questions answered: <b>{get_stat('answered')}</b>\n"
           f"From cache (free): <b>{get_stat('cached')}</b>\n"
           f"Knowledge: <b>{KB.meta.get('chunks',0)}</b> passages from "
           f"<b>{len(KB.meta.get('sources',[]))}</b> sources")
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

    # ---- commands
    m = re.match(r"^/(\w+)(?:@[\w_]+)?\s*(.*)$", text, re.S)
    if m:
        cmd, arg = m.group(1).lower(), m.group(2).strip()
        if cmd in ("start", "help"):
            tg("sendMessage", chat_id=chat, text=HELP.format(name=BOT_NAME),
               parse_mode="HTML"); return
        if cmd == "sources":
            cmd_sources(chat, msg); return
        if cmd == "stats":
            cmd_stats(chat, msg); return
        if cmd == "lang":
            a = arg.lower()
            if a in LANG_RULE:
                chat_lang(chat, a)
                tg("sendMessage", chat_id=chat, text=f"✅ Language set to <b>{a}</b>",
                   parse_mode="HTML", reply_to_message_id=msg["message_id"])
            else:
                tg("sendMessage", chat_id=chat,
                   text="Use: /lang auto | hi | en | hinglish")
            return
        if cmd == "quiz":
            if not rate_ok(uid):
                send(chat, "⏳ Thoda ruko — limit lag gayi. 1 ghante baad try karo.",
                     reply_to=msg["message_id"]); return
            typing(chat)
            try:
                send(chat, make_quiz(arg), reply_to=msg["message_id"])
                bump("answered")
            except Exception as e:
                send(chat, f"😕 Quiz nahi ban paaya: {e}", reply_to=msg["message_id"])
            return
        if cmd in ("ask", "q", "p", "poochho"):
            text = arg
            if not text:
                tg("sendMessage", chat_id=chat,
                   text="Aise likho: /ask ATMA kya hai?"); return
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

    cached = cache_get(text, lang)
    if cached:
        bump("answered"); bump("cached")
        send(chat, cached, reply_to=msg["message_id"]); return

    if not rate_ok(uid):
        send(chat, "⏳ Ek ghante me sirf {} sawaal. Thodi der baad poochho 🙏"
             .format(RATE_PER_HOUR), reply_to=msg["message_id"]); return

    typing(chat)
    try:
        ans = answer_question(text, lang)
        cache_put(text, lang, ans)
        bump("answered")
        send(chat, ans, reply_to=msg["message_id"])
    except Exception as e:
        traceback.print_exc()
        send(chat, "😕 Abhi jawab nahi de paaya (free API limit ya network issue). "
                   "Thodi der baad try karo.", reply_to=msg["message_id"])

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
        {"command": "ask", "description": "Notes se sawaal poochho"},
        {"command": "quiz", "description": "5 MCQ practice questions"},
        {"command": "lang", "description": "auto | hi | en | hinglish"},
        {"command": "sources", "description": "Kaun si books bot ke paas hain"},
        {"command": "stats", "description": "Bot usage"},
        {"command": "help", "description": "Madad"},
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
