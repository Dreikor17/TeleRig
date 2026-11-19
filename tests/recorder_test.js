/* The Recorder panel (frontend/recorder.js), tested on the REAL script in a Node vm on tests/fakedom.js
 * with a fake Web Audio graph and a fake MediaRecorder. No browser, no server, no socket, no radio:
 * window.RadioAudio / RadioControl are stubs, and RadioControl.send must never be called.
 * Run by tests/t_c1.py (unit):
 *
 *     node tests/recorder_test.js       -> exit 0 and "ALL PASS", or exit 1 naming each failure
 *
 *  M1. MP3, the normal path (a fake LAME): Record taps the radio's PCM (RadioAudio.tapPcm), no
 *      MediaRecorder; LAME gets mono, the radio's 16 kHz, 48 kb/s and every sample; Stop removes the tap
 *      and flushes; the file is an .mp3 holding every byte LAME gave, named like the others.
 *  M2. A dropout of over 1.5 s becomes silence in the MP3 (the file keeps time); jitter does not.
 *  M3. RX off while recording an MP3: it stops and is kept. No audio at all: nothing to keep, says so.
 *  The MediaRecorder cases below are the fallback, used only when the encoder is missing:
 *  R1. RX audio off: Record is disabled and the panel says why; no audio node is touched.
 *  R2. RX on: the level bar follows the RX bus (an analyser on the PRE-volume bus). Record starts a
 *      MediaRecorder on a stream destination fed by that bus (Opus, 64 kb/s, a chunk a second); the
 *      time runs; the button turns into Stop (aria-pressed).
 *  R3. Stop: one recording in the list, a download link named after the radio, frequency, mode and UTC
 *      start with the container's extension, its length and size; the tap comes off the bus.
 *  R4. Download marks it saved; Delete revokes its URL and removes it.
 *  R5. RX audio turned off while recording: the recording stops, is kept, and the panel says why.
 *  R6. The container: webm/opus, else ogg/opus, else mp4 (Safari); the extension follows the recorder's
 *      own type. No MediaRecorder at all: disabled, and the panel says the browser can't record.
 *  R7. Leaving the page asks first while recording and while a recording is not downloaded.
 *  R8. Never sends anything, never connects to the speakers, takes its taps off when done.
 */
"use strict";
const { makeEnv } = require("./fakedom");

let failures = 0, checks = 0;
function check(cond, msg) { checks++; if (!cond) { failures++; console.log("FAIL " + msg); } }
function eq(a, b, msg) { const ja = JSON.stringify(a), jb = JSON.stringify(b); check(ja === jb, msg + ": got " + ja + ", want " + jb); }

const START = Date.UTC(2026, 10, 18, 21, 6, 30);        // 2026-11-18 21:06:30 UTC

