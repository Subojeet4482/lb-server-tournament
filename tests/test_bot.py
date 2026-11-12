import json, threading, time
import pytest
from conftest import hdr


@pytest.fixture()
def bot(lb, monkeypatch):
    import tgbot, tgx
    calls = []

    def fake_api(method, _t=20, **p):
        calls.append((method, p))
        return {"ok": True, "result": {}}
    monkeypatch.setattr(tgbot, "api", fake_api)
    monkeypatch.setattr(tgbot, "TOKEN", "123:abc")
    monkeypatch.setattr(tgbot, "OWNERS", {1})
    monkeypatch.setattr(tgbot, "L", lb)
    tgbot.PENDING.clear()
    tgbot.NOTIFY.update(times=[], last=0.0)
    sent = []
    monkeypatch.setattr(tgbot, "send_file", lambda chat, name, data, cap="": sent.append((chat, name, data)) or (True, ""))
    tgbot.calls, tgbot.sent = calls, sent
    return tgbot


def msg(uid, text=None, doc=None):
    m = {"from": {"id": uid}, "chat": {"id": uid, "type": "private"}}
    if text is not None:
        m["text"] = text
    if doc:
        m["document"] = doc
    return {"message": m}


def cb(uid, data):
    return {"callback_query": {"id": "c", "from": {"id": uid}, "message": {"chat": {"id": uid}, "message_id": 5}, "data": data}}


def last_text(bot):
    return next(p["text"] for m, p in reversed(bot.calls) if m in ("sendMessage", "editMessageText"))


def last_buttons(bot):
    p = next(p for m, p in reversed(bot.calls) if m in ("sendMessage", "editMessageText"))
    return [b for row in p["reply_markup"]["inline_keyboard"] for b in row]


def test_access_control(bot):
    bot.handle_update(msg(99, "/start"))
    assert "Access denied" in last_text(bot)
    bot.calls.clear()
    bot.handle_update(cb(99, "set:t:domainCheck"))
    assert bot.calls[0][0] == "answerCallbackQuery" and bot.L.SETTINGS["domainCheck"] is True


def test_menu_and_request_approve_flow(lb, bot, client):
    client.get("/public/x", headers=hdr("https://shop.example.org"))
    bot.handle_update(msg(1, "/start"))
    assert "Pending requests: <b>1</b>" in last_text(bot) and any(b["callback_data"] == "rq:l:n:0" for b in last_buttons(bot))
    bot.handle_update(cb(1, "rq:l:n:0"))
    assert "shop.example.org" in last_text(bot)
    rid = lb.requests_list("n")[0]["id"]
    assert {f"rq:a:{rid}", f"rq:r:{rid}", f"rq:v:{rid}"} <= {b["callback_data"] for b in last_buttons(bot)}
    bot.handle_update(cb(1, f"rq:a:{rid}"))
    t = last_text(bot)
    assert "Site key" in t and "sk_" in t and "shop.example.org" in lb.DOMAINS
    bot.handle_update(cb(1, f"rq:a:{rid}"))                      # double tap: friendly message, no crash, no second key
    assert "already" in last_text(bot) or "not found" in last_text(bot)
    assert any(a["by"] == "tg:1" for a in lb.AUDIT)


def test_reject_variants_and_rejected_list(lb, bot, client):
    client.get("/public/x", headers=hdr("https://a.example.org", ip="4.4.4.4"))
    client.get("/public/x", headers=hdr("https://b.example.org", ip="4.4.4.5"))
    ra = lb.requests_list("n")
    bot.handle_update(cb(1, f"rq:rb:{next(e['id'] for e in ra if e['host'] == 'a.example.org')}"))
    assert "a.example.org" in lb.DENIED and "4.4.4.4" in lb.BANNED
    bot.handle_update(cb(1, "rq:ra"))
    assert "reject" in last_text(bot).lower()
    bot.handle_update(cb(1, "rq:ray"))
    assert "b.example.org" in lb.DENIED and not lb.requests_list("n")
    bot.handle_update(cb(1, "rj:l"))
    assert "a.example.org" in last_text(bot)
    bot.handle_update(cb(1, f"rj:u:{lb._sha('a.example.org')[:8]}"))
    assert "a.example.org" not in lb.DENIED


