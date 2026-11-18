/* The PTT hotkey (app.js "PTT hotkey", set with the dialog in device.js), tested on the REAL frontend
 * code (tr.js + app.js) in a Node vm on tests/fakedom.js, with the fake page + fake server of
 * tests/pttenv.js. No browser, no server process, no socket, no radio: "keyed" is a boolean in the
 * fake server. Run by tests/t_c1.py (unit group):
 *
 *     node tests/ptt_hotkey_test.js   -> exit 0 and "ALL PASS", or exit 1 naming each failure
 *
 * The rule (the PTT button's, I4 / G-1 c): asymmetric and fail-safe.
 *  - while this tab owns ptt or has a ptt claim pending, a press of the key ALWAYS unkeys;
 *  - only KEYING is held back: the re-key guard, another owner, a hidden page, an open dialog /
 *    confirm bar / menu, Arrange mode, a text field with a key that types, a held key's auto-repeat,
 *    a shortcut chord;
 *  - hold: key down = PTT, key up = unkey, losing the window focus unkeys; toggle: a press = a click.
 *
 *  A. no hotkey / a refused or broken stored value (Shift / Ctrl / Alt included): keys do nothing
 *  B. hold to talk: down keys, up unkeys; a short tap (up before the grant) leaves nothing keyed;
 *     a grant later than 3 s (key up before it, or still down) leaves nothing keyed; auto-repeat
 *     does nothing; window blur unkeys
 *  C. press to toggle: press keys, press unkeys, key-up does nothing, auto-repeat does nothing
 *  D. a press while owned always unkeys (keyed by the button, with a dialog open, in both modes)
 *  E. keying is refused: inside the re-key guard, another owner, a hidden tab, a viewer
 *  F. the key is left alone (not swallowed): typing in a field, an open dialog / confirm bar / menu
 *     (any visible one, modal or not), Arrange mode, a chord, a STOP control, Space for a control
 *     focused from the keyboard; a key that types nothing works from a field; a chord or a STOP
 *     control while transmitting still unkeys
 *  G. the server ends the session under a held key: nothing re-keys, the key-up sends nothing
 *  H. changing or removing the hotkey: tr:pttkey / storage; a change under a held key unkeys
 *  I. randomized property over key downs, ups, repeats, blurs, clicks, server stops and hides:
 *     (a) a hotkey press while owned/pending always ends unkeyed, (b) no ptt:on inside the guard,
 *     (c) hold mode: once the key is up and things settle, the radio is never keyed by the hotkey,
 *     (d) no claim is ever sent while the page is hidden or a dialog is open
 */
"use strict";
const { pageEnv, GUARD_MS, ON_AIR } = require("./pttenv");

let failures = 0, checks = 0;
const pending = [];
function check(cond, msg) { checks++; if (!cond) { failures++; console.log("FAIL " + msg); } }
function eq(a, b, msg) { const ja = JSON.stringify(a), jb = JSON.stringify(b); check(ja === jb, msg + ": got " + ja + ", want " + jb); }
function test(name, fn) { pending.push([name, fn]); }

const STORE = "telerig.pttkey";
function env(lat, hk, perms) {
  const ls = {};
  if (hk !== undefined && hk !== null) ls[STORE] = typeof hk === "string" ? hk : JSON.stringify(hk);
  const e = pageEnv(lat === undefined ? 20 : lat, {
    localStorage: ls,
    before: (x) => { x.el("div", { id: "userMenu", hidden: true }); x.el("button", { id: "userBtn" }); },
  });
  if (perms) e.frame({ type: "hello", v: 1, conn: 3, user: "ME", role: perms.role, perms: perms.perms, status: {}, tx: { owner: null }, hb_interval: 2 });
  const fire = (type, code, init) => {
    const o = Object.assign({ code, key: code }, init || {});
    const on = o.on || e.doc;
    delete o.on;
    const ev = new e.Event(type, o);
    on.dispatchEvent(ev);
    return ev;
  };
  e.kd = (code, init) => { e.curOp = "hk-down"; return fire("keydown", code, init); };
  e.ku = (code, init) => { e.curOp = "hk-up"; return fire("keyup", code, init); };
  e.rep = (code, init) => { e.curOp = "hk-repeat"; return fire("keydown", code, Object.assign({ repeat: true }, init || {})); };
  e.blur = () => { e.curOp = "blur"; e.win("blur", {}); };
  e.settle = () => e.adv(2 * e.lat + 5);
  e.field = (tag, type) => e.el(tag, type ? { type } : {});
  e.hot = () => e.ctx.RadioTx.hotkey();
  return e;
}
const HOLD = { code: "Space", mode: "hold", label: "Space" };
const TOGGLE = { code: "Space", mode: "toggle", label: "Space" };
const F9 = { code: "F9", mode: "hold", label: "F9" };

