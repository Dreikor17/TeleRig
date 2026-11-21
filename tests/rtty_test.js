/* RTTY receive (frontend/rtty.js): the REAL decoder in a Node vm, and the REAL tab on tests/fakedom.js. No
 * browser, no server, no radio. Run by tests/t_c1.py:
 *
 *     node tests/rtty_test.js        -> exit 0 and "ALL PASS", or exit 1 naming each failure
 *
 *  R1. made signals (a Baudot encoder below: start bit, 5 bits LSB first, 1.5 stop bits, continuous-phase
 *      AFSK): the text back exactly at 45.45 / 50 / 75 baud, 170 / 425 / 850 Hz, mark low and mark high,
 *      letters and figures (LTRS / FIGS, unshift on space), at +10 dB in 2500 Hz
 *  R2. weaker: at 0 dB most of it; mark and space the wrong way round: no copy until reversed
 *  R3. noise and silence print nothing (the squelch); a tone off by 100 Hz from the tuning prints nothing
 *  R4. the polarity the radio's mode implies: LSB, Icom RTTY, Yaesu RTTY-LSB normal; USB, data on USB,
 *      RTTY-R, RTTY-USB reversed
 *  R5. the tab: text as text, the Rev key and the speed / shift kept per browser, Clear, a click tunes to
 *      the nearest mark / space pair, decoding only while the window is on the grid
 */
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { makeEnv } = require("./fakedom");

let failures = 0, checks = 0;
function check(cond, msg) { checks++; if (!cond) { failures++; console.log("FAIL " + msg); } }
function eq(a, b, msg) { const ja = JSON.stringify(a), jb = JSON.stringify(b); check(ja === jb, msg + ": got " + ja + ", want " + jb); }

const bare = { console, Math, Float64Array, Float32Array, Int16Array, Uint8Array, Map, Set, Array, Object, Number, String, JSON };
bare.window = bare;
vm.createContext(bare);
vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "frontend", "rtty.js"), "utf8"), bare, { filename: "rtty.js" });
const R = bare.RTTY;

