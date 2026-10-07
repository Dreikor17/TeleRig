/* TeleRig — session layer, loaded before every other script (spec §3.15, §3.20, §5.5, §2.11).
 *
 * window.TR:
 *   boot / user / role / perms / can(perm) / status       the bootstrap (#tr-boot) + hello refreshes
 *   initialRadio                                          the radio the first paint uses (AR-44)
 *   api(url, {method, json})                              every fetch: JSON in and out, auth redirects
 *   toast(msg, kind) / srLive(msg) / confirm(text, opts)  feedback; confirm is inline (never blocks)
 *   handleClose(code, reason, reconnect) / linkUp()       WebSocket close codes, onAuthLost, link banner
 *   myConn / tx / isTxOwner()                             this tab's connection id + the TX owner
 *   _inject(frame)                                        sim-only: feed a synthetic server frame
 *   fallbackLayout()                                      the stacked-tile fallback (B3 tiles)
 * window.RadioLayout: a no-op stub that layout.js (B3) replaces.
 *
 * The server is the authority for everything here: roles, TX ownership and the radio. This file
 * only decides what to show and where to go when the session ends.
 */
(function () {
  "use strict";

  // Renamed from Radio WebOp: carry browser settings saved under the old "radiowebop." keys
  // across once, so the theme, band-plan toggle and per-radio connections survive the rename.
  try {
    for (const k of ["theme", "bandplan", "conns", "conn"]) {
      const nk = "telerig." + k, ok = "radiowebop." + k;
      if (localStorage.getItem(nk) === null && localStorage.getItem(ok) !== null)
        localStorage.setItem(nk, localStorage.getItem(ok));
    }
  } catch (_) {}

  const $ = (id) => document.getElementById(id);
  const ROLES = ["viewer", "operator", "admin"];

  let boot = {};
  try {
    const el = $("tr-boot");
    if (el) boot = JSON.parse(el.textContent || "{}") || {};
  } catch (_) { boot = {}; }
  if (!boot || typeof boot !== "object") boot = {};

  const TR = window.TR = {
    boot,
    user: boot.user || null,
    role: ROLES.includes(boot.role) ? boot.role : "viewer",
    perms: new Set(Array.isArray(boot.perms) ? boot.perms : []),
    status: (boot.status && typeof boot.status === "object") ? boot.status : {},
    myConn: null,
    tx: { owner: null },
  };

  TR.can = (perm) => TR.perms.has(perm);
  TR.isTxOwner = () => !!(TR.tx && TR.tx.owner && TR.myConn != null && TR.tx.owner.conn === TR.myConn);

  // Role/perms from the server (bootstrap, then hello). The CSS role channel (§4.6) reads
  // body[data-role]; only the three known roles are ever written there (fail closed).
  TR.setRole = function (role, perms) {
    if (ROLES.includes(role)) TR.role = role;
    if (Array.isArray(perms)) TR.perms = new Set(perms);
    if (document.body) document.body.dataset.role = TR.role;
  };

  // -- the first radio to paint (AR-44): connected radio > this browser's last > first profile --
  TR.initialRadio = (function () {
    const list = Array.isArray(boot.radios) ? boot.radios : [];
    const known = (id) => !!id && list.some((r) => r && r.id === id);
    const st = TR.status;
    if (st.connected && known(st.radio)) return st.radio;
    try {
      const c = JSON.parse(localStorage.getItem("telerig.conns") || "null");
      if (c && known(c.last)) return c.last;
    } catch (_) {}
    return list.length ? list[0].id : null;
  })();

  // -- RadioLayout stub (§3.20): layout.js (B3) replaces it; everything stays usable without it --
  if (!window.RadioLayout) {
    window.RadioLayout = {
      _stub: true,
      arranging: () => false,
      setRadio() {}, showTile() {}, hideTile() {}, setScopeSplit() {},
      isFallback: () => true,
    };
  }

  // -- feedback: toasts, screen-reader live region, inline confirm ---------------------------------
  function ensureEl(id, make) {
    let el = $(id);
    if (!el && document.body) { el = make(); document.body.appendChild(el); }
    return el;
  }
  const recentToasts = new Map();          // text -> time: the same message at most every 3 s
  TR.toast = function (msg, kind, opts) {
    msg = String(msg || "");
    if (!msg) return null;
    kind = ["info", "warn", "error", "ok"].includes(kind) ? kind : "info";
    opts = opts || {};
    const now = Date.now();
    if (!opts.force && now - (recentToasts.get(msg) || 0) < 3000) return null;
    recentToasts.set(msg, now);
    const box = ensureEl("toasts", () => {
      const d = document.createElement("div");
      d.id = "toasts"; d.className = "toasts"; d.setAttribute("aria-live", "polite");
      return d;
    });
    if (!box) return null;
    const t = document.createElement("div");
    t.className = "toast toast-" + kind;
    t.setAttribute("role", kind === "error" ? "alert" : "status");
    const span = document.createElement("span");
    span.textContent = msg;
    t.appendChild(span);
    if (opts.action && typeof opts.action.fn === "function") {
      const b = document.createElement("button");
      b.type = "button"; b.className = "toast-act"; b.textContent = opts.action.label || "OK";
      b.addEventListener("click", () => { try { opts.action.fn(); } finally { t.remove(); } });
      t.appendChild(b);
    }
    box.appendChild(t);
    setTimeout(() => t.remove(), opts.ms || 4000);
    return t;
  };

  TR.srLive = function (msg) {
    const el = ensureEl("srLive", () => {
      const d = document.createElement("div");
      d.id = "srLive"; d.className = "sr-only"; d.setAttribute("aria-live", "assertive");
      return d;
    });
    if (!el) return;
    el.textContent = "";
    requestAnimationFrame(() => { el.textContent = String(msg || ""); });
  };

  // Inline confirm: a small fixed bar with the question and two buttons. Never window.confirm(),
  // which freezes the tab (and with it the TX heartbeat). One at a time: a new question cancels
  // the previous one. Resolves true only on an explicit OK.
  let confirmDone = null;
  TR.confirm = function (text, opts) {
    opts = opts || {};
    if (confirmDone) confirmDone(false);
    return new Promise((resolve) => {
      const bar = document.createElement("div");
      bar.className = "tr-confirm" + (opts.danger ? " danger" : "");
      bar.setAttribute("role", "alertdialog");
      bar.setAttribute("aria-live", "assertive");
      const msg = document.createElement("span");
      msg.className = "tr-confirm-msg"; msg.textContent = String(text || "");
      const ok = document.createElement("button");
      ok.type = "button"; ok.className = "ctl-btn " + (opts.danger ? "danger" : "primary");
      ok.textContent = opts.ok || "Continue";
      const no = document.createElement("button");
      no.type = "button"; no.className = "ctl-btn"; no.textContent = opts.cancel || "Cancel";
      bar.append(msg, ok, no);
      document.body.appendChild(bar);
      let timer = 0;
      const done = (v) => {
        if (confirmDone !== done) return;
        confirmDone = null;
        clearTimeout(timer);
        document.removeEventListener("keydown", onKey, true);
        bar.remove();
        resolve(!!v);
      };
      const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); done(false); } };
      confirmDone = done;
      ok.addEventListener("click", () => done(true));
      no.addEventListener("click", () => done(false));
      document.addEventListener("keydown", onKey, true);
      timer = setTimeout(() => done(false), opts.timeout || 15000);
      (opts.danger ? no : ok).focus();
    });
  };

  // -- navigation when the session ends ----------------------------------------------------------
  TR._leaving = false;
  const LOGIN_REASONS = ["session_revoked", "expired", "signed_out", "pwchanged", "role_changed"];
  function here() { return location.pathname + location.search; }
  function loginUrl(reason) {
    const r = reason === "unauthenticated" ? "expired" : reason;
    const q = new URLSearchParams();
    if (r && LOGIN_REASONS.includes(r)) q.set("reason", r);
    q.set("next", here());
    return "/login?" + q.toString();
  }
  function leave(url) {
    if (TR._leaving) return;
    TR._leaving = true;
    location.replace(url);
  }
  TR.goLogin = (reason) => leave(loginUrl(reason));

  // -- TR.api: every fetch (§2.9: mutating requests always carry a JSON body) --------------------
  TR.api = async function (url, opts) {
    opts = opts || {};
    const method = String(opts.method || (opts.json !== undefined ? "POST" : "GET")).toUpperCase();
    const init = { method, credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" } };
    if (method !== "GET" && method !== "HEAD") {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.json === undefined ? {} : opts.json);
    }
    let r;
    try {
      r = await fetch(url, init);
    } catch (e) {
      const err = new Error("Can't reach the server — check the connection.");
      err.status = 0; err.code = "network";
      throw err;
    }
    let j = null;
    try { j = await r.json(); } catch (_) { j = null; }
    if (r.ok) return j || {};
    const code = j && typeof j.error === "string" ? j.error : null;
    const err = new Error((j && (j.message || j.error)) || ("HTTP " + r.status));
    err.status = r.status; err.code = code; err.body = j || {};
    if (j && j.field) err.field = j.field;
    if (r.status === 401 && !opts.noRedirect) { TR.goLogin("expired"); throw err; }
    if (r.status === 403 && code === "password_change_required" && !opts.noRedirect) {
      leave("/change-password"); throw err;
    }
    // 403 reauth_required: thrown as-is; admin.js prompts for the password and retries once.
    if (r.status === 409 && code === "busy" && !opts.quiet) {
      const o = err.body.owner;
      TR.toast(o === "FRONT PANEL" ? "The radio is keyed from its front panel."
               : (o ? o + " is on the air — try again when they finish." : "Someone is on the air — try again."), "warn");
    }
    throw err;
  };

  // -- the link banner + WebSocket close codes (§2.11 table) --------------------------------------
  const link = { attempts: 0, last4401: 0, fatal: false };
  function banner(kind, text, withReload) {
    const b = $("linkBanner");
    if (!b) return;
    if (!kind) { b.hidden = true; b.className = "link-banner"; b.textContent = ""; return; }
    b.className = "link-banner " + kind;
    b.textContent = "";
    const s = document.createElement("span");
    s.textContent = text;
    b.appendChild(s);
    if (withReload) {
      const btn = document.createElement("button");
      btn.type = "button"; btn.className = "ctl-btn"; btn.textContent = "Reload";
      btn.addEventListener("click", () => location.reload());
      b.appendChild(btn);
    }
    b.hidden = false;
  }
  TR.banner = banner;

  TR.linkUp = function () {
    if (link.fatal) return;
    link.attempts = 0;
    banner(null);
  };

  async function authStatus() {
    try {
      const r = await fetch("/api/auth_status", { credentials: "same-origin", cache: "no-store" });
      return await r.json();
    } catch (_) { return null; }
  }

  // 4401: the session ended. If it is still valid (our own password change re-issued the cookie),
  // reconnect; otherwise sign in again. Two consecutive 4401 closes within 30 s go straight to the
  // sign-in page, so a loop can never form (AR-62).
  async function onAuthLost(reason, reconnect) {
    const now = Date.now();
    if (link.last4401 && now - link.last4401 < 30000) { TR.goLogin("session_revoked"); return; }
    link.last4401 = now;
    const j = await authStatus();
    if (j && j.authed && !j.must_change) { reconnect(); return; }
    if (j && j.authed && j.must_change) { leave("/change-password"); return; }
    if (j && j.authed === false) { TR.goLogin(reason || "session_revoked"); return; }
    linkLost(reconnect);                        // the server is unreachable: keep trying
  }

  function linkLost(reconnect) {
    link.attempts += 1;
    const n = link.attempts;
    banner("warn", "Connection to the server lost — reconnecting… (attempt " + n + ")");
    const delay = Math.min(10000, 1200 * Math.pow(2, n - 1));
    if (n % 3 === 0) {                           // after 3 failures: is the session still valid?
      authStatus().then((j) => {
        if (j && j.authed === false) { TR.goLogin("expired"); return; }
        if (j && j.authed && j.must_change) { leave("/change-password"); return; }
        setTimeout(reconnect, delay);
      });
      return;
    }
    setTimeout(reconnect, delay);
  }

  const FATAL_TEXT = {
    bad_origin: "This page's address doesn't match the server's allowed names or origins. Open TeleRig by its usual address — or, from the host PC, add this address under Admin → Access.",
    server_locked: "The server is locked: a settings file is unreadable. Fix it on the host PC (see the TeleRig window), then reload.",
    forbidden: "The server refused this connection.",
  };
  TR.handleClose = function (code, reason, reconnect) {
    if (TR._leaving || link.fatal) return;
    if (code === 4401) { onAuthLost(reason, reconnect); return; }
    link.last4401 = 0;                           // only CONSECUTIVE 4401 closes count toward the cap
    if (code === 4403) {
      if (reason === "password_change_required") { leave("/change-password"); return; }
      link.fatal = true;
      banner("fatal", FATAL_TEXT[reason] || FATAL_TEXT.forbidden, true);
      return;
    }
    if (code === 4429) {
      banner("warn", "Too many open TeleRig tabs or viewers — close one. Retrying in 10 s…");
      setTimeout(reconnect, 10000);
      return;
    }
    linkLost(reconnect);                         // 1013 slow consumer, 1006, 1011, ...
  };

  // -- sim-only: feed a synthetic server frame into app.js (verifies owner-only UI without TX) ----
  if (boot.sim_only === true) {
    TR._inject = function (frame) {
      const f = typeof frame === "string" ? JSON.parse(frame) : frame;
      if (typeof TR._onFrame === "function") TR._onFrame(f, { injected: true });
      return f;
    };
  }

  // -- stacked-tile fallback (§3.20): the layout engine failed or never became ready ---------------
  TR.fallbackLayout = function () {
    const parking = $("tileParking");
    if (!parking || document.body.classList.contains("tr-nogrid")) return;
    const grid = $("grid");
    if (grid) grid.querySelectorAll(".grid-stack-item").forEach((el) => parking.appendChild(el));
    parking.hidden = false;
    parking.removeAttribute("aria-hidden");
    document.body.classList.add("tr-nogrid");
    TR.toast("Layout engine failed — showing basic layout", "warn");
  };
  document.addEventListener("DOMContentLoaded", () => {
    TR.setRole(TR.role);
    setTimeout(() => {
      const grid = $("grid");
      if (!grid) return;                         // no tiled layout on this page (before B3)
      const lay = window.RadioLayout;
      if (!grid.classList.contains("tr-ready") || !window.GridStack || !lay || lay._stub) TR.fallbackLayout();
    }, 2000);
  });
})();
