"""Telegram control bot - manage the whole system with inline buttons (servers, domains, fraud cases, backups).
Private: only Telegram user ids listed in TG_ADMIN_IDS can use it. Runs inside the load balancer.

TG_MODE=webhook (default)  Telegram POSTs every update to  PUBLIC_URL/tg/webhook/<secret>.  Best for FREE Render: the service may sleep
                           and is woken by the incoming message. The webhook is registered automatically at every start.
TG_MODE=polling            the bot asks Telegram for updates (getUpdates). Best for a paid / always-on server. No public URL needed.
Only ONE mode is ever active: webhook mode never calls getUpdates, polling mode deletes the webhook first and rejects webhook calls."""
import os, re, time, html, hmac, hashlib, threading, collections, requests

TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
ADMINS = {int(x) for x in os.environ.get("TG_ADMIN_IDS", "").replace(" ", "").split(",") if x.strip().lstrip("-").isdigit()}
PUBLIC_URL = (os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
UPDATES = ["message", "callback_query"]


def log(*a):
    print("[tg]", *a, flush=True)


MODE = os.environ.get("TG_MODE", "webhook").strip().lower()
if MODE not in ("webhook", "polling"):
    log(f"WARNING: TG_MODE='{MODE}' is not valid (use webhook or polling) - falling back to webhook")
    MODE = "webhook"

# secret token that Telegram sends back in the X-Telegram-Bot-Api-Secret-Token header. If TG_WEBHOOK_SECRET is empty it is derived
# from the bot token (stable across restarts, so you do not have to configure anything).
SECRET = os.environ.get("TG_WEBHOOK_SECRET", "").strip()
if SECRET and not re.fullmatch(r"[A-Za-z0-9_-]{8,256}", SECRET):
    log("WARNING: TG_WEBHOOK_SECRET must be 8-256 chars of A-Z a-z 0-9 _ - ; using an automatic secret instead")
    SECRET = ""
if not SECRET and TOKEN:
    SECRET = hashlib.sha256(("ffcb-webhook:" + TOKEN).encode()).hexdigest()[:48]

L = None                     # the load balancer module (set by init)
SEEN = collections.OrderedDict()     # update_id -> time  (duplicate protection)
SEEN_LOCK = threading.Lock()
SEEN_MAX = 5000
_warn_at = {"auth": 0}
PENDING = {}                 # chat_id -> {"await": "add_domain"}
VIEW = {}                    # chat_id -> {"blocked": [origins]}
NAMES = {"s1": "Account", "s2": "History", "s3": "Chat & Game", "s4": "Backup (all-in-one)", "s5": "Fraud check"}
GROUPS = [("Account / Balance / Match", "s1"), ("History", "s2"), ("Chat & Game", "s3"), ("Fraud check", "s5")]


# ------------------------------------------------------------------ telegram plumbing
def api(method, _t=20, **p):
    """Call the Telegram Bot API. Retries once on 429 (rate limit). Failures are logged with Telegram's own description."""
    if not TOKEN:
        return {"ok": False, "description": "TG_BOT_TOKEN not set"}
    for attempt in (1, 2):
        try:
            r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", json=p, timeout=_t).json()
        except Exception as e:
            log(f"{method} request failed: {type(e).__name__}: {str(e)[:120]}")
            return {"ok": False, "description": str(e)[:120]}
        if r.get("ok"):
            return r
        wait = (r.get("parameters") or {}).get("retry_after")
        if r.get("error_code") == 429 and wait and wait <= 10 and attempt == 1:
            time.sleep(wait + 0.5)
            continue
        d = str(r.get("description", ""))
        if "message is not modified" not in d and not (method == "getUpdates" and r.get("error_code") == 409):
            log(f"{method} failed: {r.get('error_code')} {d[:160]}")
        return r
    return r


def esc(x):
    return html.escape(str(x if x is not None else ""))


def B(text, data):
    return {"text": text, "callback_data": data}


def show(chat, msg, text, rows=None):
    """Edit the existing message (menus feel like an app) or send a new one."""
    p = dict(chat_id=chat, text=text, parse_mode="HTML", disable_web_page_preview=True, reply_markup={"inline_keyboard": rows or []})
    if msg:
        r = api("editMessageText", message_id=msg, **p)
        if r.get("ok") or "not modified" in str(r.get("description", "")):
            return
    api("sendMessage", **p)


def ago(ms):
    if not ms:
        return "never"
    s = int(time.time() - ms / 1000)
    return f"{s}s ago" if s < 90 else f"{s // 60} min ago" if s < 5400 else f"{s // 3600} h ago"


# ------------------------------------------------------------------ screens
def st_icon(i):
    s = L.S[i]
    if s["mode"] == "off":
        return "⛔"
    if s["up"] is None:
        return "⏳"
    return "✅" if s["up"] else "❌"


def mode_label(m):
    return {"auto": "AUTO", "on": "FORCED ON", "off": "OFF (admin)"}[m]


def main_text():
    up = sum(1 for i in L.IDS if L.available(i))
    act = sum(1 for d in L.DOMAINS.values() if d.get("active", True))
    chk = "🟢 ON" if L.SETTINGS["domainCheck"] else "🔴 OFF (anyone can call the API!)"
    return ("🛡 <b>FF Cash Battle — Control Panel</b>\n\n"
            f"🖥 Servers online: <b>{up}/5</b>\n🌐 Verified domains: <b>{act}</b>\n🔐 Domain check: {chk}\n\n"
            "Neeche se option choose karo 👇")


def main_rows():
    return [[B("🖥 Servers", "s:list"), B("🌐 Domains", "d:list")],
            [B("🛡 Fraud cases", "f:list"), B("🚫 Blocked requests", "b:list")],
            [B("🗺 State payment block", "g:menu")],
            [B("💾 Backup", "bk:menu"), B("📊 Stats", "st:view")],
            [B("⚙️ Settings", "set:menu"), B("🔄 Refresh", "m:main")]]


def servers_screen():
    lines = ["🖥 <b>Servers</b>\n"]
    for i in L.IDS:
        s = L.S[i]
        lat = f" · {s['latency']} ms" if s["latency"] is not None and s["mode"] != "off" else ""
        lines.append(f"{st_icon(i)} <b>{i.upper()}</b> {NAMES[i]} — {mode_label(s['mode'])}{lat}")
    rt = L.route_table()
    lines.append("\n🔀 <b>Traffic route</b>")
    for label, g in GROUPS:
        to = rt.get(g)
        tag = "❌ no server available" if not to else (f"{to.upper()}" + ("  ⚠️ <i>(backup in use)</i>" if to != g else ""))
        lines.append(f"• {label} → {tag}")
    rows, row = [], []
    for i in L.IDS:
        row.append(B(f"{st_icon(i)} {i.upper()} {NAMES[i].split()[0]}", f"s:{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([B("🔄 Check now", "s:check"), B("⬅️ Back", "m:main")])
    return "\n".join(lines), rows


def server_screen(i):
    s = L.S[i]
    host = L.URLS[i].replace("https://", "").replace("http://", "") or "— (URL not set)"
    txt = (f"{st_icon(i)} <b>{i.upper()} — {NAMES[i]}</b>\n\n🔗 <code>{esc(host)}</code>\n⚙️ Mode: <b>{mode_label(s['mode'])}</b>\n"
           f"📡 Health: <b>{'UP' if s['up'] else 'DOWN' if s['up'] is False else 'not checked'}</b>\n⏱ Latency: {s['latency'] if s['latency'] is not None else '-'} ms\n"
           f"🕒 Last check: {ago(s['last'])}\n🛠 Maintenance: {'ON' if s['info'].get('maintenance') else 'off'}\n")
    if s["error"]:
        txt += f"⚠️ Last error: <code>{esc(s['error'][:120])}</code>\n"
    txt += ("\n<b>AUTO</b> = health check se (down ho to traffic Server-4 par)\n<b>FORCED ON</b> = hamesha use karo\n<b>OFF</b> = traffic bilkul band, Server-4 sambhalega")
    if i == "s4":
        txt += "\n\n⚠️ Server-4 hi backup hai — ise OFF karoge to failover nahi chalega."
    rows = [[B("🤖 Auto", f"sm:{i}:auto"), B("🟢 Force ON", f"sm:{i}:on"), B("🔴 OFF", f"sm:{i}:off")],
            [B("🛠 Maintenance ON", f"sx:{i}:1"), B("✅ Maintenance OFF", f"sx:{i}:0")],
            [B("🔄 Check", f"s:chk:{i}"), B("⬅️ Servers", "s:list")]]
    return txt, rows


def domains_screen():
    if not L.DOMAINS:
        txt = ("🌐 <b>Verified domains</b>\n\nAbhi koi domain add nahi hai, isliye API par <b>saari requests reject</b> ho rahi hain.\n"
               "➕ Add domain dabao aur apni user-panel ka domain bhejo.")
    else:
        lines = ["🌐 <b>Verified domains</b>\n"]
        for n, d in enumerate(sorted(L.DOMAINS.values(), key=lambda x: x["addedAt"]), 1):
            lines.append(f"{n}. {'✅' if d.get('active', True) else '⏸'} <code>{esc(d['host'])}</code> · key: {'🔑 set' if d.get('keyHash') else 'none'}")
        lines.append("\nSirf inhi domains ki requests accept hongi, baaki sab reject.")
        txt = "\n".join(lines)
    rows = [[B(f"{'✅' if d.get('active', True) else '⏸'} {d['host']}", f"d:{d['id']}")] for d in sorted(L.DOMAINS.values(), key=lambda x: x["addedAt"])]
    rows.append([B("➕ Add domain", "d:add")])
    rows.append([B("⬅️ Back", "m:main")])
    return txt, rows


def domain_screen(d):
    txt = (f"🌐 <b>{esc(d['host'])}</b>\n\nStatus: <b>{'✅ Active' if d.get('active', True) else '⏸ Disabled'}</b>\n"
           f"Site key: {'🔑 set (security ke liye dobara dikhayi nahi jati)' if d.get('keyHash') else 'none (sirf Origin check)'}\n"
           f"Added: {ago(d['addedAt'])}\n\nKey kho gayi ho to ♻️ New site key dabao (purani key turant band ho jayegi).")
    rows = [[B("♻️ New site key", f"dk:{d['id']}"), B("▶️ Enable" if not d.get("active", True) else "⏸ Disable", f"dt:{d['id']}")],
            [B("🗑 Remove", f"dd:{d['id']}")], [B("⬅️ Domains", "d:list")]]
    return txt, rows


def key_message(host, key, new=True):
    return (f"{'✅ <b>Domain verified &amp; added</b>' if new else '♻️ <b>New site key generated</b>'}\n\n🌐 <code>{esc(host)}</code>\n\n"
            f"🔑 Site key <i>(sirf ek baar dikhegi — abhi copy karo)</i>:\n<code>{esc(key)}</code>\n\n"
            "<b>Frontend me kya karna hai</b>\nHar API request me ye header bhejo:\n"
            f"<code>X-Site-Key: {esc(key)}</code>\n\n"
            f"Browser <code>Origin</code> header khud bhejta hai. Sirf <code>https://{esc(host)}</code> se aayi aur sahi key wali requests accept hongi — baaki sab reject.")


def blocked_screen(chat):
    items = sorted(L.BLOCKED.values(), key=lambda x: x["last"], reverse=True)[:8]
    VIEW[chat] = {"blocked": [x["origin"] for x in items]}
    if not items:
        return "🚫 <b>Blocked requests</b>\n\n✅ Abhi tak koi unverified request reject nahi hui.", [[B("⬅️ Back", "m:main")]]
    why = {"unknown_domain": "domain registered nahi", "bad_key": "galat site key", "missing_origin": "Origin header nahi", "disabled": "domain disabled", "insecure": "http (https chahiye)"}
    lines = ["🚫 <b>Blocked requests</b> <i>(recent)</i>\n"]
    for n, x in enumerate(items, 1):
        lines.append(f"{n}. <code>{esc(x['origin'])}</code> — {x['count']}× · {why.get(x['reason'], x['reason'])}\n    last: {ago(x['last'])} · <code>{esc(x['path'])}</code> · IP {esc(x['ip'])}")
    lines.append("\nAgar koi domain tumhara apna hai to niche se allow kar do.")
    rows = []
    for n, x in enumerate(items):
        h = L.norm_host(x["origin"])
        if h and h not in L.DOMAINS:
            rows.append([B(f"➕ Allow {h}", f"ba:{n}")])
    rows.append([B("🧹 Clear log", "b:clear"), B("⬅️ Back", "m:main")])
    return "\n".join(lines), rows


def stats_screen():
    S_ = L.STATS
    up = int(time.time()) - S_["since"]
    txt = ("📊 <b>Stats</b> <i>(since last restart)</i>\n\n"
           f"✅ Allowed requests: <b>{S_['allowed']}</b>\n🚫 Blocked (domain): <b>{S_['blockedDomain']}</b>\n🔑 Blocked (wrong site key): <b>{S_['blockedKey']}</b>\n"
           f"⏳ Rate limited: <b>{S_['rateLimited']}</b>\n\n🕒 Load balancer uptime: {up // 3600} h {up % 3600 // 60} min")
    return txt, [[B("🔄 Refresh", "st:view"), B("⬅️ Back", "m:main")]]


def admin_call(group, method, path, js=None):
    """Call an admin endpoint of a server through the same fallback chain the load balancer uses (e.g. fraud: S5 -> S4)."""
    hdr = {"X-Internal-Key": L.INTERNAL_KEY, "X-Admin-Key": L.ADMIN_KEY}
    last = None
    for sid in L.candidates(group):
        try:
            r = L.sess.request(method, L.URLS[sid] + path, json=js, headers=hdr, timeout=40)
            if r.status_code in (502, 503, 504):
                last = f"{sid}: HTTP {r.status_code}"
                continue
            return r.status_code, (r.json() if r.content else {}), sid
        except Exception as e:
            last = f"{sid}: {str(e)[:80]}"
    return 0, {"error": last or "no server"}, None


def fraud_screen():
    st, j, sid = admin_call("s5", "GET", "/fraud/cases?status=open")
    if st != 200:
        return f"🛡 <b>Fraud cases</b>\n\n❌ Fraud server se data nahi mila.\n<code>{esc(j.get('error', st))}</code>", [[B("🔄 Retry", "f:list"), B("⬅️ Back", "m:main")]]
    cases = j.get("cases", [])
    if not cases:
        return "🛡 <b>Fraud cases</b>\n\n✅ Koi open case nahi hai.", [[B("🔄 Refresh", "f:list"), B("⬅️ Back", "m:main")]]
    lines = [f"🛡 <b>Fraud cases</b> — {len(cases)} open <i>(via {sid.upper()})</i>\n"]
    rows = []
    for c in cases[:5]:
        lines.append(f"<b>#{esc(c['id'][:8])}</b> · {esc(', '.join(c.get('why', [])))}\n   ✅ Kept: <code>{esc(c.get('keepUid'))}</code>\n   🚫 Banned: {len(c.get('bannedUids') or [])} · Review: {len(c.get('victims') or [])}\n   Mode: {esc(c.get('action'))}")
        rows.append([B(f"🔓 Unban #{c['id'][:6]}", f"fc:{c['id']}:u"), B(f"✅ Confirm #{c['id'][:6]}", f"fc:{c['id']}:c")])
    lines.append("\n🔓 Unban = ban hata ke whitelist · ✅ Confirm = ban final")
    rows.append([B("🔄 Refresh", "f:list"), B("⬅️ Back", "m:main")])
    return "\n".join(lines), rows


def settings_screen():
    on = L.SETTINGS["domainCheck"]
    txt = (f"⚙️ <b>Settings</b>\n\n🔐 Domain verification: <b>{'🟢 ON' if on else '🔴 OFF'}</b>\n"
           f"🤖 Bot mode: <b>{MODE.upper()}</b>\n\n"
           "ON = sirf registered domains ki requests chalti hain.\nOFF = emergency mode, koi bhi domain API use kar sakta hai (sirf debugging ke liye).")
    return txt, [[B("🔴 Turn OFF" if on else "🟢 Turn ON", "set:dc:0" if on else "set:dc:1")], [B("🔎 Bot status", "set:bot")], [B("⬅️ Back", "m:main")]]


def bot_status_screen():
    info = api("getWebhookInfo").get("result") or {}
    url = (info.get("url") or "").replace(SECRET, "***") if SECRET else (info.get("url") or "")
    lines = ["🔎 <b>Bot status</b>\n", f"Mode: <b>{MODE.upper()}</b>"]
    if MODE == "webhook":
        okurl = bool(info.get("url")) and info.get("url", "").endswith("/tg/webhook/" + SECRET)
        lines += [f"Webhook: {'✅ registered' if okurl else '❌ NOT registered'}", f"URL: <code>{esc(url or '-')}</code>", f"Pending updates: {info.get('pending_update_count', '?')}"]
        if info.get("last_error_message"):
            lines.append(f"⚠️ Last Telegram error: <code>{esc(info['last_error_message'][:140])}</code> ({ago(int(info.get('last_error_date', 0)) * 1000)})")
    else:
        lines += [f"Polling: ✅ active", f"Webhook: {'⚠️ still set (will be removed)' if info.get('url') else 'none (correct)'}"]
    lines.append(f"Admins allowed: {len(ADMINS)}")
    return "\n".join(lines), [[B("🔄 Refresh", "set:bot"), B("⬅️ Back", "set:menu")]]


def backup_screen():
    return ("💾 <b>Backup</b>\n\nServer-4 sab databases ka JSON backup banata hai.\n(Render free par disk restart pe saaf hota hai — download kar lo ya DB-4 use karo.)",
            [[B("▶️ Run backup now", "bk:run"), B("📂 List backups", "bk:list")], [B("⬅️ Back", "m:main")]])


# ------------------------------------------------------------------ state payment block
GEO_FLAGS = [("blockDeposit", "💳 Deposit / payment"), ("blockGift", "🎁 Gift code"), ("blockPaidJoin", "🎮 Paid match join"),
             ("blockPaidGame", "🪙 Paid Coin Battle"), ("blockWithdraw", "💸 Withdraw")]


def geo_load():
    st, j, sid = admin_call("s1", "GET", "/geo/admin/config")
    return (j.get("config"), j.get("states", []), None) if st == 200 and j.get("config") else (None, [], j.get("error") or f"HTTP {st}")


def geo_save(patch):
    st, j, sid = admin_call("s1", "POST", "/geo/admin/config", patch)
    return st == 200, (j.get("error") if st != 200 else None)


def geo_screen(note=""):
    cfg, states, e = geo_load()
    if not cfg:
        return f"🗺 <b>State payment block</b>\n\n❌ Server se config nahi mila.\n<code>{esc(e)}</code>", [[B("🔄 Retry", "g:menu"), B("⬅️ Back", "m:main")]]
    lines = [note + "🗺 <b>State payment block</b>\n",
             f"Status: <b>{'🟢 ON' if cfg['enabled'] else '🔴 OFF (koi block nahi)'}</b>",
             f"Blocked states ({len(cfg['blockedStates'])}): " + (", ".join(f"<b>{esc(x)}</b>" for x in cfg["blockedStates"]) or "—"),
             "\n<b>Blocked state ke user ke liye:</b> 🚫 = band, ✅ = chalu"]
    for k, label in GEO_FLAGS:
        lines.append(f"{'🚫' if cfg.get(k) else '✅'} {label}")
    lines.append("✅ Free match / free game: hamesha chalu")
    lines.append(f"\n💬 <b>Message:</b>\n<i>{esc(cfg['message'])}</i>")
    rows = [[B("🔴 Turn OFF" if cfg["enabled"] else "🟢 Turn ON", "g:m")]]
    for i in range(0, len(GEO_FLAGS), 2):
        rows.append([B(f"{'🚫' if cfg.get(k) else '✅'} {lbl}", f"g:t:{k}") for k, lbl in GEO_FLAGS[i:i + 2]])
    rows += [[B("🗺 Blocked states", "g:st:0")], [B("✏️ Edit message", "g:msg"), B("♻️ Default message", "g:msgreset")],
             [B("🔎 Check a user", "g:u")], [B("⬅️ Back", "m:main")]]
    return "\n".join(lines), rows


def geo_states_screen(page):
    cfg, states, e = geo_load()
    if not cfg:
        return geo_screen()
    per = 12
    pages = (len(states) + per - 1) // per
    page = max(0, min(page, pages - 1))
    blocked = set(cfg["blockedStates"])
    rows, row = [], []
    for i in range(page * per, min(len(states), (page + 1) * per)):
        row.append(B(("🚫 " if states[i] in blocked else "✅ ") + states[i], f"g:s:{i}:{page}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    nav = []
    if page > 0:
        nav.append(B("⬅️ Prev", f"g:st:{page - 1}"))
    nav.append(B(f"{page + 1}/{pages}", f"g:st:{page}"))
    if page < pages - 1:
        nav.append(B("Next ➡️", f"g:st:{page + 1}"))
    rows += [nav, [B("⬅️ Back", "g:menu")]]
    return (f"🗺 <b>Blocked states</b> — {len(blocked)} blocked\n\nState dabao: 🚫 = payment band, ✅ = payment chalu.\n"
            "Change turant lagu hota hai."), rows


def geo_user_screen(ident):
    st, j, sid = admin_call("s1", "GET", "/geo/admin/user/" + requests.utils.quote(ident, safe=""))
    if st != 200:
        return f"❌ User nahi mila: <code>{esc(ident)}</code>", [[B("🔎 Try again", "g:u"), B("⬅️ Back", "g:menu")]]
    ev = j.get("geo") or {}
    res = (f"🚫 <b>BLOCKED</b> ({esc(ev.get('reason'))})" if ev.get("blocked") else "✅ <b>Allowed</b>")
    txt = (f"🔎 <b>{esc(j.get('name'))}</b> · ID {esc(j.get('playerId'))}\n<code>{esc(j['uid'])}</code>\n\n"
           f"📍 Declared state: <b>{esc(j.get('declaredState') or 'not set')}</b>\n"
           f"🌐 IP state: <b>{esc(j.get('ipState') or 'unknown')}</b> {esc(j.get('ipCountry') or '')} · checked {ago(j.get('checkedAt'))}\n"
           f"⏳ Blocked IP last seen: {ago(j.get('seenBlockedAt')) if j.get('seenBlockedAt') else 'never'}\n"
           f"🛠 Admin override: <b>{esc(j.get('override') or 'none')}</b>\n\n➡️ Result: {res}")
    u = j["uid"]
    return txt, [[B("✅ Allow user", f"g:ov:{u}:a"), B("🚫 Block user", f"g:ov:{u}:b")], [B("🧹 Clear override", f"g:ov:{u}:c")], [B("⬅️ Back", "g:menu")]]


def on_geo(chat, msg, p):
    a = p[1]
    if a == "menu":
        PENDING.pop(chat, None)
        return show(chat, msg, *geo_screen())
    if a == "m":
        cfg, _, e = geo_load()
        ok_, er = geo_save({"enabled": not cfg["enabled"]}) if cfg else (False, e)
        return show(chat, msg, *geo_screen("✅ Updated\n\n" if ok_ else f"❌ {esc(er)}\n\n"))
    if a == "t" and len(p) == 3 and p[2] in dict(GEO_FLAGS):
        cfg, _, e = geo_load()
        ok_, er = geo_save({p[2]: not cfg.get(p[2])}) if cfg else (False, e)
        return show(chat, msg, *geo_screen("✅ Updated\n\n" if ok_ else f"❌ {esc(er)}\n\n"))
    if a == "st":
        return show(chat, msg, *geo_states_screen(int(p[2]) if len(p) > 2 and p[2].isdigit() else 0))
    if a == "s" and len(p) == 4 and p[2].isdigit():
        cfg, states, e = geo_load()
        if cfg and int(p[2]) < len(states):
            name, cur = states[int(p[2])], set(cfg["blockedStates"])
            cur.symmetric_difference_update({name})
            geo_save({"blockedStates": sorted(cur)})
        return show(chat, msg, *geo_states_screen(int(p[3]) if p[3].isdigit() else 0))
    if a == "msg":
        PENDING[chat] = {"await": "geo_message"}
        return show(chat, msg, "✏️ <b>Message edit</b>\n\nBlocked state ke user ko jo text dikhana hai wo bhejo (max 300 characters).\n/cancel", [[B("✖️ Cancel", "g:menu")]])
    if a == "msgreset":
        ok_, er = geo_save({"message": ""})
        return show(chat, msg, *geo_screen("♻️ Default message set\n\n" if ok_ else f"❌ {esc(er)}\n\n"))
    if a == "u":
        PENDING[chat] = {"await": "geo_user"}
        return show(chat, msg, "🔎 <b>Check a user</b>\n\nUser ki Player ID (7 digit) ya uid bhejo.\n/cancel", [[B("✖️ Cancel", "g:menu")]])
    if a == "ov" and len(p) == 4 and p[3] in ("a", "b", "c"):
        mode = {"a": "allow", "b": "block", "c": "clear"}[p[3]]
        st, j, sid = admin_call("s1", "POST", f"/geo/admin/user/{requests.utils.quote(p[2], safe='')}/override", {"mode": mode})
        t, r = geo_user_screen(p[2])
        return show(chat, msg, (f"✅ Override → <b>{mode}</b>\n\n" if st == 200 else f"❌ Failed\n\n") + t, r)
    return show(chat, msg, *geo_screen())


# ------------------------------------------------------------------ actions
def do_mode(i, mode):
    L.S[i]["mode"] = mode
    L._save()
    threading.Thread(target=L.check, args=(i,), daemon=True).start()


def do_maint(i, on):
    try:
        r = requests.post(L.URLS[i] + "/internal/maintenance", json={"on": on}, headers={"X-Internal-Key": L.INTERNAL_KEY}, timeout=15)
        threading.Thread(target=L.check, args=(i,), daemon=True).start()
        return r.status_code == 200
    except Exception:
        return False


def add_domain_text(chat, text):
    host = L.norm_host(text)
    if not host:
        return ("❌ <b>Ye valid domain nahi hai.</b>\n\nSahi format: <code>app.example.com</code>\n(https:// aur / path mat likho). Dobara bhejo ya /cancel.", [[B("✖️ Cancel", "d:list")]])
    if host in L.DOMAINS:
        PENDING.pop(chat, None)
        return (f"ℹ️ <code>{esc(host)}</code> pehle se add hai.", [[B("Open", f"d:{L.DOMAINS[host]['id']}"), B("⬅️ Domains", "d:list")]])
    if host.split(":")[0] == "localhost" and not L.ALLOW_LOCALHOST:
        return ("❌ localhost allow nahi hai. Testing ke liye LB env me <code>ALLOW_LOCALHOST=true</code> karo.", [[B("✖️ Cancel", "d:list")]])
    PENDING.pop(chat, None)
    rec, key = L.add_domain(host)
    return key_message(host, key), [[B("Open domain", f"d:{rec['id']}"), B("🌐 Domains", "d:list")]]


# ------------------------------------------------------------------ dispatcher
def on_callback(chat, msg, data):
    p = data.split(":")
    k = p[0]
    if k == "m":
        PENDING.pop(chat, None)
        return show(chat, msg, main_text(), main_rows())
    if k == "s":
        if p[1] == "list":
            return show(chat, msg, *servers_screen())
        if p[1] == "check":
            L.check_all()
            return show(chat, msg, *servers_screen())
        if p[1] == "chk":
            L.check(p[2])
            return show(chat, msg, *server_screen(p[2]))
        if p[1] in L.S:
            return show(chat, msg, *server_screen(p[1]))
    if k == "sm" and p[1] in L.S and p[2] in ("auto", "on", "off"):
        do_mode(p[1], p[2])
        t, r = server_screen(p[1])
        return show(chat, msg, f"✅ <b>{p[1].upper()}</b> mode → <b>{mode_label(p[2])}</b>\n\n" + t, r)
    if k == "sx" and p[1] in L.S:
        okk = do_maint(p[1], p[2] == "1")
        t, r = server_screen(p[1])
        return show(chat, msg, (f"✅ Maintenance {'ON' if p[2] == '1' else 'OFF'} set\n\n" if okk else "❌ Server tak pahunch nahi paye (band ya sleep?)\n\n") + t, r)
    if k == "d":
        if p[1] == "list":
            PENDING.pop(chat, None)
            return show(chat, msg, *domains_screen())
        if p[1] == "add":
            PENDING[chat] = {"await": "add_domain"}
            return show(chat, msg, "➕ <b>Domain add karo</b>\n\nApni user-panel ka domain bhejo, jaise:\n<code>app.example.com</code>\n\n(https:// aur path mat likho)  ·  /cancel", [[B("✖️ Cancel", "d:list")]])
        d = L.domain_by_id(p[1])
        return show(chat, msg, *(domain_screen(d) if d else domains_screen()))
    if k in ("dk", "dt", "dd", "ddy"):
        d = L.domain_by_id(p[1])
        if not d:
            return show(chat, msg, *domains_screen())
        if k == "dk":
            key = L.rotate_key(d["host"])
            return show(chat, msg, key_message(d["host"], key, new=False), [[B("Open domain", f"d:{d['id']}")]])
        if k == "dt":
            d["active"] = not d.get("active", True)
            L._save()
            return show(chat, msg, *domain_screen(d))
        if k == "dd":
            return show(chat, msg, f"🗑 <b>{esc(d['host'])}</b> hata dein?\n\nIske baad is domain ki saari requests reject hongi.", [[B("✅ Yes, remove", f"ddy:{d['id']}"), B("✖️ No", f"d:{d['id']}")]])
        L.DOMAINS.pop(d["host"], None)
        L._save()
        return show(chat, msg, f"✅ <code>{esc(d['host'])}</code> remove ho gaya.\n\n" + domains_screen()[0], domains_screen()[1])
    if k == "b":
        if p[1] == "clear":
            L.BLOCKED.clear()
        return show(chat, msg, *blocked_screen(chat))
    if k == "ba":
        lst = VIEW.get(chat, {}).get("blocked", [])
        host = L.norm_host(lst[int(p[1])]) if p[1].isdigit() and int(p[1]) < len(lst) else ""
        if not host:
            return show(chat, msg, *blocked_screen(chat))
        rec, key = L.add_domain(host)
        return show(chat, msg, key_message(host, key), [[B("🚫 Blocked list", "b:list"), B("🌐 Domains", "d:list")]])
    if k == "f":
        return show(chat, msg, *fraud_screen())
    if k == "fc" and len(p) == 3:
        act = "unban" if p[2] == "u" else "confirm"
        st, j, sid = admin_call("s5", "POST", f"/fraud/cases/{p[1]}/resolve", {"action": act})
        t, r = fraud_screen()
        return show(chat, msg, (f"✅ Case #{p[1][:8]} → <b>{act}</b>\n\n" if st == 200 else f"❌ Failed: <code>{esc(j.get('error', st))}</code>\n\n") + t, r)
    if k == "bk":
        if p[1] == "menu":
            return show(chat, msg, *backup_screen())
        if p[1] == "run":
            show(chat, msg, "⏳ Backup chal raha hai… (kuch der lag sakti hai)", [])
            st, j, sid = admin_call("s4", "POST", "/backup/run")
            t, r = backup_screen()
            return show(chat, msg, (f"✅ Backup ready: <code>{esc(j.get('file'))}</code> ({int(j.get('bytes', 0)) // 1024} KB)\n\n" if st == 200 else f"❌ Backup failed: <code>{esc(j.get('error', st))}</code>\n\n") + t, r)
        if p[1] == "list":
            st, j, sid = admin_call("s4", "GET", "/backup/list")
            fl = j.get("files", []) if st == 200 else []
            txt = "📂 <b>Backups</b>\n\n" + ("\n".join(f"• <code>{esc(f['name'])}</code> — {f['bytes'] // 1024} KB" for f in fl) if fl else "Koi backup nahi mila.")
            return show(chat, msg, txt, [[B("⬅️ Back", "bk:menu")]])
    if k == "g":
        return on_geo(chat, msg, p)
    if k == "st":
        return show(chat, msg, *stats_screen())
    if k == "set":
        if p[1] == "menu":
            return show(chat, msg, *settings_screen())
        if p[1] == "bot":
            return show(chat, msg, *bot_status_screen())
        if p[1] == "dc":
            L.SETTINGS["domainCheck"] = p[2] == "1"
            L._save()
            return show(chat, msg, settings_screen()[0], settings_screen()[1])
    return show(chat, msg, main_text(), main_rows())


def on_message(chat, text):
    t = (text or "").strip()
    cmd = t.split()[0].lower().split("@")[0] if t.startswith("/") else ""
    arg = t[len(t.split()[0]):].strip() if cmd else ""
    if cmd in ("/start", "/menu", "/help"):
        PENDING.pop(chat, None)
        return show(chat, None, main_text(), main_rows())
    if cmd == "/cancel":
        PENDING.pop(chat, None)
        return show(chat, None, "✖️ Cancel ho gaya.", [[B("🏠 Menu", "m:main")]])
    if cmd == "/servers":
        return show(chat, None, *servers_screen())
    if cmd == "/domains":
        return show(chat, None, *domains_screen())
    if cmd == "/stats":
        return show(chat, None, *stats_screen())
    if cmd == "/fraud":
        return show(chat, None, *fraud_screen())
    if cmd == "/geo":
        return show(chat, None, *geo_screen())
    if cmd == "/geouser":
        return show(chat, None, *geo_user_screen(arg)) if arg else on_callback(chat, None, "g:u")
    if cmd == "/adddomain":
        return show(chat, None, *add_domain_text(chat, arg)) if arg else on_callback(chat, None, "d:add")
    if cmd == "/removedomain":
        h = L.norm_host(arg)
        if h in L.DOMAINS:
            L.DOMAINS.pop(h)
            L._save()
            return show(chat, None, f"✅ <code>{esc(h)}</code> remove ho gaya.", [[B("🌐 Domains", "d:list")]])
        return show(chat, None, "❌ Ye domain list me nahi hai.", [[B("🌐 Domains", "d:list")]])
    aw = PENDING.get(chat, {}).get("await")
    if aw == "geo_message":
        PENDING.pop(chat, None)
        ok_, er = geo_save({"message": t})
        return show(chat, None, *geo_screen("✅ Message saved\n\n" if ok_ else f"❌ {esc(er)}\n\n"))
    if aw == "geo_user":
        PENDING.pop(chat, None)
        return show(chat, None, *geo_user_screen(t))
    if PENDING.get(chat, {}).get("await") == "add_domain":
        return show(chat, None, *add_domain_text(chat, t))
    return show(chat, None, main_text(), main_rows())


def handle_update(u):
    try:
        if "callback_query" in u:
            q = u["callback_query"]
            uid = q["from"]["id"]
            chat, msg = q["message"]["chat"]["id"], q["message"]["message_id"]
            if uid not in ADMINS:
                return api("answerCallbackQuery", callback_query_id=q["id"], text="⛔ Access denied", show_alert=True)
            api("answerCallbackQuery", callback_query_id=q["id"])
            return on_callback(chat, msg, q.get("data", ""))
        m = u.get("message")
        if not m:
            return
        uid, chat = m["from"]["id"], m["chat"]["id"]
        if m["chat"].get("type") != "private":
            return
        if uid not in ADMINS:
            return api("sendMessage", chat_id=chat, parse_mode="HTML",
                       text=f"⛔ <b>Access denied</b>\n\nYe private control bot hai.\nTumhari Telegram ID: <code>{uid}</code>\n(Owner ko ye ID <code>TG_ADMIN_IDS</code> me add karni hogi.)")
        on_message(chat, m.get("text", ""))
    except Exception as e:
        print("[tg] update error", repr(e), flush=True)


# ------------------------------------------------------------------ duplicate protection
def seen_before(update_id):
    """True if this update_id was already processed (Telegram re-sends when our answer was slow, e.g. after a cold start)."""
    now = time.time()
    with SEEN_LOCK:
        if update_id in SEEN:
            return True
        SEEN[update_id] = now
        while len(SEEN) > SEEN_MAX or (SEEN and next(iter(SEEN.values())) < now - 6 * 3600):
            SEEN.popitem(last=False)
    return False


def process(u):
    """Run one update (once). Used by both modes."""
    uid = u.get("update_id")
    if isinstance(uid, int) and seen_before(uid):
        log(f"duplicate update {uid} ignored")
        return False
    handle_update(u)
    return True


# ------------------------------------------------------------------ webhook mode
def handle_webhook(path_secret, header_secret, update):
    """Called by the load balancer route.  -> (http_status, text).  Always answers fast; the work runs in a thread."""
    if MODE != "webhook":
        return 404, "not found"                                   # polling mode: webhook calls are refused
    if not SECRET or not hmac.compare_digest(str(path_secret), SECRET) or not hmac.compare_digest(str(header_secret or ""), SECRET):
        if time.time() - _warn_at["auth"] > 60:
            _warn_at["auth"] = time.time()
            log("webhook call rejected: wrong secret")
        return 403, "forbidden"
    if not isinstance(update, dict) or not isinstance(update.get("update_id"), int):
        return 400, "bad update"
    if seen_before(update["update_id"]):
        log(f"duplicate update {update['update_id']} ignored")
        return 200, "duplicate"

    def run():
        try:
            handle_update(update)
        except Exception as e:
            log("update handler crashed:", repr(e))
    threading.Thread(target=run, daemon=True, name="tg-update").start()
    return 200, "ok"


def webhook_url():
    return f"{PUBLIC_URL}/tg/webhook/{SECRET}"


def register_webhook(force=True):
    """Register (or re-register) the webhook. -> True if Telegram has our webhook afterwards."""
    if not PUBLIC_URL.startswith("https://"):
        log("ERROR: PUBLIC_URL must be your https URL (set PUBLIC_URL, Render also gives RENDER_EXTERNAL_URL) - webhook NOT registered")
        return False
    url = webhook_url()
    info = api("getWebhookInfo").get("result") or {}
    if not force and info.get("url") == url:
        if info.get("last_error_message"):
            log(f"webhook OK but Telegram reports: {info['last_error_message'][:140]}")
        return True
    r = api("setWebhook", url=url, secret_token=SECRET, allowed_updates=UPDATES, max_connections=10, drop_pending_updates=False)
    if r.get("ok"):
        log(f"webhook registered -> {PUBLIC_URL}/tg/webhook/***")
        return True
    log("ERROR: setWebhook failed:", r.get("description"))
    return False


def _run_webhook():
    time.sleep(3)
    if api("getMe").get("ok") is not True:
        log("ERROR: Telegram rejected TG_BOT_TOKEN (getMe failed) - check the token")
    api("setMyCommands", commands=COMMANDS)
    for n in range(1, 7):                                  # right after a deploy the network / DNS may need a moment
        if register_webhook(force=True):
            break
        time.sleep(10 * n)
    while True:                                            # self-heal: someone (or an old deploy) replaced the webhook
        time.sleep(1800)
        try:
            register_webhook(force=False)
        except Exception as e:
            log("webhook check failed:", repr(e))


# ------------------------------------------------------------------ polling mode
def _run_polling():
    time.sleep(3)
    if api("getMe").get("ok") is not True:
        log("ERROR: Telegram rejected TG_BOT_TOKEN (getMe failed) - check the token")
    r = api("deleteWebhook")                               # polling and a webhook cannot be active together
    log("webhook removed for polling:", r.get("ok"))
    api("setMyCommands", commands=COMMANDS)
    log("bot started (long polling)")
    off, started, errs = 0, time.time(), 0
    while True:
        r = api("getUpdates", _t=70, offset=off, timeout=50, allowed_updates=UPDATES)
        if not r.get("ok"):
            errs += 1
            if r.get("error_code") == 409:
                log("409 conflict: another copy of this bot is polling or a webhook is set. Run ONE instance per token.")
                time.sleep(15)
            else:
                time.sleep(min(60, 3 * errs))
            continue
        errs = 0
        for u in r.get("result", []):
            off = u["update_id"] + 1
            m = u.get("message") or {}
            if m.get("date") and m["date"] < started - 120:       # ignore messages that piled up while the service was off
                continue
            process(u)


COMMANDS = [{"command": "start", "description": "Open control panel"}, {"command": "servers", "description": "Server status"},
            {"command": "domains", "description": "Verified domains"}, {"command": "fraud", "description": "Fraud cases"},
            {"command": "stats", "description": "Stats"}, {"command": "geo", "description": "State payment block"},
            {"command": "adddomain", "description": "Add a domain"}]


def start(lb_module):
    global L
    L = lb_module
    if not TOKEN:
        log("TG_BOT_TOKEN not set - Telegram bot disabled")
        return
    if not ADMINS:
        log("WARNING: TG_ADMIN_IDS is empty - nobody can use the bot")
    log(f"mode = {MODE.upper()}")
    threading.Thread(target=_run_polling if MODE == "polling" else _run_webhook, daemon=True, name="tg-" + MODE).start()
