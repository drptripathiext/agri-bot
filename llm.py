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

TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "120"))      # retry ka read timeout
FIRST_TIMEOUT = int(os.environ.get("LLM_FIRST_TIMEOUT", "25"))   # pehli koshish
CONNECT_TIMEOUT = 10
RETRIES = int(os.environ.get("LLM_RETRIES", "2"))      # per variant
BACKOFF = float(os.environ.get("LLM_BACKOFF", "1"))    # seconds
MAX_OUT = int(os.environ.get("LLM_MAX_TOKENS", "700"))
_lock = threading.Lock()

# Keep-alive session — har call par TLS handshake nahi hota, kaafi tez
SESSION = requests.Session()
SESSION.mount("https://", requests.adapters.HTTPAdapter(
    pool_connections=32, pool_maxsize=64, max_retries=0))


def _keys(name):
    raw = os.environ.get(name, "")
    return [k.strip() for k in raw.replace("\n", ",").split(",") if k.strip()]


KEY_COOLDOWN = int(os.environ.get("KEY_COOLDOWN", "300"))   # 429 ke baad kitni der chhodo


class Rotator:
    def __init__(self, keys):
        self.keys = keys
        self.i = 0
        self.dead = {}                     # {key: kab tak chhodna hai}

    def next(self):
        """Jis key ka quota abhi khatam hai use skip karo."""
        with _lock:
            if not self.keys:
                return None
            now = time.time()
            for _ in range(len(self.keys)):
                k = self.keys[self.i % len(self.keys)]
                self.i += 1
                if self.dead.get(k, 0) <= now:
                    return k
            # saari keys thandi hain -> jo sabse pehle zinda hogi wahi lo
            return min(self.keys, key=lambda k: self.dead.get(k, 0))

    def cool(self, key, seconds=None):
        with _lock:
            self.dead[key] = time.time() + (seconds or KEY_COOLDOWN)

    def health(self):
        now = time.time()
        alive = sum(1 for k in self.keys if self.dead.get(k, 0) <= now)
        return f"{alive}/{len(self.keys)} alive"


GEMINI = Rotator(_keys("GEMINI_API_KEY"))
GROQ = Rotator(_keys("GROQ_API_KEY"))
OPENROUTER = Rotator(_keys("OPENROUTER_API_KEY"))

_gemini_model = None
_PREFERRED = ["flash-lite", "flash"]          # free-tier friendly
AVAILABLE = []                                # /models ke liye


def list_models():
    global AVAILABLE
    key = GEMINI.next()
    if not key:
        return []
    try:
        r = SESSION.get("https://generativelanguage.googleapis.com/v1beta/models",
                        params={"key": key}, timeout=30)
        AVAILABLE = [m["name"].split("/")[-1] for m in r.json().get("models", [])
                     if "generateContent" in m.get("supportedGenerationMethods", [])]
    except Exception as e:
        print("[llm] model list failed:", e, flush=True)
    return AVAILABLE


def gemini_model():
    """Ek pakka model chuno — '-latest' alias nahi, wo kabhi-kabhi hang ho jaate hain."""
    global _gemini_model
    if _gemini_model:
        return _gemini_model
    forced = os.environ.get("GEMINI_MODEL", "").strip()
    if forced:
        _gemini_model = forced
        print(f"[llm] model forced by env: {forced}", flush=True)
        return _gemini_model

    names = [n for n in list_models()
             if not any(x in n for x in ("embedding", "vision", "image", "tts",
                                         "live", "audio", "thinking"))]
    # "-latest" / "preview" / "exp" alias avoid karo — ye unstable hote hain
    stable = [n for n in names if not n.endswith("-latest")
              and "preview" not in n and "exp" not in n and "pro" not in n]
    for want in _PREFERRED:
        cands = sorted([n for n in stable if want in n], reverse=True)
        if cands:
            _gemini_model = cands[0]
            break
    else:
        _gemini_model = (stable or names or ["gemini-2.5-flash"])[0]
    print(f"[llm] using gemini model: {_gemini_model}  "
          f"(available: {len(names)})", flush=True)
    return _gemini_model


SAFETY = [{"category": c, "threshold": "BLOCK_ONLY_HIGH"} for c in
          ["HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
           "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT"]]

LAST_ERROR = {"when": 0, "detail": ""}      # /diag ke liye


# Jo variant pichli baar chala tha, use hi sabse pehle try karo.
# Aur jo variant fail ho chuka hai use dobara mat try karo — dono se time bachta hai.
GOOD = {}                                  # {"plain"/"search": "variant-name"}
BAD = set()                                # variants jo is model par chalte hi nahi
SEARCH_OFF_UNTIL = [0]                     # grounding quota khatam -> kuch der band
DEADLINE = float(os.environ.get("LLM_DEADLINE", "30"))   # itne second me haar maan lo

# Circuit breaker: jo provider baar-baar fail ho, use kuch der ke liye chhod do
HEALTH = {}                                # {"gemini": {"fails": n, "skip_until": ts}}


