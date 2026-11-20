/* The RX volume (app.js VOL), tested on the REAL tr.js + app.js in a Node vm (tests/pttenv.js on
 * tests/fakedom.js) with a fake Web Audio context. No browser, no server, no socket, no radio: the RX
 * button only sends an rx_audio frame to the fake socket. Run by tests/t_c1.py (unit):
 *
 *     node tests/volume_test.js        -> exit 0 and "ALL PASS", or exit 1 naming each failure
 *
 *  V1. VOL is a dB scale with headroom: 0 = off, 1..100 = -36 .. +24 dB, 60 = 0 dB (the audio exactly as
 *      the radio sends it; the old slider's top). It starts at 0 dB and shows its value.
 *  V2. The graph: RX bus -> VOL -> limiter -> speakers. The tools' tap (RadioAudio.bus()) is before VOL.
 *  V3. Moving VOL sets the gain at once, shows the dB and is kept by this browser; a kept VOL comes
 *      back on the next load (and is the gain when RX audio starts). Junk kept values are ignored.
 *      The track's green fill (--p) always matches the thumb, a kept VOL included: every slider's
 *      first fill is painted before the kept VOL comes back (the slider sits in .sl, as on the page).
 *  P1. RadioAudio.tapPcm (the Recorder's MP3): a tap gets every audio frame's own samples and rate,
 *      before any resampling or VOL; removing it stops it; nothing while RX audio is off; a tap that
 *      throws neither stops the others nor the playback.
 */
"use strict";
const { pageEnv } = require("./pttenv");

let failures = 0, checks = 0;
function check(cond, msg) { checks++; if (!cond) { failures++; console.log("FAIL " + msg); } }
function eq(a, b, msg) { const ja = JSON.stringify(a), jb = JSON.stringify(b); check(ja === jb, msg + ": got " + ja + ", want " + jb); }
const near = (a, b) => Math.abs(a - b) < 1e-3 * Math.max(1, Math.abs(b));

function env(kept) {
  const made = [];
  function FakeCtx() {
    const node = (kind) => { const n = { kind, outs: [], connect(x) { n.outs.push(x); return x; }, disconnect() {} }; made.push(n); return n; };
    this.state = "running"; this.sampleRate = 48000; this.currentTime = 0;
    this.destination = node("speakers");
    this.createGain = () => { const g = node("gain"); g.gain = { value: 1 }; return g; };
    this.createDynamicsCompressor = () => {
      const c = node("limiter");
      for (const k of ["threshold", "knee", "ratio", "attack", "release"]) c[k] = { value: NaN };
      return c;
    };
    this.resume = () => {};
    this.played = 0;
    this.createBuffer = (ch, len, rate) => ({ duration: len / rate, getChannelData: () => ({ set() {} }) });
    this.createBufferSource = () => { const n = node("source"); n.start = () => { this.played++; }; return n; };
    FakeCtx.last = this;
  }
  const e = pageEnv(0, {
    localStorage: kept === undefined ? {} : { "telerig.vol": kept },
    before(e) {
      e.ctx.AudioContext = FakeCtx;
      const sl = e.el("label", { class: "sl" });           // as on the page: app.js paints every .sl slider's fill
      const vol = e.el("input", { id: "vol", type: "range", min: "0", max: "100", step: "1", value: "60" }, sl);
      vol.value = "60";                                     // the markup's default (attribute -> property)
      e.el("b", { id: "volVal", class: "sl-val" }).textContent = "0 dB";
      e.el("button", { id: "audioBtn" });
    },
  });
  e.made = made;
  e.played = () => (FakeCtx.last ? FakeCtx.last.played : 0);
  e.vol = e.doc.getElementById("vol");
  e.label = () => e.doc.getElementById("volVal").textContent;
  e.rxOn = () => e.doc.getElementById("audioBtn").onclick();
  e.move = (v) => { e.vol.value = String(v); e.vol.oninput({ target: e.vol }); };
  e.kept = () => e.ctx.localStorage.getItem("telerig.vol");
  e.fill = () => e.vol.style.getPropertyValue("--p");      // the green part of the track
  // one RX audio frame as the server sends it: 'A', 0, rate (LE), then int16 mono samples
  e.audio = (samples, rate) => {
    const buf = new ArrayBuffer(4 + 2 * samples.length), dv = new DataView(buf);
    dv.setUint8(0, 0x41); dv.setUint16(2, rate, true);
    samples.forEach((v, i) => dv.setInt16(4 + 2 * i, v, true));
    e.sock.onmessage({ data: buf });
  };
  return e;
}