// ---- A. no hotkey, refused or broken values -------------------------------------------------------
test("A no hotkey: key events do nothing and are not swallowed", async () => {
  const e = env(20, null);
  eq(e.hot(), null, "A no hotkey configured");
  const d = e.kd("Space"), u = e.ku("Space");
  e.settle();
  eq([e.frames(), d.defaultPrevented, u.defaultPrevented, e.S.keyed], [[], false, false, false], "A nothing sent, nothing swallowed");
  eq(e.btn.title, "", "A the PTT button names no hotkey");
});
for (const bad of ["{not json", JSON.stringify({ code: "Escape", mode: "hold" }), JSON.stringify({ code: "Enter" }),
                   JSON.stringify({ code: "Tab" }), JSON.stringify({ code: "F12" }), JSON.stringify({ code: "ArrowUp" }),
                   JSON.stringify({ code: "MetaLeft" }), JSON.stringify({ code: "" }), JSON.stringify({ code: 5 }),
                   JSON.stringify({ code: "Key A" }), JSON.stringify({ mode: "hold" }), "null", "[]", JSON.stringify("Space"),
                   JSON.stringify({ code: "ControlRight", mode: "hold" }), JSON.stringify({ code: "ShiftLeft" }),
                   JSON.stringify({ code: "AltRight", mode: "toggle" }), JSON.stringify({ code: "AltGraph" })]) {
  test("A a stored value that is not a usable hotkey is ignored: " + bad, async () => {
    const e = env(20, bad);
    eq(e.hot(), null, "A ignored " + bad);
    let code = "Space";
    try { const j = JSON.parse(bad); if (j && typeof j.code === "string" && j.code) code = j.code; } catch (_) {}
    e.kd(code); e.ku(code); e.settle();
    eq([e.frames(), e.S.keyed], [[], false], "A nothing keyed for " + bad);
  });
}
test("A the allowed / refused keys", async () => {
  const e = env(20, null);
  const ok = (c) => e.ctx.RadioTx.hotkeyAllowed(c);
  eq(["Space", "F9", "F13", "F24", "KeyA", "Digit1", "Numpad0", "Backquote", "Pause"].map(ok), Array(9).fill(true), "A allowed");
  eq(["Escape", "Tab", "Enter", "NumpadEnter", "Backspace", "Delete", "ArrowLeft", "F5", "F11", "F12", "MetaLeft", "CapsLock",
      "Unidentified", "", "Key A", null, undefined, 7, "x".repeat(40)].map(ok), Array(19).fill(false), "A refused");
  eq(["ShiftLeft", "ShiftRight", "ControlLeft", "ControlRight", "AltLeft", "AltRight", "AltGraph"].map(ok), Array(7).fill(false),
     "A Shift / Ctrl / Alt / AltGr are refused (other shortcuts use them; AltGr types)");
});
test("A mode defaults to hold; the PTT button names the key", async () => {
  const e = env(20, { code: "F9" });
  eq(e.hot(), { code: "F9", mode: "hold", label: "F9" }, "A default mode + label");
  eq(e.btn.title, "PTT — hotkey: F9 (hold to talk)", "A button title");
  const t = env(20, { code: "F9", mode: "toggle", label: "F9" });
  eq(t.btn.title, "PTT — hotkey: F9 (press to toggle)", "A toggle title");
});