// ---- the test signal: Baudot, US-TTY figures, the sender unshifting on space like the decoder ----
function baudot(text) {
  const codes = [31, 31];                                // two LTRS to start, as stations do
  let figs = false;
  for (const ch of text.toUpperCase()) {
    if (ch === "\n") { codes.push(8, 2); continue; }      // CR LF
    if (ch === " ") { codes.push(4); figs = false; continue; }
    let k = R.LTRS.indexOf(ch);
    if (k > 0 && k !== 4) { if (figs) { codes.push(31); figs = false; } codes.push(k); continue; }
    k = R.FIGS.indexOf(ch);
    if (k > 0) { if (!figs) { codes.push(27); figs = true; } codes.push(k); continue; }
  }
  return codes;
}
function rng(seed) {
  let a = seed >>> 0;
  return () => { a = (a + 0x6D2B79F5) >>> 0; let t = a; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
}
function signal(text, o) {
  const rate = o.rate || 16000, baud = o.baud || 45.45, shift = o.shift || 170, center = o.center || 2210;
  const mark = o.markHigh ? center + shift / 2 : center - shift / 2, space = o.markHigh ? center - shift / 2 : center + shift / 2;
  const bits = [];
  for (let i = 0; i < 1.0 * baud; i++) bits.push([1, 1]);   // a second of idle mark
  for (const c of baudot(text)) {
    bits.push([0, 1]);
    for (let b = 0; b < 5; b++) bits.push([(c >> b) & 1, 1]);
    bits.push([1, 1.5]);
  }
  for (let i = 0; i < 0.5 * baud; i++) bits.push([1, 1]);
  const sigma = 0.05, nb = (sigma * sigma * 2500) / (rate / 2), amp = o.snr === undefined ? 0 : Math.sqrt(2 * nb * Math.pow(10, o.snr / 10));
  let n = 0;
  for (const [, len] of bits) n += len;
  const out = new Float32Array(Math.ceil((n / baud) * rate) + 1);
  const r = rng(o.seed || 1);
  let ph = 0, t = 0, i = 0;
  for (const [v, len] of bits) {
    const end = t + len / baud, w = (2 * Math.PI * (v ? mark : space)) / rate;
    for (; i / rate < end && i < out.length; i++) { out[i] = (o.tone === false ? 0 : amp) * Math.sin(ph); ph += w; }
    t = end;
  }
  if (!o.quiet) for (let k = 0; k < out.length; k += 2) {
    const u = Math.max(1e-12, r()), v = r(), m = sigma * Math.sqrt(-2 * Math.log(u));
    out[k] += m * Math.cos(2 * Math.PI * v); if (k + 1 < out.length) out[k + 1] += m * Math.sin(2 * Math.PI * v);
  }
  return out;
}
function copy(pcm, opts) {
  let out = "";
  const d = new R.Decoder(opts.rate || 16000, opts, (ch) => { out += ch; });
  for (let i = 0; i < pcm.length; i += 320) d.feed(pcm.subarray(i, i + 320));   // in 20 ms frames, as the radio's audio comes
  return out;
}
const MSG = "CQ CQ DE W9XYZ W9XYZ K";
const FIG = "RST 599 599 IN EN53 QTH 73 $5 #2 (OK) 1/2";

// ---- R1 ----
for (const [baud, shift] of [[45.45, 170], [50, 170], [75, 170], [50, 425], [50, 850]]) {
  for (const markHigh of [false, true]) {
    // over four noise draws: the text back exactly at least three times (a decoder can slip sync at the
    // very start of a transmission, as real ones do), and every word of it every time
    const got = [1, 2, 3, 4].map((seed) => copy(signal(MSG, { baud, shift, markHigh, snr: 10, seed }), { baud, shift, reverse: markHigh }).trim());
    check(got.filter((g) => g === MSG).length >= 3, `R1 ${baud} baud ${shift} Hz, mark ${markHigh ? "high" : "low"}: the text back: ${JSON.stringify(got)}`);
    check(got.every((g) => g.endsWith("DE W9XYZ W9XYZ K")), `R1 ${baud} baud ${shift} Hz, mark ${markHigh ? "high" : "low"}: the call every time`);
  }
}
eq(copy(signal(FIG, { snr: 10, seed: 5 }), {}).trim(), FIG, "R1 figures and letters (LTRS / FIGS, unshift on space)");
eq(copy(signal("LINE ONE\nLINE TWO", { snr: 12, seed: 6 }), {}).trim(), "LINE ONE\nLINE TWO", "R1 CR LF -> a new line");
eq(copy(signal(MSG, { snr: 10, center: 1500, seed: 7 }), { center: 1500 }).trim(), MSG, "R1 tones around 1500 Hz, tuned there");

// ---- R2 ----
{
  const long = "THE QUICK BROWN FOX JUMPS OVER THE LAZY DOG 0123456789 ";
  const got = copy(signal(long.repeat(3), { snr: 0, seed: 9 }), {});
  let same = 0;
  for (let i = 0; i < Math.min(got.length, long.length * 3); i++) if (got[i] === long.repeat(3)[i]) same++;
  check(same / (long.length * 3) > 0.6 || got.includes("QUICK BROWN"), `R2 0 dB: most of it copies (${got.slice(0, 60)}...)`);
  const wrong = copy(signal(MSG, { snr: 10, seed: 3 }), { reverse: true });
  check(!wrong.includes("W9XYZ"), "R2 mark and space the wrong way round: no copy (" + JSON.stringify(wrong.slice(0, 30)) + ")");
}

// ---- R3 ----
{
  const noise = copy(signal(MSG, { tone: false, seed: 11 }), {});
  check(noise.replace(/\s/g, "").length <= 2, "R3 noise alone prints (almost) nothing: " + JSON.stringify(noise));
  let quiet = "";
  const d = new R.Decoder(16000, {}, (ch) => { quiet += ch; });
  d.feed(new Float32Array(16000 * 5));
  eq(quiet, "", "R3 silence prints nothing");
  const off = copy(signal(MSG, { snr: 10, center: 2310, seed: 12 }), {});
  check(!off.includes("W9XYZ"), "R3 a signal 100 Hz off the tuning doesn't copy: " + JSON.stringify(off.slice(0, 30)));
}

// ---- R4 ----
eq(["LSB", "RTTY", "RTTY-LSB", "DATA-LSB", "FM"].map(R.polarityFor), [false, false, false, false, false], "R4 normal: LSB, RTTY, RTTY-LSB, DATA-LSB");
eq(["USB", "DATA-USB", "RTTY-R", "RTTY-USB"].map(R.polarityFor), [true, true, true, true], "R4 reversed: USB, DATA-USB, RTTY-R, RTTY-USB");

// ---- R5: the tab ----
function tab(opts) {
  opts = opts || {};
  const e = makeEnv({ localStorage: opts.localStorage || {} });
  const grid = e.el("div", { id: "grid" });
  const tile = e.el("div", { class: "grid-stack-item", "data-tile": "sstv" }, grid);
  const pane = e.el("div", { id: "dmPaneRtty" }, tile);
  e.strip = e.el("canvas", { id: "rttyStrip" }, pane);
  e.mode = e.el("select", { id: "rttyMode" }, pane);
  e.rev = e.el("button", { id: "rttyRev", type: "button" }, pane);
  e.clear = e.el("button", { id: "rttyClear", type: "button" }, pane);
  e.status = e.el("p", { id: "rttyStatus" }, pane);
  e.text = e.el("div", { id: "rttyText" }, pane);
  e.grid = grid; e.tile = tile;
  e.st = Object.assign({ connected: true, mode_name: "LSB" }, opts.state || {});
  e.ctx.RadioControl = { state: () => e.st };
  e.taps = [];
  e.on = !opts.rxOff;
  e.ctx.RadioAudio = { on: () => e.on, tapPcm: (fn) => { e.taps.push(fn); return () => {}; } };
  e.load("rtty.js");
  e.feedAll = (pcm) => { for (let i = 0; i < pcm.length; i += 320) for (const f of e.taps) f(Int16Array.from(pcm.subarray(i, i + 320), (v) => Math.max(-32767, Math.min(32767, Math.round(v * 32768)))), 16000); };
  return e;
}
{
  const e = tab();
  eq(e.taps.length, 1, "R5 the tab taps the radio's audio once");
  eq(e.mode.querySelectorAll("option").map((o) => o.textContent), R.PRESETS.map((p) => p.name), "R5 the speed / shift menu");
  check(/Mark 2125 Hz, space 2295 Hz/.test(e.status.textContent), "R5 the tones shown: " + e.status.textContent);
  e.feedAll(signal(MSG, { snr: 10, seed: 3 }));
  eq(e.text.textContent.trim(), MSG, "R5 the text, decoded in the tab");
  check(e.text.children.length === 0, "R5 ... as text, never markup");
  check(/^RTTY /.test(e.ctx.RttyPanel.summary()), "R5 the window's summary: " + e.ctx.RttyPanel.summary());
  e.click = (el) => el.dispatchEvent(new e.Event("click", { bubbles: true }));
  e.click(e.clear);
  eq(e.text.textContent, "", "R5 Clear empties the text");
  e.click(e.rev);
  eq([e.rev.getAttribute("aria-pressed"), e.ctx.localStorage.getItem("telerig.rtty.rev")], ["true", "1"], "R5 Rev: pressed, kept");
  check(/Mark 2295 Hz, space 2125 Hz \(reversed\)/.test(e.status.textContent), "R5 ... the tones swap: " + e.status.textContent);
  e.feedAll(signal(MSG, { snr: 10, seed: 4, markHigh: true }));
  eq(e.text.textContent.trim(), MSG, "R5 ... and a mark-high signal copies");
  e.st.mode_name = "USB";
  e.click(e.clear);
  e.feedAll(signal(MSG, { snr: 10, seed: 4, markHigh: false }));
  eq(e.text.textContent.trim(), MSG, "R5 USB flips the polarity; with Rev on too, mark is low again");
  e.mode.value = "75-170";
  e.mode.dispatchEvent(new e.Event("change", { bubbles: true }));
  eq(e.ctx.localStorage.getItem("telerig.rtty.mode"), "75-170", "R5 the speed / shift is kept per browser");
}
{
  const e = tab();
  // a spectrum with a pair at 1400 / 1570 Hz: a click at 1450 tunes to its centre, 1485
  const binHz = 15.625, db = new Float64Array(192).fill(-100);
  db[Math.round(1400 / binHz)] = -20; db[Math.round(1570 / binHz)] = -20;
  const c = e.ctx.RttyPanel.tune(1450, { db, binHz });
  check(Math.abs(c - 1485) <= 8, "R5 a click tunes to the nearest mark / space pair's centre: " + c);
  eq(e.ctx.localStorage.getItem("telerig.rtty.center"), String(c), "R5 ... kept per browser");
  e.feedAll(signal(MSG, { snr: 10, seed: 13, center: c }));
  eq(e.text.textContent.trim(), MSG, "R5 ... and copies there");
}
{
  const e = tab();
  e.grid.removeChild(e.tile);
  e.feedAll(signal(MSG, { snr: 10, seed: 3 }));
  eq(e.text.textContent, "", "R5 the window off the grid: nothing decoded");
  const f = tab({ rxOff: true });
  eq(f.status.textContent, "", "R5 RX off: nothing here (the window's own line says so)");
}

console.log(failures ? `\n${failures} of ${checks} checks FAILED` : `\nALL PASS (${checks} checks)`);
process.exit(failures ? 1 : 0);
