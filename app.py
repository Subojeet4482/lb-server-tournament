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
ADMIN_PREFIXES = ("/fraud", "/backup", "/game/ball/admin", "/game/bomb/admin", "/geo/admin")
HEALTH_EVERY = int(env("HEALTH_INTERVAL", "30"))
FAIL_AFTER = int(env("FAIL_THRESHOLD", "2"))
HEALTH_TIMEOUT = float(env("HEALTH_TIMEOUT", "50"))      # Render free servers need up to ~50 s to wake up
CONNECT_TIMEOUT = float(env("CONNECT_TIMEOUT", "10"))
READ_TIMEOUT = float(env("REQUEST_TIMEOUT", "60"))
RATE_PER_MIN = int(env("RATE_LIMIT_PER_MIN", "300"))
STATE_FILE = env("STATE_FILE", "/tmp/lb_state.json")
STATE_STORE_URL = env("STATE_STORE_URL", URLS["s4"]).rstrip("/")

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


# ------------------------------------------------------------------ state (modes + domains + bans survive restarts)
import hashlib, secrets, re, ipaddress
from collections import deque
from urllib.parse import urlparse


def now_ms():
    return int(time.time() * 1000)


DOMAINS = {}          # host -> {"id","host","keyHash","active","addedAt","note","expiresAt","rate","hits","lastUsed"}
SETTINGS = {"domainCheck": DOMAIN_CHECK, "notify": True, "rate": 0, "maintenance": False, "maintMsg": "",
            "autoBackupHours": 0, "autoBackupDb": False, "lastBackupAt": 0, "tgMode": ""}
DENIED = {}           # host -> {"host","at","by","count","last"}   rejected in the bot: always refused and never shown again
BANNED = {}           # ip -> {"ip","at","by","note","until"}         until = 0 -> permanent
EXTRA_ADMINS = {}     # telegram id (str) -> {"id","at","by"}         added by an owner (TG_ADMIN_IDS) from the bot
AUDIT = deque(maxlen=150)     # newest first: {"t","by","do","d"}
BLOCKED = {}          # origin -> request entry (recent refused requests = the "domain requests" queue)
STATS = {"allowed": 0, "blockedDomain": 0, "blockedKey": 0, "rateLimited": 0, "rejected": 0, "banned": 0, "maintenance": 0, "since": int(time.time())}
LOADED = {"ok": False}        # True = in sync with the state store (S4), so saving can never overwrite the stored state with a partial one
DIRTY = {"edited": False}     # something was changed before the store could be read
HOOKS = {"notify": None, "mode": None}    # set by tgbot: new-request notification, bot mode switch
SETTING_KEYS = ("domainCheck", "notify", "rate", "maintenance", "maintMsg", "autoBackupHours", "autoBackupDb", "tgMode")


def _sha(x):
    return hashlib.sha256(x.encode()).hexdigest()


def _int(x, lo, hi, d=0):
    try:
        return max(lo, min(hi, int(x)))
    except Exception:
        return d


def audit(action, detail="", actor="system"):
    AUDIT.appendleft({"t": now_ms(), "by": str(actor)[:40], "do": action, "d": str(detail)[:200]})


# ---- sanitising: used for the saved state AND for a state file uploaded by an admin (never trust its shape)
def _clean_domain(host, d):
    h = norm_host(d.get("host") or host)
    kh = d.get("keyHash") or ""
    if not h or not (kh == "" or re.fullmatch(r"[0-9a-f]{64}", str(kh))):
        return None, None                                   # a broken key hash must never turn into a key-less (open) domain
    return h, {"id": _sha(h)[:8], "host": h, "keyHash": kh, "active": bool(d.get("active", True)), "addedAt": _int(d.get("addedAt"), 0, 4 * 10**12, now_ms()),
               "note": str(d.get("note") or "")[:120], "expiresAt": _int(d.get("expiresAt"), 0, 4 * 10**12), "rate": _int(d.get("rate"), 0, 100000),
               "hits": _int(d.get("hits"), 0, 10**12), "lastUsed": _int(d.get("lastUsed"), 0, 4 * 10**12)}


