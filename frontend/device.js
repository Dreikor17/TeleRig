/* TeleRig — this device: the screen kept awake, full screen, and the PTT hotkey dialog.
 * Loads after app.js and account.js (TR.modal / TR.h). Nothing here is sent to the server: all are
 * per-browser conveniences (localStorage "telerig.pttkey" for the hotkey, "telerig.awake" for Keep
 * screen awake).
 *
 * Keep screen awake (#umAwake in the account menu)
 *   While it is on and the page is in view, the browser is asked to keep the screen on (Screen Wake
 *   Lock), so a phone on the bench does not lock in the middle of a contact. It is on by default on
 *   a phone or a tablet (a touch screen and no mouse) and off on a computer; each browser keeps its
 *   choice. The browser drops the lock whenever the page is hidden (the screen locked by hand,
 *   another app in front); it is taken again when the page is back. A request the browser refuses
 *   (battery saver, a browser that grants it only after a tap), or a lock it lets go of while the
 *   page is in view, is asked for again at the next tap, click or key. The wake lock needs HTTPS (or
 *   localhost): over plain http the item is not shown, and a phone or tablet is told why once.
 *
 * Full screen (#fullBtn in the top bar, #umFull in the phone menu)
 *   One switch: the page goes full screen AND the screen is kept awake while it is, whatever Keep
 *   screen awake says. Leaving full screen (the button, Esc or F11) ends both. Where the browser has
 *   no full-screen mode for pages (iPhone) there is no full-screen switch: Keep screen awake is the
 *   one. Without a wake lock (plain http) the button still goes full screen and says so once.
 *
 * PTT hotkey (#umHotkey in the user menu; operators and admins)
 *   Pick a key and how it acts: hold to talk, or press to toggle. The key handling itself is in
 *   app.js next to the PTT button (same fail-safe rule: a press while transmitting always unkeys);
 *   this dialog only captures the key and stores {code, mode, label}, then fires `tr:pttkey`.
 *
 * window.TRDevice = { fullscreen: {on(), toggle(), off(), supported, wake, held()}, awake: {on(), set(v), held()},
 *                     hotkey: {open(), get(), set(), clear(), label()} }
 */
