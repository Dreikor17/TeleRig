/* The PTT button's fail-safe rule and the hidden-tab release (final review round 3), tested on the
 * REAL frontend code (tr.js + app.js) in a Node vm on tests/fakedom.js. No browser, no server process,
 * no socket, no radio: the PTT button is a fake element of a FAKE page, and the "server" is a few lines
 * of in-memory JS applying the arbiter's claim rules for this one connection with a one-way latency
 * (every frame the page sends reaches it LAT ms later, every answer reaches the page LAT ms later).
 * "keyed" is a boolean in this file. Run by tests/t_c1.py (unit group):
 *
 *     node tests/ptt_failsafe_test.js   -> exit 0 and "ALL PASS", or exit 1 naming each failure
 *
 * The rule (app.js pttPress, I4, G-1 c), asymmetric and fail-safe:
 *  - while this tab owns ptt or has a ptt claim pending, EVERY press unkeys: a click of any e.detail,
 *    Enter, Space, a key auto-repeat, a pen tap, a touch;
 *  - only KEYING is ever held back: an idle click with e.detail > 1, an auto-repeated key, and any
 *    press inside the re-key guard (800 ms after this tab's own PTT ended, pushed out by every press
 *    it refuses) send nothing.
 * Round 2 suppressed every click with e.detail > 1, so "key with one click, then click ON AIR … STOP
 * again within the OS double-click time" left the radio keyed. Section A pins that case explicitly.
 *
 *  A. key (detail 1) then STOP (detail 2), at several latencies
 *  B. key then STOP with detail 2..N at 150 / 350 / 480 / 1200 ms
 *  C. STOP every 250-300 ms for 5 s (mouse, pen, keyboard): unkeyed after the first, never re-keyed
 *  D. STOP while the claim is still pending (60 ms one-way): no key remains
 *  E. an idle double-click keys once; a release + quick clicks never re-key; the guard then lets go
 *  F. keyboard (Enter, Space, a held key's auto-repeat), touch and pen paths
 *  G. randomized property: (a) a press while owned/pending always ends unkeyed, (b) no ptt:on within
 *     the guard window after this page's PTT ended, (c) every ptt:on comes from a deliberate idle press
 *  H. a VOX / CW / PTT claim still pending when the tab is hidden (or left) is released when its grant
 *     lands, and heartbeats stop; a grant while visible is kept
 *  I. no claim is ever SENT while the tab is hidden or after pagehide: a touch hold still timing at
 *     the hide never keys (not even after the tab is back); RadioControl claims and presses while away
 *     send nothing; randomized property over touches, holds, hides, pagehides and claims
 */
"use strict";
const { makeEnv } = require("./fakedom");

let failures = 0, checks = 0;
const pending = [];
function check(cond, msg) { checks++; if (!cond) { failures++; console.log("FAIL " + msg); } }
function eq(a, b, msg) { const ja = JSON.stringify(a), jb = JSON.stringify(b); check(ja === jb, msg + ": got " + ja + ", want " + jb); }
function test(name, fn) { pending.push([name, fn]); }

const GUARD_MS = 800;                              // app.js PTT_REKEY_GUARD_MS
const ON_AIR = /^ON AIR \d+s — STOP$/;