// ---- B. hold to talk -------------------------------------------------------------------------------
for (const lat of [5, 40, 120]) {
  test(`B hold: key down keys, key up unkeys (one-way ${lat} ms)`, async () => {
    const e = env(lat, HOLD);
    const d = e.kd("Space");
    eq([e.ptt(), d.defaultPrevented], [["ptt:on"], true], "B key down sent ptt:on and was swallowed");
    e.settle();
    eq([e.S.keyed, ON_AIR.test(e.label())], [true, true], "B keyed while the key is down");
    e.adv(3000);
    eq(e.S.keyed, true, "B still keyed while held");
    const u = e.ku("Space");
    eq([e.ptt(), u.defaultPrevented], [["ptt:on", "ptt:off"], true], "B key up sent ptt:off at once");
    e.settle();
    eq([e.S.keyed, e.S.keys, e.label()], [false, 1, "PTT"], "B unkeyed");
  });
  test(`B hold: a short tap, key up before the grant (one-way ${lat} ms)`, async () => {
    const e = env(lat, HOLD);
    e.kd("Space");
    e.adv(Math.max(1, Math.floor(lat / 2)));          // the claim has not even reached the server
    e.ku("Space");
    eq(e.ptt(), ["ptt:on", "ptt:off(auto)"], "B the pending claim is released");
    e.adv(4 * lat + 50);
    eq([e.S.keyed, e.label()], [false, "PTT"], "B nothing stays keyed");
    e.adv(5000);
    eq([e.S.keyed, e.ons()], [false, 1], "B and nothing keys later");
  });
}
test("B hold: a grant later than 3 s, the key up before it lands: released, nothing stays keyed", async () => {
  const e = env(2200, HOLD);                           // the grant lands 4.4 s after the key down
  e.kd("Space");
  e.adv(1000);
  e.ku("Space");
  eq(e.ptt(), ["ptt:on", "ptt:off(auto)"], "B the release goes out with the key up (the claim is unanswered)");
  e.adv(12000);
  eq([e.S.keyed, e.label()], [false, "PTT"], "B the late grant was unkeyed again: nothing stays keyed");
  eq(e.ons(), 1, "B one claim only");
});
test("B hold: no answer within 3 s with the key still down: the claim is given up AND released", async () => {
  const e = env(2200, HOLD);
  e.kd("Space");
  e.adv(4500);                                         // the heartbeat tick gives the claim up after 3 s
  eq(e.ptt(), ["ptt:on", "ptt:off(auto)"], "B given up and released");
  e.adv(8000);
  eq(e.S.keyed, false, "B the grant that landed late was unkeyed again");
  e.ku("Space"); e.adv(6000);
  eq([e.S.keyed, e.ptt()], [false, ["ptt:on", "ptt:off(auto)"]], "B the key-up keys nothing and sends nothing more");
});
test("B a button claim nobody answers within 3 s is released too (a late grant never stays keyed)", async () => {
  const e = env(2600, null);
  e.mouse(1);
  e.adv(4500);
  eq(e.ptt(), ["ptt:on", "ptt:off(auto)"], "B given up and released");
  e.adv(10000);
  eq([e.S.keyed, e.label()], [false, "PTT"], "B nothing stays keyed");
});
test("B hold: a held key's auto-repeat neither keys again nor unkeys", async () => {
  const e = env(20, HOLD);
  e.kd("Space"); e.settle();
  for (let i = 0; i < 30; i++) { const r = e.rep("Space"); check(r.defaultPrevented, "B the repeat is swallowed (no page scroll)"); e.adv(33); }
  eq([e.S.keyed, e.ptt()], [true, ["ptt:on"]], "B still keyed, nothing else sent");
  e.ku("Space"); e.settle();
  eq([e.S.keyed, e.ptt()], [false, ["ptt:on", "ptt:off"]], "B released on key up");
});
test("B hold: the window losing the focus unkeys (the key-up goes elsewhere)", async () => {
  const e = env(20, HOLD);
  e.kd("Space"); e.settle();
  e.blur();
  eq(e.ptt(), ["ptt:on", "ptt:off"], "B blur sent ptt:off");
  e.settle();
  eq(e.S.keyed, false, "B unkeyed");
  e.ku("Space"); e.adv(2000);                          // a late key-up (focus came back) does nothing
  eq([e.ptt(), e.S.keyed], [["ptt:on", "ptt:off"], false], "B the late key-up sends nothing");
});
test("B hold: blur before the grant releases the pending claim", async () => {
  const e = env(80, HOLD);
  e.kd("Space"); e.adv(10); e.blur();
  eq(e.ptt(), ["ptt:on", "ptt:off(auto)"], "B released");
  e.adv(1000);
  eq(e.S.keyed, false, "B not keyed");
});
test("B hold: the client failsafe / heartbeats run as for a click", async () => {
  const e = env(20, HOLD);
  e.kd("Space"); e.adv(5000);
  check(e.S.hb >= 2, "B heartbeats while held (" + e.S.hb + ")");
  e.ku("Space"); e.settle();
  const hb = e.S.hb;
  e.adv(6000);
  check(e.S.hb - hb <= 1, "B heartbeats stop after the release");
});

// ---- C. press to toggle -----------------------------------------------------------------------------
test("C toggle: press keys, key-up does nothing, the next press unkeys", async () => {
  const e = env(20, TOGGLE);
  const d = e.kd("Space"), u = e.ku("Space");
  eq([e.ptt(), d.defaultPrevented, u.defaultPrevented], [["ptt:on"], true, true], "C keyed by the press; both events swallowed");
  e.settle(); e.adv(4000);
  eq(e.S.keyed, true, "C stays keyed after the key-up");
  e.kd("Space"); e.ku("Space");
  eq(e.ptt(), ["ptt:on", "ptt:off"], "C the second press unkeys");
  e.settle();
  eq([e.S.keyed, e.S.keys], [false, 1], "C unkeyed");
});
test("C toggle: auto-repeat of a long press never unkeys or re-keys", async () => {
  const e = env(20, TOGGLE);
  e.kd("Space"); e.settle();
  for (let i = 0; i < 40; i++) { e.rep("Space"); e.adv(33); }
  e.ku("Space"); e.settle();
  eq([e.S.keyed, e.ptt()], [true, ["ptt:on"]], "C a long press is still ONE press");
  e.kd("Space");
  for (let i = 0; i < 40; i++) { e.rep("Space"); e.adv(33); }
  e.ku("Space"); e.adv(3000);
  eq([e.S.keyed, e.ptt()], [false, ["ptt:on", "ptt:off"]], "C a long second press unkeys once and does not re-key");
});
test("C toggle: the window losing the focus does NOT unkey (like the clicked button)", async () => {
  const e = env(20, TOGGLE);
  e.kd("Space"); e.ku("Space"); e.settle();
  e.blur(); e.adv(1000);
  eq([e.S.keyed, e.ptt()], [true, ["ptt:on"]], "C still keyed");
  e.hide();
  e.settle();
  eq(e.S.keyed, false, "C hiding the tab releases, as always");
});

