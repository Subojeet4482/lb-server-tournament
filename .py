# LOAD BALANCER (Render). The user panel talks ONLY to this URL.
S1_URL=https://ff-server1-account.onrender.com
S2_URL=https://ff-server2-history.onrender.com
S3_URL=https://ff-server3-chat.onrender.com
S4_URL=https://ff-server4-backup.onrender.com
S5_URL=https://ff-server5-fraud.onrender.com

# same value as in the 5 servers
INTERNAL_KEY=
# admin password: /lb/admin/*, /fraud, /backup, /game/ball/admin.  Put the SAME value in ADMIN_API_KEY of S3, S4, S5
LB_ADMIN_KEY=

# ---------- DOMAIN VERIFICATION ----------
# Only domains added in the Telegram bot (with their site key) can use the API. Everything else is rejected.
DOMAIN_CHECK=true
# optional: domains allowed from the very first start (origin check only, no site key). Better: add them from the bot.
BOOTSTRAP_DOMAINS=
ALLOW_LOCALHOST=false
# proxies in front of this service: Render = 1, Cloudflare + Render = 2  (used to read the real client IP safely)
TRUSTED_PROXIES=1

# ---------- TELEGRAM CONTROL BOT ----------
# 1) @BotFather -> /newbot -> token      2) @userinfobot -> your numeric id (several admins: 111,222)
TG_BOT_TOKEN=
TG_ADMIN_IDS=

# ---- bot mode:  webhook  (FREE Render: Telegram wakes the service)   |   polling  (paid / always-on server) ----
TG_MODE=webhook
# webhook mode only: your https load-balancer URL. Registered automatically at every start (Render also provides RENDER_EXTERNAL_URL).
PUBLIC_URL=
# webhook mode only, OPTIONAL: if empty a secret is derived from the bot token automatically. If set: 8-256 chars of A-Z a-z 0-9 _ -
TG_WEBHOOK_SECRET=
# self ping so the service never sleeps. Empty = automatic: OFF in webhook mode (saves free hours), ON in polling mode.
KEEPALIVE=
KEEPALIVE_INTERVAL=600

# ---------- health check / failover ----------
HEALTH_INTERVAL=30
FAIL_THRESHOLD=2
HEALTH_TIMEOUT=50
CONNECT_TIMEOUT=10
REQUEST_TIMEOUT=60
RATE_LIMIT_PER_MIN=300
MAX_BODY_BYTES=1048576

# where domains + on/off choices are saved (use the Server-4 URL). Without it they are kept only in a local file that Render erases on restart!
STATE_STORE_URL=https://ff-server4-backup.onrender.com
THREADS=64