// ---- the fake page + fake server --------------------------------------------------------------------
function pageEnv(lat) {
  const sockets = [];
  function FakeWS() { this.readyState = 1; this.sent = []; this.send = (d) => this.sent.push(JSON.parse(d)); this.close = () => {}; sockets.push(this); }
  const e = makeEnv({ WebSocket: FakeWS });
  const winL = {};                                 // window listeners (fakedom's window ignores them)
  e.ctx.addEventListener = (t, fn) => { (winL[t] = winL[t] || []).push(fn); };
  e.ctx.removeEventListener = (t, fn) => { const l = winL[t]; if (l && l.indexOf(fn) >= 0) l.splice(l.indexOf(fn), 1); };
  e.win = (type, init) => { const ev = new e.Event(type, init); for (const fn of (winL[type] || []).slice()) fn(ev); return ev; };
  e.ctx.Scope = function () { return new Proxy({ meta: {} }, { get: (t, k) => (k in t ? t[k] : () => []) }); };
  for (const id of ["onAir", "onAirStop", "onAirStopLbl", "onAirWho", "onAirTime", "txStopFab", "toasts"]) e.el("div", { id });
  e.btn = e.el("button", { id: "pttBtn" });
  e.load("tr.js");
  e.load("app.js");
  const ws = sockets[sockets.length - 1];
  e.sock = ws;
  e.frame = (f) => ws.onmessage({ data: JSON.stringify(f) });
  e.frame({ type: "hello", v: 1, conn: 3, user: "ME", role: "operator",
            perms: ["view", "listen", "menu_read", "operate", "tx", "connect", "menu"], status: {}, tx: { owner: null }, hb_interval: 2 });
  e.lat = lat;
  e.toasts = [];
  e.ctx.TR.toast = (msg, kind) => { e.toasts.push([String(msg), kind]); };

  // the server: the arbiter's claim rules for conn 3 (owner auto release -> release; non-owner auto -> ignore)
  const S = { claims: new Set(), keyed: false, keys: 0, keyTimes: [], unkeyTimes: [], hb: 0, hbTimes: [] };
  e.S = S;
  e.offSeen = [];                                  // times the page saw ITS ptt end (any frame)
  const owner = () => (S.claims.size ? { conn: 3, user: "ME", claims: [...S.claims], elapsed_ms: 0, external: false } : null);
  const deliver = (f) => e.clock.setTimeout(() => {
    const had = e.ctx.RadioTx.ownedPtt();
    e.frame(f);
    if (had && !e.ctx.RadioTx.ownedPtt()) e.offSeen.push(e.clock.now);
  }, e.lat);
  const claim = (k, seq) => {
    S.claims.add(k);
    if (k === "ptt" && !S.keyed) { S.keyed = true; S.keys++; S.keyTimes.push(e.clock.now); }
    deliver({ type: "tx", owner: owner(), req: { conn: 3, seq } });
  };
  const release = (k) => {
    if (!S.claims.has(k)) return;                  // not ours (an auto release of a lost / ended claim): ignored
    S.claims.delete(k);
    if (k === "ptt") { S.keyed = false; S.unkeyTimes.push(e.clock.now); }
    deliver({ type: "tx", owner: owner() });
  };
  function onServer(f) {
    if (f.action === "tx_hb") { S.hb++; S.hbTimes.push(e.clock.now); return; }
    if (f.action === "ptt") { if (f.tx === true) claim("ptt", f.seq); else release("ptt"); }
    else if (f.action === "rx_func" && f.name === "vox") { if (f.on === true) claim("vox", f.seq); else release("vox"); }
    else if (f.action === "cw_tx") claim("cw", f.seq);
    else if (f.action === "cw_stop") release("cw");
  }
  e.serverEnds = () => {                           // the session clock / another operator's STOP / a revoke
    if (!S.claims.size) return;
    if (S.keyed) { S.keyed = false; S.unkeyTimes.push(e.clock.now); }
    S.claims.clear();
    deliver({ type: "tx", owner: null });
  };
  e.log = [];                                      // [time, label, op] for every non-heartbeat frame the page sent
  e.curOp = "boot";
  const orig = ws.send;
  ws.send = (d) => {
    orig(d);
    const f = JSON.parse(d);
    if (f.action !== "tx_hb") {
      const lbl = f.action === "ptt" ? "ptt:" + (f.tx ? "on" : "off") + (f.auto ? "(auto)" : "")
        : f.action === "rx_func" ? "vox:" + (f.on ? "on" : "off") + (f.auto ? "(auto)" : "")
        : f.action + (f.auto ? "(auto)" : "");
      e.log.push([e.clock.now, lbl, e.curOp]);
    }
    e.clock.setTimeout(() => onServer(f), e.lat);
  };
  e.frames = () => e.log.map((x) => x[1]);
  e.ptt = () => e.log.filter((x) => x[1].startsWith("ptt:")).map((x) => x[1]);
  e.ons = () => e.log.filter((x) => x[1] === "ptt:on").length;
  e.offs = () => e.log.filter((x) => x[1].startsWith("ptt:off")).length;
  e.label = () => e.btn.textContent;
  e.adv = (ms) => e.clock.advance(ms);

  // ---- input, as a browser delivers it ----
  const fire = (target, type, init) => target.dispatchEvent(new e.Event(type, init));
  const pointerClick = (pointerType, pointerId, detail) => {
    fire(e.btn, "pointerdown", { pointerType, pointerId, button: 0 });
    e.win("pointerup", { pointerType, pointerId });
    fire(e.btn, "pointerup", { pointerType, pointerId });
    fire(e.btn, "click", { detail, pointerType });
  };
  e.mouse = (detail) => { e.curOp = "mouse" + detail; pointerClick("mouse", 1, detail === undefined ? 1 : detail); };
  e.pen = (detail) => { e.curOp = "pen" + detail; pointerClick("pen", 2, detail === undefined ? 1 : detail); };
  e.key = (key, repeat) => {                       // Enter acts on keydown, Space on keyup; both click with detail 0
    e.curOp = "key" + (repeat ? "-repeat" : "");
    const ok = fire(e.btn, "keydown", { key, repeat: !!repeat });
    if (key === "Enter" && ok) fire(e.btn, "click", { detail: 0, pointerType: "" });
    if (key === " " && !repeat) { fire(e.btn, "keyup", { key }); if (ok) fire(e.btn, "click", { detail: 0, pointerType: "" }); }
    return ok;
  };
  e.touch = (holdMs, compat) => {                  // finger down, hold, lift; then the browser's compat click
    e.curOp = "touch" + holdMs;
    fire(e.btn, "pointerdown", { pointerType: "touch", pointerId: 7 });
    e.clock.advance(holdMs);
    e.win("pointerup", { pointerType: "touch", pointerId: 7 });
    fire(e.btn, "pointerup", { pointerType: "touch", pointerId: 7 });
    if (compat !== false) fire(e.btn, "click", { detail: 1, pointerType: "touch" });
    e.curOp = "after-touch";
  };
  e.hide = () => { e.doc.hidden = true; e.doc.dispatchEvent(new e.Event("visibilitychange")); };
  e.show = () => { e.doc.hidden = false; e.gone = false; e.doc.dispatchEvent(new e.Event("visibilitychange")); };
  // section I: a finger that stays down across a hide, and "is this page away right now?" as a
  // browser would say (document.hidden, or between pagehide and pageshow)
  e.gone = false;
  e.pagehide = () => { e.gone = true; e.win("pagehide", {}); };
  e.pageshow = () => { e.gone = false; e.win("pageshow", {}); };
  e.awayNow = () => !!e.doc.hidden || e.gone;
  e.touchDown = () => { e.curOp = "touch-down"; fire(e.btn, "pointerdown", { pointerType: "touch", pointerId: 7 }); };
  e.touchUp = (compat) => {
    e.curOp = "touch-up";
    e.win("pointerup", { pointerType: "touch", pointerId: 7 });
    fire(e.btn, "pointerup", { pointerType: "touch", pointerId: 7 });
    if (compat !== false) fire(e.btn, "click", { detail: 1, pointerType: "touch" });
    e.curOp = "after-touch";
  };
  e.claimsSentAway = [];                          // [time, label] of every claim frame sent while away
  const CLAIM_LBL = /^(ptt:on|vox:on|cw_tx|tune_atu)$/;
  const orig2 = ws.send;
  ws.send = (d) => {
    const f = JSON.parse(d);
    const lbl = f.action === "ptt" ? "ptt:" + (f.tx ? "on" : "off") : f.action === "rx_func" ? "vox:" + (f.on ? "on" : "off") : f.action;
    if (CLAIM_LBL.test(lbl) && e.awayNow()) e.claimsSentAway.push([e.clock.now, lbl, e.curOp]);
    orig2(d);
  };
  return e;
}

