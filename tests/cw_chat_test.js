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
 */
"use strict";
const { makeEnv } = require("./fakedom");

let failures = 0, checks = 0;
function check(cond, msg) { checks++; if (!cond) { failures++; console.log("FAIL " + msg); } }
function eq(a, b, msg) { const ja = JSON.stringify(a), jb = JSON.stringify(b); check(ja === jb, msg + ": got " + ja + ", want " + jb); }

const SR = 3200, HOP = 48;                                   // == cwtool.js SR / the worker's hop

async function page() {
  const e = makeEnv();
  const tile = e.el("div", { class: "grid-stack-item", "data-tile": "cw" });
  e.el("div", { id: "cwTool" }, tile);
  for (const id of ["cwToolBtn", "cwDecodeBtn", "cwClose", "cwClear", "cwPlay", "cwSend", "cwTx"]) e.el("button", { id, type: "button" }, tile);
  for (const id of ["cwHead", "cwHint", "cwOut", "cwStatus", "cwTxHint", "cwTxRow"]) e.el("div", { id }, tile);
  e.load("tr.js");
  e.ctx.RadioControl = { send: () => { throw new Error("nothing is sent here"); }, state: () => ({ connected: true, mode_name: "CW" }) };
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
  console.log(failures ? `${failures} of ${checks} checks FAILED` : `ALL PASS (${checks} checks)`);
  process.exit(failures ? 1 : 0);
})().catch((err) => { console.log("FAIL (crash) " + (err && err.stack || err)); process.exit(1); });
