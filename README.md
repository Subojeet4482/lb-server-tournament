# Load Balancer + Domain Verification + Telegram Bot

User panel -> **sirf is URL ko** request karta hai. Ye request ko sahi server pe bhejta hai.

## Domain verification (bypass-proof setup)
1. Sirf Telegram bot me add kiye hue **active domains** ki requests accept hoti hain. Baaki sab `403 domain_not_verified`.
2. **Koi site key nahi.** Domain ko bot me add karo (`/adddomain`) aur bas: browser `Origin` khud bhejta hai, sirf registered + active + `https` domain ki requests accept hoti hain, baaki sab reject.
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

## Telegram bot - all controls (v3)
`/start` opens the panel. Everything is also available as a command.

| Menu | What you can do | Command |
|---|---|---|
| 🖥 Servers | Auto / Force ON / OFF per server, **all servers at once**, maintenance per server / all, details + traffic per server, **Suspend / Resume / Restart / Deploy through Render** (needs `RENDER_API_KEY` + `S?_RENDER_ID`) | `/servers` |
| 🎛 Controls | **App maintenance** (whole app closed, custom message, players see a "We'll be right back" screen that re-opens by itself), **Read-only mode** (freeze every deposit / withdraw / join / game), **feature switches**: Deposit, Withdraw, Gift codes, Join matches, Games, Chat, Refer & Earn, Payment proofs, Leaderboard, Login. Custom message per feature | `/control` `/maint on <msg>` `/readonly on` `/feature withdraw off` |
| 💰 Wallet | Deposit / Withdraw / UPI / Gift card ON-OFF + notes, min deposit, min withdraw, UPI fee (same fields as the admin panel) | `/wallet` |
| 🤝 Referral | Refer & Earn ON/OFF, deposit needed, referrals per card, prize (fixed / random, min, max), credit to, window, limits | `/referral` |
| 🎮 Games | Coin Battle / Bomb & Gems ON-OFF, bots ON-OFF, live games | `/games` |
| 👤 Players | Find by Player ID / phone / e-mail / UID, balances, **ban (1 / 3 / 7 / 30 days or permanent) / unban** | `/find 1234567` `/ban 1234567 7` `/unban 1234567` |
| 🔒 Security | **Block / unblock IPs**, rate limit per IP, most rate-limited IPs (one tap to block), domain check, rejected domains | `/security` `/blockip 1.2.3.4` `/unblockip 1.2.3.4` `/ratelimit 300` |
| 📜 Activity log | Every server down / up and every admin action with the Telegram id of who did it | `/log` |
| 🔔 Alerts | Telegram message to all admins when a server goes DOWN or comes back UP, maintenance changes (Settings -> Alerts ON/OFF) | `/alerts on` |
| 🛡 Fraud · 🗺 State block · 💾 Backup · 🌐 Domains · 📊 Stats | as before (Stats now also shows traffic per server) | |

How it works: Controls / Security are enforced **inside the load balancer** (instant, no server or database change, saved in the state store so they survive restarts).
Wallet / Referral / Players use new admin endpoints on Server-1 (`/wallet/admin/switches`, `/ref/admin/config`, `/user/admin/*`), Games use the existing `/game/*/admin/config` on Server-3.
Not included on purpose: changing a player's wallet balance from Telegram (too risky without a second confirmation + audit trail) - use the admin panel for that.

## Adding your servers (easy way - no Render env editing)
After you host S1...S5 on Render you do **not** have to paste all five URLs into the load balancer's environment.
* In Render env of the load balancer keep only: `INTERNAL_KEY`, `LB_ADMIN_KEY`, `TG_BOT_TOKEN`, `TG_ADMIN_IDS`, **`S4_URL`** (the state store lives in Server-4) and `BOOTSTRAP_DOMAINS`.
* Then in Telegram: `/addserver` and paste the URLs - one or all five together:
  ```
  https://ff-s1.onrender.com
  https://ff-s2.onrender.com
  ...
  ```
  The bot asks every URL "who are you?" (`/health`) and saves it as the right server (S1/S2/S3/S4/S5) by itself, tells you if a URL is unreachable or if its `INTERNAL_KEY` differs. `/seturl s2 <url>` changes one, 🖥 Servers -> 🔗 Change URL does the same with buttons.
* The same is in the admin panel: **Servers & Control** tab -> "Add servers by URL" (set `LB_URL` + `LB_ADMIN_KEY` on the admin panel service).
* URLs set this way are saved in the state store (Server-4), so they survive restarts. If you also keep `S1_URL`... in env, the saved value wins.

Admin REST API (header `X-Admin-Key`, used by the admin panel): `GET /lb/admin/overview` · `POST /lb/admin/settings` · `POST /lb/admin/ip` · `POST /lb/admin/detect` · `POST /lb/admin/server-url` · `POST /lb/admin/domains` · `POST /lb/admin/servers/<s1..s5>` · `POST /lb/admin/check`.
