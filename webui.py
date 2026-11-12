"""Web control panel for the load balancer:  https://<your-lb>/lb/ui   (login = LB_ADMIN_KEY).
Same powers as the Telegram bot (both call the same functions in app.py).  JSON API under /lb/admin/*  (header X-Admin-Key).
The page itself holds no secrets; the key is kept only in the browser tab (sessionStorage)."""
import os, time
from functools import wraps
from flask import Blueprint, request, jsonify, send_from_directory, Response

bp = Blueprint("webui", __name__)
L = None
UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")


def init(app, lb_module):
    global L
    L = lb_module
    app.register_blueprint(bp)


def admin(fn):
    @wraps(fn)
    def w(*a, **kw):
        if not L._admin_ok():
            r = jsonify(ok=False, error="forbidden" if not L.admin_locked(L.client_ip()) else "too many wrong keys - try again in 10 minutes")
            r.status_code = 403
            r.headers["Cache-Control"] = "no-store"
            return r
        r = fn(*a, **kw)
        if isinstance(r, Response):
            r.headers["Cache-Control"] = "no-store"
        return r
    return w


def bad(msg, code=400):
    r = jsonify(ok=False, error=msg)
    r.status_code = code
    return r


def body():
    d = request.get_json(silent=True)
    return d if isinstance(d, dict) else {}


# ------------------------------------------------------------------ the page (static, strict CSP, no inline script)
CSP = "default-src 'none'; script-src 'self'; style-src 'self'; style-src-attr 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"


def static_file(name):
    r = send_from_directory(UI_DIR, name)
    r.headers.update({"Content-Security-Policy": CSP, "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-cache"})
    return r


@bp.get("/lb/ui")
@bp.get("/lb/ui/")
def ui_index():
    return static_file("index.html")


@bp.get("/lb/ui/app.js")
def ui_js():
    return static_file("app.js")


@bp.get("/lb/ui/app.css")
def ui_css():
    return static_file("app.css")


# ------------------------------------------------------------------ serialisers
def dom_json(d):
    return {"id": d["id"], "host": d["host"], "active": bool(d.get("active", True)), "live": L.domain_live(d), "hasKey": bool(d.get("keyHash")), "note": d.get("note", ""),
            "expiresAt": d.get("expiresAt", 0), "rate": d.get("rate", 0), "hits": d.get("hits", 0), "lastUsed": d.get("lastUsed", 0), "addedAt": d["addedAt"]}


def req_json(e):
    return {k: e.get(k) for k in ("id", "origin", "host", "count", "first", "last", "path", "ip", "ips", "reason", "ua")}


# ------------------------------------------------------------------ overview
@bp.get("/lb/admin/overview")
@admin
def overview():
    import tgbot
    with L.LOCK:
        servers = {i: {"name": tgbot.NAMES[i], "url": L.URLS[i], "mode": L.S[i]["mode"], "up": L.S[i]["up"], "available": L.available(i), "latencyMs": L.S[i]["latency"],
                       "lastCheck": L.S[i]["last"], "error": L.S[i]["error"], "maintenance": L.S[i]["info"].get("maintenance")} for i in L.IDS}
    return jsonify(ok=True, servers=servers, routes=L.route_table(), settings={k: L.SETTINGS[k] for k in L.SETTING_KEYS}, stats=L.STATS, now=L.now_ms(),
                   counts={"domains": len(L.DOMAINS), "pending": len(L.requests_list("n")), "rejected": len(L.DENIED), "banned": len(L.BANNED), "log": len(L.BLOCKED)},
                   bot={"enabled": bool(tgbot.TOKEN), "mode": tgbot.MODE, "envMode": tgbot.ENV_MODE, "lastPoll": int(tgbot.STATUS["lastPoll"] * 1000), "updates": tgbot.STATUS["updates"],
                        "admins": len(tgbot.admin_ids()), "publicUrl": bool(tgbot.PUBLIC_URL)}, synced=L.LOADED["ok"])