def _fail(name):
    h = HEALTH.setdefault(name, {"fails": 0, "skip_until": 0})
    h["fails"] += 1
    if h["fails"] >= 3:
        h["skip_until"] = time.time() + 600
        h["fails"] = 0
        print(f"[llm] {name} keeps failing — skipping it for 10 min", flush=True)


def _ok(name):
    HEALTH[name] = {"fails": 0, "skip_until": 0}


def _healthy(name):
    return time.time() >= HEALTH.get(name, {}).get("skip_until", 0)


def search_allowed():
    return time.time() >= SEARCH_OFF_UNTIL[0]


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

    full = {"temperature": temperature, "maxOutputTokens": MAX_OUT}
    nothink = dict(full, thinkingConfig={"thinkingBudget": 0})
    out = []
    if use_search and search_allowed():
        out.append(("search+nothink", base(nothink, {"tools": [{"google_search": {}}]})))
        out.append(("search", base(full, {"tools": [{"google_search": {}}]})))
    out.append(("nothink", base(nothink)))
    out.append(("plain", base(full)))
    # sabse compatible: na systemInstruction, na safety, na config
    out.append(("bare", {"contents": [{"role": "user",
                                       "parts": [{"text": system + "\n\n" + user}]}]}))

    out = [(k, v) for k, v in out if k not in BAD]      # jo chalte hi nahi, hata do
    if not out:
        out = [("plain", base(full))]
    # pichli baar jo chala tha wo sabse aage le aao
    known = GOOD.get("search" if use_search else "plain")
    if known:
        out.sort(key=lambda kv: kv[0] != known)
    return out


def _gemini(system, user, temperature, use_search=False):
    """Har variant ko har key ke saath try karo; 429 par thoda ruk kar dubara."""
    model = gemini_model()
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent")
    last = "unknown error"
    slot = "search" if use_search else "plain"
    t0 = time.time()

    for label, body in _variants(system, user, temperature, use_search):
        # har variant ko RETRIES baar: fail -> 1s ruko -> doosri koshish
        for attempt in range(max(1, RETRIES)):
            left = DEADLINE - (time.time() - t0)
            if left < 5:                      # bas, ab agle provider par jao
                print(f"[llm] deadline hit after {time.time()-t0:.0f}s", flush=True)
                LAST_ERROR["when"] = int(time.time()); LAST_ERROR["detail"] = last
                raise RuntimeError(f"gemini timed out: {last}")
            key = GEMINI.next()
            if not key:
                break
            fatal = False
            read_to = min(FIRST_TIMEOUT if attempt == 0 else TIMEOUT, left)
            try:
                r = SESSION.post(url, params={"key": key}, json=body,
                                 timeout=(CONNECT_TIMEOUT, read_to))
                if r.status_code == 200:
                    d = r.json()
                    cand = (d.get("candidates") or [{}])[0]
                    parts = (cand.get("content") or {}).get("parts") or []
                    txt = "".join(p.get("text", "") for p in parts).strip()
                    if txt:
                        if GOOD.get(slot) != label:
                            GOOD[slot] = label      # yaad rakho, agli baar seedhe yahi
                            print(f"[llm] variant '{label}' locked in for {slot}",
                                  flush=True)
                        print(f"[llm] {slot} answer in {time.time()-t0:.1f}s",
                              flush=True)
                        return txt
                    last = (f"[{label}] empty, finishReason="
                            f"{cand.get('finishReason','?')}")
                    fatal = True                    # retry se fayda nahi, agla variant
                else:
                    last = f"[{label}] HTTP {r.status_code}: {r.text[:220]}"
                    # 400 = ye variant is model par chalta hi nahi -> hamesha ke liye hata do
                    if r.status_code == 400:
                        fatal = True
                        BAD.add(label)
                        print(f"[llm] variant '{label}' disabled (400)", flush=True)
                    # grounding ka quota khatam -> 30 min search band
                    elif r.status_code == 429 and "search" in label:
                        fatal = True
                        SEARCH_OFF_UNTIL[0] = time.time() + 1800
                        print("[llm] search grounding quota exhausted — "
                              "disabled for 30 min", flush=True)
                    elif r.status_code == 429:
                        # is key ka quota khatam — thodi der doosri key use karo
                        GEMINI.cool(key)
                        print(f"[llm] key …{key[-6:]} cooling down "
                              f"({GEMINI.health()})", flush=True)
                        if len(GEMINI.keys) > 1:
                            continue          # doosri key se turant try karo
            except Exception as e:
                last = f"[{label}] {type(e).__name__}: {e}"
            print(f"[llm] {last}", flush=True)
            if fatal:
                if GOOD.get(slot) == label:
                    GOOD.pop(slot, None)            # ye ab nahi chalta, bhool jao
                break
            if attempt < RETRIES - 1:
                time.sleep(BACKOFF)
    LAST_ERROR["when"] = int(time.time())
    LAST_ERROR["detail"] = last
    raise RuntimeError(f"gemini failed: {last}")


