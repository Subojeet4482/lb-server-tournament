"""LOAD BALANCER (Render).  The user panel only talks to this URL.

  /auth /user /wallet /matches /leaderboard /public /ref -> Server-1  (fallback Server-4)   (/ref = Refer & Earn + scratch cards)
  /history                                           -> Server-2   (fallback Server-4)
  /chat /game                                        -> Server-3   (fallback Server-4)
  /fraud (admin key)                                 -> Server-5   (fallback Server-4)
  /backup (admin key)                                -> Server-4

Health checks run in the background. When a server is down (or switched off by the admin) its traffic goes to Server-4
automatically, and comes back by itself when the server is healthy again.
Admin control:  GET /lb/admin/servers   POST /lb/admin/servers/<s1..s5>  {"mode":"auto|on|off"}   (header X-Admin-Key)
"""
import os, json, time, threading, hmac
from concurrent.futures import ThreadPoolExecutor
import requests
from flask import Flask, request, Response, jsonify

def env(n, d=""):
    v = os.environ.get(n)
    return v.strip() if v and v.strip() else d

IDS = ["s1", "s2", "s3", "s4", "s5"]
URLS = {i: env(i.upper() + "_URL").rstrip("/") for i in IDS}
INTERNAL_KEY = env("INTERNAL_KEY")
ADMIN_KEY = env("LB_ADMIN_KEY")
TRUSTED_PROXIES = int(env("TRUSTED_PROXIES", "1"))        # Render = 1 proxy in front of us. Cloudflare + Render = 2
DOMAIN_CHECK = env("DOMAIN_CHECK", "true").lower() not in ("0", "false", "no", "off")
ALLOW_LOCALHOST = env("ALLOW_LOCALHOST", "false").lower() in ("1", "true", "yes")
MAX_BODY = int(env("MAX_BODY_BYTES", str(1024 * 1024)))
BOOTSTRAP_DOMAINS = [d.strip().lower() for d in env("BOOTSTRAP_DOMAINS").split(",") if d.strip()]   # origin-only (no site key) until the bot adds them
ADMIN_PREFIXES = ("/fraud", "/backup", "/game/ball/admin", "/game/bomb/admin", "/geo/admin", "/wallet/admin", "/ref/admin", "/user/admin")
HEALTH_EVERY = int(env("HEALTH_INTERVAL", "30"))
FAIL_AFTER = int(env("FAIL_THRESHOLD", "2"))
HEALTH_TIMEOUT = float(env("HEALTH_TIMEOUT", "50"))      # Render free servers need up to ~50 s to wake up
CONNECT_TIMEOUT = float(env("CONNECT_TIMEOUT", "10"))
READ_TIMEOUT = float(env("REQUEST_TIMEOUT", "60"))
RATE_PER_MIN = int(env("RATE_LIMIT_PER_MIN", "300"))
STATE_FILE = env("STATE_FILE", "/tmp/lb_state.json")
STATE_STORE_ENV = env("STATE_STORE_URL").rstrip("/")
URL_BOT = {i: False for i in IDS}      # True = this server's URL was set from Telegram / admin panel (saved in the state store), not from Render env


def state_store():
    """Where the saved state lives: STATE_STORE_URL if set, else Server-4's URL (env or set from Telegram)."""
    return STATE_STORE_ENV or URLS["s4"]

ROUTES = [("/auth", "s1"), ("/user", "s1"), ("/wallet", "s1"), ("/matches", "s1"), ("/leaderboard", "s1"), ("/public", "s1"),
          ("/ref", "s1"), ("/geo", "s1"), ("/history", "s2"), ("/chat", "s3"), ("/game", "s3"), ("/fraud", "s5"), ("/backup", "s4")]
BACKUP = "s4"
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
       "content-encoding", "x-internal-key", "x-real-ip", "x-forwarded-for", "x-from"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_BODY
sess = requests.Session()
sess.mount("https://", requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=50))
sess.mount("http://", requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=50))
LOCK = threading.RLock()
S = {i: {"mode": "auto", "up": None, "fails": 0, "latency": None, "last": 0, "error": "", "info": {}} for i in IDS}   # up None = not checked yet


# ------------------------------------------------------------------ state (modes + domains survive restarts)
import hashlib, secrets, re
from collections import deque
from urllib.parse import urlparse

DOMAINS = {}          # host -> {"id","host","keyHash","active","addedAt","note"}
SETTINGS = {"domainCheck": DOMAIN_CHECK,
            "appMaintenance": False, "maintMsg": "",          # whole app OFF (every user call answers 503 + this message)
            "readOnly": False,                                # freeze every write (deposit / withdraw / join / games ...) but keep reading
            "rateLimit": RATE_PER_MIN,                        # requests per minute per IP
            "alerts": True,                                   # Telegram alert when a server goes down / comes back
            "features": {},                                   # key -> False when switched OFF
            "featureMsg": {},                                 # key -> custom message shown to players
            "ipBlock": []}                                    # blocked IP addresses

