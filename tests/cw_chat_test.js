/* The CW chat's received bubbles (frontend/cwtool.js): a pause of 5 s or more in the received text starts
 * a new bubble. The REAL tr.js + cwtool.js in a Node vm on tests/fakedom.js, with a stand-in for the
 * neural decoder's Worker (it answers what the test says the model heard) and for the audio capture
 * worklet. No browser, no model, no radio; nothing here keys anything. Run by tests/t_c1.py (unit):
 *
 *     node tests/cw_chat_test.js        -> exit 0 and "ALL PASS", or exit 1 naming each failure
 *
 *  W1. text decoded close together stays in one bubble
 *  W2. new text after a pause of 5 s or more (the signal still there, nothing decoded) is a new bubble,
 *      from its live preview on; a shorter pause is not
 *  W3. Clear empties the chat and forgets the pause
 *  W4. in the Modem window (the panel is its CW pane): committed text is news for the CW key, a live
 *      preview alone is not
 *  W5. CwPanel.summary(): the latest text heard, the decoder on / off, this page's CW on the air;
 *      CwPanel.busy(): only this page's own CW message on the air
 *  W6. the scope's CW button: the window shown and on CW, the decoder started (a running one keeps its
 *      chat); with CW in view it toggles the decoder; while SSTV sends a picture nothing changes. The
 *      window collapsed: the decoder keeps listening; the window removed: it stops
 */
"use strict";
const { makeEnv } = require("./fakedom");

let failures = 0, checks = 0;
function check(cond, msg) { checks++; if (!cond) { failures++; console.log("FAIL " + msg); } }
function eq(a, b, msg) { const ja = JSON.stringify(a), jb = JSON.stringify(b); check(ja === jb, msg + ": got " + ja + ", want " + jb); }

const SR = 3200, HOP = 48;                                   // == cwtool.js SR / the worker's hop

async function page() {
  const e = makeEnv();
  const tile = e.el("div", { class: "grid-stack-item", "data-tile": "sstv" });   // the Modem window; the panel is its CW pane
  e.el("div", { id: "cwTool" }, tile);
  for (const id of ["cwToolBtn", "cwDecodeBtn", "cwClear", "cwPlay", "cwSend", "cwTx"]) e.el("button", { id, type: "button" }, tile);
  for (const id of ["cwHead", "cwHint", "cwOut", "cwStatus", "cwTxHint", "cwTxRow"]) e.el("div", { id }, tile);
  e.load("tr.js");
  e.st = { connected: true, mode_name: "CW" };
  e.ctx.RadioControl = { send: () => { throw new Error("nothing is sent here"); }, state: () => e.st };
  e.ctx.RadioTx = { owner: () => null, nextSeq: () => 1 };
  e.ctx.Worker = function () { e.worker = this; this.posted = []; this.postMessage = (m) => this.posted.push(m); };
  e.ctx.AudioWorkletNode = function () { e.node = this; this.port = {}; this.connect = () => {}; this.disconnect = () => {}; };
  const actx = { audioWorklet: { addModule: () => Promise.resolve() }, destination: {} };
  e.ctx.RadioAudio = { ensure() {}, ctx: () => actx, bus: () => ({ connect() {} }), on: () => true, state: () => ({}) };
  e.load("cwtool.js");
  e.doc.getElementById("cwDecodeBtn").dispatchEvent(new e.Event("click", { bubbles: true }));
  for (let i = 0; i < 5; i++) await e.flush();
  e.worker.onmessage({ data: { type: "ready", hop: HOP } });
  // one second of the radio's audio (a steady signal: never "quiet") and the decode tick, then the model's
  // answer to the decode it asked for (as the real worker does, one at a time): `text` heard, and with
  // `commit` a settled word gap as late as cwtool allows (its tail guard), when the audio is long enough
  let answered = 0;
  e.sec = (text, commit) => {
    e.node.port.onmessage({ data: new Float32Array(SR).fill(0.05) });
    e.clock.advance(1000);
    const d = e.worker.posted.filter((m) => m.type === "decode").slice(-1)[0];
    if (!d || d.id === answered) return false;
    answered = d.id;
    let len = d.audio.length;                                // the real audio, before the zero padding
    while (len > 0 && d.audio[len - 1] === 0) len--;
    const gap = len - Math.round(1.25 * SR) - 200;           // == cwtool TAIL_GUARD, a little inside it
    let ws = [], cs = [];
    const can = commit && gap >= 2 * SR + 100;               // == cwtool MIN_CONFIRMED
    if (can) {
      const f = Math.floor(gap / HOP) - 1;
      cs = commit.split("").map((ch, i) => ({ char: ch, endFrame: Math.min(f - 2, 10 + i) })).concat([{ char: " ", endFrame: f + 2 }]);
      ws = [{ startFrame: f, endFrame: f + 2 }];
    }
    e.worker.onmessage({ data: { type: "result", id: d.id, text, wordSpaceSpans: ws, characterSpans: cs } });
    return !!can;
  };
  e.say = (text) => { for (let i = 0; i < 8; i++) if (e.sec(text, text)) return; throw new Error("never committed: " + text); };
  e.bubbles = () => {
    const html = e.doc.getElementById("cwOut").innerHTML || "";
    return [...html.matchAll(/<div class="cw-msg rx">(.*?)<\/div><\/div>/g)].map((m) => m[1].replace(/<[^>]+>/g, ""));
  };
  e.click = (el) => el.dispatchEvent(new e.Event("click", { bubbles: true }));
  e.dec = e.doc.getElementById("cwDecodeBtn");
  e.decOn = () => e.dec.getAttribute("aria-pressed") === "true" && e.dec.classList.contains("on");
  return e;
}