// ---- D. a press while owned always unkeys -------------------------------------------------------------
for (const hk of [HOLD, TOGGLE]) {
  test(`D ${hk.mode}: keyed by the button, the hotkey unkeys`, async () => {
    const e = env(20, hk);
    e.mouse(1); e.settle();
    eq(e.S.keyed, true, "D keyed by a click");
    e.kd("Space");
    eq(e.ptt(), ["ptt:on", "ptt:off"], "D the hotkey press unkeys");
    e.settle();
    e.ku("Space"); e.adv(1500);
    eq([e.S.keyed, e.ptt()], [false, ["ptt:on", "ptt:off"]], "D the key-up that follows does nothing (and nothing re-keys)");
  });
  test(`D ${hk.mode}: a claim still pending is released by the hotkey`, async () => {
    const e = env(100, hk);
    e.mouse(1); e.adv(20);                             // pending
    e.kd("Space"); e.ku("Space");
    eq(e.ptt(), ["ptt:on", "ptt:off(auto)"], "D pending claim released");
    e.adv(1500);
    eq(e.S.keyed, false, "D not keyed");
  });
  test(`D ${hk.mode}: while this tab transmits, the hotkey unkeys even with a dialog open or in Arrange mode`, async () => {
    for (const mark of ["tr-modal-open", "arranging"]) {
      const e = env(20, hk);
      e.mouse(1); e.settle();
      e.body.classList.add(mark);
      const d = e.kd("Space");
      e.settle();
      eq([e.S.keyed, d.defaultPrevented], [false, true], "D unkeyed with " + mark);
    }
  });
}
test("D a key that types, pressed in a text field while transmitting, is text (documented exception); F9 unkeys from a field", async () => {
  const e = env(20, TOGGLE);
  e.mouse(1); e.settle();
  const inp = e.field("input", "text");
  const d = e.kd("Space", { on: inp });
  e.settle();
  eq([e.S.keyed, d.defaultPrevented], [true, false], "D Space in a field types a space");
  const f = env(20, { code: "F9", mode: "toggle", label: "F9" });
  f.mouse(1); f.settle();
  const inp2 = f.field("input", "text");
  f.kd("F9", { on: inp2 });
  f.settle();
  eq(f.S.keyed, false, "D F9 unkeys from a field");
});

// ---- E. keying is refused -----------------------------------------------------------------------------
test("E the re-key guard: a press right after this tab's PTT ended is ignored, then it keys again", async () => {
  const e = env(20, HOLD);
  e.kd("Space"); e.settle(); e.ku("Space"); e.settle();
  e.adv(300);
  e.kd("Space"); e.adv(50); e.ku("Space");
  eq(e.ons(), 1, "E a press 300 ms after the release keys nothing");
  e.adv(GUARD_MS + 60);
  e.kd("Space"); e.settle();
  eq([e.ons(), e.S.keyed], [2, true], "E after the guard the key works again");
  e.ku("Space"); e.settle();
  eq(e.S.keyed, false, "E released");
});
test("E mashing the key to stop never re-keys (hold and toggle)", async () => {
  for (const hk of [HOLD, TOGGLE]) {
    const e = env(40, hk);
    e.mouse(1); e.settle();
    for (let i = 0; i < 12; i++) { e.kd("Space"); e.adv(60); e.ku("Space"); e.adv(140); }
    e.adv(3000);
    eq([e.S.keyed, e.S.keys, e.ons()], [false, 1, 1], `E ${hk.mode}: one key, ends unkeyed, never re-keyed`);
  }
});
test("E another owner: the hotkey keys nothing and says why", async () => {
  const e = env(20, HOLD);
  e.frame({ type: "tx", owner: { conn: 9, user: "OTHER", claims: ["ptt"], elapsed_ms: 0, external: false } });
  const d = e.kd("Space"); e.ku("Space"); e.settle();
  eq([e.frames(), e.S.keyed, d.defaultPrevented], [[], false, true], "E nothing sent");
  check(e.toasts.some((t) => /OTHER is transmitting/.test(t[0])), "E a hint names the owner: " + JSON.stringify(e.toasts));
});
test("E a hidden tab: no claim is sent; a hold that was down when the tab hid is released", async () => {
  const e = env(20, HOLD);
  e.hide();
  e.kd("Space"); e.adv(100); e.ku("Space"); e.adv(500);
  eq([e.frames(), e.claimsSentAway.length], [[], 0], "E nothing sent while hidden");
  e.show();
  e.kd("Space"); e.settle();
  eq(e.S.keyed, true, "E keyed when visible");
  e.hide(); e.settle();
  eq(e.S.keyed, false, "E hiding releases");
  e.ku("Space"); e.show(); e.adv(2000);
  eq([e.S.keyed, e.ons()], [false, 1], "E nothing re-keys");
});
test("E a viewer has no hotkey effect", async () => {
  const e = env(20, HOLD, { role: "viewer", perms: ["view", "listen", "menu_read"] });
  e.kd("Space"); e.ku("Space"); e.settle();
  eq([e.frames(), e.S.keyed], [[], false], "E nothing sent");
});

