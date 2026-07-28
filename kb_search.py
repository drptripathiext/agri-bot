"""BM25 keyword search over the notes knowledge-base. Zero heavy dependencies."""
import os, re, json, gzip, math, pickle
from collections import Counter, defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))


def _find_kb():
    """kb/ folder ya root — dono jagah dhoondh lo (upload aasaan rahe)."""
    if os.environ.get("KB_DIR"):
        return os.environ["KB_DIR"]
    for d in (os.path.join(_HERE, "kb"), _HERE, os.path.join(os.getcwd(), "kb"), os.getcwd()):
        if os.path.exists(os.path.join(d, "bm25.pkl.gz")):
            return d
    return os.path.join(_HERE, "kb")


KB_DIR = _find_kb()

STOP = set("""a an the is are was were be been being of in on at to for from by with without and or
but if then than that this these those it its as into about over under can could will would shall
should may might must do does did done have has had not no nor so such which who whom whose what
when where why how all any both each few more most other some only own same too very s t just
i me my we our you your he him his she her they them their kya hai kaise kyu kyun ka ki ke ko me
mein se aur ya bhi ye yeh vo woh hota hoti hote tell explain define describe about please give
answer question sir bhai""".split())

TOKEN = re.compile(r"[a-z0-9]+|[ऀ-ॿ]+")

# Domain shortcuts -> extra search terms (agri-extension specific)
EXPAND = {
    "atma": ["agricultural", "technology", "management", "agency"],
    "kvk": ["krishi", "vigyan", "kendra", "farm", "science", "centre"],
    "icar": ["indian", "council", "agricultural", "research"],
    "nmaet": ["national", "mission", "agricultural", "extension", "technology"],
    "t&v": ["training", "visit", "system"],
    "tandv": ["training", "visit"],
    "ffs": ["farmer", "field", "school"],
    "ptd": ["participatory", "technology", "development"],
    "akis": ["agricultural", "knowledge", "information", "system"],
    "fsre": ["farming", "system", "research", "extension"],
    "irdp": ["integrated", "rural", "development", "programme"],
    "ict": ["information", "communication", "technology"],
    "sau": ["state", "agricultural", "university"],
    "ngo": ["non", "government", "organisation"],
    "shg": ["self", "help", "group"],
    "fpo": ["farmer", "producer", "organisation"],
    "diffusion": ["adoption", "innovation", "rogers"],
    "adoption": ["diffusion", "innovation", "adopter"],
}


# ---------------------------------------------------------------- Hindi support
# Notes English me hain, isliye Devanagari sawaal ko Roman + English terms me badalte hain.

_DEVA = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n", "च": "ch", "छ": "chh",
    "ज": "j", "झ": "jh", "ञ": "n", "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh",
    "ण": "n", "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n", "प": "p",
    "फ": "ph", "ब": "b", "भ": "bh", "म": "m", "य": "y", "र": "r", "ल": "l",
    "व": "v", "श": "sh", "ष": "sh", "स": "s", "ह": "h", "ळ": "l",
    "ा": "a", "ि": "i", "ी": "i", "ु": "u", "ू": "u", "े": "e", "ै": "ai",
    "ो": "o", "ौ": "au", "ृ": "ri", "ं": "n", "ँ": "n", "ः": "h", "्": "",
    "अ": "a", "आ": "a", "इ": "i", "ई": "i", "उ": "u", "ऊ": "u", "ए": "e",
    "ऐ": "ai", "ओ": "o", "औ": "au", "ऋ": "ri", "ॐ": "om", "़": "",
    "०": "0", "१": "1", "२": "2", "३": "3", "४": "4",
    "५": "5", "६": "6", "७": "7", "८": "8", "९": "9",
}

HI_EN = {
    "कृषि": "agriculture", "प्रसार": "extension", "शिक्षा": "education",
    "संचार": "communication", "प्रशिक्षण": "training", "अनुसंधान": "research",
    "ग्रामीण": "rural", "विकास": "development", "महिला": "women",
    "किसान": "farmer", "कृषक": "farmer", "योजना": "scheme plan programme",
    "नेतृत्व": "leadership", "प्रेरणा": "motivation", "मूल्यांकन": "evaluation",
    "प्रबंधन": "management", "नवाचार": "innovation", "प्रसारण": "diffusion",
    "अपनाना": "adoption", "समूह": "group", "पंचायत": "panchayat",
    "सहभागिता": "participation", "सशक्तिकरण": "empowerment",
    "उद्यमिता": "entrepreneurship", "पत्रकारिता": "journalism",
    "विधि": "method", "सिद्धांत": "principle theory", "परिभाषा": "definition",
    "उद्देश्य": "objective", "महत्व": "importance", "प्रकार": "type",
    "विशेषता": "characteristic", "लाभ": "advantage benefit",
    "प्रक्रिया": "process", "व्यवहार": "behaviour", "अभिवृत्ति": "attitude",
    "ज्ञान": "knowledge", "कौशल": "skill", "तकनीक": "technique technology",
    "प्रौद्योगिकी": "technology", "सूचना": "information", "संगठन": "organisation",
    "नियोजन": "planning", "कार्यक्रम": "programme", "स्तर": "level",
    "मॉडल": "model", "मापन": "measurement", "प्रतिचयन": "sampling",
    "परिकल्पना": "hypothesis", "आंकड़ा": "data", "सर्वेक्षण": "survey",
    "विज्ञान": "vigyan science", "केंद्र": "kendra centre", "केन्द्र": "kendra centre",
    "राष्ट्रीय": "national", "सरकार": "government", "नीति": "policy",
    "इतिहास": "history", "स्थापना": "establishment established",
}


