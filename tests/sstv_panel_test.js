/* The SSTV panel (frontend/sstv.js's page side), tested on the REAL script in a Node vm on tests/fakedom.js,
 * with the real tr.js + account.js (TR.modal, the viewer's dialog), fed with SSTV audio from
 * tests/sstv_encode.js through a stand-in for app.js's PCM tap. No browser, no radio; nothing here can
 * transmit. Run by tests/t_c1.py (unit):
 *
 *     node tests/sstv_panel_test.js     -> exit 0 and "ALL PASS", or exit 1 naming each failure
 *
 *  Q1. RX audio off: the panel says to turn it on; on: it is listening (Auto = the VIS header)
 *  Q2. a Robot 36 picture through the tap: the canvas takes its size, every row is painted, the status
 *      counts the lines with the frequency, then the picture goes into the strip (a thumbnail named after
 *      the mode, frequency and UTC start, with its delete X), and the status says it was received
 *  Q3. the collapsed panel's summary while receiving and after
 *  Q4. off the grid (the panel removed) the audio is ignored
 *  Q5. the mode menu: Auto + every mode; a chosen mode is kept by this browser and named in the status
 *  Q6. Clear stops a picture and listens again; the strip keeps 12 pictures, freeing the older ones (and
 *      closing the viewer on one that leaves)
 *  Q7. a header with no picture after it shows nothing at all: no blank canvas, no "receiving", no dot
 *  Q8. a picture that ended early is kept only if a quarter of it arrived; the status says when it isn't
 *  Q9. the X deletes one picture (its PNG freed, the summary updated), the focus goes to its neighbour;
 *      the X that slides under the pointer ignores the second click of a double click
 *  Q10. clicking a picture opens the viewer: the picture, when it came, Export PNG (the PNG's own name),
 *      Delete; Esc closes it and the focus goes back; no viewer while this tab transmits; without the
 *      dialog kit a click saves the PNG
 */
"use strict";
const { makeEnv } = require("./fakedom");
const E = require("./sstv_encode");

let failures = 0, checks = 0;
function check(cond, msg) { checks++; if (!cond) { failures++; console.log("FAIL " + msg); } }
function eq(a, b, msg) { const ja = JSON.stringify(a), jb = JSON.stringify(b); check(ja === jb, msg + ": got " + ja + ", want " + jb); }

const START = Date.UTC(2026, 10, 19, 18, 30, 0);
const LISTENING = "Listening for an SSTV picture (its VIS header picks the mode).";
const R36_NAME = "Robot 36 · 14.230 MHz USB · 18:30Z";

function page(opts) {
  opts = opts || {};
  const e = makeEnv({ localStorage: opts.localStorage || {} });
  e.clock.now = START;
  e.el("div", { id: "modalRoot" });
  const grid = e.el("div", { id: "grid" }), park = e.el("div", { id: "tileParking" });
  const tile = e.el("div", { class: "grid-stack-item", "data-tile": "sstv" }, opts.parked ? park : grid);
  e.el("select", { id: "sstvMode" }, tile).appendChild(Object.assign(e.doc.createElement("option"), { value: "auto" }));
  e.doc.getElementById("sstvMode").value = "auto";
  e.el("button", { id: "sstvClear", type: "button" }, tile);
  e.el("p", { id: "sstvStatus" }, tile);
  const canvas = e.el("canvas", { id: "sstvCanvas" }, tile);
  e.el("div", { id: "sstvGallery", role: "list" }, tile);
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
  e.tx = false;
  e.ctx.RadioTx = { ownedOrPending: () => e.tx };
  e.load("tr.js");
  e.load("account.js");
  e.toasts = []; e.said = []; e.dots = []; e.saved = [];
  e.ctx.TR.toast = (m) => e.toasts.push(m);
  e.ctx.TR.srLive = (m) => e.said.push(m);
  e.doc.addEventListener("tr:digital", (ev) => e.dots.push(ev.detail && ev.detail.mode));
  e.doc.addEventListener("click", (ev) => { const t = ev.target; if (t && t.getAttribute && t.getAttribute("download")) e.saved.push(t.getAttribute("download")); });
  if (opts.noKit) delete e.ctx.TR.modal;
  e.load("sstv.js");
  e.canvas = canvas;
  e.status = () => e.doc.getElementById("sstvStatus").textContent;
  e.feed = (pcm) => { for (let i = 0; i < pcm.length; i += 320) for (const fn of e.taps) fn(pcm.subarray(i, i + 320), 16000); };
  e.items = () => e.doc.getElementById("sstvGallery").querySelectorAll(".sstv-item");
  e.thumb = (i) => e.items()[i].querySelector("button.sstv-pic");
  e.x = (i) => e.items()[i].querySelector("button.sstv-del");
  e.alts = () => e.items().map((it) => it.querySelector("img").alt);
  e.dialog = () => e.doc.querySelector(".tr-modal-overlay:not([hidden]) .tr-modal");
  e.key = (key) => e.doc.dispatchEvent(new e.Event("keydown", { key }));
  e.tick = () => e.clock.advance(1000);
  return e;
}
function robot(e) { return e.ctx.SSTV.byId("r36"); }
function pictureAudio(e, id, lines, opts) {          // a header and the first `lines` lines (all of them if omitted)
  const m = e.ctx.SSTV.byId(id), segs = E.picture(m, E.pic(m.width, m.height)), per = segs.length / m.height;
  return E.audio([...E.vis(m.vis), ...(lines === undefined ? segs : segs.slice(0, lines * per))], opts);
}
let seed = 777;
function noise(sec, rms) {
  const out = new Int16Array(Math.round(sec * 16000));
  for (let i = 0; i < out.length; i++) {
    let s = 0;
    for (let k = 0; k < 6; k++) { seed = (seed * 1103515245 + 12345) & 0x7fffffff; s += seed / 0x7fffffff; }
    out[i] = Math.max(-32767, Math.min(32767, Math.round((s - 3) * 1.4 * rms * 32767)));
  }
  return out;
}