function pageEnv(opts) {
  opts = opts || {};
  const e = makeEnv();
  e.clock.now = START;
  const winL = {};
  e.ctx.addEventListener = (t, fn) => { (winL[t] = winL[t] || []).push(fn); };
  e.leave = () => { const ev = { prevented: false, returnValue: undefined, preventDefault() { this.prevented = true; } };
                    for (const fn of winL.beforeunload || []) fn(ev); return ev.prevented; };
  // the audio graph app.js builds: rx bus -> volume -> speakers
  const log = [];
  const node = (kind) => ({ kind, outs: [],
    connect(n) { this.outs.push(n); log.push("connect " + this.kind + ">" + n.kind); return n; },
    disconnect(n) { const i = this.outs.indexOf(n); if (i >= 0) this.outs.splice(i, 1); log.push("disconnect " + this.kind + ">" + (n && n.kind)); } });
  const A = { rx: false, level: 0, sent: [] };
  const actx = { state: "running", destination: node("speakers"),
    createMediaStreamDestination() { const d = node("stream"); d.stream = { id: "rx-stream" }; return d; },
    createAnalyser() {
      const a = node("analyser"); a.fftSize = 2048;
      a.getFloatTimeDomainData = (buf) => { for (let i = 0; i < buf.length; i++) buf[i] = (i % 2 ? -1 : 1) * A.level; };
      return a;
    } };
  const volume = node("volume"); volume.connect(actx.destination);
  const bus = node("rxbus"); bus.connect(volume);
  log.length = 0;
  e.ctx.RadioAudio = { ensure: () => {}, ctx: () => actx, bus: () => bus, state: () => ({}), on: () => A.rx };
  e.ctx.RadioControl = { send: (o) => A.sent.push(o), radios: () => [],
    state: () => ({ connected: true, radio: "ft991a", radio_name: "FT-991A", freq: 14074000, mode_name: "USB" }) };
  e.ctx.TR = { srLive: (m) => (e.said = e.said || []).push(m) };
  // the recorder: a fake MediaRecorder (a chunk per timeslice; stop delivers the last one, then stop)
  const made = [];
  if (opts.noRecorder !== true) {
    const supported = opts.types || ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"];
    function MR(stream, o) {
      this.stream = stream; this.opts = o || {}; this.state = "inactive";
      this.mimeType = this.opts.mimeType || opts.defaultType || "audio/webm;codecs=opus";
      made.push(this);
    }
    MR.isTypeSupported = (t) => supported.includes(t);
    MR.prototype.start = function (slice) {
      this.state = "recording"; this.slice = slice;
      const emit = () => { if (this.state !== "recording") return; this.ondataavailable({ data: { size: 8000 } }); this.t = e.clock.setTimeout(emit, slice); };
      this.t = e.clock.setTimeout(emit, slice);
    };
    MR.prototype.stop = function () {
      if (this.state === "inactive") throw new Error("InvalidStateError");
      this.state = "inactive"; e.clock.clear(this.t);
      e.clock.setTimeout(() => { this.ondataavailable({ data: { size: 3000 } }); if (this.onstop) this.onstop(); }, 0);
    };
    e.ctx.MediaRecorder = MR;
  }
  e.ctx.Blob = function (parts, o) { this.parts = parts; this.type = (o && o.type) || "";
    this.size = parts.reduce((s, p) => s + (p.size !== undefined ? p.size : p.length || 0), 0); };
  // the MP3 path: app.js's PCM tap and a fake LAME (it counts what it is given and returns 1 byte per 8 samples)
  e.taps = new Set();
  e.enc = [];
  if (opts.mp3) {
    e.ctx.RadioAudio.tapPcm = (fn) => { e.taps.add(fn); return () => { e.taps.delete(fn); }; };
    e.ctx.lamejs = { Mp3Encoder: function (ch, rate, kbps) {
      const me = { args: [ch, rate, kbps], samples: 0, zeros: 0, bytesOut: 0, flushed: false };
      e.enc.push(me);
      this.encodeBuffer = (pcm) => {
        const silent = pcm.every((v) => v === 0);
        if (silent) me.zeros += pcm.length; else me.samples += pcm.length;
        const out = new Int8Array(Math.floor(pcm.length / 8)); me.bytesOut += out.length; return out;
      };
      this.flush = () => { me.flushed = true; me.bytesOut += 100; return new Int8Array(100); };
    } };
  }
  e.pcm = (n, rate, amp) => { const pcm = new Int16Array(n).fill(amp); for (const fn of [...e.taps]) fn(pcm, rate); };
  const urls = { made: [], revoked: [] };
  class FakeURL extends URL {}
  FakeURL.createObjectURL = (b) => { const u = "blob:telerig/" + (urls.made.length + 1); urls.made.push([u, b]); return u; };
  FakeURL.revokeObjectURL = (u) => { urls.revoked.push(u); };
  e.ctx.URL = FakeURL;
  // the panel's markup (index.html)
  const btn = e.el("button", { id: "recBtn", type: "button", class: "key rec-btn", "aria-pressed": "false" });
  const svg = e.el("svg", { "aria-hidden": "true" }, btn); e.el("use", { href: "#i-tr-record" }, svg);
  e.el("span", { id: "recBtnLbl" }, btn).textContent = "Record";
  e.el("span", { id: "recTime" });
  const bar = e.el("div", { class: "mic-bar" }); e.el("div", { id: "recLevel", class: "mic-bar-fill" }, bar);
  e.el("p", { id: "recHint" });
  e.el("ul", { id: "recList" });
  e.load("recorder.js");
  Object.assign(e, { A, actx, bus, volume, log, made, urls, btn, $: (id) => e.doc.getElementById(id) });
  e.adv = (ms) => e.clock.advance(ms);
  e.click = (el) => el.dispatchEvent(new e.Event("click", { bubbles: true }));
  e.items = () => e.$("recList").querySelectorAll("li.rec-item");
  return e;
}