# Kill-switches that work at the load balancer: no server / database change needed, instant, survive restarts.
import re as _re
FEATURES = {
    "deposit":     ("💳 Add money (deposit)", _re.compile(r"^/wallet/deposit(/|$)"), "Deposit is temporarily switched off. Please try again later."),
    "withdraw":    ("💸 Withdraw", _re.compile(r"^/wallet/withdraw(/|$)"), "Withdrawals are temporarily switched off. Please try again later."),
    "gift":        ("🎁 Gift codes", _re.compile(r"^/wallet/gift(/|$)"), "Gift codes are temporarily switched off."),
    "join":        ("🎮 Join matches", _re.compile(r"^/matches/[^/]+/(join|slot)$"), "Joining matches is temporarily switched off."),
    "games":       ("🪙 Coin Battle / Bomb & Gems", _re.compile(r"^/game(/|$)"), "Games are temporarily switched off."),
    "chat":        ("💬 Chat", _re.compile(r"^/chat(/|$)"), "Chat is temporarily switched off."),
    "refer":       ("🤝 Refer & Earn", _re.compile(r"^/ref(/|$)"), "Refer & Earn is temporarily switched off."),
    "proof":       ("🧾 Payment proofs", _re.compile(r"^/wallet/proof(/|$)"), "Proofs are temporarily switched off."),
    "leaderboard": ("🏆 Leaderboard", _re.compile(r"^/leaderboard(/|$)"), "Leaderboard is temporarily unavailable."),
    "login":       ("🔑 Login / sign-up", _re.compile(r"^/auth/login$"), "Login is temporarily closed. Please try again later."),
}
EVENTS = deque(maxlen=300)      # activity log: server up/down, every admin action
SRV = {i: {"req": 0, "err": 0, "ms": 0} for i in IDS}      # traffic per server since restart
NOISY = {}                      # ip -> times rate limited (to spot abusers)


def log_event(kind, text, who=""):
    EVENTS.appendleft({"t": int(time.time() * 1000), "kind": kind, "text": str(text)[:300], "who": str(who)[:40]})


def notify(text):
    """Telegram message to every admin (only if alerts are ON). Never raises."""
    if not SETTINGS.get("alerts", True):
        return
    try:
        import tgbot
        threading.Thread(target=tgbot.notify_admins, args=(text,), daemon=True).start()
    except Exception:
        pass
BLOCKED = {}          # origin -> {"origin","count","last","path","ip","reason"}   (recent rejected domains)
STATS = {"allowed": 0, "blockedDomain": 0, "blockedKey": 0, "rateLimited": 0, "since": int(time.time())}
LOADED = {"ok": False}


def _sha(x):
    return hashlib.sha256(x.encode()).hexdigest()


def _snapshot():
    return {"servers": {i: {"mode": S[i]["mode"]} for i in IDS}, "domains": DOMAINS, "settings": SETTINGS, "events": list(EVENTS)[:80], "urls": {i: URLS[i] for i in IDS if URL_BOT[i] and URLS[i]}}


def _save():
    st = _snapshot()
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(st, f)
    except Exception:
        pass
    if state_store() and INTERNAL_KEY:
        try:
            requests.put(state_store() + "/internal/sys/lbstate", json={"state": st}, headers={"X-Internal-Key": INTERNAL_KEY}, timeout=8)
        except Exception:
            pass


def _apply(st):
    if not isinstance(st, dict):
        return False
    srv = st.get("servers") or {k: v for k, v in st.items() if k in S}       # old format = {s1:{mode}}
    for i, v in srv.items():
        if i in S and isinstance(v, dict) and v.get("mode") in ("auto", "on", "off"):
            S[i]["mode"] = v["mode"]
    if isinstance(st.get("domains"), dict):
        DOMAINS.clear()
        for h, d in st["domains"].items():
            if isinstance(d, dict) and d.get("host"):
                DOMAINS[h] = d
    se = st.get("settings")
    if isinstance(se, dict):
        for k in ("domainCheck", "appMaintenance", "readOnly", "alerts"):
            if k in se:
                SETTINGS[k] = bool(se[k])
        if isinstance(se.get("maintMsg"), str):
            SETTINGS["maintMsg"] = se["maintMsg"][:300]
        if isinstance(se.get("rateLimit"), (int, float)) and 20 <= se["rateLimit"] <= 100000:
            SETTINGS["rateLimit"] = int(se["rateLimit"])
        if isinstance(se.get("features"), dict):
            SETTINGS["features"] = {k: bool(v) for k, v in se["features"].items() if k in FEATURES}
        if isinstance(se.get("featureMsg"), dict):
            SETTINGS["featureMsg"] = {k: str(v)[:300] for k, v in se["featureMsg"].items() if k in FEATURES}
        if isinstance(se.get("ipBlock"), list):
            SETTINGS["ipBlock"] = [str(x)[:64] for x in se["ipBlock"]][:500]
    if isinstance(st.get("urls"), dict):                       # server URLs added from Telegram / admin panel
        for i, u in st["urls"].items():
            u = clean_url(u)
            if i in URLS and u:
                URLS[i], URL_BOT[i] = u, True
    if isinstance(st.get("events"), list) and not EVENTS:
        for e in reversed(st["events"][:80]):
            if isinstance(e, dict) and e.get("text"):
                EVENTS.appendleft(e)
    return True


