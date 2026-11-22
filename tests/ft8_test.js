/* FT8 receive (frontend/ft8-decode.js): the REAL decoder in a Node vm. No browser, no server, no radio.
 * Run by tests/t_c1.py:
 *
 *     node tests/ft8_test.js        -> exit 0 and "ALL PASS", or exit 1 naming each failure
 *
 *  F1. the FFT (TeleRig's mixed radix 2 / 3 / 5) equals a direct DFT, at the sizes the decoder uses
 *  F2. a real 15 s recording (tests/data/ft8, from ft8_lib's test set) against WSJT-X's own decodes of
 *      it: most of them found, nothing WSJT-X didn't decode, the same DT, frequency and about the SNR
 *  F3. made signals at 16 kHz (the radio's audio here), 12 and 48 kHz: every message back exactly, at
 *      its DT and frequency, its SNR about the one made
 *  F4. the message types: CQ, CQ DX / CQ nnn, reports and R-reports, RRR / RR73 / 73, /R and /P calls,
 *      free text, telemetry; a type 4 call with a hashed partner: <...> until the partner is heard,
 *      then <CALL> (the hash table lives on across slots, aged out, emptied by reset())
 *  F5. no false decodes: noise and silence decode nothing; a short or empty slot doesn't throw
 *  F6. as a worker: { id, rate, samples } in, { id, decodes } out; garbage gives { id, error } or nothing
 *  F7. a wider window (21 s from 3 s before the slot, t0 -3): messages 3 s early to 4 s late found at their
 *      DT from the slot's start; the neighbouring slots' messages, partly inside it, never; t0 through the
 *      worker
 */
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const enc = require("./ft8_encode");

let failures = 0, checks = 0;
function check(cond, msg) { checks++; if (!cond) { failures++; console.log("FAIL " + msg); } }
function eq(a, b, msg) { const ja = JSON.stringify(a), jb = JSON.stringify(b); check(ja === jb, msg + ": got " + ja + ", want " + jb); }

const SRC = fs.readFileSync(path.join(__dirname, "..", "frontend", "ft8-decode.js"), "utf8");
function load(extra) {
  const ctx = Object.assign({ console, Math, Float64Array, Float32Array, Uint8Array, Uint32Array, Int16Array, Map, Array, Object, String,
                              Number, BigInt, Error, JSON }, extra || {});
  vm.createContext(ctx);
  vm.runInContext(SRC, ctx, { filename: "ft8-decode.js" });
  return ctx;
}
const FT8 = load().FT8;

// ---- F1: the FFT ----
for (const n of [30, 1024, 3840, 5120]) {
  const fft = FT8.makeFft(n), x = new Float64Array(n);
  for (let i = 0; i < n; i++) x[i] = Math.sin(i * 0.37) + 0.5 * Math.cos(i * 1.91) + ((i * 7919) % 13) / 13;
  const [re, im] = fft(x);
  let err = 0;
  for (const k of [0, 1, 7, n >> 3, n >> 1, n - 1]) {
    let r = 0, s = 0;
    for (let i = 0; i < n; i++) { const a = (-2 * Math.PI * k * i) / n; r += x[i] * Math.cos(a); s += x[i] * Math.sin(a); }
    err = Math.max(err, Math.abs(r - re[k]), Math.abs(s - im[k]));
  }
  check(err < 1e-6 * n, `F1 FFT ${n}: equals the direct DFT (max error ${err.toExponential(1)})`);
}
let threw = false;
try { FT8.makeFft(7 * 64); } catch (_) { threw = true; }
check(threw, "F1 a size with a factor other than 2, 3, 5 is refused");

