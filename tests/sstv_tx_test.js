/* SSTV transmit (frontend/sstv-tx.js): the REAL encoder and the REAL Send panel in a Node vm on
 * tests/fakedom.js. No browser, no server, no socket, no radio: RadioControl / RadioTx / RadioAudio are
 * stand-ins that only record what the panel asks for ("keyed" is a boolean here). Run by tests/t_c1.py:
 *
 *     node tests/sstv_tx_test.js        -> exit 0 and "ALL PASS", or exit 1 naming each failure
 *
 *  E1. the modes offered: only those whose whole transmission (settle, VIS, picture, tail) ends 3 s
 *      inside the radios' longest keying (177 s: 3 s inside their 3-minute TOT), so 174 s on the air at
 *      most, each with its time; the longer ones are not offered; the page's times are the server's
 *      (backend/sstv.py, checked by tests/t_c1.py)
 *  E2. the encoder, sample for sample, equals the reference encoder (tests/sstv_encode.js, written from
 *      the specifications) for every mode offered; the level scales it and never clips
 *  E3. what it sends decodes (the real decoder, sstv.js): the VIS picks the mode, the picture matches
 *  S1. the panel: no picture, nothing to send; the test card makes one; the text goes on the picture
 *  S2. Send: the TX path first (no claim before the server's "ready"), then ONE ptt claim, no audio
 *      before the grant; then the audio in real time, never more than 120 ms ahead, every sample as
 *      encoded; after the last sample has played out, ONE release and the path given back
 *  S3. STOP at every step: before ready (no claim at all), while claiming (the pending claim released),
 *      on the air (released at once, no more audio); a click right after starts nothing
 *  S4. the claim lost on the air (limit, STOP elsewhere, hidden tab): the audio stops, nothing re-keys;
 *      the panel closed on the air: released; busy / denied / no ready / a TX device error: nothing
 *      keyed; a claim that can't be sent: nothing keyed
 *  S5. never sends for a viewer, with no radio, a radio without PTT, a mode with no audio TX, someone
 *      else on the air, or this tab already transmitting; the controls are locked while sending;
 *      mode, text and level are kept per browser
 */
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { makeEnv } = require("./fakedom");
const ref = require("./sstv_encode");

let failures = 0, checks = 0;
function check(cond, msg) { checks++; if (!cond) { failures++; console.log("FAIL " + msg); } }
function eq(a, b, msg) { const ja = JSON.stringify(a), jb = JSON.stringify(b); check(ja === jb, msg + ": got " + ja + ", want " + jb); }

const FS = 16000;
// a picture as the canvas gives it: RGBA, from the reference test picture
function imageData(w, h) {
  const p = ref.pic(w, h), data = new Uint8ClampedArray(w * h * 4);
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) { const c = p(x, y), i = (y * w + x) * 4; data[i] = c[0]; data[i + 1] = c[1]; data[i + 2] = c[2]; data[i + 3] = 255; }
  return { width: w, height: h, data };
}

// ---- E1-E3: the encoder (no DOM) ----
const bare = { console, Math, Float32Array, Float64Array, Int32Array, Int16Array, Uint8ClampedArray, Uint8Array, Map, Set, Array, Object,
               Number, String, JSON, isNaN, Infinity };
