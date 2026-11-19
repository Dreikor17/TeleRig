/* Generated TX audio (app.js RadioAudio.txBegin / txPush / txEnd, for sstv-tx.js): the REAL tr.js +
 * app.js in a Node vm (tests/pttenv.js on tests/fakedom.js). No browser, no server, no radio: the
 * socket is fake and records what the page sends. Run by tests/t_c1.py (unit):
 *
 *     node tests/tx_audio_test.js       -> exit 0 and "ALL PASS", or exit 1 naming each failure
 *
 *  G1. txBegin: the server's mic flag on for this connection; nothing else, no claim (it keys nothing)
 *  G2. txPush: one binary frame per chunk, the mic's format (16 kHz mono int16), exactly the samples
 *      given (a part of a buffer is sent as just that part); nothing before txBegin or after txEnd
 *  G3. txEnd: the mic flag off again (the client mic was off); a second txBegin while one runs: refused
 *  G4. the server's {type: mic, ready} and a TX device error reach the page as tr:mic / tr:error
 *  G5. a viewer can't take the path; a dropped connection gives it back
 */
"use strict";
const { pageEnv } = require("./pttenv");

let failures = 0, checks = 0;
function check(cond, msg) { checks++; if (!cond) { failures++; console.log("FAIL " + msg); } }
function eq(a, b, msg) { const ja = JSON.stringify(a), jb = JSON.stringify(b); check(ja === jb, msg + ": got " + ja + ", want " + jb); }

const jsonSent = (e) => e.sock.sent.filter((f) => f && f.action && f.action !== "tx_hb").map((f) => Object.assign({}, f));
const mic = (e) => jsonSent(e).filter((f) => f.action === "mic").map((f) => f.on);
const samples = (buf) => Array.from(new Int16Array(buf));

{
  const e = pageEnv(0);
  const A = e.ctx.RadioAudio;
  const before = jsonSent(e).length;
  eq([A.txPush(new Int16Array([1, 2])), e.binary.length], [false, 0], "G2 nothing is sent before txBegin");
  eq(A.txBegin(), true, "G1 txBegin takes the path");
  eq(jsonSent(e).slice(before).map((f) => [f.action, f.on]), [["mic", true]], "G1 ... the server's mic flag on, nothing else");
  eq(A.txActive(), true, "G1 ... and says so");
  eq(A.txBegin(), false, "G3 a second txBegin while one runs: refused");
  const pcm = new Int16Array([1, -2, 32767, -32768, 0, 12345]);
  eq(A.txPush(pcm), true, "G2 txPush sends");
  eq([e.binary.length, samples(e.binary[0][1])], [1, [1, -2, 32767, -32768, 0, 12345]], "G2 one binary frame: exactly the samples");
  A.txPush(pcm.subarray(2, 4));
  eq(samples(e.binary[1][1]), [32767, -32768], "G2 a part of a buffer is sent as just that part");
  let got = null, err = null;
  e.doc.addEventListener("tr:mic", (ev) => { got = ev.detail; });
  e.doc.addEventListener("tr:error", (ev) => { err = ev.detail; });
  e.frame({ type: "mic", on: true, ready: true });
  eq(got && got.ready, true, "G4 the server's ready reaches the page as tr:mic");
  e.frame({ type: "error", code: "tx_device_missing", message: "The pinned TX device 'X' is not present." });
  eq(err && err.code, "tx_device_missing", "G4 a TX device error reaches the page as tr:error");
  A.txEnd();
  eq([mic(e), A.txActive()], [[true, false], false], "G3 txEnd: the mic flag off again (the client mic was off)");
  eq([A.txPush(pcm), e.binary.length], [false, 2], "G2 nothing is sent after txEnd");
  A.txEnd();
  eq(mic(e), [true, false], "G3 a second txEnd does nothing");
  eq(jsonSent(e).filter((f) => f.action === "ptt" || f.action === "cw_tx" || f.action === "rx_func").length, 0,
     "G1 none of this sends a claim: it keys nothing");
}
{
  const e = pageEnv(0);
  e.ctx.TR.can = (p) => p !== "tx";                        // a viewer (no tx permission)
  const n = jsonSent(e).length;
  eq([e.ctx.RadioAudio.txBegin(), jsonSent(e).length - n], [false, 0], "G5 a viewer can't take the path; nothing is sent");
}
{
  const e = pageEnv(0);
  e.ctx.RadioAudio.txBegin();
  e.sock.onclose({ code: 1006, reason: "" });
  eq(e.ctx.RadioAudio.txActive(), false, "G5 a dropped connection gives the path back");
}

console.log(failures ? `${failures} of ${checks} checks FAILED` : `ALL PASS (${checks} checks)`);
process.exit(failures ? 1 : 0);