// ---- F. the key is left alone ---------------------------------------------------------------------------
test("F typing: a key that types is text in input / textarea / select / contenteditable, and is not swallowed", async () => {
  const e = env(20, HOLD);
  const targets = [e.field("input"), e.field("input", "text"), e.field("input", "search"), e.field("input", "number"),
                   e.field("input", "password"), e.field("textarea"), e.field("select")];
  const ce = e.field("div"); ce.isContentEditable = true; targets.push(ce);
  for (const t of targets) {
    const d = e.kd("Space", { on: t }), u = e.ku("Space", { on: t });
    eq([d.defaultPrevented, u.defaultPrevented], [false, false], "F not swallowed in <" + t.tagName + " type=" + (t.type || "") + ">");
  }
  e.settle();
  eq([e.frames(), e.S.keyed], [[], false], "F nothing keyed while typing");
});
test("F typing: buttons, checkboxes and sliders are not text fields (the hotkey acts and the control is not activated)", async () => {
  for (const [tag, type] of [["button", null], ["input", "checkbox"], ["input", "range"], ["input", "radio"], ["a", null], ["div", null]]) {
    const e = env(20, HOLD);
    const t = e.field(tag, type);
    const d = e.kd("Space", { on: t });
    e.settle();
    const u = e.ku("Space", { on: t });
    e.settle();
    eq([d.defaultPrevented, u.defaultPrevented, e.ptt(), e.S.keyed], [true, true, ["ptt:on", "ptt:off"], false],
       "F acts on <" + tag + (type ? " type=" + type : "") + ">");
  }
});
test("F the PTT button itself has the focus: one action per press, not two", async () => {
  const e = env(20, TOGGLE);
  const d = e.kd("Space", { on: e.btn });
  const u = e.ku("Space", { on: e.btn });
  if (!u.defaultPrevented && !d.defaultPrevented) e.btn.dispatchEvent(new e.Event("click", { detail: 0, pointerType: "" }));   // a browser's own click
  e.settle();
  eq([e.ptt(), e.S.keyed, d.defaultPrevented, u.defaultPrevented], [["ptt:on"], true, true, true], "F keyed once; the button's own click is suppressed");
});
test("F a key that types nothing (F9) works from a text field", async () => {
  const e = env(20, F9);
  const inp = e.field("input", "text");
  e.kd("F9", { on: inp }); e.settle();
  eq(e.S.keyed, true, "F keyed from a field");
  e.ku("F9", { on: inp }); e.settle();
  eq(e.S.keyed, false, "F released");
});
test("F an open dialog, confirm bar or menu, and Arrange mode keep their keys (nothing keyed, nothing swallowed)", async () => {
  const setups = [
    ["dialog", (e) => e.body.classList.add("tr-modal-open"), (e) => e.body.classList.remove("tr-modal-open")],
    ["arrange", (e) => e.body.classList.add("arranging"), (e) => e.body.classList.remove("arranging")],
    ["confirm bar", (e) => { e._c = e.el("div", { class: "tr-confirm" }); }, (e) => e._c.remove()],
    ["menu", (e) => { e.ctx.TR.menu = { isOpen: () => true, close() {} }; }, (e) => { e.ctx.TR.menu = { isOpen: () => false, close() {} }; }],
  ];
  for (const [name, on, off] of setups) {
    const e = env(20, HOLD);
    on(e);
    const d = e.kd("Space"), u = e.ku("Space");
    e.settle();
    eq([e.frames(), e.S.keyed, d.defaultPrevented, u.defaultPrevented], [[], false, false, false], "F " + name + ": untouched");
    off(e);
    e.kd("Space"); e.settle();
    eq(e.S.keyed, true, "F " + name + " closed: the key works");
    e.ku("Space"); e.settle();
  }
});
test("F a chord (Ctrl / Alt / Meta + the key) is not the hotkey; Shift alone does not matter", async () => {
  const e = env(20, HOLD);
  for (const m of ["ctrlKey", "altKey", "metaKey"]) {
    const d = e.kd("Space", { [m]: true }); e.ku("Space", { [m]: true });
    eq(d.defaultPrevented, false, "F " + m + "+Space left alone");
  }
  e.settle();
  eq(e.frames(), [], "F no chord keyed");
  e.kd("Space", { shiftKey: true }); e.settle();
  eq(e.S.keyed, true, "F Shift+Space is still the key");
  e.ku("Space", { shiftKey: true }); e.settle();
});
test("F a chord while transmitting still unkeys: a chord only holds back KEYING (toggle and hold)", async () => {
  const t = env(20, { code: "F9", mode: "toggle", label: "F9" });
  t.kd("F9"); t.ku("F9"); t.settle();
  eq(t.S.keyed, true, "F toggle keyed");
  for (const mod of ["ctrlKey", "altKey", "metaKey"]) {
    t.adv(GUARD_MS + 50);
    if (!t.S.keyed) { t.kd("F9"); t.ku("F9"); t.settle(); }
    t.kd("F9", { [mod]: true }); t.ku("F9"); t.settle();
    eq(t.S.keyed, false, "F toggle: " + mod + " + F9 while transmitting unkeys");
    t.adv(GUARD_MS + 50);
    t.kd("F9", { [mod]: true }); t.ku("F9"); t.settle();
    eq(t.S.keyed, false, "F toggle: " + mod + " + F9 while idle keys nothing");
  }
  const h = env(20, F9);
  h.kd("F9"); h.settle();
  eq(h.S.keyed, true, "F hold keyed");
  h.kd("F9", { altKey: true }); h.settle();
  eq(h.S.keyed, false, "F hold: Alt + F9 while the key is down unkeys");
  h.ku("F9"); h.settle();
  eq(h.S.keyed, false, "F and nothing re-keys");
});
test("F a STOP control keeps its key: the hotkey never keys from it; while transmitting it unkeys and the STOP control still gets the key", async () => {
  for (const id of ["onAirStop", "txStopFab", "cwTx"]) {
    const e = env(20, { code: "F9", mode: "toggle", label: "F9" });
    const stop = e.el("button", { id });
    let d = e.kd("F9", { on: stop }); e.ku("F9", { on: stop }); e.settle();
    eq([e.frames(), d.defaultPrevented], [[], false], "F nothing keyed from #" + id + ", the key is left to it");
    e.kd("F9"); e.ku("F9"); e.settle();
    eq(e.S.keyed, true, "F keyed from elsewhere");
    d = e.kd("F9", { on: stop }); e.ku("F9", { on: stop }); e.settle();
    eq([e.S.keyed, d.defaultPrevented], [false, false], "F unkeyed from #" + id + " and the key was not taken from it");
  }
});
test("F any visible dialog / alertdialog / menu (modal or not, a popover too) holds back keying; a hidden one does not", async () => {
  for (const role of ["dialog", "alertdialog", "menu"]) {
    const e = env(20, F9);
    const p = e.el("div", { role });
    const d = e.kd("F9"); e.ku("F9"); e.settle();
    eq([e.frames(), d.defaultPrevented], [[], false], "F nothing keyed while a " + role + " is shown");
    p.hidden = true;
    e.kd("F9"); e.settle();
    eq(e.S.keyed, true, "F keys once the " + role + " is hidden");
    e.ku("F9"); e.settle();
    p.hidden = false;
    e.adv(GUARD_MS + 50);
    e.mouse(1); e.settle();
    eq(e.S.keyed, true, "F keyed by the button with the " + role + " shown");
    e.kd("F9"); e.settle();
    eq(e.S.keyed, false, "F while transmitting the hotkey still unkeys with the " + role + " shown");
    e.ku("F9"); e.settle();
  }
});
test("F Space for a control that has the focus from the KEYBOARD is that control's key; a button focused by a click is not", async () => {
  const e = env(20, HOLD);
  const b = e.el("button", { id: "someBtn" });
  b.dispatchEvent(new e.Event("focusin"));            // Tab moved the focus here (no pointer)
  let d = e.kd("Space", { on: b }); e.ku("Space", { on: b }); e.settle();
  eq([e.frames(), d.defaultPrevented], [[], false], "F keyboard focus: Space stays the button's, nothing keyed");
  const r = e.el("div", { role: "switch", tabindex: "0" });
  r.dispatchEvent(new e.Event("focusin"));
  d = e.kd("Space", { on: r }); e.ku("Space", { on: r }); e.settle();
  eq([e.frames(), d.defaultPrevented], [[], false], "F also for a role=switch control");
  b.dispatchEvent(new e.Event("pointerdown", { pointerType: "mouse" }));
  b.dispatchEvent(new e.Event("focusin"));            // a click put the focus here
  d = e.kd("Space", { on: b }); e.settle();
  eq([e.S.keyed, d.defaultPrevented], [true, true], "F a clicked button does not keep Space: the hotkey keys");
  const other = e.el("button", { id: "other" });
  other.dispatchEvent(new e.Event("focusin"));        // Tab to another control while transmitting
  e.ku("Space", { on: other }); e.settle();
  eq(e.S.keyed, false, "F the key-up unkeys wherever the focus went");
  e.adv(GUARD_MS + 50);
  const t = env(20, TOGGLE);
  t.mouse(1); t.settle();
  const b2 = t.el("button", { id: "b2" });
  b2.dispatchEvent(new t.Event("focusin"));
  t.kd("Space", { on: b2 }); t.ku("Space", { on: b2 }); t.settle();
  eq(t.S.keyed, false, "F while transmitting, Space on a keyboard-focused control still unkeys");
  t.adv(GUARD_MS + 50);
  const f = env(20, F9);
  const b3 = f.el("button", { id: "b3" });
  b3.dispatchEvent(new f.Event("focusin"));
  f.kd("F9", { on: b3 }); f.settle();
  eq(f.S.keyed, true, "F only Space belongs to a focused control: F9 keys from it");
  f.ku("F9", { on: b3 }); f.settle();
});
test("F other keys are never touched", async () => {
  const e = env(20, HOLD);
  for (const code of ["KeyA", "Enter", "Escape", "F9", "ArrowUp", "ControlRight"]) {
    const d = e.kd(code), u = e.ku(code);
    eq([d.defaultPrevented, u.defaultPrevented], [false, false], "F " + code + " untouched");
  }
  e.settle();
  eq(e.frames(), [], "F nothing sent");
});