bare.window = bare;
vm.createContext(bare);
for (const f of ["sstv.js", "sstv-tx.js"]) vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "frontend", f), "utf8"), bare, { filename: f });
const { MODES, Decoder } = bare.SSTV, TXM = bare.SstvTx;
{
  const offered = TXM.modes().map((m) => m.id);
  eq(offered, ["r36", "r72", "m1", "m2", "s1", "s2", "pd50", "pd90", "pd120", "pd160"],
     "E1 the modes offered: the ones that end 3 s inside the radios' longest keying (174 s on the air)");
  for (const m of MODES) {
    const s = TXM.onAirSeconds(m), want = 0.3 + 0.91 + m.durationS + 0.6;
    check(Math.abs(s - want) < 0.03, `E1 ${m.name}: ${s.toFixed(2)} s on the air (settle + VIS + ${m.durationS.toFixed(1)} s + tail)`);
    check(offered.includes(m.id) === (s <= 174), `E1 ${m.name} ${offered.includes(m.id) ? "offered" : "not offered"} at ${s.toFixed(1)} s`);
  }
  check(TXM.onAirSeconds(MODES.find((m) => m.id === "pd160")) < 174 && TXM.onAirSeconds(MODES.find((m) => m.id === "pd180")) > 177,
        "E1 PD-160 (2:43) is the longest offered; PD-180 (3:09) is out");
}
for (const m of TXM.modes()) {
  const img = imageData(m.width, m.height), out = TXM.encode(m, img, 0.5);
  const want = ref.audio(ref.vis(m.vis).concat(ref.picture(m, ref.pic(m.width, m.height))), { lead: 0.3, tail: 0.6 });
  let same = out.length === want.length;
  for (let i = 0; same && i < out.length; i++) if (out[i] !== want[i]) same = false;
  check(same, `E2 ${m.name}: sample for sample the reference encoder's audio (${out.length} samples, want ${want.length})`);
}
{
  const m = MODES.find((x) => x.id === "r36"), img = imageData(320, 240);
  const peak = (a) => { let p = 0; for (const v of a) p = Math.max(p, Math.abs(v)); return p; };
  const p25 = peak(TXM.encode(m, img, 0.25)), p100 = peak(TXM.encode(m, img, 1));
  check(Math.abs(p25 - 0.25 * 32767) < 40 && p100 <= 32767 && p100 > 32700, `E2 the level scales the audio, never past full scale: ${p25}, ${p100}`);
  const silent = TXM.encode(m, img, 0.5).subarray(0, 4800).every((v) => v === 0);
  check(silent, "E2 the first 0.3 s is silence: the radio settles on TX before the VIS");
}
function mae(rows, m, p) {
  let s = 0, n = 0;
  for (let y = 0; y < rows.length; y++) for (let x = 0; x < m.width; x++) { const w = p(x, y); for (let c = 0; c < 3; c++) { s += Math.abs(rows[y][x * 4 + c] - w[c]); n++; } }
  return n ? s / n : 999;
}
for (const id of ["r36", "m2", "s2", "pd50"]) {
  const m = MODES.find((x) => x.id === id), pcm = TXM.encode(m, imageData(m.width, m.height), 0.5);
  const r = { starts: [], rows: [] };
  const d = new Decoder(FS, { onStart: (s) => r.starts.push(s.mode.id), onRow: (x) => r.rows.push(Array.from(x.rgba)) });
  for (let i = 0; i < pcm.length; i += 320) d.feed(pcm.subarray(i, i + 320));
  const err = mae(r.rows, m, ref.pic(m.width, m.height));
  check(r.starts[0] === id && r.rows.length === m.height && err < (m.color === "GBR" ? 3 : 5),
        `E3 ${m.name}: decodes as ${r.starts[0]}, ${r.rows.length} rows, error ${err.toFixed(2)}`);
}

