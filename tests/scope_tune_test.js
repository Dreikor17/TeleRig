/* Tuning from the Scope window, tested on the REAL frontend (tr.js + app.js) in a Node vm on
 * tests/fakedom.js with a fake clock. No browser, no server, no radio: the "server" is a fake WebSocket
 * whose sent messages the test reads. Nothing here transmits, and the test checks that: the only messages
 * these controls may send are the frequency's own `tune` and `set_freq`. Run by tests/t_c1.py (unit):
 *
 *     node tests/scope_tune_test.js     -> exit 0 and "ALL PASS", or exit 1 naming each failure
 *
 *  S1. nothing that scrolls tunes on the scope: the wheel sends nothing and is left to the page (not
 *      prevented); a finger or a pen moving across it sends nothing
 *  S2. what still tunes there: a finger tap, a pen tap and a mouse click go to the frequency under them
 *      (a long finger press does not); a mouse drag pans, in whole steps
 *  S3. the step keys (beside the VFO's meter): a tap is one step, taken at its click (nothing when the finger lands, so a touch
 *      that turns into a scroll tunes nothing); + up, - down; Enter / Space (a click with no pointer)
 *      too; a second finger and the right mouse button do nothing
 *  S4. a held key: nothing for 400 ms, then a step every 100 ms until the pointer is lifted; the click
 *      that ends the hold is not one more step; it stops when the touch is cancelled, slides off the key
 *      (a wobble doesn't), the key loses the focus or the page is hidden, and after 30 s at most
 *  S5. who and when: a viewer's keys do nothing; nothing while panels are being arranged, and a hold
 *      stops when arranging begins
 *  S6. the step: both pickers (Controls, the VFO's) list the radio's steps and show the same one;
 *      changing either sets both and the keys' step; it is kept per radio on this device and comes back
 *      after a reload; a kept value that isn't one of the radio's steps gives its default; another radio
 *      has its own
 */
"use strict";
const { makeEnv } = require("./fakedom");

let failures = 0, checks = 0;
function check(cond, msg) { checks++; if (!cond) { failures++; console.log("FAIL " + msg); } }
function eq(a, b, msg) { const ja = JSON.stringify(a), jb = JSON.stringify(b); check(ja === jb, msg + ": got " + ja + ", want " + jb); }

const PROFILES = [
  { id: "rig_a", make: "Test", name: "Rig A", has_scope: true, default_step: 100,
    bands: [{ name: "20m", lo: 14000000, hi: 14350000 }], modes: ["USB", "CW"],
    steps: [{ v: 10, label: "10 Hz" }, { v: 100, label: "100 Hz" }, { v: 1000, label: "1 kHz" }] },
  { id: "rig_b", make: "Test", name: "Rig B", has_scope: false, default_step: 1000,
    bands: [{ name: "2m", lo: 144000000, hi: 148000000 }], modes: ["USB", "FM"],
    steps: [{ v: 1000, label: "1 kHz" }, { v: 25000, label: "25 kHz" }] },
];
const jsonResponse = (status, body) => ({ ok: status >= 200 && status < 300, status, json: async () => body });
// the scope's view in these tests: 14.000 to 14.100 MHz over 1000 drawing px, the page's px the same
const LO = 14000000, HI = 14100000, W = 1000;