def sanitize_state(st):
    """-> clean state dict (only known fields, valid values)."""
    out = {"servers": {}, "domains": {}, "denied": {}, "banned": {}, "admins": {}, "settings": {}, "audit": []}
    if not isinstance(st, dict):
        return out
    srv = st.get("servers") if isinstance(st.get("servers"), dict) else {k: v for k, v in st.items() if k in IDS}     # old format = {s1:{mode}}
    for i, v in srv.items():
        if i in IDS and isinstance(v, dict) and v.get("mode") in ("auto", "on", "off"):
            out["servers"][i] = {"mode": v["mode"]}
    for k, d in (st.get("domains") or {}).items() if isinstance(st.get("domains"), dict) else []:
        if isinstance(d, dict):
            h, rec = _clean_domain(k, d)
            if h:
                out["domains"][h] = rec
    for k, d in (st.get("denied") or {}).items() if isinstance(st.get("denied"), dict) else []:
        h = norm_host(k)
        if h and isinstance(d, dict):
            out["denied"][h] = {"host": h, "at": _int(d.get("at"), 0, 4 * 10**12), "by": str(d.get("by") or "")[:40], "count": _int(d.get("count"), 0, 10**12), "last": _int(d.get("last"), 0, 4 * 10**12)}
    for k, d in (st.get("banned") or {}).items() if isinstance(st.get("banned"), dict) else []:
        try:
            ip = str(ipaddress.ip_address(str(k)))
        except ValueError:
            continue
        if isinstance(d, dict):
            out["banned"][ip] = {"ip": ip, "at": _int(d.get("at"), 0, 4 * 10**12), "by": str(d.get("by") or "")[:40], "note": str(d.get("note") or "")[:80], "until": _int(d.get("until"), 0, 4 * 10**12)}
    for k, d in (st.get("admins") or {}).items() if isinstance(st.get("admins"), dict) else []:
        if str(k).lstrip("-").isdigit() and isinstance(d, dict):
            out["admins"][str(k)] = {"id": str(k), "at": _int(d.get("at"), 0, 4 * 10**12), "by": str(d.get("by") or "")[:40]}
    s = st.get("settings") if isinstance(st.get("settings"), dict) else {}
    for k in ("domainCheck", "notify", "maintenance", "autoBackupDb"):
        if k in s:
            out["settings"][k] = bool(s[k])
    if "rate" in s:
        out["settings"]["rate"] = _int(s["rate"], 0, 100000)
    if "autoBackupHours" in s:
        out["settings"]["autoBackupHours"] = _int(s["autoBackupHours"], 0, 720)
    if "lastBackupAt" in s:
        out["settings"]["lastBackupAt"] = _int(s["lastBackupAt"], 0, 4 * 10**12)
    if "maintMsg" in s:
        out["settings"]["maintMsg"] = str(s["maintMsg"] or "")[:200]
    if s.get("tgMode") in ("", "webhook", "polling"):
        out["settings"]["tgMode"] = s["tgMode"]
    for a in (st.get("audit") or [])[:150] if isinstance(st.get("audit"), list) else []:
        if isinstance(a, dict):
            out["audit"].append({"t": _int(a.get("t"), 0, 4 * 10**12), "by": str(a.get("by") or "")[:40], "do": str(a.get("do") or "")[:40], "d": str(a.get("d") or "")[:200]})
    return out


def _snapshot():
    for _ in range(3):
        try:
            with LOCK:
                return json.loads(json.dumps({"servers": {i: {"mode": S[i]["mode"]} for i in IDS}, "domains": DOMAINS, "denied": DENIED, "banned": BANNED,
                                              "admins": EXTRA_ADMINS, "settings": SETTINGS, "audit": list(AUDIT)}))
        except RuntimeError:
            time.sleep(0.01)
    return {}