(async function main() {
  // W1 + W2
  {
    const e = await page();
    e.sec(); e.sec("CQ"); e.say("CQ CQ");
    e.sec("DE N0"); e.say("DE N0CALL");
    eq(e.bubbles(), ["CQ CQ DE N0CALL"], "W1 text close together: one bubble");
    for (let i = 0; i < 3; i++) e.sec("");                    // 3 s: signal, nothing decoded
    e.sec("K");
    eq(e.bubbles(), ["CQ CQ DE N0CALL K"], "W2 a 4 s pause: still the same bubble (live preview)");
    e.say("K");
    for (let i = 0; i < 6; i++) e.sec("");                    // 6 s: signal, nothing decoded
    e.sec("N1CALL");
    eq(e.bubbles(), ["CQ CQ DE N0CALL K", "N1CALL"], "W2 a 6 s pause: the new characters open a new bubble");
    e.say("N1CALL DE");
    eq(e.bubbles(), ["CQ CQ DE N0CALL K", "N1CALL DE"], "W2 ... and their text is committed there");
    // W3
    e.doc.getElementById("cwClear").dispatchEvent(new e.Event("click", { bubbles: true }));
    eq(e.bubbles(), [], "W3 Clear empties the chat");
    for (let i = 0; i < 6; i++) e.sec("");
    e.sec("TE"); e.say("TEST"); e.sec("IN"); e.say("ING");
    eq(e.bubbles(), ["TEST ING"], "W3 ... and forgets the pause: the next text is one bubble");
  }
  // a commit long after its text first showed does not move it to a new bubble
  {
    const e = await page();
    e.sec(); e.sec("CQ"); e.say("CQ");
    e.sec("QRZ");
    for (let i = 0; i < 6; i++) e.sec("QRZ");                 // the same text for 6 s (no new characters)
    e.say("QRZ");
    eq(e.bubbles(), ["CQ QRZ"], "W2 text committed 6 s after it showed stays in the bubble it showed in");
  }
  // W4: news for the window's CW key
  {
    const e = await page();
    const news = [];
    e.doc.addEventListener("tr:digital", (ev) => news.push(ev.detail && ev.detail.mode));
    e.sec(); e.sec("CQ");
    eq(news, [], "W4 a live preview alone: no news");
    e.say("CQ CQ");
    check(news.length > 0 && news.every((m) => m === "cw"), "W4 committed text: news for CW's key: " + JSON.stringify(news));
  }
  // W5: the collapsed window's summary; busy = this page's own CW message on the air
  {
    const e = await page();
    const P = e.ctx.CwPanel;
    eq([P.summary(), P.busy()], ["CW decoder on", false], "W5 the decoder on, nothing heard yet; not busy");
    e.sec(); e.say("CQ CQ DE N0CALL");
    eq(P.summary(), "CW CQ CQ DE N0CALL", "W5 the latest text heard");
    e.say("N0CALL K");
    eq([e.bubbles(), P.summary()], [["CQ CQ DE N0CALL N0CALL K"], "CW DE N0CALL N0CALL K"], "W5 ... its last 18 characters");
    e.click(e.dec);
    eq([P.summary(), e.decOn()], ["CW decoder off", false], "W5 Decode off: says so");
    e.st = Object.assign({}, e.st, { cw_tx: true });
    e.ctx.TR.isTxOwner = () => false;
    eq([P.busy(), P.summary()], [false, "CW decoder off"], "W5 someone else's CW on the air: not this page's");
    e.ctx.TR.isTxOwner = () => true;
    eq([P.busy(), P.summary()], [true, "CW on the air"], "W5 this page's CW message on the air: busy (the window stays on CW)");
  }
  // W6: the scope's CW button and the window (stand-ins for RadioLayout and DigitalPanel)
  {
    const e = await page();                                  // page() turned the decoder on
    const calls = [];
    let vis = false, mode = "ft8", lock = false;
    e.ctx.RadioLayout = { visible: (id) => (calls.push(["visible", id]), vis), showTile: (id) => (calls.push(["showTile", id]), vis = true) };
    e.ctx.DigitalPanel = { mode: () => mode, show: (m) => { calls.push(["show", m]); if (lock) return false; mode = m; return true; } };
    const scopeBtn = e.doc.getElementById("cwToolBtn");
    e.click(e.dec);
    check(!e.decOn(), "W6 (Decode off to start with)");
    e.click(scopeBtn);
    eq(calls, [["visible", "sstv"], ["showTile", "sstv"], ["show", "cw"]], "W6 the scope's CW button: the window shown, on CW");
    check(e.decOn() && scopeBtn.classList.contains("active"), "W6 ... and the decoder started, Decode lit");
    calls.length = 0;
    e.click(scopeBtn);
    eq([calls, e.decOn()], [[["visible", "sstv"]], false], "W6 CW in view: the button toggles the decoder");
    e.click(e.dec);
    e.sec(); e.say("CQ");
    mode = "rtty";
    e.click(scopeBtn);
    eq([mode, e.decOn(), e.bubbles()], ["cw", true, ["CQ"]], "W6 another mode shown, the decoder running: CW in view, its chat kept");
    e.click(e.dec);
    mode = "sstv"; lock = true;
    e.click(scopeBtn);
    eq([mode, e.decOn()], ["sstv", false], "W6 SSTV sending a picture: the window stays on it, no decoder started");
    lock = false; mode = "cw";
    e.click(e.dec);
    e.doc.dispatchEvent(new e.ctx.CustomEvent("tr:tile", { detail: { id: "sstv", visible: true, collapsed: true } }));
    check(e.decOn(), "W6 the window collapsed: the decoder keeps listening");
    e.doc.dispatchEvent(new e.ctx.CustomEvent("tr:tile", { detail: { id: "spots", visible: false } }));
    check(e.decOn(), "W6 another panel removed: no matter");
    e.doc.dispatchEvent(new e.ctx.CustomEvent("tr:tile", { detail: { id: "sstv", visible: false } }));
    check(!e.decOn(), "W6 the window removed: the decoder stops");
  }
  console.log(failures ? `${failures} of ${checks} checks FAILED` : `ALL PASS (${checks} checks)`);
  process.exit(failures ? 1 : 0);
})().catch((err) => { console.log("FAIL (crash) " + (err && err.stack || err)); process.exit(1); });