(function main() {
  // Q1
  {
    const e = page({ rx: false });
    eq(e.status(), "", "Q1 RX off: nothing here (the window's own line says so)");
    e.rx = true; e.tick();
    eq(e.status(), LISTENING, "Q1 RX on: listening for a VIS header");
    eq(e.taps.length, 1, "Q1 it taps the radio's PCM once");
  }
  // Q2 + Q3 + Q10 (the viewer) + Q6 (the strip keeps 12)
  {
    const e = page();
    const pcm = pictureAudio(e, "r36");
    const half = Math.round(pcm.length * 0.45);
    e.feed(pcm.subarray(0, half));
    eq([e.canvas.width, e.canvas.height], [320, 240], "Q2 the canvas takes the mode's size");
    check(/^Receiving Robot 36 · line \d+ of 240 · 14\.230 MHz USB$/.test(e.status()), "Q2 the status counts the lines: " + e.status());
    check(/^Robot 36 \d+\/240$/.test(e.ctx.SstvPanel.summary()), "Q3 the summary while receiving: " + e.ctx.SstvPanel.summary());
    eq([e.dots, e.said[0]], [["sstv"], "SSTV picture starting, Robot 36"], "Q2 ... a dot on SSTV's key, and it is announced");
    e.feed(pcm.subarray(half));
    eq(e.painted.length, 240, "Q2 every row is painted");
    eq(e.painted.slice(0, 3), [0, 1, 2], "Q2 ... top down");
    eq(e.items().length, 1, "Q2 the picture goes into the strip");
    eq([e.items()[0].getAttribute("role"), e.thumb(0).getAttribute("title"), e.alts()[0]], ["listitem", R36_NAME, R36_NAME],
       "Q2 ... a thumbnail named after the mode, frequency and UTC start");
    eq([e.x(0).getAttribute("aria-label"), e.x(0).getAttribute("title")], ["Delete " + R36_NAME, "Delete"], "Q2 ... with its delete X");
    eq(e.status(), "Received Robot 36 (240 lines) at 18:30Z.", "Q2 the status says it was received");
    eq(e.ctx.SstvPanel.summary(), "1 picture", "Q3 the summary after");
    // Q10: the viewer
    e.thumb(0).focus();
    e.thumb(0).click();
    const d = e.dialog();
    check(!!d, "Q10 clicking the picture opens the viewer");
    if (d) {
      const title = e.doc.getElementById(d.getAttribute("aria-labelledby"));
      eq([d.getAttribute("role"), d.getAttribute("aria-modal"), title && title.textContent], ["dialog", "true", R36_NAME],
         "Q10 ... a modal dialog named after the picture");
      const img = d.querySelector("img.sstv-view-img"), exp = e.doc.getElementById("sstvViewExport");
      eq([img.src, img.alt], ["blob:sstv/1", R36_NAME], "Q10 ... the picture");
      eq(d.querySelector(".sstv-view-cap").textContent, "Received 2026-11-19 18:30:00 UTC · 320 × 240", "Q10 ... when it came, its size");
      eq([exp.textContent, exp.getAttribute("href"), exp.getAttribute("download")],
         ["Export PNG", "blob:sstv/1", "TeleRig_SSTV_Robot-36_14.230MHz_20261119-183000Z.png"], "Q10 Export PNG saves the PNG under its own name");
      check(e.doc.activeElement === exp, "Q10 ... and has the focus");
      e.key("Escape");
      eq([!!e.dialog(), e.doc.activeElement === e.thumb(0)], [false, true], "Q10 Esc closes it, the focus back on the picture");
    }
    e.tx = true;
    e.thumb(0).click();
    eq([!!e.dialog(), e.toasts], [false, ["Stop transmitting to open a picture."]], "Q10 no viewer while this tab transmits (it would cover STOP)");
    e.tx = false;
    // Q6: the strip keeps 12; the viewer on a picture that leaves closes
    e.thumb(0).click();
    check(!!e.dialog(), "Q6 the viewer is open on the first picture");
    for (let k = 0; k < 12; k++) e.feed(pcm);
    eq([e.items().length, e.urls.revoked.length], [12, 1], "Q6 the strip keeps 12 pictures, freeing the oldest");
    eq(e.urls.revoked, ["blob:sstv/1"], "Q6 ... the first one");
    eq(!!e.dialog(), false, "Q6 ... and the viewer showing it closes");
  }
  // Q4: off the grid
  {
    const e = page({ parked: true });
    e.feed(pictureAudio(e, "r36").subarray(0, 8 * 16000));
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
    e.feed(pictureAudio(e, "m2").subarray(0, 10 * 16000));
    check(/^Receiving Martin M2/.test(e.status()), "Q6 receiving: " + e.status());
    e.doc.getElementById("sstvClear").dispatchEvent(new e.Event("click", { bubbles: true }));
    eq(e.status(), LISTENING, "Q6 Clear: listening again");
    eq(e.items().length, 0, "Q6 ... a cleared picture is not kept");
  }
  // Q7: a header with nothing after it
  {
    const e = page();
    for (const after of [noise(6, 0.25), new Int16Array(6 * 16000)]) {
      e.feed(E.audio(E.vis(robot(e).vis), { tail: 0 }));
      e.feed(after);
    }
    e.tick();
    eq([e.painted.length, e.canvas.width, e.canvas.height, e.dots, e.said, e.items().length, e.ctx.SstvPanel.summary(), e.status()],
       [0, 320, 256, [], [], 0, "", LISTENING], "Q7 a Robot 36 header, then noise or silence: nothing shown, nothing kept");
  }
  // Q8: pictures that ended early
  {
    const e = page();
    e.feed(pictureAudio(e, "r36", 30, { tail: 2.5 }));
    check(/^Robot 36 ended early: the signal went away at line \d+ of 240\. Not kept: less than a quarter of it arrived\.$/.test(e.status()),
          "Q8 30 lines of 240: says so and why it isn't kept: " + e.status());
    eq(e.items().length, 0, "Q8 ... and the strip stays empty");
    e.feed(pictureAudio(e, "r36", 130, { tail: 2.5 }));
    check(/^Robot 36 ended early: the signal went away at line \d+ of 240\.$/.test(e.status()), "Q8 130 lines of 240: " + e.status());
    eq(e.alts(), [R36_NAME + " (part)"], "Q8 ... kept, as a part");
    e.thumb(0).click();
    check(/^Received 2026-11-19 18:30:\d\d UTC · 320 × 240 · \d+ of 240 lines$/.test(e.dialog().querySelector(".sstv-view-cap").textContent),
          "Q8 ... the viewer says how much of it came: " + e.dialog().querySelector(".sstv-view-cap").textContent);
  }
  // Q9: the delete X
  {
    const e = page();
    for (let k = 0; k < 3; k++) { e.feed(pictureAudio(e, "r36", 70, { tail: 2.5 })); e.clock.advance(60000); }
    eq(e.items().length, 3, "Q9 three pictures");
    const names = e.alts();
    e.x(1).focus();
    e.x(1).click();                                    // a keyboard press (no click count)
    eq([e.alts(), e.urls.revoked, e.ctx.SstvPanel.summary()], [[names[0], names[2]], ["blob:sstv/2"], "2 pictures"],
       "Q9 the X deletes that picture, frees its PNG, and the summary follows");
    check(e.doc.activeElement === e.thumb(1), "Q9 ... the focus goes to the next one");
    check(e.said.includes("Deleted " + names[1].replace(" (part)", "")), "Q9 ... and it is announced");
    e.x(0).dispatchEvent(new e.Event("click", { detail: 1 }));
    eq(e.alts(), [names[0], names[2]], "Q9 the X that slid under the pointer ignores a double click's second click");
    e.clock.advance(500);
    e.x(0).dispatchEvent(new e.Event("click", { detail: 1 }));
    eq(e.alts(), [names[2]], "Q9 ... a click after that deletes");
    e.thumb(0).click();
    e.doc.getElementById("sstvViewDelete").click();
    const gal = e.doc.getElementById("sstvGallery");
    eq([e.items().length, !!e.dialog(), e.doc.activeElement === gal, gal.getAttribute("tabindex")], [0, false, true, "-1"],
       "Q9 Delete in the viewer: gone, the viewer closed, the focus held by the empty strip");
    eq(e.ctx.SstvPanel.summary(), "", "Q9 ... no pictures left");
  }
  // Q10: no dialog kit (account.js missing): a click saves the PNG
  {
    const e = page({ noKit: true });
    e.feed(pictureAudio(e, "r36", 70, { tail: 2.5 }));
    e.thumb(0).click();
    eq([!!e.dialog(), e.saved], [false, ["TeleRig_SSTV_Robot-36_14.230MHz_20261119-183000Z.png"]], "Q10 without the dialog kit a click saves the PNG");
  }
  console.log(failures ? `${failures} of ${checks} checks FAILED` : `ALL PASS (${checks} checks)`);
  process.exit(failures ? 1 : 0);
})();