def export_state():
    st = _snapshot()
    st["version"], st["exportedAt"] = 2, now_ms()
    return st


def _save():
    st = _snapshot()
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(st, f)
    except Exception:
        pass
    if STATE_STORE_URL and INTERNAL_KEY:
        if not LOADED["ok"]:
            DIRTY["edited"] = True          # the store was not read yet: pushing now could overwrite the saved domains with a partial list
            return
        try:
            requests.put(STATE_STORE_URL + "/internal/sys/lbstate", json={"state": st}, headers={"X-Internal-Key": INTERNAL_KEY}, timeout=8)
        except Exception:
            pass


def _apply(st, merge=False):
    """Apply a (sanitised) state. merge=True only ADDS what is missing and keeps everything that is set now."""
    if not isinstance(st, dict):
        return False
    st = sanitize_state(st)
    with LOCK:
        if not merge:
            for i, v in st["servers"].items():
                S[i]["mode"] = v["mode"]
        for name, target in (("domains", DOMAINS), ("denied", DENIED), ("banned", BANNED), ("admins", EXTRA_ADMINS)):
            if not merge:
                target.clear()
            for k, d in st[name].items():
                if merge:
                    target.setdefault(k, d)
                else:
                    target[k] = d
        if not merge:
            SETTINGS.update(st["settings"])
            if st["audit"]:
                AUDIT.clear()
                AUDIT.extend(st["audit"])
        for h in list(DENIED):                       # a domain cannot be approved and rejected at the same time (approved wins)
            if h in DOMAINS:
                DENIED.pop(h, None)
    return True


def state_summary(st):
    st = sanitize_state(st)
    return {"domains": len(st["domains"]), "denied": len(st["denied"]), "banned": len(st["banned"]), "admins": len(st["admins"]), "servers": len(st["servers"])}


def _load(initial=True):
    st, reached = None, False
    if STATE_STORE_URL and INTERNAL_KEY:
        try:
            r = requests.get(STATE_STORE_URL + "/internal/sys/lbstate", headers={"X-Internal-Key": INTERNAL_KEY}, timeout=8)
            if r.status_code == 200:
                reached = True
                inner = r.json().get("state") or {}
                st = inner.get("state") if isinstance(inner.get("state"), dict) else inner
        except Exception:
            pass
    if not reached and not initial:
        return                                       # retry later - never apply an old local file over newer changes
    if not st:
        try:
            st = json.load(open(STATE_FILE))
        except Exception:
            st = None
    if st:
        _apply(st, merge=DIRTY["edited"])            # changed before the store was readable -> keep those changes, add what was saved
    # in sync = the store answered, or there is no store to wait for
    LOADED["ok"] = reached or not (STATE_STORE_URL and INTERNAL_KEY)
    for h in BOOTSTRAP_DOMAINS:
        DOMAINS.setdefault(h, {"id": _sha(h)[:8], "host": h, "keyHash": "", "active": True, "addedAt": now_ms(), "note": "bootstrap (env)", "expiresAt": 0, "rate": 0, "hits": 0, "lastUsed": 0})
    if LOADED["ok"] and DIRTY["edited"]:
        DIRTY["edited"] = False
        _save()
    if LOADED["ok"] and HOOKS["mode"]:
        HOOKS["mode"]()                              # the saved bot mode (Settings -> Bot mode) may differ from env TG_MODE


def import_state(raw, mode="replace", actor="system"):
    """Restore a state exported by export_state(). mode: replace | merge. -> summary dict"""
    clean = sanitize_state(raw)
    _apply(clean, merge=(mode == "merge"))
    audit("state.import", f"{mode}: {state_summary(clean)}", actor)
    _save()
    return state_summary(clean)


# ---------------- domain verification
HOST_RE = re.compile(r"^(localhost|([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24})(:\d{1,5})?$")