# ------------------------------------------------------------------ domains
@bp.get("/lb/admin/domains")
@admin
def domains():
    return jsonify(ok=True, domains=[dom_json(d) for d in sorted(L.DOMAINS.values(), key=lambda x: x["addedAt"])])


@bp.post("/lb/admin/domains")
@admin
def domain_add():
    d = body()
    host = L.norm_host(d.get("host", ""))
    if not host:
        return bad("not a valid domain (example: app.example.com)")
    if host in L.DOMAINS:
        return bad("domain already added", 409)
    if host.split(":")[0] == "localhost" and not L.ALLOW_LOCALHOST:
        return bad("localhost needs ALLOW_LOCALHOST=true")
    rec, key = L.add_domain(host, keyless=bool(d.get("keyless")), note=str(d.get("note") or ""), actor="web")
    return jsonify(ok=True, domain=dom_json(rec), siteKey=key)           # the key is shown ONCE


@bp.patch("/lb/admin/domains/<did>")
@admin
def domain_edit(did):
    dom = L.domain_by_id(did)
    if not dom:
        return bad("not found", 404)
    d, host, out = body(), dom["host"], {}
    kw = {}
    if "active" in d:
        kw["active"] = bool(d["active"])
    if "note" in d:
        kw["note"] = str(d["note"] or "")[:120]
    if "rate" in d:
        kw["rate"] = d["rate"]
    if "expiresInHours" in d:
        try:
            h = float(d["expiresInHours"])
        except (TypeError, ValueError):
            return bad("expiresInHours must be a number")
        kw["expiresAt"] = int(L.now_ms() + h * 3600 * 1000) if h > 0 else 0
    if kw:
        L.set_domain(host, "web", **kw)
    if d.get("rotateKey"):
        out["siteKey"] = L.rotate_key(host, "web")
    if d.get("removeKey"):
        L.clear_key(host, "web")
    return jsonify(ok=True, domain=dom_json(dom), **out)


@bp.delete("/lb/admin/domains/<did>")
@admin
def domain_del(did):
    dom = L.domain_by_id(did)
    return jsonify(ok=True) if dom and L.remove_domain(dom["host"], "web") else bad("not found", 404)


# ------------------------------------------------------------------ domain requests (approve / reject)
@bp.get("/lb/admin/requests")
@admin
def requests_get():
    mode = request.args.get("mode", "n")
    return jsonify(ok=True, requests=[req_json(e) for e in L.requests_list(mode if mode in ("n", "t", "a") else "n")[:100]])


@bp.post("/lb/admin/requests/<rid>/approve")
@admin
def request_approve(rid):
    rec, key = L.approve_request(rid, keyless=bool(body().get("keyless")), actor="web")
    return bad(key, 404) if not rec else jsonify(ok=True, domain=dom_json(rec), siteKey=key)


@bp.post("/lb/admin/requests/<rid>/reject")
@admin
def request_reject(rid):
    h = L.reject_request(rid, actor="web", ban_ip=bool(body().get("banIp")))
    return jsonify(ok=True, host=h) if h else bad("request not found", 404)


@bp.post("/lb/admin/requests/clear")
@admin
def requests_clear():
    L.BLOCKED.clear()
    L.audit("requests.clear", "", "web")
    return jsonify(ok=True)


@bp.get("/lb/admin/rejected")
@admin
def rejected_get():
    return jsonify(ok=True, rejected=sorted(L.DENIED.values(), key=lambda x: -x.get("at", 0)))


@bp.post("/lb/admin/rejected/undo")
@admin
def rejected_undo():
    return jsonify(ok=True) if L.unreject(L.norm_host(body().get("host", "")), "web") else bad("not found", 404)


# ------------------------------------------------------------------ banned IPs
@bp.get("/lb/admin/bans")
@admin
def bans_get():
    return jsonify(ok=True, bans=[b for b in sorted(L.BANNED.values(), key=lambda x: -x["at"]) if L.is_banned(b["ip"])])


