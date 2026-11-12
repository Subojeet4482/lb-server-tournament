import json, time
from conftest import hdr

ADM = {"X-Admin-Key": "adminkey123"}


def test_unknown_domain_queued_then_approved(lb, client):
    r = client.get("/public/x", headers=hdr("https://shop.example.org"))
    assert r.status_code == 403 and r.get_json()["reason"] == "unknown_domain"
    reqs = lb.requests_list("n")
    assert len(reqs) == 1 and reqs[0]["host"] == "shop.example.org"
    rec, key = lb.approve_request(reqs[0]["id"], actor="t")
    assert key.startswith("sk_") and not lb.requests_list("n")
    assert client.get("/public/x", headers=hdr("https://shop.example.org")).get_json()["reason"] == "bad_key"
    r = client.get("/public/x", headers=hdr("https://shop.example.org", key))
    assert r.status_code == 503 and r.get_json()["code"] == "unavailable"          # passed verification, no backend servers in the test
    assert lb.DOMAINS["shop.example.org"]["hits"] == 1


def test_reject_is_silent_and_not_listed_again(lb, client):
    client.get("/public/x", headers=hdr("https://evil.example.org"))
    rid = lb.requests_list("n")[0]["id"]
    assert lb.reject_request(rid, "t") == "evil.example.org"
    for _ in range(3):
        r = client.get("/public/x", headers=hdr("https://evil.example.org"))
        assert r.status_code == 403 and r.get_json()["reason"] == "rejected"
    assert not lb.requests_list("n") and not lb.requests_list("a")
    assert lb.STATS["rejected"] == 3 and lb.DENIED["evil.example.org"]["count"] == 4
    lb.unreject("evil.example.org")
    client.get("/public/x", headers=hdr("https://evil.example.org"))
    assert len(lb.requests_list("n")) == 1


def test_preflight_only_for_live_domains(lb, client):
    assert client.options("/public/x", headers=hdr("https://nope.example.org")).status_code == 403
    lb.add_domain("ok.example.org")
    r = client.options("/public/x", headers=hdr("https://ok.example.org"))
    assert r.status_code == 204 and r.headers["Access-Control-Allow-Origin"] == "https://ok.example.org"
    lb.set_domain("ok.example.org", expiresAt=lb.now_ms() - 1000)
    assert client.options("/public/x", headers=hdr("https://ok.example.org")).status_code == 403


def test_expiry_disable_and_origin_only(lb, client):
    rec, key = lb.add_domain("a.example.org", keyless=True)
    assert key == "" and rec["keyHash"] == ""
    assert client.get("/public/x", headers=hdr("https://a.example.org")).status_code == 503       # origin only: no key needed
    lb.set_domain("a.example.org", expiresAt=lb.now_ms() - 5)
    assert client.get("/public/x", headers=hdr("https://a.example.org")).get_json()["reason"] == "expired"
    lb.set_domain("a.example.org", expiresAt=0, active=False)
    assert client.get("/public/x", headers=hdr("https://a.example.org")).get_json()["reason"] == "disabled"


def test_http_origin_refused(lb, client):
    lb.add_domain("a.example.org")
    assert client.get("/public/x", headers=hdr("http://a.example.org")).get_json()["reason"] == "insecure"


def test_ip_ban_and_expiry(lb, client):
    lb.add_domain("a.example.org", keyless=True)
    ok, _ = lb.ban_ip_("5.5.5.5", 0, "t", "t")
    assert ok and client.get("/public/x", headers=hdr("https://a.example.org", ip="5.5.5.5")).status_code == 403
    assert client.get("/public/x", headers=hdr("https://a.example.org", ip="6.6.6.6")).status_code == 503
    assert lb.ban_ip_("nonsense")[0] is False
    lb.BANNED["5.5.5.5"]["until"] = lb.now_ms() - 1
    assert lb.is_banned("5.5.5.5") is False and "5.5.5.5" not in lb.BANNED


def test_per_domain_rate_limit(lb, client):
    lb.add_domain("a.example.org", keyless=True)
    lb.set_domain("a.example.org", rate=3)
    codes = [client.get("/public/x", headers=hdr("https://a.example.org")).status_code for _ in range(5)]
    assert codes[:3] == [503] * 3 and codes[3:] == [429] * 2


def test_maintenance(lb, client):
    lb.add_domain("a.example.org", keyless=True)
    lb.set_setting("maintenance", True)
    lb.set_setting("maintMsg", "back soon")
    r = client.get("/public/x", headers=hdr("https://a.example.org"))
    assert r.status_code == 503 and r.get_json()["code"] == "maintenance" and r.get_json()["error"] == "back soon"
    assert client.get("/public/x", headers={**hdr("https://a.example.org"), **ADM}).get_json()["code"] == "unavailable"   # admin key bypasses


def test_domain_check_off(lb, client):
    lb.set_setting("domainCheck", False)
    assert client.get("/public/x", headers=hdr("https://anything.example.org")).get_json()["code"] == "unavailable"


def test_web_api_auth_and_lockout(lb, client):
    assert client.get("/lb/admin/overview").status_code == 403
    assert client.get("/lb/admin/overview", headers=ADM).status_code == 200
    for _ in range(10):
        client.get("/lb/admin/overview", headers={"X-Admin-Key": "wrong", "X-Forwarded-For": "7.7.7.7"})
    r = client.get("/lb/admin/overview", headers={**ADM, "X-Forwarded-For": "7.7.7.7"})
    assert r.status_code == 403                                   # locked even with the right key
    assert client.get("/lb/admin/overview", headers={**ADM, "X-Forwarded-For": "8.8.8.8"}).status_code == 200
    assert client.get("/lb/admin/overview", headers={"X-Admin-Key": "ключ"}).status_code == 403   # non-ASCII must not crash