(function () {
  "use strict";
  const TR = window.TR;
  if (!TR) return;
  const $ = (id) => document.getElementById(id);
  const doc = document;

  // ================= keep awake + full screen =======================================================
  const root = doc.documentElement;
  const canFs = !!(root && typeof root.requestFullscreen === "function" && doc.fullscreenEnabled !== false);
  const canWake = !!(window.navigator && navigator.wakeLock && typeof navigator.wakeLock.request === "function");
  const fullBtn = $("fullBtn"), umFull = $("umFull"), umFullLbl = $("umFullLbl");
  const umAwake = $("umAwake"), umAwakeLbl = $("umAwakeLbl");
  const AWAKE_KEY = "telerig.awake", TOLD_KEY = "telerig.awake.told";
  const stored = (k) => { try { return localStorage.getItem(k); } catch (_) { return null; } };
  const keep = (k, v) => { try { localStorage.setItem(k, v); } catch (_) {} };
  // a phone or a tablet: a touch screen and no mouse to hover with
  const touchFirst = (() => {
    try { return !!(window.matchMedia && window.matchMedia("(hover: none) and (pointer: coarse)").matches); }
    catch (_) { return false; }
  })();
  let awake = stored(AWAKE_KEY) === "1" || (stored(AWAKE_KEY) !== "0" && touchFirst);   // Keep screen awake
  let want = false;                                // the full-screen switch: on until the user (or leaving full screen) turns it off
  let lock = null;                                 // the WakeLockSentinel while we hold one
  let asking = false;                              // a request is out
  let retry = false;                               // refused, or let go while in view: ask again at the next tap or key
  let toldNoWake = false;
  const wanted = () => awake || want;

  function isFull() { return !!doc.fullscreenElement; }
  function isOn() { return canFs && isFull(); }
  function setUse(el, name) {
    const use = el && el.querySelector ? el.querySelector("use") : null;
    if (use) use.setAttribute("href", "#i-" + name);
  }
  function render() {
    const on = isOn();
    const label = on ? "Leave full screen" : "Full screen";
    const tip = on ? label : "Full screen" + (canWake ? " (keeps the screen awake)" : "");
    for (const b of [fullBtn, umFull]) {
      if (!b) continue;
      b.setAttribute("aria-pressed", on ? "true" : "false");
      setUse(b, on ? "tr-fullscreen-exit" : "tr-fullscreen");
    }
    if (fullBtn) { fullBtn.title = tip; fullBtn.setAttribute("aria-label", label); }
    if (umFullLbl) umFullLbl.textContent = label;
    if (umAwake) {                                 // like full screen's: the label and the icon say what a click does
      umAwake.setAttribute("aria-pressed", awake ? "true" : "false");
      setUse(umAwake, awake ? "eye-off" : "eye");
    }
    if (umAwakeLbl) umAwakeLbl.textContent = awake ? "Let the screen sleep" : "Keep screen awake";
    if (doc.body && doc.body.classList) doc.body.classList.toggle("tr-awake", !!lock);
  }
  async function takeLock() {
    if (!canWake || !wanted() || lock || asking || doc.hidden) return;
    asking = true;
    let l = null;
    try { l = await navigator.wakeLock.request("screen"); }
    catch (_) { retry = true; }                    // refused (battery saver, policy, no tap yet): full screen still works
    asking = false;
    if (l && (!wanted() || doc.hidden)) {          // turned off, or hidden, while we waited
      try { await l.release(); } catch (_) {}
    } else if (l) {
      lock = l;
      retry = false;
      l.addEventListener("release", () => {
        if (lock !== l) return;
        lock = null;
        if (wanted() && !doc.hidden) retry = true;   // let go while in view (battery saver): again at the next tap
        render();
      });
    }
    render();
  }
  function dropLock() {
    const l = lock;
    lock = null;
    if (l) { try { const p = l.release(); if (p && p.catch) p.catch(() => {}); } catch (_) {} }
  }
  function setAwake(v) {
    awake = !!v;
    keep(AWAKE_KEY, awake ? "1" : "0");
    if (awake) takeLock(); else if (!wanted()) dropLock();
    render();
  }
  const FS_WAIT_MS = 3000;                         // a browser that never answers the request counts as "no"
  async function fullOn() {
    if (!canFs) return false;
    want = true;
    if (!isFull()) {
      let ok = false;
      try {
        await Promise.race([
          Promise.resolve(root.requestFullscreen({ navigationUI: "hide" })).then(() => { ok = true; }),
          new Promise((res) => setTimeout(res, FS_WAIT_MS)),
        ]);
      } catch (_) { ok = false; }
      if (!ok && !isFull()) {                      // refused, or no answer (some embedded browsers)
        want = false;
        if (!wanted()) dropLock();
        TR.toast("The browser did not allow full screen.", "warn");
        render();
        return false;
      }
    }
    await takeLock();
    if (!canWake && !toldNoWake) {
      toldNoWake = true;
      TR.toast(window.isSecureContext === false
        ? "Full screen is on. Keeping the screen awake needs HTTPS (or localhost) — see Remote access."
        : "Full screen is on. This browser cannot keep the screen awake.", "info");
    }
    render();
    return true;
  }
  function fullOff() {
    want = false;
    if (!wanted()) dropLock();                     // Keep screen awake still on: the lock stays
    if (isFull() && typeof doc.exitFullscreen === "function") {
      try { const p = doc.exitFullscreen(); if (p && p.catch) p.catch(() => {}); } catch (_) {}
    }
    render();
  }
  function fullToggle() { return isOn() ? (fullOff(), false) : fullOn(); }
  doc.addEventListener("fullscreenchange", () => {
    if (canFs && !isFull() && want) { want = false; if (!wanted()) dropLock(); }   // left with Esc / F11
    render();
  });
  doc.addEventListener("visibilitychange", () => { if (!doc.hidden && wanted()) takeLock(); });
  // a refused request is asked again at the next tap, click or key: the events a browser counts as the
  // user's own (a touch's pointerup and touchend, a mouse's mousedown, a key)
  const again = () => { if (retry && !lock && !asking) { retry = false; takeLock(); } };
  for (const t of ["pointerup", "touchend", "mousedown", "keydown"]) doc.addEventListener(t, again, { capture: true, passive: true });

  const bind = (b, fn) => {
    if (!b) return;
    b.hidden = false;
    b.addEventListener("click", () => { if (TR.menu && TR.menu.close) TR.menu.close(false); fn(); });
  };
  if (canFs) { bind(fullBtn, fullToggle); bind(umFull, fullToggle); }
  if (canWake) bind(umAwake, () => setAwake(!awake));
  render();
  if (wanted()) takeLock();
  // over plain http a phone's screen can't be kept awake: say so once in this browser
  if (!canWake && touchFirst && window.isSecureContext === false && stored(TOLD_KEY) !== "1") {
    keep(TOLD_KEY, "1");
    toldNoWake = true;
    TR.toast("Over plain http the browser can't keep the screen awake, so it may lock. Open TeleRig over HTTPS to keep it on (see Remote access).", "info");
  }

  // ================= the PTT hotkey dialog ============================================================
  const STORE = "telerig.pttkey";
  const tx = () => window.RadioTx || null;
  function allowed(code) {
    const t = tx();
    return !!(t && typeof t.hotkeyAllowed === "function" && t.hotkeyAllowed(code));
  }
  const NAMES = { Space: "Space", ControlLeft: "Left Ctrl", ControlRight: "Right Ctrl", AltLeft: "Left Alt",
                  AltRight: "Right Alt", ShiftLeft: "Left Shift", ShiftRight: "Right Shift", Backquote: "`",
                  Minus: "-", Equal: "=", BracketLeft: "[", BracketRight: "]", Backslash: "\\", Semicolon: ";",
                  Quote: "'", Comma: ",", Period: ".", Slash: "/", Insert: "Insert", Pause: "Pause" };
  function keyLabel(code) {
    if (NAMES[code]) return NAMES[code];
    let m = /^Key([A-Z])$/.exec(code);
    if (m) return m[1];
    m = /^Digit(\d)$/.exec(code);
    if (m) return m[1];
    m = /^Numpad(.+)$/.exec(code);
    if (m) return "Numpad " + m[1];
    return code;
  }
  function getKey() {
    try {
      const raw = JSON.parse(localStorage.getItem(STORE) || "null");
      if (raw && allowed(raw.code)) return { code: raw.code, mode: raw.mode === "toggle" ? "toggle" : "hold", label: keyLabel(raw.code) };
    } catch (_) {}
    return null;
  }
  function announce() { doc.dispatchEvent(new CustomEvent("tr:pttkey")); }
  function setKey(code, mode) {
    if (!allowed(code)) return false;
    try { localStorage.setItem(STORE, JSON.stringify({ code, mode: mode === "toggle" ? "toggle" : "hold", label: keyLabel(code) })); }
    catch (_) { TR.toast("This browser did not save the hotkey (storage is blocked).", "warn"); return false; }
    announce();
    return true;
  }
  function clearKey() {
    try { localStorage.removeItem(STORE); } catch (_) {}
    announce();
  }

  let dlg = null;
  function build() {
    const h = TR.h;
    let capturing = false;
    const m = TR.modal({ id: "hk", className: "tm-narrow", returnFocus: () => $("userBtn"),
                         onEscape: () => { if (capturing) stopCapture("Cancelled."); else m.close(); },
                         onClose: () => stopCapture("") });
    const keyBox = h("kbd", { class: "hk-key", id: "hkKey" });
    const msg = h("div", { class: "tm-ferr hk-msg", id: "hkMsg", role: "status", "aria-live": "polite" });
    const setBtn = h("button", { type: "button", class: "ctl-btn primary", id: "hkSet" }, TR.icon("keyboard"), "Set key…");
    const clrBtn = h("button", { type: "button", class: "ctl-btn", id: "hkClear", text: "Remove" });
    const radio = (val, title, sub) => {
      const input = h("input", { type: "radio", name: "hkMode", value: val, id: "hkMode-" + val });
      return { input, el: h("label", { class: "hk-mode", for: input.id }, input,
                            h("span", null, h("b", { text: title }), h("span", { class: "tm-hint", text: " " + sub }))) };
    };
    const hold = radio("hold", "Hold to talk", "key down transmits, releasing it stops");
    const toggle = radio("toggle", "Press to toggle", "one press keys, the next press stops (like clicking PTT)");
    const done = h("button", { type: "button", class: "ctl-btn", text: "Done", on: { click: () => m.close() } });
    m.dialog.append(
      h("header", { class: "tm-head" },
        h("span", { class: "tm-chip" }, TR.icon("keyboard"), h("b", { id: m.titleId, text: "PTT HOTKEY" })),
        h("button", { type: "button", class: "tm-x", title: "Close", "aria-label": "Close", on: { click: () => m.close() } }, TR.icon("x"))),
      h("div", { class: "tm-body" },
        h("p", { class: "tm-note", text: "A key on this device that works like the PTT button. It is kept in this browser only." }),
        h("div", { class: "hk-row" }, h("span", { class: "tm-label", text: "Key" }), keyBox, setBtn, clrBtn),
        msg,
        h("div", { class: "hk-modes", role: "radiogroup", "aria-label": "How the key acts" }, hold.el, toggle.el),
        h("ul", { class: "hk-notes" },
          h("li", { text: "Pressing the key while you transmit always stops the transmission." }),
          h("li", { text: "It never keys while you type in a field, while a dialog or menu is open, or while this tab is hidden." }),
          h("li", { text: "Hold to talk also stops when this window loses the focus. After a transmission ends, the key is ignored for a moment (0.8 s), like a second click on PTT." }),
          h("li", { text: "A function key is the best choice (F13-F24 if your keyboard or foot switch has them): Space and letters are also used for typing. Shift, Ctrl and Alt can't be used." })),
        h("div", { class: "tm-actions" }, done)));

    // The captured key's own key-up must not act on whatever has the focus by then (Space would
    // click the "Set key…" button again).
    let swallowUp = null;
    doc.addEventListener("keyup", (e) => {
      if (swallowUp && e.code === swallowUp) { e.preventDefault(); e.stopPropagation(); swallowUp = null; }
    }, true);
    function show() {
      const k = getKey();
      keyBox.textContent = capturing ? "press a key…" : (k ? k.label : "not set");
      keyBox.classList.toggle("empty", !k && !capturing);
      keyBox.classList.toggle("wait", capturing);
      clrBtn.disabled = !k || capturing;
      setBtn.disabled = capturing;
      const mode = k ? k.mode : (hold.input.checked || !toggle.input.checked ? "hold" : "toggle");
      hold.input.checked = mode === "hold";
      toggle.input.checked = mode === "toggle";
    }
    function say(text) { msg.textContent = text || ""; }
    function onKey(e) {
      // the dialog's own Escape / Tab handling runs first (account.js); everything else is a candidate
      if (!capturing || e.key === "Escape" || e.key === "Tab") return;
      e.preventDefault();
      e.stopPropagation();
      if (e.repeat) return;
      const code = String(e.code || "");
      swallowUp = code;
      if (/^(Shift|Control|Alt)(Left|Right)$|^AltGraph$/.test(code)) {
        say("Shift, Ctrl and Alt can't be the hotkey: they are part of other shortcuts. Press a function key (F1-F24; a foot switch can send one).");
        return;
      }
      if (!allowed(code)) { say((keyLabel(code) || "That key") + " can't be the hotkey (it is needed elsewhere). Press another key."); return; }
      const mode = toggle.input.checked ? "toggle" : "hold";
      if (setKey(code, mode)) stopCapture(keyLabel(code) + " is the PTT hotkey now.");
      else stopCapture("");
    }
    function startCapture() {
      if (capturing) return;
      capturing = true;
      say("Press the key to use. Esc cancels.");
      doc.addEventListener("keydown", onKey, true);
      show();
      keyBox.focus && keyBox.focus();
    }
    function stopCapture(text) {
      const was = capturing;
      if (capturing) doc.removeEventListener("keydown", onKey, true);
      capturing = false;
      say(text);
      show();
      if (was && m.isOpen()) setBtn.focus();
    }
    setBtn.addEventListener("click", startCapture);
    clrBtn.addEventListener("click", () => { clearKey(); stopCapture("No hotkey."); });
    for (const r of [hold, toggle]) r.input.addEventListener("change", () => {
      const k = getKey();
      if (k) setKey(k.code, r.input.value);
      show();
    });
    keyBox.tabIndex = -1;
    doc.addEventListener("tr:pttkey", () => { if (m.isOpen() && !capturing) show(); });   // changed elsewhere
    return { m, show, first: () => setBtn, stopCapture };
  }
  function openHotkey() {
    if (typeof TR.modal !== "function" || typeof TR.h !== "function") return;
    if (!TR.can("tx")) { TR.toast("The PTT hotkey needs the operator role.", "warn"); return; }
    if (TR.txBlocksModal && TR.txBlocksModal()) { TR.toast("Stop transmitting before changing the hotkey.", "warn"); return; }
    if (TR.menu && TR.menu.close) TR.menu.close(false);
    try { const L = window.RadioLayout; if (L && L.arranging && L.arranging() && L.exitArrange) L.exitArrange(); } catch (_) {}
    if (!dlg) dlg = build();
    dlg.stopCapture("");
    dlg.m.open();
    dlg.first().focus();
  }
  const um = $("umHotkey");
  if (um && typeof TR.modal === "function" && typeof TR.h === "function" && tx()) {
    um.hidden = false;
    um.addEventListener("click", openHotkey);
  }

  window.TRDevice = {
    fullscreen: { on: isOn, toggle: fullToggle, off: fullOff, supported: canFs, wake: canWake, held: () => !!lock },
    awake: { on: () => awake, set: setAwake, held: () => !!lock },
    hotkey: { open: openHotkey, get: getKey, set: setKey, clear: clearKey, label: keyLabel },
  };
})();