def norm_host(text):
    t = (text or "").strip().lower()
    t = re.sub(r"^[a-z]+://", "", t).split("/")[0].split("?")[0].strip()
    return t if HOST_RE.match(t) else ""


def new_site_key():
    return "sk_" + secrets.token_urlsafe(24)


def request_id(origin):
    return _sha(origin)[:8]


def domain_live(rec):
    exp = rec.get("expiresAt") or 0
    return bool(rec.get("active", True)) and not (exp and exp < now_ms())


def add_domain(host, keyless=False, note="", actor="system"):
    """-> (record, plain_key). The plain key is shown ONCE (only its hash is stored). keyless=True -> Origin check only."""
    key = "" if keyless else new_site_key()
    rec = {"id": _sha(host)[:8], "host": host, "keyHash": _sha(key) if key else "", "active": True, "addedAt": now_ms(), "note": note[:120],
           "expiresAt": 0, "rate": 0, "hits": 0, "lastUsed": 0}
    with LOCK:
        DOMAINS[host] = rec
        DENIED.pop(host, None)
        for o in [o for o, e in BLOCKED.items() if e.get("host") == host]:
            BLOCKED.pop(o, None)
    audit("domain.add", host + (" (origin only)" if keyless else ""), actor)
    _save()
    return rec, key


def rotate_key(host, actor="system"):
    key = new_site_key()
    DOMAINS[host]["keyHash"] = _sha(key)
    audit("domain.newkey", host, actor)
    _save()
    return key


def clear_key(host, actor="system"):
    DOMAINS[host]["keyHash"] = ""
    audit("domain.keyoff", host, actor)
    _save()


def set_domain(host, actor="system", **kw):
    """Validated update of one domain: active, note, rate (req/min per IP, 0 = global), expiresAt (ms, 0 = never)."""
    d = DOMAINS.get(host)
    if not d:
        return False
    if "active" in kw:
        d["active"] = bool(kw["active"])
    if "note" in kw:
        d["note"] = str(kw["note"] or "")[:120]
    if "rate" in kw:
        d["rate"] = _int(kw["rate"], 0, 100000)
    if "expiresAt" in kw:
        d["expiresAt"] = _int(kw["expiresAt"], 0, 4 * 10**12)
    audit("domain.edit", f"{host} {', '.join(f'{k}={v}' for k, v in kw.items())}"[:200], actor)
    _save()
    return True


def remove_domain(host, actor="system"):
    with LOCK:
        gone = DOMAINS.pop(host, None)
    if gone:
        audit("domain.remove", host, actor)
        _save()
    return bool(gone)


def domain_by_id(did):
    return next((d for d in DOMAINS.values() if d["id"] == did), None)


def request_by_id(rid):
    return next((e for e in BLOCKED.values() if e.get("id") == rid), None)


def requests_list(mode="n"):
    """n = unknown domains, newest first | t = unknown domains, most requests first | a = every refused request (log)."""
    items = list(BLOCKED.values())
    if mode in ("n", "t"):
        items = [e for e in items if e.get("reason") == "unknown_domain" and e.get("host") and norm_host(e["host"]) and e["host"] not in DOMAINS and e["host"] not in DENIED]
    return sorted(items, key=(lambda e: (-e["count"], -e["last"])) if mode == "t" else (lambda e: -e["last"]))


def approve_request(rid, keyless=False, actor="system"):
    """Accept a domain from the request queue. -> (record, key) or (None, error)"""
    e = request_by_id(rid)
    h = norm_host((e or {}).get("host", ""))
    if not e or not h:
        return None, "request not found (maybe already handled)"
    if h in DOMAINS:
        return None, "already approved"
    return add_domain(h, keyless=keyless, note=f"approved from request ({e['count']} hits)", actor=actor)


