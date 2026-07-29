"""
Free LLM providers with automatic key-rotation and fallback.

Env vars (comma-separate multiple keys for more free quota):
    GEMINI_API_KEY      -> https://aistudio.google.com/apikey        (recommended)
    GROQ_API_KEY        -> https://console.groq.com/keys             (fallback)
    OPENROUTER_API_KEY  -> https://openrouter.ai/keys                (fallback)
    GEMINI_MODEL        -> optional override, e.g. gemini-2.5-flash
"""
import os, json, time, itertools, threading
import requests

TIMEOUT = 60
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


def _gemini(system, user, temperature, use_search=False):
    """
    Gemini call with three self-healing retries:
      • 'thinking' models poora output-budget soch me kha jaate hain -> thinkingBudget 0
      • google_search tool na chale / quota khatam -> bina search dubara
      • ek key ka quota khatam -> agli key
    """
    model = gemini_model()
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent")
    last = "unknown error"
    no_think = True                       # pehle thinking off ke saath try karo
    search = use_search
    attempts = max(1, len(GEMINI.keys)) + 2

    for _ in range(attempts):
        key = GEMINI.next()
        if not key:
            break
        gen = {"temperature": temperature, "maxOutputTokens": 2048}
        if no_think:
            gen["thinkingConfig"] = {"thinkingBudget": 0}
        body = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": gen,
            "safetySettings": [
                {"category": c, "threshold": "BLOCK_ONLY_HIGH"} for c in
                ["HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
                 "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT"]
            ],
        }
        if search:
            body["tools"] = [{"google_search": {}}]
        try:
            r = requests.post(url, params={"key": key}, json=body, timeout=TIMEOUT)
            if r.status_code == 200:
                d = r.json()
                cand = (d.get("candidates") or [{}])[0]
                parts = cand.get("content", {}).get("parts") or []
                txt = "".join(p.get("text", "") for p in parts).strip()
                if txt:
                    return txt
                fr = cand.get("finishReason", "?")
                last = f"empty response (finishReason={fr})"
                print(f"[llm] gemini empty, finishReason={fr}", flush=True)
                if fr in ("MAX_TOKENS", "OTHER") and not no_think:
                    no_think = True                 # thinking budget khatam kar do
                    continue
                if search:                          # search ke bina dubara
                    search = False
                    continue
                continue

            last = f"{r.status_code} {r.text[:200]}"
            low = r.text.lower()
            # thinkingConfig ye model support nahi karta
            if no_think and r.status_code == 400 and "think" in low:
                no_think = False
                continue
            # google_search tool support nahi / grounding quota khatam
            if search and r.status_code in (400, 403, 429):
                print("[llm] search grounding unavailable, retrying without it",
                      flush=True)
                search = False
                continue
            if r.status_code in (429, 403, 500, 503):
                continue
        except Exception as e:
            last = str(e)
            if search:
                search = False
    raise RuntimeError(f"gemini failed: {last}")


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
