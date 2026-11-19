/* The SSTV panel (frontend/sstv.js's page side), tested on the REAL script in a Node vm on tests/fakedom.js,
 * fed with SSTV audio from tests/sstv_encode.js through a stand-in for app.js's PCM tap. No browser, no
 * radio; nothing here can transmit. Run by tests/t_c1.py (unit):
 *
 *     node tests/sstv_panel_test.js     -> exit 0 and "ALL PASS", or exit 1 naming each failure
 *
 *  Q1. RX audio off: the panel says to turn it on; on: it is listening (Auto = the VIS header)
 *  Q2. a Robot 36 picture through the tap: the canvas takes its size, every row is painted, the status
 *      counts the lines with the frequency, then the picture goes into the strip as a PNG named after the
 *      mode, frequency and UTC start, and the status says it was received
 *  Q3. the collapsed panel's summary while receiving and after
 *  Q4. off the grid (the panel removed) the audio is ignored
 *  Q5. the mode menu: Auto + every mode; a chosen mode is kept by this browser and named in the status
 *  Q6. Clear stops a picture and listens again; the strip keeps 12 pictures, freeing the older ones
 */
"use strict";
const { makeEnv } = require("./fakedom");
const E = require("./sstv_encode");

let failures = 0, checks = 0;
function check(cond, msg) { checks++; if (!cond) { failures++; console.log("FAIL " + msg); } }
function eq(a, b, msg) { const ja = JSON.stringify(a), jb = JSON.stringify(b); check(ja === jb, msg + ": got " + ja + ", want " + jb); }

const START = Date.UTC(2026, 10, 19, 18, 30, 0);