// ---- F2: a real recording against WSJT-X ----
function wav(file) {
  const b = fs.readFileSync(file);
  let p = 12, rate = 0, data = null;
  while (p + 8 <= b.length) {
    const id = b.toString("ascii", p, p + 4), len = b.readUInt32LE(p + 4);
    if (id === "fmt ") rate = b.readUInt32LE(p + 12);
    if (id === "data") data = b.subarray(p + 8, p + 8 + len);
    p += 8 + len + (len & 1);
  }
  const s = new Float32Array(data.length / 2);
  for (let i = 0; i < s.length; i++) s[i] = data.readInt16LE(2 * i) / 32768;
  return { rate, s };
}
{
  const dir = path.join(__dirname, "data", "ft8");
  const want = fs.readFileSync(path.join(dir, "191111_110615.txt"), "utf8").trim().split(/\r?\n/).map((l) => {
    const t = l.trim().split(/\s+/);
    return { snr: +t[1], dt: +t[2], freq: +t[3], text: l.slice(l.indexOf("~") + 1).trim().split(/\s{2,}/)[0] };
  });
  const { rate, s } = wav(path.join(dir, "191111_110615.wav"));
  eq([rate, s.length], [12000, 180000], "F2 the recording: 15 s at 12 kHz");
  FT8.reset();
  const t0 = Date.now(), got = FT8.decodeSlot(s, rate), ms = Date.now() - t0;
  const texts = new Set(want.map((w) => w.text));
  check(got.length >= 17, `F2 ${got.length} of WSJT-X's ${texts.size} different messages decoded (at least 17)`);
  check(got.every((d) => texts.has(d.text)), "F2 nothing WSJT-X didn't decode: " + got.filter((d) => !texts.has(d.text)).map((d) => d.text).join(" | "));
  eq(got.length, new Set(got.map((d) => d.text)).size, "F2 each message once");
  const errs = [];
  for (const d of got) {
    const w = want.filter((x) => x.text === d.text).sort((a, b) => Math.abs(a.freq - d.freq) - Math.abs(b.freq - d.freq))[0];
    if (!w) continue;
    check(Math.abs(d.dt - w.dt) <= 0.15, `F2 ${d.text}: DT ${d.dt} (WSJT-X ${w.dt})`);
    check(Math.abs(d.freq - w.freq) <= 3, `F2 ${d.text}: ${d.freq} Hz (WSJT-X ${w.freq})`);
    if (w.snr <= 5) errs.push(Math.abs(d.snr - w.snr));
  }
  const mean = errs.reduce((a, b) => a + b, 0) / errs.length;
  check(mean <= 4.5, `F2 the SNR: within ${mean.toFixed(1)} dB of WSJT-X's on average (weak and middle signals)`);
  check(got.some((d) => d.text === "<...> ON7EE JO10"), "F2 a hashed call nobody has been heard sending: <...>");
  check(got.some((d) => d.text === "ET3RFG/R IN3ADG -23"), "F2 a /R call and a report");
  check(got.some((d) => d.text === "VK4BLE OH8JK R-17"), "F2 an R-report");
  check(got.some((d) => d.text === "CQ JA OH1LWZ KP11"), "F2 a directed CQ (CQ JA)");
  console.log(`(F2 decoded ${got.length} messages in ${ms} ms)`);
}

// ---- F3: made signals at 16, 12 and 48 kHz ----
function slot(rate, sigs, seed) {
  const buf = new Float32Array(rate * 15);
  enc.noise(buf, rate, 0.05, seed);
  for (const g of sigs) enc.add(buf, rate, enc.tones(enc.pack(g.text)), { f0: g.f0, start: 0.5 + g.dt, snr: g.snr, sigma: 0.05 });
  return buf;
}
{
  const sigs = [
    { text: "CQ K1ABC FN42", f0: 1000, dt: 0.0, snr: -5 },
    { text: "W9XYZ K1ABC -12", f0: 1500, dt: 0.3, snr: -10 },
    { text: "K1ABC W9XYZ R-08", f0: 612.5, dt: -0.4, snr: -15 },
    { text: "W9XYZ K1ABC RR73", f0: 2300, dt: 1.2, snr: -18 },
  ];
  FT8.reset();
  const got = FT8.decodeSlot(slot(16000, sigs, 7), 16000);
  eq(got.map((d) => d.text).sort(), sigs.map((g) => g.text).sort(), "F3 16 kHz: all four messages, and nothing else");
  for (const g of sigs) {
    const d = got.find((x) => x.text === g.text);
    if (!d) continue;
    check(Math.abs(d.dt - g.dt) <= 0.1, `F3 ${g.text}: DT ${d.dt} (made ${g.dt})`);
    check(Math.abs(d.freq - g.f0) <= 3.2, `F3 ${g.text}: ${d.freq} Hz (made ${g.f0})`);
    check(Math.abs(d.snr - g.snr) <= 4, `F3 ${g.text}: ${d.snr} dB (made ${g.snr} dB in 2500 Hz)`);
  }
  for (const rate of [12000, 48000]) {
    FT8.reset();
    const r = FT8.decodeSlot(slot(rate, [{ text: "CQ DX K1ABC FN42", f0: 1234, dt: 0.2, snr: -12 }], 3), rate);
    eq(r.map((d) => [d.text, Math.abs(d.freq - 1234) <= 3.2, Math.abs(d.dt - 0.2) <= 0.1]), [["CQ DX K1ABC FN42", true, true]],
       `F3 ${rate / 1000} kHz: the same decoder`);
  }
  FT8.reset();
  const weak = FT8.decodeSlot(slot(16000, [{ text: "CQ K1ABC FN42", f0: 1000, dt: 0, snr: -30 }], 11), 16000);
  eq(weak.length, 0, "F3 far below the noise (-30 dB): nothing (and nothing made up)");
}

