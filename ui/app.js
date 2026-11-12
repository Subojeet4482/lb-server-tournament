"use strict";
/* LB control panel. All data from the API is inserted with textContent (never innerHTML): domain names / origins come from strangers. */
const root = document.getElementById("app");
let KEY = "";
try { KEY = sessionStorage.getItem("lbk") || ""; } catch (e) {}
let timer = null, current = "overview";

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat()) if (c != null && c !== false) el.append(c.nodeType ? c : document.createTextNode(String(c)));
  return el;
}
const $ = (el, kids) => { el.replaceChildren(...[kids].flat(Infinity).filter(c => c != null && c !== false).map(c => c.nodeType ? c : document.createTextNode(String(c)))); return el; };

async function api(path, method = "GET", body) {
  const r = await fetch("/lb/admin/" + path, { method, headers: { "X-Admin-Key": KEY, "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  if (r.status === 403 && path !== "overview") { logout(); throw new Error("forbidden"); }
  const j = await r.json().catch(() => ({}));
  if (!r.ok || j.ok === false) throw new Error(j.error || "HTTP " + r.status);
  return j;
}
function toast(msg) { const t = h("div", { class: "toast" }, msg); document.body.append(t); setTimeout(() => t.remove(), 2600); }
async function act(fn, ok) { try { const r = await fn(); if (ok) toast(ok); return r; } catch (e) { toast("⚠ " + e.message); } }
const ago = ms => { if (!ms) return "never"; const s = Math.max(0, (Date.now() - ms) / 1000 | 0); return s < 90 ? s + "s ago" : s < 5400 ? (s / 60 | 0) + " min ago" : s < 172800 ? (s / 3600 | 0) + " h ago" : (s / 86400 | 0) + " d ago"; };
const when = ms => ms ? new Date(ms).toLocaleString() : "-";
const num = n => (n || 0).toLocaleString();

function modal(title, ...body) {
  const m = h("div", { class: "modal", onclick: e => { if (e.target === m) m.remove(); } }, h("div", { class: "card" }, h("h2", {}, title), ...body, h("div", { class: "row", style: "margin-top:12px" }, h("button", { class: "btn", onclick: () => m.remove() }, "Close"))));
  document.body.append(m); return m;
}
function showKey(title, host, key) {
  modal(title, h("p", {}, host), key ? [h("p", { class: "muted" }, "Site key — shown only once. Put it in the frontend as header X-Site-Key."), h("div", { class: "keybox mono" }, key),
    h("button", { class: "btn pri", onclick: () => { navigator.clipboard?.writeText(key); toast("Copied"); } }, "Copy key")] : h("p", { class: "muted" }, "Origin-only access (no site key)."));
}
function confirmBox(text, fn) {
  const m = modal("Are you sure?", h("p", {}, text)); m.querySelector(".row").prepend(h("button", { class: "btn bad", onclick: () => { m.remove(); fn(); } }, "Yes, do it"));
}

// ------------------------------------------------------------------ shell
const PAGES = [["overview", "Overview"], ["requests", "Domain requests"], ["domains", "Domains"], ["servers", "Servers"], ["bans", "Banned IPs"], ["backup", "Backup"], ["activity", "Activity"], ["settings", "Settings"]];
let badge = 0;
function logout() { KEY = ""; try { sessionStorage.removeItem("lbk"); } catch (e) {} clearInterval(timer); loginView(); }
function loginView(err) {
  const inp = h("input", { type: "password", placeholder: "LB_ADMIN_KEY", autocomplete: "current-password", style: "width:100%" });
  const msg = h("div", { class: "err" }, err || "");
  const go = async () => { KEY = inp.value.trim(); try { await api("overview"); try { sessionStorage.setItem("lbk", KEY); } catch (e) {} start(); } catch (e) { KEY = ""; msg.textContent = e.message === "forbidden" ? "Wrong key." : e.message; } };
  inp.addEventListener("keydown", e => e.key === "Enter" && go());
  $(root, h("div", { class: "login" }, h("div", { class: "card" }, h("h2", {}, "LB Control Panel"), h("p", { class: "sub" }, "Enter the admin key to continue."), inp, msg, h("button", { class: "btn pri", onclick: go }, "Sign in"))));
  inp.focus();
}
function shell() {
  const nav = h("nav", {}, h("h1", {}, "🛡 LB Control"), ...PAGES.map(([id, label]) => h("a", { class: id === current ? "on" : "", onclick: () => go(id) }, label, id === "requests" && badge ? h("span", { class: "badge" }, badge) : null)),
    h("div", { class: "sp" }), h("a", { onclick: logout }, "Sign out"));
  const main = h("main", {});
  $(root, h("div", { class: "shell" }, nav, main));
  return main;
}
async function go(id) { current = id; location.hash = id; await render(); }
async function render() {
  const ov = await api("overview").catch(() => null);
  if (!ov) return;
  badge = ov.counts.pending;
  const main = shell();
  try { await VIEWS[current](main, ov); } catch (e) { $(main, h("div", { class: "empty" }, "⚠ " + e.message)); }
}
function start() { current = (location.hash || "#overview").slice(1); if (!VIEWS[current]) current = "overview"; render(); clearInterval(timer); timer = setInterval(() => { if (!document.querySelector(".modal") && !document.activeElement?.matches("input,textarea,select")) render(); }, 15000); }
const head = (t, s) => [h("h2", {}, t), h("p", { class: "sub" }, s)];
const chip = (t, c) => h("span", { class: "chip " + (c || "") }, t);

// ------------------------------------------------------------------ views
const VIEWS = {
  async overview(m, ov) {
    const s = ov.stats, c = ov.counts, up = Object.values(ov.servers).filter(x => x.available).length;
    const stat = (l, v) => h("div", { class: "card stat" }, h("span", {}, l), h("b", {}, v));
    $(m, [head("Overview", "Everything at a glance. Refreshes every 15 s."),
      ov.settings.maintenance ? h("div", { class: "banner" }, "🛠 Maintenance mode is ON — users get 503.") : null,
      !ov.settings.domainCheck ? h("div", { class: "banner" }, "🔓 Domain check is OFF — any website can call your API.") : null,
      !ov.synced ? h("div", { class: "banner" }, "State store (Server-4) not reached yet — changes are kept locally and will merge when it wakes up.") : null,
      h("div", { class: "grid" }, stat("Servers online", up + " / 5"), stat("Verified domains", c.domains), stat("Pending requests", c.pending), stat("Banned IPs", c.banned), stat("Rejected domains", c.rejected)),
      h("div", { class: "grid" }, stat("Allowed requests", num(s.allowed)), stat("Blocked (domain)", num(s.blockedDomain)), stat("Wrong site key", num(s.blockedKey)), stat("Rate limited", num(s.rateLimited)), stat("Rejected hits", num(s.rejected))),
      h("div", { class: "card" }, h("b", {}, "Telegram bot"), h("p", { class: "muted" }, ov.bot.enabled ? `Mode ${ov.bot.mode.toUpperCase()} · ${ov.bot.admins} admin(s) · ${num(ov.bot.updates)} updates handled` + (ov.bot.mode === "polling" ? " · last poll " + ago(ov.bot.lastPoll) : "") : "TG_BOT_TOKEN not set — bot disabled."))]);
  },

  async requests(m) {
    const mode = VIEWS._rm || "n";
    const [{ requests: list }, { rejected }] = await Promise.all([api("requests?mode=" + mode), api("rejected")]);
    const tab = (k, l) => h("button", { class: "btn" + (mode === k ? " on" : ""), onclick: () => { VIEWS._rm = k; render(); } }, l);
    const rows = list.map(e => {
      const actionable = mode !== "a" || e.reason === "unknown_domain";
      return h("tr", {}, h("td", {}, h("code", {}, e.host || e.origin), h("div", { class: "muted" }, e.path)), h("td", {}, num(e.count)), h("td", {}, chip(e.reason.replace("_", " "), e.reason === "unknown_domain" ? "warn" : "")),
        h("td", { class: "mono" }, (e.ips || [e.ip]).join(", ")), h("td", {}, ago(e.last)),
        h("td", {}, h("div", { class: "row" }, actionable && e.reason === "unknown_domain" ? [
          h("button", { class: "btn sm ok", onclick: async () => { const r = await act(() => api(`requests/${e.id}/approve`, "POST", {}), "Approved"); if (r) { showKey("Domain approved", r.domain.host, r.siteKey); render(); } } }, "Approve"),
          h("button", { class: "btn sm", title: "Origin check only, no site key", onclick: async () => { const r = await act(() => api(`requests/${e.id}/approve`, "POST", { keyless: true }), "Approved"); if (r) { showKey("Approved (origin only)", r.domain.host, ""); render(); } } }, "Origin only"),
          h("button", { class: "btn sm bad", onclick: async () => { await act(() => api(`requests/${e.id}/reject`, "POST", {}), "Rejected"); render(); } }, "Reject"),
          h("button", { class: "btn sm bad", onclick: () => confirmBox("Reject the domain and ban its IP(s)?", async () => { await act(() => api(`requests/${e.id}/reject`, "POST", { banIp: true }), "Rejected + banned"); render(); }) }, "Reject + ban")] : null)));
    });
    $(m, [head("Domain requests", "Websites that tried to use your API but are not approved. Approve → they get a site key. Reject → they are blocked silently and never shown again."),
      h("div", { class: "tabs" }, tab("n", "Newest"), tab("t", "Most active"), tab("a", "All refused (log)"), h("span", { style: "flex:1" }),
        h("button", { class: "btn", onclick: () => confirmBox("Clear the request log?", async () => { await act(() => api("requests/clear", "POST", {})); render(); }) }, "Clear log")),
      list.length ? h("div", { class: "card tbl" }, h("table", { class: "table" }, h("thead", {}, h("tr", {}, ["Domain", "Requests", "Reason", "IP", "Last", ""].map(x => h("th", {}, x)))), h("tbody", {}, rows))) : h("div", { class: "card empty" }, "✅ Nothing here."),
      h("h2", { style: "margin-top:22px;font-size:16px" }, `Rejected domains (${rejected.length})`),
      rejected.length ? h("div", { class: "card tbl" }, h("table", { class: "table" }, h("tbody", {}, rejected.map(d => h("tr", {}, h("td", {}, h("code", {}, d.host)), h("td", {}, num(d.count) + " hits"), h("td", {}, ago(d.last || d.at)),
        h("td", {}, h("button", { class: "btn sm", onclick: async () => { await act(() => api("rejected/undo", "POST", { host: d.host }), "Allowed again"); render(); } }, "Allow again"))))))) : h("div", { class: "card empty" }, "None.")]);
  },

  async domains(m) {
    const { domains } = await api("domains");
    const host = h("input", { type: "text", placeholder: "app.example.com", style: "min-width:220px" });
    const noKey = h("input", { type: "checkbox" });
    const add = async () => { const r = await act(() => api("domains", "POST", { host: host.value, keyless: noKey.checked }), "Domain added"); if (r) { showKey("Domain added", r.domain.host, r.siteKey); render(); } };
    const exp = d => d.expiresAt ? (d.expiresAt < Date.now() ? chip("expired", "bad") : chip("until " + new Date(d.expiresAt).toLocaleDateString(), "warn")) : chip("never");
    const rows = domains.map(d => h("tr", {}, h("td", {}, h("code", {}, d.host), h("div", { class: "muted" }, d.note || "")), h("td", {}, d.live ? chip("active", "ok") : chip(d.active ? "expired" : "disabled", "bad")),
      h("td", {}, d.hasKey ? chip("🔑 key") : chip("origin only", "warn")), h("td", {}, exp(d)), h("td", {}, d.rate ? d.rate + "/min" : "default"), h("td", {}, num(d.hits), h("div", { class: "muted" }, ago(d.lastUsed))),
      h("td", {}, h("div", { class: "row" },
        h("button", { class: "btn sm", onclick: async () => { await act(() => api("domains/" + d.id, "PATCH", { active: !d.active })); render(); } }, d.active ? "Disable" : "Enable"),
        h("button", { class: "btn sm", onclick: async () => { const r = await act(() => api("domains/" + d.id, "PATCH", { rotateKey: true }), "New key"); if (r) showKey("New site key", d.host, r.siteKey); render(); } }, d.hasKey ? "New key" : "Add key"),
        h("select", { onchange: async e => { await act(() => api("domains/" + d.id, "PATCH", { expiresInHours: +e.target.value }), "Expiry set"); render(); } }, h("option", { value: "" , disabled: true, selected: true }, "Expiry…"), ...[["0", "Never"], ["24", "24 hours"], ["168", "7 days"], ["720", "30 days"]].map(([v, l]) => h("option", { value: v }, l))),
        h("select", { onchange: async e => { await act(() => api("domains/" + d.id, "PATCH", { rate: +e.target.value }), "Rate set"); render(); } }, h("option", { value: "", disabled: true, selected: true }, "Rate…"), ...[["0", "Default"], ["60", "60/min"], ["120", "120/min"], ["300", "300/min"], ["600", "600/min"]].map(([v, l]) => h("option", { value: v }, l))),
        h("button", { class: "btn sm", onclick: async () => { const n = prompt("Note for " + d.host, d.note || ""); if (n !== null) { await act(() => api("domains/" + d.id, "PATCH", { note: n })); render(); } } }, "Note"),
        h("button", { class: "btn sm bad", onclick: () => confirmBox(`Remove ${d.host}? Its requests will be refused.`, async () => { await act(() => api("domains/" + d.id, "DELETE"), "Removed"); render(); }) }, "Remove")))));
    $(m, [head("Domains", "Only these websites can use the API."),
      h("div", { class: "card row", style: "margin-bottom:14px" }, host, h("label", { class: "row" }, noKey, "origin only (no site key)"), h("button", { class: "btn pri", onclick: add }, "Add domain")),
      domains.length ? h("div", { class: "card tbl" }, h("table", { class: "table" }, h("thead", {}, h("tr", {}, ["Domain", "Status", "Access", "Expires", "Rate", "Requests", ""].map(x => h("th", {}, x)))), h("tbody", {}, rows))) : h("div", { class: "card empty" }, "No domains yet — every request is refused until you add one.")]);
  },

  async servers(m, ov) {
    const rows = Object.entries(ov.servers).map(([id, s]) => h("tr", {}, h("td", {}, h("b", {}, id.toUpperCase()), " ", s.name, h("div", { class: "muted mono" }, s.url.replace(/^https?:\/\//, "") || "URL not set")),
      h("td", {}, s.mode === "off" ? chip("off", "bad") : s.up === null ? chip("not checked") : s.up ? chip("up", "ok") : chip("down", "bad"), s.maintenance ? " " : "", s.maintenance ? chip("maintenance", "warn") : ""),
      h("td", {}, s.latencyMs != null ? s.latencyMs + " ms" : "-", h("div", { class: "muted" }, ago(s.lastCheck))),
      h("td", {}, h("div", { class: "row" }, ["auto", "on", "off"].map(md => h("button", { class: "btn sm" + (s.mode === md ? " pri" : ""), onclick: async () => { await act(() => api("servers/" + id, "POST", { mode: md }), id.toUpperCase() + " → " + md); setTimeout(render, 600); } }, md)),
        h("button", { class: "btn sm", onclick: async () => { await act(() => api("servers/" + id, "POST", { maintenance: !s.maintenance }), "Maintenance toggled"); setTimeout(render, 600); } }, "Maint. " + (s.maintenance ? "off" : "on"))))));
    const route = Object.entries(ov.routes).map(([g, to]) => h("tr", {}, h("td", {}, g.toUpperCase()), h("td", {}, to ? [to.toUpperCase(), to !== g ? chip(" backup in use", "warn") : null] : chip("no server available", "bad"))));
    $(m, [head("Servers", "AUTO = health check decides · ON = always use · OFF = never (Server-4 takes over)."),
      h("div", { class: "row", style: "margin-bottom:12px" }, h("button", { class: "btn", onclick: async () => { await act(() => api("check", "POST", {}), "Checked"); render(); } }, "Check now"), h("button", { class: "btn", onclick: async () => { await act(() => api("servers-all", "POST", {}), "All → auto"); render(); } }, "All → AUTO")),
      h("div", { class: "card tbl" }, h("table", { class: "table" }, h("thead", {}, h("tr", {}, ["Server", "Health", "Latency", "Control"].map(x => h("th", {}, x)))), h("tbody", {}, rows))),
      h("h2", { style: "margin-top:22px;font-size:16px" }, "Traffic route"), h("div", { class: "card tbl" }, h("table", { class: "table" }, h("tbody", {}, route)))]);
  },

  async bans(m) {
    const { bans } = await api("bans");
    const ip = h("input", { type: "text", placeholder: "1.2.3.4 or IPv6" }), hrs = h("input", { type: "number", min: "0", placeholder: "hours (0 = permanent)", style: "width:190px" });
    const add = async () => { await act(() => api("bans", "POST", { ip: ip.value, hours: +hrs.value || 0 }), "IP banned"); render(); };
    $(m, [head("Banned IPs", "A banned IP gets a silent 403 on every API call."), h("div", { class: "card row", style: "margin-bottom:14px" }, ip, hrs, h("button", { class: "btn pri", onclick: add }, "Ban IP")),
      bans.length ? h("div", { class: "card tbl" }, h("table", { class: "table" }, h("tbody", {}, bans.map(b => h("tr", {}, h("td", { class: "mono" }, b.ip), h("td", {}, b.until ? "until " + when(b.until) : "permanent"), h("td", { class: "muted" }, b.note || "", " · ", b.by), h("td", {},
        h("button", { class: "btn sm", onclick: async () => { await act(() => api("bans/remove", "POST", { ip: b.ip }), "Unbanned"); render(); } }, "Unban"))))))) : h("div", { class: "card empty" }, "No banned IPs.")]);
  },

  async backup(m) {
    let files = [], err = "";
    try { files = (await api("backup/list")).files; } catch (e) { err = e.message; }
    const file = h("input", { type: "file", accept: "application/json,.json" });
    const preview = h("div", {});
    let parsed = null;
    file.addEventListener("change", async () => {
      try { parsed = JSON.parse(await file.files[0].text()); const r = await api("import", "POST", { state: parsed, preview: true }); const s = r.summary;
        $(preview, [h("p", {}, `File has ${s.domains} domains, ${s.denied} rejected, ${s.banned} IP bans, ${s.admins} extra admins.`), h("div", { class: "row" },
          h("button", { class: "btn bad", onclick: () => confirmBox("Replace EVERYTHING now with the file?", async () => { await act(() => api("import", "POST", { state: parsed, mode: "replace" }), "Restored"); render(); }) }, "Replace all"),
          h("button", { class: "btn", onclick: async () => { await act(() => api("import", "POST", { state: parsed, mode: "merge" }), "Merged"); render(); } }, "Merge"))]);
      } catch (e) { $(preview, h("p", { class: "err" }, "Not a valid config backup. " + e.message)); }
    });
    const dl = async name => { const r = await fetch("/lb/admin/backup/download/" + name, { headers: { "X-Admin-Key": KEY } }); if (!r.ok) return toast("Download failed"); const a = h("a", { href: URL.createObjectURL(await r.blob()), download: name }); document.body.append(a); a.click(); a.remove(); };
    const dlCfg = async () => { const r = await fetch("/lb/admin/export", { headers: { "X-Admin-Key": KEY } }); const a = h("a", { href: URL.createObjectURL(await r.blob()), download: "lb-config.json" }); document.body.append(a); a.click(); a.remove(); };
    $(m, [head("Backup", "Config backup = domains, key hashes, rejected, IP bans, settings. Database backup = Server-4 JSON export."),
      h("div", { class: "card", style: "margin-bottom:14px" }, h("b", {}, "⚙️ Config backup"), h("div", { class: "row", style: "margin:10px 0" }, h("button", { class: "btn pri", onclick: dlCfg }, "Download config"),
        h("button", { class: "btn", onclick: async () => { await act(() => api("backup/telegram", "POST", {}), "Sent to Telegram admins"); } }, "Send to Telegram")), h("div", { class: "field" }, h("label", {}, "Restore from file"), file), preview),
      h("div", { class: "card" }, h("b", {}, "🗄 Database backup (Server-4)"), h("div", { class: "row", style: "margin:10px 0" }, h("button", { class: "btn pri", onclick: async () => { await act(() => api("backup/run", "POST", {}), "Backup created"); render(); } }, "Run backup now")),
        err ? h("p", { class: "err" }, err) : files.length ? h("table", { class: "table" }, h("tbody", {}, files.slice().reverse().map(f => h("tr", {}, h("td", { class: "mono" }, f.name), h("td", {}, Math.round(f.bytes / 1024) + " KB"), h("td", {}, h("button", { class: "btn sm", onclick: () => dl(f.name) }, "Download")))))) : h("p", { class: "muted" }, "No backups on the server yet."))]);
  },

  async activity(m) {
    const { log } = await api("audit");
    $(m, [head("Activity", "Who changed what (latest 150)."), log.length ? h("div", { class: "card tbl" }, h("table", { class: "table" }, h("tbody", {}, log.map(a => h("tr", {}, h("td", { class: "muted", style: "white-space:nowrap" }, when(a.t)), h("td", {}, h("b", {}, a.do)), h("td", {}, a.d), h("td", { class: "muted" }, a.by)))))) : h("div", { class: "card empty" }, "Nothing yet.")]);
  },

  async settings(m, ov) {
    const s = ov.settings;
    const set = async (k, v) => { await act(() => api("settings", "POST", { [k]: v }), "Saved"); render(); };
    const tg = (k, t, d) => h("div", { class: "toggle" }, h("div", {}, h("b", {}, t), h("small", {}, d)), h("button", { class: "sw" + (s[k] ? " on" : ""), "aria-label": t, onclick: () => set(k, !s[k]) }));
    const sel = (k, opts, cur) => h("select", { onchange: e => set(k, e.target.value === "" || isNaN(+e.target.value) ? e.target.value : +e.target.value) }, opts.map(([v, l]) => h("option", { value: v, selected: String(v) === String(cur) }, l)));
    const msg = h("input", { type: "text", value: s.maintMsg, placeholder: "default message", style: "width:100%", maxlength: "200" });
    $(m, [head("Settings", "Changes apply immediately."), h("div", { class: "card" },
      tg("domainCheck", "Domain verification", "ON = only approved domains can use the API. OFF is an emergency mode."),
      tg("notify", "New-request alerts (Telegram)", "Message with Approve / Reject buttons when a new domain knocks (max 12 per hour)."),
      tg("maintenance", "Maintenance mode", "All users get 503 with your message; requests with the admin key still work."),
      h("div", { class: "toggle" }, h("div", { style: "flex:1" }, h("b", {}, "Maintenance message"), msg), h("button", { class: "btn", onclick: () => set("maintMsg", msg.value) }, "Save")),
      h("div", { class: "toggle" }, h("div", {}, h("b", {}, "Rate limit"), h("small", {}, "Requests per minute per IP (0 = env default).")), sel("rate", [[0, "Default"], [60, "60"], [120, "120"], [300, "300"], [600, "600"], [1200, "1200"]], s.rate)),
      h("div", { class: "toggle" }, h("div", {}, h("b", {}, "Auto backup to Telegram"), h("small", {}, "Config file is sent to all bot admins (only runs while the service is awake).")), sel("autoBackupHours", [[0, "Off"], [6, "Every 6 h"], [12, "Every 12 h"], [24, "Daily"], [72, "Every 3 days"], [168, "Weekly"]], s.autoBackupHours)),
      tg("autoBackupDb", "Include database backup in auto backup", "Also runs a Server-4 backup and sends the file (max 50 MB)."),
      h("div", { class: "toggle" }, h("div", {}, h("b", {}, "Telegram bot mode"), h("small", {}, `Now: ${ov.bot.mode.toUpperCase()}. Polling needs an always-awake service.`)), sel("tgMode", [["", "Env default (" + ov.bot.envMode + ")"], ["webhook", "Webhook"], ["polling", "Polling"]], s.tgMode)))]);
  },
};
VIEWS._rm = "n";
window.addEventListener("hashchange", () => { const id = location.hash.slice(1); if (KEY && VIEWS[id] && id !== current) { current = id; render(); } });

if (KEY) api("overview").then(start).catch(() => loginView()); else loginView();