def diagnose():
    """Live probe — /diag command ke liye. Exact status aur body wapas karta hai."""
    model = gemini_model()
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent")
    key = GEMINI.next()
    out = [f"order: {'groq -> gemini' if FAST_FIRST else 'gemini -> groq'}"
           f"   (web questions always gemini first)",
           f"gemini model: {model}",
           f"groq model: {_groq_model[0] or GROQ_MODELS[0]}",
           f"locked: {GOOD or 'none'}   disabled: {sorted(BAD) or 'none'}",
           f"grounding: {'on' if search_allowed() else 'OFF (quota)'}",
           f"keys: gemini {GEMINI.health()}, groq {GROQ.health()}, "
           f"openrouter {OPENROUTER.health()}",
           f"budget: {DEADLINE}s, first {FIRST_TIMEOUT}s / retry {TIMEOUT}s, "
           f"retries {RETRIES}, max tokens {MAX_OUT}", ""]
    if not key:
        out.append("NO GEMINI KEY"); return "\n".join(out)

    # asli speed — jaisa sawaal poochne par hota hai
    t0 = time.time()
    try:
        _gemini("You are a study assistant. Be brief.",
                "In one sentence, what is agricultural extension?", 0.2, False)
        out.append(f"REAL ANSWER SPEED: {time.time()-t0:.1f}s")
    except Exception as e:
        out.append(f"REAL ANSWER FAILED after {time.time()-t0:.1f}s: {str(e)[:150]}")
    out.append("")

    for label, body in _variants("You are a test.", "Reply with the word OK.",
                                 0.1, True):
        try:
            t1 = time.time()
            r = SESSION.post(url, params={"key": key}, json=body, timeout=40)
            label = f"{label} ({time.time()-t1:.1f}s)"
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
    r = SESSION.post(f"{base}/chat/completions", headers=h, json={
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": temperature,
        "max_tokens": MAX_OUT,
    }, timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"{r.status_code} {r.text[:180]}")
    return r.json()["choices"][0]["message"]["content"].strip()


GROQ_MODELS = [m.strip() for m in os.environ.get(
    "GROQ_MODEL",
    "llama-3.3-70b-versatile,llama-3.1-8b-instant,llama3-70b-8192").split(",") if m.strip()]
_groq_model = [None]                       # jo chala use yaad rakho


def _groq(system, user, temperature):
    last = None
    models = ([_groq_model[0]] if _groq_model[0] else []) + \
             [m for m in GROQ_MODELS if m != _groq_model[0]]
    for model in models:
        for _ in range(max(1, len(GROQ.keys))):
            key = GROQ.next()
            if not key:
                break
            try:
                out = _openai_style("https://api.groq.com/openai/v1", key, model,
                                    system, user, temperature)
                if _groq_model[0] != model:
                    _groq_model[0] = model
                    print(f"[llm] groq model: {model}", flush=True)
                return out
            except Exception as e:
                last = str(e)
                low = last.lower()
                if "429" in last:
                    GROQ.cool(key, 60)
                    continue
                if any(x in low for x in ("decommission", "not found",
                                          "does not exist", "model_not_found")):
                    break                   # agla model try karo
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


def warmup():
    """Startup par TLS + model ready kar lo, taaki pehla sawaal bhi tez ho."""
    for name, fn in (("groq", _groq), ("gemini", None)):
        try:
            t0 = time.time()
            if name == "groq" and GROQ.keys:
                _groq("Reply with one word.", "Say ready.", 0.1)
                print(f"[warm] groq ready in {time.time()-t0:.1f}s", flush=True)
            elif name == "gemini" and GEMINI.keys:
                _gemini("Reply with one word.", "Say ready.", 0.1, False)
                print(f"[warm] gemini ready in {time.time()-t0:.1f}s", flush=True)
        except Exception as e:
            print(f"[warm] {name}: {str(e)[:120]}", flush=True)


FAST_FIRST = os.environ.get("FAST_FIRST", "1") == "1"


def complete(system, user, temperature=0.25, use_search=False):
    """Pehle sabse tez provider, jo fail ho to agla.

    use_search=True -> Gemini pehle (Google Search grounding sirf usi me hai).
    warna Groq pehle — wo Gemini se kai guna tez likhta hai.
    """
    providers = {"gemini": (_gemini, GEMINI), "groq": (_groq, GROQ),
                 "openrouter": (_openrouter, OPENROUTER)}
    if use_search or not FAST_FIRST:
        order = ["gemini", "groq", "openrouter"]
    else:
        order = ["groq", "gemini", "openrouter"]      # speed ke liye Groq pehle

    live = [(n, providers[n][0], providers[n][1])
            for n in order if providers[n][1].keys]
    if not live:
        raise RuntimeError("no API key configured")

    # jo provider abhi bimar hai use peeche daal do (agar koi aur maujood hai)
    if len(live) > 1:
        live.sort(key=lambda p: not _healthy(p[0]))

    errors = []
    for name, fn, rot in live:
        try:
            out = (fn(system, user, temperature, use_search) if name == "gemini"
                   else fn(system, user, temperature))
            _ok(name)
            return out
        except Exception as e:
            _fail(name)
            errors.append(f"{name}: {e}")
            print(f"[llm] {name} failed -> {e}", flush=True)
    raise RuntimeError(" | ".join(errors))
