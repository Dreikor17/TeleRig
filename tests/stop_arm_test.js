/* B4: the two-tap STOP of SOMEONE ELSE's transmission is bound to the owner it was armed for (D-16, F18,
 * I23). C1: a tap on the CW STOP control can never START a transmission (S9-S16: the cw_tx:false frame
 * landing between the taps of a double-tap, for our own CW, nobody's and someone else's; a message that
 * ends just before a single STOP tap; a randomized property run).
 * Runs the REAL frontend/tr.js (TR.stopArm) and the REAL frontend/cwtool.js click handler in a Node
 * vm with a tiny fake DOM, a fake clock and a recording RadioControl.send stub. No browser, no server, no
 * socket: nothing here can reach a radio. Run by tests/t_admin_ui.py (unit group):
 *
 *     node tests/stop_arm_test.js        -> exit 0 and "ALL PASS", or exit 1 naming each failure
 */
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const FRONT = path.join(__dirname, "..", "frontend");
let failures = 0, checks = 0;
function check(cond, msg) {
  checks++;
  if (!cond) { failures++; console.log("FAIL " + msg); }
}
function eq(a, b, msg) {
  const ja = JSON.stringify(a), jb = JSON.stringify(b);
  check(ja === jb, msg + ": got " + ja + ", want " + jb);
}

// ---- fake clock -----------------------------------------------------------------------------------
function makeClock() {
  const c = { now: 1_000_000, seq: 0, tasks: [] };
  c.setTimeout = (fn, ms) => { const id = ++c.seq; c.tasks.push({ id, at: c.now + (+ms || 0), fn, every: 0 }); return id; };
  c.setInterval = (fn, ms) => { const id = ++c.seq; c.tasks.push({ id, at: c.now + (+ms || 0), fn, every: +ms || 1 }); return id; };
  c.clear = (id) => { c.tasks = c.tasks.filter((t) => t.id !== id); };
  c.advance = (ms) => {
    const end = c.now + ms;
    for (;;) {
      const due = c.tasks.filter((t) => t.at <= end).sort((a, b) => a.at - b.at || a.id - b.id)[0];
      if (!due) break;
      c.now = due.at;
      if (due.every) due.at += due.every; else c.clear(due.id);
      due.fn();
    }
    c.now = end;
  };
  return c;
}