// ---- F4: the message types; the hash table ----
{
  const one = (text, seed) => FT8.decodeSlot(slot(16000, [{ text, f0: 1200, dt: 0.1, snr: -8 }], seed || 5), 16000).map((d) => d.text);
  FT8.reset();
  for (const t of ["CQ DX K1ABC FN42", "CQ 123 K1ABC FN42", "CQ POTA K1ABC FN42", "K1ABC W9XYZ EN37", "K1ABC W9XYZ +05", "K1ABC W9XYZ R+05",
                   "K1ABC W9XYZ RRR", "K1ABC W9XYZ 73", "K1ABC/R W9XYZ EN37", "PA3XYZ/P GM4ABC/P JO22", "QRZ W9XYZ EN37",
                   "TNX BOB 73 GL", "0123456789ABCDEF01"]) {
    eq(one(t), [t], "F4 " + t);
  }
  FT8.reset();
  const hashedFirst = FT8.decodeSlot(slot(16000, [{ text: "x", f0: 0, dt: 0, snr: -99 }].slice(1), 1), 16000);
  eq(hashedFirst, [], "F4 an empty slot first");
  const t4 = enc.packNonstd("W9XYZ", "PJ4/K1ABC", "73");
  const b4 = new Float32Array(16000 * 15);
  enc.noise(b4, 16000, 0.05, 21);
  enc.add(b4, 16000, enc.tones(t4), { f0: 800, start: 0.6, snr: -6, sigma: 0.05 });
  eq(FT8.decodeSlot(b4, 16000).map((d) => d.text), ["<...> PJ4/K1ABC 73"], "F4 type 4, the hashed partner not heard yet: <...>");
  eq(one("W9XYZ K1ABC FN42", 9), ["W9XYZ K1ABC FN42"], "F4 then W9XYZ is heard");
  eq(FT8.decodeSlot(b4, 16000).map((d) => d.text), ["<W9XYZ> PJ4/K1ABC 73"], "F4 ... and the same type 4 message names it: <W9XYZ>");
  const cq4 = new Float32Array(16000 * 15);
  enc.noise(cq4, 16000, 0.05, 22);
  enc.add(cq4, 16000, enc.tones(enc.pack("CQ PJ4/K1ABC")), { f0: 900, start: 0.5, snr: -6, sigma: 0.05 });
  eq(FT8.decodeSlot(cq4, 16000).map((d) => d.text), ["CQ PJ4/K1ABC"], "F4 a type 4 CQ (the call in plain)");
  for (let i = 0; i < 12; i++) FT8.decodeSlot(new Float32Array(16000 * 15), 16000);
  eq(FT8.decodeSlot(b4, 16000).map((d) => d.text), ["<...> PJ4/K1ABC 73"], "F4 11 slots later the partner has aged out: <...> again");
  one("W9XYZ K1ABC FN42", 9);
  FT8.reset();
  eq(FT8.decodeSlot(b4, 16000).map((d) => d.text), ["<...> PJ4/K1ABC 73"], "F4 reset() empties the hash table");
}

// ---- F5: no false decodes ----
{
  FT8.reset();
  let n = 0;
  for (let seed = 100; seed < 112; seed++) n += FT8.decodeSlot(enc.noise(new Float32Array(16000 * 15), 16000, 0.05, seed), 16000).length;
  eq(n, 0, "F5 12 slots of noise: no decode");
  eq(FT8.decodeSlot(new Float32Array(16000 * 15), 16000).length, 0, "F5 silence: no decode");
  let ok = true;
  try { FT8.decodeSlot(new Float32Array(0), 16000); FT8.decodeSlot(new Float32Array(16000 * 3), 16000); } catch (_) { ok = false; }
  check(ok, "F5 an empty or a 3 s slot doesn't throw");
  const short = slot(16000, [{ text: "CQ K1ABC FN42", f0: 1000, dt: 0, snr: -5 }], 4).subarray(0, 16000 * 14);
  eq(FT8.decodeSlot(short, 16000).map((d) => d.text), ["CQ K1ABC FN42"], "F5 14 s of a slot still decodes");
}

