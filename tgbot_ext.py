"""Telegram bot - EXTRA CONTROLS (loaded by tgbot.py).

  🎛 Controls    whole-app maintenance, read-only (freeze money), per-feature kill-switches (deposit, withdraw, join, games, chat ...)
  💰 Wallet      deposit / withdraw / UPI / gift-card ON-OFF + notes, min deposit, min withdraw, fee
  🤝 Referral    Refer & Earn ON/OFF and its rules
  🎮 Games       Coin Battle / Bomb & Gems ON-OFF and bots ON-OFF
  👤 Players     find by Player ID / phone / e-mail / UID, ban (1 day ... permanent), unban
  🔒 Security    IP block list, rate limit, rate-limited IPs, domain check
  📜 Activity    every server up/down and every admin action (who pressed what)
  🖥 Servers+    all-servers Auto / ON, maintenance for all, server details + traffic, REAL on/off through the Render API (optional)

Callback data of this module always starts with "x:" (Telegram limit: 64 bytes).
"""
import ipaddress
import threading
import time

import requests

import tgbot as T

L = None                     # the load balancer module (T.L)
TL = threading.local()       # who pressed the button (Telegram id) - for the activity log

RENDER_KEY = ""
RENDER_IDS = {}


def init(lb):
    global L, RENDER_KEY, RENDER_IDS
    L = lb
    RENDER_KEY = L.env("RENDER_API_KEY")
    RENDER_IDS = {i: L.env(i.upper() + "_RENDER_ID") for i in L.IDS}


B, esc, show, ago = T.B, T.esc, T.show, T.ago
PENDING = T.PENDING


def who():
    return getattr(TL, "uid", "")


def log(kind, text):
    L.log_event(kind, text, who())


def onoff(v):
    return "✅ ON" if v else "⛔ OFF"


def num(text):
    try:
        v = float(str(text).replace(",", ".").strip())
        return v if v == v and abs(v) < 1e9 else None
    except Exception:
        return None


def back_main():
    return [B("🏠 Menu", "m:main")]