// ---- G. the server ends the session under a held key --------------------------------------------------
test("G hold: a STOP by another operator while the key is down: nothing re-keys, the key-up sends nothing", async () => {
  const e = env(20, HOLD);
  e.kd("Space"); e.settle();
  e.serverEnds(); e.settle();
  eq(e.S.keyed, false, "G stopped by the server");
  for (let i = 0; i < 20; i++) { e.rep("Space"); e.adv(33); }
  e.ku("Space"); e.adv(2000);
  eq([e.ptt(), e.S.keyed, e.S.keys], [["ptt:on"], false, 1], "G the held key did not re-key and its key-up sent nothing");
  e.kd("Space"); e.settle();
  eq(e.S.keyed, true, "G a fresh press later keys again");
  e.ku("Space"); e.settle();
});
test("G hold: denied / busy answers leave nothing keyed and nothing stuck", async () => {
  const e = env(20, HOLD);
  const ws = e.sock;
  const real = ws.send;
  ws.send = (d) => {                                   // this server refuses the claim
    const f = JSON.parse(d);
    if (f.action === "ptt" && f.tx === true) {
      e.clock.setTimeout(() => e.frame({ type: "busy", action: "ptt", owner: "OTHER", req: { conn: 3, seq: f.seq } }), e.lat);
      return;
    }
    real(d);
  };
  e.kd("Space"); e.adv(200);
  eq(e.S.keyed, false, "G refused");
  e.ku("Space"); e.adv(1000);
  eq(e.S.keyed, false, "G still not keyed after the key-up");
});