def test_new_request_alert_and_throttle(lb, bot, client):
    lb.HOOKS["notify"] = bot.notify_new_request
    client.get("/public/x", headers=hdr("https://n1.example.org"))
    time.sleep(0.3)
    sent = [p for m, p in bot.calls if m == "sendMessage" and p["chat_id"] == 1]
    assert sent and "New domain request" in sent[0]["text"] and "n1.example.org" in sent[0]["text"]
    client.get("/public/x", headers=hdr("https://n2.example.org"))              # inside the 10 s window -> no second alert
    time.sleep(0.3)
    assert len([1 for m, p in bot.calls if m == "sendMessage"]) == 1
    lb.set_setting("notify", False)
    bot.NOTIFY["last"] = 0
    client.get("/public/x", headers=hdr("https://n3.example.org"))
    time.sleep(0.3)
    assert len([1 for m, p in bot.calls if m == "sendMessage"]) == 1


def test_domain_controls(lb, bot):
    rec, key = lb.add_domain("a.example.org")
    d = rec["id"]
    for data in (f"dx:{d}:e:24", f"dx:{d}:r:120", f"dx:{d}:t", f"dx:{d}:t", f"dx:{d}:ko", f"dx:{d}:k"):
        bot.handle_update(cb(1, data))
    r = lb.DOMAINS["a.example.org"]
    assert r["rate"] == 120 and r["expiresAt"] > lb.now_ms() and r["active"] and r["keyHash"]
    bot.handle_update(cb(1, f"dx:{d}:n"))
    bot.handle_update(msg(1, "my note"))
    assert lb.DOMAINS["a.example.org"]["note"] == "my note"
    bot.handle_update(cb(1, f"dx:{d}:d"))
    bot.handle_update(cb(1, f"dx:{d}:dy"))
    assert "a.example.org" not in lb.DOMAINS


def test_bans_and_commands(lb, bot):
    bot.handle_update(msg(1, "/banip 2001:db8::1 24"))
    assert "2001:db8::1" in lb.BANNED and lb.BANNED["2001:db8::1"]["until"] > 0
    bot.handle_update(msg(1, "/banip nonsense"))
    assert "valid IP" in last_text(bot)
    bot.handle_update(cb(1, "ip:u:2001:db8::1"))
    assert not lb.BANNED
    bot.handle_update(msg(1, "/adddomain https://x.example.org/path"))
    assert "x.example.org" in lb.DOMAINS
    bot.handle_update(msg(1, "/removedomain x.example.org"))
    assert not lb.DOMAINS
    for c in ("/help", "/requests", "/domains", "/servers", "/stats", "/log", "/settings", "/backup", "/admins", "/status", "/geo"):
        bot.calls.clear()
        bot.handle_update(msg(1, c))
        assert any(m in ("sendMessage", "editMessageText") for m, p in bot.calls), c


def test_admin_management_owner_only(lb, bot):
    bot.handle_update(msg(1, "/addadmin 42"))
    assert "42" in lb.EXTRA_ADMINS
    bot.handle_update(msg(42, "/start"))
    assert "Control Panel" in last_text(bot)                                  # new admin works immediately
    bot.handle_update(msg(42, "/addadmin 77"))
    assert "77" not in lb.EXTRA_ADMINS and "owner" in last_text(bot).lower()
    bot.handle_update(cb(42, "ad:rm:42"))
    assert "42" in lb.EXTRA_ADMINS                                            # an extra admin cannot remove admins
    bot.handle_update(cb(1, "ad:rm:42"))
    assert "42" not in lb.EXTRA_ADMINS
    bot.handle_update(msg(42, "/start"))
    assert "Access denied" in last_text(bot)