// ---- S1-S5: the Send panel ----
function page(opts) {
  opts = opts || {};
  const e = makeEnv({ localStorage: opts.localStorage || {} });
  const tile = e.el("div", { class: "grid-stack-item", "data-tile": "sstv" });
  e.tabRx = e.el("button", { id: "sstvTabRx", type: "button", class: "sstv-tab on" }, tile);
  e.tabTx = e.el("button", { id: "sstvTabTx", type: "button", class: "sstv-tab" }, tile);
  e.paneRx = e.el("div", { id: "sstvRx" }, tile);
  e.paneTx = e.el("div", { id: "sstvTx" }, tile); e.paneTx.hidden = true;
  e.mode = e.el("select", { id: "sstvTxMode" }, e.paneTx);
  e.file = e.el("input", { id: "sstvTxFile", type: "file" }, e.paneTx);
  e.card = e.el("button", { id: "sstvTxCard", type: "button" }, e.paneTx);
  e.text = e.el("input", { id: "sstvTxText", type: "text" }, e.paneTx);
  e.canvas = e.el("canvas", { id: "sstvTxCanvas", width: "320", height: "256" }, e.paneTx);
  e.level = e.el("input", { id: "sstvTxLevel", type: "range", min: "10", max: "100" }, e.paneTx); e.level.value = "50";
  e.levelVal = e.el("b", { id: "sstvTxLevelVal" }, e.paneTx);
  e.btn = e.el("button", { id: "sstvTxSend", type: "button" }, e.paneTx);
  e.statusEl = e.el("p", { id: "sstvTxStatus" }, e.paneTx);
  // a canvas that records what is drawn and gives back the reference picture at its size
  const g = { calls: [], fillStyle: "", font: "10px x", textAlign: "", textBaseline: "", lineWidth: 1, strokeStyle: "",
    fillRect(...a) { g.calls.push(["fillRect", ...a]); }, drawImage(s, ...a) { g.calls.push(["drawImage", ...a]); },
    createLinearGradient() { return { addColorStop() {} }; },
    measureText(t) { const px = +((/(\d+)px/.exec(g.font) || [0, 10])[1]); return { width: t.length * px * 0.62 }; },
    fillText(t, x, y) { g.calls.push(["fillText", t, x, y]); }, strokeText(t) { g.calls.push(["strokeText", t]); },
    getImageData(x, y, w, h) { g.calls.push(["getImageData", w, h]); return imageData(w, h); } };
  e.g = g;
  e.canvas.getContext = () => g;
  e.st = Object.assign({ connected: true, radio: "ft991a", mode_name: "USB", freq: 14230000 }, opts.state || {});
  e.prof = { id: "ft991a", capabilities: opts.noPtt ? { ptt: false } : {} };
  e.sent = []; e.own = false; e.pending = false; e.other = null; e.sendOk = true;
  e.ctx.RadioControl = {
    send: (o) => { e.sent.push(Object.assign({ t: e.clock.now }, o)); if (o.action === "ptt" && o.tx === true && e.sendOk) e.pending = true;
                   if (o.action === "ptt" && o.tx === false) { e.pending = false; e.own = false; } return e.sendOk; },
    state: () => e.st, radios: () => [e.prof] };
  e.ctx.RadioTx = { ownedPtt: () => e.own, ownedOrPending: () => e.own || e.pending,
                    owner: () => (e.own ? { user: "ME", claims: ["ptt"] } : e.other) };
  e.ctx.TR = { can: (p) => (p === "tx" ? !opts.viewer : true), srLive: () => {}, isTxOwner: () => e.own };
  e.pcm = []; e.pushes = []; e.begun = 0; e.ended = 0;
  e.ctx.RadioAudio = { txBegin: () => { e.begun++; return true; }, txEnd: () => { e.ended++; },
    txPush: (p) => { e.pcm.push(Int16Array.from(p)); e.pushes.push([e.clock.now, p.length]); return true; } };
  e.vis = true;
  e.ctx.RadioLayout = { visible: (id) => id === "sstv" && e.vis };
  e.load("sstv.js");
  e.load("sstv-tx.js");
  const ev = (type, detail) => { const x = new e.Event(type); x.detail = detail; e.doc.dispatchEvent(x); };
  Object.assign(e, {
    click: (el) => el.dispatchEvent(new e.Event("click", { bubbles: true })),
    change: (el) => el.dispatchEvent(new e.Event("change", { bubbles: true })),
    input: (el) => el.dispatchEvent(new e.Event("input", { bubbles: true })),
    adv: (ms) => e.clock.advance(ms),
    status: () => e.statusEl.textContent,
    ready: () => ev("tr:mic", { type: "mic", on: true, ready: true }),
    grant: () => { e.pending = false; e.own = true; ev("tr:tx", { owner: { user: "ME", claims: ["ptt"] } }); },
    lose: () => { e.own = false; e.pending = false; ev("tr:tx", { owner: null }); },
    busy: (owner) => { e.pending = false; ev("tr:busy", { type: "busy", action: "ptt", owner }); },
    denied: (reason) => { e.pending = false; ev("tr:denied", { type: "denied", action: "ptt", reason }); },
    error: (code) => ev("tr:error", { type: "error", code, message: "The pinned TX device is not present." }),
    claims: () => e.sent.filter((o) => o.action === "ptt" && o.tx === true),
    releases: () => e.sent.filter((o) => o.action === "ptt" && o.tx === false),
    samples: () => e.pcm.reduce((s, p) => s + p.length, 0),
    setMode: (id) => { e.mode.value = id; e.change(e.mode); },
  });
  return e;
}

