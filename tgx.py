"""Telegram bot - extra control screens (domain requests, domains, IP bans, admins, activity log, backup / restore, settings, bot mode).
Imported by tgbot.py.  All state changes go through app.py functions, so the web panel (/lb/ui) and the bot always behave the same."""
import json, time, datetime as dt
import tgbot as T
from tgbot import B, esc, ago, show

PER = 10
WHY = {"unknown_domain": "domain registered nahi", "bad_key": "galat site key", "missing_origin": "Origin header nahi", "disabled": "domain disabled",
       "expired": "access expire", "insecure": "http (https chahiye)"}
RATE_PRESETS = [0, 60, 120, 300, 600, 1200]
BACKUP_HOURS = [0, 6, 12, 24, 72, 168]


def L():
    return T.L


def fmt_ts(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%d %b %H:%M UTC") if ms else "-"


def until(ms):
    s = int((ms - time.time() * 1000) / 1000)
    if s <= 0:
        return "expired"
    return f"{s // 86400} d {s % 86400 // 3600} h" if s >= 86400 else f"{s // 3600} h {s % 3600 // 60} min"


def actor():
    return T.actor()


def home_btn():
    return [B("🏠 Menu", "m:main")]


# ------------------------------------------------------------------ main menu
def pending_count():
    return len(L().requests_list("n"))


def main_text():
    l = L()
    up = sum(1 for i in l.IDS if l.available(i))
    act = sum(1 for d in l.DOMAINS.values() if l.domain_live(d))
    chk = "🟢 ON" if l.SETTINGS["domainCheck"] else "🔴 OFF (anyone can call the API!)"
    lines = ["🛡 <b>FF Cash Battle — Control Panel</b>\n",
             f"🖥 Servers online: <b>{up}/5</b>   🌐 Domains: <b>{act}</b>",
             f"📥 Pending requests: <b>{pending_count()}</b>   🚷 Banned IPs: <b>{len(l.BANNED)}</b>",
             f"🔐 Domain check: {chk}"]
    if l.SETTINGS["maintenance"]:
        lines.append("🛠 <b>MAINTENANCE MODE ON</b> — users ko 503 mil raha hai")
    lines.append(f"🤖 Bot mode: <b>{T.MODE.upper()}</b>\n\nNeeche se option choose karo 👇")
    return "\n".join(lines)


def main_rows():
    n = pending_count()
    rows = [[B(f"📥 Domain requests{f' ({n})' if n else ''}", "rq:l:n:0")],
            [B("🌐 Domains", "d:list"), B("🖥 Servers", "s:list")],
            [B("🛡 Fraud cases", "f:list"), B("🗺 State block", "g:menu")],
            [B("💾 Backup", "bk:menu"), B("📊 Stats", "st:view")],
            [B("🚷 Banned IPs", "ip:l"), B("📜 Activity log", "au:l")],
            [B("⚙️ Settings", "set:menu"), B("👥 Admins", "ad:l")]]
    last = [B("🔄 Refresh", "m:main")]
    if T.PUBLIC_URL.startswith("https://"):
        last.insert(0, {"text": "🖥 Web panel", "url": T.PUBLIC_URL + "/lb/ui"})
    rows.append(last)
    return rows


HELP = ("🛡 <b>Commands</b>\n\n/start — menu\n/requests — domain requests (approve / reject)\n/domains — verified domains\n/adddomain <i>host</i> — domain add\n"
        "/removedomain <i>host</i>\n/banip <i>ip</i> [hours] · /unbanip <i>ip</i>\n/admins · /addadmin <i>id</i>\n/servers · /stats · /fraud · /geo\n"
        "/backup · /settings · /status · /log\n/cancel — chalu action roko")


# ------------------------------------------------------------------ domain requests (the queue: approve / reject)
def requests_screen(mode="n", page=0):
    l = L()
    items = l.requests_list(mode)
    pages = max(1, (len(items) + PER - 1) // PER)
    page = max(0, min(page, pages - 1))
    chunk = items[page * PER:(page + 1) * PER]
    title = {"n": "🆕 Newest", "t": "🔥 Most active", "a": "📋 All refused (log)"}.get(mode, "🆕 Newest")
    lines = [f"📥 <b>Domain requests</b> — {title}", f"{len(items)} total · page {page + 1}/{pages}\n"]
    if not chunk:
        lines.append("✅ Abhi koi pending request nahi hai." if mode != "a" else "✅ Abhi tak koi request reject nahi hui.")
    rows = []
    for n, e in enumerate(chunk, 1):
        lines.append(f"{n}. <code>{esc(e.get('host') or e['origin'])}</code> — {e['count']}× · {WHY.get(e.get('reason'), e.get('reason'))}\n    {ago(e['last'])} · IP {esc(e.get('ip'))}")
        act = e.get("reason") == "unknown_domain" and e.get("host") and l.norm_host(e["host"]) and e["host"] not in l.DOMAINS and e["host"] not in l.DENIED
        row = [B(f"🔍 {n}", f"rq:v:{e['id']}")]
        if act:
            row = [B(f"✅ {n}", f"rq:a:{e['id']}"), B(f"❌ {n}", f"rq:r:{e['id']}")] + row
        rows.append(row)
    if chunk and mode != "a":
        lines.append("\n✅ approve (site key ke saath) · ❌ reject (hamesha ke liye block) · 🔍 detail")
    nav = []
    if page > 0:
        nav.append(B("⬅️ Prev", f"rq:l:{mode}:{page - 1}"))
    if page < pages - 1:
        nav.append(B("Next ➡️", f"rq:l:{mode}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([B(("• " if mode == m else "") + lbl, f"rq:l:{m}:0") for m, lbl in (("n", "🆕 Newest"), ("t", "🔥 Top"), ("a", "📋 Log"))])
    rows.append([B(f"🚫 Rejected ({len(l.DENIED)})", "rj:l"), B("🧹 Clear log", "rq:clr")])
    if mode != "a" and len(items) > 1:
        rows.append([B(f"❌ Reject all ({len(items)})", "rq:ra")])
    rows.append([B("🔄 Refresh", f"rq:l:{mode}:{page}"), B("⬅️ Back", "m:main")])
    return "\n".join(lines), rows


def request_screen(rid):
    l = L()
    e = l.request_by_id(rid)
    if not e:
        return "ℹ️ Ye request ab list me nahi hai (handle ho chuki ya clear).", [[B("📥 Requests", "rq:l:n:0")]]
    h = e.get("host", "")
    act = e.get("reason") == "unknown_domain" and h and l.norm_host(h) and h not in l.DOMAINS and h not in l.DENIED
    txt = (f"🔍 <b>Request detail</b>\n\n🌐 <code>{esc(e['origin'])}</code>\nReason: <b>{WHY.get(e.get('reason'), e.get('reason'))}</b>\nRequests: <b>{e['count']}×</b>\n"
           f"First: {ago(e.get('first'))} · Last: {ago(e['last'])}\nPath: <code>{esc(e.get('path'))}</code>\nIPs: <code>{esc(', '.join(e.get('ips') or [e.get('ip')]))}</code>\n"
           f"Browser: <code>{esc((e.get('ua') or '-')[:120])}</code>")
    if e.get("reason") == "bad_key":
        txt += "\n\n💡 Ye domain approved hai par request me galat site key aa rahi hai. Domain screen se ♻️ New key banao aur frontend me lagao."
    rows = []
    if act:
        rows += [[B("✅ Approve + site key", f"rq:a:{rid}"), B("✅ Origin only", f"rq:o:{rid}")], [B("❌ Reject", f"rq:r:{rid}"), B("❌ Reject + ban IP", f"rq:rb:{rid}")]]
    if e.get("ips"):
        rows.append([B("🚷 Ban IP", f"ip:br:{rid}")])
    rows.append([B("⬅️ Requests", "rq:l:n:0")])
    return txt, rows


def key_msg_keyless(host):
    return (f"✅ <b>Domain approved (Origin only)</b>\n\n🌐 <code>{esc(host)}</code>\n\n⚠️ Site key nahi hai — sirf <code>https://{esc(host)}</code> ka Origin check hoga "
            "(curl se nakli Origin bheja ja sakta hai). Behtar: domain screen me 🔑 Create key.")


def on_requests(chat, msg, p):
    l, a = L(), p[1]
    if a == "l":
        return show(chat, msg, *requests_screen(p[2] if len(p) > 2 else "n", int(p[3]) if len(p) > 3 and p[3].isdigit() else 0))
    if a == "v":
        return show(chat, msg, *request_screen(p[2]))
    if a in ("a", "o"):
        rec, key = l.approve_request(p[2], keyless=(a == "o"), actor=actor())
        if not rec:
            return show(chat, msg, f"❌ {esc(key)}", [[B("📥 Requests", "rq:l:n:0")]])
        return show(chat, msg, T.key_message(rec["host"], key) if key else key_msg_keyless(rec["host"]), [[B("📥 Requests", "rq:l:n:0"), B("🌐 Domains", "d:list")]])
    if a in ("r", "rb"):
        e = l.request_by_id(p[2])
        ips = list((e or {}).get("ips", []))
        h = l.reject_request(p[2], actor=actor(), ban_ip=(a == "rb"))
        t, r = requests_screen("n", 0)
        return show(chat, msg, (f"❌ <code>{esc(h)}</code> reject ho gaya{' + IP ban: ' + esc(', '.join(ips)) if a == 'rb' else ''}\n\n" if h else "ℹ️ Request pehle hi handle ho chuki.\n\n") + t, r)
    if a == "ra":
        n = len(l.requests_list("n"))
        return show(chat, msg, f"❌ <b>Saare {n} pending domains reject karein?</b>\n\nYe sab hamesha ke liye block ho jayenge (Rejected list se wapas allow kar sakte ho).",
                    [[B("✅ Yes, reject all", "rq:ray"), B("✖️ No", "rq:l:n:0")]])
    if a == "ray":
        done = [l.reject_request(e["id"], actor=actor()) for e in l.requests_list("n")]
        t, r = requests_screen("n", 0)
        return show(chat, msg, f"❌ {len([x for x in done if x])} domains reject hue\n\n" + t, r)
    if a == "clr":
        l.BLOCKED.clear()
        l.audit("requests.clear", "", actor())
        return show(chat, msg, *requests_screen("n", 0))
    return show(chat, msg, *requests_screen())


def rejected_screen():
    l = L()
    items = sorted(l.DENIED.values(), key=lambda x: -x.get("at", 0))[:15]
    lines = [f"🚫 <b>Rejected domains</b> ({len(l.DENIED)})\n", "In domains ki requests chupchap refuse hoti hain aur dobara list me nahi aatin.\n"]
    rows = []
    for d in items:
        lines.append(f"• <code>{esc(d['host'])}</code> — {d.get('count', 0)}× · {ago(d.get('last') or d.get('at'))}")
        rows.append([B(f"↩️ Allow again: {d['host'][:30]}", f"rj:u:{l._sha(d['host'])[:8]}")])
    if not items:
        lines.append("Koi rejected domain nahi.")
    rows.append([B("⬅️ Requests", "rq:l:n:0")])
    return "\n".join(lines), rows


def on_rejected(chat, msg, p):
    l = L()
    if p[1] == "u" and len(p) > 2:
        h = next((x for x in l.DENIED if l._sha(x)[:8] == p[2]), None)
        if h:
            l.unreject(h, actor())
    return show(chat, msg, *rejected_screen())


# ------------------------------------------------------------------ domains
DPER = 12


def domains_screen(page=0):
    l = L()
    ds = sorted(l.DOMAINS.values(), key=lambda x: x["addedAt"])
    pages = max(1, (len(ds) + DPER - 1) // DPER)
    page = max(0, min(page, pages - 1))
    if not ds:
        txt = ("🌐 <b>Verified domains</b>\n\nAbhi koi domain add nahi hai, isliye API par <b>saari requests reject</b> ho rahi hain.\n"
               "📥 Requests se kisi domain ko approve karo ya ➕ Add domain dabao.")
    else:
        lines = [f"🌐 <b>Verified domains</b> ({len(ds)}) · page {page + 1}/{pages}\n"]
        for n, d in enumerate(ds[page * DPER:(page + 1) * DPER], page * DPER + 1):
            icon = "✅" if l.domain_live(d) else ("⌛" if d.get("expiresAt") and d["expiresAt"] < l.now_ms() else "⏸")
            lines.append(f"{n}. {icon} <code>{esc(d['host'])}</code> · {'🔑' if d.get('keyHash') else '⚠️ no key'} · {d.get('hits', 0)} req")
        lines.append("\nSirf inhi domains ki requests accept hongi, baaki sab reject.")
        txt = "\n".join(lines)
    rows = []
    for d in ds[page * DPER:(page + 1) * DPER]:
        rows.append([B(f"{'✅' if l.domain_live(d) else '⏸'} {d['host']}", f"d:{d['id']}")])
    nav = ([B("⬅️ Prev", f"d:list:{page - 1}")] if page > 0 else []) + ([B("Next ➡️", f"d:list:{page + 1}")] if page < pages - 1 else [])
    if nav:
        rows.append(nav)
    rows.append([B("➕ Add domain", "d:add"), B("📥 Requests", "rq:l:n:0")])
    rows.append([B("⬅️ Back", "m:main")])
    return txt, rows


def domain_screen(d, note=""):
    l = L()
    live = l.domain_live(d)
    state = "✅ Active" if live else ("⌛ Expired" if d.get("expiresAt") and d["expiresAt"] < l.now_ms() else "⏸ Disabled")
    txt = (f"{note}🌐 <b>{esc(d['host'])}</b>\n\nStatus: <b>{state}</b>\n"
           f"Access: {'🔑 Origin + site key' if d.get('keyHash') else '⚠️ Origin only (weaker)'}\n"
           f"Expires: <b>{('in ' + until(d['expiresAt']) + ' (' + fmt_ts(d['expiresAt']) + ')') if d.get('expiresAt') else 'never'}</b>\n"
           f"Rate limit: <b>{str(d['rate']) + '/min per IP' if d.get('rate') else 'global default'}</b>\n"
           f"Requests: <b>{d.get('hits', 0)}</b> · last used {ago(d.get('lastUsed'))}\nAdded: {ago(d['addedAt'])}\n"
           f"Note: {esc(d.get('note') or '-')}\n\nKey kho gayi ho to ♻️ New key dabao (purani key turant band).")
    i = d["id"]
    rows = [[B("⏸ Disable" if d.get("active", True) else "▶️ Enable", f"dx:{i}:t")],
            [B("♻️ New key", f"dx:{i}:k"), B("🔓 Key off", f"dx:{i}:ko")] if d.get("keyHash") else [B("🔑 Create key", f"dx:{i}:k")],
            [B(("• " if not d.get("expiresAt") else "") + "♾ Never", f"dx:{i}:e:0"), B("⏳ 24h", f"dx:{i}:e:24"), B("7d", f"dx:{i}:e:168"), B("30d", f"dx:{i}:e:720")],
            [B(("• " if (d.get("rate") or 0) == r else "") + (f"{r}/min" if r else "Default"), f"dx:{i}:r:{r}") for r in RATE_PRESETS[:4]],
            [B("📝 Note", f"dx:{i}:n"), B("🗑 Remove", f"dx:{i}:d")], [B("⬅️ Domains", "d:list")]]
    return txt, rows


def on_domain(chat, msg, p):
    l = L()
    if p[0] == "d":
        if p[1] == "list":
            T.PENDING.pop(chat, None)
            return show(chat, msg, *domains_screen(int(p[2]) if len(p) > 2 and p[2].isdigit() else 0))
        if p[1] == "add":
            T.PENDING[chat] = {"await": "add_domain"}
            return show(chat, msg, "➕ <b>Domain add karo</b>\n\nApni user-panel ka domain bhejo, jaise:\n<code>app.example.com</code>\n\n(https:// aur path mat likho)  ·  /cancel", [[B("✖️ Cancel", "d:list")]])
        d = l.domain_by_id(p[1])
        return show(chat, msg, *(domain_screen(d) if d else domains_screen()))
    d = l.domain_by_id(p[1])                                                     # dx:<id>:<action>[:arg]
    if not d:
        return show(chat, msg, *domains_screen())
    a, h = p[2], d["host"]
    if a == "k":
        key = l.rotate_key(h, actor())
        return show(chat, msg, T.key_message(h, key, new=False), [[B("Open domain", f"d:{d['id']}")]])
    if a == "ko":
        l.clear_key(h, actor())
        return show(chat, msg, *domain_screen(d, "🔓 Site key hata di — ab sirf Origin check hoga\n\n"))
    if a == "t":
        l.set_domain(h, actor(), active=not d.get("active", True))
    elif a == "e" and len(p) > 3 and p[3].isdigit():
        hrs = int(p[3])
        l.set_domain(h, actor(), expiresAt=(l.now_ms() + hrs * 3600 * 1000) if hrs else 0)
    elif a == "r" and len(p) > 3 and p[3].isdigit():
        l.set_domain(h, actor(), rate=int(p[3]))
    elif a == "n":
        T.PENDING[chat] = {"await": "dom_note", "host": h}
        return show(chat, msg, f"📝 <b>{esc(h)}</b> ke liye note bhejo (max 120 chars, '-' = note hatao).\n/cancel", [[B("✖️ Cancel", f"d:{d['id']}")]])
    elif a == "d":
        return show(chat, msg, f"🗑 <b>{esc(h)}</b> hata dein?\n\nIske baad is domain ki saari requests reject hongi.", [[B("✅ Yes, remove", f"dx:{d['id']}:dy"), B("✖️ No", f"d:{d['id']}")]])
    elif a == "dy":
        l.remove_domain(h, actor())
        t, r = domains_screen()
        return show(chat, msg, f"✅ <code>{esc(h)}</code> remove ho gaya.\n\n" + t, r)
    return show(chat, msg, *domain_screen(d, "✅ Updated\n\n"))


# ------------------------------------------------------------------ banned IPs
def bans_screen():
    l = L()
    items = [b for b in sorted(l.BANNED.values(), key=lambda x: -x["at"]) if l.is_banned(b["ip"])][:15]
    lines = [f"🚷 <b>Banned IPs</b> ({len(l.BANNED)})\n"]
    rows = []
    for b in items:
        lines.append(f"• <code>{esc(b['ip'])}</code> — {'till ' + fmt_ts(b['until']) if b.get('until') else 'permanent'}{' · ' + esc(b['note']) if b.get('note') else ''}")
        rows.append([B(f"✅ Unban {b['ip']}", f"ip:u:{b['ip']}")])
    if not items:
        lines.append("Koi IP ban nahi hai.")
    lines.append("\nBan hui IP ko API se 403 milta hai (kuch nahi dikhta).")
    rows += [[B("➕ Ban an IP", "ip:add")], [B("⬅️ Back", "m:main")]]
    return "\n".join(lines), rows


def on_ip(chat, msg, p):
    l = L()
    if p[1] == "u" and len(p) > 2:
        l.unban_ip(":".join(p[2:]), actor())                          # IPv6 contains ':'
    elif p[1] == "br" and len(p) > 2:
        e = l.request_by_id(p[2])
        done = [ip for ip in (e or {}).get("ips", []) if l.ban_ip_(ip, 0, f"request {e['host']}", actor(), save=False)[0]]
        l._save()
        return show(chat, msg, f"🚷 Banned: <code>{esc(', '.join(done) or '-')}</code>\n\n" + bans_screen()[0], bans_screen()[1])
    elif p[1] == "add":
        T.PENDING[chat] = {"await": "ban_ip"}
        return show(chat, msg, "🚷 <b>IP ban</b>\n\nIP bhejo. Kuch ghanto ke liye: <code>1.2.3.4 24</code> (24 ghante). Sirf IP = permanent.\n/cancel", [[B("✖️ Cancel", "ip:l")]])
    return show(chat, msg, *bans_screen())


def ban_text(chat, text):
    parts = text.split()
    ok_, er = L().ban_ip_(parts[0], float(parts[1]) if len(parts) > 1 and parts[1].replace(".", "", 1).isdigit() else 0, "from bot", actor()) if parts else (False, "empty")
    T.PENDING.pop(chat, None) if ok_ else None
    t, r = bans_screen()
    return (f"✅ Banned\n\n{t}" if ok_ else f"❌ {esc(er)} — dobara bhejo ya /cancel\n\n"), r


# ------------------------------------------------------------------ admins (owners = TG_ADMIN_IDS env; extra admins added here)
def admins_screen():
    l = L()
    lines = ["👥 <b>Admins</b>\n", "👑 <b>Owners</b> (env <code>TG_ADMIN_IDS</code>):"] + [f"• <code>{i}</code>" for i in sorted(T.OWNERS)]
    lines.append("\n👤 <b>Extra admins</b>:" + ("" if l.EXTRA_ADMINS else " koi nahi"))
    rows = []
    for a in sorted(l.EXTRA_ADMINS.values(), key=lambda x: x["at"]):
        lines.append(f"• <code>{a['id']}</code> — added {ago(a['at'])}")
        rows.append([B(f"🗑 Remove {a['id']}", f"ad:rm:{a['id']}")])
    lines.append("\nExtra admin = owner ke jaisa control, sirf admins add/remove karna sirf owner kar sakta hai.\nUser ki ID: @userinfobot")
    rows += [[B("➕ Add admin", "ad:add")], [B("⬅️ Back", "m:main")]]
    return "\n".join(lines), rows


def add_admin(uid, by):
    l = L()
    if not uid.lstrip("-").isdigit():
        return False, "ID number honi chahiye"
    if int(uid) in T.OWNERS or uid in l.EXTRA_ADMINS:
        return False, "ye pehle se admin hai"
    l.EXTRA_ADMINS[uid] = {"id": uid, "at": l.now_ms(), "by": by}
    l.audit("admin.add", uid, by)
    l._save()
    return True, ""


def on_admins(chat, msg, p):
    l = L()
    if p[1] != "l" and not T.is_owner():
        return show(chat, msg, "⛔ Sirf owner admins badal sakta hai.", [[B("⬅️ Back", "ad:l")]])
    if p[1] == "add":
        T.PENDING[chat] = {"await": "add_admin"}
        return show(chat, msg, "➕ <b>Admin add</b>\n\nNaye admin ki Telegram numeric ID bhejo (@userinfobot se milti hai).\n/cancel", [[B("✖️ Cancel", "ad:l")]])
    if p[1] == "rm" and len(p) > 2 and l.EXTRA_ADMINS.pop(p[2], None):
        l.audit("admin.remove", p[2], actor())
        l._save()
    return show(chat, msg, *admins_screen())


# ------------------------------------------------------------------ activity log
def audit_screen():
    items = list(L().AUDIT)[:12]
    lines = ["📜 <b>Activity log</b> <i>(latest 12 — poora log web panel me)</i>\n"] + [f"• {fmt_ts(a['t'])} · <b>{esc(a['do'][:30])}</b>\n    {esc(a['d'][:90])} <i>({esc(a['by'][:20])})</i>" for a in items]
    if not items:
        lines.append("Abhi koi activity nahi.")
    return "\n".join(lines), [[B("🔄 Refresh", "au:l"), B("⬅️ Back", "m:main")]]


# ------------------------------------------------------------------ settings + bot mode
def cycle(cur, options):
    return options[(options.index(cur) + 1) % len(options)] if cur in options else options[0]


def settings_screen(note=""):
    l, s = L(), L().SETTINGS
    on = lambda v: "🟢 ON" if v else "🔴 OFF"
    txt = (f"{note}⚙️ <b>Settings</b>\n\n🔐 Domain verification: <b>{on(s['domainCheck'])}</b>\n🔔 New-request alerts: <b>{on(s['notify'])}</b>\n"
           f"⏳ Rate limit: <b>{s['rate'] or l.RATE_PER_MIN}/min per IP</b>{'' if s['rate'] else ' (default)'}\n"
           f"🛠 Maintenance mode: <b>{on(s['maintenance'])}</b>\n   ↳ <i>{esc(s['maintMsg'] or 'default message')}</i>\n"
           f"💾 Auto backup: <b>{'every %s h' % s['autoBackupHours'] if s['autoBackupHours'] else 'OFF'}</b> · DB backup included: <b>{on(s['autoBackupDb'])}</b>\n"
           f"🤖 Bot mode: <b>{T.MODE.upper()}</b>\n\n"
           "<b>Domain check OFF</b> = emergency, koi bhi domain API use kar sakta hai.\n<b>Maintenance</b> = sab users ko 503 (admin key wale chalte hain).")
    rows = [[B("🔴 Domain check OFF" if s["domainCheck"] else "🟢 Domain check ON", f"set:t:domainCheck")],
            [B("🔕 Alerts OFF" if s["notify"] else "🔔 Alerts ON", "set:t:notify")],
            [B("⏳ Rate limit: change", "set:rate"), B("🛠 Maintenance " + ("OFF" if s["maintenance"] else "ON"), "set:t:maintenance")],
            [B("✏️ Maintenance message", "set:mm"), B("💾 Auto backup: change", "set:ab")],
            [B("💽 DB backup in auto: " + ("OFF" if s["autoBackupDb"] else "ON"), "set:t:autoBackupDb")],
            [B("🤖 Bot mode (webhook / polling)", "set:mode"), B("🔎 Bot status", "set:bot")],
            [B("⬅️ Back", "m:main")]]
    return txt, rows


def mode_screen(note=""):
    l = L()
    cur, over = T.MODE, l.SETTINGS.get("tgMode") or ""
    txt = (f"{note}🤖 <b>Bot mode</b>\n\nAbhi: <b>{cur.upper()}</b> ({'bot se set' if over else 'env TG_MODE'})\n\n"
           "<b>WEBHOOK</b> — Telegram message bhejta hai. Free Render ke liye best (service so jaye to message se jaag jati hai).\n"
           "<b>POLLING</b> — bot khud Telegram se updates mangta hai. Service hamesha jaagti honi chahiye (paid ya UptimeRobot/keep-alive). Free Render par so gayi to bot jawab nahi dega.\n\n"
           "Switch turant lagu hota hai, restart nahi chahiye.")
    rows = [[B(("• " if cur == "webhook" else "") + "🔗 Webhook", "set:ms:webhook"), B(("• " if cur == "polling" else "") + "🔄 Polling", "set:ms:polling")]]
    if over:
        rows.append([B("♻️ Use env default", "set:my:")])
    rows.append([B("⬅️ Settings", "set:menu")])
    return txt, rows


def on_settings(chat, msg, p):
    l, a = L(), p[1]
    if a == "menu":
        T.PENDING.pop(chat, None)
        return show(chat, msg, *settings_screen())
    if a == "bot":
        return show(chat, msg, *T.bot_status_screen())
    if a == "t" and len(p) > 2 and p[2] in ("domainCheck", "notify", "maintenance", "autoBackupDb"):
        l.set_setting(p[2], not l.SETTINGS[p[2]], actor())
        return show(chat, msg, *settings_screen("✅ Updated\n\n"))
    if a == "rate":
        l.set_setting("rate", cycle(l.SETTINGS["rate"], RATE_PRESETS), actor())
        return show(chat, msg, *settings_screen("✅ Rate limit updated (0 = default)\n\n"))
    if a == "ab":
        l.set_setting("autoBackupHours", cycle(l.SETTINGS["autoBackupHours"], BACKUP_HOURS), actor())
        return show(chat, msg, *settings_screen("✅ Auto backup updated\n\n"))
    if a == "mm":
        T.PENDING[chat] = {"await": "maint_msg"}
        return show(chat, msg, "✏️ <b>Maintenance message</b>\n\nUsers ko jo text dikhana hai wo bhejo (max 200). '-' = default.\n/cancel", [[B("✖️ Cancel", "set:menu")]])
    if a == "mode":
        return show(chat, msg, *mode_screen())
    if a == "ms" and len(p) > 2 and p[2] in ("webhook", "polling"):
        m = p[2]
        if m == T.MODE:
            return show(chat, msg, *mode_screen(f"ℹ️ Already {m.upper()}\n\n"))
        if m == "webhook" and not T.PUBLIC_URL.startswith("https://"):
            return show(chat, msg, *mode_screen("❌ Webhook ke liye env <code>PUBLIC_URL</code> (https) chahiye — warna bot band ho jayega.\n\n"))
        warn = ("\n\n⚠️ Free Render par service 15 min baad so jati hai — tab polling bot jawab nahi dega. Keep-alive apne-aap ON ho jayega, par free hours jaldi khatam honge."
                if m == "polling" else "\n\nℹ️ Ek hi bot copy chalni chahiye.")
        return show(chat, msg, f"🤖 Bot ko <b>{m.upper()}</b> mode par switch karein?{warn}", [[B("✅ Yes, switch", f"set:my:{m}"), B("✖️ No", "set:mode")]])
    if a == "my":
        m = p[2] if len(p) > 2 else ""
        if m not in ("", "webhook", "polling"):
            return show(chat, msg, *mode_screen())
        l.set_setting("tgMode", m, actor())
        time.sleep(1.5)
        return show(chat, msg, *mode_screen(f"✅ Mode → <b>{(m or 'env default').upper()}</b>\n\n"))
    return show(chat, msg, *settings_screen())


def settings_text(chat, aw, text):
    T.PENDING.pop(chat, None)
    L().set_setting("maintMsg", "" if text.strip() == "-" else text.strip(), actor())
    return settings_screen("✅ Message saved\n\n")


# ------------------------------------------------------------------ backup / restore
def backup_screen(note=""):
    s = L().SETTINGS
    return (f"{note}💾 <b>Backup</b>\n\n<b>🗄 Database backup</b> — Server-4 sab databases ka JSON.gz banata hai. Render free par disk restart pe saaf hota hai, "
            "isliye 📤 se seedha Telegram me file le lo.\n\n<b>⚙️ Config backup</b> — domains, site-key hashes, rejected, IP bans, settings, admins (ek .json file). "
            "Kabhi bhi 📥 Restore se wapas la sakte ho.\n\n"
            f"⏱ Auto backup: <b>{'every %s h' % s['autoBackupHours'] if s['autoBackupHours'] else 'OFF'}</b> (Settings me badlo) · last: {ago(s['lastBackupAt'])}",
            [[B("▶️ DB backup + send", "bk:run"), B("📂 List backups", "bk:list")],
             [B("📤 Config export", "bk:cfg"), B("📥 Config restore", "bk:restore")],
             [B("⚙️ Auto backup settings", "set:menu"), B("⬅️ Back", "m:main")]])


def config_file():
    l = L()
    return f"lb-config-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.json", json.dumps(l.export_state(), indent=1).encode()


def on_backup(chat, msg, p):
    l, a = L(), p[1]
    if a == "menu":
        T.PENDING.pop(chat, None)
        return show(chat, msg, *backup_screen())
    if a == "cfg":
        name, data = config_file()
        ok_, er = T.send_file(chat, name, data, "⚙️ LB config backup (domains, bans, settings). Restore: Backup → Config restore.")
        l.audit("backup.config", "exported", actor())
        return show(chat, msg, *backup_screen("✅ Config file bhej di\n\n" if ok_ else f"❌ {esc(er)}\n\n"))
    if a == "run":
        show(chat, msg, "⏳ Backup chal raha hai… (kuch der lag sakti hai)", [])
        ok_, info = T.run_db_backup(chat)
        l.audit("backup.db", info if isinstance(info, str) else "", actor())
        return show(chat, msg, *backup_screen(("✅ " if ok_ else "❌ ") + esc(info) + "\n\n"))
    if a == "list":
        st, j, sid = T.admin_call("s4", "GET", "/backup/list")
        fl = (j.get("files", []) if st == 200 else [])[-8:]
        rows = [[B(f"📤 {f['name']} ({f['bytes'] // 1024} KB)"[:60], f"bk:g:{f['name']}")] for f in reversed(fl)]
        txt = "📂 <b>Backups</b> <i>(server par, latest)</i>\n\n" + ("Kisi par tap karo → Telegram me file aa jayegi." if fl else (f"Koi backup nahi mila.\n<code>{esc(j.get('error', ''))}</code>" if st != 200 else "Koi backup nahi mila."))
        return show(chat, msg, txt, rows + [[B("⬅️ Back", "bk:menu")]])
    if a == "g" and len(p) > 2:
        ok_, info = T.send_db_backup(chat, p[2])
        return show(chat, msg, *backup_screen(("✅ " if ok_ else "❌ ") + esc(info) + "\n\n"))
    if a == "restore":
        T.PENDING[chat] = {"await": "restore"}
        return show(chat, msg, "📥 <b>Config restore</b>\n\nPehle export ki hui <code>lb-config-….json</code> file yahan bhejo (document ki tarah).\n/cancel", [[B("✖️ Cancel", "bk:menu")]])
    if a == "rs" and len(p) > 2:
        pend = T.PENDING.pop(chat, {})
        if "restore" not in pend:
            return show(chat, msg, *backup_screen("ℹ️ Restore ka data nahi mila — dobara file bhejo.\n\n"))
        s = l.import_state(pend["restore"], "merge" if p[2] == "m" else "replace", actor())
        return show(chat, msg, *backup_screen(f"✅ Restore ho gaya ({'merge' if p[2] == 'm' else 'replace'}): {s['domains']} domains, {s['denied']} rejected, {s['banned']} bans, {s['admins']} admins\n\n"))
    return show(chat, msg, *backup_screen())


def on_document(chat, m):
    """An uploaded file while waiting for a config restore."""
    l = L()
    d = m.get("document") or {}
    if T.PENDING.get(chat, {}).get("await") != "restore":
        return show(chat, None, "ℹ️ File mili, par abhi koi restore chalu nahi hai. Backup → Config restore dabao.", [[B("💾 Backup", "bk:menu")]])
    if d.get("file_size", 0) > 2 * 1024 * 1024:
        return show(chat, None, "❌ File 2 MB se badi hai — ye config backup nahi lagti.", [[B("✖️ Cancel", "bk:menu")]])
    raw = T.download_file(d.get("file_id", ""), 2 * 1024 * 1024)
    try:
        st = json.loads(raw.decode("utf-8")) if raw else None
    except Exception:
        st = None
    if not isinstance(st, dict) or not ("domains" in st or "settings" in st):
        return show(chat, None, "❌ Ye valid LB config backup nahi hai. Sahi file bhejo ya /cancel.", [[B("✖️ Cancel", "bk:menu")]])
    T.PENDING[chat] = {"await": None, "restore": st}
    s = l.state_summary(st)
    return show(chat, None, f"📥 <b>Backup file mil gayi</b>\n\n🌐 Domains: <b>{s['domains']}</b>\n🚫 Rejected: <b>{s['denied']}</b>\n🚷 IP bans: <b>{s['banned']}</b>\n👤 Extra admins: <b>{s['admins']}</b>\n\n"
                "<b>Replace</b> = abhi ka sab hata kar file wala lagao.\n<b>Merge</b> = abhi ka rakho, file se jo missing hai wo jodo.",
                [[B("🔁 Replace all", "bk:rs:r"), B("➕ Merge", "bk:rs:m")], [B("✖️ Cancel", "bk:menu")]])


# ------------------------------------------------------------------ typed answers
def on_text(chat, aw, text):
    """aw = PENDING[chat].  -> (text, rows) or None if this module does not own that await."""
    l, kind = L(), aw.get("await")
    if kind == "dom_note":
        T.PENDING.pop(chat, None)
        d = l.DOMAINS.get(aw["host"])
        if d:
            l.set_domain(aw["host"], actor(), note="" if text.strip() == "-" else text.strip())
            return domain_screen(d, "✅ Note saved\n\n")
        return domains_screen()
    if kind == "ban_ip":
        return ban_text(chat, text)
    if kind == "add_admin":
        if not T.is_owner():
            T.PENDING.pop(chat, None)
            return "⛔ Sirf owner admin add kar sakta hai.", [[B("⬅️ Back", "m:main")]]
        ok_, er = add_admin(text.strip(), actor())
        if ok_:
            T.PENDING.pop(chat, None)
        t, r = admins_screen()
        return (("✅ Admin added\n\n" if ok_ else f"❌ {esc(er)} — dobara bhejo ya /cancel\n\n") + t), r
    if kind == "maint_msg":
        return settings_text(chat, aw, text)
    if kind == "restore":
        return "📥 Text nahi, <b>file</b> bhejo (lb-config-….json) ya /cancel.", [[B("✖️ Cancel", "bk:menu")]]
    return None