@bp.post("/lb/admin/bans")
@admin
def bans_add():
    d = body()
    try:
        hrs = max(0.0, float(d.get("hours") or 0))
    except (TypeError, ValueError):
        return bad("hours must be a number")
    ok_, er = L.ban_ip_(str(d.get("ip", "")), hrs, str(d.get("note") or ""), "web")
    return jsonify(ok=True) if ok_ else bad(er)


@bp.post("/lb/admin/bans/remove")
@admin
def bans_remove():
    return jsonify(ok=True) if L.unban_ip(str(body().get("ip", "")), "web") else bad("not found", 404)


# ------------------------------------------------------------------ settings / activity / servers
@bp.post("/lb/admin/settings")
@admin
def settings_set():
    changed = {}
    for k, v in body().items():
        if k not in L.SETTING_KEYS:
            return bad(f"unknown setting: {k}")
        if k == "tgMode" and v == "webhook":
            import tgbot
            if not tgbot.PUBLIC_URL.startswith("https://"):
                return bad("webhook mode needs env PUBLIC_URL (https)")
        ok_, er = L.set_setting(k, v, "web")
        if not ok_:
            return bad(er)
        changed[k] = L.SETTINGS[k]
    return jsonify(ok=True, settings=changed)


@bp.get("/lb/admin/audit")
@admin
def audit_get():
    return jsonify(ok=True, log=list(L.AUDIT))


@bp.post("/lb/admin/servers-all")
@admin
def servers_all_auto():
    for i in L.IDS:
        L.S[i]["mode"] = "auto"
    L.audit("server.mode", "all -> auto", "web")
    L._save()
    return jsonify(ok=True)


# ------------------------------------------------------------------ backup / restore
@bp.get("/lb/admin/export")
@admin
def export_cfg():
    import json
    r = Response(json.dumps(L.export_state(), indent=1), mimetype="application/json")
    r.headers["Content-Disposition"] = f"attachment; filename=lb-config-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.json"
    return r


@bp.post("/lb/admin/import")
@admin
def import_cfg():
    d = body()
    st = d.get("state")
    if not isinstance(st, dict) or not ("domains" in st or "settings" in st):
        return bad("not a valid config backup")
    if d.get("preview"):
        return jsonify(ok=True, summary=L.state_summary(st))
    return jsonify(ok=True, summary=L.import_state(st, "merge" if d.get("mode") == "merge" else "replace", "web"))


@bp.post("/lb/admin/backup/run")
@admin
def backup_run():
    import tgbot
    st, j, sid = tgbot.admin_call("s4", "POST", "/backup/run")
    return (jsonify(ok=True, file=j.get("file"), bytes=j.get("bytes"), via=sid) if st == 200 else bad(str(j.get("error", st)), 502))


@bp.get("/lb/admin/backup/list")
@admin
def backup_list():
    import tgbot
    st, j, sid = tgbot.admin_call("s4", "GET", "/backup/list")
    return jsonify(ok=True, files=j.get("files", [])) if st == 200 else bad(str(j.get("error", st)), 502)


@bp.get("/lb/admin/backup/download/<name>")
@admin
def backup_download(name):
    import tgbot, re
    if not re.fullmatch(r"backup-[0-9-]+\.json\.gz", name):
        return bad("bad file name")
    st, data, sid = tgbot.admin_raw("s4", "GET", "/backup/download/" + name)
    if st != 200 or not isinstance(data, bytes):
        return bad("download failed", 502)
    r = Response(data, mimetype="application/gzip")
    r.headers["Content-Disposition"] = f"attachment; filename={name}"
    return r


@bp.post("/lb/admin/backup/telegram")
@admin
def backup_to_telegram():
    """Send the config file (and optionally a fresh DB backup) to every bot admin."""
    import tgbot
    if not tgbot.TOKEN:
        return bad("TG_BOT_TOKEN not set")
    import tgx
    name, data = tgx.config_file()
    res = [tgbot.send_file(c, name, data, "⚙️ LB config backup (from web panel)") for c in sorted(tgbot.admin_ids())]
    return jsonify(ok=all(r[0] for r in res), sentTo=len(res))
