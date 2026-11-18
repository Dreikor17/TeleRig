/* device.js (full screen + keep awake, the PTT hotkey dialog) and bridge.js (the WSJT-X bridge
 * indicator), tested on the REAL frontend code in a Node vm (tests/fakedom.js). No browser, no server,
 * no socket, no radio. The Fullscreen and Screen Wake Lock APIs are fakes defined in this file; the
 * hotkey dialog runs against the real app.js on the fake page of tests/pttenv.js (its "server" is a
 * few lines of in-memory JS; "keyed" is a boolean). Run by tests/t_c1.py (unit group):
 *
 *     node tests/device_bridge_ui_test.js   -> exit 0 and "ALL PASS", or exit 1 naming each failure
 *
 *  A. full screen + keep awake: on / off, Esc, the lock re-taken after the tab was hidden, a refused
 *     or never-answered request, no wake lock (plain http), no full-screen API (iPhone), neither
 *  B. the PTT hotkey dialog: capture, reserved keys refused, hold / toggle, remove, nothing is keyed
 *     while capturing, not opened while transmitting or for a viewer
 *  C. the bridge indicator: states, the panel per role, pause / resume, the pause announced once,
 *     text never parsed as HTML
 *  D. Administration > WSJT-X bridge (admin.js): the form shows the server's settings and status,
 *     Save sends exactly what the form says, allowing transmit asks first, a refused save shows the
 *     server's message at its field, the status is re-read while the tab is open
 */
"use strict";
const { makeEnv } = require("./fakedom");
const { pageEnv } = require("./pttenv");

let failures = 0, checks = 0;
const pending = [];
function check(cond, msg) { checks++; if (!cond) { failures++; console.log("FAIL " + msg); } }
function eq(a, b, msg) { const ja = JSON.stringify(a), jb = JSON.stringify(b); check(ja === jb, msg + ": got " + ja + ", want " + jb); }
function test(name, fn) { pending.push([name, fn]); }
const OP_PERMS = ["view", "listen", "menu_read", "operate", "tx", "connect", "menu"];

// ---- A. full screen + keep awake -------------------------------------------------------------------
// opts: fs = "ok" | "reject" | "hang" | false (no API); wake = "ok" | "reject" | false (no API); secure
function fullEnv(opts) {
  opts = Object.assign({ fs: "ok", wake: "ok", secure: true }, opts || {});
  const env = makeEnv({});
  const { doc, ctx } = env;
  const root = doc.documentElement;
  const log = [];
  env.log = log;
  doc.fullscreenElement = null;
  const fsEvent = () => doc.dispatchEvent(new env.Event("fullscreenchange"));
  if (opts.fs) {
    doc.fullscreenEnabled = true;
    root.requestFullscreen = (o) => {
      log.push("fs:request");
      if (opts.fs === "reject") return Promise.reject(new Error("denied"));
      if (opts.fs === "hang") return new Promise(() => {});
      doc.fullscreenElement = root;
      fsEvent();
      return Promise.resolve();
    };
    doc.exitFullscreen = () => { log.push("fs:exit"); doc.fullscreenElement = null; fsEvent(); return Promise.resolve(); };
  }
  env.escape = () => { doc.fullscreenElement = null; fsEvent(); };       // the user pressed Esc / F11
  env.locks = [];
  if (opts.wake) {
    ctx.navigator.wakeLock = {
      request: (type) => {
        log.push("wake:request:" + type);
        if (opts.wake === "reject") return Promise.reject(Object.assign(new Error("no"), { name: "NotAllowedError" }));
        const l = { released: false, _l: [], addEventListener(t, fn) { if (t === "release") this._l.push(fn); },
                    release() { if (!this.released) { this.released = true; log.push("wake:release"); for (const fn of this._l) fn(); } return Promise.resolve(); } };
        env.locks.push(l);
        return Promise.resolve(l);
      },
    };
  }
  ctx.isSecureContext = !!opts.secure;
  env.full = env.el("button", { id: "fullBtn", hidden: true, "aria-pressed": "false" });
  env.full.appendChild(Object.assign(doc.createElement("svg"), {})).appendChild(doc.createElement("use")).setAttribute("href", "#i-tr-fullscreen");
  env.um = env.el("button", { id: "umFull", hidden: true, "aria-pressed": "false" });
  env.um.appendChild(doc.createElement("use")).setAttribute("href", "#i-tr-fullscreen");
  env.umLbl = env.el("span", { id: "umFullLbl" }, env.um);
  env.el("div", { id: "toasts" });
  env.load("tr.js");
  env.toasts = [];
  ctx.TR.toast = (msg, kind) => { env.toasts.push([kind, String(msg)]); };
  env.load("device.js");
  env.D = ctx.TRDevice;
  env.tick = async (ms) => { for (let i = 0; i < 4; i++) await env.flush(); if (ms) { env.clock.advance(ms); for (let i = 0; i < 4; i++) await env.flush(); } };
  env.icon = () => env.full.querySelector("use").getAttribute("href");
  env.held = () => env.locks.filter((l) => !l.released).length;
  return env;
}

