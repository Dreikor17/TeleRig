/* The fake page + fake server the PTT tests share (tests/ptt_failsafe_test.js, tests/ptt_hotkey_test.js).
 * The REAL frontend code (tr.js + app.js) runs in a Node vm on tests/fakedom.js. No browser, no server
 * process, no socket, no radio: the PTT button is a fake element of a FAKE page, and the "server" is a
 * few lines of in-memory JS applying the arbiter's claim rules for this one connection with a one-way
 * latency (every frame the page sends reaches it `lat` ms later, every answer reaches the page `lat` ms
 * later). "keyed" is a boolean in here.
 *
 *   const e = pageEnv(lat, opts)     opts.localStorage: initial localStorage content (the PTT hotkey)
 *   e.mouse(detail) e.pen(detail) e.key(key, repeat) e.touch(holdMs)   input, as a browser delivers it
 *   e.adv(ms)                        advance the fake clock
 *   e.S                              the fake server: {keyed, keys, claims, ...}
 *   e.log / e.frames() / e.ptt() / e.ons() / e.offs()                 what the page sent
 */
"use strict";
const { makeEnv } = require("./fakedom");

const GUARD_MS = 800;                              // app.js PTT_REKEY_GUARD_MS
const ON_AIR = /^ON AIR \d+s — STOP$/;

function pageEnv(lat, opts) {
  const sockets = [];
  function FakeWS() { this.readyState = 1; this.sent = []; this.send = (d) => this.sent.push(JSON.parse(d)); this.close = () => {}; sockets.push(this); }
  const e = makeEnv(Object.assign({ WebSocket: FakeWS }, opts || {}));
  const winL = {};                                 // window listeners (fakedom's window ignores them)
  e.ctx.addEventListener = (t, fn) => { (winL[t] = winL[t] || []).push(fn); };
  e.ctx.removeEventListener = (t, fn) => { const l = winL[t]; if (l && l.indexOf(fn) >= 0) l.splice(l.indexOf(fn), 1); };
  e.win = (type, init) => { const ev = new e.Event(type, init); for (const fn of (winL[type] || []).slice()) fn(ev); return ev; };
  e.ctx.Scope = function () { return new Proxy({ meta: {} }, { get: (t, k) => (k in t ? t[k] : () => []) }); };
  for (const id of ["onAir", "onAirStop", "onAirStopLbl", "onAirWho", "onAirTime", "txStopFab", "toasts"]) e.el("div", { id });
  e.btn = e.el("button", { id: "pttBtn" });
  if (opts && typeof opts.before === "function") opts.before(e);   // more page elements, before the scripts run
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
  e.binary = [];                                   // [time, ArrayBuffer]: binary frames (TX audio) the page sent
  e.curOp = "boot";
  const orig = ws.send;
  ws.send = (d) => {
    if (typeof d !== "string") { e.binary.push([e.clock.now, d]); return; }
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
    if (typeof d !== "string") { orig2(d); return; }       // binary (TX audio): never a claim
    const f = JSON.parse(d);
    const lbl = f.action === "ptt" ? "ptt:" + (f.tx ? "on" : "off") : f.action === "rx_func" ? "vox:" + (f.on ? "on" : "off") : f.action;
    if (CLAIM_LBL.test(lbl) && e.awayNow()) e.claimsSentAway.push([e.clock.now, lbl, e.curOp]);
    orig2(d);
  };
  return e;
}

module.exports = { pageEnv, GUARD_MS, ON_AIR };
