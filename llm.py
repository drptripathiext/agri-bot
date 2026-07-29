"""
Free LLM providers with automatic key-rotation and fallback.

Env vars (comma-separate multiple keys for more free quota):
    GEMINI_API_KEY      -> https://aistudio.google.com/apikey        (recommended)
    GROQ_API_KEY        -> https://console.groq.com/keys             (fallback)
    OPENROUTER_API_KEY  -> https://openrouter.ai/keys                (fallback)
    GEMINI_MODEL        -> optional override, e.g. gemini-2.5-flash
"""
import os, json, time, threading
import requests

TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "180"))
RETRIES = int(os.environ.get("LLM_RETRIES", "3"))      # per variant
BACKOFF = float(os.environ.get("LLM_BACKOFF", "2"))    # seconds
_lock = threading.Lock()


def _keys(name):
    raw = os.environ.get(name, "")
    return [k.strip() for k in raw.replace("\n", ",").split(",") if k.strip()]


class Rotator:
    def __init__(self, keys):
        self.keys = keys
        self.i = 0

    def next(self):
        with _lock:
            if not self.keys:
                return None
            k = self.keys[self.i % len(self.keys)]
            self.i += 1
            return k


GEMINI = Rotator(_keys("GEMINI_API_KEY"))
GROQ = Rotator(_keys("GROQ_API_KEY"))
OPENROUTER = Rotator(_keys("OPENROUTER_API_KEY"))

_gemini_model = None
_PREFERRED = ["flash-lite", "flash"]          # free-tier friendly


def gemini_model():
    """Discover a working free Flash model once, so hard-coded names never break the bot."""
    global _gemini_model
    if _gemini_model:
        return _gemini_model
    forced = os.environ.get("GEMINI_MODEL", "").strip()
    if forced:
        _gemini_model = forced
        return _gemini_model
    key = GEMINI.next()
    if key:
        try:
            r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                             params={"key": key}, timeout=30)
            names = [m["name"].split("/")[-1] for m in r.json().get("models", [])
                     if "generateContent" in m.get("supportedGenerationMethods", [])]
            names = [n for n in names if "embedding" not in n and "vision" not in n
                     and "image" not in n and "tts" not in n and "live" not in n]
            for want in _PREFERRED:
                cands = sorted([n for n in names if want in n and "preview" not in n
                                and "exp" not in n], reverse=True)
                if cands:
                    _gemini_model = cands[0]
                    print(f"[llm] using gemini model: {_gemini_model}", flush=True)
                    return _gemini_model
            if names:
                _gemini_model = names[0]
                return _gemini_model
        except Exception as e:
            print("[llm] model discovery failed:", e, flush=True)
    _gemini_model = "gemini-2.5-flash"
    return _gemini_model


SAFETY = [{"category": c, "threshold": "BLOCK_ONLY_HIGH"} for c in
          ["HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
           "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT"]]

LAST_ERROR = {"when": 0, "detail": ""}      # /diag ke liye


def _variants(system, user, temperature, use_search):
    """Sabse feature-rich request se sabse simple tak — jo chal jaye wahi sahi."""
    def base(gen, extra=None):
        b = {"systemInstruction": {"parts": [{"text": system}]},
             "contents": [{"role": "user", "parts": [{"text": user}]}],
             "generationConfig": gen,
             "safetySettings": SAFETY}
        if extra:
            b.update(extra)
        return b

    full = {"temperature": temperature, "maxOutputTokens": 2048}
    nothink = dict(full, thinkingConfig={"thinkingBudget": 0})
    out = []
    if use_search:
        out.append(("search+nothink", base(nothink, {"tools": [{"google_search": {}}]})))
        out.append(("search", base(full, {"tools": [{"google_search": {}}]})))
    out.append(("nothink", base(nothink)))
    out.append(("plain", base(full)))
    # sabse compatible: na system_instruction, na safety, na config
    out.append(("bare", {"contents": [{"role": "user",
                                       "parts": [{"text": system + "\n\n" + user}]}]}))
    return out


