import os, sys, tempfile
os.environ.update(LB_ADMIN_KEY="adminkey123", INTERNAL_KEY="", STATE_FILE=os.path.join(tempfile.mkdtemp(), "st.json"), TRUSTED_PROXIES="1",
                  S1_URL="", S2_URL="", S3_URL="", S4_URL="", S5_URL="", STATE_STORE_URL="", PUBLIC_URL="https://lb.example.com")
os.environ.pop("TG_BOT_TOKEN", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest


@pytest.fixture()
def lb():
    import app as L
    for d in (L.DOMAINS, L.DENIED, L.BANNED, L.EXTRA_ADMINS, L.BLOCKED, L.RATE, L.ADMIN_FAILS):
        d.clear()
    L.AUDIT.clear()
    L.SETTINGS.update(domainCheck=True, notify=True, rate=0, maintenance=False, maintMsg="", autoBackupHours=0, autoBackupDb=False, lastBackupAt=0, tgMode="")
    L.HOOKS.update(notify=None, mode=None)
    for k in ("allowed", "blockedDomain", "blockedKey", "rateLimited", "rejected", "banned", "maintenance"):
        L.STATS[k] = 0
    return L


@pytest.fixture()
def client(lb):
    return lb.app.test_client()


def hdr(origin=None, key=None, ip="9.9.9.9"):
    h = {"X-Forwarded-For": ip}
    if origin:
        h["Origin"] = origin
    if key:
        h["X-Site-Key"] = key
    return h