// the page, booted: tr.js + app.js run, the socket says hello, /api/radios answers, the radio's state arrives.
// opts.role "viewer"; opts.storage: this device's localStorage; opts.radio: the connected radio
async function page(opts) {
  opts = opts || {};
  const sockets = [];
  function FakeWS() { this.readyState = 1; this.sent = []; this.send = (d) => this.sent.push(JSON.parse(d)); this.close = () => {}; sockets.push(this); }
  const fetch = (url) => Promise.resolve(String(url).split("?")[0] === "/api/radios"
    ? jsonResponse(200, { radios: PROFILES }) : jsonResponse(404, { ok: false, error: "not_found" }));
  const e = makeEnv({ WebSocket: FakeWS, fetch, localStorage: opts.storage || {} });
  e.load("waterfall.js");
  e.ctx.Scope = function () {                               // no canvas here: the view above, everything else a no-op
    return new Proxy({ meta: {}, W, visibleRange: () => [LO, HI], freqAtX: (px) => LO + (px / W) * (HI - LO) },
                     { get: (t, k) => (k in t ? t[k] : () => []) });
  };
  for (const id of ["bandRow", "modeRow", "toasts"]) e.el("div", { id });
  e.el("select", { id: "radioSel" });
  e.wrap = e.el("div", { id: "scopeWrap" });
  const ov = e.el("canvas", { id: "overlay" }, e.wrap);
  ov.getBoundingClientRect = () => ({ left: 0, top: 0, width: W, height: 300 });
  e.stepSel = e.el("select", { id: "step" });               // the Controls panel's STEP
  const keys = e.el("div", { id: "vfoStepCtl", class: "meter-step", "data-gate": "operate" });   // in the VFO's meter
  e.dn = e.el("button", { id: "stepDn", type: "button", class: "m-btn step-key" }, keys);
  e.vfoSel = e.el("select", { id: "vfoStep", class: "step-sel" }, keys);
  e.up = e.el("button", { id: "stepUp", type: "button", class: "m-btn step-key" }, keys);
  e.load("tr.js");
  e.load("app.js");
  const ws = sockets[sockets.length - 1];
  e.frame = (f) => ws.onmessage({ data: JSON.stringify(f) });
  e.frame({ type: "hello", v: 1, conn: 3, user: "ME", role: opts.role || "operator",
            perms: opts.role === "viewer" ? ["view", "listen"] : ["view", "listen", "operate", "tx"],
            status: {}, tx: { owner: null }, hb_interval: 2 });
  for (let i = 0; i < 8; i++) await e.flush();
  e.radio = (id) => e.frame({ type: "state", connected: true, radio: id, freq: 14050000, mode_name: "USB", filter: 1,
                              main: { freq: 14050000 }, sub: { freq: 0 }, active_band: "main" });
  e.radio(opts.radio || "rig_a");
  for (let i = 0; i < 4; i++) await e.flush();
  ws.sent.length = 0;
  e.sent = ws.sent;
  e.settle = (ms) => e.clock.advance(ms === undefined ? 200 : ms);   // a frame and the 120 ms backup: queued tunes go out
  e.tunes = () => e.sent.filter((m) => m.action === "tune").map((m) => m.delta);
  e.total = () => e.tunes().reduce((a, b) => a + b, 0);
  e.freqs = () => e.sent.filter((m) => m.action === "set_freq").map((m) => m.hz);
  e.actions = () => [...new Set(e.sent.map((m) => m.action))].sort();
  e.clear = () => { e.sent.length = 0; };
  // a pointer on an element: ev(el, "pointerdown", { pointerType: "touch", clientX: 250 })
  e.ev = (el, type, o) => { const ev = new e.Event(type, Object.assign({ pointerId: 1, button: 0, isPrimary: true, pointerType: "mouse", clientX: 0, clientY: 0 }, o || {})); el.dispatchEvent(ev); return ev; };
  e.click = (el) => el.dispatchEvent(new e.Event("click", { bubbles: true }));
  // a tap as the browser delivers it: down, up, then the click
  e.tap = (el, o) => { e.ev(el, "pointerdown", o); e.clock.advance(60); e.ev(el, "pointerup", o); e.click(el); };
  e.pick = (sel, v) => { sel.value = String(v); sel.dispatchEvent(new e.Event("change", { bubbles: true })); };
  e.opts = (sel) => sel.children.map((o) => o.textContent);
  return e;
}

