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
    model = gemini_model()
    last = None
    for _ in range(max(1, len(GEMINI.keys))):
        key = GEMINI.next()
        if not key:
            break
        body = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": temperature,
                                 "maxOutputTokens": 1400},
            "safetySettings": [
                {"category": c, "threshold": "BLOCK_ONLY_HIGH"} for c in
                ["HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
                 "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT"]
            ],
        }
        if use_search:
            body["tools"] = [{"google_search": {}}]
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                params={"key": key}, json=body, timeout=TIMEOUT,
            )
            if r.status_code == 200:
                d = r.json()
                cand = (d.get("candidates") or [{}])[0]
                parts = cand.get("content", {}).get("parts") or []
                txt = "".join(p.get("text", "") for p in parts).strip()
                if txt:
                    return txt
                last = "empty response"
            else:
                last = f"{r.status_code} {r.text[:180]}"
                # search tool support na ho / quota khatam -> bina search dubara
                if use_search and r.status_code in (400, 429):
                    use_search = False
                    continue
                if r.status_code in (429, 403):      # quota -> try next key
                    continue
        except Exception as e:
            last = str(e)
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
