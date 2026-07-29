"""Official ASRB NET / ARS / SMS / STO syllabus — Agricultural Extension (Subject 48)."""
import os, re

_HERE = os.path.dirname(os.path.abspath(__file__))
_PATH = os.path.join(_HERE, "syllabus.txt")

RAW = ""
for p in (_PATH, os.path.join(os.getcwd(), "syllabus.txt")):
    if os.path.exists(p):
        RAW = open(p, encoding="utf-8").read().strip()
        break

_UNIT = re.compile(r"(?im)^Unit\s+(\d+)\s*:\s*(.+?)\s*$")

UNITS = []          # [(no, title, body), ...]
if RAW:
    marks = list(_UNIT.finditer(RAW))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(RAW)
        body = RAW[m.end():end].strip()
        UNITS.append((int(m.group(1)), m.group(2).strip(), body))

HEADER = "ASRB NET / ARS / SMS / STO — Subject 48: AGRICULTURAL EXTENSION"


def unit_list():
    return "\n".join(f"<b>Unit {n}</b> — {t}" for n, t, _ in UNITS)


def get_unit(n):
    for u, t, b in UNITS:
        if u == n:
            return u, t, b
    return None


def find_units(query, limit=2):
    """Sawaal se sabse milte-julte unit(s) dhoondo."""
    words = set(re.findall(r"[a-z]{4,}", query.lower()))
    if not words:
        return []
    scored = []
    for n, t, b in UNITS:
        text = (t + " " + b).lower()
        hits = sum(1 for w in words if w in text)
        if hits:
            scored.append((hits + (2 if any(w in t.lower() for w in words) else 0), n, t, b))
    scored.sort(reverse=True)
    return [(n, t, b) for _, n, t, b in scored[:limit]]


def context_for(query, max_chars=6000):
    """Syllabus wale sawaal ke liye LLM ko dene layak text."""
    m = re.search(r"unit\s*[-–]?\s*(\d{1,2})", query, re.I)
    if m:
        u = get_unit(int(m.group(1)))
        if u:
            return f"OFFICIAL SYLLABUS — Unit {u[0]}: {u[1]}\n{u[2]}"
    hits = find_units(query)
    if hits:
        return "\n\n".join(f"OFFICIAL SYLLABUS — Unit {n}: {t}\n{b}" for n, t, b in hits)[:max_chars]
    return ("OFFICIAL SYLLABUS (" + HEADER + ")\n" + RAW)[:max_chars]