// ---- H. changing the hotkey ---------------------------------------------------------------------------------
test("H tr:pttkey re-reads the stored key; removing it disables it", async () => {
  const e = env(20, HOLD);
  e.ctx.localStorage.setItem(STORE, JSON.stringify(F9));
  e.doc.dispatchEvent(new e.Event("tr:pttkey"));
  eq(e.hot(), F9, "H the new key is active");
  e.kd("Space"); e.ku("Space"); e.settle();
  eq(e.frames(), [], "H the old key does nothing");
  e.kd("F9"); e.settle();
  eq(e.S.keyed, true, "H the new key keys");
  e.ku("F9"); e.settle();
  e.ctx.localStorage.removeItem(STORE);
  e.doc.dispatchEvent(new e.Event("tr:pttkey"));
  eq([e.hot(), e.btn.title], [null, ""], "H removed");
  e.adv(GUARD_MS + 50);
  e.kd("F9"); e.ku("F9"); e.settle();
  eq(e.S.keyed, false, "H no hotkey any more");
});
test("H a change in another tab (storage event) is picked up", async () => {
  const e = env(20, null);
  e.ctx.localStorage.setItem(STORE, JSON.stringify(TOGGLE));
  e.win("storage", { key: "some.other.key" });
  eq(e.hot(), null, "H an unrelated storage event changes nothing");
  e.win("storage", { key: STORE });
  eq(e.hot(), TOGGLE, "H the hotkey from the other tab is active");
});
test("H changing the key while it is held unkeys", async () => {
  const e = env(20, HOLD);
  e.kd("Space"); e.settle();
  eq(e.S.keyed, true, "H keyed");
  e.ctx.localStorage.setItem(STORE, JSON.stringify(F9));
  e.doc.dispatchEvent(new e.Event("tr:pttkey"));
  e.settle();
  eq(e.S.keyed, false, "H the change released the held transmission");
  e.ku("Space"); e.adv(1500);
  eq([e.S.keyed, e.ons()], [false, 1], "H the old key's key-up does nothing");
});