def reject_request(rid, actor="system", ban_ip=False):
    """Reject a domain for good: it is refused silently and never shown again (until undone). -> host | None"""
    e = request_by_id(rid)
    h = (e or {}).get("host", "")
    if not e or not h:
        return None
    with LOCK:
        DENIED[h] = {"host": h, "at": now_ms(), "by": str(actor)[:40], "count": e["count"], "last": e["last"]}
        for o in [o for o, x in BLOCKED.items() if x.get("host") == h]:
            BLOCKED.pop(o, None)
    audit("domain.reject", h, actor)
    if ban_ip:
        for ip in e.get("ips", []):
            ban_ip_(ip, 0, f"rejected domain {h}", actor, save=False)
    _save()
    return h


def unreject(host, actor="system"):
    gone = DENIED.pop(host, None)
    if gone:
        audit("domain.unreject", host, actor)
        _save()
    return bool(gone)


def ban_ip_(ip, hours=0, note="", actor="system", save=True):
    try:
        ip = str(ipaddress.ip_address(str(ip).strip()))
    except ValueError:
        return False, "not a valid IP address"
    BANNED[ip] = {"ip": ip, "at": now_ms(), "by": str(actor)[:40], "note": str(note)[:80], "until": now_ms() + int(hours * 3600 * 1000) if hours else 0}
    audit("ip.ban", f"{ip} {'for %sh' % hours if hours else 'permanent'} {note}".strip(), actor)
    if save:
        _save()
    return True, ""


def unban_ip(ip, actor="system"):
    gone = BANNED.pop(ip, None)
    if gone:
        audit("ip.unban", ip, actor)
        _save()
    return bool(gone)


def is_banned(ip):
    b = BANNED.get(ip)
    if not b:
        return False
    if b.get("until") and b["until"] < now_ms():
        BANNED.pop(ip, None)
        return False
    return True


def set_setting(key, value, actor="system"):
    """Validated settings change (same rules for the Telegram bot and the web panel). -> (ok, error)"""
    if key in ("domainCheck", "notify", "maintenance", "autoBackupDb"):
        SETTINGS[key] = bool(value)
    elif key == "rate":
        SETTINGS[key] = _int(value, 0, 100000)
    elif key == "autoBackupHours":
        SETTINGS[key] = _int(value, 0, 720)
    elif key == "maintMsg":
        SETTINGS[key] = str(value or "")[:200]
    elif key == "tgMode":
        if value not in ("", "webhook", "polling"):
            return False, "tgMode must be webhook or polling"
        SETTINGS[key] = value
        if HOOKS["mode"]:
            HOOKS["mode"]()
    else:
        return False, "unknown setting"
    audit("setting", f"{key} = {SETTINGS[key]}", actor)
    _save()
    return True, ""


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
    return o[:120]


def origin_status():
    """-> (host|None, reason, record|None).  reason: ok | missing_origin | unknown_domain | rejected | disabled | expired | insecure"""
    o = request_origin()
    if not o:
        return None, "missing_origin", None
    u = urlparse(o)
    host = (u.netloc or "").lower()
    if host in DENIED:
        return host, "rejected", None
    rec = DOMAINS.get(host)
    if not rec:
        return host, "unknown_domain", None
    if not rec.get("active", True):
        return host, "disabled", rec
    if rec.get("expiresAt") and rec["expiresAt"] < now_ms():
        return host, "expired", rec
    local = host.split(":")[0] == "localhost"
    if u.scheme != "https" and not (local and ALLOW_LOCALHOST):
        return host, "insecure", rec
    return host, "ok", rec


def verify_domain():
    """-> (host|None, reason). reason in ok | missing_origin | unknown_domain | rejected | disabled | expired | insecure | bad_key"""
    host, why, rec = origin_status()
    if why != "ok":
        return None, why
    if rec.get("keyHash"):
        k = request.headers.get("X-Site-Key", "")
        if not k or not hmac.compare_digest(_sha(k), rec["keyHash"]):
            return None, "bad_key"
    return host, "ok"