// ---- A. key (detail 1) then STOP (detail 2): the case round 2 got wrong -----------------------------
for (const lat of [5, 25, 60, 100]) {
  for (const gap of [200, 350]) {
    test(`A key (detail 1) then STOP (detail 2) ${gap} ms later, one-way ${lat} ms`, async () => {
      const e = pageEnv(lat);
      e.mouse(1);
      check(ON_AIR.test(e.label()), "A the button reads ON AIR … — STOP after the key click (" + e.label() + ")");
      e.adv(gap);
      e.mouse(2);                                  // the browser reports detail 2: inside the double-click time
      eq(e.offs(), 1, `A [${lat}/${gap}] the STOP click (detail 2) sent ptt:false at once`);
      e.adv(2 * lat + 1);
      eq(e.S.keyed, false, `A [${lat}/${gap}] unkeyed one round trip after the STOP click`);
      e.adv(3000);
      eq([e.ons(), e.S.keyed, e.S.keys, e.label()], [1, false, 1, "PTT"], `A [${lat}/${gap}] keyed once, ends unkeyed`);
    });
  }
}

// ---- B. key then STOP with detail 2..N at 150 / 350 / 480 / 1200 ms ---------------------------------
for (const gap of [150, 350, 480, 1200]) {
  for (const n of [2, 3, 5]) {
    for (const lat of [10, 60]) {
      test(`B key, then STOP clicks detail 2..${n} every ${gap} ms (one-way ${lat} ms)`, async () => {
        const e = pageEnv(lat);
        e.mouse(1);
        e.adv(gap);
        e.mouse(2);
        const tStop = e.clock.now;
        for (let d = 3; d <= n; d++) { e.adv(gap); e.mouse(d); }
        e.adv(3000);
        eq([e.ons(), e.offs(), e.S.keyed, e.S.keys, e.label()], [1, 1, false, 1, "PTT"],
           `B [${gap} ms, detail 2..${n}, ${lat} ms] one key, one unkey, ends unkeyed`);
        check(e.S.unkeyTimes.length === 1 && e.S.unkeyTimes[0] <= tStop + lat + 1,
              `B [${gap}/${n}/${lat}] the radio unkeyed one hop after the first STOP click: ${JSON.stringify(e.S.unkeyTimes)} vs ${tStop}`);
      });
    }
  }
}

// ---- C. STOP every 250-300 ms for 5 s ----------------------------------------------------------------
for (const how of ["mouse", "pen", "keyboard"]) {
  test(`C key, then STOP every 250-300 ms for 5 s (${how}): unkeyed after the first, never re-keyed`, async () => {
    const e = pageEnv(25);
    const press = (d) => (how === "mouse" ? e.mouse(d) : how === "pen" ? e.pen(d) : e.key("Enter"));
    press(1);
    e.adv(260);
    press(2);                                      // the first STOP
    const tFirst = e.clock.now;
    let d = 3, keyedAfter = 0;
    while (e.clock.now - tFirst < 5000) {
      e.adv(d % 2 ? 250 : 300);
      if (e.clock.now - tFirst > 2 * e.lat && e.S.keyed) keyedAfter++;
      press(d++);                                  // the browser keeps counting: detail 3, 4, 5 ...
    }
    e.adv(3000);
    eq([e.ons(), e.offs(), e.S.keyed, e.S.keys], [1, 1, false, 1], `C ${how}: one key, one unkey (${d - 2} STOP presses)`);
    eq(keyedAfter, 0, `C ${how}: never keyed again after the first STOP landed`);
    eq(e.label(), "PTT", `C ${how}: the button reads PTT`);
  });
}

