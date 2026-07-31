"""
Asli PYQ/MCQ bank — 674 exam-tagged questions.

Ye AI se nahi banaye jaate, seedhe file se aate hain. Matlab:
  • turant (koi API call nahi)
  • bilkul sahi answer (asli answer key)
  • asli previous-year questions, bane hue nahi
"""
import os, re, json, gzip, random

_HERE = os.path.dirname(os.path.abspath(__file__))

ITEMS = []
for p in (os.path.join(_HERE, "mcq.json.gz"), os.path.join(os.getcwd(), "mcq.json.gz"),
          os.path.join(_HERE, "kb", "mcq.json.gz")):
    if os.path.exists(p):
        try:
            with gzip.open(p, "rt", encoding="utf-8") as fh:
                ITEMS = json.load(fh)
            print(f"[mcq] {len(ITEMS)} questions loaded", flush=True)
        except Exception as e:
            print("[mcq] load failed:", e, flush=True)
        break

TOPICS = sorted({i["topic"] for i in ITEMS if i.get("topic")})

_STOP = set("""the a an of in on at to for from by with and or is are was were be
me mein ka ki ke ko se aur ya par kuch some any about related regarding
question questions mcq mcqs pyq pyqs previous year exam practice quiz test
do de dijiye chahiye batao give send karo karna par set""".split())


def _toks(s):
    return [w for w in re.findall(r"[a-z0-9]+", s.lower())
            if w not in _STOP and len(w) > 2]


def _score(item, words):
    if not words:
        return 0
    hay = (item["q"] + " " + " ".join(item["o"]) + " " + item.get("topic", "")).lower()
    topic = item.get("topic", "").lower()
    s = 0
    for w in words:
        if w in topic:
            s += 3
        elif w in hay:
            s += 1
    return s


def pick(topic="", n=5, seed=None):
    """Topic se milte-julte n questions. Topic khali ho to random mix."""
    if not ITEMS:
        return []
    n = max(1, min(10, n))
    rng = random.Random(seed)
    words = _toks(topic or "")
    if words:
        scored = [(_score(i, words), i) for i in ITEMS]
        hits = [i for s, i in scored if s >= 2]
        if len(hits) < n:
            hits = [i for s, i in scored if s >= 1]
        if len(hits) >= n:
            return rng.sample(hits, n)
        if hits:                                  # thode mile — baaki random
            rest = [i for i in ITEMS if i not in hits]
            return hits + rng.sample(rest, min(n - len(hits), len(rest)))
    return rng.sample(ITEMS, min(n, len(ITEMS)))


def search(query, limit=8):
    """Text jawab ke liye — sabse relevant questions."""
    words = _toks(query)
    if not words or not ITEMS:
        return []
    scored = sorted(((_score(i, words), i) for i in ITEMS),
                    key=lambda x: -x[0])
    return [i for s, i in scored[:limit] if s >= 2]


def as_text(items):
    """Chat me dikhane layak format (markdown — bot khud HTML bana leta hai)."""
    out = []
    for k, it in enumerate(items, 1):
        opts = "\n".join(f"({chr(97+j)}) {o}" for j, o in enumerate(it["o"]))
        ans = f"({chr(97 + it['a'])}) {it['o'][it['a']]}"
        tag = f"  · *{it['tag']}*" if it.get("tag") else ""
        out.append(f"**Q{k}.** {it['q']}\n{opts}\n**Ans:** {ans}{tag}")
    return "\n\n".join(out)


def topic_list():
    counts = {}
    for i in ITEMS:
        t = i.get("topic") or "Other"
        counts[t] = counts.get(t, 0) + 1
    return sorted(counts.items(), key=lambda x: -x[1])