def test_settings_toggles(lb, bot):
    for data in ("set:t:maintenance", "set:t:notify", "set:rate", "set:ab", "set:t:autoBackupDb"):
        bot.handle_update(cb(1, data))
    s = lb.SETTINGS
    assert s["maintenance"] and not s["notify"] and s["rate"] == 60 and s["autoBackupHours"] == 6 and s["autoBackupDb"]
    bot.handle_update(cb(1, "set:mm"))
    bot.handle_update(msg(1, "Back at 6pm"))
    assert s["maintMsg"] == "Back at 6pm"


def test_polling_webhook_runtime_switch(lb, bot, monkeypatch):
    stop = threading.Event()
    started = []

    def fake(gen):
        started.append((bot.MODE, gen))
        stop.wait(5)
    monkeypatch.setattr(bot, "_run_polling", fake)
    monkeypatch.setattr(bot, "_run_webhook", fake)
    monkeypatch.setattr(bot, "MODE", "webhook")
    monkeypatch.setattr(bot, "PUBLIC_URL", "https://lb.example.com")
    lb.HOOKS["mode"] = bot.apply_mode
    bot.RUN.update(thread=None)
    bot.apply_mode()
    time.sleep(0.1)
    bot.handle_update(cb(1, "set:ms:polling"))
    assert "POLLING" in last_text(bot) and bot.MODE == "webhook"            # needs confirmation first
    g0 = bot.RUN["gen"]
    bot.handle_update(cb(1, "set:my:polling"))
    time.sleep(0.2)
    assert bot.MODE == "polling" and lb.SETTINGS["tgMode"] == "polling" and bot.RUN["gen"] == g0 + 1 and started[-1][0] == "polling"
    assert bot.handle_webhook("x", "x", {"update_id": 1}) == (404, "not found")        # polling mode refuses webhook calls
    bot.handle_update(cb(1, "set:my:webhook"))
    time.sleep(0.2)
    assert bot.MODE == "webhook" and started[-1][0] == "webhook"
    monkeypatch.setattr(bot, "PUBLIC_URL", "")
    bot.handle_update(cb(1, "set:my:polling")); time.sleep(0.2)
    bot.handle_update(cb(1, "set:ms:webhook"))
    assert "PUBLIC_URL" in last_text(bot) and bot.MODE == "polling"           # cannot switch to webhook without a public https URL
    stop.set()


def test_polling_loop_exits_on_generation_change(lb, bot, monkeypatch):
    monkeypatch.setattr(bot.time, "sleep", lambda s: None)
    n = {"c": 0}

    def api(method, _t=20, **p):
        if method == "getUpdates":
            n["c"] += 1
            bot.RUN["gen"] += 1                                          # mode switched while we were waiting
            return {"ok": True, "result": [{"update_id": 5, "message": msg(1, "/start")["message"]}]}
        return {"ok": True, "result": {}}
    monkeypatch.setattr(bot, "api", api)
    bot.RUN["gen"] = 7
    bot._run_polling(7)
    assert n["c"] == 1 and bot.STATUS["updates"] >= 0                    # returned without processing / looping


def test_config_backup_export_and_restore(lb, bot, monkeypatch):
    lb.add_domain("keep.example.org")
    bot.handle_update(cb(1, "bk:cfg"))
    chat, name, data = bot.sent[-1]
    assert chat == 1 and name.startswith("lb-config-") and "keep.example.org" in json.loads(data)["domains"]
    lb.DOMAINS.clear()
    bot.handle_update(cb(1, "bk:restore"))
    monkeypatch.setattr(bot, "download_file", lambda fid, mx: data)
    bot.handle_update(msg(1, doc={"file_id": "f", "file_size": len(data)}))
    assert "Backup file mil gayi" in last_text(bot) and not lb.DOMAINS
    bot.handle_update(cb(1, "bk:rs:r"))
    assert "keep.example.org" in lb.DOMAINS
    bot.handle_update(msg(1, doc={"file_id": "f", "file_size": 10}))             # file without a restore in progress
    assert "restore" in last_text(bot).lower()
    bot.handle_update(cb(1, "bk:restore"))
    monkeypatch.setattr(bot, "download_file", lambda fid, mx: b"not json")
    bot.handle_update(msg(1, doc={"file_id": "f", "file_size": 8}))
    assert "valid" in last_text(bot)