(async function main() {
  // ---- S1: nothing that scrolls tunes on the scope ----
  {
    const e = await page();
    e.ev(e.wrap, "pointerenter"); e.clock.advance(1000);     // the pointer rests on the scope (the old wheel rule)
    const wheels = [];
    for (let i = 0; i < 5; i++) wheels.push(e.ev(e.wrap, "wheel", { deltaY: i % 2 ? 100 : -100, clientX: 500 }));
    e.settle(500);
    eq([e.sent, wheels.some((w) => w.defaultPrevented)], [[], false], "S1 the wheel over the scope: nothing sent, the event left to the page");
    for (const type of ["touch", "pen"]) {
      const o = { pointerType: type };
      e.ev(e.wrap, "pointerdown", Object.assign({ clientX: 100 }, o));
      for (const x of [130, 180, 260, 400]) { e.clock.advance(16); e.ev(e.wrap, "pointermove", Object.assign({ clientX: x }, o)); }
      e.ev(e.wrap, "pointerup", Object.assign({ clientX: 400 }, o));
      e.settle(500);
      eq(e.sent, [], "S1 a " + type + " moving across the scope: nothing sent");
    }
    // a swipe the browser takes for a page scroll: it cancels the pointer
    e.ev(e.wrap, "pointerdown", { pointerType: "touch", clientX: 300 });
    e.ev(e.wrap, "pointercancel", { pointerType: "touch", clientX: 300 });
    e.settle(500);
    eq(e.sent, [], "S1 a finger whose swipe scrolls the page (the pointer cancelled): nothing sent");
  }
  // ---- S2: what still tunes on the scope ----
  {
    const e = await page();
    e.ev(e.wrap, "pointerdown", { pointerType: "touch", clientX: 250 }); e.clock.advance(80);
    e.ev(e.wrap, "pointerup", { pointerType: "touch", clientX: 250 });
    eq(e.freqs(), [14025000], "S2 a finger tap: the frequency under it");
    e.clear();
    e.ev(e.wrap, "pointerdown", { pointerType: "touch", clientX: 250 }); e.clock.advance(700);
    e.ev(e.wrap, "pointerup", { pointerType: "touch", clientX: 250 });
    eq(e.sent, [], "S2 a long finger press is not a tap");
    e.ev(e.wrap, "pointerdown", { pointerType: "pen", clientX: 750 }); e.clock.advance(80);
    e.ev(e.wrap, "pointerup", { pointerType: "pen", clientX: 750 });
    eq(e.freqs(), [14075000], "S2 a pen tap: the frequency under it");
    e.clear();
    e.ev(e.wrap, "pointerdown", { clientX: 600 }); e.ev(e.wrap, "pointerup", { clientX: 600 });
    eq(e.freqs(), [14060000], "S2 a mouse click: the frequency under it");
    e.clear();
    e.ev(e.wrap, "pointerdown", { clientX: 500 });
    e.ev(e.wrap, "pointermove", { clientX: 400 });           // 100 px left of 1000 over 100 kHz: up 10 kHz
    e.ev(e.wrap, "pointerup", { clientX: 400 });
    e.settle();
    eq([e.total(), e.freqs(), e.tunes().every((d) => d % 100 === 0)], [10000, [], true], "S2 a mouse drag pans, in whole steps, and is not a click");
    eq(e.actions(), ["tune"], "S2 ... sending the frequency's tune only");
  }
  {
    const e = await page({ role: "viewer" });
    e.ev(e.wrap, "pointerdown", { pointerType: "touch", clientX: 250 }); e.clock.advance(80);
    e.ev(e.wrap, "pointerup", { pointerType: "touch", clientX: 250 });
    e.settle();
    eq(e.sent, [], "S2 a viewer's tap on the scope: nothing");
  }
  // ---- S3: the step keys, a tap ----
  {
    const e = await page();
    e.ev(e.up, "pointerdown", { pointerType: "touch" });
    e.settle(300);
    eq(e.sent, [], "S3 a finger landing on +: nothing yet");
    e.ev(e.up, "pointercancel", { pointerType: "touch" });   // the touch became a scroll
    e.settle(3000);
    eq(e.sent, [], "S3 ... and a touch that turned into a scroll: nothing at all");
    e.tap(e.up, { pointerType: "touch" });
    eq(e.tunes(), [100], "S3 a tap on +: one step up, at once");
    e.settle(3000);
    eq(e.tunes(), [100], "S3 ... and only one");
    e.tap(e.dn, { pointerType: "touch" }); e.tap(e.dn);
    eq(e.tunes(), [100, -100, -100], "S3 a tap and a mouse click on -: one step down each");
    e.clear();
    e.click(e.up);                                           // Enter / Space on the focused key
    eq(e.tunes(), [100], "S3 Enter or Space on +: one step");
    e.clear();
    e.ev(e.up, "pointerdown", { pointerType: "touch", isPrimary: false, pointerId: 2 });   // a second finger
    e.ev(e.up, "pointerdown", { button: 2 });                // the right mouse button
    e.settle(3000);
    eq(e.sent, [], "S3 a second finger, the right mouse button: nothing, however long");
    eq(e.actions(), [], "S3 (nothing else was sent)");
  }
  // ---- S4: a held key ----
  {
    const e = await page();
    e.ev(e.up, "pointerdown", { pointerType: "touch" });
    e.clock.advance(399);
    eq(e.sent, [], "S4 + held 399 ms: nothing yet");
    e.clock.advance(1); e.settle(0); e.clock.advance(20);
    eq(e.total(), 100, "S4 at 400 ms: the first step");
    e.clock.advance(980);                                    // 1400 ms in all: steps at 400, 500 ... 1400
    e.ev(e.up, "pointerup", { pointerType: "touch" });
    e.click(e.up);                                           // the tap's click, right after the pointerup
    e.settle();
    eq(e.total(), 1100, "S4 held 1.4 s: eleven steps, and the click that ends the hold is not one more");
    e.settle(5000);
    eq(e.total(), 1100, "S4 lifted: it stops");
    e.clock.advance(600);
    e.tap(e.up, { pointerType: "touch" });
    eq(e.total(), 1200, "S4 a tap afterwards is a step again");
    eq(e.actions(), ["tune"], "S4 a held key sends the frequency's tune and nothing else");
    // the other key, down
    e.clear();
    e.ev(e.dn, "pointerdown"); e.clock.advance(700); e.ev(e.dn, "pointerup"); e.click(e.dn); e.settle();
    eq(e.total(), -400, "S4 - held 0.7 s with the mouse: four steps down");
  }
  for (const [how, end, more] of [
    ["the touch cancelled", (e) => e.ev(e.up, "pointercancel", { pointerType: "touch" }), 0],
    ["the pointer gone from the key (a mouse)", (e) => e.ev(e.up, "pointerleave"), 0],
    ["the finger slid off the key", (e) => e.ev(e.up, "pointermove", { pointerType: "touch", clientX: 40, clientY: 5 }), 0],
    ["the key losing the focus", (e) => e.up.dispatchEvent(new e.Event("blur", { bubbles: false })), 0],
    ["the page hidden", (e) => { e.doc.hidden = true; e.doc.dispatchEvent(new e.Event("visibilitychange")); }, 0],
    ["a wobble of the finger (it goes on)", (e) => e.ev(e.up, "pointermove", { pointerType: "touch", clientX: 10, clientY: -8 }), 1200],
  ]) {
    const e = await page();
    e.ev(e.up, "pointerdown", { pointerType: "touch" });
    e.clock.advance(650);                                    // steps at 400, 500, 600
    end(e);
    e.settle();
    const at = e.total();
    e.clock.advance(1000); e.settle();                       // 1.2 s more
    eq([at >= 300, e.total() - at], [true, more], "S4 a hold and then " + how + ": " + (more ? "a step every 100 ms for the next 1.2 s" : "it stops"));
  }
  {
    const e = await page();
    e.ev(e.up, "pointerdown", { pointerType: "touch" });
    e.clock.advance(31000); e.settle();
    const n = e.total() / 100;
    e.clock.advance(20000); e.settle();
    check(n >= 295 && n <= 302 && e.total() / 100 === n, "S4 a key held for ever (its pointerup lost): 30 s of steps, then none: " + n + ", " + e.total() / 100);
  }
  // ---- S5: who and when ----
  {
    const e = await page({ role: "viewer" });
    e.tap(e.up, { pointerType: "touch" }); e.click(e.dn);
    e.ev(e.up, "pointerdown", { pointerType: "touch" }); e.clock.advance(3000); e.settle();
    eq(e.sent, [], "S5 a viewer: the keys do nothing, tapped or held");
  }
  {
    const e = await page();
    let arranging = true;
    e.ctx.RadioLayout.arranging = () => arranging;
    e.tap(e.up, { pointerType: "touch" });
    e.ev(e.dn, "pointerdown"); e.clock.advance(2000); e.ev(e.dn, "pointerup"); e.settle();
    eq(e.sent, [], "S5 while panels are being arranged: nothing, tapped or held");
    arranging = false;
    e.ev(e.up, "pointerdown", { pointerType: "touch" });
    e.clock.advance(650); e.settle(0);
    arranging = true;                                        // Arrange begins under a held key
    e.clock.advance(3000); e.settle();
    eq(e.total(), 300, "S5 a hold stops when arranging begins");
  }
  // ---- S6: the step ----
  {
    const store = {};
    const e = await page({ storage: store });
    eq([e.opts(e.stepSel), e.opts(e.vfoSel)], [["10 Hz", "100 Hz", "1 kHz"], ["10 Hz", "100 Hz", "1 kHz"]], "S6 both pickers list the radio's steps");
    eq([e.stepSel.value, e.vfoSel.value], ["100", "100"], "S6 ... and show its default");
    e.pick(e.vfoSel, 1000);
    eq([e.stepSel.value, e.vfoSel.value, e.ctx.localStorage.getItem("telerig.step.rig_a")], ["1000", "1000", "1000"],
       "S6 the VFO's picker sets both, and the step is kept for this radio");
    e.tap(e.up, { pointerType: "touch" });
    eq(e.tunes(), [1000], "S6 the keys step by it");
    e.pick(e.stepSel, 10);
    e.tap(e.dn);
    eq([e.vfoSel.value, e.tunes()], ["10", [1000, -10]], "S6 the Controls picker sets both too");
    // the server switches to another radio: its own steps and default; then its own kept step
    e.radio("rig_b");
    for (let i = 0; i < 4; i++) await e.flush();
    eq([e.opts(e.vfoSel), e.stepSel.value, e.vfoSel.value], [["1 kHz", "25 kHz"], "1000", "1000"], "S6 another radio: its steps, its default");
    e.pick(e.vfoSel, 25000);
    eq([e.ctx.localStorage.getItem("telerig.step.rig_b"), e.ctx.localStorage.getItem("telerig.step.rig_a")], ["25000", "10"], "S6 ... kept apart from the first radio's");
    // a reload on this device
    const store2 = { "telerig.step.rig_a": "10", "telerig.step.rig_b": "25000" };
    const f = await page({ storage: store2 });
    f.tap(f.up, { pointerType: "touch" });
    eq([f.stepSel.value, f.vfoSel.value, f.tunes()], ["10", "10", [10]], "S6 after a reload: the kept step, in both pickers and under the keys");
    const g = await page({ storage: { "telerig.step.rig_a": "12345" } });
    eq([g.stepSel.value, g.vfoSel.value], ["100", "100"], "S6 a kept value that isn't one of the radio's steps: its default");
    const h = await page({ storage: { "telerig.step.rig_a": "<b>" } });
    eq(h.vfoSel.value, "100", "S6 a kept value that isn't a number: its default");
  }
  console.log(failures ? `${failures} of ${checks} checks FAILED` : `ALL PASS (${checks} checks)`);
  process.exit(failures ? 1 : 0);
})().catch((err) => { console.log("FAIL (crash) " + (err && err.stack || err)); process.exit(1); });