def note_blocked(reason):
    """Remember a refused request (the Telegram bot / web panel show them so the admin can approve or reject the domain)."""
    o = request_origin() or "(no origin)"
    host = urlparse(o).netloc.lower() if "://" in o else ""
    if reason == "rejected":                                    # already rejected by the admin: count it, never show it again
        STATS["rejected"] += 1
        d = DENIED.get(host)
        if d:
            d["count"], d["last"] = d.get("count", 0) + 1, now_ms()
        return
    ip, now = client_ip(), now_ms()
    with LOCK:
        e = BLOCKED.get(o)
        new = e is None
        if new:
            e = BLOCKED[o] = {"origin": o, "id": request_id(o), "host": host, "count": 0, "first": now, "ips": []}
        e.update(count=e["count"] + 1, last=now, path=request.path[:80], ip=ip, reason=reason, ua=request.headers.get("User-Agent", "")[:160])
        if ip not in e["ips"] and len(e["ips"]) < 5:
            e["ips"].append(ip)
        if len(BLOCKED) > 200:                                  # a flood of fake origins evicts itself first (lowest count, oldest)
            for k in sorted(BLOCKED, key=lambda k: (BLOCKED[k]["count"], BLOCKED[k]["last"]))[:50]:
                BLOCKED.pop(k, None)
        snap = dict(e)
    if new and reason == "unknown_domain" and norm_host(host) and SETTINGS["notify"] and HOOKS["notify"]:
        threading.Thread(target=HOOKS["notify"], args=(snap,), daemon=True).start()


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
        s["last"], s["latency"], s["info"], s["error"] = int(time.time() * 1000), int((time.time() - t0) * 1000), info, err
        if ok:
            s["fails"], s["up"] = 0, True
        else:
            s["fails"] += 1
            if s["fails"] >= FAIL_AFTER or s["up"] is None:
                s["up"] = False


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


def rate_ok(ip, limit=None):
    limit = limit or SETTINGS.get("rate") or RATE_PER_MIN
    now, arr = time.time(), [t for t in RATE.get(ip, []) if time.time() - t < 60]
    if len(arr) >= limit:
        RATE[ip] = arr
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
    if o and rec and domain_live(rec):
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


ADMIN_FAILS = {}


def admin_locked(ip):
    arr = [x for x in ADMIN_FAILS.get(ip, []) if time.time() - x < 600]
    if arr:
        ADMIN_FAILS[ip] = arr
    else:
        ADMIN_FAILS.pop(ip, None)
    return len(arr) >= 10                      # 10 wrong admin keys in 10 minutes -> this IP is locked out for the rest of the window