// M1-M3: MP3 through LAME from the radio's PCM (the normal path whenever the encoder loads)
{
  const e = pageEnv({ mp3: true });
  e.A.rx = true; e.A.level = 0.25;
  e.adv(200);
  check(!e.btn.disabled, "M1 Record is enabled with RX on and the encoder there");
  e.click(e.btn);
  eq([e.made.length, e.log.filter((l) => /stream/.test(l))], [0, []], "M1 no MediaRecorder and no stream tap");
  eq(e.taps.size, 1, "M1 it taps the radio's PCM");
  for (let i = 0; i < 100; i++) { e.adv(20); e.pcm(320, 16000, 1200); }        // 2 s in 20 ms chunks
  eq(e.enc.length, 1, "M1 one encoder");
  eq(e.enc[0].args, [1, 16000, 48], "M1 LAME: mono, the radio's own 16 kHz, 48 kb/s");
  eq([e.enc[0].samples, e.enc[0].zeros], [32000, 0], "M1 every sample encoded, no padding while audio flows");
  e.adv(60); e.pcm(320, 16000, 1200);                                           // 40 ms late: jitter
  eq(e.enc[0].zeros, 0, "M2 a little jitter adds no silence");
  e.adv(3000); e.pcm(320, 16000, 1200);                                         // a 3 s dropout
  check(e.enc[0].zeros > 47500 && e.enc[0].zeros < 49000, "M2 a 3 s dropout becomes ~3 s of silence (the file keeps time with the clock): " + e.enc[0].zeros);
  check(/^REC 0:0[45]$/.test(e.ctx.Recorder.summary()), "M1 the summary while recording: " + e.ctx.Recorder.summary());
  e.click(e.btn); e.adv(10);
  eq([e.taps.size, e.enc[0].flushed], [0, true], "M1 Stop removes the tap and flushes LAME");
  const li = e.items();
  eq(li.length, 1, "M1 one recording in the list");
  const a = li[0].querySelector("a.rec-save");
  eq(a.getAttribute("download") || a.download, "TeleRig_FT-991A_14.074MHz_USB_20261118-210630Z.mp3",
     "M1 an .mp3 named after the radio, frequency, mode and UTC start");
  const blob = e.urls.made[0][1];
  eq([blob.type, blob.size], ["audio/mpeg", e.enc[0].bytesOut], "M1 the file holds every MP3 byte, the flush too");
  check(/^21:06Z · 0:05 · /.test(li[0].querySelector(".rec-meta").textContent),
        "M1 the entry gives the audio's own length (5.06 s with the filled dropout): " + li[0].querySelector(".rec-meta").textContent);
  e.pcm(320, 16000, 1200);
  eq(e.enc[0].samples, 32640, "M1 nothing more is encoded after Stop");
  eq(e.A.sent, [], "M1 nothing was sent to the server");
  // M3: RX off while recording an MP3 keeps it; a recording with no audio at all keeps nothing
  e.click(e.btn); e.adv(20); e.pcm(320, 16000, 900); e.adv(500); e.pcm(320, 16000, 900);
  e.A.rx = false; e.adv(250);
  eq([e.items().length, e.taps.size], [2, 0], "M3 RX off stops the MP3 and keeps it");
  check(/RX audio was turned off/.test(e.$("recHint").textContent), "M3 the panel says why: " + e.$("recHint").textContent);
  e.A.rx = true; e.adv(200);
  e.click(e.btn); e.adv(2000); e.click(e.btn); e.adv(10);
  eq([e.items().length, e.taps.size], [2, 0], "M3 no audio arrived: nothing is added");
  check(/Nothing was recorded/.test(e.$("recHint").textContent), "M3 ... and the panel says so: " + e.$("recHint").textContent);
}