def _gemini(system, user, temperature, use_search=False):
    """Har variant ko har key ke saath try karo; 429 par thoda ruk kar dubara."""
    model = gemini_model()
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent")
    last = "unknown error"
    keys = GEMINI.keys or []

    for label, body in _variants(system, user, temperature, use_search):
        # har variant ko RETRIES baar: fail -> 2s ruko -> dubara -> 4s -> dubara
        for attempt in range(max(1, RETRIES)):
            key = GEMINI.next()
            if not key:
                break
            fatal = False
            try:
                r = requests.post(url, params={"key": key}, json=body, timeout=TIMEOUT)
                if r.status_code == 200:
                    d = r.json()
                    cand = (d.get("candidates") or [{}])[0]
                    parts = (cand.get("content") or {}).get("parts") or []
                    txt = "".join(p.get("text", "") for p in parts).strip()
                    if txt:
                        if label != "search+nothink":
                            print(f"[llm] ok via variant '{label}'", flush=True)
                        return txt
                    last = (f"[{label}] empty, finishReason="
                            f"{cand.get('finishReason','?')}")
                    fatal = True                    # retry se fayda nahi, agla variant
                else:
                    last = f"[{label}] HTTP {r.status_code}: {r.text[:220]}"
                    # 400 = request hi galat hai -> retry bekaar, agla variant lo
                    if r.status_code == 400:
                        fatal = True
            except Exception as e:
                last = f"[{label}] {type(e).__name__}: {e}"
            print(f"[llm] {last}", flush=True)
            if fatal:
                break
            if attempt < RETRIES - 1:
                time.sleep(BACKOFF * (attempt + 1))   # 2s, phir 4s
    LAST_ERROR["when"] = int(time.time())
    LAST_ERROR["detail"] = last
    raise RuntimeError(f"gemini failed: {last}")


def diagnose():
    """Live probe — /diag command ke liye. Exact status aur body wapas karta hai."""
    model = gemini_model()
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent")
    key = GEMINI.next()
    out = [f"model: {model}", f"keys: {len(GEMINI.keys)} gemini, "
           f"{len(GROQ.keys)} groq, {len(OPENROUTER.keys)} openrouter"]
    if not key:
        out.append("NO GEMINI KEY"); return "\n".join(out)
    for label, body in _variants("You are a test.", "Reply with the word OK.",
                                 0.1, True):
        try:
            r = requests.post(url, params={"key": key}, json=body, timeout=40)
            if r.status_code == 200:
                d = r.json()
                cand = (d.get("candidates") or [{}])[0]
                parts = (cand.get("content") or {}).get("parts") or []
                txt = "".join(p.get("text", "") for p in parts).strip()
                out.append(f"{label}: 200 {'OK -> ' + txt[:30] if txt else 'EMPTY fr=' + str(cand.get('finishReason'))}")
            else:
                out.append(f"{label}: {r.status_code} {r.text[:150]}")
        except Exception as e:
            out.append(f"{label}: {type(e).__name__} {e}")
    if LAST_ERROR["detail"]:
        out.append(f"\nlast real failure: {LAST_ERROR['detail'][:300]}")
    return "\n".join(out)


def _openai_style(base, key, model, system, user, temperature, extra_headers=None):
    h = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if extra_headers:
        h.update(extra_headers)
    r = requests.post(f"{base}/chat/completions", headers=h, json={
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": temperature,
        "max_tokens": 1400,
    }, timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"{r.status_code} {r.text[:180]}")
    return r.json()["choices"][0]["message"]["content"].strip()


def _groq(system, user, temperature):
    last = None
    model = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
    for _ in range(max(1, len(GROQ.keys))):
        key = GROQ.next()
        if not key:
            break
        try:
            return _openai_style("https://api.groq.com/openai/v1", key, model,
                                 system, user, temperature)
        except Exception as e:
            last = str(e)
            if "model" in last and ("decommission" in last or "not found" in last):
                model = "llama-3.1-8b-instant"
    raise RuntimeError(f"groq failed: {last}")


def _openrouter(system, user, temperature):
    last = None
    model = os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
    for _ in range(max(1, len(OPENROUTER.keys))):
        key = OPENROUTER.next()
        if not key:
            break
        try:
            return _openai_style("https://openrouter.ai/api/v1", key, model,
                                 system, user, temperature,
                                 {"HTTP-Referer": "https://huggingface.co",
                                  "X-Title": "AgriBot"})
        except Exception as e:
            last = str(e)
    raise RuntimeError(f"openrouter failed: {last}")


def have_any_key():
    return bool(GEMINI.keys or GROQ.keys or OPENROUTER.keys)


def complete(system, user, temperature=0.25, use_search=False):
    """Try each configured provider in order; return the first successful answer.

    use_search=True -> Gemini ko Google Search grounding de do (live web results).
    Groq/OpenRouter me search nahi hota, wo apni knowledge se jawab denge.
    """
    errors = []
    for name, fn, rot in (("gemini", _gemini, GEMINI),
                          ("groq", _groq, GROQ),
                          ("openrouter", _openrouter, OPENROUTER)):
        if not rot.keys:
            continue
        try:
            if name == "gemini":
                return fn(system, user, temperature, use_search)
            return fn(system, user, temperature)
        except Exception as e:
            errors.append(f"{name}: {e}")
            print(f"[llm] {name} failed -> {e}", flush=True)
    raise RuntimeError(" | ".join(errors) or "no API key configured")