def _admin_ok():
    k = request.headers.get("X-Admin-Key", "")
    if not ADMIN_KEY or not k:
        return False
    ip = client_ip()
    if admin_locked(ip):
        return False
    ok = hmac.compare_digest(k.encode("utf-8", "ignore"), ADMIN_KEY.encode())
    if not ok:
        ADMIN_FAILS.setdefault(ip, []).append(time.time())
        if len(ADMIN_FAILS) > 5000:
            ADMIN_FAILS.clear()
    return ok


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
    audit("server.edit", f"{sid} {out}", "web/api")
    _save()
    threading.Thread(target=check, args=(sid,), daemon=True).start()
    return cors(jsonify(ok=True, server=sid, **out))


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
    if is_banned(ip):                                          # banned from the bot / web panel: silent refusal
        STATS["banned"] += 1
        return Response("", 403)
    if not rate_ok(ip):
        STATS["rateLimited"] += 1
        return jerr("Too many requests. Please slow down.", 429, code="rate_limited"), 429

    # ---------- domain verification: only registered, active domains (with their site key) may use the API
    host = None
    if SETTINGS["domainCheck"]:
        if request.method == "OPTIONS":
            h, why, rec = origin_status()
            if why in ("ok", "insecure"):
                return cors(Response("", 204))
            note_blocked(why)
            STATS["blockedDomain"] += 1
            return Response("", 403)
        host, why = verify_domain()
        if not host:
            note_blocked(why)
            STATS["blockedKey" if why == "bad_key" else "blockedDomain"] += 1
            msg = {"bad_key": "Invalid site key for this domain.", "disabled": "This domain has been disabled by the admin.", "expired": "This domain's access has expired.",
                   "insecure": "HTTPS is required."}.get(why, "This domain is not authorised to use this API.")
            return jerr(msg, 403, code="domain_not_verified", reason=why), 403
        rec = DOMAINS.get(host) or {}
        rec["hits"], rec["lastUsed"] = rec.get("hits", 0) + 1, now_ms()
        if rec.get("rate") and not rate_ok(f"{ip}|{host}", rec["rate"]):          # per-domain limit set in the bot / web panel
            STATS["rateLimited"] += 1
            return jerr("Too many requests. Please slow down.", 429, code="rate_limited"), 429
    elif request.method == "OPTIONS":
        return cors(Response("", 204))
    if SETTINGS["maintenance"] and not _admin_ok():
        STATS["maintenance"] += 1
        return jerr(SETTINGS["maintMsg"] or "We are under maintenance. Please try again shortly.", 503, code="maintenance"), 503
    STATS["allowed"] += 1

    primary = group_of(low)
    if not primary:
        return jerr("Not found", 404), 404
    is_admin = low == "/fraud" or any(low == p or low.startswith(p + "/") for p in ADMIN_PREFIXES)
    if is_admin and not _admin_ok():
        return jerr("forbidden", 403), 403

    drop = HOP | {"x-site-key"} | (set() if is_admin else {"x-admin-key"})
    headers = {k: v for k, v in request.headers if k.lower() not in drop}
    headers.update({"X-Real-IP": ip, "X-Forwarded-For": ip, "X-Internal-Key": INTERNAL_KEY})
    data = request.get_data()
    safe = request.method in ("GET", "HEAD")
    tried, last = [], None
    cands = candidates(primary)
    for sid in cands:
        tried.append(sid)
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
        resp = Response(r.content, status=r.status_code)
        for k, v in r.headers.items():
            if k.lower() not in HOP and not k.lower().startswith("access-control") and k.lower() != "vary":
                resp.headers[k] = v
        resp.headers["X-LB"] = "1"
        resp.headers["X-Served-By"] = sid
        return cors(resp)
    return jerr("Service temporarily unavailable. Please try again in a moment.", 503, code="unavailable", tried=tried, detail=last), 503


@app.post("/tg/webhook/<secret>")
def tg_webhook(secret):
    import tgbot
    code, text = tgbot.handle_webhook(secret, request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), request.get_json(silent=True))
    return Response(text, code)


def _keepalive_loop():
    """Free Render sleeps after 15 min without traffic - a sleeping load balancer cannot poll Telegram. Ping ourselves.
    Re-evaluated every round, so switching the bot to polling from the bot / web panel turns it on without a restart."""
    import tgbot
    url = (env("KEEPALIVE_URL") or env("RENDER_EXTERNAL_URL")).rstrip("/")
    time.sleep(60)
    while True:
        flag = env("KEEPALIVE").lower()
        # default: ON for polling (it must stay awake), OFF for webhook (Telegram wakes the service -> saves free hours)
        enabled = flag in ("1", "true", "yes") if flag else (tgbot.MODE == "polling" and bool(tgbot.TOKEN))
        if url and enabled:
            try:
                requests.get(url + "/health", timeout=15)
            except Exception:
                pass
        time.sleep(max(60, int(env("KEEPALIVE_INTERVAL", "600"))))


def _reload_loop():
    """If the state store (Server-4) was asleep at boot, keep retrying until the saved domains / modes are loaded."""
    while not LOADED["ok"]:
        time.sleep(30)
        try:
            _load(initial=False)
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


import webui                                   # web control panel  (/lb/ui)  - registered before start()
webui.init(app, __import__(__name__))
start()
