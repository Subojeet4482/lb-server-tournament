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
`/start` dabao: **State payment block** (state list, switches, message, user check), **Servers** (Auto / Force ON / OFF / Maintenance, health, traffic route), **Domains** (add, new site key, disable, remove), **Fraud cases** (unban / confirm), **Blocked requests** (jo domain reject hue, ek tap me allow), **Backup**, **Stats**, **Settings** (domain check on/off).
Setup: @BotFather se token, @userinfobot se apni ID -> `TG_BOT_TOKEN`, `TG_ADMIN_IDS`. Pehla domain: bot me `/adddomain app.example.com`.

### Bot mode (`TG_MODE`)
| | `webhook` (free Render) | `polling` (paid / VPS) |
|---|---|---|
| Kaise | Telegram message `PUBLIC_URL/tg/webhook/<secret>` par bhejta hai | Bot Telegram se `getUpdates` mangta hai |
| Free hours | LB soya ho to message se jaag jata hai (pehla button 30-60 sec) | LB hamesha jaga rehta hai (keep-alive ON) |
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