# ------------------------------------------------------------------ 🎛 CONTROLS
def controls_screen(note=""):
    S = L.SETTINGS
    app = "🔴 <b>MAINTENANCE</b> (players see a message)" if S["appMaintenance"] else "🟢 <b>LIVE</b>"
    ro = "🧊 <b>ON</b> (no deposit / withdraw / join / game — only reading)" if S["readOnly"] else "off"
    lines = [note + "🎛 <b>Controls</b>\n", f"📱 App: {app}", f"🧊 Read-only mode: {ro}", ""]
    off = [k for k, v in S["features"].items() if v is False]
    lines.append("<b>Feature switches</b> <i>(tap to switch, works instantly for every player)</i>")
    rows, row = [], []
    for key, (label, _, _) in L.FEATURES.items():
        isoff = S["features"].get(key) is False
        row.append(B(("⛔ " if isoff else "✅ ") + label.split(" ", 1)[1][:22], f"x:ft:{key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    lines.append(f"Switched OFF now: <b>{len(off)}</b>" + (" — " + ", ".join(L.FEATURES[k][0].split(' ', 1)[1] for k in off) if off else ""))
    rows.append([B("🟢 Maintenance OFF" if S["appMaintenance"] else "🚧 Maintenance ON", "x:am:0" if S["appMaintenance"] else "x:am:1"),
                 B("🧊 Read-only OFF" if S["readOnly"] else "🧊 Read-only ON", "x:ro:0" if S["readOnly"] else "x:ro:1")])
    rows.append([B("✏️ Maintenance message", "x:amsg"), B("✏️ Feature messages", "x:fm")])
    if off:
        rows.append([B("♻️ Turn ALL features ON", "x:fall")])
    rows.append([B("💰 Wallet", "x:wl"), B("🤝 Referral", "x:rf"), B("🎮 Games", "x:gp")])
    rows.append([B("⬅️ Back", "m:main")])
    if S["appMaintenance"] and S["maintMsg"]:
        lines.append(f"\n💬 Maintenance message: <i>{esc(S['maintMsg'])}</i>")
    return "\n".join(lines), rows


def feature_msgs_screen():
    rows, lines = [], ["✏️ <b>Feature messages</b>\n\nJab koi feature OFF ho to players ko ye message dikhta hai. Feature chuno, phir naya message bhejo (<code>-</code> bhejo = default).\n"]
    for key, (label, _, dflt) in L.FEATURES.items():
        cur = L.SETTINGS["featureMsg"].get(key)
        lines.append(f"• {label}: <i>{esc(cur) if cur else 'default'}</i>")
        rows.append([B(label, f"x:fmp:{key}")])
    rows.append([B("⬅️ Back", "x:ctl")])
    return "\n".join(lines), rows


# ------------------------------------------------------------------ 💰 WALLET
def wallet_screen(note=""):
    st, j, sid = T.admin_call("s1", "GET", "/wallet/admin/switches")
    if st != 200:
        return f"{note}💰 <b>Wallet</b>\n\n❌ Server-1 se data nahi mila.\n<code>{esc(j.get('error', st))}</code>", [[B("🔄 Retry", "x:wl"), B("⬅️ Back", "x:ctl")]]
    c = j.get("config", {})
    note_of = lambda k: (f"\n   📝 <i>{esc(c.get(k))}</i>" if c.get(k) else "")
    txt = (f"{note}💰 <b>Wallet switches</b> <i>(admin panel ke Settings jaisa, Firestore config/banner)</i>\n\n"
           f"💳 Deposit: <b>{onoff(c.get('depositEnabled'))}</b>{note_of('depositNote')}\n"
           f"💸 Withdraw (sab): <b>{onoff(c.get('withdrawEnabled'))}</b>{note_of('withdrawNote')}\n"
           f"   • UPI: <b>{onoff(c.get('wdUpiEnabled'))}</b>{note_of('wdUpiNote')}\n"
           f"   • Google Play gift card: <b>{onoff(c.get('wdGpEnabled'))}</b>{note_of('wdGpNote')}\n\n"
           f"⬇️ Min deposit: <b>₹{c.get('minDeposit') if c.get('minDeposit') is not None else '5 (default)'}</b>\n"
           f"⬆️ Min withdraw: <b>₹{c.get('minWithdraw') if c.get('minWithdraw') is not None else '10 (default)'}</b>\n"
           f"🧾 UPI withdraw fee: <b>₹{c.get('withdrawFee') if c.get('withdrawFee') is not None else '2 (default)'}</b>\n\n"
           "Change 10 second ke andar app me dikhta hai.")
    sw = lambda label, k: B(("⛔ " if c.get(k) else "✅ ") + ("Turn OFF " if c.get(k) else "Turn ON ") + label, f"x:ws:{k}:{0 if c.get(k) else 1}")
    rows = [[sw("Deposit", "depositEnabled"), sw("Withdraw", "withdrawEnabled")],
            [sw("UPI", "wdUpiEnabled"), sw("Gift card", "wdGpEnabled")],
            [B("✏️ Min deposit", "x:wn:minDeposit"), B("✏️ Min withdraw", "x:wn:minWithdraw"), B("✏️ Fee", "x:wn:withdrawFee")],
            [B("📝 Deposit note", "x:wt:depositNote"), B("📝 Withdraw note", "x:wt:withdrawNote")],
            [B("📝 UPI note", "x:wt:wdUpiNote"), B("📝 Gift card note", "x:wt:wdGpNote")],
            [B("🔄 Refresh", "x:wl"), B("⬅️ Back", "x:ctl")]]
    return txt, rows


def wallet_set(patch):
    st, j, sid = T.admin_call("s1", "POST", "/wallet/admin/switches", patch)
    if st == 200:
        log("wallet", "wallet settings: " + ", ".join(f"{k}={v}" for k, v in patch.items())[:200])
        return True, None
    return False, j.get("error") or f"HTTP {st}"


# ------------------------------------------------------------------ 🤝 REFERRAL
def referral_screen(note=""):
    st, j, sid = T.admin_call("s1", "GET", "/ref/admin/config")
    if st != 200:
        return f"{note}🤝 <b>Refer & Earn</b>\n\n❌ Server-1 se data nahi mila.\n<code>{esc(j.get('error', st))}</code>", [[B("🔄 Retry", "x:rf"), B("⬅️ Back", "x:ctl")]]
    c = j.get("config", {})
    amt = (f"fixed ₹{c.get('fixedAmount')}" if c.get("amountMode") == "fixed" else f"random ₹{c.get('minAmount')}–₹{c.get('maxAmount')}")
    txt = (f"{note}🤝 <b>Refer & Earn</b>\n\nStatus: <b>{onoff(c.get('enabled'))}</b>\n"
           f"🎟 Referrals per scratch card: <b>{c.get('referralsPerCard')}</b>\n💳 Friend must deposit: <b>₹{c.get('depositRequired')}</b> <i>(0 = no deposit needed)</i>\n"
           f"🎁 Prize: <b>{amt}</b> → <b>{c.get('creditTo')}</b> balance\n⏳ Add-code window: <b>{c.get('claimWindowHrs')} h</b>\n"
           f"👥 Max referrals per user: <b>{c.get('maxReferralsPerUser') or 'no limit'}</b> · Max cards per user: <b>{c.get('maxCardsPerUser') or 'no limit'}</b>")
    rows = [[B("⛔ Turn OFF" if c.get("enabled") else "✅ Turn ON", f"x:rs:enabled:{0 if c.get('enabled') else 1}"),
             B("🔀 Prize: fixed" if c.get("amountMode") != "fixed" else "🔀 Prize: random", f"x:rs:amountMode:{'fixed' if c.get('amountMode') != 'fixed' else 'random'}")],
            [B("💳 Credit → withdraw" if c.get("creditTo") != "withdraw" else "💳 Credit → deposit", f"x:rs:creditTo:{'withdraw' if c.get('creditTo') != 'withdraw' else 'deposit'}")],
            [B("✏️ Deposit needed", "x:rn:depositRequired"), B("✏️ Per card", "x:rn:referralsPerCard")],
            [B("✏️ Min prize", "x:rn:minAmount"), B("✏️ Max prize", "x:rn:maxAmount"), B("✏️ Fixed", "x:rn:fixedAmount")],
            [B("✏️ Window (h)", "x:rn:claimWindowHrs"), B("✏️ Max/user", "x:rn:maxReferralsPerUser")],
            [B("🔄 Refresh", "x:rf"), B("⬅️ Back", "x:ctl")]]
    return txt, rows


def referral_set(patch):
    st, j, sid = T.admin_call("s1", "POST", "/ref/admin/config", patch)
    if st == 200:
        log("referral", "referral settings: " + ", ".join(f"{k}={v}" for k, v in patch.items())[:200])
        return True, None
    return False, j.get("error") or f"HTTP {st}"


# ------------------------------------------------------------------ 🎮 GAMES
GAMES = (("ball", "🪙 Coin Battle"), ("bomb", "💣 Bomb & Gems"))


def games_screen(note=""):
    lines, rows = [note + "🎮 <b>Games</b>\n"], []
    for gid, label in GAMES:
        st, j, sid = T.admin_call("s3", "GET", f"/game/{gid}/admin/overview")
        if st != 200:
            lines.append(f"{label}: ❌ <code>{esc(j.get('error', st))}</code>")
            continue
        c, live = j.get("cfg", {}), j.get("live", 0)
        en, bots = c.get("enabled", True), c.get("botsEnabled", True)
        lines.append(f"{label}: <b>{onoff(en)}</b> · bots: <b>{onoff(bots)}</b> · live games: <b>{live}</b>")
        rows.append([B(("⛔ Turn OFF " if en else "✅ Turn ON ") + label.split(' ', 1)[1], f"x:gt:{gid}:{0 if en else 1}"),
                     B(("🤖 Bots OFF" if bots else "🤖 Bots ON") + " · " + label.split(' ', 1)[1][:6], f"x:gb:{gid}:{0 if bots else 1}")])
    lines.append("\nOFF karne se game card app me chhup jata hai. Chal rahe games khatam ho jate hain.\nTurant sab band karna ho to 🎛 Controls me <b>Games</b> switch use karo.")
    rows.append([B("🔄 Refresh", "x:gp"), B("⬅️ Back", "x:ctl")])
    return "\n".join(lines), rows


def game_set(gid, patch):
    st, j, sid = T.admin_call("s3", "POST", f"/game/{gid}/admin/config", patch)
    if st == 200:
        log("game", f"{gid}: " + ", ".join(f"{k}={v}" for k, v in patch.items()))
        return True, None
    return False, j.get("error") or f"HTTP {st}"


# ------------------------------------------------------------------ 👤 PLAYERS
def players_screen(note=""):
    st, j, sid = T.admin_call("s1", "GET", "/user/admin/summary")
    tot = ""
    if st == 200:
        tot = f"👥 Players: <b>{j.get('users')}</b> · 🚫 Banned: <b>{j.get('banned')}</b> · 🤝 Referrals: <b>{j.get('referrals')}</b>\n\n"
    txt = (f"{note}👤 <b>Players</b>\n\n{tot}Player ID, phone, e-mail ya UID bhejkar player dhundo, phir ban / unban karo.\n"
           "Commands: <code>/find 1234567</code> · <code>/ban 1234567 7</code> (7 din; 0 = permanent) · <code>/unban 1234567</code>")
    return txt, [[B("🔎 Find player", "x:pf")], [B("⬅️ Back", "m:main")]]


def player_card(u):
    ban = ""
    if u.get("banned"):
        ban = f"\n🚫 <b>BANNED</b> until <code>{esc(u.get('bannedUntil') or 'permanent')}</code> — {esc(u.get('banReason') or '-')}"
    return (f"👤 <b>{esc(u.get('name') or 'No name')}</b>{' ✔️' if u.get('verified') else ''}\n"
            f"🆔 Player ID: <code>{esc(u.get('playerId'))}</code>\n🔑 UID: <code>{esc(u.get('uid'))}</code>\n"
            f"📞 {esc(u.get('phone') or '-')} · ✉️ {esc(u.get('email') or '-')}\n🎮 FF: {esc(u.get('gameName') or '-')} / <code>{esc(u.get('gameUid') or '-')}</code>\n"
            f"💳 Deposit bal: <b>₹{u.get('depositBalance')}</b> · 💸 Withdraw bal: <b>₹{u.get('withdrawBalance')}</b>\n"
            f"📈 Total deposited: ₹{u.get('totalDeposited')} · Earned: ₹{u.get('totalEarned')} · Matches: {u.get('matchesPlayed')}\n"
            f"🗺 State: {esc(u.get('state') or '-')}{ban}")


def player_find(q):
    st, j, sid = T.admin_call("s1", "GET", "/user/admin/find?q=" + requests.utils.quote(q))
    if st != 200:
        return f"❌ <code>{esc(j.get('error', st))}</code>", [[B("⬅️ Back", "x:pl")]]
    users = j.get("users", [])
    if not users:
        return f"🔎 <b>{esc(q)}</b>\n\n❌ Koi player nahi mila.", [[B("🔎 Dobara", "x:pf"), B("⬅️ Back", "x:pl")]]
    rows = []
    out = []
    for u in users[:3]:
        out.append(player_card(u))
        rows.append([B("✅ Unban " + (u.get("playerId") or "")[:8], f"x:ub:{u['uid']}") if u.get("banned") else B("🚫 Ban " + (u.get("playerId") or "")[:8], f"x:bn:{u['uid']}"),
                     B("🔄", f"x:pu:{u['uid']}")])
    rows.append([B("🔎 Find another", "x:pf"), B("⬅️ Back", "x:pl")])
    return "\n\n".join(out), rows


def player_ban(uid, days):
    st, j, sid = T.admin_call("s1", "POST", "/user/admin/ban", {"uid": uid, "days": days, "reason": "Banned by admin"})
    ok = st == 200
    log("ban", f"BAN {uid} {'permanent' if days <= 0 else str(days) + ' days'}: {'ok' if ok else (j.get('error') or st)}")
    return ok, (j.get("error") if not ok else None)


def player_unban(uid):
    st, j, sid = T.admin_call("s1", "POST", "/user/admin/unban", {"uid": uid})
    ok = st == 200
    log("unban", f"UNBAN {uid}: {'ok' if ok else (j.get('error') or st)}")
    return ok, (j.get("error") if not ok else None)


# ------------------------------------------------------------------ 🔒 SECURITY
def security_screen(note=""):
    S = L.SETTINGS
    noisy = sorted(L.NOISY.items(), key=lambda kv: -kv[1])[:5]
    lines = [note + "🔒 <b>Security</b>\n",
             f"🔐 Domain check: <b>{onoff(S['domainCheck'])}</b>", f"⏱ Rate limit: <b>{S['rateLimit']}</b> requests / minute / IP",
             f"⛔ Blocked IPs: <b>{len(S['ipBlock'])}</b> · requests refused: <b>{L.STATS.get('blockedIp', 0)}</b>",
             f"🚫 Rejected domains (recent): <b>{len(L.BLOCKED)}</b>"]
    if noisy:
        lines.append("\n🔥 <b>Most rate-limited IPs</b> <i>(since restart)</i>")
        for n, (ip, c) in enumerate(noisy, 1):
            lines.append(f"{n}. <code>{esc(ip)}</code> — {c}×" + (" ⛔ blocked" if ip in S["ipBlock"] else ""))
    rows = [[B("60", "x:rl:60"), B("120", "x:rl:120"), B("300", "x:rl:300"), B("600", "x:rl:600"), B("✏️ Custom", "x:rlc")]]
    for n, (ip, c) in enumerate(noisy):
        if ip not in S["ipBlock"]:
            rows.append([B(f"⛔ Block {ip}", f"x:ipb:{n}")])
    rows += [[B("📋 Blocked IP list", "x:ip"), B("➕ Block an IP", "x:ipa")],
             [B("🔴 Domain check OFF" if S["domainCheck"] else "🟢 Domain check ON", "set:dc:0" if S["domainCheck"] else "set:dc:1"), B("🚫 Rejected domains", "b:list")],
             [B("⬅️ Back", "m:main")]]
    return "\n".join(lines), rows


def ip_list_screen(note=""):
    ips = L.SETTINGS["ipBlock"]
    lines = [note + "⛔ <b>Blocked IPs</b>\n"] + ([f"{n}. <code>{esc(ip)}</code>" for n, ip in enumerate(ips, 1)] if ips else ["Koi IP block nahi hai."])
    rows = [[B(f"♻️ Unblock {ip}", f"x:ipd:{n}")] for n, ip in enumerate(ips[:15])]
    rows.append([B("➕ Block an IP", "x:ipa"), B("⬅️ Back", "x:sc")])
    return "\n".join(lines), rows


def block_ip(ip):
    try:
        ip = str(ipaddress.ip_address(ip.strip()))
    except Exception:
        return False, "Ye valid IP nahi hai"
    if ip in L.SETTINGS["ipBlock"]:
        return False, "Ye IP pehle se block hai"
    if len(L.SETTINGS["ipBlock"]) >= 500:
        return False, "Max 500 IPs"
    L.SETTINGS["ipBlock"].append(ip)
    L._save()
    log("ip", f"BLOCK IP {ip}")
    return True, None


def unblock_ip(ip):
    if ip in L.SETTINGS["ipBlock"]:
        L.SETTINGS["ipBlock"].remove(ip)
        L._save()
        log("ip", f"UNBLOCK IP {ip}")
        return True
    return False


# ------------------------------------------------------------------ 📜 ACTIVITY LOG
def log_screen(note=""):
    icon = {"down": "🔴", "up": "🟢", "ban": "🚫", "unban": "✅", "ip": "⛔", "wallet": "💰", "referral": "🤝", "game": "🎮", "control": "🎛", "server": "🖥", "render": "⏻",
            "security": "🔒", "settings": "⚙️"}
    items = list(L.EVENTS)[:15]
    lines = [note + "📜 <b>Activity log</b> <i>(latest first)</i>\n"]
    for e in items:
        lines.append(f"{icon.get(e['kind'], '•')} <i>{ago(e['t'])}</i> — {esc(e['text'])}" + (f" <code>by {esc(e['who'])}</code>" if e.get("who") else ""))
    if not items:
        lines.append("Abhi kuch record nahi hai.")
    return "\n".join(lines), [[B("🔄 Refresh", "x:lg"), B("🧹 Clear", "x:lgc")], [B("⬅️ Back", "m:main")]]


# ------------------------------------------------------------------ 🖥 SERVERS+
def render_on(i):
    return bool(RENDER_KEY and RENDER_IDS.get(i))


def render_call(i, action):
    """Real power control through the Render API: suspend (= OFF, no free hours used) / resume / restart / deploy."""
    path = {"suspend": "suspend", "resume": "resume", "restart": "restart", "deploy": "deploys"}[action]
    try:
        r = requests.post(f"https://api.render.com/v1/services/{RENDER_IDS[i]}/{path}", headers={"Authorization": f"Bearer {RENDER_KEY}", "Accept": "application/json"},
                          json={} if action == "deploy" else None, timeout=25)
        ok = r.status_code in (200, 201, 202, 204)
        log("render", f"{action.upper()} {i.upper()}: {'ok' if ok else 'HTTP ' + str(r.status_code)}")
        return ok, ("" if ok else f"HTTP {r.status_code} {r.text[:120]}")
    except Exception as e:
        return False, str(e)[:120]


def server_extra_rows(i):
    rows = [[B("ℹ️ Details & traffic", f"x:sd:{i}")]]
    if render_on(i):
        rows.append([B("⏻ Suspend (real OFF)", f"x:rd:{i}:suspend"), B("▶️ Resume", f"x:rd:{i}:resume")])
        rows.append([B("♻️ Restart", f"x:rd:{i}:restart"), B("🚀 Deploy latest", f"x:rd:{i}:deploy")])
    return rows


def server_detail(i):
    s = L.S[i]
    c = L.SRV[i]
    avg = int(c["ms"] / c["req"]) if c["req"] else 0
    info = s.get("info") or {}
    lines = [f"ℹ️ <b>{i.upper()} — details</b>\n",
             f"📡 Health: <b>{'UP' if s['up'] else 'DOWN' if s['up'] is False else 'not checked'}</b> · fails in a row: {s['fails']}",
             f"⏱ Latency (health): {s['latency'] if s['latency'] is not None else '-'} ms · last check {ago(s['last'])}",
             f"📈 Traffic since restart: <b>{c['req']}</b> requests · <b>{c['err']}</b> errors · avg <b>{avg} ms</b>"]
    for k in list(info)[:14]:
        v = info[k]
        if isinstance(v, (dict, list)):
            v = str(v)[:90]
        lines.append(f"• {esc(k)}: <code>{esc(str(v)[:90])}</code>")
    if s["error"]:
        lines.append(f"\n⚠️ Last error: <code>{esc(s['error'][:150])}</code>")
    lines.append("\n" + ("⏻ Render control: ✅ configured" if render_on(i) else f"⏻ Render control: not set (env <code>RENDER_API_KEY</code> + <code>{i.upper()}_RENDER_ID</code>)"))
    return "\n".join(lines), [[B("🔄 Refresh", f"x:sd:{i}"), B("⬅️ Back", f"s:{i}")]]


def bulk_mode(mode):
    for i in L.IDS:
        L.S[i]["mode"] = mode
    L._save()
    log("server", f"ALL servers mode → {mode.upper()}")
    threading.Thread(target=L.check_all, daemon=True).start()


def bulk_maint(on):
    done = []
    for i in L.IDS:
        if L.URLS[i] and T.do_maint(i, on):
            done.append(i.upper())
    log("server", f"maintenance {'ON' if on else 'OFF'} for {', '.join(done) or 'none'}")
    return done


# ------------------------------------------------------------------ callbacks
def on_callback(chat, msg, p):
    """p = data.split(':') with p[0] == 'x'.  -> True if handled."""
    a = p[1] if len(p) > 1 else ""
    S = L.SETTINGS
    # ----- controls hub
    if a == "ctl":
        PENDING.pop(chat, None)
        return show(chat, msg, *controls_screen())
    if a == "ft" and len(p) == 3 and p[2] in L.FEATURES:
        k = p[2]
        isoff = S["features"].get(k) is False
        S["features"][k] = True if isoff else False
        if S["features"][k] is True:
            S["features"].pop(k, None)
        L._save()
        log("control", f"feature {L.FEATURES[k][0]} → {'ON' if isoff else 'OFF'}")
        return show(chat, msg, *controls_screen(f"{'✅' if isoff else '⛔'} <b>{esc(L.FEATURES[k][0])}</b> → <b>{'ON' if isoff else 'OFF'}</b>\n\n"))
    if a == "fall":
        S["features"].clear()
        L._save()
        log("control", "ALL features → ON")
        return show(chat, msg, *controls_screen("♻️ Sab features ON ho gaye.\n\n"))
    if a == "am" and len(p) == 3:
        if p[2] == "1":
            return show(chat, msg, "🚧 <b>Maintenance ON karein?</b>\n\nPoora app band ho jayega: har player ko maintenance message dikhega (login, wallet, matches — sab). Sirf admin / bot chalega.",
                        [[B("✅ Yes, turn ON", "x:amy"), B("✖️ No", "x:ctl")]])
        S["appMaintenance"] = False
        L._save()
        log("control", "APP maintenance OFF")
        L.notify("🟢 <b>Maintenance OFF</b> — app is live again.")
        return show(chat, msg, *controls_screen("🟢 App LIVE ho gaya.\n\n"))
    if a == "amy":
        S["appMaintenance"] = True
        L._save()
        log("control", "APP maintenance ON")
        L.notify("🚧 <b>Maintenance ON</b> — app is closed for players.")
        return show(chat, msg, *controls_screen("🚧 Maintenance ON.\n\n"))
    if a == "ro" and len(p) == 3:
        S["readOnly"] = p[2] == "1"
        L._save()
        log("control", f"READ-ONLY {'ON' if S['readOnly'] else 'OFF'}")
        return show(chat, msg, *controls_screen(f"🧊 Read-only {'ON' if S['readOnly'] else 'OFF'}.\n\n"))
    if a == "amsg":
        PENDING[chat] = {"await": "x_maint_msg"}
        return show(chat, msg, "✏️ <b>Maintenance message</b>\n\nPlayers ko jo message dikhana hai wo bhejo (max 300 chars).\n<code>-</code> bhejo = default message.\n\n/cancel", [[B("✖️ Cancel", "x:ctl")]])
    if a == "fm":
        return show(chat, msg, *feature_msgs_screen())
    if a == "fmp" and len(p) == 3 and p[2] in L.FEATURES:
        PENDING[chat] = {"await": "x_feat_msg", "key": p[2]}
        return show(chat, msg, f"✏️ <b>{esc(L.FEATURES[p[2]][0])}</b>\n\nJab ye OFF ho to players ko ye message dikhega. Naya message bhejo.\n<code>-</code> = default.\n\n/cancel", [[B("✖️ Cancel", "x:fm")]])
    # ----- wallet
    if a == "wl":
        PENDING.pop(chat, None)
        return show(chat, msg, *wallet_screen())
    if a == "ws" and len(p) == 4:
        ok_, er = wallet_set({p[2]: p[3] == "1"})
        return show(chat, msg, *wallet_screen("✅ Saved\n\n" if ok_ else f"❌ {esc(er)}\n\n"))
    if a == "wn" and len(p) == 3:
        PENDING[chat] = {"await": "x_wl_num", "key": p[2]}
        names = {"minDeposit": "Min deposit (₹)", "minWithdraw": "Min withdraw (₹)", "withdrawFee": "UPI withdraw fee (₹)"}
        return show(chat, msg, f"✏️ <b>{names.get(p[2], p[2])}</b>\n\nNayi value bhejo (sirf number). <code>-</code> = default par wapas.\n\n/cancel", [[B("✖️ Cancel", "x:wl")]])
    if a == "wt" and len(p) == 3:
        PENDING[chat] = {"await": "x_wl_txt", "key": p[2]}
        return show(chat, msg, f"📝 <b>Note</b> <code>{esc(p[2])}</code>\n\nJab ye OFF ho to players ko ye message dikhega. Message bhejo. <code>-</code> = khali / default.\n\n/cancel", [[B("✖️ Cancel", "x:wl")]])
    # ----- referral
    if a == "rf":
        PENDING.pop(chat, None)
        return show(chat, msg, *referral_screen())
    if a == "rs" and len(p) == 4:
        v = p[3] == "1" if p[2] == "enabled" else p[3]
        ok_, er = referral_set({p[2]: v})
        return show(chat, msg, *referral_screen("✅ Saved\n\n" if ok_ else f"❌ {esc(er)}\n\n"))
    if a == "rn" and len(p) == 3:
        PENDING[chat] = {"await": "x_rf_num", "key": p[2]}
        return show(chat, msg, f"✏️ <b>{esc(p[2])}</b>\n\nNayi value bhejo (number).\n\n/cancel", [[B("✖️ Cancel", "x:rf")]])
    # ----- games
    if a == "gp":
        return show(chat, msg, *games_screen())
    if a in ("gt", "gb") and len(p) == 4 and p[2] in ("ball", "bomb"):
        ok_, er = game_set(p[2], {("enabled" if a == "gt" else "botsEnabled"): p[3] == "1"})
        return show(chat, msg, *games_screen("✅ Saved\n\n" if ok_ else f"❌ {esc(er)}\n\n"))
    # ----- players
    if a == "pl":
        PENDING.pop(chat, None)
        return show(chat, msg, *players_screen())
    if a == "pf":
        PENDING[chat] = {"await": "x_player"}
        return show(chat, msg, "🔎 <b>Find player</b>\n\nPlayer ID, phone, e-mail, FF UID ya app UID bhejo.\n\n/cancel", [[B("✖️ Cancel", "x:pl")]])
    if a == "pu" and len(p) == 3:
        return show(chat, msg, *player_find(p[2]))
    if a == "bn" and len(p) == 3:
        u = p[2]
        return show(chat, msg, "🚫 <b>Kitne din ke liye ban?</b>",
                    [[B("1 day", f"x:bd:{u}:1"), B("3 days", f"x:bd:{u}:3"), B("7 days", f"x:bd:{u}:7")], [B("30 days", f"x:bd:{u}:30"), B("♾ Permanent", f"x:bd:{u}:0")],
                     [B("✖️ Cancel", f"x:pu:{u}")]])
    if a == "bd" and len(p) == 4 and p[3].isdigit():
        ok_, er = player_ban(p[2], int(p[3]))
        t, r = player_find(p[2])
        return show(chat, msg, ("✅ Ban ho gaya.\n\n" if ok_ else f"❌ {esc(er)}\n\n") + t, r)
    if a == "ub" and len(p) == 3:
        ok_, er = player_unban(p[2])
        t, r = player_find(p[2])
        return show(chat, msg, ("✅ Unban ho gaya.\n\n" if ok_ else f"❌ {esc(er)}\n\n") + t, r)
    # ----- security
    if a == "sc":
        PENDING.pop(chat, None)
        return show(chat, msg, *security_screen())
    if a == "rl" and len(p) == 3 and p[2].isdigit():
        S["rateLimit"] = max(20, min(100000, int(p[2])))
        L._save()
        log("security", f"rate limit → {S['rateLimit']}/min")
        return show(chat, msg, *security_screen(f"✅ Rate limit {S['rateLimit']}/min\n\n"))
    if a == "rlc":
        PENDING[chat] = {"await": "x_rate"}
        return show(chat, msg, "⏱ <b>Rate limit</b>\n\nPer IP per minute kitni requests? (20 – 100000)\n\n/cancel", [[B("✖️ Cancel", "x:sc")]])
    if a == "ip":
        return show(chat, msg, *ip_list_screen())
    if a == "ipa":
        PENDING[chat] = {"await": "x_ip"}
        return show(chat, msg, "⛔ <b>Block an IP</b>\n\nIP address bhejo, jaise <code>203.0.113.7</code>\n\n/cancel", [[B("✖️ Cancel", "x:sc")]])
    if a == "ipb" and len(p) == 3 and p[2].isdigit():
        noisy = sorted(L.NOISY.items(), key=lambda kv: -kv[1])[:5]
        if int(p[2]) < len(noisy):
            ok_, er = block_ip(noisy[int(p[2])][0])
            return show(chat, msg, *security_screen("⛔ IP blocked.\n\n" if ok_ else f"❌ {esc(er)}\n\n"))
        return show(chat, msg, *security_screen())
    if a == "ipd" and len(p) == 3 and p[2].isdigit():
        ips = S["ipBlock"]
        if int(p[2]) < len(ips):
            unblock_ip(ips[int(p[2])])
        return show(chat, msg, *ip_list_screen("♻️ Unblocked.\n\n"))
    # ----- log + alerts
    if a == "lg":
        return show(chat, msg, *log_screen())
    if a == "lgc":
        L.EVENTS.clear()
        return show(chat, msg, *log_screen("🧹 Cleared.\n\n"))
    if a == "al" and len(p) == 3:
        S["alerts"] = p[2] == "1"
        L._save()
        log("settings", f"alerts {'ON' if S['alerts'] else 'OFF'}")
        return show(chat, msg, *T.settings_screen())
    # ----- servers+
    if a == "sa" and len(p) == 3 and p[2] in ("auto", "on"):
        bulk_mode(p[2])
        t, r = T.servers_screen()
        return show(chat, msg, f"✅ Sab servers → <b>{T.mode_label(p[2])}</b>\n\n" + t, r)
    if a == "mall" and len(p) == 3:
        if p[2] == "1":
            return show(chat, msg, "🚧 <b>Sab servers par maintenance ON?</b>\n\nHar server 503 dega aur traffic fail hoga (Server-4 bhi). Sirf tab karo jab database / deploy ka kaam ho.",
                        [[B("✅ Yes, all ON", "x:mally"), B("✖️ No", "s:list")]])
        done = bulk_maint(False)
        t, r = T.servers_screen()
        return show(chat, msg, f"✅ Maintenance OFF: {', '.join(done) or '—'}\n\n" + t, r)
    if a == "mally":
        done = bulk_maint(True)
        t, r = T.servers_screen()
        return show(chat, msg, f"🚧 Maintenance ON: {', '.join(done) or '—'}\n\n" + t, r)
    if a == "sd" and len(p) == 3 and p[2] in L.S:
        return show(chat, msg, *server_detail(p[2]))
    if a == "rd" and len(p) == 4 and p[2] in L.S and p[3] in ("suspend", "resume", "restart", "deploy") and render_on(p[2]):
        warn = {"suspend": "Server band ho jayega (Render par suspend). Resume karne tak koi request nahi chalegi.", "resume": "Suspended server dobara chalu hoga.",
                "restart": "Server 30–60 second ke liye restart hoga.", "deploy": "Latest code deploy hoga (kuch minute)."}[p[3]]
        return show(chat, msg, f"⏻ <b>{p[2].upper()} — {p[3].upper()}?</b>\n\n{warn}", [[B("✅ Yes", f"x:rdy:{p[2]}:{p[3]}"), B("✖️ No", f"s:{p[2]}")]])
    if a == "rdy" and len(p) == 4 and p[2] in L.S and p[3] in ("suspend", "resume", "restart", "deploy") and render_on(p[2]):
        ok_, er = render_call(p[2], p[3])
        t, r = T.server_screen(p[2])
        return show(chat, msg, (f"✅ Render: <b>{p[3]}</b> sent for {p[2].upper()}\n\n" if ok_ else f"❌ Render API: <code>{esc(er)}</code>\n\n") + t, r)
    return False


# ------------------------------------------------------------------ typed text
def on_text(chat, t, cmd, arg):
    """-> True if the message was handled here."""
    S = L.SETTINGS
    aw = PENDING.get(chat, {})
    kind = aw.get("await", "")
    # ----- slash commands
    if cmd in ("/control", "/controls"):
        return show(chat, None, *controls_screen()) or True
    if cmd == "/wallet":
        return show(chat, None, *wallet_screen()) or True
    if cmd in ("/referral", "/refer"):
        return show(chat, None, *referral_screen()) or True
    if cmd == "/games":
        return show(chat, None, *games_screen()) or True
    if cmd in ("/players", "/player"):
        return show(chat, None, *players_screen()) or True
    if cmd in ("/security",):
        return show(chat, None, *security_screen()) or True
    if cmd in ("/log", "/activity"):
        return show(chat, None, *log_screen()) or True
    if cmd == "/find":
        return (show(chat, None, *player_find(arg)) if arg else on_callback(chat, None, ["x", "pf"])) or True
    if cmd in ("/ban", "/unban"):
        parts = arg.split()
        if not parts:
            return show(chat, None, "Use: <code>/ban 1234567 7</code> (7 din; 0 = permanent) · <code>/unban 1234567</code>", [[B("👤 Players", "x:pl")]]) or True
        st, j, sid = T.admin_call("s1", "GET", "/user/admin/find?q=" + requests.utils.quote(parts[0]))
        us = (j.get("users") or []) if st == 200 else []
        if len(us) != 1:
            return show(chat, None, "❌ Player nahi mila ya ek se zyada mile — <code>/find</code> use karo.", [[B("👤 Players", "x:pl")]]) or True
        uid = us[0]["uid"]
        if cmd == "/ban":
            days = int(num(parts[1]) or 0) if len(parts) > 1 else 0
            ok_, er = player_ban(uid, days)
        else:
            ok_, er = player_unban(uid)
        t, r = player_find(uid)
        return show(chat, None, (("✅ Done\n\n") if ok_ else f"❌ {esc(er)}\n\n") + t, r) or True
    if cmd == "/maint":
        on = arg.lower().split()[:1] == ["on"]
        if arg.lower().split()[:1] not in (["on"], ["off"]):
            return show(chat, None, "Use: <code>/maint on Kuch der me aate hain</code> · <code>/maint off</code>") or True
        S["appMaintenance"] = on
        rest = arg.split(None, 1)[1].strip() if on and len(arg.split(None, 1)) > 1 else None
        if rest:
            S["maintMsg"] = rest[:300]
        L._save()
        log("control", f"APP maintenance {'ON' if on else 'OFF'} (command)")
        return show(chat, None, *controls_screen(f"{'🚧 Maintenance ON' if on else '🟢 App LIVE'}\n\n")) or True
    if cmd == "/readonly":
        v = arg.lower().strip()
        if v not in ("on", "off"):
            return show(chat, None, "Use: <code>/readonly on</code> / <code>/readonly off</code>") or True
        S["readOnly"] = v == "on"
        L._save()
        log("control", f"READ-ONLY {v.upper()} (command)")
        return show(chat, None, *controls_screen(f"🧊 Read-only {v.upper()}\n\n")) or True
    if cmd == "/feature":
        parts = arg.lower().split()
        if len(parts) != 2 or parts[0] not in L.FEATURES or parts[1] not in ("on", "off"):
            return show(chat, None, "Use: <code>/feature withdraw off</code>\nKeys: " + ", ".join(f"<code>{k}</code>" for k in L.FEATURES)) or True
        if parts[1] == "on":
            S["features"].pop(parts[0], None)
        else:
            S["features"][parts[0]] = False
        L._save()
        log("control", f"feature {parts[0]} → {parts[1].upper()} (command)")
        return show(chat, None, *controls_screen(f"✅ {esc(parts[0])} → {parts[1].upper()}\n\n")) or True
    if cmd == "/blockip":
        ok_, er = block_ip(arg)
        return show(chat, None, *security_screen("⛔ Blocked.\n\n" if ok_ else f"❌ {esc(er)}\n\n")) or True
    if cmd == "/unblockip":
        okk = unblock_ip(arg.strip())
        return show(chat, None, *ip_list_screen("♻️ Unblocked.\n\n" if okk else "❌ Ye IP list me nahi hai.\n\n")) or True
    if cmd == "/ratelimit":
        v = num(arg)
        if v is None or not 20 <= v <= 100000:
            return show(chat, None, "Use: <code>/ratelimit 300</code> (20 – 100000 per minute)") or True
        S["rateLimit"] = int(v)
        L._save()
        log("security", f"rate limit → {int(v)}/min (command)")
        return show(chat, None, *security_screen(f"✅ {int(v)}/min\n\n")) or True
    if cmd == "/alerts":
        v = arg.lower().strip()
        if v in ("on", "off"):
            S["alerts"] = v == "on"
            L._save()
        return show(chat, None, *T.settings_screen()) or True
    # ----- answers to a prompt
    if not kind.startswith("x_"):
        return False
    PENDING.pop(chat, None)
    if kind == "x_maint_msg":
        S["maintMsg"] = "" if t == "-" else t[:300]
        L._save()
        log("control", "maintenance message changed")
        return show(chat, None, *controls_screen("✅ Message saved\n\n")) or True
    if kind == "x_feat_msg":
        if t == "-":
            S["featureMsg"].pop(aw["key"], None)
        else:
            S["featureMsg"][aw["key"]] = t[:300]
        L._save()
        log("control", f"message for {aw['key']} changed")
        return show(chat, None, *feature_msgs_screen()) or True
    if kind == "x_wl_num":
        if t == "-":
            ok_, er = wallet_set({aw["key"]: ""})
        else:
            v = num(t)
            ok_, er = (False, "Sirf number bhejo") if v is None else wallet_set({aw["key"]: v})
        return show(chat, None, *wallet_screen("✅ Saved\n\n" if ok_ else f"❌ {esc(er)}\n\n")) or True
    if kind == "x_wl_txt":
        ok_, er = wallet_set({aw["key"]: "" if t == "-" else t})
        return show(chat, None, *wallet_screen("✅ Saved\n\n" if ok_ else f"❌ {esc(er)}\n\n")) or True
    if kind == "x_rf_num":
        v = num(t)
        ok_, er = (False, "Sirf number bhejo") if v is None else referral_set({aw["key"]: v})
        return show(chat, None, *referral_screen("✅ Saved\n\n" if ok_ else f"❌ {esc(er)}\n\n")) or True
    if kind == "x_player":
        return show(chat, None, *player_find(t)) or True
    if kind == "x_ip":
        ok_, er = block_ip(t)
        return show(chat, None, *security_screen("⛔ Blocked.\n\n" if ok_ else f"❌ {esc(er)}\n\n")) or True
    if kind == "x_rate":
        v = num(t)
        if v is None or not 20 <= v <= 100000:
            return show(chat, None, *security_screen("❌ 20 se 100000 ke beech number bhejo.\n\n")) or True
        S["rateLimit"] = int(v)
        L._save()
        log("security", f"rate limit → {int(v)}/min")
        return show(chat, None, *security_screen(f"✅ {int(v)}/min\n\n")) or True
    return False