def hindi_boost(s):
    """Devanagari text -> extra Roman + English search terms."""
    extra = []
    for w, en in HI_EN.items():
        if w in s:
            extra += en.split()
    roman = "".join(_DEVA.get(ch, ch if not ("ऀ" <= ch <= "ॿ") else "") for ch in s)
    return extra, roman


def tokenize(s):
    s = s.lower()
    out = []
    for w in TOKEN.findall(s):
        if w in STOP or len(w) < 2:
            continue
        for suf in ("ization", "isation", "ations", "ation", "ments", "ment",
                    "ness", "ities", "ity", "ies", "ing", "ers", "er", "ed", "s"):
            if len(w) > len(suf) + 3 and w.endswith(suf):
                w = w[: -len(suf)]
                break
        out.append(w)
    return out


class KnowledgeBase:
    def __init__(self, kb_dir=None):
        kb_dir = kb_dir or _find_kb()
        print(f"[kb] reading from {kb_dir}", flush=True)
        with gzip.open(os.path.join(kb_dir, "bm25.pkl.gz"), "rb") as fh:
            d = pickle.load(fh)
        self.inv, self.idf = d["inv"], d["idf"]
        self.dl, self.avgdl, self.N = d["dl"], d["avgdl"], d["N"]
        self.chunks = []
        with gzip.open(os.path.join(kb_dir, "chunks.jsonl.gz"), "rt", encoding="utf-8") as fh:
            for line in fh:
                self.chunks.append(json.loads(line))
        try:
            self.meta = json.load(open(os.path.join(kb_dir, "meta.json"), encoding="utf-8"))
        except Exception:
            self.meta = {"chunks": len(self.chunks), "sources": []}
        self.k1, self.b = 1.5, 0.75

    def _bm25(self, weighted_terms, top):
        """weighted_terms: dict term -> weight"""
        scores = defaultdict(float)
        for w, wt in weighted_terms.items():
            post = self.inv.get(w)
            if not post:
                continue
            idf = self.idf.get(w, 0.0)
            if idf <= 0:
                continue
            for i, tf in post:
                denom = tf + self.k1 * (1 - self.b + self.b * self.dl[i] / self.avgdl)
                scores[i] += wt * idf * (tf * (self.k1 + 1)) / denom
        return sorted(scores.items(), key=lambda x: -x[1])[:top]

    def search(self, query, top_k=8, pool=60):
        hi_terms, roman = [], ""
        if re.search(r"[ऀ-ॿ]", query):
            hi_terms, roman = hindi_boost(query)
        q = tokenize(query + " " + roman)
        if not q:
            return []
        terms = defaultdict(float)
        for w, c in Counter(q).items():
            terms[w] += 1.0 + 0.3 * (c - 1)
        for w in tokenize(" ".join(hi_terms)):  # Hindi -> English mapped terms
            terms[w] = max(terms[w], 0.9)
        for w in set(q):                       # expansion terms count less
            for e in EXPAND.get(w, []):
                if e not in terms:
                    terms[e] += 0.35
        hits = self._bm25(terms, pool)
        if not hits:
            return []

        qset = set(q)
        bigrams = {(q[i], q[i + 1]) for i in range(len(q) - 1)}
        rescored = []
        for i, s in hits:
            toks = tokenize(self.chunks[i]["text"])
            tset = set(toks)
            cover = len(qset & tset) / max(len(qset), 1)
            # phrase/proximity bonus: query bigrams appearing in the chunk
            if bigrams:
                cb = {(toks[j], toks[j + 1]) for j in range(len(toks) - 1)}
                phrase = len(bigrams & cb) / len(bigrams)
            else:
                phrase = 0.0
            rescored.append((i, s * (1 + 1.3 * cover + 1.0 * phrase)))
        rescored.sort(key=lambda x: -x[1])

        # keep diverse sources: max 3 chunks per source file
        out, per_src = [], Counter()
        for i, s in rescored:
            src = self.chunks[i]["src"]
            if per_src[src] >= 3:
                continue
            per_src[src] += 1
            out.append({"src": src, "text": self.chunks[i]["text"], "score": s})
            if len(out) >= top_k:
                break
        return out

    def build_context(self, hits, max_chars=9000):
        parts, total = [], 0
        for h in hits:
            block = f"[{h['src']}]\n{h['text']}"
            if total + len(block) > max_chars:
                break
            parts.append(block)
            total += len(block)
        return "\n\n---\n\n".join(parts)