// ---- D. STOP while the claim is still pending (60 ms one-way) -----------------------------------------
for (const [at, label] of [[20, "before the server has the claim"], [90, "the server keyed, the grant is still on its way"]]) {
  for (const how of ["mouse detail 2", "mouse detail 1", "keyboard", "pen detail 2", "touch"]) {
    test(`D STOP ${at} ms after the key click (${label}), ${how}: no key remains`, async () => {
      const e = pageEnv(60);
      e.mouse(1);
      e.adv(at);
      eq(e.label().startsWith("ON AIR"), true, "D the pending claim reads ON AIR … — STOP");
      if (how === "mouse detail 2") e.mouse(2);
      else if (how === "mouse detail 1") e.mouse(1);
      else if (how === "keyboard") e.key("Enter");
      else if (how === "pen detail 2") e.pen(2);
      else e.touch(60);
      eq(e.ptt(), ["ptt:on", "ptt:off(auto)"], `D [${at}, ${how}] the pending claim is released (auto) at once`);
      e.adv(3000);
      eq([e.S.keyed, [...e.S.claims], e.ctx.RadioTx.ownedOrPending(), e.label()], [false, [], false, "PTT"],
         `D [${at}, ${how}] nothing keyed, nothing held, the page agrees`);
      check(e.S.keys <= 1, `D [${at}, ${how}] keyed at most once (${e.S.keys})`);
    });
  }
}

// ---- E. idle double-click; release + quick clicks never re-key; the guard lets go ---------------------
test("E an idle double-click keys once and its 2nd click unkeys", async () => {
  for (const lat of [5, 25, 60]) {
    const e = pageEnv(lat);
    e.mouse(1); e.adv(150); e.mouse(2); e.adv(3000);
    eq([e.ons(), e.offs(), e.S.keyed, e.S.keys, e.label()], [1, 1, false, 1, "PTT"], `E idle double-click (one-way ${lat} ms)`);
  }
});
test("E an idle triple-click: one key, one unkey, the 3rd click sends nothing", async () => {
  const e = pageEnv(25);
  e.mouse(1); e.adv(150); e.mouse(2); e.adv(150); e.mouse(3); e.adv(3000);
  eq([e.ptt(), e.S.keyed, e.S.keys], [["ptt:on", "ptt:off"], false, 1], "E triple-click");
});
test("E after a release, quick clicks never re-key; a press after the guard keys again", async () => {
  const e = pageEnv(25);
  e.mouse(1); e.adv(3000);
  e.mouse(1);                                      // STOP
  const tOff = e.clock.now;
  e.adv(100); e.mouse(1);                          // a slow double-click reads detail 1
  e.adv(300); e.key("Enter");                      // and the keyboard
  e.adv(300); e.pen(1);
  e.adv(300); e.mouse(1);                          // each refused press pushes the guard out
  const tLast = e.clock.now;
  e.adv(GUARD_MS - 50); e.mouse(1);                // still inside the guard of the last refused press
  eq([e.ons(), e.S.keyed], [1, false], "E presses inside the guard send nothing (" + (e.clock.now - tOff) + " ms after the STOP)");
  check(tLast - tOff > GUARD_MS, "E (the burst outlasted the first 800 ms window)");
  e.adv(GUARD_MS + 50); e.mouse(1);
  e.adv(1000);
  eq([e.ons(), e.S.keyed, e.S.keys], [2, true, 2], "E a deliberate press after the guard keys again");
});
test("E the server ends the session as STOP is clicked: no click re-keys", async () => {
  const e = pageEnv(25);
  e.mouse(1); e.adv(3000);
  e.serverEnds(); e.adv(30);                       // the null frame has landed
  e.mouse(1); e.adv(250); e.mouse(2); e.adv(250); e.key("Enter");
  e.adv(2000);
  eq([e.ons(), e.S.keyed], [1, false], "E clicks meant as STOP right after the server's end send nothing");
});