test("A full screen on: the page goes full screen AND holds a screen wake lock; off undoes both", async () => {
  const e = fullEnv();
  eq([e.full.hidden, e.um.hidden, e.D.fullscreen.supported, e.D.fullscreen.wake], [false, false, true, true], "A both buttons are bound");
  eq([e.full.getAttribute("aria-pressed"), e.icon(), e.full.title], ["false", "#i-tr-fullscreen", "Full screen (keeps the screen awake)"], "A off");
  e.full.click();
  await e.tick();
  eq([e.log, !!e.doc.fullscreenElement, e.held(), e.D.fullscreen.held()], [["fs:request", "wake:request:screen"], true, 1, true], "A on: full screen + one lock");
  eq([e.full.getAttribute("aria-pressed"), e.um.getAttribute("aria-pressed"), e.icon(), e.full.title, e.umLbl.textContent],
     ["true", "true", "#i-tr-fullscreen-exit", "Leave full screen", "Leave full screen"], "A on: both controls show it");
  check(e.body.classList.contains("tr-awake"), "A body.tr-awake while the lock is held");
  eq(e.toasts, [], "A no message when everything works");
  e.full.click();
  await e.tick();
  eq([!!e.doc.fullscreenElement, e.held(), e.full.getAttribute("aria-pressed"), e.icon(), e.body.classList.contains("tr-awake")],
     [false, 0, "false", "#i-tr-fullscreen", false], "A off again: left full screen, the lock is released");
  eq(e.log.slice(2), ["wake:release", "fs:exit"], "A off: release, then exit");
});
test("A leaving full screen with Esc / F11 lets the screen sleep again", async () => {
  const e = fullEnv();
  e.um.click();                                       // the phone-menu item is the same switch
  await e.tick();
  eq(e.held(), 1, "A on");
  e.escape();
  await e.tick();
  eq([e.held(), e.D.fullscreen.on(), e.full.getAttribute("aria-pressed")], [0, false, "false"], "A Esc: lock released, switch off");
  e.doc.hidden = true; e.doc.dispatchEvent(new e.Event("visibilitychange"));
  e.doc.hidden = false; e.doc.dispatchEvent(new e.Event("visibilitychange"));
  await e.tick();
  eq(e.held(), 0, "A nothing is re-taken after the switch went off");
});
test("A the browser drops the lock while the tab is hidden; it is taken again when the tab is back", async () => {
  const e = fullEnv();
  e.full.click();
  await e.tick();
  e.doc.hidden = true; e.doc.dispatchEvent(new e.Event("visibilitychange"));
  e.locks[0].release();                               // what a browser does on hide
  await e.tick();
  eq([e.held(), e.D.fullscreen.on()], [0, true], "A hidden: no lock, the switch is still on");
  e.doc.hidden = false; e.doc.dispatchEvent(new e.Event("visibilitychange"));
  await e.tick();
  eq([e.held(), e.locks.length], [1, 2], "A visible again: a new lock");
  e.doc.hidden = true; e.doc.dispatchEvent(new e.Event("visibilitychange"));
  await e.tick();
  eq(e.locks.length, 2, "A no lock is requested while hidden");
});
test("A the browser refuses full screen: off, a message, no lock kept", async () => {
  const e = fullEnv({ fs: "reject" });
  e.full.click();
  await e.tick();
  eq([e.D.fullscreen.on(), e.held(), e.full.getAttribute("aria-pressed")], [false, 0, "false"], "A refused: off");
  eq(e.toasts, [["warn", "The browser did not allow full screen."]], "A says so");
});
test("A the browser never answers the full-screen request: off after 3 s, a message", async () => {
  const e = fullEnv({ fs: "hang" });
  e.full.click();
  await e.tick(2000);
  eq([e.toasts.length, e.held()], [0, 0], "A still waiting at 2 s");
  await e.tick(1200);
  eq([e.D.fullscreen.on(), e.held(), e.toasts], [false, 0, [["warn", "The browser did not allow full screen."]]], "A given up after 3 s");
});
test("A no wake lock (plain http): full screen still works and the reason is said once", async () => {
  const e = fullEnv({ wake: false, secure: false });
  eq([e.full.hidden, e.D.fullscreen.wake, e.full.title], [false, false, "Full screen"], "A the button does not promise keep-awake");
  e.full.click();
  await e.tick();
  eq([!!e.doc.fullscreenElement, e.toasts.length], [true, 1], "A full screen on");
  check(/HTTPS/.test(e.toasts[0][1]), "A the message names HTTPS: " + e.toasts[0][1]);
  e.full.click(); await e.tick();
  e.full.click(); await e.tick();
  eq(e.toasts.length, 1, "A said once");
});
test("A the wake lock is refused (battery saver): full screen stays on, nothing breaks", async () => {
  const e = fullEnv({ wake: "reject" });
  e.full.click();
  await e.tick();
  eq([!!e.doc.fullscreenElement, e.D.fullscreen.on(), e.D.fullscreen.held(), e.full.getAttribute("aria-pressed")], [true, true, false, "true"], "A on without a lock");
  e.full.click();
  await e.tick();
  eq(e.D.fullscreen.on(), false, "A off");
});
test("A no full-screen API (iPhone): the switch is Keep screen awake alone", async () => {
  const e = fullEnv({ fs: false });
  eq([e.um.hidden, e.D.fullscreen.supported, e.umLbl.textContent], [false, false, "Keep screen awake"], "A labelled for what it does");
  e.um.click();
  await e.tick();
  eq([e.held(), e.D.fullscreen.on(), e.umLbl.textContent, e.um.getAttribute("aria-pressed")], [1, true, "Let the screen sleep", "true"], "A awake");
  e.um.click();
  await e.tick();
  eq([e.held(), e.D.fullscreen.on(), e.umLbl.textContent], [0, false, "Keep screen awake"], "A released");
});
test("A switched on while the tab is hidden: the lock waits until the tab is visible", async () => {
  const e = fullEnv({ fs: false });
  e.doc.hidden = true;
  e.D.fullscreen.toggle();
  await e.tick();
  eq([e.locks.length, e.D.fullscreen.on()], [0, true], "A on, but no lock is requested from a hidden tab");
  e.doc.hidden = false; e.doc.dispatchEvent(new e.Event("visibilitychange"));
  await e.tick();
  eq(e.held(), 1, "A taken once the tab is visible");
});
test("A neither API: the buttons stay hidden", async () => {
  const e = fullEnv({ fs: false, wake: false });
  eq([e.full.hidden, e.um.hidden], [true, true], "A nothing to offer");
});