def _load():
    st = None
    if state_store() and INTERNAL_KEY:
        try:
            r = requests.get(state_store() + "/internal/sys/lbstate", headers={"X-Internal-Key": INTERNAL_KEY}, timeout=8)
            if r.status_code == 200:
                inner = r.json().get("state") or {}
                st = inner.get("state") if isinstance(inner.get("state"), dict) else inner
        except Exception:
            pass
    if not st:
        try:
            st = json.load(open(STATE_FILE))
        except Exception:
            st = None
    LOADED["ok"] = bool(st) and _apply(st)
    for h in BOOTSTRAP_DOMAINS:
        DOMAINS.setdefault(h, {"id": _sha(h)[:8], "host": h, "keyHash": "", "active": True, "addedAt": int(time.time() * 1000), "note": "bootstrap (env)"})


# ---------------- domain verification
HOST_RE = re.compile(r"^(localhost|([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24})(:\d{1,5})?$")


def norm_host(text):
    t = (text or "").strip().lower()
    t = re.sub(r"^[a-z]+://", "", t).split("/")[0].split("?")[0].strip()
    return t if HOST_RE.match(t) else ""


def new_site_key():
    return "sk_" + secrets.token_urlsafe(24)


def add_domain(host):
    """Register a domain. NO site key any more: the browser's Origin header must be this registered https domain (domain verification only).
    Returns (record, "") - the empty second value is kept so older callers keep working."""
    rec = {"id": _sha(host)[:8], "host": host, "keyHash": "", "active": True, "addedAt": int(time.time() * 1000), "note": ""}
    DOMAINS[host] = rec
    BLOCKED.pop("https://" + host, None)
    _save()
    return rec, ""


def rotate_key(host):
    key = new_site_key()
    DOMAINS[host]["keyHash"] = _sha(key)
    _save()
    return key


def domain_by_id(did):
    return next((d for d in DOMAINS.values() if d["id"] == did), None)


def request_origin():
    """Browser tells us which site made the call: Origin (always sent on cross-site fetch) or Referer as fallback."""
    o = request.headers.get("Origin", "").strip()
    if not o or o == "null":
        ref = request.headers.get("Referer", "").strip()
        if ref:
            u = urlparse(ref)
            o = f"{u.scheme}://{u.netloc}" if u.scheme and u.netloc else ""
        else:
            o = ""
    return o


def verify_domain():
    """-> (host|None, reason). reason in ok | missing_origin | unknown_domain | disabled | insecure"""
    o = request_origin()
    if not o:
        return None, "missing_origin"
    u = urlparse(o)
    host = (u.netloc or "").lower()
    rec = DOMAINS.get(host)
    if not rec:
        return None, "unknown_domain"
    if not rec.get("active", True):
        return None, "disabled"
    local = host.split(":")[0] == "localhost"
    if u.scheme != "https" and not (local and ALLOW_LOCALHOST):
        return None, "insecure"
    # no site key: a registered + active + https Origin is all that is checked (an old X-Site-Key header from a cached app is simply ignored)
    return host, "ok"


def note_blocked(reason):
    o = request_origin() or "(no origin)"
    e = BLOCKED.setdefault(o, {"origin": o, "count": 0})
    e.update(count=e["count"] + 1, last=int(time.time() * 1000), path=request.path[:80], ip=client_ip(), reason=reason)
    if len(BLOCKED) > 200:
        for k in sorted(BLOCKED, key=lambda k: BLOCKED[k]["last"])[:50]:
            BLOCKED.pop(k, None)