// ---- F. keyboard, touch and pen ---------------------------------------------------------------------
test("F keyboard: Enter keys, Enter / Space stop, quick presses after the stop send nothing", async () => {
  const e = pageEnv(25);
  e.key("Enter"); e.adv(350);
  e.key("Enter"); e.adv(200);
  e.key(" "); e.adv(200); e.key("Enter"); e.adv(2000);
  eq([e.ptt(), e.S.keyed], [["ptt:on", "ptt:off"], false], "F Enter twice: key then stop; the rest is inert");
  e.key(" "); e.adv(500);
  eq([e.ons(), e.S.keyed], [2, true], "F Space keys after the guard");
  e.key(" "); e.adv(500);
  eq([e.offs(), e.S.keyed], [2, false], "F Space stops");
});
test("F keyboard: a held key's auto-repeat never keys, but unkeys an owned PTT", async () => {
  const e = pageEnv(25);
  eq([e.key("Enter", true), e.key(" ", true)], [false, false], "F idle: repeats are default-prevented");
  eq(e.frames(), [], "F idle: a repeat sends nothing");
  e.key("Enter"); e.adv(500);                      // keyed by a real press
  check(ON_AIR.test(e.label()), "F keyed by Enter");
  eq(e.key("Enter", true), true, "F owned: a repeat is NOT prevented (it unkeys like any press)");
  e.adv(100);
  eq([e.ptt(), e.S.keyed], [["ptt:on", "ptt:off"], false], "F owned: the repeat unkeyed");
  eq(e.key("Enter", true), false, "F after the unkey: further repeats are prevented again");
  e.adv(3000);
  eq([e.ons(), e.S.keyed], [1, false], "F a held Enter: one key, one unkey, no chatter");
});
test("F touch: a 400 ms hold keys (its compat click is swallowed); a tap unkeys at once; a double-tap never re-keys", async () => {
  const e = pageEnv(25);
  e.touch(400); e.adv(1000);
  eq([e.ptt(), e.S.keyed], [["ptt:on"], true], "F touch hold keyed, the compat click did not unkey it");
  e.touch(80); e.adv(120); e.touch(80); e.adv(3000);
  eq([e.ptt(), e.S.keyed, e.S.keys], [["ptt:on", "ptt:off"], false, 1], "F touch double-tap: unkeyed once, never re-keyed");
  e.touch(80); e.adv(1000);
  eq(e.ons(), 1, "F a short tap on an idle PTT keys nothing");
});
test("F touch: a hold right after a release is refused; after the guard it keys", async () => {
  const e = pageEnv(25);
  e.mouse(1); e.adv(2000); e.mouse(1);             // key, STOP
  e.adv(100); e.touch(400);                        // a deliberate hold inside the guard
  e.adv(1000);
  eq([e.ons(), e.S.keyed], [1, false], "F a touch hold inside the guard keys nothing");
  e.adv(1000); e.touch(400); e.adv(1000);
  eq([e.ons(), e.S.keyed], [2, true], "F a touch hold after the guard keys");
});
test("F a keyboard STOP inside a touch's compat-click window still unkeys", async () => {
  const e = pageEnv(25);
  e.touch(400, false);                             // keyed by a hold; the compat click is still to come
  e.adv(100);
  check(e.ctx.RadioTx.ownedPtt(), "F keyed by the hold");
  e.key("Enter");                                  // detail 0: never taken for a compat click
  e.adv(3000);
  eq([e.ptt(), e.S.keyed], [["ptt:on", "ptt:off"], false], "F Enter right after the touch hold unkeyed");
  const a = pageEnv(25);
  a.touch(400, false); a.adv(100);
  a.curOp = "assistive";
  a.btn.dispatchEvent(new a.Event("click", { detail: 1, pointerType: "" }));   // a screen reader's click
  a.adv(3000);
  eq([a.ptt(), a.S.keyed], [["ptt:on", "ptt:off"], false], "F an assistive-tech click right after the touch hold unkeyed");
  const t = pageEnv(25);
  t.touch(400, false); t.adv(100);
  t.btn.dispatchEvent(new t.Event("click", { detail: 1 }));                    // the compat click, as a MouseEvent
  t.adv(3000);
  eq([t.ptt(), t.S.keyed], [["ptt:on"], true], "F the compat click (MouseEvent, no pointerType) is swallowed: the hold stays keyed");
});
test("F pen: a pen tap keys, a pen double-tap on ON AIR stops and never re-keys", async () => {
  const e = pageEnv(25);
  e.pen(1); e.adv(3000);
  e.pen(1); e.adv(250); e.pen(2); e.adv(3000);
  eq([e.ptt(), e.S.keyed, e.S.keys], [["ptt:on", "ptt:off"], false, 1], "F pen double-tap");
  e.pen(1); e.adv(300); e.pen(2); e.adv(3000);
  eq([e.ptt(), e.S.keyed, e.S.keys], [["ptt:on", "ptt:off", "ptt:on", "ptt:off"], false, 2], "F pen: key (detail 1) then STOP (detail 2)");
});
test("F pen / mouse clicks right after a touch are real presses (a pen pointerdown ends the swallow window)", async () => {
  const e = pageEnv(25);
  e.touch(400, false); e.adv(100);                 // keyed by a hold
  e.pen(1); e.adv(3000);
  eq([e.ptt(), e.S.keyed], [["ptt:on", "ptt:off"], false], "F a pen tap right after the touch unkeyed");
});