function page(opts) {
  opts = opts || {};
  const e = makeEnv({ localStorage: opts.localStorage || {} });
  e.clock.now = START;
  const grid = e.el("div", { id: "grid" }), park = e.el("div", { id: "tileParking" });
  const tile = e.el("div", { class: "grid-stack-item", "data-tile": "sstv" }, opts.parked ? park : grid);
  e.el("select", { id: "sstvMode" }, tile).appendChild(Object.assign(e.doc.createElement("option"), { value: "auto" }));
  e.doc.getElementById("sstvMode").value = "auto";
  e.el("button", { id: "sstvClear", type: "button" }, tile);
  e.el("p", { id: "sstvStatus" }, tile);
  const canvas = e.el("canvas", { id: "sstvCanvas" }, tile);
  e.el("div", { id: "sstvGallery" }, tile);
  // a 2D context that records, and a canvas that makes "PNGs"
  e.painted = [];
  e.ctx2d = { fillRect() {}, putImageData: (img, x, y) => e.painted.push(y), set fillStyle(v) {} };
  canvas.getContext = () => e.ctx2d;
  canvas.toBlob = (cb) => cb({ size: canvas.width * canvas.height, type: "image/png" });
  e.ctx.ImageData = function (data, w, h) { this.data = data; this.width = w; this.height = h; };
  e.urls = { made: 0, revoked: [] };
  class FakeURL extends URL {}
  FakeURL.createObjectURL = () => "blob:sstv/" + (++e.urls.made);
  FakeURL.revokeObjectURL = (u) => e.urls.revoked.push(u);
  e.ctx.URL = FakeURL;
  e.rx = opts.rx !== false;
  e.taps = [];
  e.ctx.RadioAudio = { on: () => e.rx, tapPcm: (fn) => { e.taps.push(fn); return () => {}; } };
  e.ctx.RadioControl = { state: () => ({ connected: true, freq: 14230000, mode_name: "USB" }), send: () => { throw new Error("no TX here"); } };
  e.ctx.TR = { srLive: () => {} };
  e.load("sstv.js");
  e.canvas = canvas;
  e.status = () => e.doc.getElementById("sstvStatus").textContent;
  e.feed = (pcm) => { for (let i = 0; i < pcm.length; i += 320) for (const fn of e.taps) fn(pcm.subarray(i, i + 320), 16000); };
  e.pics = () => e.doc.getElementById("sstvGallery").querySelectorAll("a.sstv-pic");
  e.tick = () => e.clock.advance(1000);
  return e;
}
(function main() {
  // Q1
  {
    const e = page({ rx: false });
    eq(e.status(), "Turn on RX in the Audio panel to receive SSTV.", "Q1 RX off: says so");
    e.rx = true; e.tick();
    eq(e.status(), "Listening for an SSTV picture (its VIS header picks the mode).", "Q1 RX on: listening for a VIS header");
    eq(e.taps.length, 1, "Q1 it taps the radio's PCM once");
  }
  // Q2 + Q3 + Q6 (strip)
  {
    const e = page();
    const m = e.ctx.SSTV.byId("r36"), img = E.pic(m.width, m.height);
    const pcm = E.audio([...E.vis(m.vis), ...E.picture(m, img)]);
    const half = Math.round(pcm.length * 0.45);
    e.feed(pcm.subarray(0, half));
    eq([e.canvas.width, e.canvas.height], [320, 240], "Q2 the canvas takes the mode's size");
    check(/^Receiving Robot 36 · line \d+ of 240 · 14\.230 MHz USB$/.test(e.status()), "Q2 the status counts the lines: " + e.status());
    check(/^Robot 36 \d+\/240$/.test(e.ctx.SstvPanel.summary()), "Q3 the summary while receiving: " + e.ctx.SstvPanel.summary());
    e.feed(pcm.subarray(half));
    eq(e.painted.length, 240, "Q2 every row is painted");
    eq(e.painted.slice(0, 3), [0, 1, 2], "Q2 ... top down");
    const pics = e.pics();
    eq(pics.length, 1, "Q2 the picture goes into the strip");
    eq(pics[0].download || pics[0].getAttribute("download"), "TeleRig_SSTV_Robot-36_14.230MHz_20261119-183000Z.png",
       "Q2 ... as a PNG named after the mode, frequency and UTC start");
    eq(e.status(), "Received Robot 36 (240 lines) at 18:30Z.", "Q2 the status says it was received");
    eq(e.ctx.SstvPanel.summary(), "1 picture", "Q3 the summary after");
    // Q6: the strip keeps 12
    for (let k = 0; k < 12; k++) e.feed(pcm);
    eq([e.pics().length, e.urls.revoked.length], [12, 1], "Q6 the strip keeps 12 pictures, freeing the oldest");
    eq(e.urls.revoked, ["blob:sstv/1"], "Q6 ... the first one");
  }
  // Q4: off the grid
  {
    const e = page({ parked: true });
    const m = e.ctx.SSTV.byId("r36");
    e.feed(E.audio([...E.vis(m.vis), ...E.picture(m, E.pic(m.width, m.height))]).subarray(0, 8 * 16000));
    eq([e.painted.length, e.ctx.SstvPanel.summary()], [0, ""], "Q4 the panel off the grid: the audio is ignored");
  }
  // Q5: the mode menu
  {
    const e = page();
    const sel = e.doc.getElementById("sstvMode");
    const opts = sel.querySelectorAll("option").map((o) => o.value);
    eq(opts, ["auto"].concat(e.ctx.SSTV.MODES.map((m) => m.id)), "Q5 Auto and every mode");
    sel.value = "m2"; sel.dispatchEvent(new e.Event("change", { bubbles: true }));
    eq(e.ctx.localStorage.getItem("telerig.sstv.mode"), "m2", "Q5 a chosen mode is kept by this browser");
    eq(e.status(), "Listening for Martin M2: it starts at the next sync pulse.", "Q5 ... and named in the status");
    const e2 = page({ localStorage: { "telerig.sstv.mode": "m2" } });
    eq(e2.doc.getElementById("sstvMode").value, "m2", "Q5 ... and comes back on the next visit");
    const e3 = page({ localStorage: { "telerig.sstv.mode": "nonsense" } });
    eq(e3.doc.getElementById("sstvMode").value, "auto", "Q5 a junk kept mode means Auto");
  }
  // Q6: Clear
  {
    const e = page();
    const m = e.ctx.SSTV.byId("m2");
    e.feed(E.audio([...E.vis(m.vis), ...E.picture(m, E.pic(m.width, m.height))]).subarray(0, 10 * 16000));
    check(/^Receiving Martin M2/.test(e.status()), "Q6 receiving: " + e.status());
    e.doc.getElementById("sstvClear").dispatchEvent(new e.Event("click", { bubbles: true }));
    eq(e.status(), "Listening for an SSTV picture (its VIS header picks the mode).", "Q6 Clear: listening again");
    eq(e.pics().length, 0, "Q6 ... a cleared picture is not kept");
  }
  console.log(failures ? `${failures} of ${checks} checks FAILED` : `ALL PASS (${checks} checks)`);
  process.exit(failures ? 1 : 0);
})();
