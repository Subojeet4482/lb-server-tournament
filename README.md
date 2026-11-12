# Load Balancer + Domain Verification + Telegram Bot

User panel -> **sirf is URL ko** request karta hai. Ye request ko sahi server pe bhejta hai.

## Domain verification (bypass-proof setup)
1. Sirf Telegram bot me add kiye hue **active domains** ki requests accept hoti hain. Baaki sab `403 domain_not_verified`.
2. Har domain ki ek **site key** hoti hai (`sk_...`). Frontend har request me `X-Site-Key: sk_...` bhejta hai; browser `Origin` khud bhejta hai. Origin + key dono match hone chahiye, https zaroori.
3. CORS sirf verified domain ko milta hai (kabhi `*` nahi).
4. Servers (S1-S5) ke public Render URL direct hit karne par `403 use_load_balancer` — server sirf LB ki secret key sunte hain.
5. `/internal`, `/lb`, `/tg` public se band. `/fraud`, `/backup`, `/game/ball/admin` ke liye admin key chahiye.
6. Real client IP right-side se liya jata hai (X-Forwarded-For spoof nahi hota).
> Limit: koi script (curl) copy kiya hua Origin + key bhej sakta hai. Isliye asli suraksha = 1 ghante ka login token + rate limit + Server-5 fraud check + server-side validation. Domain check bas browsers / dusre sites ko rokta hai.

## Telegram bot (inline buttons)
`/start` dabao. Menu:
* **📥 Domain requests** — jo websites tumhari API use karne aayi par approved nahi thi (top list, 10 per page: 🆕 newest / 🔥 most active / 📋 log).
  Har ek ko **✅ Approve** (site key milti hai) / **✅ Origin only** / **❌ Reject** (hamesha ke liye silent block, dobara list me nahi aata) / **❌ Reject + ban IP** / 🚷 Ban IP. "Reject all" bhi hai. Rejected list se wapas allow kar sakte ho.
  Naya domain aate hi **alert** milta hai (Approve/Reject buttons ke saath; max 12/ghanta, spam se bachne ke liye).
* **🌐 Domains** — enable/disable, ♻️ new key, 🔓 key off (origin only), **expiry** (24h / 7d / 30d / never), **per-domain rate limit**, note, remove, request count.
* **🖥 Servers** — Auto / Force ON / OFF, maintenance, health, traffic route, "All → AUTO".
* **🛡 Fraud cases**, **🗺 State block**, **📊 Stats** (top domains, blocked / rejected / banned counters).
* **🚷 Banned IPs** — ban (permanent ya N ghante), unban. `/banip 1.2.3.4 24`.
* **📜 Activity log** — kisne kab kya badla (bot + web dono ka).
* **👥 Admins** — `TG_ADMIN_IDS` = owners; owner bot se extra admin add/remove kar sakta hai (`/addadmin <id>`).
* **💾 Backup** — DB backup chalao aur file **seedha Telegram me** lo (Render disk restart pe saaf hota hai), purane backups ki list se file lo,
  **config export** (domains + key hashes + rejected + bans + settings ek .json) aur **config restore** (file bhejo -> Replace ya Merge). **Auto backup** har 6/12/24/72/168 ghante (Settings).
* **⚙️ Settings** — domain check, alerts, rate limit, **maintenance mode** (+ message), auto backup, **bot mode (webhook <-> polling) runtime me switch**, bot status.
Commands: `/help` me poori list.
Setup: @BotFather se token, @userinfobot se apni ID -> `TG_BOT_TOKEN`, `TG_ADMIN_IDS`. Pehla domain: bot me `/adddomain app.example.com`.

## Web control panel
`https://<tumhara-lb>/lb/ui` — login = `LB_ADMIN_KEY` (key sirf browser tab me rehti hai). Bot ke saare kaam: requests approve/reject, domains, servers, IP bans, backup/restore (config download/upload, DB backup download), activity log, settings.
10 galat key -> us IP par 10 min lock. Page strict CSP ke saath, data kabhi HTML ki tarah insert nahi hota.
JSON API: `/lb/admin/*` (header `X-Admin-Key`).

## Notes
* State (domains, rejected, bans, admins, settings, log) Server-4 (`STATE_STORE_URL`) me save hoti hai. Agar LB restart ke time Server-4 so raha ho, to jab tak wo padhi nahi jati tab tak LB usse **overwrite nahi karta** — baad me merge karke save karta hai.
* Per-domain request counters restart par 0 nahi hote jab tak state save ho (save har change par hota hai); global Stats restart par reset hoti hain.
* `BOOTSTRAP_DOMAINS` wale domain restart par wapas add ho jate hain — unhe hamesha ke liye hatana ho to env se bhi hatao.
* Tests: `pip install pytest && python -m pytest tests -q`.

### Bot mode (`TG_MODE`)
| | `webhook` (free Render) | `polling` (paid / VPS) |
|---|---|---|
| Kaise | Telegram message `PUBLIC_URL/tg/webhook/<secret>` par bhejta hai | Bot Telegram se `getUpdates` mangta hai |
| Free hours | LB soya ho to message se jaag jata hai (pehla button 30-60 sec) | LB hamesha jaga rehta hai (keep-alive ON) |

Mode **bot ke andar se bhi** badal sakte ho: Settings -> Bot mode (restart nahi chahiye, env `TG_MODE` default rehta hai).
| Env | `PUBLIC_URL` (https). Secret apne-aap (ya `TG_WEBHOOK_SECRET`) | kuch extra nahi |
* Webhook mode: har start par webhook **apne-aap register** hota hai, har 30 min check hota hai (kisi ne badla to wapas set), header me secret token verify hota hai, same `update_id` dobara process nahi hota.
* Dono mode kabhi ek saath active nahi: webhook me `getUpdates` nahi chalta; polling me pehle `deleteWebhook` hota hai aur `/tg/webhook` 404 deta hai.
* Bot me Settings -> **Bot status** se mode, webhook URL (secret chhupa hua), pending updates, Telegram ki last error dikhti hai.
* Polling me ek token par ek hi LB copy chalao (warna 409 conflict).

## Routing
| Path | Server | Band ho to |
|---|---|---|
| /auth /user /wallet /matches /leaderboard /public /ref | Server-1 | Server-4 |
| /history | Server-2 | Server-4 |
| /chat /game | Server-3 | Server-4 |
| /fraud /backup /game/ball/admin (admin key) | Server-5 / Server-4 / Server-3 | Server-4 |

Health check har `HEALTH_INTERVAL` sec. Server down -> traffic apne-aap Server-4 par, wapas aane par wapas.
Admin HTTP API (header `X-Admin-Key`): `GET /lb/admin/servers`, `POST /lb/admin/servers/s1 {"mode":"auto|on|off"}`, `POST /lb/admin/check`.