# ------------------------------------------------------------------ health
def check(i):
    if not URLS[i]:
        return
    t0 = time.time()
    ok, info, err = False, {}, ""
    try:
        r = requests.get(URLS[i] + "/health", params={"deep": 1}, headers={"X-Internal-Key": INTERNAL_KEY}, timeout=HEALTH_TIMEOUT)
        info = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        ok = r.status_code == 200 and info.get("ok", True) is not False
        if not ok:
            err = f"HTTP {r.status_code}" + (" maintenance" if info.get("maintenance") else "")
    except Exception as e:
        err = str(e)[:120]
    with LOCK:
        s = S[i]
        prev = s["up"]
        s["last"], s["latency"], s["info"], s["error"] = int(time.time() * 1000), int((time.time() - t0) * 1000), info, err
        if ok:
            s["fails"], s["up"] = 0, True
        else:
            s["fails"] += 1
            if s["fails"] >= FAIL_AFTER or s["up"] is None:
                s["up"] = False
        now_up = s["up"]
    if now_up is False and prev is not False and s["mode"] != "off":
        log_event("down", f"{i.upper()} is DOWN ({err or 'no answer'})")
        notify(f"🔴 <b>{i.upper()} is DOWN</b>\n<code>{(err or 'no answer')[:120]}</code>\nTraffic moves to Server-4 (backup) automatically." if i != BACKUP else f"🔴 <b>{i.upper()} (BACKUP) is DOWN</b>\n<code>{(err or 'no answer')[:120]}</code>")
    elif now_up is True and prev is False:
        log_event("up", f"{i.upper()} is back UP")
        notify(f"🟢 <b>{i.upper()} is back UP</b>")


def check_all():
    with ThreadPoolExecutor(5) as ex:
        list(ex.map(check, IDS))


def _health_loop():
    while True:
        try:
            check_all()
        except Exception as e:
            print("[health]", e, flush=True)
        time.sleep(HEALTH_EVERY)


def mark_down(i):
    with LOCK:
        S[i]["fails"] += 1
        if S[i]["fails"] >= FAIL_AFTER:
            S[i]["up"] = False


def available(i):
    """off -> never. on -> always. auto -> health (unknown counts as available so the first request is not refused)."""
    s = S[i]
    if not URLS[i] or s["mode"] == "off":
        return False
    return True if s["mode"] == "on" else s["up"] is not False


def candidates(primary):
    order = [primary] if primary == BACKUP else [primary, BACKUP]
    good = [i for i in order if available(i)]
    if good:
        return good
    # nothing healthy: last resort, try every server that is not switched off (health info may be stale)
    return [i for i in order if URLS[i] and S[i]["mode"] != "off"]


def group_of(path):
    for pre, sid in ROUTES:
        if path == pre or path.startswith(pre + "/"):
            return sid
    return None


# ------------------------------------------------------------------ CORS / rate limit
RATE = {}


def client_ip():
    """Real client IP. Anyone can send their own X-Forwarded-For, but every proxy in front of us APPENDS the address it saw,
    so the trustworthy value is counted from the right (TRUSTED_PROXIES = number of proxies in front of this service)."""
    parts = [p.strip() for p in request.headers.get("X-Forwarded-For", "").split(",") if p.strip()]
    if parts and len(parts) >= TRUSTED_PROXIES:
        return parts[-TRUSTED_PROXIES][:64]
    return (request.remote_addr or "?")[:64]


def rate_ok(ip):
    now, arr = time.time(), [t for t in RATE.get(ip, []) if time.time() - t < 60]
    if len(arr) >= int(SETTINGS.get("rateLimit") or RATE_PER_MIN):
        RATE[ip] = arr
        NOISY[ip] = NOISY.get(ip, 0) + 1
        if len(NOISY) > 500:
            NOISY.clear()
        return False
    arr.append(now)
    RATE[ip] = arr
    if len(RATE) > 30000:
        RATE.clear()
    return True


def cors(r):
    """CORS headers ONLY for a verified domain (never '*')."""
    o = request_origin()
    host = urlparse(o).netloc.lower() if o else ""
    rec = DOMAINS.get(host)
    if o and rec and rec.get("active", True):
        r.headers["Access-Control-Allow-Origin"] = o
        r.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, X-Device-FP, X-Site-Key, X-Admin-Key"
        r.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
        r.headers["Access-Control-Expose-Headers"] = "X-Served-By, X-LB"
        r.headers["Access-Control-Max-Age"] = "600"
        r.headers["Vary"] = "Origin"
    return r


def jerr(msg, _status, **kw):
    r = jsonify(dict(kw, ok=False, error=msg))
    r.status_code = _status
    return cors(r)


def _admin_ok():
    k = request.headers.get("X-Admin-Key", "")
    return bool(ADMIN_KEY) and len(k) == len(ADMIN_KEY) and __import__("hmac").compare_digest(k, ADMIN_KEY)


# ------------------------------------------------------------------ server URLs (add / change from Telegram or the admin panel)
def clean_url(u):
    """'my-s1.onrender.com' / 'https://my-s1.onrender.com/' -> 'https://my-s1.onrender.com'.  '' when it is not a usable address."""
    u = str(u or "").strip().strip("<>\"' ").rstrip("/")
    if not u or " " in u:
        return ""
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    pr = urlparse(u)
    host = (pr.hostname or "").lower()
    if not host or "." not in host and host != "localhost":
        return ""
    if pr.scheme.lower() == "http" and host not in ("localhost", "127.0.0.1") and not ALLOW_LOCALHOST:
        u = "https://" + u[7:]                                  # servers on Render are https; never talk to them in clear text
    return (pr.scheme.lower() + "://" + pr.netloc.lower()) if not pr.path.strip("/") else u.split("?")[0].rstrip("/")