// S1: the panel before anything is sent
{
  const e = page();
  const opts = e.mode.querySelectorAll("option").map((o) => o.textContent);
  eq(opts, ["Robot 36 · 0:38", "Robot 72 · 1:14", "Martin M1 · 1:57", "Martin M2 · 1:00", "Scottie S1 · 1:52", "Scottie S2 · 1:13",
            "PD-50 · 0:52", "PD-90 · 1:32", "PD-120 · 2:08", "PD-160 · 2:43"], "S1 the mode menu: each mode with its time on the air");
  eq(e.mode.value, "s1", "S1 Scottie S1 by default");
  check(e.btn.disabled && /Choose a picture/.test(e.status()), "S1 no picture: Send is off and the panel says what to do: " + e.status());
  e.click(e.tabTx);
  check(!e.paneTx.hidden && e.paneRx.hidden && e.tabTx.getAttribute("aria-selected") === "true", "S1 the Send tab shows the Send pane");
  e.text.value = "N0CALL"; e.input(e.text);
  e.click(e.card);
  check(!e.btn.disabled && /^Ready\. Watch the radio's ALC/.test(e.status()), "S1 the test card: ready to send: " + e.status());
  eq(e.btn.textContent, "Send · 1:52", "S1 the button gives the time on the air");
  check(e.g.calls.some((c) => c[0] === "fillText" && c[1] === "N0CALL"), "S1 the text goes on the picture");
  check(e.g.calls.some((c) => c[0] === "getImageData" && c[1] === 320 && c[2] === 256), "S1 the picture is taken at the mode's size");
  eq([e.begun, e.sent], [0, []], "S1 nothing asked of the radio before Send");
  e.setMode("r36");
  eq([e.canvas.width, e.canvas.height], [320, 240], "S1 another mode: the picture at its size");
  eq([e.ctx.localStorage.getItem("telerig.sstv.tx.mode"), e.ctx.localStorage.getItem("telerig.sstv.tx.text")], ["r36", "N0CALL"],
     "S5 mode and text are kept in this browser");
}
{
  const e = page({ localStorage: { "telerig.sstv.tx.mode": "m2", "telerig.sstv.tx.text": "W9XYZ", "telerig.sstv.tx.level": "35" } });
  eq([e.mode.value, e.text.value, e.level.value, e.levelVal.textContent], ["m2", "W9XYZ", "35", "35%"], "S5 a browser's kept choices come back");
}

// S2: one picture, start to finish (Robot 36)
{
  const e = page();
  e.setMode("r36"); e.click(e.card);
  e.click(e.btn);
  eq([e.begun, e.claims().length, e.samples()], [1, 0, 0], "S2 Send: the TX path first; no claim, no audio yet");
  eq(e.btn.textContent, "STOP", "S2 ... the button is STOP from the first step");
  check(/Opening the radio's TX audio/.test(e.status()), "S2 ... and the panel says what it waits for: " + e.status());
  check(e.mode.disabled && e.card.disabled && e.text.disabled && e.level.disabled, "S5 the controls are locked while sending");
  e.adv(300); e.ready();
  eq(e.claims().map((o) => [o.action, o.tx, o.auto, o.sstv]), [["ptt", true, undefined, "r36"]],
     "S2 the server's ready: ONE ptt claim, naming the mode (the server holds it for the picture's length)");
  e.adv(500);
  eq(e.samples(), 0, "S2 no audio before the grant");
  e.grant();
  const t0 = e.clock.now;
  let ahead = 0, peakBurst = 0, lastT = -1, inTick = 0;
  for (const [t, n] of e.pushes) { if (t !== lastT) { inTick = 0; lastT = t; } inTick += n; peakBurst = Math.max(peakBurst, inTick); }
  for (let step = 0; step < 50; step++) {
    e.adv(20);
    ahead = Math.max(ahead, e.samples() - Math.floor((e.clock.now - t0) * 16));
  }
  check(ahead <= 1920 && ahead >= 1920 - 1024, "S2 the audio runs in real time, at most 120 ms ahead: " + ahead + " samples ahead");
  check(/^On the air: Robot 36, line \d+ of 240, 0:3\d left\.$/.test(e.status()), "S2 the panel counts down: " + e.status());
  check(e.btn.classList.contains("on"), "S2 the button shows the transmission");
  e.adv(36300);                                                       // 37.3 s of the 37.8 s
  eq(e.releases().length, 0, "S2 not released before the picture has played out");
  e.adv(1000);
  const want = e.ctx.SstvTx.encode(e.ctx.SstvTx.modes().find((m) => m.id === "r36"), imageData(320, 240), 0.5);
  const got = new Int16Array(e.samples());
  let o = 0; for (const p of e.pcm) { got.set(p, o); o += p.length; }
  let same = got.length === want.length;
  for (let i = 0; same && i < got.length; i++) if (got[i] !== want[i]) same = false;
  check(same, `S2 every sample as encoded, in order (${got.length} of ${want.length})`);
  for (const [t, n] of e.pushes) { if (t !== lastT) { inTick = 0; lastT = t; } inTick += n; peakBurst = Math.max(peakBurst, inTick); }
  check(peakBurst <= 8000, "S2 never more than 0.5 s of audio in one tick (the server takes 64 KB/s): " + peakBurst);
  eq(e.releases().map((x) => [x.tx, x.auto]), [[false, undefined]], "S2 then ONE release");
  check(e.releases()[0].t - t0 >= (want.length / 16) + 100, "S2 ... after the last sample has played out (+0.1 s)");
  eq([e.ended, e.claims().length], [1, 1], "S2 the TX path given back; one claim in all");
  check(/^Sent Robot 36\.$/.test(e.status()) && e.btn.textContent === "Send · 0:38", "S2 the panel says it was sent: " + e.status());
  const n = e.samples();
  e.adv(1500);
  eq(e.samples(), n, "S2 nothing more after the release");
  // S3: a click right after starts nothing; later, a new one does
  e.click(e.btn);
  eq(e.begun, 1, "S3 a click right after a picture ends starts nothing");
  e.adv(3100);
  check(!e.btn.disabled, "S3 ... Send is back after 3 s");
  e.click(e.btn);
  eq(e.begun, 2, "S3 ... and a click then starts the next one");
}

// S3: STOP at each step
{
  const e = page();
  e.click(e.card); e.click(e.btn);
  e.click(e.btn);                                                     // STOP before the ready
  e.ready();
  eq([e.claims().length, e.releases().length, e.ended], [0, 0, 1], "S3 STOP before the TX path is ready: no claim ever, the path given back");
  check(/^Stopped\.$/.test(e.status()), "S3 ... the panel says so: " + e.status());
}
{
  const e = page();
  e.click(e.card); e.click(e.btn); e.ready();
  e.click(e.btn);                                                     // STOP while the claim is pending
  eq(e.releases().map((x) => [x.tx, x.auto]), [[false, true]], "S3 STOP while claiming: the pending claim released (auto)");
  e.grant();                                                          // a late grant
  e.adv(1000);
  eq(e.samples(), 0, "S3 ... a late grant gets no audio");
}
{
  const e = page();
  e.click(e.card); e.click(e.btn); e.ready(); e.grant();
  e.adv(5000);
  const n = e.samples();
  e.click(e.btn);                                                     // STOP on the air
  eq(e.releases().map((x) => [x.tx, x.auto]), [[false, undefined]], "S3 STOP on the air: released at once");
  e.adv(200);
  e.click(e.btn);                                                     // the second click of a double-click
  eq(e.begun, 1, "S3 the second click of a STOP double-click starts nothing");
  e.adv(3000);
  eq([e.samples(), e.ended], [n, 1], "S3 ... no more audio, the path given back");
}

// S4: lost, closed, refused
{
  const e = page();
  e.click(e.card); e.click(e.btn); e.ready(); e.grant();
  e.adv(4000);
  const n = e.samples();
  e.lose();                                                           // the 120 s limit, a STOP elsewhere, a hidden tab ...
  eq(e.ended, 1, "S4 ... stopped at that tx frame, not a tick later");
  e.adv(2000);
  eq([e.samples(), e.claims().length, e.ended], [n, 1, 1], "S4 the claim lost on the air: the audio stops, nothing re-keys");
  check(/^Stopped: the transmission ended/.test(e.status()), "S4 ... the panel says so: " + e.status());
}
{
  const e = page();
  e.click(e.card); e.click(e.btn); e.ready(); e.grant();
  e.adv(2000);
  e.own = false;                                                      // lost with no tx frame (a dropped socket)
  e.adv(40);
  const n = e.samples();
  e.adv(1000);
  eq([e.samples(), e.ended], [n, 1], "S4 ownership gone without a frame: the next tick stops");
}
{
  const e = page();
  e.setMode("r36"); e.click(e.card); e.click(e.btn); e.ready(); e.grant();
  e.adv(1000);
  const n = e.samples();
  e.clock.now += 2000;                                                // the page stalls for 2 s: no ticks; a browser
  for (const t of e.clock.tasks) if (t.at < e.clock.now) t.at = e.clock.now;   // then fires the timer once
  e.adv(0);
  check(e.samples() - n <= 1920 + 1024, "S2 after a 2 s stall the late audio is skipped, not sent at speed: " + (e.samples() - n));
  e.adv(1000);
  const sent = e.samples() - n;
  check(sent > 15000 && sent < 16000 + 1920 + 1024, "S2 ... then real time again: " + sent + " samples in about 1 s");
  e.adv(40000);
  eq(e.releases().map((x) => x.auto), [undefined], "S2 ... and the picture still ends on time, released once");
}
{
  const e = page({ state: { ptt_hold_max: 60 } });                    // a radio whose longest keying is shorter
  e.click(e.card);
  check(e.btn.disabled && /longer than the radio's TX limit/.test(e.status()), "S5 a mode longer than the radio's TX limit: Send is off: " + e.status());
  e.setMode("r36");
  check(!e.btn.disabled, "S5 ... a mode inside it can be sent");
}
{
  const e = page();                                                   // no ptt_hold_max: a voice over's 120 s
  e.click(e.card);
  e.setMode("m1");
  check(!e.btn.disabled, "S5 no ptt_hold_max: Martin M1 (1:57) fits 120 s");
  e.setMode("pd120");
  check(e.btn.disabled && /longer than the radio's TX limit/.test(e.status()), "S5 ... PD-120 (2:08) doesn't: " + e.status());
}
{
  // PD-160 on a radio with a 3-minute TOT (ptt_hold_max 177), start to finish: the claim names it, the
  // audio runs the whole 2:43 in real time, ONE release after the tail
  const e = page({ state: { ptt_hold_max: 177 } });
  e.click(e.card); e.setMode("pd160");
  check(!e.btn.disabled, "S5 PD-160 (2:43) fits a radio whose longest keying is 177 s: " + e.status());
  eq(e.btn.textContent, "Send · 2:43", "S2 PD-160: the button gives its time on the air");
  e.click(e.btn); e.adv(100); e.ready();
  eq(e.claims().map((o) => o.sstv), ["pd160"], "S2 PD-160: one claim, naming PD-160");
  e.grant();
  const total = Math.round(0.3 * FS) + Math.ceil(e.ctx.SstvTx.onAirSeconds(e.ctx.SSTV.byId("pd160")) * FS - 0.3 * FS - 0.6 * FS) + Math.round(0.6 * FS);
  e.adv(120000);
  eq([e.releases().length, e.btn.textContent], [0, "STOP"], "S2 PD-160: still sending at 2:00 (no voice-over limit in the page)");
  e.adv(42000);
  eq(e.releases().length, 0, "S2 PD-160: still sending at 2:42");
  e.adv(1500);
  eq(e.releases().map((x) => x.auto), [undefined], "S2 PD-160: released once, after its last line and the tail");
  check(Math.abs(e.samples() - total) <= 1, "S2 PD-160: every sample sent: " + e.samples() + " of " + total);
}
{
  const e = page();
  e.click(e.card); e.click(e.btn); e.ready(); e.grant();
  e.adv(2000);
  e.vis = false;                                                      // the panel removed from the grid
  e.adv(40);
  eq([e.releases().length, e.ended], [1, 1], "S4 the panel closed on the air: released");
}
for (const [name, fn, want] of [
  ["busy", (e) => e.busy("K9ABC"), /^Nothing sent: K9ABC is transmitting\.$/],
  ["busy (front panel)", (e) => e.busy("FRONT PANEL"), /^Nothing sent: the radio's front panel is transmitting\.$/],
  ["denied", (e) => e.denied("role"), /^Nothing sent: the server refused \(role\)\.$/],
]) {
  const e = page();
  e.click(e.card); e.click(e.btn); e.ready();
  fn(e);
  e.adv(1000);
  check(e.samples() === 0 && e.ended === 1 && want.test(e.status()), `S4 ${name}: nothing sent, the path given back: ${e.status()}`);
}
{
  const e = page();
  e.click(e.card); e.click(e.btn);
  e.adv(5100);                                                        // no ready
  eq([e.claims().length, e.ended], [0, 1], "S4 the TX path never opens: no claim, after 5 s");
  check(/did not open/.test(e.status()), "S4 ... the panel says so: " + e.status());
}
{
  const e = page();
  e.click(e.card); e.click(e.btn);
  e.error("tx_device_missing");
  e.ready();
  eq([e.claims().length, e.ended], [0, 1], "S4 a TX device error: no claim");
  check(/TX device is not present/.test(e.status()), "S4 ... the server's words: " + e.status());
}
{
  const e = page();
  e.click(e.card); e.click(e.btn); e.ready();
  e.adv(3600);                                                        // no answer to the claim
  eq([e.releases().map((x) => x.auto), e.samples(), e.ended], [[true], 0, 1], "S4 no answer to the claim: released (auto), nothing sent");
}
{
  const e = page();
  e.sendOk = false;                                                   // a hidden tab: app.js sends no claim
  e.click(e.card); e.click(e.btn); e.ready();
  eq([e.samples(), e.ended], [0, 1], "S4 a claim that could not be sent: nothing on the air");
  check(/could not be sent/.test(e.status()), "S4 ... says so: " + e.status());
}

// S5: never for a viewer, without a radio, without PTT, in a mode with no audio, someone else on the air
for (const [name, opts, setup, want] of [
  ["a viewer", { viewer: true }, null, /Only operators/],
  ["no radio", { state: { connected: false } }, null, /Connect a radio/],
  ["a radio without PTT", { noPtt: true }, null, /can't be keyed/],
  ["CW", { state: { mode_name: "CW" } }, null, /Set the radio to USB, LSB, FM or a data mode/],
  ["CW-USB", { state: { mode_name: "CW-USB" } }, null, /Set the radio/],
  ["RTTY", { state: { mode_name: "RTTY-LSB" } }, null, /Set the radio/],
  ["DV", { state: { mode_name: "DV" } }, null, /Set the radio/],
  ["C4FM", { state: { mode_name: "C4FM" } }, null, /Set the radio/],
  ["someone else on the air", {}, (e) => { e.other = { user: "K9ABC", claims: ["ptt"] }; }, /Busy: K9ABC is transmitting/],
  ["the front panel keyed", {}, (e) => { e.other = { external: true, claims: ["ptt"] }; }, /Busy: the radio's front panel/],
  ["this tab on the air (PTT)", {}, (e) => { e.own = true; }, /transmitting already/],
]) {
  const e = page(opts);
  if (setup) setup(e);
  e.click(e.card);
  e.adv(1100);
  e.click(e.btn);
  check(e.btn.disabled && want.test(e.status()) && e.begun === 0 && e.sent.length === 0, `S5 ${name}: Send is off, nothing sent: ${e.status()}`);
}
for (const mode of ["LSB", "FM", "AM", "DATA-USB", "DATA-LSB", "DATA-FM"]) {
  const e = page({ state: { mode_name: mode } });
  e.click(e.card);
  check(!e.btn.disabled, `S5 ${mode}: Send is available`);
}

console.log(failures ? `${failures} of ${checks} checks FAILED` : `ALL PASS (${checks} checks)`);
process.exit(failures ? 1 : 0);