// ---- B. the PTT hotkey dialog -----------------------------------------------------------------------
function hkEnv(perms, stored) {
  const ls = stored ? { "telerig.pttkey": JSON.stringify(stored) } : {};
  const e = pageEnv(20, {
    localStorage: ls,
    before: (x) => {
      x.el("div", { id: "userMenu", hidden: true });
      x.el("button", { id: "userBtn" });
      x.um = x.el("button", { id: "umHotkey", hidden: true });
      x.el("div", { id: "modalRoot" });
    },
  });
  if (perms) e.frame({ type: "hello", v: 1, conn: 3, user: "ME", role: perms.role, perms: perms.perms, status: {}, tx: { owner: null }, hb_interval: 2 });
  e.load("account.js");
  e.load("device.js");
  e.fired = 0;
  e.doc.addEventListener("tr:pttkey", () => { e.fired++; });
  e.q = (sel) => e.doc.querySelector(sel);
  e.id = (id) => e.doc.documentElement.querySelectorAll("#" + id)[0] || null;
  e.key = (code, init) => { const ev = new e.Event("keydown", Object.assign({ code, key: init && init.key || code }, init || {})); e.doc.dispatchEvent(ev); return ev; };
  e.keyup = (code) => { const ev = new e.Event("keyup", { code, key: code }); e.doc.dispatchEvent(ev); return ev; };
  e.stored = () => { const v = e.ctx.localStorage.getItem("telerig.pttkey"); return v ? JSON.parse(v) : null; };
  e.open = () => e.body.classList.contains("tr-modal-open");
  return e;
}
test("B the dialog: capture a key, reserved keys are refused, the engine follows", async () => {
  const e = hkEnv();
  eq(e.um.hidden, false, "B the menu item is bound");
  e.um.click();
  eq([e.open(), e.id("hkKey").textContent, e.id("hkClear").disabled], [true, "not set", true], "B open, nothing set");
  e.id("hkSet").click();
  eq([e.id("hkKey").textContent, e.id("hkSet").disabled], ["press a key…", true], "B waiting for a key");
  e.key("Tab", { key: "Tab" });
  eq(e.stored(), null, "B Tab is never captured");
  for (const code of ["F12", "Enter", "ArrowUp", "MetaLeft", "Backspace"]) {
    const ev = e.key(code);
    eq([e.stored(), ev.defaultPrevented], [null, true], "B " + code + " is refused (and swallowed)");
    check(/can't be the hotkey/.test(e.id("hkMsg").textContent), "B says why for " + code + ": " + e.id("hkMsg").textContent);
  }
  e.key("F9", { repeat: true });
  eq(e.stored(), null, "B an auto-repeat is not a key press");
  const ev = e.key("F9");
  eq([e.stored(), ev.defaultPrevented, e.fired], [{ code: "F9", mode: "hold", label: "F9" }, true, 1], "B F9 stored (hold), tr:pttkey fired");
  eq([e.ctx.RadioTx.hotkey(), e.id("hkKey").textContent, e.id("hkMsg").textContent, e.id("hkClear").disabled, e.id("hkSet").disabled],
     [{ code: "F9", mode: "hold", label: "F9" }, "F9", "F9 is the PTT hotkey now.", false, false], "B the engine has it; the dialog shows it");
  eq(e.btn.title, "PTT — hotkey: F9 (hold to talk)", "B the PTT button names it");
  check(e.keyup("F9").defaultPrevented, "B the captured key's own key-up is swallowed");
  e.key("F9");                                        // not capturing any more: the dialog is still open
  e.keyup("F9");
  e.adv(200);
  eq([e.frames(), e.S.keyed], [[], false], "B nothing was keyed by any of this (a dialog is open)");
});
test("B hold / toggle, labels, remove", async () => {
  const e = hkEnv();
  e.um.click();
  e.id("hkMode-toggle").checked = true; e.id("hkMode-hold").checked = false;
  e.id("hkSet").click();
  e.key("ControlRight", { ctrlKey: true });
  eq(e.stored(), null, "B Right Ctrl is refused");
  check(/Shift, Ctrl and Alt can't be the hotkey/.test(e.id("hkMsg").textContent), "B says why: " + e.id("hkMsg").textContent);
  for (const code of ["ShiftLeft", "AltRight", "AltGraph", "ControlLeft"]) {
    e.key(code);
    eq(e.stored(), null, "B " + code + " is refused");
  }
  e.key("F13");
  eq(e.stored(), { code: "F13", mode: "toggle", label: "F13" }, "B the chosen mode is stored with the key; a readable label");
  e.id("hkMode-hold").checked = true; e.id("hkMode-toggle").checked = false;
  e.id("hkMode-hold").dispatchEvent(new e.Event("change"));
  eq([e.stored().mode, e.ctx.RadioTx.hotkey().mode], ["hold", "hold"], "B switching the mode re-saves");
  eq(["Space", "KeyQ", "Digit7", "Numpad0", "F3", "AltRight", "Backquote"].map(e.ctx.TRDevice.hotkey.label),
     ["Space", "Q", "7", "Numpad 0", "F3", "Right Alt", "`"], "B key labels");
  e.id("hkClear").click();
  eq([e.stored(), e.ctx.RadioTx.hotkey(), e.id("hkKey").textContent, e.btn.title], [null, null, "not set", ""], "B removed");
});
test("B Esc cancels a capture and keeps the dialog; a second Esc closes it", async () => {
  const e = hkEnv(null, { code: "Space", mode: "hold", label: "Space" });
  e.um.click();
  eq(e.id("hkKey").textContent, "Space", "B shows the stored key");
  e.id("hkSet").click();
  e.key("Escape", { key: "Escape" });
  eq([e.open(), e.id("hkKey").textContent, e.id("hkMsg").textContent, e.stored().code], [true, "Space", "Cancelled.", "Space"], "B capture cancelled, key kept");
  e.key("Escape", { key: "Escape" });
  eq(e.open(), false, "B closed");
  e.key("F9");
  eq(e.stored().code, "Space", "B a key after closing is not captured");
});
test("B capturing never keys, even when the pressed key IS the current hotkey", async () => {
  const e = hkEnv(null, { code: "Space", mode: "hold", label: "Space" });
  e.um.click();
  e.id("hkSet").click();
  e.key("Space"); e.keyup("Space");
  e.adv(300);
  eq([e.frames(), e.S.keyed, e.stored().code], [[], false, "Space"], "B Space re-captured, nothing keyed");
});
test("B the dialog does not open while this tab transmits, nor for a viewer", async () => {
  const e = hkEnv();
  e.mouse(1); e.adv(100);
  eq(e.S.keyed, true, "B keyed (fake server)");
  e.um.click();
  eq(e.open(), false, "B not opened while transmitting");
  check(e.toasts.some((t) => /Stop transmitting/.test(t[0])), "B says why");
  const v = hkEnv({ role: "viewer", perms: ["view", "listen", "menu_read"] });
  v.ctx.TRDevice.hotkey.open();
  eq(v.open(), false, "B a viewer gets no dialog");
});
test("B a key change made elsewhere shows in the open dialog", async () => {
  const e = hkEnv();
  e.um.click();
  e.ctx.TRDevice.hotkey.set("F8", "toggle");
  eq([e.id("hkKey").textContent, e.id("hkMode-toggle").checked], ["F8", true], "B the dialog follows");
  eq(e.ctx.TRDevice.hotkey.set("Escape", "hold"), false, "B set() refuses a reserved key");
  eq(e.stored().code, "F8", "B ... and keeps the old one");
});

// ---- C. the bridge indicator ---------------------------------------------------------------------------
function brEnv(role) {
  const env = makeEnv({});
  env.btn = env.el("button", { id: "bridgeBtn", hidden: true, "aria-expanded": "false" });
  env.pop = env.el("div", { id: "bridgePop", hidden: true });
  env.el("div", { id: "toasts" });
  env.el("div", { id: "userMenu", hidden: true });
  env.load("tr.js");
  const perms = { viewer: ["view", "listen", "menu_read"], operator: OP_PERMS, admin: OP_PERMS.concat(["admin"]) }[role || "operator"];
  env.ctx.TR.setRole(role || "operator", perms);
  env.toasts = [];
  env.ctx.TR.toast = (msg, kind) => { env.toasts.push([kind, String(msg)]); };
  env.sent = [];
  env.sendOk = true;
  env.ctx.RadioControl = { send: (o) => { env.sent.push(o); return env.sendOk; } };
  env.adminOpened = [];
  env.ctx.TRAdmin = { open: (tab) => env.adminOpened.push(tab) };
  env.refreshes = 0;
  env.doc.addEventListener("tr:refresh-status", () => { env.refreshes++; });
  env.load("bridge.js");
  env.push = (b) => env.doc.dispatchEvent(new env.Event("tr:bridge", { detail: b }));
  env.text = () => env.pop.textContent;
  env.byId = (id) => env.doc.documentElement.querySelectorAll("#" + id)[0] || null;
  return env;
}
const BASE = { enabled: true, listening: true, port: 4532, listen: "local", ptt: false, clients: 0, paused: null, on_air: false, error: false };
const FULL = Object.assign({}, BASE, { error_text: null, peers: [] });

test("C the button exists only while the bridge is enabled; its states", async () => {
  const e = brEnv("operator");
  eq(e.btn.hidden, true, "C hidden with no status");
  e.push(Object.assign({}, FULL, { enabled: false, listening: false }));
  eq(e.btn.hidden, true, "C hidden while the bridge is off");
  e.push(FULL);
  eq([e.btn.hidden, e.btn.getAttribute("data-state"), e.btn.title], [false, "idle", "WSJT-X bridge: waiting"], "C idle");
  e.push(Object.assign({}, FULL, { clients: 2, peers: [{ ip: "127.0.0.1", secs: 5, cmds: 9, tx: false }, { ip: "100.64.0.9", secs: 700, cmds: 1, tx: false }] }));
  eq([e.btn.getAttribute("data-state"), e.btn.title], ["live", "WSJT-X bridge: connected (2 programs)"], "C live");
  e.push(Object.assign({}, FULL, { clients: 1, ptt: true, paused: { reason: "stopped", by: "OP1" } }));
  eq([e.btn.getAttribute("data-state"), e.btn.title], ["paused", "WSJT-X bridge: transmit paused — stopped by OP1"], "C paused");
  e.push(Object.assign({}, FULL, { clients: 1, ptt: true, on_air: true }));
  eq(e.btn.getAttribute("data-state"), "onair", "C on air");
  e.push(Object.assign({}, FULL, { listening: false, error: true, error_text: "Port 4532 could not be opened (in use)." }));
  eq(e.btn.getAttribute("data-state"), "error", "C error");
  e.push(Object.assign({}, FULL, { enabled: false }));
  eq(e.btn.hidden, true, "C hidden again when switched off");
});
test("C the panel: what is listening, who is connected, whether it may transmit", async () => {
  const e = brEnv("operator");
  e.push(Object.assign({}, FULL, { clients: 1, peers: [{ ip: "127.0.0.1", secs: 125, cmds: 340, tx: false }] }));
  e.btn.click();
  eq([e.pop.hidden, e.btn.getAttribute("aria-expanded"), e.refreshes >= 1], [false, "true", true], "C opens and re-reads the status");
  const t = e.text();
  for (const part of ["WSJT-X BRIDGE", "connected", "Listening on port 4532 · programs on this PC only.", "127.0.0.1", "2 min", "340 commands",
                      "Transmit over the bridge is off", "Network Server = localhost:4532"]) check(t.includes(part), "C panel has: " + part + " | " + t);
  eq([e.byId("bridgeTxBtn"), e.byId("bridgeAdminBtn")], [null, null], "C transmit off + not an admin: no buttons");
  e.push(Object.assign({}, FULL, { listen: "network", clients: 1, ptt: true, peers: [{ ip: "100.64.0.9", secs: 4, cmds: 1, tx: true }] , on_air: true }));
  const t2 = e.text();
  for (const part of ["this PC and the allowed computers", "100.64.0.9", "transmitting", "Transmit over the bridge is allowed", "this PC's address:4532"])
    check(t2.includes(part), "C live update while open: " + part + " | " + t2);
  e.doc.dispatchEvent(new e.Event("keydown", { key: "Escape" }));
  eq([e.pop.hidden, e.btn.getAttribute("aria-expanded")], [true, "false"], "C Escape closes");
  e.btn.click();
  e.body.dispatchEvent(new e.Event("pointerdown"));
  eq(e.pop.hidden, true, "C a press outside closes");
  e.btn.click();
  e.pop.dispatchEvent(new e.Event("pointerdown"));
  eq(e.pop.hidden, false, "C a press inside does not");
  const n = e.refreshes;
  e.clock.advance(7000);
  check(e.refreshes >= n + 2, "C the status is re-read every few seconds while the panel is open");
  e.btn.click();
  const m = e.refreshes;
  e.clock.advance(7000);
  eq(e.refreshes, m, "C ... and not after it closed");
});
test("C pause / resume (operators): the buttons send bridge_tx; the pause is announced once", async () => {
  const e = brEnv("operator");
  e.push(Object.assign({}, FULL, { clients: 1, ptt: true, peers: [{ ip: "127.0.0.1", secs: 1, cmds: 2, tx: false }] }));
  e.btn.click();
  const b = e.byId("bridgeTxBtn");
  eq(b.textContent, "Pause transmit", "C a Pause button while transmit is allowed");
  b.click();
  eq(e.sent, [{ action: "bridge_tx", on: false }], "C Pause sends bridge_tx off");
  eq(e.toasts, [], "C nothing announced yet (the server decides)");
  const paused = Object.assign({}, FULL, { clients: 1, ptt: true, paused: { reason: "stopped", by: "OP1" }, peers: [{ ip: "127.0.0.1", secs: 3, cmds: 4, tx: false }] });
  e.push(paused);
  eq(e.toasts.length, 1, "C the pause is announced");
  check(/transmit paused \(stopped by OP1\)/.test(e.toasts[0][1]) && /Resume it/.test(e.toasts[0][1]), "C ... with who stopped it: " + e.toasts[0][1]);
  e.push(paused); e.push(paused);
  eq(e.toasts.length, 1, "C ... once, however many frames repeat it");
  check(e.text().includes("PAUSED (stopped by OP1)"), "C the panel says it is paused");
  const r = e.byId("bridgeTxBtn");
  eq(r.textContent, "Resume transmit", "C now a Resume button");
  r.click();
  eq(e.sent.length, 1, "C a Resume right after the pause appeared does nothing (the button just changed under the pointer)");
  e.clock.advance(1100);
  check(e.byId("bridgeTxBtn") === r, "C the panel was not rebuilt meanwhile (same button)");
  r.click();
  eq(e.sent[1], { action: "bridge_tx", on: true }, "C Resume sends bridge_tx on");
  e.push(Object.assign({}, FULL, { clients: 1, ptt: true, peers: [] }));
  e.push(Object.assign({}, paused, { paused: { reason: "heartbeat", by: null } }));
  eq(e.toasts.length, 2, "C a new pause is announced again");
  check(/stopped talking/.test(e.toasts[1][1]), "C the silence reason in plain words: " + e.toasts[1][1]);
  e.clock.advance(1100);
  e.sendOk = false;
  e.byId("bridgeTxBtn").click();
  check(/Not connected/.test(e.toasts[2][1]), "C a failed send says so");
});
test("C a double-click on Pause pauses once and never resumes; a Pause is never held back", async () => {
  const e = brEnv("operator");
  const live = Object.assign({}, FULL, { clients: 1, ptt: true, peers: [{ ip: "127.0.0.1", secs: 1, cmds: 2, tx: true }], on_air: true });
  e.push(live);
  e.btn.click();
  const b = e.byId("bridgeTxBtn");
  b.dispatchEvent(new e.Event("click", { detail: 1 }));
  eq(e.sent, [{ action: "bridge_tx", on: false }], "C the first click pauses");
  e.push(Object.assign({}, live, { on_air: false, paused: { reason: "operator", by: "ME" }, peers: [{ ip: "127.0.0.1", secs: 2, cmds: 3, tx: false }] }));
  const r = e.byId("bridgeTxBtn");
  eq(r.textContent, "Resume transmit", "C the button turned into Resume under the pointer");
  r.dispatchEvent(new e.Event("click", { detail: 2 }));
  e.clock.advance(400);
  r.dispatchEvent(new e.Event("click", { detail: 1 }));
  eq(e.sent.length, 1, "C the double-click's second click and a quick click resume nothing");
  e.clock.advance(1100);
  r.dispatchEvent(new e.Event("click", { detail: 2 }));
  eq(e.sent.length, 1, "C a resume is never the tail of a double-click");
  r.dispatchEvent(new e.Event("click", { detail: 1 }));
  eq(e.sent[1], { action: "bridge_tx", on: true }, "C a deliberate click resumes");
  e.push(live);
  const p = e.byId("bridgeTxBtn");
  eq(p.textContent, "Pause transmit", "C Pause again");
  p.dispatchEvent(new e.Event("click", { detail: 2 }));
  eq(e.sent[2], { action: "bridge_tx", on: false }, "C a Pause acts at once, at any click count (never held back)");
});
test("C the open panel is updated in place: counters refresh, the button stays; a change rebuilds it", async () => {
  const e = brEnv("operator");
  const s1 = Object.assign({}, FULL, { clients: 1, ptt: true, peers: [{ ip: "127.0.0.1", secs: 1, cmds: 2, tx: false }] });
  e.push(s1);
  e.btn.click();
  const b = e.byId("bridgeTxBtn");
  check(e.text().includes("1 s · 2 commands"), "C counters shown: " + e.text());
  e.push(Object.assign({}, s1, { peers: [{ ip: "127.0.0.1", secs: 7, cmds: 9, tx: false }] }));
  check(e.byId("bridgeTxBtn") === b, "C same button after a counters-only update");
  check(e.text().includes("7 s · 9 commands") && !e.text().includes("1 s · 2 commands"), "C counters refreshed: " + e.text());
  e.push(Object.assign({}, s1, { ptt: false }));
  check(e.byId("bridgeTxBtn") === null, "C transmit switched off: rebuilt, no Pause button");
  check(e.text().includes("Transmit over the bridge is off"), "C ... and says so");
});
test("C viewers see the summary and no buttons; admins get Settings", async () => {
  const v = brEnv("viewer");
  v.push(Object.assign({}, BASE, { clients: 2, ptt: true, paused: { reason: "timeout", by: null } }));
  v.btn.click();
  const t = v.text();
  check(t.includes("2 programs are connected.") && t.includes("the 120 s limit was reached"), "C viewer text: " + t);
  eq([v.byId("bridgeTxBtn"), v.byId("bridgeAdminBtn")], [null, null], "C a viewer has no buttons");
  check(!/Resume it/.test(v.toasts[0][1]), "C a viewer is not told to resume it: " + v.toasts[0][1]);
  v.push(Object.assign({}, BASE, { listening: false, error: true }));
  check(v.text().includes("could not be opened. Ask an admin"), "C a viewer gets the generic error text: " + v.text());
  const a = brEnv("admin");
  a.push(Object.assign({}, FULL, { listening: false, error: true, error_text: "Port 4532 could not be opened (in use)." }));
  a.btn.click();
  check(a.text().includes("Port 4532 could not be opened (in use)."), "C an admin gets the real error");
  a.byId("bridgeAdminBtn").click();
  eq([a.adminOpened, a.pop.hidden], [["bridge"], true], "C Settings opens Administration at the bridge tab and closes the panel");
});
test("C text is never parsed as HTML", async () => {
  const e = brEnv("operator");
  const evil = '<img src=x onerror="alert(1)">';
  e.push(Object.assign({}, FULL, { clients: 1, ptt: true, paused: { reason: "stopped", by: evil },
                                   peers: [{ ip: evil, secs: 1, cmds: 1, tx: false }], error_text: evil }));
  e.btn.click();
  eq(e.pop.querySelectorAll("img").length, 0, "C no element was created from a peer address or a name");
  check(e.text().includes(evil), "C it is shown as text");
});

// ---- D. Administration > WSJT-X bridge ------------------------------------------------------------------
function jsonResponse(status, body) { return { ok: status >= 200 && status < 300, status, json: async () => body }; }
function adminEnv(cfg, status, putAnswer) {
  const calls = [];
  const state = { config: Object.assign({ enabled: false, port: 4532, listen: "local", allow: [], ptt: false, silence_s: 10 }, cfg || {}),
                  status: status || null, gets: 0 };
  const env = makeEnv({ fetch: (url, init) => {
    const u = String(url).split("?")[0], method = (init && init.method) || "GET";
    calls.push({ url: u, method, body: init && init.body ? JSON.parse(init.body) : null });
    if (u !== "/api/admin/bridge") return Promise.resolve(jsonResponse(404, { ok: false, error: "not_found" }));
    if (method === "GET") {
      state.gets++;
      return Promise.resolve(jsonResponse(200, { config: state.config, status: state.status, web_port: 8790,
                                                 limits: { port: [1024, 65535], silence_s: [3, 120], allow: 32, clients: 6 } }));
    }
    const ans = putAnswer ? putAnswer(JSON.parse(init.body)) : [200, { ok: true }];
    if (ans[0] === 200) { const saved = JSON.parse(init.body); delete saved.expect; state.config = saved; }
    return Promise.resolve(jsonResponse(ans[0], ans[1]));
  } });
  for (const id of ["userBtn", "adminBtn", "umAdmin", "umChangePw", "umSignoutAll", "umSignout", "modalRoot"]) env.el("div", { id });
  env.load("tr.js");
  env.ctx.TR.user = "ADMIN";
  env.ctx.TR.setRole("admin", OP_PERMS.concat(["admin"]));
  env.confirms = [];
  env.confirmAnswer = true;
  env.ctx.TR.confirm = async (text, o) => { env.confirms.push([String(text), o && o.ok]); return env.confirmAnswer; };
  env.sent = [];
  env.ctx.RadioControl = { send: (o) => { env.sent.push(o); return true; }, radios: () => [] };
  env.load("account.js");
  env.load("admin.js");
  env.calls = calls;
  env.state = state;
  env.settle = async () => { for (let i = 0; i < 8; i++) await env.flush(); };
  env.byId = (id) => env.doc.documentElement.querySelectorAll("#" + id)[0] || null;
  env.text = () => env.doc.querySelector(".tm-admin").textContent;
  env.puts = () => calls.filter((c) => c.method === "PUT");
  env.refreshed = 0;
  env.doc.addEventListener("tr:refresh-status", () => { env.refreshed++; });
  return env;
}
const LIVE = { enabled: true, listening: true, port: 4532, listen: "local", ptt: true, clients: 1, paused: null, on_air: false, error: false,
               error_text: null, peers: [{ ip: "127.0.0.1", secs: 75, cmds: 12, tx: false }] };

test("D the tab shows the server's settings and status", async () => {
  const e = adminEnv({ enabled: true, ptt: true }, LIVE);
  e.ctx.TRAdmin.open("bridge");
  await e.settle();
  eq([e.byId("admBrOn").checked, e.byId("admBrPort").value, e.byId("admBrLocal").checked, e.byId("admBrNet").checked,
      e.byId("admBrPtt").checked, e.byId("admBrSilence").value], [true, "4532", true, false, true, "10"], "D the form mirrors config");
  eq([e.byId("admBrAllowWrap").hidden, e.byId("admBrPttWarn").hidden], [true, false], "D the allow list is hidden for this-PC-only; the transmit warning shows");
  const t = e.text();
  for (const part of ["listening on port 4532", "this PC only", "transmit allowed", "127.0.0.1", "12", "Hamlib NET rigctl", "localhost:4532", "Fake It"])
    check(t.includes(part), "D status has: " + part);
  e.byId("admBrNet").checked = true; e.byId("admBrLocal").checked = false;
  e.byId("admBrNet").dispatchEvent(new e.Event("change"));
  eq(e.byId("admBrAllowWrap").hidden, false, "D choosing other computers shows the allow list and its warning");
  check(/no password/.test(e.byId("admBrAllowWrap").textContent), "D the warning says the protocol has no password");
});
test("D Save sends exactly the form; allowing transmit asks first", async () => {
  const e = adminEnv({}, Object.assign({}, LIVE, { enabled: false, listening: false, clients: 0, peers: [], ptt: false }));
  e.ctx.TRAdmin.open("bridge");
  await e.settle();
  e.byId("admBrOn").checked = true;
  e.byId("admBrPort").value = " 4600 ";
  e.byId("admBrNet").checked = true; e.byId("admBrLocal").checked = false;
  e.byId("admBrAllow").value = "100.64.0.9\n192.168.2.0/24, 10.0.0.7";
  e.byId("admBrSilence").value = "15";
  e.byId("admBrSave").click();
  await e.settle();
  eq(e.puts().map((c) => { const b = Object.assign({}, c.body); delete b.expect; return b; }),
     [{ enabled: true, port: 4600, listen: "network", allow: ["100.64.0.9", "192.168.2.0/24", "10.0.0.7"],
        ptt: false, silence_s: 15 }], "D one PUT with the form's values (numbers as numbers)");
  eq(e.puts()[0].body.expect, { enabled: false, port: 4532, listen: "local", allow: [], ptt: false, silence_s: 10 },
     "D ... and `expect`: the settings the form was showing");
  eq([e.confirms.length, e.refreshed >= 1], [0, true], "D no question when transmit stays off; the page re-reads the status");
  e.byId("admBrPtt").checked = true;
  e.confirmAnswer = false;
  e.byId("admBrSave").click();
  await e.settle();
  eq([e.puts().length, e.confirms.length, e.confirms[0][1]], [1, 1, "Allow transmit"], "D allowing transmit asks; No sends nothing");
  e.confirmAnswer = true;
  e.byId("admBrPtt").checked = true;
  e.byId("admBrSave").click();
  await e.settle();
  eq([e.puts().length, e.puts()[1].body.ptt], [2, true], "D Yes saves it");
  e.byId("admBrSilence").value = "20";
  e.byId("admBrSave").click();
  await e.settle();
  eq([e.puts().length, e.confirms.length], [3, 2], "D no second question while transmit was already allowed");
});
test("D settings changed meanwhile (409 conflict): nothing is saved, the tab shows the saved ones and says why", async () => {
  const now = { enabled: true, port: 4600, listen: "local", allow: [], ptt: true, silence_s: 10 };
  const e = adminEnv({}, null, () => [409, { ok: false, error: "conflict", config: now,
    message: "The bridge settings were changed meanwhile (by another admin, or by hand in config.json). Here they are as saved now: check them and save again." }]);
  e.ctx.TRAdmin.open("bridge");
  await e.settle();
  const gets = e.state.gets;
  e.state.config = now;                                // what the server holds now
  e.byId("admBrSilence").value = "15";
  e.byId("admBrSave").click();
  await e.settle();
  eq([e.puts().length, e.puts()[0].body.expect.ptt], [1, false], "D the stale form's PUT carried what it showed");
  eq(e.state.gets > gets, true, "D the tab read the settings again");
  eq([e.byId("admBrPtt").checked, e.byId("admBrPort").value], [true, "4600"], "D ... and shows the saved ones");
  check(/changed meanwhile/.test(e.text()), "D the reason is shown: " + e.text().slice(0, 200));
});
test("D a refused save shows the server's message and changes nothing", async () => {
  const e = adminEnv({}, null, (b) => [400, { ok: false, error: "invalid", field: "port", message: "The port must be a whole number from 1024 to 65535." }]);
  e.ctx.TRAdmin.open("bridge");
  await e.settle();
  e.byId("admBrPort").value = "80";
  e.byId("admBrSave").click();
  await e.settle();
  eq([e.byId("admBrErr").hidden, e.byId("admBrErr").textContent, e.puts()[0].body.port, e.doc.activeElement === e.byId("admBrPort")],
     [false, "The port must be a whole number from 1024 to 65535.", 80, true], "D the message is shown and the field focused");
  e.byId("admBrPort").value = "abc";
  e.byId("admBrSave").click();
  await e.settle();
  eq(e.puts()[1].body.port, "abc", "D junk is sent as it is, for the server to refuse (never guessed into a number)");
});
test("D the status is re-read while the tab is open; a paused bridge can be resumed from here", async () => {
  const e = adminEnv({ enabled: true, ptt: true }, Object.assign({}, LIVE, { paused: { reason: "stopped", by: "OP1" } }));
  e.ctx.TRAdmin.open("bridge");
  await e.settle();
  check(e.text().includes("paused (stopped by OP1)"), "D the pause and who caused it");
  e.byId("admBrResume").click();
  eq(e.sent, [{ action: "bridge_tx", on: true }], "D Resume sends bridge_tx on");
  const g = e.state.gets;
  e.state.status = Object.assign({}, LIVE, { clients: 0, peers: [] });
  e.clock.advance(3100);
  await e.settle();
  check(e.state.gets > g && e.text().includes("No program is connected."), "D the status block follows the server (" + (e.state.gets - g) + " re-reads)");
  eq(e.byId("admBrPort").value, "4532", "D ... without touching the form");
  e.ctx.TRAdmin.close();
  const g2 = e.state.gets;
  e.clock.advance(10000);
  await e.settle();
  eq(e.state.gets, g2, "D no polling after the dialog closed");
});

(async () => {
  for (const [name, fn] of pending) {
    try { await fn(); } catch (err) { failures++; checks++; console.log("FAIL " + name + " threw: " + (err && err.stack || err)); }
  }
  console.log(failures ? failures + " of " + checks + " checks FAILED" : "ALL PASS (" + checks + " checks)");
  process.exit(failures ? 1 : 0);
})();