def detect_server(url, timeout=55):
    """Ask the URL who it is. -> {ok, server: 's1'.., title, caps} or {ok: False, error}. Waits up to ~55 s (free Render servers sleep)."""
    url = clean_url(url)
    if not url:
        return {"ok": False, "error": "Ye valid URL nahi hai (jaise https://ff-s1.onrender.com)"}
    try:
        r = requests.get(url + "/health", headers={"X-Internal-Key": INTERNAL_KEY}, timeout=timeout)
    except requests.exceptions.ConnectTimeout:
        return {"ok": False, "url": url, "error": "Server ne jawab nahi diya (free Render par sleep ho sakta hai) - 1 minute baad dobara try karo"}
    except Exception as e:
        return {"ok": False, "url": url, "error": "Server tak pahunch nahi paya: " + str(e)[:90]}
    try:
        j = r.json()
    except Exception:
        return {"ok": False, "url": url, "error": f"HTTP {r.status_code} - ye FF server nahi lagta (health JSON nahi mila)"}
    if not j.get("server"):
        return {"ok": False, "url": url, "error": "Server chal raha hai par INTERNAL_KEY match nahi kar raha - dono jagah same INTERNAL_KEY daalo"}
    return {"ok": True, "url": url, "server": str(j["server"]).lower(), "title": j.get("title") or "", "caps": j.get("caps") or [], "maintenance": bool(j.get("maintenance"))}


def set_server_url(sid, url, who=""):
    if sid not in URLS:
        return False, "unknown server"
    u = clean_url(url)
    if not u:
        return False, "Ye valid URL nahi hai"
    old = URLS[sid]
    URLS[sid], URL_BOT[sid] = u, True
    S[sid].update({"up": None, "fails": 0, "error": ""})
    log_event("server", f"{sid.upper()} URL {'changed' if old else 'added'}: {u.replace('https://', '')}", who)
    _save()
    threading.Thread(target=check, args=(sid,), daemon=True).start()
    return True, None


def clear_server_url(sid, who=""):
    if sid not in URLS or not URLS[sid]:
        return False
    URLS[sid], URL_BOT[sid] = "", False
    S[sid].update({"up": None, "fails": 0, "error": "", "latency": None})
    log_event("server", f"{sid.upper()} URL removed", who)
    _save()
    return True


def apply_settings(d, who=""):
    """Whole-app switches (used by the admin panel; the Telegram bot edits the same SETTINGS). -> list of changes."""
    ch = []
    for k, lab in (("appMaintenance", "maintenance"), ("readOnly", "read-only"), ("alerts", "alerts"), ("domainCheck", "domain check")):
        if k in d and bool(d[k]) != bool(SETTINGS.get(k)):
            SETTINGS[k] = bool(d[k])
            ch.append(f"{lab} {'ON' if SETTINGS[k] else 'OFF'}")
    if isinstance(d.get("maintMsg"), str):
        SETTINGS["maintMsg"] = d["maintMsg"][:300]
    if d.get("rateLimit") not in (None, ""):
        try:
            v = int(d["rateLimit"])
            if 20 <= v <= 100000 and v != SETTINGS["rateLimit"]:
                SETTINGS["rateLimit"] = v
                ch.append(f"rate limit {v}/min")
        except Exception:
            pass
    if isinstance(d.get("features"), dict):
        for k, v in d["features"].items():
            if k in FEATURES:
                if bool(v):
                    if SETTINGS["features"].pop(k, None) is False:
                        ch.append(f"{k} ON")
                elif SETTINGS["features"].get(k) is not False:
                    SETTINGS["features"][k] = False
                    ch.append(f"{k} OFF")
    if isinstance(d.get("featureMsg"), dict):
        for k, v in d["featureMsg"].items():
            if k in FEATURES:
                if str(v).strip():
                    SETTINGS["featureMsg"][k] = str(v)[:300]
                else:
                    SETTINGS["featureMsg"].pop(k, None)
    if ch:
        log_event("control", "admin panel: " + ", ".join(ch), who)
    _save()
    return ch


