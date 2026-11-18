/* TeleRig — Administration (spec §5.4, §2.17-§2.19, E10): the admin modal, built on first open in
 * #modalRoot with the TR.modal shell from account.js.
 *
 * Tabs: Users · Access · Remote · Radio & audio · WSJT-X bridge · Activity · Lockouts. Every request goes through
 * TR.api to the /api/admin/* routes (§2.17). A 403 reauth_required opens the sudo prompt ("Confirm
 * your password to continue", §2.18) and the call is retried ONCE. Mutating controls carry
 * data-mut: all of them are disabled while one request is pending (8 s fallback), and the result is
 * shown in one inline line (green "Done" / red error, cleared after 4 s). All text is rendered with
 * textContent, never innerHTML. The server decides every rule (last admin, self-demote, lock-out,
 * local-only Serve, sudo); this file only asks and shows the answer.
 *
 * window.TRAdmin = { open(tab, opts), close(), isOpen() }
 * TR.noticeActions (§4.5): lockouts -> Lockouts · behind_proxy_stale -> Remote + Clear flag ·
 *                          audio_default:<radio> -> Radio & audio
 */
(function () {
  "use strict";
  const TR = window.TR;
  if (!TR || typeof TR.modal !== "function" || typeof TR.h !== "function") return;   // account.js missing
  const $ = (id) => document.getElementById(id);
  const h = TR.h;
  const enc = window.TextEncoder ? new TextEncoder() : null;
  const bytes = (s) => (enc ? enc.encode(String(s)).length : unescape(encodeURIComponent(String(s))).length);

  // ---- formatting ----------------------------------------------------------------------------------
  const pad = (n) => String(n).padStart(2, "0");
  function hhmm(epochSec) {
    const d = new Date(Number(epochSec) * 1000);
    return isNaN(d) ? "?" : pad(d.getHours()) + ":" + pad(d.getMinutes());
  }
  function stamp(iso) {                              // local YYYY-MM-DD HH:MM:SS
    const d = new Date(iso);
    if (!iso || isNaN(d)) return String(iso || "");
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " " +
           pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }
  function rel(iso) {
    if (!iso) return "never";
    const t = Date.parse(iso);
    if (isNaN(t)) return String(iso);
    const s = Math.round((Date.now() - t) / 1000);
    if (s < 60) return "just now";
    if (s < 3600) return Math.floor(s / 60) + " min ago";
    if (s < 86400) return Math.floor(s / 3600) + " h ago";
    return Math.floor(s / 86400) + " d ago";
  }
  function mmss(ms) {
    const s = Math.max(0, Math.floor(ms / 1000));
    return Math.floor(s / 60) + ":" + pad(s % 60);
  }
  function uidOf(name) {                             // §2.17: lowercase hex of the canonical name's UTF-8
    const b = enc ? enc.encode(name) : [...unescape(encodeURIComponent(name))].map((c) => c.charCodeAt(0));
    return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
  }
  const chip = (text, kind) => h("span", { class: "tm-tag" + (kind ? " " + kind : ""), text });
  const note = (text) => h("p", { class: "tm-note", text });
  const head3 = (text, sub) => h("div", { class: "tm-sechead" }, h("h3", { class: "tm-h", text }), sub ? h("span", { class: "tm-hint", text: sub }) : null);
  function btn(label, opts) {
    opts = opts || {};
    const b = h("button", { type: "button", class: "ctl-btn" + (opts.cls ? " " + opts.cls : ""), id: opts.id, title: opts.title,
                            disabled: !!opts.disabled, "data-mut": opts.mut ? true : null, "aria-label": opts.aria },
                opts.icon ? TR.icon(opts.icon) : null, label);
    if (opts.on) b.addEventListener("click", opts.on);
    return b;
  }
  function loading(text) { return h("p", { class: "tm-note tm-loading", text: text || "Loading…" }); }
  // Element.append() would turn a null into the text "null": skip the absent parts
  function put(el) {
    for (let i = 1; i < arguments.length; i++) { const k = arguments[i]; if (k !== null && k !== undefined && k !== false) el.append(k); }
    return el;
  }

  const ERR = {
    last_admin: "There must be at least one admin.",
    self: "Use Change password for your own account.",
    self_demote: "You can't change your own role.",
    self_delete: "You can't delete your own account.",
    exists: "That callsign already exists.",
    not_found: "No such user (it may have been deleted).",
  };
  function errText(e) {
    if (!e) return "Something went wrong.";
    if (e.status === 0) return "Can't reach the server — check the connection.";
    if (e.code && ERR[e.code]) return ERR[e.code];
    return e.message || ("HTTP " + e.status);
  }

  // ---- state ---------------------------------------------------------------------------------------
  const TABS = [["users", "Users"], ["access", "Access"], ["remote", "Remote"], ["audio", "Radio & audio"],
                ["bridge", "WSJT-X bridge"], ["activity", "Activity"], ["lockouts", "Lockouts"], ["update", "Update"]];
  const TAB_KEY = "telerig.admin.tab";
  let M = null;                          // the modal shell (built on first open)
  const tabBtns = {};
  let panel = null, resultEl = null, sudoLayer = null;
  let cur = null, renderSeq = 0;         // the open tab; a newer render supersedes an older async one
  let timers = [];                       // the open tab's intervals (sessions poll, on-air clock)
  // A temporary password is shown exactly ONCE. `secret` holds it only until the next Users render
  // takes it (secretPanel); from then on it lives only in that panel. The panel goes away on Done and
  // on ANY later render (a role change, another action, a refresh, the tab switch, closing the
  // dialog): scrubSecret() then blanks the shown text and drops the value, so a re-render can never
  // show it again.
  let secret = null;
  let shownSecret = null;                // {s, code}: the one on screen
  function scrubSecret() {
    if (!shownSecret) return;
    shownSecret.s.pw = null;
    shownSecret.code.textContent = "";
    shownSecret = null;
  }

  function clearTimers() {                // leaving a tab (or closing): its intervals and any STOP arm end
    for (const t of timers) clearInterval(t);
    timers = [];
    if (onAir && onAir.arm) onAir.arm.disarm();
  }
  const alive = (seq) => seq === renderSeq && M && M.isOpen();

  // ---- result line + double-submit guard ---------------------------------------------------------------
  let resultT = 0;
  function say(msg, ok) {
    clearTimeout(resultT);
    if (!resultEl) return;
    resultEl.textContent = "";
    resultEl.className = "tm-result" + (msg ? (ok ? " ok" : " err") : "");
    if (!msg) return;
    resultEl.append(TR.icon(ok ? "check" : "triangle-alert"), h("span", { text: String(msg) }));
    resultT = setTimeout(() => say(""), 4000);
  }
  let pending = false, pendingT = 0, pendingEls = [];
  function setPending(on) {
    clearTimeout(pendingT);
    if (on) {
      pending = true;
      pendingEls = [...M.dialog.querySelectorAll("[data-mut]")].filter((el) => !el.disabled);
      for (const el of pendingEls) el.disabled = true;
      M.dialog.classList.add("tm-busy");
      pendingT = setTimeout(function fallback() {     // 8 s fallback (never while the sudo prompt waits)
        if (sudoLayer && sudoLayer.open) { pendingT = setTimeout(fallback, 8000); return; }
        setPending(false);
      }, 8000);
      return;
    }
    pending = false;
    for (const el of pendingEls) if (el.isConnected) el.disabled = false;
    pendingEls = [];
    if (M) M.dialog.classList.remove("tm-busy");
  }
  // One mutation: guard, sudo retry, result line. -> {ok, data} | {ok:false, err}
  async function act(fn, done) {
    if (pending) return { ok: false, err: { code: "pending", message: "Another change is still being saved." } };
    setPending(true);
    try {
      const data = await fn();
      if (done) say(typeof done === "function" ? done(data) : done, true);
      return { ok: true, data };
    } catch (e) {
      say(e && e.code === "cancelled" ? "Cancelled — nothing changed." : errText(e), false);
      return { ok: false, err: e };
    } finally {
      setPending(false);
    }
  }
  // TR.api + the sudo window: 403 reauth_required -> "Confirm your password" -> retry ONCE (§2.18)
  async function api(url, opts) {
    try {
      return await TR.api(url, opts || {});
    } catch (e) {
      if (!(e && e.status === 403 && e.code === "reauth_required")) throw e;
      await sudoLayer.ask();                         // rejects {code:"cancelled"} when the admin cancels
      return TR.api(url, opts || {});
    }
  }

  // ---- the sudo prompt (a layer inside the dialog) --------------------------------------------------------
  function buildSudo() {
    const input = h("input", { type: "password", id: "admSudoPw", class: "tm-input", autocomplete: "current-password",
                               maxlength: "128", spellcheck: "false", "aria-describedby": "admSudoMsg" });
    const msg = h("div", { class: "tm-ferr", id: "admSudoMsg", role: "alert" });
    const ok = h("button", { type: "submit", class: "ctl-btn primary" }, TR.icon("check"), "Confirm");
    const cancel = h("button", { type: "button", class: "ctl-btn", text: "Cancel" });
    const form = h("form", { class: "tm-sudo-card", novalidate: true },
      h("div", { class: "tm-sudo-title" }, TR.icon("lock"), h("b", { id: "admSudoTitle", text: "Confirm your password to continue" })),
      note("Changes to users and settings need a recent password check. It lasts 10 minutes."),
      h("label", { class: "tm-label", for: "admSudoPw", text: "Password for " + (TR.user || "this account") }),
      input, msg, h("div", { class: "tm-actions" }, cancel, ok));
    const el = h("div", { class: "tm-sudo", hidden: true, role: "dialog", "aria-modal": "true", "aria-labelledby": "admSudoTitle" }, form);
    const S = { el, open: false, waiters: [] };
    let lockedUntil = 0, lockT = 0;
    function setInert(on) { for (const c of M.dialog.children) if (c !== el) c.inert = on; }
    function finish(good, err) {
      S.open = false; el.hidden = true; input.value = ""; msg.textContent = "";
      clearInterval(lockT);
      setInert(false);
      const w = S.waiters; S.waiters = [];
      for (const x of w) { if (good) x.res(); else x.rej(err); }
    }
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (Date.now() < lockedUntil) return;
      if (!input.value) { msg.textContent = "Enter your password."; input.focus(); return; }
      ok.disabled = true;
      try {
        await TR.api("/api/admin/reauth", { method: "POST", json: { password: input.value }, noRedirect: true, quiet: true });
        finish(true);
      } catch (er) {
        input.value = "";
        if (er.status === 401 && er.code === "invalid_credentials") { msg.textContent = "That password is not right."; input.focus(); }
        else if (er.status === 401) { finish(false, er); TR.goLogin("expired"); }
        else if (er.status === 429) {
          lockedUntil = Date.now() + Math.max(1, +(er.body && er.body.retry_after) || 60) * 1000;
          const tick = () => {
            const left = Math.max(0, Math.ceil((lockedUntil - Date.now()) / 1000));
            msg.textContent = left > 0 ? "Too many attempts. Try again in " + mmss(left * 1000) : "";
            if (left <= 0) { clearInterval(lockT); TR.srLive("You can try again now."); }
          };
          tick(); clearInterval(lockT); lockT = setInterval(tick, 1000);
        } else msg.textContent = er.message || "Could not confirm the password.";
      } finally {
        ok.disabled = false;
      }
    });
    cancel.addEventListener("click", () => finish(false, Object.assign(new Error("cancelled"), { code: "cancelled" })));
    S.ask = () => new Promise((res, rej) => {
      S.waiters.push({ res, rej });
      if (S.open) return;
      S.open = true; el.hidden = false; setInert(true);
      input.value = ""; msg.textContent = "";
      setTimeout(() => input.focus(), 0);
    });
    S.cancel = () => { if (S.open) cancel.click(); };
    return S;
  }

  // ---- the modal shell --------------------------------------------------------------------------------
  function build() {
    M = TR.modal({
      id: "adm", className: "tm-admin",
      onEscape: () => { if (sudoLayer && sudoLayer.open) sudoLayer.cancel(); else M.close(); },
      onClose: onClosed,
      returnFocus: () => {
        const b = $("adminBtn");
        return b && b.getClientRects().length ? b : $("userBtn");
      },
    });
    const tabs = h("div", { class: "tm-tabs", role: "tablist", "aria-label": "Administration sections" });
    for (const [id, label] of TABS) {
      const b = h("button", { type: "button", class: "tm-tab", role: "tab", id: "admTab-" + id, "aria-selected": "false",
                              "aria-controls": "admPanel", tabindex: "-1", text: label, dataset: { tab: id } });
      b.addEventListener("click", () => select(id));
      tabBtns[id] = b;
      tabs.append(b);
    }
    tabs.addEventListener("keydown", (e) => {        // arrow keys move between tabs (ARIA tabs pattern)
      const ids = TABS.map((t) => t[0]);
      let i = ids.indexOf(cur);
      if (e.key === "ArrowRight") i = (i + 1) % ids.length;
      else if (e.key === "ArrowLeft") i = (i - 1 + ids.length) % ids.length;
      else if (e.key === "Home") i = 0;
      else if (e.key === "End") i = ids.length - 1;
      else return;
      e.preventDefault();
      select(ids[i]);
      tabBtns[ids[i]].focus();
    });
    resultEl = h("div", { class: "tm-result", role: "status", "aria-live": "polite" });
    panel = h("div", { class: "tm-body", role: "tabpanel", id: "admPanel", tabindex: "-1" });
    sudoLayer = buildSudo();
    M.dialog.append(
      h("header", { class: "tm-head" },
        h("span", { class: "tm-chip" }, TR.icon("shield"), h("b", { id: M.titleId, text: "ADMIN" })),
        h("span", { class: "tm-who", text: TR.user || "" }),
        h("button", { type: "button", class: "tm-x", title: "Close", "aria-label": "Close administration",
                      on: { click: () => M.close() } }, TR.icon("x"))),
      tabs, resultEl, panel, sudoLayer.el);
  }
  function onClosed() {
    clearTimers();
    renderSeq++;
    secret = null;                                    // shown once: gone with the dialog
    scrubSecret();
    if (sudoLayer) sudoLayer.cancel();
    say("");
    if (panel) panel.textContent = "";
    cur = null;
  }
  function select(id, opts) {
    if (!TABS.some((t) => t[0] === id)) id = "users";
    if (cur !== id) secret = null;                    // leaving the tab ends the one-time display
    scrubSecret();                                    // every re-render ends it too (shown once)
    clearTimers();
    cur = id;
    try { sessionStorage.setItem(TAB_KEY, id); } catch (_) {}
    for (const [tid] of TABS) {
      const b = tabBtns[tid], on = tid === id;
      b.setAttribute("aria-selected", on ? "true" : "false");
      b.tabIndex = on ? 0 : -1;
      b.classList.toggle("on", on);
    }
    panel.setAttribute("aria-labelledby", "admTab-" + id);
    panel.textContent = "";
    panel.scrollTop = 0;
    const seq = ++renderSeq;
    Promise.resolve()
      .then(() => RENDER[id](panel, seq, opts || {}))
      .catch((e) => {
        if (!alive(seq)) return;
        panel.textContent = "";
        panel.append(h("div", { class: "tm-box err", role: "alert", text: "Could not load this section: " + errText(e) }),
                     btn("Retry", { icon: "rotate-ccw", on: () => select(id) }));
      });
  }
  const rerender = () => { if (M && M.isOpen() && cur) select(cur); };

  function open(tab, opts) {
    if (!TR.can("admin")) { TR.toast("Administration needs the admin role.", "warn"); return; }
    if (TR.txBlocksModal()) {                         // the backdrop would cover the top-bar STOP
      TR.toast("Stop transmitting before opening Administration.", "warn");
      TR.srLive("Stop transmitting before opening Administration");
      return;
    }
    if (TR.menu && TR.menu.close) TR.menu.close(false);
    try { const L = window.RadioLayout; if (L && L.arranging && L.arranging() && L.exitArrange) L.exitArrange(); } catch (_) {}
    if (!M) build();
    const wasOpen = M.isOpen();
    M.open();
    let t = tab;
    if (!t) { try { t = sessionStorage.getItem(TAB_KEY); } catch (_) {} }
    if (!wasOpen || t !== cur || opts) select(t || "users", opts);
    if (tabBtns[cur]) tabBtns[cur].focus();
  }
  function close() { if (M) M.close(); }

  // remove notices the admin just resolved (they come only from the page bootstrap)
  function dropNotice(pred) {
    if (!Array.isArray(TR.notices) || typeof TR.renderNotices !== "function") return;
    const next = TR.notices.filter((k) => !pred(k));
    if (next.length !== TR.notices.length) TR.renderNotices(next);
  }
  async function copyText(text, b) {
    let ok = false;
    try { if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); ok = true; } } catch (_) {}
    if (!ok) {                                        // plain http on a LAN name: no Clipboard API
      const ta = h("textarea", { class: "tm-copybuf", "aria-hidden": "true", readonly: true });
      ta.value = text;
      M.dialog.append(ta);
      ta.select();
      try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
      ta.remove();
    }
    if (b) { const t = b.lastChild; const old = t.textContent; t.textContent = ok ? "Copied" : "Copy failed"; setTimeout(() => { t.textContent = old; }, 1500); }
  }

  // =====================================================================================================
  // Users
  // =====================================================================================================
  const ROLES = ["viewer", "operator", "admin"];
  const ROLE_TEXT = { viewer: "watch & listen, can't change the radio", operator: "operate & transmit, connect",
                      admin: "operator + users & settings" };
  const CALL_RE = /^[A-Za-z0-9/_-]{2,32}$/;
  function canonical(raw) {
    const n = String(raw || "").trim();
    return CALL_RE.test(n) && /[A-Za-z0-9]/.test(n) ? n.toUpperCase() : null;
  }
  function pwProblem(pw) {
    const n = bytes(pw);
    return n < 8 || n > 72 ? "Password must be 8–72 bytes (this one is " + n + ")." : null;
  }
  // "Generate" (server-made temporary password, the default) or "Type one" (8-72 bytes)
  let pwSeq = 0;
  function pwChoice() {
    const name = "admPw" + (++pwSeq);
    const gen = h("input", { type: "radio", name, value: "gen", checked: true });
    const typ = h("input", { type: "radio", name, value: "type" });
    const field = h("input", { type: "text", class: "tm-input tm-pwfield", autocomplete: "off", spellcheck: "false", maxlength: "128",
                               placeholder: "8–72 characters", disabled: true, "aria-label": "Temporary password to set" });
    gen.addEventListener("change", () => { field.value = ""; field.disabled = true; });
    typ.addEventListener("change", () => { field.disabled = false; field.focus(); });
    const el = h("div", { class: "tm-pwmode", role: "radiogroup", "aria-label": "Temporary password" },
      h("label", { class: "tm-radio" }, gen, "Generate"),
      h("label", { class: "tm-radio" }, typ, "Type one:"), field);
    return { el, typed: () => (typ.checked ? field.value : null) };
  }
  function secretPanel() {
    if (!secret) return null;
    const s = secret;
    secret = null;                                    // taken: no later render can show it again
    scrubSecret();
    const lines = [h("b", { text: s.what === "added" ? s.user + " was added as " + s.role + "."
                                                       : s.user + "'s password was reset — their open sessions ended." })];
    if (s.pw) {
      const copyB = btn("Copy", { icon: "copy", cls: "sm", on: () => { if (s.pw) copyText(s.pw, copyB); } });
      const code = h("code", { class: "tm-secret", text: s.pw });
      shownSecret = { s, code };
      lines.push(h("div", { class: "tm-secret-row" }, h("span", { text: "Temporary password (shown once): " }),
                   code, copyB));
    } else if (s.typed) {
      lines.push(note("The password you typed is set."));
    }
    lines.push(note(s.must_change ? "They choose their own password at their first sign-in."
                                  : "They keep this password (no change required)."));
    const el = h("div", { class: "tm-box ok tm-secret-panel", role: "status" }, lines);
    el.append(h("div", { class: "tm-actions" }, btn("Done", { on: () => { secret = null; scrubSecret(); el.remove(); } })));
    return el;
  }

  let addOpen = false;
  function addUserForm() {
    const callIn = h("input", { type: "text", id: "admNewUser", class: "tm-input tm-call", maxlength: "32", spellcheck: "false",
                                autocomplete: "off", autocapitalize: "characters", placeholder: "N0CALL", pattern: "[A-Za-z0-9/_-]{2,32}" });
    const callErr = h("div", { class: "tm-ferr", hidden: true });
    const roleSel = h("select", { id: "admNewRole", class: "tm-select" },
                      ROLES.map((r) => h("option", { value: r, text: r + " — " + ROLE_TEXT[r] })));
    roleSel.value = "viewer";                        // F13: new users default to viewer
    const pw = pwChoice();
    const pwErr = h("div", { class: "tm-ferr", hidden: true });
    const mc = h("input", { type: "checkbox", checked: true });
    const formErr = h("div", { class: "tm-ferr", hidden: true });
    const go = h("button", { type: "submit", class: "ctl-btn primary", "data-mut": true }, TR.icon("plus"), "Add user");
    const cancel = btn("Cancel", { on: () => { addOpen = false; form.remove(); toggle.setAttribute("aria-expanded", "false"); } });
    const form = h("form", { class: "tm-card tm-addform", novalidate: true },
      h("div", { class: "tm-grid2" },
        h("div", { class: "tm-field" }, h("label", { class: "tm-label", for: "admNewUser", text: "Callsign" }), callIn, callErr),
        h("div", { class: "tm-field" }, h("label", { class: "tm-label", for: "admNewRole", text: "Role" }), roleSel)),
      h("div", { class: "tm-field" }, h("span", { class: "tm-label", text: "Password" }), pw.el, pwErr),
      h("label", { class: "tm-check1" }, mc, "Require a new password at first sign-in"),
      formErr,
      h("div", { class: "tm-actions" }, cancel, go));
    const show = (el, t) => { el.textContent = t; el.hidden = !t; };
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      show(callErr, ""); show(pwErr, ""); show(formErr, "");
      const name = canonical(callIn.value);
      if (!name) { show(callErr, "Use 2–32 letters, digits, / _ -"); callIn.focus(); return; }
      const typed = pw.typed();
      if (typed !== null) { const p = pwProblem(typed); if (p) { show(pwErr, p); return; } }
      const body = { user: name, role: roleSel.value, must_change: mc.checked };
      if (typed !== null) body.password = typed;
      const r = await act(() => api("/api/admin/users", { method: "POST", json: body }), (d) => (d.user || name) + " added.");
      if (!r.ok) {
        const er = r.err || {};
        if (er.field === "user" || er.code === "exists") show(callErr, errText(er));
        else if (er.field === "password") show(pwErr, errText(er));
        else if (er.code !== "cancelled") show(formErr, errText(er));
        return;
      }
      secret = { what: "added", user: r.data.user || name, role: body.role, pw: r.data.temp_password || null,
                 typed: typed !== null, must_change: mc.checked };
      addOpen = false;
      rerender();
    });
    // the toggle is created by the caller; bound below
    let toggle = null;
    form._bindToggle = (t) => { toggle = t; };
    return form;
  }

  async function renderUsers(p, seq) {
    p.append(loading());
    const j = await TR.api("/api/admin/users");
    if (!alive(seq)) return;
    p.textContent = "";
    const me = j.me, users = Array.isArray(j.users) ? j.users : [];
    const admins = users.filter((u) => u.role === "admin").length;
    const sp = secretPanel();
    if (sp) p.append(sp);
    const toggle = btn("Add user", { icon: "plus", id: "admAddUser" });
    toggle.setAttribute("aria-expanded", addOpen ? "true" : "false");
    const bar = h("div", { class: "tm-toolbar" }, toggle,
      h("span", { class: "tm-hint", text: users.length + " account" + (users.length === 1 ? "" : "s") + " · roles: viewer (watch & listen) · operator (operate & transmit) · admin (users & settings)" }));
    p.append(bar);
    let form = null;
    const showForm = () => {
      form = addUserForm(); form._bindToggle(toggle);
      bar.after(form);
      toggle.setAttribute("aria-expanded", "true");
      form.querySelector("#admNewUser").focus();
    };
    toggle.addEventListener("click", () => {
      if (form && form.isConnected) { form.remove(); addOpen = false; toggle.setAttribute("aria-expanded", "false"); return; }
      addOpen = true; showForm();
    });
    if (addOpen) { form = addUserForm(); form._bindToggle(toggle); bar.after(form); }

    const table = h("table", { class: "tm-table tm-users" },
      h("thead", null, h("tr", null, h("th", { text: "Callsign" }), h("th", { text: "Role" }), h("th", { text: "Status" }),
                                     h("th", { text: "Last sign-in" }), h("th", null, h("span", { class: "sr-only", text: "Actions" })))));
    const tb = h("tbody");
    for (const u of users) tb.append(userRow(u, me, admins));
    table.append(tb);
    p.append(table);
  }

  function userRow(u, me, admins) {
    const mine = u.user === me;
    const lastAdmin = u.role === "admin" && admins <= 1;
    const tr = h("tr", { dataset: { user: u.user } });
    // role
    const roleErr = h("div", { class: "tm-ferr", hidden: true });
    const roleSel = h("select", { class: "tm-select", "aria-label": "Role of " + u.user, "data-mut": mine ? null : true, disabled: mine,
                                  title: mine ? "You can't change your own role" : "Change the role — their open sessions end" },
                      ROLES.map((r) => h("option", { value: r, text: r })));
    roleSel.value = u.role;
    roleSel.addEventListener("change", async () => {
      const to = roleSel.value;
      roleErr.hidden = true;
      const r = await act(() => api("/api/admin/users/" + u.uid, { method: "PATCH", json: { role: to } }),
                          u.user + " is now " + to + " — their open sessions ended.");
      if (!r.ok) {
        roleSel.value = u.role;
        if (r.err && r.err.code !== "cancelled") { roleErr.textContent = errText(r.err); roleErr.hidden = false; }
        return;
      }
      rerender();
    });
    // status chips
    const chips = h("div", { class: "tm-chips" },
      mine ? chip("you", "me") : null,
      u.must_change ? chip("must change pw", "warn") : null,
      u.default_pw ? chip("default", "warn") : null,
      u.migrated_v1 ? chip("migrated", "info") : null,
      u.online > 0 ? chip("● online ×" + u.online, "ok") : null,
      u.on_air ? chip("ON AIR", "danger") : null,
      u.locked_until ? chip("locked until " + hhmm(u.locked_until), "danger") : null);
    // actions
    const actions = h("div", { class: "tm-rowacts" },
      btn("Reset password", { cls: "sm", icon: "key-round", disabled: mine, title: mine ? "Use Change password (user menu) for your own account" : null,
                              on: () => sub(tr, "reset", () => resetPanel(u)) }),
      btn("Sign out everywhere", { cls: "sm", icon: "log-out", disabled: mine, title: mine ? "Use Sign out everywhere in your user menu" : "Sign " + u.user + " out on every device",
                        on: () => sub(tr, "signout", () => confirmPanel("Sign " + u.user + " out on every device? Their open sessions end now.",
                          "Sign out everywhere", "log-out", () => api("/api/admin/users/" + u.uid + "/signout", { method: "POST" }),
                          (d) => u.user + " signed out (" + (d.kicked || 0) + " open session" + (d.kicked === 1 ? "" : "s") + " closed).")) }),
      u.locked_until ? btn("Unlock", { cls: "sm", icon: "lock", mut: true,
                                       on: async () => { const r = await act(() => api("/api/admin/users/" + u.uid + "/unlock", { method: "POST" }), u.user + " unlocked.");
                                                          if (r.ok) { rerender(); refreshLockNotice(); } } }) : null,
      btn("Reset layout", { cls: "sm", icon: "rotate-ccw",
                            on: () => sub(tr, "layout", () => confirmPanel("Reset " + u.user + "'s panel layout to the default (desktop and phone)? It applies at their next page load.",
                              "Reset layout", "rotate-ccw", () => api("/api/admin/users/" + u.uid + "/layout", { method: "DELETE" }),
                              u.user + "'s layout was reset.")) }),
      btn("Delete", { cls: "sm danger-ghost", icon: "x", disabled: mine || lastAdmin,
                      title: mine ? "You can't delete your own account" : (lastAdmin ? "There must be at least one admin" : "Delete " + u.user),
                      on: () => sub(tr, "delete", () => deletePanel(u)) }));
    tr.append(
      h("td", { "data-label": "Callsign", class: "tm-call" }, h("b", { text: u.user })),
      h("td", { "data-label": "Role" }, roleSel, roleErr),
      h("td", { "data-label": "Status" }, chips),
      h("td", { "data-label": "Last sign-in", title: u.last_login ? stamp(u.last_login) : "never signed in", text: rel(u.last_login) }),
      h("td", { "data-label": "Actions" }, actions));
    return tr;
  }
  // one inline action panel under a row at a time
  function sub(tr, kind, make) {
    const next = tr.nextElementSibling;
    const open = next && next.classList.contains("tm-subrow") ? next : null;
    const same = open && open.dataset.kind === kind;
    for (const s of tr.parentElement.querySelectorAll(".tm-subrow")) s.remove();
    if (same) return;
    const cell = h("td", { colspan: String(tr.children.length || 1) }, make());
    const row = h("tr", { class: "tm-subrow", dataset: { kind } }, cell);
    tr.after(row);
    const f = row.querySelector("input:not([disabled]), button");
    if (f) f.focus();
  }
  function closeSub(el) { const r = el.closest(".tm-subrow"); if (r) r.remove(); }
  function confirmPanel(text, okLabel, icon, call, done) {
    const err = h("div", { class: "tm-ferr", hidden: true });
    const el = h("div", { class: "tm-subpanel" }, h("p", { class: "tm-note", text }), err);
    const ok = btn(okLabel, { icon, cls: "danger", mut: true, on: async () => {
      const r = await act(call, done);
      if (r.ok) { closeSub(el); rerender(); return; }
      if (r.err && r.err.code !== "cancelled") { err.textContent = errText(r.err); err.hidden = false; }
    } });
    el.append(h("div", { class: "tm-actions" }, btn("Cancel", { on: () => closeSub(el) }), ok));
    return el;
  }
  function resetPanel(u) {
    const pw = pwChoice();
    const mc = h("input", { type: "checkbox", checked: true });
    const err = h("div", { class: "tm-ferr", hidden: true });
    const el = h("div", { class: "tm-subpanel" },
      h("p", { class: "tm-note", text: "Reset " + u.user + "'s password. Their open sessions end now." }),
      pw.el, h("label", { class: "tm-check1" }, mc, "Require a new password at first sign-in"), err);
    const go = btn("Reset password", { icon: "key-round", cls: "primary", mut: true, on: async () => {
      err.hidden = true;
      const typed = pw.typed();
      if (typed !== null) { const p = pwProblem(typed); if (p) { err.textContent = p; err.hidden = false; return; } }
      const body = { must_change: mc.checked };
      if (typed !== null) body.password = typed;
      const r = await act(() => api("/api/admin/users/" + u.uid + "/reset_password", { method: "POST", json: body }),
                          u.user + "'s password was reset.");
      if (!r.ok) { if (r.err && r.err.code !== "cancelled") { err.textContent = errText(r.err); err.hidden = false; } return; }
      secret = { what: "reset", user: u.user, pw: r.data.temp_password || null, typed: typed !== null, must_change: mc.checked };
      rerender();
    } });
    el.append(h("div", { class: "tm-actions" }, btn("Cancel", { on: () => closeSub(el) }), go));
    return el;
  }
  function deletePanel(u) {
    const input = h("input", { type: "text", class: "tm-input tm-call", spellcheck: "false", autocomplete: "off",
                               "aria-label": "Type " + u.user + " to confirm", placeholder: u.user });
    const err = h("div", { class: "tm-ferr", hidden: true });
    const go = btn("Delete " + u.user, { icon: "x", cls: "danger", disabled: true, on: async () => {
      if (input.value.trim().toUpperCase() !== u.user) return;
      const r = await act(() => api("/api/admin/users/" + u.uid, { method: "DELETE" }), u.user + " was deleted.");
      if (r.ok) { closeSub(el); rerender(); return; }
      if (r.err && r.err.code !== "cancelled") { err.textContent = errText(r.err); err.hidden = false; }
    } });
    input.addEventListener("input", () => {
      const okv = input.value.trim().toUpperCase() === u.user;
      go.disabled = !okv;
      if (okv) go.setAttribute("data-mut", ""); else go.removeAttribute("data-mut");
    });
    const el = h("div", { class: "tm-subpanel" },
      h("p", { class: "tm-note", text: "Delete " + u.user + "? This removes the account and its panel layout and ends their sessions. Type the callsign to confirm." }),
      input, err, h("div", { class: "tm-actions" }, btn("Cancel", { on: () => closeSub(el) }), go));
    return el;
  }

  // =====================================================================================================
  // Access: allowed names (Host check) and allowed origins (I15, F21, G-18)
  // =====================================================================================================
  const NAME_RE = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$/;
  const ORIGIN_RE = /^(https?):\/\/([^:/?#\s]+)(?::(\d{1,5}))?$/;
  const IPV4_RE = /^\d{1,3}(\.\d{1,3}){3}$/;
  const ALLOWED_MAX = 32;
  function badHost(hn) {                             // the server's rule (admin_api._bad_host)
    if (hn === "localhost" || hn.endsWith(".localhost")) return "localhost can't be listed (it always works on the host PC)";
    if (IPV4_RE.test(hn) || hn.includes(":") || /^\d+$/.test(hn.split(".").pop()))
      return "IP addresses can't be listed (they always pass the Host check)";
    if (hn.length > 253 || !NAME_RE.test(hn)) return "not a valid host name";
    return null;
  }
  function parseEntry(raw, kind) {
    const e = String(raw || "").trim().toLowerCase();
    if (!e) return { error: kind === "origin" ? "Enter an origin such as https://radio.example.org" : "Enter a host name" };
    if (kind === "origin") {
      if (!e.includes("://")) return { error: "An origin starts with https:// (or http://)" };
      const m = ORIGIN_RE.exec(e);
      if (!m) return { error: "An origin is scheme://name[:port] (http or https, nothing after the port)" };
      const why = badHost(m[2]);
      if (why) return { error: why };
      const port = m[3] ? +m[3] : (m[1] === "https" ? 443 : 80);
      if (!(port >= 1 && port <= 65535)) return { error: "invalid port" };
      return { value: m[1] + "://" + m[2] + ":" + port };
    }
    if (e.includes("://")) return { error: "That is an origin — add it under Allowed origins" };
    if (/^[^:]+:\d+$/.test(e)) return { error: "A name has no port — for a proxy on another port add the full origin under Allowed origins" };
    const why = badHost(e.replace(/^\[|\]$/g, ""));
    return why ? { error: why } : { value: e };
  }
  let acc = null;                                    // {orig, entries, host, origin, behind, err}
  async function renderAccess(p, seq) {
    p.append(loading());
    const j = await TR.api("/api/admin/allowed_hosts");
    if (!alive(seq)) return;
    const vals = (Array.isArray(j.entries) ? j.entries : []).map((e) => (e && typeof e === "object" ? e.value : e)).filter((v) => typeof v === "string");
    acc = { orig: vals.slice(), entries: vals.slice(), host: j.current_host || "", origin: j.current_origin || "",
            behind: !!j.behind_proxy, err: "" };
    drawAccess(p);
  }
  function drawAccess(p) {
    p.textContent = "";
    const names = acc.entries.filter((v) => !v.includes("://"));
    const origins = acc.entries.filter((v) => v.includes("://"));
    const dirty = acc.entries.length !== acc.orig.length || acc.entries.some((v, i) => v !== acc.orig[i]);
    const chipList = (list, empty) => list.length
      ? h("div", { class: "tm-hostchips" }, list.map((v) => h("span", { class: "tm-hostchip" }, h("code", { text: v }),
          h("button", { type: "button", class: "tm-chipx", title: "Remove " + v, "aria-label": "Remove " + v,
                        on: { click: () => { acc.entries = acc.entries.filter((x) => x !== v); acc.err = ""; drawAccess(p); } } }, TR.icon("x")))))
      : h("p", { class: "tm-hint tm-empty", text: empty });
    const addRow = (kind, id) => {
      const input = h("input", { type: "text", id, class: "tm-input", spellcheck: "false", autocomplete: "off",
                                 placeholder: kind === "origin" ? "https://radio.example.org:8443" : "radio-pc or radio.example.org",
                                 "aria-label": kind === "origin" ? "Origin to allow" : "Host name to allow" });
      const err = h("div", { class: "tm-ferr", hidden: true });
      const add = () => {
        const r = parseEntry(input.value, kind);
        if (r.error) { err.textContent = r.error; err.hidden = false; input.focus(); return; }
        if (acc.entries.includes(r.value)) { err.textContent = "Already in the list."; err.hidden = false; return; }
        if (acc.entries.length >= ALLOWED_MAX) { err.textContent = "At most " + ALLOWED_MAX + " entries."; err.hidden = false; return; }
        acc.entries = acc.entries.concat([r.value]); acc.err = "";
        drawAccess(p);
        const again = $(id); if (again) again.focus();
      };
      input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); add(); } });
      input.addEventListener("input", () => { err.hidden = true; });
      return h("div", { class: "tm-addrow" }, h("div", { class: "tm-inline" }, input, btn("Add", { icon: "plus", on: add })), err);
    };
    const quick = (() => {                             // "Add this browser's address (<hostname>)"
      const hn = String(acc.host || "").toLowerCase();
      if (!hn || badHost(hn) || names.includes(hn)) return null;
      return btn("Add this browser's address (" + hn + ")", { icon: "plus", cls: "sm", id: "admAddHere",
        on: () => { acc.entries = acc.entries.concat([hn]); acc.err = ""; drawAccess(p); } });
    })();
    const save = btn("Save", { icon: "check", cls: "primary", id: "admAccessSave", mut: true, disabled: !dirty, on: async () => {
      const r = await act(() => api("/api/admin/allowed_hosts", { method: "PUT", json: { entries: acc.entries } }), "Saved.");
      if (r.ok) {
        const vals = (r.data.entries || []).map((e) => (e && typeof e === "object" ? e.value : e));
        acc.orig = vals.slice(); acc.entries = vals.slice(); acc.err = "";
      } else if (r.err && r.err.code !== "cancelled") {
        acc.err = errText(r.err);                      // would_lock_out: the server's text, verbatim
      }
      drawAccess(p);
    } });
    const revert = btn("Revert", { icon: "rotate-ccw", id: "admAccessRevert", disabled: !dirty,
                                   on: () => { acc.entries = acc.orig.slice(); acc.err = ""; drawAccess(p); } });
    put(p,
      note("Empty list = this PC's own names, IP addresses, localhost and LAN-style names (e.g. radio-pc, radio.local). " +
           "Once you add a name, only listed names, IP addresses and localhost work. Origins are only needed when a proxy changes the Host header."),
      h("div", { class: "tm-kv" },
        h("span", { class: "tm-k", text: "This browser" }),
        h("span", null, "Host ", h("code", { text: acc.host || "—" }), " · Origin ", h("code", { text: location.origin }),
          acc.origin && acc.origin !== location.origin
            ? h("span", { class: "tm-hint" }, " · this server's own origin here: ", h("code", { text: acc.origin })) : null,
          acc.behind ? h("span", { class: "tm-hint", text: " · behind a proxy" }) : null)),
      h("section", { class: "tm-sec" }, head3("ALLOWED NAMES", "the Host check"), chipList(names, "None — the local-name rule applies."),
        addRow("name", "admAddName"), quick),
      h("section", { class: "tm-sec" }, head3("ALLOWED ORIGINS", "full https://name[:port], for a proxy that rewrites Host"),
        chipList(origins, "None."), addRow("origin", "admAddOrigin")),
      h("p", { class: "tm-hint", text: acc.entries.length + " of " + ALLOWED_MAX + " entries" + (dirty ? " · unsaved changes" : "") }),
      acc.err ? h("div", { class: "tm-box err", role: "alert", id: "admAccessErr", text: acc.err }) : null,
      h("div", { class: "tm-actions" }, revert, save));
  }

  // =====================================================================================================
  // Remote: Tailscale Serve (F9, G-20, AR-22, AR-34)
  // =====================================================================================================
  async function renderRemote(p, seq, opts) {
    p.append(loading("Checking Tailscale…"));
    const s = await TR.api("/api/admin/remote_status");
    if (!alive(seq)) return;
    p.textContent = "";
    const ts = s.tailscale || {}, port = s.port || TR.boot.port || "";
    const refresh = () => btn("Refresh", { icon: "rotate-ccw", id: "admRemoteRefresh", on: () => select("remote") });
    p.append(h("div", { class: "tm-box info tm-why" }, h("b", { text: "Why HTTPS? " }),
      "Browsers allow the microphone only over a secure (HTTPS) connection. Listening and radio control work over plain HTTP, " +
      "but to transmit with your mic from another device you need HTTPS. Tailscale provides it and keeps access private to " +
      "your own devices — no port-forwarding."));
    const st = h("section", { class: "tm-sec", id: "admRemoteStatus" }, head3("STATUS"));
    const out = h("div", { class: "tm-ferr", role: "alert", hidden: true });
    const showErr = (e) => { out.textContent = errText(e); out.hidden = false; };
    if (!ts.installed) {
      st.append(h("p", { class: "tm-bad", text: "Tailscale isn't installed on this PC." }),
        h("p", null, h("a", { href: "https://tailscale.com/download", target: "_blank", rel: "noopener", text: "Download Tailscale" }),
          ", install it and sign in, then Refresh."), refresh());
    } else if (!ts.running) {
      st.append(h("p", { class: "tm-bad", text: "Tailscale is installed but not signed in." }),
        note("Open the Tailscale app on this PC and sign in, then Refresh."), refresh());
    } else if (ts.serve_running && ts.url) {
      const copyB = btn("Copy", { icon: "copy", cls: "sm", on: () => copyText(ts.url, copyB) });
      st.append(h("p", { class: "tm-good" }, TR.icon("check"), " Remote access is ON — tailnet-only, HTTPS."),
        h("div", { class: "tm-inline tm-url" }, h("code", { text: ts.url }), copyB),
        note("Open that address from any device signed into your tailnet."),
        h("div", { class: "tm-actions tm-left" },
          btn("Turn off remote access", { icon: "unplug", id: "admServeOff", mut: true, on: async () => {
            out.hidden = true;
            const r = await act(() => api("/api/admin/tailscale_serve_off", { method: "POST" }), "Remote access is off.");
            if (r.ok) { select("remote"); return; }
            if (r.err && r.err.code !== "cancelled") {
              out.textContent = "Turn off failed: " + errText(r.err) + " — the behind-proxy setting was left as it was.";
              out.hidden = false;
            }
          } }), refresh()), out);
    } else if (ts.url) {
      const local = !!s.can_serve_on;
      st.append(h("p", { class: "tm-good" }, "Tailscale is ready (", h("code", { text: ts.url }), ")."),
        h("div", { class: "tm-actions tm-left" },
          btn("Turn on secure remote access", { icon: "globe", cls: "primary", id: "admServeOn", mut: local, disabled: !local,
            title: local ? "Runs tailscale serve for port " + port : "Only on the host PC itself",
            on: async () => {
              if (!local) return;
              out.hidden = true;
              const r = await act(() => api("/api/admin/tailscale_serve", { method: "POST" }), "Remote access is on.");
              if (r.ok) { select("remote"); return; }
              if (r.err && r.err.code !== "cancelled") showErr(r.err);
            } }), refresh()),
        local ? note("Runs tailscale serve, so your *.ts.net address serves HTTPS to TeleRig.")
              : h("p", { class: "tm-hint", id: "admServeHint",
                         text: "Open this page on the host PC itself (http://localhost:" + port + ") to turn remote access on." }),
        out);
    } else if (ts.running && !ts.url) {               // G-20: running, but no HTTPS address yet
      st.append(h("p", { class: "tm-warn", text: "Tailscale is running, but this PC has no HTTPS address yet." }),
        h("p", null, "In the Tailscale admin console → DNS, enable ", h("b", { text: "MagicDNS" }), " and ",
          h("b", { text: "HTTPS Certificates" }), ", then ", h("b", { text: "Refresh" }), "."), refresh());
    } else {
      st.append(h("p", { class: "tm-bad", text: "Couldn't determine the Tailscale state." }), refresh());
    }
    p.append(st);
    p.append(h("section", { class: "tm-sec" }, head3("HOW IT WORKS"),
      h("ol", { class: "tm-steps" },
        h("li", { text: "Install Tailscale on this PC and on the device you operate from; sign both into the same account." }),
        h("li", null, "In the Tailscale admin console → DNS, enable ", h("b", { text: "MagicDNS" }), " and ", h("b", { text: "HTTPS Certificates" }), "."),
        h("li", null, "Turn on secure remote access above (on the host PC), or run ", h("code", { text: "tailscale serve --bg " + port }), "."),
        h("li", null, "Open your ", h("code", { text: "*.ts.net" }), " address from any of your devices — HTTPS, mic and all.")),
      h("p", { class: "tm-note" }, "Tip: start the server with ", h("code", { text: "--host 127.0.0.1" }), " so only Tailscale can reach it.")));
    // behind_proxy: read-only; set and cleared only by confirmed Tailscale results (E8, AR-22) —
    // except a flag set by hand in config.json (e.g. "behind_proxy_source": "manual" for an nginx or
    // socat front, REMOTE-ACCESS.md), which TeleRig never changes and never calls stale.
    const byHand = !!(s.behind_proxy && s.behind_proxy_by_hand);
    const bp = h("section", { class: "tm-sec", id: "admProxy" }, head3("BEHIND A PROXY"),
      h("p", null, s.behind_proxy ? "Yes" : "No",
        s.behind_proxy && s.behind_proxy_source
          ? h("span", { class: "tm-hint", text: byHand ? " (set by hand in config.json)" : " (set by " + s.behind_proxy_source + ")" })
          : null),
      byHand
        ? note("Set by hand in config.json (\"behind_proxy_source\": " + JSON.stringify(s.behind_proxy_source) + ") for a proxy " +
               "or tunnel that Tailscale knows nothing about. TeleRig never changes it; edit config.json to remove it. " +
               "While it is set, this server never treats a request as coming from the host PC.")
        : note("Read-only: it is set when Tailscale confirms Serve is on and cleared when it confirms Serve is off. " +
               "While it is set, this server never treats a request as coming from the host PC."));
    const flagErr = h("div", { class: "tm-ferr", role: "alert", hidden: true });
    let clearB = null;
    if (s.stale_flag && !byHand) {
      clearB = btn("Clear flag", { icon: "x", id: "admClearFlag", mut: true, on: async () => {
        flagErr.hidden = true;
        const r = await act(() => api("/api/admin/clear_behind_proxy", { method: "POST" }), "The behind-proxy flag was cleared.");
        if (r.ok) { dropNotice((k) => k === "behind_proxy_stale"); select("remote"); return; }
        if (r.err && r.err.code !== "cancelled") { flagErr.textContent = errText(r.err); flagErr.hidden = false; }
      } });
      bp.append(h("div", { class: "tm-box warn" }, "Tailscale Serve is off for this port, so this flag is stale.", " "), clearB, flagErr);
    }
    p.append(bp, note("Everyone signs in — including on the host PC."));
    if (opts.clearFlag && clearB && !clearB.disabled) clearB.click();   // the notice's [Clear flag]
  }

  // =====================================================================================================
  // Radio & audio: the F20 connection pin + host sound-card pins (U1, F22, §2.14)
  // =====================================================================================================
  function radioList() {
    let list = [];
    try { if (window.RadioControl && typeof window.RadioControl.radios === "function") list = window.RadioControl.radios() || []; } catch (_) {}
    if (!list.length) list = Array.isArray(TR.boot.radios) ? TR.boot.radios : [];
    return list.filter((r) => r && r.id).map((r) => ({ id: r.id, name: r.name || r.id, has_network: r.has_network,
                                                         default_baud: r.default_baud }));
  }
  const KIND = { sim: "the simulator", serial: "USB (serial)", lan: "LAN", none: "nothing" };
  function describePin(pin) {
    if (!pin || typeof pin !== "object") return "";
    if (pin.transport === "sim") return "Simulator";
    if (pin.transport === "serial") return (pin.port || "?") + " · " + (pin.baud || "?") + " baud";
    if (pin.transport === "lan") return "LAN " + (pin.host || "?") + ":" + (pin.port || 50001);
    return String(pin.transport || "?");
  }
  function savedConns() {
    try { const c = JSON.parse(localStorage.getItem("telerig.conns") || "null"); return c && c.radios ? c.radios : {}; }
    catch (_) { return {}; }
  }
  // The transport the radio is connected with right now (never credentials): from the server's
  // transport kind plus, for serial / LAN, this browser's own connect form for that radio.
  function currentConn(r, a) {
    if (!(a.connected && a.radio === r.id)) return { why: "Connect this radio first" };
    const k = a.transport_kind;
    if (k === "sim") return { body: { transport: "sim" } };
    const c = savedConns()[r.id] || {};
    if (k === "serial" && c.transport === "serial" && c.port)
      return { body: { transport: "serial", port: c.port, baud: parseInt(c.baud, 10) || r.default_baud || 115200 } };
    if (k === "lan" && c.transport === "lan" && c.host) return { body: { transport: "lan", host: c.host, port: 50001 } };
    return { why: "This browser didn't connect it — connect it from here once, or let the admin who did pin it" };
  }
  // "Use the devices this browser used before": the v1 pickers' device indexes (telerig.conns)
  function previousDevices(rid, a) {
    const c = savedConns()[rid] || {};
    const find = (list, idx) => {
      if (idx === undefined || idx === null || idx === "") return null;
      const d = (list || []).find((x) => String(x.id) === String(idx));
      return d ? d.name : null;
    };
    const rx = find(a.inputs, c.rxDev), tx = find(a.outputs, c.micDev);
    return rx || tx ? { rx, tx } : null;
  }

  async function renderAudio(p, seq) {
    p.append(loading("Listing the host's sound cards…"));
    const a = await TR.api("/api/admin/audio");
    if (!alive(seq)) return;
    p.textContent = "";
    const radios = radioList();
    const curId = a.radio;
    radios.sort((x, y) => (y.id === curId) - (x.id === curId));
    if (!a.available) p.append(h("div", { class: "tm-box warn", role: "status", text: "Host sound cards are unavailable: install sounddevice on the host (pip install sounddevice) and restart TeleRig." }));
    const codecs = (list) => (list || []).filter((d) => /codec/i.test(d.name || "")).length;
    if (codecs(a.inputs) > 1 || codecs(a.outputs) > 1)
      p.append(h("div", { class: "tm-box warn", role: "status", text: "Several USB Audio CODEC devices are connected. Windows numbers them by plug order " +
        "(e.g. \"Microphone (2- USB Audio CODEC)\"), so after re-plugging a pin can point at another radio's sound card. Check the names below after re-plugging." }));
    p.append(note("Operators can connect only the current radio, and only over the transport pinned here (an admin's connect pins it automatically). " +
                  "Host sound cards carry the audio of USB (serial) radios; a LAN radio sends its own audio."));
    for (const r of radios) p.append(radioCard(r, a, r.id === curId));
  }
  function radioCard(r, a, isCur) {
    const pin = (a.transports || {})[r.id] || null;
    const pins = (a.pins || {})[r.id] || {};
    const connectedHere = !!(a.connected && a.radio === r.id);
    const card = h("details", { class: "tm-card tm-radiocard", open: isCur, dataset: { radio: r.id } });
    card.append(h("summary", null, h("b", { text: r.name }),
      isCur ? chip(a.connected ? "connected · " + (KIND[a.transport_kind] || a.transport_kind) : "last used", "ok") : null,
      pin ? chip("pinned: " + describePin(pin), "info") : chip("not pinned", null),
      pins.rx || pins.tx ? chip("audio pinned", "info") : null));
    // (1) the connection pin (F20)
    const pinErr = h("div", { class: "tm-ferr", hidden: true });
    const cc = currentConn(r, a);
    const putPin = (transport, done) => act(() => api("/api/admin/radio_transport", { method: "PUT", json: { radio: r.id, transport } }), done)
      .then((res) => {
        if (res.ok) { document.dispatchEvent(new CustomEvent("tr:refresh-status")); select("audio"); return; }
        if (res.err && res.err.code !== "cancelled") { pinErr.textContent = errText(res.err); pinErr.hidden = false; }
      });
    card.append(h("section", { class: "tm-sec" }, head3("CONNECTION PIN", "the one transport operators may reconnect"),
      h("p", { class: "tm-pin", dataset: { pin: pin ? pin.transport : "" } },
        pin ? h("code", { text: describePin(pin) }) : h("span", { class: "tm-hint", text: "Not pinned — operators can't connect this radio until an admin connects it once (or pins it here)." })),
      h("div", { class: "tm-actions tm-left" },
        btn("Clear", { icon: "x", cls: "sm", mut: !!pin, disabled: !pin, id: "admPinClear-" + r.id, title: pin ? "Remove the pin: operators can no longer connect " + r.name : "Nothing pinned",
                       on: () => putPin(null, r.name + ": pin cleared.") }),
        btn("Pin current connection", { icon: "plug", cls: "sm", mut: !!cc.body, disabled: !cc.body, id: "admPinNow-" + r.id,
                                        title: cc.body ? "Pin " + describePin(cc.body) : cc.why,
                                        on: () => { if (cc.body) putPin(cc.body, r.name + " pinned to " + describePin(cc.body) + "."); } })),
      pinErr));
    // (2) host sound cards
    const sec = h("section", { class: "tm-sec" }, head3("HOST SOUND CARDS", "USB (serial) radios"));
    if (connectedHere && a.transport_kind !== "serial")
      sec.append(note("Connected over " + (KIND[a.transport_kind] || a.transport_kind) + ": host sound cards are used only for USB (serial) radios."));
    else if (r.has_network !== false)
      sec.append(note("Used only while this radio is connected over USB (serial); over LAN the radio sends its own audio."));
    const sug = a.suggested || {};
    const sel = {}, state = {};
    const unsaved = h("span", { class: "tm-tag warn", text: "suggested · not saved", hidden: true });
    const devRow = (dir, list, label) => {
      const pinned = pins[dir] || null;
      const s = h("select", { class: "tm-select", "aria-label": label + " for " + r.name, disabled: !a.available, id: "admDev-" + dir + "-" + r.id },
        h("option", { value: "", text: "— not set —" }),
        (list || []).map((d) => h("option", { value: d.name, text: d.name })));
      if (pinned && !(list || []).some((d) => d.name === pinned)) s.append(h("option", { value: pinned, text: pinned + " (not found)" }));
      if (pinned) s.value = pinned;
      else if (sug[dir] && a.available) s.value = sug[dir];            // pre-selected suggestion, NOT saved
      sel[dir] = s; state[dir] = pinned || "";
      const res = isCur && a.resolved ? a.resolved[dir] : null;
      // "automatic": the server's F22 default is (or, for another radio, would be) in use: no pin and
      // exactly one CODEC device for this direction
      const auto = res ? res.source === "default" : (!pinned && !!(a.defaults && a.defaults[dir]));
      const badges = h("span", { class: "tm-chips" },
        auto ? chip("automatic", "info") : null,
        res && res.error === "missing" ? chip("missing", "danger") : null,
        pinned ? chip("pinned", "ok") : null);
      return h("div", { class: "tm-devrow" }, h("label", { class: "tm-label", for: s.id, text: label }), s, badges);
    };
    sec.append(devRow("rx", a.inputs, "Host RX (radio audio in)"), devRow("tx", a.outputs, "Host TX (audio to the radio)"));
    const dirty = () => (sel.rx.value || "") !== state.rx || (sel.tx.value || "") !== state.tx;
    const audErr = h("div", { class: "tm-ferr", hidden: true });
    const saveB = btn("Save", { icon: "check", cls: "primary sm", id: "admAudioSave-" + r.id, on: async () => {
      audErr.hidden = true;
      const body = { radio: r.id, rx: sel.rx.value || null, tx: sel.tx.value || null };
      const res = await act(() => api("/api/admin/audio", { method: "PUT", json: body }),
                            (d) => r.name + ": sound cards saved" + (d.restarted ? " (audio restarted)." : "."));
      if (!res.ok) { if (res.err && res.err.code !== "cancelled") { audErr.textContent = errText(res.err); audErr.hidden = false; } return; }
      dropNotice((k) => k === "audio_default:" + r.id);
      document.dispatchEvent(new CustomEvent("tr:refresh-status"));
      select("audio");
    } });
    const syncDirty = () => {
      const d = dirty();
      unsaved.hidden = !d;
      saveB.disabled = !d || !a.available;
      if (saveB.disabled) saveB.removeAttribute("data-mut"); else saveB.setAttribute("data-mut", "");
    };
    sel.rx.addEventListener("change", syncDirty);
    sel.tx.addEventListener("change", syncDirty);
    const acts = h("div", { class: "tm-actions tm-left" }, saveB);
    const prev = a.available ? previousDevices(r.id, a) : null;
    if (prev) acts.append(btn("Use the devices this browser used before", { cls: "sm", icon: "history", on: () => {
      if (prev.rx) sel.rx.value = prev.rx;
      if (prev.tx) sel.tx.value = prev.tx;
      syncDirty();
    } }));
    if (pins.rx || pins.tx) acts.append(btn("Clear pins", { cls: "sm", icon: "x", mut: true, on: async () => {
      const res = await act(() => api("/api/admin/audio", { method: "PUT", json: { radio: r.id, rx: null, tx: null } }), r.name + ": sound-card pins cleared.");
      if (res.ok) { document.dispatchEvent(new CustomEvent("tr:refresh-status")); select("audio"); }
    } }));
    sec.append(h("div", { class: "tm-inline" }, unsaved), acts, audErr);
    if (isCur) sec.append(h("p", { class: "tm-hint tm-live", text:
      (a.rx_running ? "Capture running" : "Capture stopped") + " · " + (a.listeners || 0) + " listener" + (a.listeners === 1 ? "" : "s") +
      (a.tx_open ? " · TX audio open" : "") }));
    card.append(sec);
    syncDirty();
    return card;
  }

  // =====================================================================================================
  // Activity: live sessions (polled 15 s) + the audit log (§2.19)
  // =====================================================================================================
  let auditKind = "tx";
  const TX_EV = new Set(["tx_start", "tx_end", "tx_reattach", "tx_stop_forced", "tx_hb_timeout"]);
  // The on-air banner follows the LIVE owner from this tab's WebSocket (TR.tx, every tx frame), never
  // the 15 s sessions poll, so the STOP below can only ever act on the owner the admin is looking at.
  let onAir = null;                                 // {el, text, stop, stopLbl, owner, since, arm}
  const liveOwner = () => (TR.tx && TR.tx.owner) || null;
  function renderOnAir() {
    if (!onAir || !onAir.el.isConnected) return;
    const o = onAir.owner, el = onAir.el;
    onAir.arm.check(o);                             // the armed owner gone -> disarmed (I23)
    if (!o) { el.hidden = true; return; }
    el.hidden = false;
    const mine = TR.myConn != null && o.conn === TR.myConn;
    const who = o.external ? "FRONT PANEL" : (o.user || "someone");
    onAir.text.textContent = (mine ? "You are" : who) + " on air for " + mmss(performance.now() - onAir.since) +
                             (Array.isArray(o.claims) && o.claims.length ? " (" + o.claims.join(", ") + ")" : "");
    onAir.stop.hidden = mine || !TR.can("tx");
  }
  function setOwner(o) {
    if (!onAir) return;
    if (TR.ownerKey(o) !== TR.ownerKey(onAir.owner)) onAir.since = performance.now() - Math.max(0, +(o && o.elapsed_ms) || 0);
    onAir.owner = o;
    renderOnAir();
  }
  document.addEventListener("tr:tx", () => {
    if (onAir && onAir.el.isConnected) setOwner(liveOwner());
  });
  function onAirBanner() {
    if (onAir && onAir.arm) onAir.arm.disarm();      // a re-render: the old banner's arm ends with it
    const text = h("span", { class: "tm-onair-txt" });
    const stopLbl = h("span", { text: "Stop" });
    const stop = h("button", { type: "button", class: "ctl-btn danger sm", id: "admTxStop", "data-tx": true,
                               title: "Two taps: stop this transmission" }, TR.icon("octagon-x"), stopLbl);
    const el = h("div", { class: "tm-onair", role: "status", hidden: true }, h("span", { class: "onair-dot", "aria-hidden": "true" }), text, stop);
    // two taps (D-16, F18): the first arms for THIS owner for 3 s; the second sends tx_stop over the WS,
    // and only while that same owner still transmits (TR.stopArm: never a stale arm, I23)
    const arm = TR.stopArm((o) => {
      stop.classList.toggle("armed", !!o);
      stopLbl.textContent = o ? "Tap again to stop " + (o.external ? "FRONT PANEL" : o.user) : "Stop";
      if (o) TR.srLive("Tap Stop again within 3 seconds to stop " + (o.external ? "the front panel" : o.user));
    });
    onAir = { el, text, stop, stopLbl, owner: null, since: 0, arm };
    stop.addEventListener("click", () => {
      const o = liveOwner();                         // the owner right now, not the banner's last paint
      if (!o || (TR.myConn != null && o.conn === TR.myConn) || !TR.can("tx")) { arm.disarm(); return; }
      if (arm.tap(o) === "fire" && window.RadioControl) window.RadioControl.send({ action: "tx_stop" });
    });
    return el;
  }

  async function renderActivity(p, seq) {
    const sess = h("section", { class: "tm-sec", id: "admSessions" }, loading());
    const aud = h("section", { class: "tm-sec", id: "admAudit" });
    p.append(onAirBanner(), sess, aud);
    setOwner(liveOwner());
    await Promise.all([loadSessions(sess, seq), loadAudit(aud, seq)]);
    if (!alive(seq)) return;
    // the 15 s refresh waits while a "Sign out user" confirmation is open in the table
    timers.push(setInterval(() => { if (alive(seq) && !sess.querySelector(".tm-subrow")) loadSessions(sess, seq); }, 15000));
    timers.push(setInterval(() => { if (alive(seq)) renderOnAir(); }, 1000));
  }
  async function loadSessions(box, seq) {
    let j;
    try { j = await TR.api("/api/admin/sessions"); }
    catch (e) { if (alive(seq)) { box.textContent = ""; box.append(head3("SESSIONS"), h("div", { class: "tm-box err", text: errText(e) })); } return; }
    if (!alive(seq)) return;
    const rows = Array.isArray(j.sessions) ? j.sessions : [];
    box.textContent = "";
    box.append(head3("SESSIONS", rows.length + " open · refreshed every 15 s"));
    if (!rows.length) { box.append(h("p", { class: "tm-empty", text: "No open sessions." })); return; }
    const tb = h("tbody");
    for (const s of rows) {
      const here = TR.myConn != null && s.conn === TR.myConn;
      const mine = s.user === TR.user;
      const tr = h("tr", { dataset: { conn: String(s.conn) } });
      tr.append(
        h("td", { "data-label": "User" }, h("b", { text: s.user }), here ? chip("this tab", "me") : null),
        h("td", { "data-label": "Role", text: s.role }),
        h("td", { "data-label": "IP" }, h("code", { text: s.ip || "?" })),
        h("td", { "data-label": "Connected", title: stamp(s.since), text: rel(s.since) }),
        h("td", { "data-label": "Activity" }, h("span", { class: "tm-chips" },
          s.tx_owner ? chip("TX", "danger") : null, s.rx_audio ? chip("RX", "ok") : null, s.mic ? chip("MIC", "warn") : null,
          !s.tx_owner && !s.rx_audio && !s.mic ? h("span", { class: "tm-hint", text: "—" }) : null)),
        h("td", { "data-label": "Actions" },
          btn("Sign out user", { cls: "sm", icon: "log-out", disabled: mine,
            title: mine ? "Use Sign out everywhere in your user menu" : "Sign " + s.user + " out on every device",
            on: () => sub(tr, "signout", () => confirmPanel("Sign " + s.user + " out on every device? All their sessions end now.",
              "Sign out " + s.user, "log-out", () => api("/api/admin/users/" + uidOf(s.user) + "/signout", { method: "POST" }),
              (d) => s.user + " signed out (" + (d.kicked || 0) + " session" + (d.kicked === 1 ? "" : "s") + " closed).")) })));
      tb.append(tr);
    }
    box.append(h("table", { class: "tm-table tm-sessions" },
      h("thead", null, h("tr", null, ["User", "Role", "IP", "Connected", "Activity"].map((t) => h("th", { text: t })),
        h("th", null, h("span", { class: "sr-only", text: "Actions" })))), tb));
  }
  function txDetails(r) {
    const parts = [];
    if (r.kind) parts.push(r.kind);
    if (typeof r.freq === "number") parts.push((r.freq / 1e6).toFixed(4) + " MHz");
    if (r.mode) parts.push(r.mode);
    if (r.radio) parts.push(r.radio);
    if (typeof r.dur_s === "number") parts.push(r.dur_s < 60 ? r.dur_s.toFixed(1) + " s" : mmss(r.dur_s * 1000));
    if (r.reason) parts.push(r.reason);
    if (r.by) parts.push("stopped by " + r.by);
    if (r.owner && r.ev === "tx_stop_forced") parts.push("owner " + r.owner);
    if (r.ip) parts.push(r.ip);
    return parts.join(" · ");
  }
  function genericDetails(r) {
    const skip = new Set(["ts", "ev", "user", "key"]);
    return Object.keys(r).filter((k) => !skip.has(k)).map((k) => {
      const v = r[k];
      return k + " " + (v && typeof v === "object" ? JSON.stringify(v) : String(v));
    }).join(" · ");
  }
  async function loadAudit(box, seq) {
    box.textContent = "";
    const seg = h("div", { class: "tm-seg", role: "radiogroup", "aria-label": "Audit log" },
      [["tx", "TX"], ["security", "Security"], ["all", "All"]].map(([k, l]) =>
        h("button", { type: "button", role: "radio", "aria-checked": auditKind === k ? "true" : "false", class: auditKind === k ? "on" : null,
                      text: l, dataset: { kind: k }, on: { click: () => { auditKind = k; loadAudit(box, seq); } } })));
    const dl = h("a", { class: "ctl-btn sm", id: "admAuditDownload", href: "/api/admin/audit?kind=" + auditKind + "&format=jsonl",
                        download: "telerig-audit.jsonl", title: "Download the " + auditKind + " log as JSON Lines" }, TR.icon("arrow-down"), "Download");
    box.append(head3("AUDIT LOG", auditKind === "tx" ? "TX accountability (audit-tx.log)" : (auditKind === "security" ? "security events (audit.log)" : "both logs")),
      h("div", { class: "tm-toolbar" }, seg, btn("Refresh", { cls: "sm", icon: "rotate-ccw", on: () => loadAudit(box, seq) }), dl));
    const body = h("div", null, loading());
    box.append(body);
    let j;
    try { j = await TR.api("/api/admin/audit?kind=" + encodeURIComponent(auditKind) + "&limit=200"); }
    catch (e) { if (alive(seq)) { body.textContent = ""; body.append(h("div", { class: "tm-box err", text: errText(e) })); } return; }
    if (!alive(seq) || !box.isConnected) return;
    body.textContent = "";
    const rows = Array.isArray(j.entries) ? j.entries : [];
    if (j.dropped) body.append(h("div", { class: "tm-box warn", text: j.dropped + " audit entries were dropped since the server started (the writer queue was full)." }));
    if (!rows.length) { body.append(h("p", { class: "tm-empty", text: "No entries yet." })); return; }
    const tb = h("tbody");
    for (const r of rows) {
      tb.append(h("tr", { class: TX_EV.has(r.ev) ? "tm-txrow" : null },
        h("td", { "data-label": "Time", class: "tm-time", title: String(r.ts || ""), text: stamp(r.ts) }),
        h("td", { "data-label": "Event" }, h("span", { class: "tm-ev", text: String(r.ev || "") }), r.count > 1 ? h("span", { class: "tm-hint", text: " ×" + r.count }) : null),
        h("td", { "data-label": "User", text: r.user ? String(r.user)          // the account; else who did it
          : (Array.isArray(r.users) ? r.users.join(", ") : (r.by ? String(r.by) : "—")) }),
        h("td", { "data-label": "Details", class: "tm-details", text: TX_EV.has(r.ev) ? txDetails(r) : genericDetails(r) })));
    }
    body.append(h("table", { class: "tm-table tm-audit" },
      h("thead", null, h("tr", null, ["Time", "Event", "User", "Details"].map((t) => h("th", { text: t })))), tb));
  }

  // =====================================================================================================
  // Lockouts (§2.15 F7: user keys K2 + K3, source keys K1)
  // =====================================================================================================
  async function refreshLockNotice() {
    try {
      const j = await TR.api("/api/admin/lockouts", { noRedirect: true, quiet: true });
      if (!(Array.isArray(j.users) && j.users.length)) dropNotice((k) => k === "lockouts");
    } catch (_) {}
  }
  async function renderLockouts(p, seq) {
    p.append(loading());
    const j = await TR.api("/api/admin/lockouts");
    if (!alive(seq)) return;
    p.textContent = "";
    const users = Array.isArray(j.users) ? j.users : [], ips = Array.isArray(j.ips) ? j.ips : [];
    p.append(h("div", { class: "tm-toolbar" }, btn("Refresh", { cls: "sm", icon: "rotate-ccw", on: () => select("lockouts") }),
      h("span", { class: "tm-hint", text: "Repeated failed sign-ins lock an account or a source for a while. Recovery: sign in on the host PC, from a known device, after an unlock here, or with --reset-admin." })));
    if (!users.length && !ips.length) {
      p.append(h("p", { class: "tm-empty", id: "admNoLockouts", text: "No lockouts." }));
      dropNotice((k) => k === "lockouts");
      return;
    }
    if (users.length) {
      const tb = h("tbody");
      for (const u of users) tb.append(h("tr", { dataset: { key: u.key } },
        h("td", { "data-label": "Account" }, h("b", { text: u.key })),
        h("td", { "data-label": "Failures", text: u.count ? String(u.count) : "—",
                  title: "failures still in the 15-minute window (the window restarts when the lock trips)" }),
        h("td", { "data-label": "Until", text: hhmm(u.until) }),
        h("td", { "data-label": "Lock", text: "#" + ((u.level | 0) + 1),
                  title: "repeat locks last longer: 15, 30, 60, 120, then 240 minutes" }),
        h("td", { "data-label": "Actions" }, btn("Unlock", { cls: "sm", icon: "key-round", mut: true, on: async () => {
          const r = await act(() => api("/api/admin/users/" + uidOf(u.key) + "/unlock", { method: "POST" }), u.key + " unlocked.");
          if (r.ok) { refreshLockNotice(); select("lockouts"); }
        } }))));
      p.append(h("section", { class: "tm-sec" }, head3("ACCOUNTS", "locked after many failures from several sources"),
        h("table", { class: "tm-table tm-lock-users" },
          h("thead", null, h("tr", null, ["Account", "Failures", "Until", "Lock"].map((t) => h("th", { text: t })), h("th", null, h("span", { class: "sr-only", text: "Actions" })))),
          tb)));
    }
    if (ips.length) {
      const tb = h("tbody");
      for (const s of ips) tb.append(h("tr", { dataset: { key: s.key } },
        h("td", { "data-label": "Source" }, h("code", { text: s.key })),
        h("td", { "data-label": "Failures", text: String(s.count) }),
        h("td", { "data-label": "Until", text: hhmm(s.until) }),
        h("td", { "data-label": "Actions" }, btn("Unlock", { cls: "sm", icon: "key-round", mut: true, on: async () => {
          const r = await act(() => api("/api/admin/lockouts/unlock_ip", { method: "POST", json: { ip: s.key } }), s.key + " unlocked.");
          if (r.ok) select("lockouts");
        } }))));
      p.append(h("section", { class: "tm-sec" }, head3("SOURCES", "IP addresses (or this PC) with too many failures"),
        h("table", { class: "tm-table tm-lock-ips" },
          h("thead", null, h("tr", null, ["Source", "Failures", "Until"].map((t) => h("th", { text: t })), h("th", null, h("span", { class: "sr-only", text: "Actions" })))),
          tb)));
    }
  }

  // ---- WSJT-X bridge (docs/WSJTX-BRIDGE.md, backend/bridge.py) -------------------------------------------
  // GET / PUT /api/admin/bridge. The server validates and decides everything; this form only asks.
  const BR_PAUSE = { stopped: "stopped", operator: "held", timeout: "the 120 s limit was reached",
                     heartbeat: "the program stopped talking to TeleRig", admin_switch: "an admin switched the radio",
                     server_locked: "the server was locked" };
  function brAgo(secs) {
    const s = Math.max(0, +secs || 0);
    return s < 90 ? s + " s" : s < 5400 ? Math.round(s / 60) + " min" : Math.round(s / 3600) + " h";
  }
  function bridgeStatus(box, st, cfg) {
    box.textContent = "";
    box.append(head3("STATUS"));
    if (!st) { box.append(note("No status.")); return; }
    const chips = h("div", { class: "tm-chips" });
    if (!st.enabled) chips.append(chip("off", null));
    else if (st.listening) chips.append(chip("listening on port " + st.port, "ok"),
                                        chip(st.listen === "network" ? "this PC + allowed computers" : "this PC only", "info"));
    else chips.append(chip("not listening", "danger"));
    if (st.enabled) chips.append(chip(st.ptt ? "transmit allowed" : "receive only", st.ptt ? "warn" : null));
    if (st.on_air) chips.append(chip("ON AIR", "danger"));
    if (st.paused) chips.append(chip("transmit paused", "warn"));
    box.append(chips);
    if (st.enabled && !st.listening)
      box.append(h("div", { class: "tm-box err", role: "alert", text: st.error_text || "The bridge port could not be opened." }));
    if (st.paused) {
      const why = (BR_PAUSE[st.paused.reason] || "paused") + (st.paused.by ? " by " + st.paused.by : "");
      box.append(h("div", { class: "tm-box warn", role: "status" },
        h("span", { text: "Transmit over the bridge is paused (" + why + "). The program cannot key until it is resumed. " }),
        btn("Resume transmit", { icon: "tr-play", cls: "sm", id: "admBrResume", on: () => {
          const rc = window.RadioControl;
          if (rc && rc.send({ action: "bridge_tx", on: true })) say("Transmit over the bridge resumed.", true);
          else say("Not connected to the server — try again in a moment.", false);
        } })));
    }
    const peers = st.peers || [];
    if (st.enabled && st.listening) {
      if (!peers.length) box.append(note("No program is connected."));
      else {
        const tb = h("table", { class: "tm-table", id: "admBrPeers" },
          h("thead", null, h("tr", null, h("th", { text: "Program at" }), h("th", { text: "Connected" }),
                                         h("th", { text: "Commands" }), h("th", { text: "" }))));
        const body = h("tbody");
        for (const x of peers)
          body.append(h("tr", null, h("td", null, h("code", { text: x.ip })), h("td", { text: brAgo(x.secs) }),
                        h("td", { text: String(x.cmds) }), h("td", null, x.tx ? chip("transmitting", "danger") : null)));
        tb.append(body);
        box.append(tb);
      }
      const where = st.listen === "network" ? "<this PC's address>" : "localhost";
      box.append(h("p", { class: "tm-note" },
        "In WSJT-X (File > Settings > Radio): Rig ", h("code", { text: "Hamlib NET rigctl" }),
        ", Network Server ", h("code", { text: where + ":" + st.port }),
        ", PTT Method ", h("code", { text: st.ptt ? "CAT" : "VOX (transmit over the bridge is off)" }),
        ", Mode ", h("code", { text: "Data/Pkt" }), ", Split Operation ", h("code", { text: "Fake It" }), " or None."));
    }
  }
  async function renderBridge(p, seq) {
    p.append(loading("Reading the bridge settings…"));
    const j = await TR.api("/api/admin/bridge");
    if (!alive(seq)) return;
    p.textContent = "";
    const c = j.config || {}, lim = j.limits || {};
    const portLim = lim.port || [1024, 65535], silLim = lim.silence_s || [3, 120];
    p.append(note("The bridge lets WSJT-X, JS8Call, Fldigi or a logging program share the radio TeleRig is connected to, " +
                  "including an Icom on its LAN port. The program connects to TeleRig as if it were Hamlib's rigctld; " +
                  "TeleRig keeps the radio and applies the same rules as for a browser: one transmitter at a time, STOP, the 120 s limit."));
    const stBox = h("section", { class: "tm-sec", id: "admBrStatus" });
    bridgeStatus(stBox, j.status, c);
    p.append(stBox);
    timers.push(setInterval(async () => {
      try {
        const jj = await TR.api("/api/admin/bridge", { quiet: true });
        if (alive(seq) && cur === "bridge") bridgeStatus(stBox, jj.status, jj.config || c);
      } catch (_) {}
    }, 3000));

    // ---- settings ----
    const on = h("input", { type: "checkbox", id: "admBrOn", checked: !!c.enabled });
    const port = h("input", { type: "number", id: "admBrPort", class: "tm-input tm-short", min: String(portLim[0]), max: String(portLim[1]),
                              step: "1", value: String(c.port), inputmode: "numeric", "aria-describedby": "admBrPortHint" });
    const loc = h("input", { type: "radio", name: "admBrListen", id: "admBrLocal", value: "local", checked: c.listen !== "network" });
    const net = h("input", { type: "radio", name: "admBrListen", id: "admBrNet", value: "network", checked: c.listen === "network" });
    const allow = h("textarea", { id: "admBrAllow", class: "tm-input tm-area", rows: "3", spellcheck: "false", autocomplete: "off",
                                  placeholder: "one per line, e.g.\n100.101.102.103\n192.168.1.0/24", "aria-label": "Computers that may connect",
                                  value: (c.allow || []).join("\n") });
    const allowWrap = h("div", { class: "tm-field", id: "admBrAllowWrap" },
      h("label", { class: "tm-label", for: "admBrAllow" }, "Computers that may connect",
        h("span", { class: "tm-hint", text: " IP addresses or networks; this PC is always allowed" })),
      allow,
      h("div", { class: "tm-box warn", role: "note", text: "The rigctl protocol has no password: every program on a listed computer can tune this radio " +
        "(and key it, if transmit is allowed below) without signing in to TeleRig. List only computers you control, " +
        "and prefer a private network such as Tailscale." }));
    const ptt = h("input", { type: "checkbox", id: "admBrPtt", checked: !!c.ptt });
    const pttWarn = h("div", { class: "tm-box warn", role: "note", id: "admBrPttWarn", text:
      "With this on, a connected program keys the radio by itself (WSJT-X transmits on its own schedule once you enable Tx there). " +
      "You are the control operator: stay at the station. TeleRig still shows ON AIR to everyone, any operator can STOP it " +
      "(which also pauses the bridge until someone resumes it), and the 120 s limit and the silence limit below apply." });
    const sil = h("input", { type: "number", id: "admBrSilence", class: "tm-input tm-short", min: String(silLim[0]), max: String(silLim[1]),
                             step: "1", value: String(c.silence_s), inputmode: "numeric" });
    const ferr = h("div", { class: "tm-ferr", id: "admBrErr", role: "alert", hidden: true });
    const sync = () => { allowWrap.hidden = !net.checked; pttWarn.hidden = !ptt.checked; };
    for (const x of [loc, net, ptt]) x.addEventListener("change", sync);
    sync();
    const lines = (t) => String(t || "").split(/[\s,;]+/).map((s) => s.trim()).filter(Boolean);
    const num = (el) => { const v = String(el.value).trim(); return /^\d+$/.test(v) ? parseInt(v, 10) : v; };
    const FIELD = { enabled: on, port, listen: loc, allow, ptt, silence_s: sil };
    const save = btn("Save", { icon: "check", cls: "primary", id: "admBrSave", mut: true, on: async () => {
      ferr.hidden = true;
      // `expect`: the settings this form was showing. If they changed meanwhile (another admin, a hand
      // edit), the server saves nothing and answers 409 conflict; the tab then shows the saved ones.
      const body = { enabled: on.checked, port: num(port), listen: net.checked ? "network" : "local",
                     allow: lines(allow.value), ptt: ptt.checked, silence_s: num(sil), expect: c };
      if (body.ptt && !c.ptt) {
        const yes = await TR.confirm("Allow programs to transmit through the bridge? A connected program then keys the radio on its own schedule.",
                                     { ok: "Allow transmit", danger: true });
        if (!yes) { say("Cancelled — nothing changed.", false); return; }
      }
      const res = await act(() => api("/api/admin/bridge", { method: "PUT", json: body }), "Bridge settings saved.");
      if (res.ok) { document.dispatchEvent(new CustomEvent("tr:refresh-status")); select("bridge"); return; }
      if (res.err && res.err.code === "conflict") { select("bridge"); say(errText(res.err), false); return; }
      if (res.err && res.err.code !== "cancelled") {
        ferr.textContent = errText(res.err);
        ferr.hidden = false;
        const f = FIELD[res.err.field];
        if (f && typeof f.focus === "function") f.focus();
      }
    } });
    p.append(h("section", { class: "tm-sec" }, head3("SETTINGS", "saved in config.json; applied at once"),
      h("label", { class: "tm-check1", for: "admBrOn" }, on, h("b", { text: "Bridge on" })),
      h("div", { class: "tm-field" },
        h("label", { class: "tm-label", for: "admBrPort" }, "Port", h("span", { class: "tm-hint", id: "admBrPortHint", text: " rigctld's standard port is 4532, which is also WSJT-X's default" })),
        port),
      h("div", { class: "tm-field", role: "radiogroup", "aria-label": "Who may connect" },
        h("span", { class: "tm-label", text: "Who may connect" }),
        h("label", { class: "tm-radio", for: "admBrLocal" }, loc, "Programs on this PC only"),
        h("label", { class: "tm-radio", for: "admBrNet" }, net, "Also the computers listed below")),
      allowWrap,
      h("label", { class: "tm-check1", for: "admBrPtt" }, ptt, h("b", { text: "Allow transmit (PTT) through the bridge" })),
      pttWarn,
      h("div", { class: "tm-field" },
        h("label", { class: "tm-label", for: "admBrSilence" }, "Silence limit, seconds",
          h("span", { class: "tm-hint", text: " a keyed program that sends TeleRig nothing for this long is unkeyed (WSJT-X polls every second)" })),
        sil),
      ferr, h("div", { class: "tm-actions tm-left" }, save)));
    p.append(note("Not offered over the bridge: split with a separate transmit VFO (use WSJT-X's “Fake It”), RIT, memories. " +
                  "Audio does not go through the bridge: the program uses the radio's USB sound card as usual."));
  }

  // ---- Update (backend/updater.py) ------------------------------------------------------------------------
  // GET /api/admin/update; POST /api/admin/update/check (fetch + compare, changes nothing); POST
  // /api/admin/update (sudo). The server updates itself from GitHub (this install's own branch: master
  // or dev) and restarts; this tab follows the steps, then waits for the new server (a new boot id) and
  // reloads the page onto it.
  const UP_STEP = { fetch: "Fetch from GitHub", check: "Check", update: "Update the files", packages: "Packages",
                    "self-test": "Self-test of the new version", rollback: "Back to the previous version",
                    restart: "Restart", error: "Error" };
  function upWhen(iso) {
    const t = Date.parse(iso || "");
    return isNaN(t) ? String(iso || "") : new Date(t).toLocaleString();
  }
  function upSteps(job) {
    const ul = h("ul", { class: "up-steps", id: "admUpSteps" });
    for (const s of (job && job.steps) || []) {
      ul.append(h("li", { class: s.ok ? "ok" : "bad" }, TR.icon(s.ok ? "check" : "triangle-alert"),
                  h("b", { text: UP_STEP[s.step] || s.step }), s.text ? h("span", { class: "up-t", text: s.text }) : null));
    }
    return ul;
  }
  async function renderUpdate(p, seq) {
    p.append(loading("Reading this install…"));
    const j = await TR.api("/api/admin/update");
    if (!alive(seq)) return;
    p.textContent = "";
    const info = j.info || {}, job = j.job || {}, boot0 = j.boot;
    p.append(note("Update TeleRig from GitHub without going to its PC. The install follows its own branch: " +
                  "a master install updates from master (the releases), a dev install from dev."));
    const tb = h("tbody");
    function showInfo(inf, version) {
      tb.textContent = "";
      const row = (k, v) => tb.append(h("tr", null, h("th", { text: k }), h("td", null, v)));
      row("Version", "v" + (version || "?"));
      row("Branch", inf.branch ? inf.branch + (inf.branch === "dev" ? " (development)" : " (releases)") : "—");
      row("Commit", inf.short ? h("span", { id: "admUpCommit" }, h("code", { text: inf.short }), " · " + (inf.subject || "") +
                                      (inf.date ? " · " + upWhen(inf.date) : "")) : "—");
    }
    showInfo(info, j.version);
    p.append(h("section", { class: "tm-sec", id: "admUpInfo" }, head3("THIS INSTALL"),
               h("table", { class: "tm-table tm-kv" }, tb)));
    if (!info.updatable) {
      p.append(h("div", { class: "tm-box warn", role: "status", id: "admUpWhy", text: info.reason || "This install cannot update itself." }));
      return;
    }
    const listBox = h("div", { id: "admUpList" });
    const progress = h("div", { id: "admUpProgress" });
    const goBtn = btn("Update now", { icon: "arrow-up", cls: "primary", id: "admUpGo", mut: true, disabled: true, on: () => go() });
    const checkBtn = btn("Check for updates", { icon: "rotate-ccw", id: "admUpCheck", on: () => check() });
    p.append(h("section", { class: "tm-sec", id: "admUpSec" }, head3("UPDATES", "from GitHub's " + info.branch),
      listBox, h("div", { class: "tm-actions tm-left" }, checkBtn, goBtn),
      note("Update now refuses while anyone is transmitting. TeleRig then restarts: everyone is disconnected for " +
           "about half a minute, and the radio reconnects when the page loads again."),
      progress));
    let last = j.check || null;
    function showCheck(c) {
      listBox.textContent = "";
      goBtn.disabled = true;
      if (!c) { listBox.append(note("Not checked yet.")); return; }
      if (!c.behind) {
        listBox.append(h("p", { class: "tm-note", id: "admUpCurrent" }, chip("up to date", "ok"),
                         " Nothing new on GitHub's " + c.branch + " (checked " + rel(new Date(c.at * 1000).toISOString()) + ")."));
        return;
      }
      if (c.ahead) {
        listBox.append(h("div", { class: "tm-box warn", role: "status", text: "This install has commits that GitHub's " + c.branch +
                                  " does not have, so it cannot update itself. Update it by hand." }));
        return;
      }
      const body = h("tbody");
      for (const x of c.commits || [])
        body.append(h("tr", null, h("td", null, h("code", { text: x.short })), h("td", { text: x.subject }), h("td", { text: upWhen(x.date) })));
      listBox.append(h("p", { class: "tm-note" }, chip(c.behind + (c.behind === 1 ? " new commit" : " new commits"), "info"),
                       " on GitHub's " + c.branch + " (" + c.target_short + ")."),
                     h("table", { class: "tm-table", id: "admUpCommits" },
                       h("thead", null, h("tr", null, h("th", { text: "Commit" }), h("th", { text: "What changed" }), h("th", { text: "When" }))), body));
      goBtn.disabled = false;
    }
    async function check() {
      const res = await act(() => api("/api/admin/update/check", { method: "POST", json: {} }));
      if (!alive(seq)) return;
      if (res.ok) { last = res.data.check; showCheck(last); }
    }
    async function go() {
      if (!last || !last.behind) return;
      const yes = await TR.confirm("Update TeleRig to " + last.target_short + " from GitHub's " + last.branch + " and restart it? " +
                                   "Everyone is disconnected for about half a minute.", { ok: "Update now", danger: true });
      if (!yes) { say("Cancelled — nothing changed.", false); return; }
      const res = await act(() => api("/api/admin/update", { method: "POST", json: {} }));
      if (!alive(seq) || !res.ok) return;
      follow();
    }
    // Follow a running update until it ends here, or until the restarted server answers.
    let following = false;
    function follow() {
      if (following) return;
      following = true;
      checkBtn.disabled = goBtn.disabled = true;
      let down = false;
      const tick = async () => {
        let s = null;
        try { s = await TR.api("/api/admin/update", { quiet: true }); } catch (_) { s = null; }
        if (!alive(seq) || cur !== "update") return;
        if (!s) { down = true; progress.textContent = ""; progress.append(note("TeleRig is restarting…")); return; }
        if (s.boot && s.boot !== boot0) {
          clearInterval(t);
          progress.textContent = "";
          progress.append(h("div", { class: "tm-box ok", role: "status", id: "admUpDone",
            text: "Updated and restarted: TeleRig now runs " + (((s.info || {}).short) || "the new version") + ". Reloading…" }));
          setTimeout(() => { try { window.location.reload(); } catch (_) {} }, 1500);
          return;
        }
        const st = (s.job || {}).state;
        progress.textContent = "";
        put(progress, upSteps(s.job), st === "restarting" || down ? note("Restarting TeleRig…") : null);
        if (st === "failed" || st === "current" || st === "updated") {
          clearInterval(t);
          following = false;
          checkBtn.disabled = false;
          const msg = st === "failed" ? (s.job.error || "The update failed.") :
                      st === "current" ? "Already up to date." : "Updated. Restart TeleRig on its PC to run the new version.";
          progress.append(h("div", { class: "tm-box " + (st === "failed" ? "err" : "ok"), role: "status", id: "admUpEnd", text: msg }));
          last = s.check || last;
          showCheck(last);
          if (st !== "failed" && s.info) showInfo(s.info, s.version);   // the files are at the new commit
        }
      };
      const t = setInterval(tick, 1000);
      timers.push(t);
      tick();
    }
    showCheck(last);
    if (job.state === "running" || job.state === "restarting") follow();      // already under way (another admin)
    else if (job.steps && job.steps.length) progress.append(upSteps(job));
  }

  const RENDER = { users: renderUsers, access: renderAccess, remote: renderRemote, audio: renderAudio,
                   bridge: renderBridge, activity: renderActivity, lockouts: renderLockouts, update: renderUpdate };

  // ---- entry points --------------------------------------------------------------------------------------
  window.TRAdmin = { open, close, isOpen: () => !!(M && M.isOpen()) };
  const bind = (id) => { const el = $(id); if (el) { el.hidden = false; el.addEventListener("click", () => open()); } };
  bind("adminBtn");
  bind("umAdmin");
  // §4.5 notice buttons (tr.js renders them at DOMContentLoaded, after this script has run)
  TR.noticeActions.lockouts = { label: "Lockouts", fn: () => open("lockouts") };
  TR.noticeActions.behind_proxy_stale = { label: "Clear flag", fn: () => open("remote", { clearFlag: true }) };
  TR.noticeActions.audio_default = { label: "Radio & audio", fn: () => open("audio") };
  if (Array.isArray(TR.notices) && TR.notices.length) TR.renderNotices();   // already rendered: add the buttons
})();