// ---- I. randomized property -----------------------------------------------------------------------------------
function rng(seed) { let s = seed >>> 0; return () => { s = (Math.imul(s, 1664525) + 1013904223) >>> 0; return s / 4294967296; }; }
for (const mode of ["hold", "toggle"]) {
  test(`I property (${mode}): random key downs, ups, repeats, blurs, clicks, server stops, hides, dialogs`, async () => {
    let bad = 0, first = "", presses = 0, ownedPresses = 0, onsTotal = 0, blocked = 0;
    for (let run = 0; run < 70; run++) {
      const rnd = rng(1000 + run * 7919 + (mode === "hold" ? 0 : 1));
      const lat = [5, 20, 60, 120][Math.floor(rnd() * 4)];
      const e = env(lat, { code: "Space", mode, label: "Space" });
      const tx = e.ctx.RadioTx;
      let down = false, dialog = false;
      const trace = [];
      const fail = (m) => { bad++; if (!first) first = `run ${run} (lat ${lat}): ${m}; trace ${trace.join(" ")}`; };
      const sentAt = [];                               // [time, label, pageAway, dialogOpen, op] for every ptt frame
      const prev = e.sock.send;
      e.sock.send = (d) => {
        const f = JSON.parse(d);
        if (f.action === "ptt") sentAt.push([e.clock.now, f.tx ? "on" : "off", e.awayNow(), dialog, e.curOp]);
        prev(d);
      };
      for (let i = 0; i < 60; i++) {
        const r = rnd();
        // "this tab owns ptt or its ptt claim is pending", exactly as the PTT button shows it (the
        // label is redrawn synchronously): a claim already released with ptt:off(auto) is not that
        const owned = ON_AIR.test(e.label());
        if (r < 0.30 && !down) {
          trace.push("D");
          presses++;
          if (owned) ownedPresses++;
          if (dialog && !owned) blocked++;
          const offsBefore = e.offs();
          e.kd("Space"); down = true;
          if (owned && e.offs() !== offsBefore + 1) fail("a press while owned/pending did not send ptt:off at once");
        } else if (r < 0.55 && down) {
          trace.push("U"); e.ku("Space"); down = false;
        } else if (r < 0.63 && down) {
          trace.push("R"); e.rep("Space");
        } else if (r < 0.68) {
          trace.push("B"); e.blur(); if (mode === "hold") { /* the key may still be physically down */ }
        } else if (r < 0.74) {
          // a click on PTT: a dialog's backdrop covers the button, so no click reaches it then
          if (!dialog) { trace.push("C"); e.mouse(1); } else trace.push("c");
        } else if (r < 0.79) {
          trace.push("S"); e.serverEnds();
        } else if (r < 0.83) {
          trace.push(e.doc.hidden ? "v" : "h"); if (e.doc.hidden) e.show(); else e.hide();
        } else if (r < 0.88) {
          dialog = !dialog; trace.push(dialog ? "m" : "n");
          // a dialog closes when this tab transmits (account.js); model: it can only open while idle
          if (dialog && tx.ownedOrPending()) dialog = false;
          e.body.classList.toggle("tr-modal-open", dialog);
        } else {
          trace.push("w");
        }
        e.adv([0, 15, 60, 200, 450, 900][Math.floor(rnd() * 6)]);
        if (dialog && tx.ownedOrPending()) { dialog = false; e.body.classList.remove("tr-modal-open"); }
      }
      if (down) { trace.push("U"); e.ku("Space"); down = false; }
      if (e.doc.hidden) e.show();
      e.adv(4 * lat + 50);
      // (c) hold: the key is up and things have settled. Whatever is keyed now was keyed by a CLICK
      //     (the last ptt:on came from a mouse op), never by the hotkey.
      if (mode === "hold" && e.S.keyed) {
        const lastOn = e.log.filter((x) => x[1] === "ptt:on").pop();
        if (!lastOn || !/^mouse/.test(lastOn[2])) fail("hold: keyed with the key up, last ptt:on from " + (lastOn && lastOn[2]));
      }
      // (b) no ptt:on inside the guard after this page's own PTT ended
      const ons = e.log.filter((x) => x[1] === "ptt:on").map((x) => x[0]);
      onsTotal += ons.length;
      const ends = e.unkeyTimesSeen || [];
      for (const t of ons) for (const off of e.offSeen) if (t > off && t - off < GUARD_MS) fail(`ptt:on ${t - off} ms after this tab's PTT ended`);
      void ends;
      // (d) no claim while hidden or while a dialog is open
      for (const [t, lbl, away, dlg, op] of sentAt) {
        if (lbl === "on" && away) fail("ptt:on sent at " + t + " while the page was hidden (" + op + ")");
        if (lbl === "on" && dlg) fail("ptt:on sent at " + t + " while a dialog was open (" + op + ")");
      }
      // (a) settle fully: if the last hotkey action was a press while owned, nothing is keyed by it
      e.adv(3000);
      if (mode === "hold" && e.S.keyed) {
        const lastOn = e.log.filter((x) => x[1] === "ptt:on").pop();
        if (!lastOn || !/^mouse/.test(lastOn[2])) fail("hold: still keyed by the hotkey after 3 s with the key up");
      }
    }
    check(bad === 0, `I property (${mode}): ${bad} violations: ${first}`);
    check(presses > 300 && ownedPresses > 30 && onsTotal > 150 && blocked > 10,
          `I property (${mode}) exercised: ${presses} presses, ${ownedPresses} while owned/pending, ${onsTotal} ptt:on, ${blocked} presses under a dialog`);
    console.log(`info I property (${mode}): 70 runs x 60 ops: ${presses} hotkey presses, ${ownedPresses} while owned/pending, ${onsTotal} ptt:on, ${blocked} under a dialog`);
  });
}

(async () => {
  for (const [name, fn] of pending) {
    try { await fn(); } catch (err) { failures++; checks++; console.log("FAIL " + name + " threw: " + (err && err.stack || err)); }
  }
  console.log(failures ? failures + " of " + checks + " checks FAILED" : "ALL PASS (" + checks + " checks)");
  process.exit(failures ? 1 : 0);
})();