def overview():
    with LOCK:
        servers = {i: {"url": URLS[i], "urlSource": "app" if URL_BOT[i] else ("env" if URLS[i] else ""), "mode": S[i]["mode"], "up": S[i]["up"], "available": available(i),
                       "fails": S[i]["fails"], "latencyMs": S[i]["latency"], "lastCheck": S[i]["last"], "error": S[i]["error"], "caps": S[i]["info"].get("caps"),
                       "title": S[i]["info"].get("title"), "maintenance": S[i]["info"].get("maintenance"), "uptime": S[i]["info"].get("uptime"), "db": S[i]["info"].get("db"),
                       "traffic": dict(SRV[i])} for i in IDS}
    import tgbot
    return {"servers": servers, "routes": route_table(), "stateStore": bool(state_store()), "stateStoreFromEnv": bool(STATE_STORE_ENV or (URLS["s4"] and not URL_BOT["s4"])),
            "settings": {k: SETTINGS[k] for k in ("domainCheck", "appMaintenance", "maintMsg", "readOnly", "rateLimit", "alerts", "ipBlock")},
            "features": [{"key": k, "label": v[0], "off": SETTINGS["features"].get(k) is False, "message": SETTINGS["featureMsg"].get(k, ""), "default": v[2]} for k, v in FEATURES.items()],
            "stats": dict(STATS, uptime=int(time.time()) - STATS["since"]), "events": list(EVENTS)[:40],
            "domains": [{"id": d["id"], "host": d["host"], "active": d.get("active", True), "addedAt": d.get("addedAt")} for d in DOMAINS.values()],
            "rejected": sorted(BLOCKED.values(), key=lambda x: x["last"], reverse=True)[:10], "noisy": sorted(NOISY.items(), key=lambda kv: -kv[1])[:8],
            "bot": {"configured": bool(tgbot.TOKEN), "admins": len(tgbot.ADMINS), "mode": tgbot.MODE},
            "render": {i: bool(env("RENDER_API_KEY") and env(i.upper() + "_RENDER_ID")) for i in IDS}}


# ------------------------------------------------------------------ routes
def route_table():
    out = {}
    for g in ("s1", "s2", "s3", "s5"):
        c = candidates(g)
        out[g] = c[0] if c else None
    return out


@app.get("/health")
def health():
    return jsonify(ok=True, role="load-balancer", time=int(time.time() * 1000))


@app.get("/lb/health")
def lb_health():
    return jsonify(ok=all(available(i) for i in ("s4",)) or any(available(i) for i in IDS))


@app.get("/lb/admin/servers")
def admin_servers():
    if not _admin_ok():
        return jerr("forbidden", 403), 403
    with LOCK:
        servers = {i: {"url": URLS[i], "mode": S[i]["mode"], "up": S[i]["up"], "available": available(i), "fails": S[i]["fails"], "latencyMs": S[i]["latency"],
                       "lastCheck": S[i]["last"], "error": S[i]["error"], "caps": S[i]["info"].get("caps"), "maintenance": S[i]["info"].get("maintenance")} for i in IDS}
    return cors(jsonify(ok=True, servers=servers, routes=route_table()))


@app.post("/lb/admin/servers/<sid>")
def admin_set(sid):
    if not _admin_ok():
        return jerr("forbidden", 403), 403
    if sid not in S:
        return jerr("unknown server", 404), 404
    d = request.get_json(silent=True) or {}
    out = {}
    if d.get("mode") in ("auto", "on", "off"):
        S[sid]["mode"] = d["mode"]
        out["mode"] = d["mode"]
    if "maintenance" in d and URLS[sid]:                    # also tell the server itself to stop / start serving
        try:
            r = requests.post(URLS[sid] + "/internal/maintenance", json={"on": bool(d["maintenance"])}, headers={"X-Internal-Key": INTERNAL_KEY}, timeout=10)
            out["maintenance"] = r.status_code == 200 and bool(d["maintenance"])
        except Exception as e:
            out["maintenanceError"] = str(e)[:100]
    _save()
    threading.Thread(target=check, args=(sid,), daemon=True).start()
    return cors(jsonify(ok=True, server=sid, **out))


@app.get("/lb/admin/overview")
def admin_overview():
    if not _admin_ok():
        return jerr("forbidden", 403), 403
    return cors(jsonify(ok=True, **overview()))


@app.post("/lb/admin/settings")
def admin_settings():
    if not _admin_ok():
        return jerr("forbidden", 403), 403
    ch = apply_settings(request.get_json(silent=True) or {}, "admin panel")
    return cors(jsonify(ok=True, changed=ch))


@app.post("/lb/admin/ip")
def admin_ip():
    if not _admin_ok():
        return jerr("forbidden", 403), 403
    d = request.get_json(silent=True) or {}
    import ipaddress
    try:
        ip = str(ipaddress.ip_address(str(d.get("ip", "")).strip()))
    except Exception:
        return jerr("Not a valid IP", 400), 400
    if d.get("action") == "unblock":
        if ip in SETTINGS["ipBlock"]:
            SETTINGS["ipBlock"].remove(ip)
    elif ip not in SETTINGS["ipBlock"] and len(SETTINGS["ipBlock"]) < 500:
        SETTINGS["ipBlock"].append(ip)
    log_event("ip", f"{'UNBLOCK' if d.get('action') == 'unblock' else 'BLOCK'} IP {ip}", "admin panel")
    _save()
    return cors(jsonify(ok=True, ipBlock=SETTINGS["ipBlock"]))