// ---- G. randomized property --------------------------------------------------------------------------
test("G property: a press while owned/pending always ends unkeyed; nothing keys inside the guard; only deliberate idle presses key", async () => {
  let seed = 0x5EED2026;
  const rnd = () => { seed ^= seed << 13; seed >>>= 0; seed ^= seed >>> 17; seed ^= seed << 5; seed >>>= 0; return seed / 4294967296; };
  const pick = (a) => a[Math.floor(rnd() * a.length)];
  let badA = 0, badB = 0, badC = 0, ons = 0, offs = 0, ownedPresses = 0, firstBad = "";
  const note = (s) => { if (!firstBad) firstBad = s; };
  for (let run = 0; run < 80; run++) {
    const e = pageEnv(5 + Math.floor(rnd() * 146));
    const dblMs = 300 + Math.floor(rnd() * 1200);  // this "user's" OS double-click time (slow settings too)
    let lastPtr = -1e9, detail = 0;
    const ptrDetail = () => { detail = e.clock.now - lastPtr < dblMs ? detail + 1 : 1; lastPtr = e.clock.now; return detail; };
    const trace = [];
    for (let i = 0; i < 60; i++) {
      const r = rnd();
      const was = e.ctx.RadioTx.ownedOrPending();
      const onsBefore = e.ons();
      let op;
      if (r < 0.30) { const d = ptrDetail(); op = "m" + d; e.mouse(d); }
      else if (r < 0.40) { const d = ptrDetail(); op = "p" + d; e.pen(d); }
      else if (r < 0.50) { op = "k"; e.key(pick(["Enter", " "])); }
      else if (r < 0.54) { op = "kr"; e.key("Enter", true); }
      else if (r < 0.62) { const h = pick([60, 120, 320, 450, 700]); op = "t" + h; e.touch(h, rnd() < 0.85); }
      else if (r < 0.68) { op = "end"; e.curOp = "server"; e.serverEnds(); }
      else { const ms = Math.floor(rnd() * (rnd() < 0.5 ? 400 : 1600)); op = "w" + ms; e.curOp = "wait"; e.adv(ms); }
      trace.push(op);
      if (was && /^(m|p|k|t)/.test(op)) {
        ownedPresses++;
        if (e.ons() !== onsBefore) { badA++; note(`run ${run}: a press while owned/pending sent ptt:on; trace ${trace.join(" ")}`); }
        const tag = trace.length, tPress = e.clock.now;
        e.clock.setTimeout(() => {                  // (a) one round trip later the radio is unkeyed
          if (e.S.keyed) { badA++; note(`run ${run}: still keyed ${2 * e.lat + 1} ms after press #${tag} (${op}) at ${tPress} while owned/pending; trace ${trace.join(" ")}`); }
        }, 2 * e.lat + 1);
      }
    }
    e.curOp = "wait";
    e.adv(3000);
    let lastEnd = -1e9;
    const ends = e.offSeen.slice();
    for (const [t, f, opName] of e.log) {
      if (f.startsWith("ptt:off")) { lastEnd = Math.max(lastEnd, t); offs++; continue; }
      if (f !== "ptt:on") continue;
      ons++;
      const end = Math.max(lastEnd, ...ends.filter((x) => x <= t), -1e9);
      if (t - end < GUARD_MS) { badB++; note(`run ${run}: ptt:on ${t - end} ms after this page's PTT ended (${opName})`); }
      // (c) only a deliberate idle press keys: a click / pen tap with detail <= 1, a real key press, a touch hold
      if (!/^(mouse[01]|pen[01]|key|touch(3|4|7))/.test(opName) || /^key-repeat/.test(opName)) {
        badC++; note(`run ${run}: ptt:on from ${opName}`);
      }
    }
  }
  check(badA === 0, "G (a) " + badA + " presses while owned/pending did not end unkeyed: " + firstBad);
  check(badB === 0, "G (b) " + badB + " re-keys inside the guard window: " + firstBad);
  check(badC === 0, "G (c) " + badC + " keys from a press that may not key: " + firstBad);
  check(ons > 150 && offs > 150 && ownedPresses > 300,
        `G the runs exercised both sides (${ons} ptt:on, ${offs} ptt:off, ${ownedPresses} presses while owned/pending)`);
  console.log(`info G property: 80 runs x 60 ops: ${ons} ptt:on, ${offs} ptt:off, ${ownedPresses} presses while owned/pending`);
});

// ---- H. a claim granted while the tab is hidden (or after pagehide) is released ----------------------
const CLAIMS = [
  ["VOX", { action: "rx_func", name: "vox", on: true }, "vox:off(auto)"],
  ["CW", { action: "cw_tx", text: "CQ", wpm: 18 }, "cw_stop(auto)"],
  ["PTT", { action: "ptt", tx: true }, "ptt:off(auto)"],
];
for (const [name, msg, rel] of CLAIMS) {
  for (const away of ["hidden", "pagehide"]) {
    test(`H a ${name} claim pending when the tab is ${away}: the grant is released at once, heartbeats stop`, async () => {
      const e = pageEnv(40);
      eq(e.ctx.RadioControl.send(msg), true, `H ${name}: the claim left the page`);
      e.adv(20);                                   // the claim is still on its way
      const before = e.frames().length;
      if (away === "hidden") e.hide(); else e.win("pagehide", {});
      e.adv(30);                                   // the server has it now; its grant lands at +100
      e.adv(60);
      // VOX / CW: nothing was HELD at the hide, so the release must come with the grant (round 3);
      // PTT: the hide itself releases the pending claim (auto), as before
      check(e.frames().slice(before).includes(rel), `H ${name}/${away}: the claim was released (${JSON.stringify(e.frames())})`);
      e.adv(10000);
      eq([...e.S.claims], [], `H ${name}/${away}: the server holds nothing`);
      eq([e.ctx.TR.isTxOwner(), e.ctx.RadioTx.ownedOrPending()], [false, false], `H ${name}/${away}: the page owns nothing`);
      const lastHb = e.S.hbTimes.length ? e.S.hbTimes[e.S.hbTimes.length - 1] : 0;
      check(e.clock.now - lastHb > 9000, `H ${name}/${away}: no heartbeat keeps it alive (last tx_hb ${e.clock.now - lastHb} ms ago)`);
      if (name === "PTT") eq(e.S.keyed, false, "H PTT: unkeyed");
    });
  }
}
test("H a grant while the tab is visible is kept; after hide + show a new grant is kept too", async () => {
  const e = pageEnv(40);
  e.ctx.RadioControl.send({ action: "rx_func", name: "vox", on: true });
  e.adv(1000);
  eq([[...e.S.claims], e.ctx.RadioTx.holds("vox"), e.frames()], [["vox"], true, ["vox:on"]], "H visible: VOX granted and kept");
  e.hide();                                        // hiding releases a HELD claim (as before)
  e.adv(1000);
  eq([[...e.S.claims], e.frames()], [[], ["vox:on", "vox:off(auto)"]], "H hide releases the held VOX");
  e.show();
  e.ctx.RadioControl.send({ action: "rx_func", name: "vox", on: true });
  e.adv(1000);
  eq([[...e.S.claims], e.frames().length], [["vox"], 3], "H visible again: a new VOX grant is kept");
  e.win("pagehide", {}); e.adv(1000);
  eq([...e.S.claims], [], "H pagehide releases it");
  e.win("pageshow", {});
  e.ctx.RadioControl.send({ action: "cw_tx", text: "CQ", wpm: 18 });
  e.adv(1000);
  eq([...e.S.claims], ["cw"], "H back from the bfcache (pageshow): a new grant is kept");
});