// V1 + V2: the default and the graph
{
  const e = env();
  eq([e.vol.value, e.label(), e.vol.getAttribute("aria-valuetext"), e.fill()], ["60", "0 dB", "0 dB", "60%"],
     "V1 VOL starts at 0 dB, says so, and the fill reaches the thumb");
  e.rxOn();
  const speakers = e.made.find((n) => n.kind === "speakers");
  const lim = e.made.find((n) => n.kind === "limiter");
  const bus = e.ctx.RadioAudio.bus();
  check(bus && bus.outs.length === 1, "V2 the RX bus exists once RX audio is on");
  const volume = bus.outs[0];
  check(volume && volume.kind === "gain" && volume !== bus, "V2 the RX bus feeds VOL (the tools tap the bus, before VOL)");
  check(near(volume.gain.value, 1), "V2 VOL at 0 dB is a gain of 1: " + volume.gain.value);
  eq(volume.outs.map((n) => n.kind), ["limiter"], "V2 VOL feeds the limiter");
  eq(lim.outs.map((n) => n.kind), ["speakers"], "V2 the limiter feeds the speakers");
  eq([lim.threshold.value, lim.ratio.value], [-2, 20], "V2 the limiter only catches peaks near full scale");
  check(!speakers.outs.length && !bus.outs.includes(speakers), "V2 nothing skips VOL and the limiter");
  // V3: moving VOL
  for (const [v, gain, label] of [[80, Math.pow(10, 12 / 20), "+12 dB"], [100, Math.pow(10, 24 / 20), "+24 dB"],
                                  [30, Math.pow(10, -18 / 20), "−18 dB"], [0, 0, "off"], [61, Math.pow(10, 0.6 / 20), "+1 dB"]]) {
    e.move(v);
    check(near(volume.gain.value, gain), `V3 VOL ${v}: gain ${gain.toFixed(4)} (got ${volume.gain.value})`);
    eq([e.label(), e.vol.getAttribute("aria-valuetext"), e.kept(), e.fill()], [label, label, String(v), v + "%"],
       `V3 VOL ${v}: readout, kept and fill`);
  }
  eq(e.toasts, [], "no warnings along the way");
}
// V3: a kept VOL comes back, and is the gain when RX starts
{
  const e = env("85");
  eq([e.vol.value, e.label(), e.fill()], ["85", "+15 dB", "85%"], "V3 the kept VOL comes back at load, its fill with it");
  e.rxOn();
  check(near(e.ctx.RadioAudio.bus().outs[0].gain.value, Math.pow(10, 15 / 20)), "V3 ... and is the gain when RX audio starts");
}
{
  const e = env("0");                                       // kept off: the thumb at the left, and no fill
  eq([e.vol.value, e.label(), e.fill()], ["0", "off", "0%"], "V3 a kept VOL off comes back off, with no fill");
}
for (const junk of ["abc", "150", "-5", ""]) {
  const e = env(junk);
  eq([e.vol.value, e.label(), e.fill()], ["60", "0 dB", "60%"], `V3 a kept ${JSON.stringify(junk)} is ignored`);
}

// P1: the PCM tap
{
  const e = env();
  e.rxOn();
  const got = [], other = [];
  const untap = e.ctx.RadioAudio.tapPcm((pcm, rate) => got.push([Array.from(pcm), rate]));
  e.ctx.RadioAudio.tapPcm(() => { throw new Error("a broken tap"); });
  e.ctx.RadioAudio.tapPcm((pcm) => other.push(pcm.length));
  e.move(100);                                              // VOL +24 dB: the tap still sees the raw samples
  e.audio([1, -2, 300, -32768, 32767], 16000);
  eq(got, [[[1, -2, 300, -32768, 32767], 16000]], "P1 a tap gets the frame's own samples and rate, before VOL");
  eq(other, [5], "P1 a tap that throws doesn't stop the others");
  eq(e.played(), 1, "P1 ... nor the playback (the frame was scheduled to play)");
  untap();
  e.audio([5, 6], 16000);
  eq(got.length, 1, "P1 a removed tap gets nothing more");
  e.doc.getElementById("audioBtn").onclick();              // RX off
  e.audio([7, 8], 16000);
  eq(other, [5, 2], "P1 nothing reaches a tap while RX audio is off");
}

console.log(failures ? `${failures} of ${checks} checks FAILED` : `ALL PASS (${checks} checks)`);
process.exit(failures ? 1 : 0);