def test_db_backup_send(lb, bot, monkeypatch):
    def admin_call(group, method, path, js=None):
        return 200, {"file": "backup-20260101-000000.json.gz", "files": [{"name": "backup-20260101-000000.json.gz", "bytes": 2048}]}, "s4"
    monkeypatch.setattr(bot, "admin_call", admin_call)
    monkeypatch.setattr(bot, "admin_raw", lambda g, m, p: (200, b"GZ" * 10, "s4"))
    bot.handle_update(cb(1, "bk:run"))
    assert bot.sent[-1][1] == "backup-20260101-000000.json.gz" and "bhej di" in last_text(bot)
    assert bot.send_db_backup(1, "../../etc/passwd")[0] is False
    monkeypatch.setattr(bot, "admin_raw", lambda g, m, p: (200, b"x" * (50 * 1024 * 1024), "s4"))
    ok, info = bot.send_db_backup(1, "backup-1.json.gz")
    assert not ok and "50 MB" in info
    bot.handle_update(cb(1, "bk:list"))
    assert any(b["callback_data"].startswith("bk:g:backup-") for b in last_buttons(bot))


def test_auto_backup_runs_when_due(lb, bot, monkeypatch):
    monkeypatch.setattr(bot, "admin_call", lambda *a, **k: (200, {"file": "backup-1.json.gz"}, "s4"))
    monkeypatch.setattr(bot, "admin_raw", lambda *a: (200, b"zz", "s4"))
    lb.SETTINGS.update(autoBackupHours=6, autoBackupDb=True)
    bot.run_auto_backup()
    names = [n for c, n, d in bot.sent]
    assert any(n.startswith("lb-config-") for n in names) and "backup-1.json.gz" in names and lb.SETTINGS["lastBackupAt"] > 0


def test_every_screen_fits_telegram_limits(lb, bot, client, monkeypatch):
    import tgx
    for i in range(40):
        lb.add_domain(f"site{i}.example.org", note="n" * 100)
        client.get("/public/x", headers=hdr(f"https://req{i}-" + "a" * 50 + ".example.org", ip=f"3.3.3.{i}"))
        lb.ban_ip_(f"2001:db8:{i:x}:1:2:3:4:5", 0, "n" * 70, "t")
    lb.reject_request(lb.requests_list("n")[0]["id"], "t")
    for i in range(30):
        lb.audit("x" * 30, "d" * 200, "tg:123456789")
    monkeypatch.setattr(bot, "admin_call", lambda *a, **k: (200, {"files": [{"name": f"backup-20260101-0000{i:02d}.json.gz", "bytes": 1} for i in range(20)]}, "s4"))
    screens = [bot.main_text(), tgx.requests_screen("n", 0), tgx.requests_screen("t", 1), tgx.requests_screen("a", 0), tgx.rejected_screen(), tgx.domains_screen(0), tgx.domains_screen(2),
               tgx.domain_screen(next(iter(lb.DOMAINS.values()))), tgx.bans_screen(), tgx.admins_screen(), tgx.audit_screen(), tgx.settings_screen(), tgx.mode_screen(), tgx.backup_screen(),
               bot.stats_screen(), bot.servers_screen(), tgx.request_screen(lb.requests_list("n")[0]["id"])]
    screens[0] = (bot.main_text(), bot.main_rows())
    for txt, rows in screens:
        assert len(txt) <= 4096
        for row in rows:
            assert len(row) <= 4
            for b in row:
                assert len(b.get("callback_data", "").encode()) <= 64, b
                assert "callback_data" in b or "url" in b


def test_show_falls_back_to_plain_text_on_parse_error(bot, monkeypatch):
    calls = []

    def api(method, _t=20, **p):
        calls.append((method, p))
        return {"ok": False, "description": "Bad Request: can't parse entities"} if p.get("parse_mode") else {"ok": True}
    monkeypatch.setattr(bot, "api", api)
    bot.show(1, None, "<b>hello</b> &amp; <i>x", [])
    assert calls[-1][1]["text"] == "hello & x" and "parse_mode" not in calls[-1][1]