// ---- I. no key is ever SENT while the tab is hidden or after pagehide (final review P2) --------------
// A touch hold keys from a 300 ms timer. Before this fix, a hold still timing when the tab was hidden
// (or the page left) fired anyway and sent ptt:true from the hidden page; only the grant's auto-release
// (section H) took it back, after the radio had keyed. Now hide / pagehide cancel the hold, and no TX
// claim (ptt, vox, cw, tune) leaves a hidden or departing page at all.
for (const away of ["hidden", "pagehide"]) {
  const goAway = (e) => (away === "hidden" ? e.hide() : e.pagehide());
  const comeBack = (e) => (away === "hidden" ? e.show() : e.pageshow());
  for (const heldMs of [40, 150, 290]) {
    test(`I a touch hold ${heldMs} ms in when the tab is ${away}: it never keys, not even after the tab is back`, async () => {
      const e = pageEnv(25);
      e.touchDown();
      e.adv(heldMs);
      goAway(e);
      e.curOp = "wait";
      e.adv(600);                                  // the 300 ms hold timer would have fired in here
      eq([e.frames(), e.claimsSentAway, e.S.keys], [[], [], 0], `I [${away}/${heldMs}] nothing sent while away, never keyed`);
      comeBack(e);
      e.adv(600);                                  // the finger is still down: a cancelled hold stays cancelled
      eq([e.ons(), e.S.keyed], [0, false], `I [${away}/${heldMs}] back with the finger still down: still not keyed`);
      e.touchUp();                                 // lift; the browser's compat click follows
      e.adv(1500);
      eq([e.frames(), e.S.keys, e.ctx.RadioTx.ownedOrPending(), e.label()], [[], 0, false, "PTT"],
         `I [${away}/${heldMs}] the lift and its compat click key nothing`);
      e.touch(400); e.adv(1000);                   // positive control: a new deliberate hold keys
      eq([e.ptt(), e.S.keyed], [["ptt:on"], true], `I [${away}/${heldMs}] a fresh hold after the return keys`);
    });
  }
  test(`I a touch hold that spans a short ${away} (back before 300 ms) is cancelled, not resumed`, async () => {
    const e = pageEnv(25);
    e.touchDown(); e.adv(80);
    goAway(e); e.adv(60);
    comeBack(e);
    e.curOp = "wait"; e.adv(800);                  // the old timer would fire at 300 ms, on a visible page
    eq([e.frames(), e.S.keys], [[], 0], `I [${away}] the interrupted hold keyed nothing`);
    e.touchUp(); e.adv(1000);
    eq([e.frames(), e.S.keys], [[], 0], `I [${away}] its lift + compat click keyed nothing`);
  });
  test(`I a touch hold lifted while the tab is ${away}: nothing is sent`, async () => {
    const e = pageEnv(25);
    e.touchDown(); e.adv(100);
    goAway(e);
    e.adv(100); e.touchUp(); e.adv(1000);
    comeBack(e); e.adv(1000);
    eq([e.frames(), e.claimsSentAway, e.S.keys], [[], [], 0], `I [${away}] a hold lifted while away sent nothing`);
  });
  for (const [name, msg] of [["PTT", { action: "ptt", tx: true }], ["VOX", { action: "rx_func", name: "vox", on: true }],
                             ["CW", { action: "cw_tx", text: "CQ", wpm: 18 }], ["TUNE", { action: "tune_atu" }]]) {
    test(`I a ${name} claim asked for while the tab is ${away} is not sent (RadioControl.send -> false)`, async () => {
      const e = pageEnv(25);
      goAway(e);
      e.curOp = "radiocontrol";
      eq(e.ctx.RadioControl.send(Object.assign({}, msg)), false, `I [${away}/${name}] send() refuses`);
      e.adv(1000);
      eq([e.frames(), [...e.S.claims], e.ctx.RadioTx.ownedOrPending(), e.S.hb], [[], [], false, 0],
         `I [${away}/${name}] no frame, no claim, nothing pending, no heartbeat`);
      comeBack(e);
      eq(e.ctx.RadioControl.send(Object.assign({}, msg)), true, `I [${away}/${name}] back: the same send goes out`);
    });
  }
  test(`I a click / Enter on PTT while the tab is ${away} sends nothing`, async () => {
    const e = pageEnv(25);
    goAway(e);
    e.mouse(1); e.adv(200); e.key("Enter"); e.adv(200); e.pen(1); e.adv(1000);
    eq([e.frames(), e.S.keys], [[], 0], `I [${away}] presses while away sent nothing`);
  });
}
test("I property: random touches, holds, hides, pagehides and claims never send a claim while away", async () => {
  let seed = 0x0A11D0E5;
  const rnd = () => { seed ^= seed << 13; seed >>>= 0; seed ^= seed >>> 17; seed ^= seed << 5; seed >>>= 0; return seed / 4294967296; };
  const pick = (a) => a[Math.floor(rnd() * a.length)];
  const CLAIM_MSGS = [{ action: "ptt", tx: true }, { action: "rx_func", name: "vox", on: true },
                      { action: "cw_tx", text: "CQ", wpm: 18 }, { action: "tune_atu" }];
  let bad = 0, holdKeysAfterAway = 0, holdKeys = 0, awayPeriods = 0, cancelledHolds = 0, firstBad = "";
  for (let run = 0; run < 60; run++) {
    const e = pageEnv(5 + Math.floor(rnd() * 100));
    let state = "visible", down = false, awaySinceDown = false, downAt = 0;
    const trace = [];
    const orig = e.sock.send;
    e.sock.send = (d) => {                        // a ptt:on while the finger is down and no press runs = the hold timer
      const f = JSON.parse(d);
      if (f.action === "ptt" && f.tx === true && down && e.curOp === "wait") {
        holdKeys++;
        if (awaySinceDown) { holdKeysAfterAway++; if (!firstBad) firstBad = `run ${run}: a hold keyed after an away; trace ${trace.join(" ")}`; }
      }
      orig(d);
    };
    for (let i = 0; i < 50; i++) {
      const r = rnd();
      let op;
      if (r < 0.18 && !down) { op = "down"; e.touchDown(); down = true; awaySinceDown = false; downAt = e.clock.now; }
      else if (r < 0.30 && down) {
        op = "up"; if (e.clock.now - downAt < 300 && awaySinceDown) cancelledHolds++;
        e.touchUp(rnd() < 0.8); down = false;
      }
      else if (r < 0.42) {
        if (state === "visible") { if (rnd() < 0.6) { op = "hide"; e.hide(); state = "hidden"; } else { op = "pagehide"; e.pagehide(); state = "gone"; } awayPeriods++; if (down) awaySinceDown = true; }
        else if (state === "hidden") { op = "show"; e.show(); state = "visible"; }
        else { op = "pageshow"; e.pageshow(); state = "visible"; }
      }
      else if (r < 0.52) { op = "claim"; e.curOp = "radiocontrol"; e.ctx.RadioControl.send(Object.assign({}, pick(CLAIM_MSGS))); }
      else if (r < 0.60 && state === "visible") { op = "click"; e.mouse(1); }
      else if (r < 0.64) { op = "end"; e.curOp = "server"; e.serverEnds(); }
      else { const ms = pick([50, 120, 200, 280, 350, 600, 1500]); op = "w" + ms; e.curOp = "wait"; e.adv(ms); }
      trace.push(op);
    }
    e.curOp = "wait"; e.adv(4000);
    if (e.claimsSentAway.length) {
      bad += e.claimsSentAway.length;
      if (!firstBad) firstBad = `run ${run}: ${JSON.stringify(e.claimsSentAway[0])} sent while away; trace ${trace.join(" ")}`;
    }
  }
  check(bad === 0, `I property: ${bad} claim frames were sent while the page was away: ${firstBad}`);
  check(holdKeysAfterAway === 0, `I property: ${holdKeysAfterAway} touch holds keyed although the tab went away during them: ${firstBad}`);
  check(holdKeys > 20 && awayPeriods > 200 && cancelledHolds > 10,
        `I property exercised holds (${holdKeys} hold keys), away periods (${awayPeriods}) and holds spanning an away (${cancelledHolds})`);
  console.log(`info I property: 60 runs x 50 ops: ${holdKeys} hold keys, ${awayPeriods} away periods, ${cancelledHolds} holds spanning an away`);
});

(async () => {
  for (const [name, fn] of pending) {
    try { await fn(); } catch (err) { failures++; checks++; console.log("FAIL " + name + " threw: " + (err && err.stack || err)); }
  }
  console.log(failures ? failures + " of " + checks + " checks FAILED" : "ALL PASS (" + checks + " checks)");
  process.exit(failures ? 1 : 0);
})();