// ---- fake DOM ---------------------------------------------------------------------------------------
class FakeEl {
  constructor(id) {
    this.id = id; this.hidden = false; this.disabled = false; this.textContent = ""; this.title = "";
    this.value = ""; this.innerHTML = ""; this.scrollTop = 0; this.scrollHeight = 0;
    this.dataset = {}; this.style = {}; this.attrs = {}; this.listeners = {};
    const cls = new Set();
    this.classList = {
      add: (c) => cls.add(c), remove: (c) => cls.delete(c), contains: (c) => cls.has(c),
      toggle: (c, on) => { const v = on === undefined ? !cls.has(c) : !!on; if (v) cls.add(c); else cls.delete(c); return v; },
    };
  }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  removeEventListener() {}
  fire(t, ev) { for (const fn of this.listeners[t] || []) fn(Object.assign({ preventDefault() {}, stopPropagation() {} }, ev || {})); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  closest() { return this; }                       // cwTool "is in a tile": the floating-panel code stays off
  appendChild(x) { return x; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
}

function makeEnv() {
  const clock = makeClock();
  const els = {};
  const docListeners = {};
  const document = {
    getElementById: (id) => (id === "tr-boot" ? null : (els[id] = els[id] || new FakeEl(id))),
    addEventListener: (t, fn) => { (docListeners[t] = docListeners[t] || []).push(fn); },
    removeEventListener() {},
    createElement: (t) => new FakeEl(t),
    createElementNS: (_, t) => new FakeEl(t),
    documentElement: new FakeEl("html"),
    body: null,
  };
  const store = {};
  const RealDate = Date;
  function FakeDate(...a) { return a.length ? new RealDate(...a) : new RealDate(clock.now); }
  FakeDate.now = () => clock.now;
  FakeDate.prototype = RealDate.prototype;
  const sent = [];
  const ctx = {
    console, JSON, Math, Set, Map, Array, Object, String, Number, Boolean, Promise, Error, RegExp, Float32Array,
    Date: FakeDate,
    setTimeout: clock.setTimeout, clearTimeout: clock.clear, setInterval: clock.setInterval, clearInterval: clock.clear,
    requestAnimationFrame: (fn) => clock.setTimeout(fn, 16),
    performance: { now: () => clock.now },
    localStorage: { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: (k) => { delete store[k]; } },
    sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    location: { pathname: "/", search: "", replace() { throw new Error("navigation in a test"); } },
    CustomEvent: function (t, o) { this.type = t; this.detail = o && o.detail; },
    document,
  };
  ctx.window = ctx;
  ctx.addEventListener = () => {};
  vm.createContext(ctx);
  const env = {
    ctx, clock, els, sent,
    fireDoc(t, detail) { for (const fn of docListeners[t] || []) fn({ type: t, detail }); },
    load(f) { vm.runInContext(fs.readFileSync(path.join(FRONT, f), "utf8"), ctx, { filename: f }); },
  };
  return env;
}

// ---- 1. TR.stopArm (tr.js) ---------------------------------------------------------------------------
{
  const env = makeEnv();
  env.load("tr.js");
  const TR = env.ctx.TR;
  check(typeof TR.stopArm === "function" && typeof TR.ownerKey === "function", "tr.js exports stopArm/ownerKey");
  const OP1 = { conn: 5, user: "OP1", claims: ["cw"] }, OP9 = { conn: 9, user: "OP9", claims: ["ptt"] };
  const OP1b = { conn: 6, user: "OP1", claims: ["cw"] };        // OP1 from ANOTHER tab
  const FP = { conn: -1, external: true, claims: ["ptt"] };
  const changes = [];
  const arm = TR.stopArm((o) => changes.push(o ? TR.ownerKey(o) : null));
  eq([arm.tap(OP1), arm.tap(OP1)], ["arm", "fire"], "same owner, two taps -> fire");
  eq([arm.tap(OP1), arm.tap(OP9)], ["arm", "none"], "owner swapped between the taps (no frame) -> nothing");
  eq(arm.armed(), false, "... and disarmed");
  eq([arm.tap(OP1), arm.tap(OP1b)], ["arm", "none"], "the same user from another connection is another owner");
  arm.tap(OP1); arm.check(OP9);
  eq([arm.armed(), arm.tap(OP9)], [false, "arm"], "check() on an owner change disarms; the next tap arms for the new owner");
  arm.check(OP9); eq(arm.armed(), true, "check() with the same owner keeps the arm");
  arm.check(null); eq(arm.armed(), false, "check(null): the owner released -> disarmed");
  arm.tap(FP); eq(arm.tap(OP1), "none", "armed for FRONT PANEL, then OP1 owns -> nothing");
  arm.tap(OP1); env.clock.advance(3001);
  eq([arm.armed(), arm.tap(OP1)], [false, "arm"], "the arm lapses after 3 s; a later tap only re-arms");
  arm.disarm();
  eq(arm.tap(null), "none", "no owner -> nothing");
  eq(changes.slice(0, 3), ["5|OP1", null, "5|OP1"], "onChange reports arm (owner) and disarm (null)");
}

// ---- 2. cwtool.js CW STOP (the real click handler) ----------------------------------------------------
function cwEnv() {
  const env = makeEnv();
  env.load("tr.js");
  const TR = env.ctx.TR;
  TR.myConn = 3;                                                   // this tab
  const st = { connected: true, has_cw_tx: true, cw_tx_ready: true, mode_name: "CW", cw_tx: false };
  env.st = st;
  env.ctx.RadioControl = { send: (o) => env.sent.push(JSON.parse(JSON.stringify(o))), state: () => st };
  env.ctx.RadioTx = { owner: () => (TR.tx && TR.tx.owner) || null, nextSeq: (() => { let s = 0; return () => ++s; })() };
  env.load("cwtool.js");
  // the time cwtool's 400 ms state poll last SAW a CW message on the air (the S16 property counts
  // from what the tab could see: a CW that came and went between two polls never showed a STOP)
  env.lastSeenOn = -1e9;
  for (const t of env.clock.tasks) {
    if (t.every !== 400) continue;
    const f = t.fn;
    t.fn = () => { if (st.cw_tx) env.lastSeenOn = env.clock.now; f(); };
  }
  env.tx = (owner, cw) => {                                        // a tx frame + the radio's state
    TR.tx = { owner };
    st.cw_tx = !!cw;
    env.fireDoc("tr:tx", TR.tx);
  };
  env.tap = () => env.els.cwTx.fire("click");
  env.hint = () => env.els.cwTxHint.textContent;
  env.actions = () => env.sent.map((f) => f.action);
  return env;
}
const OP1 = { conn: 5, user: "OP1", claims: ["cw"] }, OP9 = { conn: 9, user: "OP9", claims: ["cw"] };
{ // S1: owner switches between the taps -> the arm is dropped, no tx_stop
  const e = cwEnv();
  e.tx(OP1, true); e.tap();
  eq([e.actions(), e.hint()], [[], "Tap again to stop OP1"], "S1 first tap arms for OP1, sends nothing");
  e.tx(OP9, true);
  check(!/Tap again/.test(e.hint()), "S1 the owner switch disarms at once (hint: " + e.hint() + ")");
  e.tap();
  eq([e.actions(), e.hint()], [[], "Tap again to stop OP9"], "S1 the next tap only arms for OP9 (no tx_stop)");
}
{ // S2: the same owner, two taps 400 ms apart -> exactly one tx_stop
  const e = cwEnv();
  e.tx(OP1, true); e.tap(); e.clock.advance(400); e.tap();
  eq(e.actions(), ["tx_stop"], "S2 same owner: exactly one tx_stop");
}
{ // S3: OP1 releases (null frame) and the same connection keys CW again -> no tx_stop
  const e = cwEnv();
  e.tx(OP1, true); e.tap(); e.tx(null, false); e.tx(OP1, true); e.tap();
  eq(e.actions(), [], "S3 release + re-key by the same conn between the taps: nothing sent");
}
{ // S4: frame-less swap (TR.tx changed without an event) -> the click-time check sends nothing
  const e = cwEnv();
  e.tx(OP1, true); e.tap();
  e.ctx.TR.tx = { owner: OP9 };                                    // no tr:tx event, no state poll yet
  e.tap();
  eq(e.actions(), [], "S4 owner swapped without a frame: the second tap sends nothing");
}
{ // S5: the CW being stopped ends before the second tap -> that tap never starts a transmission
  const e = cwEnv();
  e.els.cwSend.value = "CQ CQ TEST";
  e.tx(OP1, true); e.tap(); e.clock.advance(500);
  e.tx(null, false);                                               // OP1's message ended, nobody on air
  e.tap();
  eq(e.actions(), [], "S5 the second tap after the CW ended sends nothing (no cw_tx!)");
  check(/nothing sent/.test(e.hint()), "S5 says nothing was sent (hint: " + e.hint() + ")");
  e.clock.advance(3100);
  e.tap();
  eq(e.actions(), ["cw_tx"], "S5 a deliberate tap after the arm window transmits as before");
}
{ // S6: the arm lapses after 3 s, then a tap only re-arms
  const e = cwEnv();
  e.tx(OP1, true); e.tap(); e.clock.advance(3001);
  check(!/Tap again/.test(e.hint()), "S6 the arm lapsed (hint: " + e.hint() + ")");
  e.tap();
  eq([e.actions(), e.hint()], [[], "Tap again to stop OP1"], "S6 a tap after the lapse re-arms, sends nothing");
}
{ // S7: the state poll alone (no tr:tx event) also disarms on an owner change
  const e = cwEnv();
  e.tx(OP1, true); e.tap();
  e.ctx.TR.tx = { owner: OP9 };
  e.clock.advance(450);                                            // syncTx runs every 400 ms
  check(!/Tap again/.test(e.hint()), "S7 the 400 ms state poll disarms (hint: " + e.hint() + ")");
  e.tap(); e.tap();
  eq(e.actions(), ["tx_stop"], "S7 two fresh taps on OP9's CW: one tx_stop for OP9");
}
{ // S8: our own CW (or nobody's) is one tap -> cw_stop
  const e = cwEnv();
  e.tx({ conn: 3, user: "ME", claims: ["cw"] }, true); e.tap();
  e.tx(null, true); e.tap();
  eq(e.actions(), ["cw_stop", "cw_stop"], "S8 own CW / no owner: one tap sends cw_stop");
}

// ---- C1: a tap on a STOP control can never START a transmission -----------------------------------
// The race: the first tap of a STOP double-tap stops the CW, the state frame (cw_tx:false) lands before
// the second tap, and the second tap used to fall through to the TX path and send the typed message
// again as a new cw_tx. Own CW, nobody's CW and someone else's CW, plus a message that ends by itself
// just before a single STOP tap.
const ME = { conn: 3, user: "ME", claims: ["cw"] };
{ // S9: OUR CW, the cw_tx:false frame arrives between the two taps
  const e = cwEnv();
  e.els.cwSend.value = "CQ CQ TEST";
  e.tx(ME, true); e.tap();
  e.tx(null, false);                                               // our cw_stop landed: nobody on air
  e.tap();
  eq(e.actions(), ["cw_stop"], "S9 own CW: the second tap after the stop frame sends nothing (no cw_tx)");
  check(/nothing sent/.test(e.hint()), "S9 says nothing was sent (hint: " + e.hint() + ")");
}
{ // S9b: OUR CW, only the radio state changes between the taps (no tx frame yet)
  const e = cwEnv();
  e.els.cwSend.value = "CQ";
  e.tx(ME, true); e.tap();
  e.st.cw_tx = false;                                              // state poll first, tx frame later
  e.tap(); e.clock.advance(300); e.tap();
  eq(e.actions(), ["cw_stop"], "S9b own CW, state-only change: later taps send nothing");
}
{ // S10: NOBODY's CW (owner null, radio still sending), the frame lands between the taps
  const e = cwEnv();
  e.els.cwSend.value = "CQ";
  e.tx(null, true); e.tap(); e.clock.advance(250);
  e.tx(null, false); e.tap();
  eq(e.actions(), ["cw_stop"], "S10 nobody's CW: the second tap sends nothing");
}
{ // S11: someone else's CW ends by itself just before a SINGLE tap meant as STOP
  const e = cwEnv();
  e.els.cwSend.value = "CQ";
  e.tx(OP1, true); e.clock.advance(450);                           // the button showed "Tap twice to stop OP1"
  e.tx(null, false); e.clock.advance(200); e.tap();
  eq(e.actions(), [], "S11 other's CW just ended: a tap meant as STOP sends nothing");
}
{ // S12: OUR CW ends by itself just before a single tap meant as STOP
  const e = cwEnv();
  e.els.cwSend.value = "CQ";
  e.tx(ME, true); e.clock.advance(1250);
  e.tx(null, false); e.clock.advance(150); e.tap();
  eq(e.actions(), [], "S12 own CW just ended: a tap meant as STOP sends nothing");
}
{ // S13: a burst of taps never transmits: every guarded tap extends the STOP role
  const e = cwEnv();
  e.els.cwSend.value = "CQ";
  e.tx(ME, true); e.tap(); e.tx(null, false);
  e.tap(); e.clock.advance(2500); e.tap(); e.clock.advance(2900); e.tap();
  eq(e.actions(), ["cw_stop"], "S13 a burst of taps after the stop: only the cw_stop");
  e.clock.advance(3100); e.tap();
  eq(e.actions(), ["cw_stop", "cw_tx"], "S13 a deliberate tap 3 s after the last tap transmits again");
}
{ // S14: the button shows the guard: disabled right after the CW ended, enabled once it lapses
  const e = cwEnv();
  e.tx(ME, true); e.clock.advance(400);
  eq([e.els.cwTx.textContent, e.els.cwTx.disabled], ["■", false], "S14 on air: an enabled STOP");
  e.tx(null, false); e.clock.advance(400);
  eq([e.els.cwTx.textContent, e.els.cwTx.disabled], ["TX", true], "S14 just ended: TX shown but inert");
  check(/just ended/.test(e.els.cwTx.title), "S14 the title says why (" + e.els.cwTx.title + ")");
  e.clock.advance(3200);
  eq(e.els.cwTx.disabled, false, "S14 enabled again after the guard window");
}
{ // S15: someone else's CW: arm, the CW ends, two more taps -> nothing; no cw_tx at any point
  const e = cwEnv();
  e.els.cwSend.value = "CQ";
  e.tx(OP1, true); e.tap(); e.tx(null, false); e.tap(); e.tap();
  eq(e.actions(), [], "S15 other's CW ended while armed: the later taps send nothing");
}
// S16: a randomized property check over tap / frame / clock sequences (seeded, reproducible). Every
// cw_tx this tab sends must come at least 3 s after its last STOP tap (a tap while CW was on the air)
// and at least 3 s after its state poll last showed a CW message on the air.
{
  let seed = 0x2545F491;
  const rnd = () => { seed ^= seed << 13; seed >>>= 0; seed ^= seed >>> 17; seed ^= seed << 5; seed >>>= 0; return seed / 4294967296; };
  const pick = (a) => a[Math.floor(rnd() * a.length)];
  let bad = 0, cwTxSent = 0, stopTaps = 0, firstBad = "";
  for (let run = 0; run < 1500; run++) {
    const e = cwEnv();
    e.els.cwSend.value = "CQ TEST";
    let lastStopTap = -1e9;
    const trace = [];
    for (let i = 0; i < 40; i++) {
      const op = pick(["tap", "tap", "tap", "own", "other", "nobody", "off", "offkeep", "wait", "wait"]);
      const before = e.sent.length;
      if (op === "tap") { if (e.st.cw_tx) { lastStopTap = e.clock.now; stopTaps++; } e.tap(); }
      else if (op === "own") e.tx(ME, true);
      else if (op === "other") e.tx(pick([OP1, OP9]), true);
      else if (op === "nobody") e.tx(null, true);
      else if (op === "off") e.tx(null, false);
      else if (op === "offkeep") e.st.cw_tx = false;                // radio state only, no tx frame
      else e.clock.advance(Math.floor(rnd() * 1500));
      trace.push(op + "@" + (e.clock.now - 1_000_000));
      for (const f of e.sent.slice(before)) {
        if (f.action !== "cw_tx") continue;
        cwTxSent++;
        const sinceSeen = e.clock.now - e.lastSeenOn, sinceStop = e.clock.now - lastStopTap;
        if (sinceSeen < 3000 || sinceStop < 3000) {
          bad++;
          if (!firstBad) firstBad = "run " + run + ": cw_tx " + sinceSeen + " ms after CW was seen on air, " + sinceStop +
                                    " ms after a STOP tap; trace " + trace.join(" ");
        }
      }
    }
  }
  check(bad === 0, "S16 a STOP tap or a just-seen CW never leads to a cw_tx (" + bad + " bad of " + cwTxSent + "): " + firstBad);
  check(cwTxSent > 50 && stopTaps > 1000, "S16 the run exercised both sides (" + cwTxSent + " deliberate cw_tx, " + stopTaps + " STOP taps)");
}

console.log(failures ? failures + " of " + checks + " checks FAILED" : "ALL PASS (" + checks + " checks)");
process.exit(failures ? 1 : 0);