// M4: the entry gives the audio's own length, not the time between the clicks
{
  const e = pageEnv({ mp3: true });
  e.A.rx = true; e.adv(200);
  e.click(e.btn);
  for (let i = 0; i < 50; i++) { e.adv(20); e.pcm(320, 16000, 1000); }       // 1 s of audio
  e.adv(9000);                                                                // then nothing for 9 s
  e.click(e.btn); e.adv(10);
  const meta = e.items()[0].querySelector(".rec-meta").textContent;
  check(/ · 0:01 · /.test(meta), "M4 1 s of audio over 10 s of clock: the entry says 0:01: " + meta);
}

// R1-R4, R7, R8: one session
{
  const e = pageEnv();
  e.adv(250);
  check(e.btn.disabled, "R1 Record is disabled while RX audio is off");
  check(/Turn on RX/.test(e.$("recHint").textContent), "R1 the panel says to turn on RX: " + e.$("recHint").textContent);
  eq(e.log, [], "R1 no audio node touched while RX is off");
  check(!e.leave(), "R7 leaving the page asks nothing with no recordings");

  e.A.rx = true; e.A.level = 0.25;
  e.adv(200);
  check(!e.btn.disabled, "R2 Record is enabled once RX audio is on");
  eq(e.log, ["connect rxbus>analyser"], "R2 the level meter taps the pre-volume RX bus");
  eq(e.$("recLevel").style.width, "50%", "R2 the level bar follows the bus (peak 0.25 -> 50 %)");
  e.click(e.btn);
  eq(e.made.length, 1, "R2 one MediaRecorder");
  const r = e.made[0];
  eq([r.stream && r.stream.id, r.opts.mimeType, r.opts.audioBitsPerSecond, r.slice, r.state],
     ["rx-stream", "audio/webm;codecs=opus", 64000, 1000, "recording"], "R2 Opus/WebM at 64 kb/s, a chunk a second, from the RX stream");
  check(e.log.includes("connect rxbus>stream"), "R2 the recording is fed by the pre-volume RX bus: " + e.log.join(", "));
  check(!e.log.some((l) => /speakers|volume/.test(l)), "R8 nothing connects to the volume or the speakers: " + e.log.join(", "));
  eq([e.btn.getAttribute("aria-pressed"), e.$("recBtnLbl").textContent, e.btn.querySelector("use").getAttribute("href")],
     ["true", "Stop", "#i-square"], "R2 the button turns into Stop");
  check(e.leave(), "R7 leaving the page asks while recording");
  e.adv(83100);                                           // 83 chunks; the panel ticks every 100 ms
  eq(e.$("recTime").textContent, "1:23", "R2 the time runs on");
  e.click(e.btn);                                          // stop
  e.adv(10);
  eq(r.state, "inactive", "R3 the recorder stopped");
  const li = e.items();
  eq(li.length, 1, "R3 one recording in the list");
  const a = li[0].querySelector("a.rec-save");
  eq([a.getAttribute("download") || a.download, a.href || a.getAttribute("href")],
     ["TeleRig_FT-991A_14.074MHz_USB_20261118-210630Z.webm", "blob:telerig/1"], "R3 the download link: radio, freq, mode, UTC start, .webm");
  const blob = e.urls.made[0][1];
  eq([blob.type, blob.size], ["audio/webm;codecs=opus", 83 * 8000 + 3000], "R3 the file holds every chunk, the last one too");
  eq(li[0].querySelector(".rec-name").textContent, "14.074.000 USB", "R3 the entry names the frequency and mode");
  eq(li[0].querySelector(".rec-meta").textContent, "21:06Z · 1:23 · 651 KB", "R3 ... its UTC start, length and size");
  check(e.log.includes("disconnect rxbus>stream"), "R8 the recording's tap comes off the bus");
  eq([e.btn.getAttribute("aria-pressed"), e.$("recBtnLbl").textContent], ["false", "Record"], "R3 the button is Record again");
  check(/until you download/.test(e.$("recHint").textContent), "R4 the panel says recordings stay until downloaded: " + e.$("recHint").textContent);
  check(e.leave(), "R7 leaving the page asks while a recording is not downloaded");
  e.click(a);
  check(!e.leave(), "R4 a downloaded recording no longer holds the page");
  // a second recording, deleted
  e.click(e.btn); e.adv(2000); e.click(e.btn); e.adv(10);
  eq(e.items().length, 2, "R4 the newest recording first, two in the list");
  check(e.leave(), "R7 the new one is not downloaded yet");
  e.click(e.items()[0].querySelector("button.rec-del"));
  eq([e.items().length, e.urls.revoked], [1, ["blob:telerig/2"]], "R4 Delete removes it and frees its memory");
  check(!e.leave(), "R7 nothing left to lose");
  eq(e.A.sent, [], "R8 nothing was sent to the server");
  eq(e.ctx.Recorder.summary(), "1 recording", "the collapsed panel's summary");

  // R5: RX off while recording
  e.click(e.btn); e.adv(3000);
  check(/^REC 0:0[23]$/.test(e.ctx.Recorder.summary()), "the summary while recording: " + e.ctx.Recorder.summary());
  e.A.rx = false;
  e.adv(250);
  eq([e.made[e.made.length - 1].state, e.items().length], ["inactive", 2], "R5 RX off stops the recording and keeps it");
  check(/RX audio was turned off/.test(e.$("recHint").textContent), "R5 the panel says why: " + e.$("recHint").textContent);
  check(e.btn.disabled, "R5 Record waits for RX audio again");
  check(e.log.includes("disconnect rxbus>analyser"), "R8 the level meter's tap comes off with RX audio");
  eq(e.$("recLevel").style.width, "0%", "R5 the level bar drops to zero");
}