@app.post("/lb/admin/detect")
def admin_detect():
    if not _admin_ok():
        return jerr("forbidden", 403), 403
    return cors(jsonify(detect_server((request.get_json(silent=True) or {}).get("url"), 20)))


@app.post("/lb/admin/server-url")
def admin_server_url():
    if not _admin_ok():
        return jerr("forbidden", 403), 403
    d = request.get_json(silent=True) or {}
    sid = str(d.get("sid") or "").lower()
    if d.get("clear"):
        return cors(jsonify(ok=clear_server_url(sid, "admin panel")))
    if not sid:                                             # no server chosen: detect which server this URL is
        det = detect_server(d.get("url"), 20)
        if not det.get("ok"):
            return jerr(det["error"], 400), 400
        sid = det["server"]
    okk, er = set_server_url(sid, d.get("url"), "admin panel")
    return (cors(jsonify(ok=True, sid=sid)) if okk else (jerr(er, 400), 400))


@app.post("/lb/admin/domains")
def admin_domains():
    if not _admin_ok():
        return jerr("forbidden", 403), 403
    d = request.get_json(silent=True) or {}
    host = norm_host(d.get("host") or "")
    act = d.get("action")
    if act == "add":
        if not host:
            return jerr("Not a valid domain", 400), 400
        add_domain(host)
        log_event("domain", f"domain added: {host}", "admin panel")
    elif act in ("remove", "toggle") and host in DOMAINS:
        if act == "remove":
            DOMAINS.pop(host)
        else:
            DOMAINS[host]["active"] = not DOMAINS[host].get("active", True)
        log_event("domain", f"domain {act}: {host}", "admin panel")
        _save()
    return cors(jsonify(ok=True))


@app.post("/lb/admin/check")
def admin_check():
    if not _admin_ok():
        return jerr("forbidden", 403), 403
    check_all()
    return cors(jsonify(ok=True, routes=route_table()))


def _clean_path(path):
    import posixpath, urllib.parse
    p = urllib.parse.unquote("/" + path)
    n = posixpath.normpath(p)
    return "/" if n in (".", "//") else ("/" + n.lstrip("/"))


