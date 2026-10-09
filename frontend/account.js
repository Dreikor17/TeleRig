/* TeleRig — account: the user menu's account items and the voluntary change-password modal (spec §5.2,
 * §5.3, §2.16). Loads after app.js / cwtool.js and before admin.js.
 *
 * Also provides the small UI kit both modals use (admin.js builds on it):
 *   TR.h(tag, props, ...children)   DOM builder; text goes in with textContent only (never innerHTML)
 *   TR.modal(opts)                  a modal shell in #modalRoot: role=dialog + aria-modal, focus trap,
 *                                   Escape / backdrop close, focus returned to the opener
 *   TR.txBlocksModal()              true while this tab owns TX or has a claim pending: a modal would
 *                                   cover the top-bar STOP, so none opens (and an open one closes)
 *
 * User menu (§5.3): Change password… (this modal), Sign out everywhere (inline confirm ->
 * POST /api/me/signout_all), Sign out (POST /api/logout). Both sign-outs land on
 * /login?reason=signed_out. The server is the authority for every rule (F17: a migrated shared-password
 * session gets 403 migrated_local_only, shown as the server's own message).
 */
(function () {
  "use strict";
  const TR = window.TR;
  if (!TR) return;
  const $ = (id) => document.getElementById(id);

  // ---- DOM builder ------------------------------------------------------------------------------
  // props: class, text, on:{event:fn}, dataset:{}, and the properties below; everything else is an
  // attribute (true -> present, false/null -> absent). Children: nodes or strings (text nodes).
  const PROPS = new Set(["disabled", "hidden", "checked", "readOnly", "tabIndex", "open"]);
  TR.h = function h(tag, props) {
    const el = document.createElement(tag);
    let value;
    if (props) {
      for (const k of Object.keys(props)) {
        const v = props[k];
        if (v === null || v === undefined || v === false) continue;
        if (k === "class") el.className = v;
        else if (k === "text") el.textContent = String(v);
        else if (k === "on") { for (const ev of Object.keys(v)) el.addEventListener(ev, v[ev]); }
        else if (k === "dataset") Object.assign(el.dataset, v);
        else if (k === "value") value = v;
        else if (PROPS.has(k)) el[k] = v;
        else el.setAttribute(k, v === true ? "" : String(v));
      }
    }
    for (let i = 2; i < arguments.length; i++) append(el, arguments[i]);
    if (value !== undefined) el.value = value;           // after the <option>s exist
    return el;
  };
  function append(el, c) {
    if (c === null || c === undefined || c === false) return;
    if (Array.isArray(c)) { for (const x of c) append(el, x); return; }
    el.append(c instanceof Node ? c : String(c));
  }
  const h = TR.h;

  // ---- TX guard for modals ----------------------------------------------------------------------
  TR.txBlocksModal = function () {
    try { return !!(window.RadioTx && window.RadioTx.ownedOrPending && window.RadioTx.ownedOrPending()); }
    catch (_) { return false; }
  };

  // ---- modal shell ------------------------------------------------------------------------------
  const openModals = [];
  function visible(el) { return !!el && el.isConnected && el.getClientRects().length > 0; }
  TR.modal = function (opts) {
    opts = opts || {};
    const root = $("modalRoot") || document.body;
    const titleId = (opts.id || "trm") + "Title";
    const dialog = h("div", { class: "tr-modal" + (opts.className ? " " + opts.className : ""), role: "dialog",
                              "aria-modal": "true", "aria-labelledby": titleId, tabindex: "-1" });
    const overlay = h("div", { class: "tr-modal-overlay", hidden: true }, dialog);
    root.appendChild(overlay);
    let returnTo = null;
    const m = {
      overlay, dialog, titleId,
      isOpen: () => !overlay.hidden,
      open() {
        if (!overlay.hidden) return;
        returnTo = document.activeElement;
        overlay.hidden = false;
        openModals.push(m);
        document.body.classList.add("tr-modal-open");
      },
      close() {
        if (overlay.hidden) return;
        overlay.hidden = true;
        const i = openModals.indexOf(m);
        if (i >= 0) openModals.splice(i, 1);
        if (!openModals.length) document.body.classList.remove("tr-modal-open");
        try { if (opts.onClose) opts.onClose(); } catch (_) {}
        const back = (opts.returnFocus && opts.returnFocus()) || returnTo;
        if (visible(back) && typeof back.focus === "function") back.focus();
      },
      // focusable controls of the active layer (a sub-layer such as admin.js's sudo prompt makes
      // the rest of the dialog inert, so it drops out of this list on its own)
      focusables() {
        // a[href] only: the icons' SVG <use href> must never count as focusable
        return [...dialog.querySelectorAll('button, a[href], input, select, textarea, summary, [tabindex]:not([tabindex="-1"])')]
          .filter((el) => !el.disabled && !el.closest("[inert]") && visible(el));
      },
      escape(e) { if (opts.onEscape) opts.onEscape(e); else m.close(); },
    };
    // a click that starts AND ends on the backdrop (outside the dialog) closes
    let downOnBackdrop = false;
    overlay.addEventListener("pointerdown", (e) => { downOnBackdrop = e.target === overlay; });
    overlay.addEventListener("click", (e) => { if (e.target === overlay && downOnBackdrop) m.escape(e); });
    return m;
  };
  // Escape and the Tab trap, for whichever modal is on top. Capture phase on the document, so the
  // page's own Escape handlers (radio help, user menu) never see a key meant for the modal.
  document.addEventListener("keydown", (e) => {
    const m = openModals[openModals.length - 1];
    if (!m) return;
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); m.escape(e); return; }
    if (e.key !== "Tab") return;
    const list = m.focusables();
    if (!list.length) { e.preventDefault(); m.dialog.focus(); return; }
    const first = list[0], last = list[list.length - 1], a = document.activeElement;
    const inside = m.dialog.contains(a) && list.includes(a);
    if (e.shiftKey && (a === first || !inside)) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && (a === last || !inside)) { e.preventDefault(); first.focus(); }
  }, true);
  // Safety net: this tab became the TX owner while a modal is open (the backdrop would cover the
  // top-bar STOP) -> close it at once.
  document.addEventListener("tr:tx", () => {
    if (!openModals.length || !TR.isTxOwner()) return;
    for (const m of openModals.slice().reverse()) m.close();
    TR.toast("Closed the dialog — you are transmitting.", "warn");
    TR.srLive("Dialog closed: you are transmitting");
  });

  // ---- sign out (§5.3, §2.16) -------------------------------------------------------------------
  let leaving = false;
  async function leaveVia(url, json) {
    if (leaving) return false;
    leaving = true;
    TR._leaving = true;                    // the 4401 close that follows is ours: tr.js must not handle it
    try {
      await TR.api(url, { method: "POST", json: json || {}, noRedirect: true, quiet: true });
    } catch (e) {
      if (e.status === 0 || (e.status === 403 && e.code === "migrated_local_only") || e.status >= 500) {
        leaving = false; TR._leaving = false;           // nothing happened: stay here
        TR.toast(e.message || "Could not sign out.", "error");
        return false;
      }
      // 401: the session had already ended. Either way it is over now.
    }
    location.replace("/login?reason=signed_out");
    return true;
  }
  function signOut() { leaveVia("/api/logout"); }
  async function signOutEverywhere() {
    const ok = await TR.confirm("Sign out everywhere? Every TeleRig session of " + (TR.user || "this account") +
                                " ends, on every device — including this one.", { ok: "Sign out everywhere", danger: true });
    if (ok) leaveVia("/api/me/signout_all");
  }

  // ---- change password (voluntary, §5.2) ----------------------------------------------------------
  const enc = window.TextEncoder ? new TextEncoder() : null;
  const bytes = (s) => (enc ? enc.encode(s).length : unescape(encodeURIComponent(s)).length);
  let cp = null;                           // the modal, built on first open

  function buildChangePassword() {
    const m = TR.modal({ id: "cp", className: "tm-narrow", returnFocus: () => $("userBtn"),
                         onEscape: () => { if (!busy) m.close(); }, onClose: () => clear() });
    let busy = false, lockedUntil = 0, lockTimer = 0;
    const field = (id, label, auto, hint) => {
      const input = h("input", { id, type: "password", class: "tm-input", autocomplete: auto, maxlength: "128",
                                 spellcheck: "false", "aria-describedby": id + "Err" });
      const err = h("div", { class: "tm-ferr", id: id + "Err", hidden: true });
      return { input, err, el: h("div", { class: "tm-field" },
        h("label", { class: "tm-label", for: id }, label, hint ? h("span", { class: "tm-hint", text: " " + hint }) : null),
        input, err) };
    };
    const cur = field("cpCur", "Current password", "current-password");
    const nw = field("cpNew", "New password", "new-password", "8–72 characters");
    const rep = field("cpRep", "Confirm new password", "new-password");
    const box = h("div", { class: "tm-box err", role: "alert", hidden: true });
    const item = (t) => h("li", { text: "○ " + t });
    const cLen = item("8–72 bytes"), cMatch = item("Matches"), cDiff = item("Different from the current password");
    const btn = h("button", { type: "submit", class: "ctl-btn primary", disabled: true }, TR.icon("key-round"), "Change password");
    const cancel = h("button", { type: "button", class: "ctl-btn", text: "Cancel", on: { click: () => { if (!busy) m.close(); } } });
    const form = h("form", { class: "tm-form", novalidate: true },
      h("p", { class: "tm-note", text: "Choose a new password for " + (TR.user || "your account") +
                                        ". Your other devices are signed out when it changes." }),
      box, cur.el, nw.el, rep.el,
      h("ul", { class: "tm-check", "aria-live": "polite" }, cLen, cMatch, cDiff),
      h("div", { class: "tm-actions" }, cancel, btn));
    m.dialog.append(
      h("header", { class: "tm-head" },
        h("span", { class: "tm-chip" }, TR.icon("key-round"), h("b", { id: m.titleId, text: "CHANGE PASSWORD" })),
        h("button", { type: "button", class: "tm-x", title: "Close", "aria-label": "Close", on: { click: () => { if (!busy) m.close(); } } },
          TR.icon("x"))),
      h("div", { class: "tm-body" }, form));

    function mark(li, ok, text) { li.className = ok ? "ok" : ""; li.textContent = (ok ? "✓ " : "○ ") + text; }
    function problem() {                               // client checks, in the §5.2 order
      const n = bytes(nw.input.value);
      const ok = { len: n >= 8 && n <= 72, match: nw.input.value !== "" && nw.input.value === rep.input.value,
                   diff: nw.input.value !== "" && nw.input.value !== cur.input.value };
      mark(cLen, ok.len, "8–72 bytes"); mark(cMatch, ok.match, "Matches");
      mark(cDiff, ok.diff, "Different from the current password");
      if (n < 8) return "The new password needs at least 8 bytes.";
      if (n > 72) return "The new password can be at most 72 bytes.";
      if (!ok.match) return "The two new passwords don't match.";
      if (!cur.input.value) return "Enter your current password.";
      if (!ok.diff) return "Choose a password that is different from the current one.";
      return null;
    }
    function refresh() { btn.disabled = busy || Date.now() < lockedUntil || problem() !== null; }
    for (const f of [cur, nw, rep]) f.input.addEventListener("input", refresh);
    function hideErrors() { box.hidden = true; cur.err.hidden = true; nw.err.hidden = true; rep.err.hidden = true; }
    function fieldErr(f, msg) { f.err.textContent = msg; f.err.hidden = false; f.input.focus(); }
    function showBox(msg) { box.textContent = msg; box.hidden = false; }
    function startLock(sec) {
      lockedUntil = Date.now() + Math.max(1, +sec || 60) * 1000;
      clearInterval(lockTimer);
      const tick = () => {
        const left = Math.max(0, Math.ceil((lockedUntil - Date.now()) / 1000));
        if (left <= 0) { clearInterval(lockTimer); box.hidden = true; TR.srLive("You can try again now."); refresh(); return; }
        showBox("Too many attempts. Try again in " + Math.floor(left / 60) + ":" + String(left % 60).padStart(2, "0"));
      };
      tick(); lockTimer = setInterval(tick, 1000); refresh();
    }
    function clear() {                                 // never keep passwords in the page
      for (const f of [cur, nw, rep]) f.input.value = "";
      hideErrors(); refresh();
    }
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (busy) return;
      const p = problem();
      if (p) { showBox(p); return; }
      hideErrors();
      busy = true; refresh();
      let fallback = setTimeout(() => { busy = false; refresh(); }, 8000);   // double-submit guard, 8 s fallback
      try {
        await TR.api("/api/me/password", { method: "POST", json: { current: cur.input.value, new: nw.input.value },
                                           noRedirect: true, quiet: true });
        clearTimeout(fallback); fallback = 0;
        busy = false;
        m.close();                                      // clears the fields
        // the server re-issued this tab's cookie and closed its socket (4401): tr.js reconnects
        TR.toast("Password changed — other devices were signed out.", "ok", { force: true });
        TR.srLive("Password changed. Other devices were signed out.");
      } catch (er) {
        if (er.status === 401 && er.code === "invalid_credentials") fieldErr(cur, er.message || "The current password is wrong.");
        else if (er.status === 401) { TR.goLogin("expired"); return; }
        else if (er.status === 429) startLock(er.body && er.body.retry_after);
        else if (er.status === 400 && er.field === "new") fieldErr(nw, er.message || "That password can't be used.");
        else showBox(er.message || "Could not change the password.");
      } finally {
        if (fallback) clearTimeout(fallback);
        busy = false; refresh();
      }
    });
    return { m, first: () => cur.input, clear };
  }

  function openChangePassword() {
    if (TR.txBlocksModal()) {
      TR.toast("Stop transmitting before changing your password.", "warn");
      TR.srLive("Stop transmitting before changing your password");
      return;
    }
    if (TR.menu && TR.menu.close) TR.menu.close(false);
    try { const L = window.RadioLayout; if (L && L.arranging && L.arranging() && L.exitArrange) L.exitArrange(); } catch (_) {}
    if (!cp) cp = buildChangePassword();
    cp.clear();
    cp.m.open();
    cp.first().focus();
  }

  // ---- bind the user-menu items (they stay hidden until this script binds them) -------------------
  const bind = (id, fn) => { const el = $(id); if (el) { el.hidden = false; el.addEventListener("click", fn); } };
  bind("umChangePw", openChangePassword);
  bind("umSignoutAll", signOutEverywhere);
  bind("umSignout", signOut);

  window.TRAccount = { changePassword: openChangePassword, signOut, signOutEverywhere };
  // §4.5 "This admin password is the old shared password … Change it here": the notice's button
  // opens this dialog (tr.js renders the notices at DOMContentLoaded, after this script ran)
  if (TR.noticeActions) TR.noticeActions.migrated_v1_password = { label: "Change password", fn: () => openChangePassword() };
})();