// R6: the container and its extension
for (const [types, def, want] of [
  [["audio/ogg;codecs=opus"], null, ["audio/ogg;codecs=opus", "ogg"]],
  [["audio/mp4"], null, ["audio/mp4", "m4a"]],
  [[], "audio/webm;codecs=opus", [undefined, "webm"]],
]) {
  const e = pageEnv({ types, defaultType: def });
  e.A.rx = true; e.adv(200);
  e.click(e.btn); e.adv(1500); e.click(e.btn); e.adv(10);
  const a = e.items()[0] && e.items()[0].querySelector("a.rec-save");
  eq([e.made[0].opts.mimeType, a && (a.getAttribute("download") || a.download || "").split(".").pop()], want,
     "R6 supported " + JSON.stringify(types) + ": the type asked for, the extension");
}
{
  const e = pageEnv({ noRecorder: true });
  e.A.rx = true; e.adv(200);
  check(e.btn.disabled && /can't record/.test(e.$("recHint").textContent), "R6 no MediaRecorder: disabled and says so: " + e.$("recHint").textContent);
  e.click(e.btn);
  eq([e.items().length, e.log.filter((l) => /stream/.test(l))], [0, []], "R6 ... and a click records nothing");
}

console.log(failures ? `${failures} of ${checks} checks FAILED` : `ALL PASS (${checks} checks)`);
process.exit(failures ? 1 : 0);