@app.route("/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
@app.route("/<path:path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
def proxy(path):
    full = _clean_path(path)
    low = full.lower()
    if full == "/":
        return jsonify(ok=True, service="FF Cash Battle API")
    if low.startswith("/internal") or low.startswith("/lb/") or low.startswith("/tg/") or "/internal" in low:
        return jerr("forbidden", 403), 403
    ip = client_ip()
    if ip in SETTINGS["ipBlock"]:
        STATS["blockedIp"] = STATS.get("blockedIp", 0) + 1
        return jerr("Access denied.", 403, code="ip_blocked"), 403
    if not rate_ok(ip):
        STATS["rateLimited"] += 1
        return jerr("Too many requests. Please slow down.", 429, code="rate_limited"), 429

    # ---------- domain verification: only registered, active https domains may use the API (no site key)
    host = None
    if SETTINGS["domainCheck"]:
        if request.method == "OPTIONS":
            h, why = None, "ok"
            o = request_origin()
            rec = DOMAINS.get(urlparse(o).netloc.lower()) if o else None
            if rec and rec.get("active", True):
                return cors(Response("", 204))
            note_blocked("unknown_domain" if o else "missing_origin")
            STATS["blockedDomain"] += 1
            return Response("", 403)
        host, why = verify_domain()
        if not host:
            note_blocked(why)
            STATS["blockedKey" if why == "bad_key" else "blockedDomain"] += 1
            msg = {"bad_key": "Invalid site key for this domain.", "disabled": "This domain has been disabled by the admin.",
                   "insecure": "HTTPS is required."}.get(why, "This domain is not authorised to use this API.")
            return jerr(msg, 403, code="domain_not_verified", reason=why), 403
    elif request.method == "OPTIONS":
        return cors(Response("", 204))
    STATS["allowed"] += 1

    primary = group_of(low)
    if not primary:
        return jerr("Not found", 404), 404
    is_admin = low == "/fraud" or any(low == p or low.startswith(p + "/") for p in ADMIN_PREFIXES)
    if is_admin and not _admin_ok():
        return jerr("forbidden", 403), 403
    if not is_admin:
        g_ = gate(full, request.method)
        if g_:
            return g_

    drop = HOP | {"x-site-key"} | (set() if is_admin else {"x-admin-key"})
    headers = {k: v for k, v in request.headers if k.lower() not in drop}
    headers.update({"X-Real-IP": ip, "X-Forwarded-For": ip, "X-Internal-Key": INTERNAL_KEY})
    data = request.get_data()
    safe = request.method in ("GET", "HEAD")
    tried, last = [], None
    cands = candidates(primary)
    for sid in cands:
        tried.append(sid)
        t_req = time.time()
        try:
            r = sess.request(request.method, URLS[sid] + full, params=request.args, data=data, headers=headers, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                             allow_redirects=False)
        except (requests.ConnectionError, requests.exceptions.ConnectTimeout) as e:
            mark_down(sid)
            last = str(e)[:100]
            continue
        except requests.exceptions.ReadTimeout:
            if safe:
                mark_down(sid)
                last = "timeout"
                continue
            return jerr("The server took too long. Check your balance/history before retrying.", 504, code="timeout"), 504
        maint = r.status_code == 503 and "maintenance" in r.text[:300]
        if r.status_code in (502, 503, 504) and (safe or maint) and sid != cands[-1]:
            if not maint:
                mark_down(sid)
            last = f"HTTP {r.status_code}"
            continue
        c_ = SRV[sid]
        c_["req"] += 1
        c_["ms"] += int((time.time() - t_req) * 1000)
        if r.status_code >= 500:
            c_["err"] += 1
        resp = Response(r.content, status=r.status_code)
        for k, v in r.headers.items():
            if k.lower() not in HOP and not k.lower().startswith("access-control") and k.lower() != "vary":
                resp.headers[k] = v
        resp.headers["X-LB"] = "1"
        resp.headers["X-Served-By"] = sid
        return cors(resp)
    return jerr("Service temporarily unavailable. Please try again in a moment.", 503, code="unavailable", tried=tried, detail=last), 503


def gate(path, method):
    """Admin kill-switches (Telegram bot -> Control): whole-app maintenance, read-only mode, single features. -> error response or None."""
    low = path.lower()
    if SETTINGS["appMaintenance"] and method != "OPTIONS":
        return jerr(SETTINGS["maintMsg"] or "We are under maintenance. Please try again in a little while.", 503, code="maintenance", maintenance=True), 503
    if SETTINGS["readOnly"] and method in ("POST", "PUT", "PATCH", "DELETE") and not low.startswith("/auth"):
        return jerr("Payments and matches are paused for a short time. Please try again soon.", 503, code="read_only"), 503
    off = SETTINGS["features"]
    if off:
        for key, (label, rx, dflt) in FEATURES.items():
            if off.get(key) is False and rx.match(low):
                if key == "games" and "/admin" in low:
                    continue
                return jerr(SETTINGS["featureMsg"].get(key) or dflt, 503, code="feature_off", feature=key), 503
    return None


@app.get("/lb/status")
def lb_status():
    """Public (registered domains only via CORS): lets the app show a maintenance notice. No secrets."""
    r = jsonify(ok=True, maintenance=SETTINGS["appMaintenance"], message=SETTINGS["maintMsg"], readOnly=SETTINGS["readOnly"],
                off=[k for k, v in SETTINGS["features"].items() if v is False])
    return cors(r)


@app.post("/tg/webhook/<secret>")
def tg_webhook(secret):
    import tgbot
    code, text = tgbot.handle_webhook(secret, request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), request.get_json(silent=True))
    return Response(text, code)


def _keepalive_loop():
    """Free Render sleeps after 15 min without traffic - a sleeping load balancer cannot poll Telegram. Ping ourselves."""
    import tgbot
    url = (env("KEEPALIVE_URL") or env("RENDER_EXTERNAL_URL")).rstrip("/")
    flag = env("KEEPALIVE").lower()
    # default: ON for polling (it must stay awake), OFF for webhook (Telegram wakes the service -> saves free hours)
    enabled = flag in ("1", "true", "yes") if flag else (tgbot.MODE == "polling" and bool(tgbot.TOKEN))
    if not url or not enabled:
        return
    time.sleep(60)
    while True:
        try:
            requests.get(url + "/health", timeout=15)
        except Exception:
            pass
        time.sleep(int(env("KEEPALIVE_INTERVAL", "600")))


def _reload_loop():
    """If the state store (Server-4) was asleep at boot, keep retrying until the saved domains / modes are loaded."""
    while not LOADED["ok"]:
        time.sleep(30)
        try:
            _load()
        except Exception:
            pass


_started = False


def start():
    global _started
    if _started:
        return
    _started = True
    _load()
    threading.Thread(target=_health_loop, daemon=True, name="health").start()
    threading.Thread(target=_reload_loop, daemon=True, name="state-reload").start()
    threading.Thread(target=_keepalive_loop, daemon=True, name="keepalive").start()
    import tgbot
    tgbot.start(__import__(__name__))


start()