def test_web_api_flow(lb, client):
    client.get("/public/x", headers=hdr("https://new.example.org"))
    rid = client.get("/lb/admin/requests", headers=ADM).get_json()["requests"][0]["id"]
    j = client.post(f"/lb/admin/requests/{rid}/approve", headers=ADM, json={}).get_json()
    assert j["ok"] and j["siteKey"].startswith("sk_")
    did = j["domain"]["id"]
    assert client.patch(f"/lb/admin/domains/{did}", headers=ADM, json={"expiresInHours": 24, "rate": 50, "note": "hi"}).get_json()["domain"]["rate"] == 50
    assert client.patch(f"/lb/admin/domains/{did}", headers=ADM, json={"expiresInHours": "x"}).status_code == 400
    assert client.post("/lb/admin/domains", headers=ADM, json={"host": "bad host"}).status_code == 400
    assert client.post("/lb/admin/domains", headers=ADM, json={"host": "new.example.org"}).status_code == 409
    assert client.post("/lb/admin/bans", headers=ADM, json={"ip": "1.2.3.4", "hours": 1}).get_json()["ok"]
    assert client.post("/lb/admin/settings", headers=ADM, json={"rate": 99}).get_json()["settings"]["rate"] == 99
    assert client.post("/lb/admin/settings", headers=ADM, json={"evil": 1}).status_code == 400
    assert client.post("/lb/admin/settings", headers=ADM, json={"tgMode": "bogus"}).status_code == 400
    assert any(a["do"] == "domain.add" for a in client.get("/lb/admin/audit", headers=ADM).get_json()["log"])
    assert client.delete(f"/lb/admin/domains/{did}", headers=ADM).get_json()["ok"]


def test_ui_page_and_headers(client):
    r = client.get("/lb/ui")
    assert r.status_code == 200 and "script-src 'self'" in r.headers["Content-Security-Policy"] and r.headers["X-Frame-Options"] == "DENY"
    assert client.get("/lb/ui/app.js").status_code == 200 and client.get("/lb/ui/app.css").status_code == 200
    assert client.get("/lb/ui/../app.py").status_code in (403, 404)
    assert client.get("/lb/other", headers=hdr("https://x.example.org")).status_code == 403


def test_export_import_roundtrip_and_sanitizing(lb, client):
    lb.add_domain("a.example.org")
    lb.add_domain("b.example.org", keyless=True)
    lb.ban_ip_("1.1.1.1", 0, "x", "t")
    st = client.get("/lb/admin/export", headers=ADM).get_json()
    lb.DOMAINS.clear(); lb.BANNED.clear()
    s = lb.import_state(st, "replace", "t")
    assert set(lb.DOMAINS) == {"a.example.org", "b.example.org"} and "1.1.1.1" in lb.BANNED and s["domains"] == 2
    evil = {"domains": {"x.example.org": {"host": "x.example.org", "keyHash": "NOTHASH"}, "<script>": {"host": "<script>"}, "ok.example.org": {"host": "ok.example.org", "keyHash": "", "rate": "abc"}},
            "banned": {"nope": {}}, "settings": {"tgMode": "evil", "rate": 10**9, "domainCheck": 0}}
    lb.import_state(evil, "replace", "t")
    assert list(lb.DOMAINS) == ["ok.example.org"] and lb.DOMAINS["ok.example.org"]["rate"] == 0     # broken key hash never becomes an open domain
    assert not lb.BANNED and lb.SETTINGS["rate"] == 100000 and lb.SETTINGS["tgMode"] == "" or lb.SETTINGS["tgMode"] != "evil"
    lb.import_state({"domains": {"m.example.org": {"host": "m.example.org", "keyHash": ""}}}, "merge", "t")
    assert set(lb.DOMAINS) == {"ok.example.org", "m.example.org"}


def test_save_never_overwrites_store_before_it_was_read(lb, monkeypatch):
    calls = []
    monkeypatch.setattr(lb, "STATE_STORE_URL", "https://store.example")
    monkeypatch.setattr(lb, "INTERNAL_KEY", "k")
    monkeypatch.setattr(lb.requests, "put", lambda *a, **k: calls.append(a))
    lb.LOADED["ok"] = False
    lb.add_domain("a.example.org")
    assert calls == [] and lb.DIRTY["edited"]
    stored = {"state": {"domains": {"old.example.org": {"host": "old.example.org", "keyHash": ""}}}}

    class R:
        status_code = 200
        def json(self): return {"state": stored}
    monkeypatch.setattr(lb.requests, "get", lambda *a, **k: R())
    lb._load(initial=False)
    assert set(lb.DOMAINS) == {"a.example.org", "old.example.org"} and lb.LOADED["ok"] and calls       # merged, then pushed once
    lb.LOADED["ok"] = True


def test_flood_of_fake_origins_keeps_busy_ones(lb, client):
    for _ in range(5):
        client.get("/public/x", headers=hdr("https://real.example.org"))
    for i in range(260):
        client.get("/public/x", headers=hdr(f"https://f{i}.example.org", ip=f"10.0.{i // 250}.{i % 250}"))
    assert len(lb.BLOCKED) <= 201 and "https://real.example.org" in lb.BLOCKED


def test_notify_hook_only_for_new_unknown(lb, client):
    got = []
    lb.HOOKS["notify"] = lambda e: got.append(e["host"])
    for _ in range(3):
        client.get("/public/x", headers=hdr("https://n.example.org"))
    client.get("/public/x", headers=hdr("weird"))
    time.sleep(0.2)
    assert got == ["n.example.org"]