// ---- F6: as a worker ----
{
  const posted = [];
  const W = load({ importScripts: () => {}, postMessage: (m) => posted.push(m) });
  check(typeof W.onmessage === "function", "F6 a worker context gets an onmessage handler");
  const samples = slot(16000, [{ text: "CQ K1ABC FN42", f0: 1000, dt: 0, snr: -5 }], 8);
  W.onmessage({ data: { id: 7, rate: 16000, samples } });
  eq(posted.map((m) => [m.id, (m.decodes || []).map((d) => d.text)]), [[7, ["CQ K1ABC FN42"]]], "F6 { id, rate, samples } -> { id, decodes }");
  W.onmessage({ data: { id: 8, rate: 7, samples: new Float32Array(10) } });
  check(posted[1] && posted[1].id === 8 && typeof posted[1].error === "string", "F6 a rate it can't use -> { id, error }");
  W.onmessage({ data: null });
  check(posted.length === 3 && Array.isArray(posted[2].decodes) && posted[2].decodes.length === 0, "F6 an empty message -> no decodes");
  const page = load({ document: {} });
  check(page.onmessage === undefined, "F6 in a page (not a worker) no handler is installed");
}

// ---- F7: a window wider than the slot ----
{
  // 45 s of audio, three slots from 0, 15 and 30 s; the window for the middle one runs from 12 to 33 s
  const three = (sigs, seed) => {
    const buf = new Float32Array(16000 * 45);
    enc.noise(buf, 16000, 0.05, seed);
    for (const g of sigs) enc.add(buf, 16000, enc.tones(enc.pack(g.text)), { f0: g.f0, start: g.at, snr: g.snr, sigma: 0.05 });
    return buf.slice(16000 * 12, 16000 * 33);
  };
  FT8.reset();
  const win = three([{ text: "CQ K1ABC FN42", f0: 700, at: 15 + 0.5 - 3.0, snr: -8 },      // the middle slot's, 3 s early
                     { text: "W9XYZ K1ABC -12", f0: 1300, at: 15 + 0.5, snr: -8 },        // on time
                     { text: "K1ABC W9XYZ R-08", f0: 1900, at: 15 + 0.5 + 4.0, snr: -8 }], 51);   // 4 s late
  const got = FT8.decodeSlot(win, 16000, -3);
  eq(got.map((d) => d.text).sort(), ["CQ K1ABC FN42", "K1ABC W9XYZ R-08", "W9XYZ K1ABC -12"], "F7 3 s early, on time, 4 s late: all found");
  for (const [t, dt] of [["CQ K1ABC FN42", -3.0], ["W9XYZ K1ABC -12", 0], ["K1ABC W9XYZ R-08", 4.0]]) {
    const d = got.find((x) => x.text === t);
    check(d && Math.abs(d.dt - dt) <= 0.15, `F7 ${t}: DT ${d && d.dt} from the slot's start (made ${dt})`);
  }
  FT8.reset();
  const near = three([{ text: "CQ DX K1ABC FN42", f0: 800, at: 0.5, snr: -5 },              // the slot before: its last 1.1 s inside
                      { text: "CQ DX W9XYZ EN53", f0: 1600, at: 30.5, snr: -5 }], 52);      // the slot after: its first 2.5 s inside
  eq(FT8.decodeSlot(near, 16000, -3), [], "F7 the slots either side, partly inside the window: not decoded");
  FT8.reset();
  eq(FT8.decodeSlot(slot(16000, [{ text: "CQ K1ABC FN42", f0: 1000, dt: 0.2, snr: -5 }], 9), 16000, 0).map((d) => d.dt),
     [0.2], "F7 t0 0: the 15 s slot as before");
  const posted = [];
  const W = load({ importScripts: () => {}, postMessage: (m) => posted.push(m) });
  W.onmessage({ data: { id: 3, rate: 16000, samples: three([{ text: "CQ K1ABC FN42", f0: 700, at: 15 + 0.5 + 3.5, snr: -6 }], 53), t0: -3 } });
  eq(posted.map((m) => (m.decodes || []).map((d) => [d.text, Math.abs(d.dt - 3.5) <= 0.15])), [[["CQ K1ABC FN42", true]]],
     "F7 the worker takes t0: a message 3.5 s late, at its DT");
}

console.log(failures ? `\n${failures} of ${checks} checks FAILED` : `\nALL PASS (${checks} checks)`);
process.exit(failures ? 1 : 0);
