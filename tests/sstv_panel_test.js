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
 *  Q9. no X on the strip's pictures (2026-11-23: Delete is in the viewer); the viewer's Delete deletes one
 *      (its PNG freed, the summary updated), the focus goes to its neighbour
 *  Q10. clicking a picture opens the viewer: the picture, when it came, Export PNG (the PNG's own name),
 *      Delete; Esc closes it and the focus goes back; no viewer while this tab transmits; without the
 *      dialog kit a click saves the PNG
 *  Q11. the strip is kept by this browser (IndexedDB, a stand-in here): a picture received is stored with
 *      its PNG, under its file name (the same picture kept by another tab is one record); a reload brings
 *      the kept pictures back, newest first, under the ones received since; the X, and the 12-picture
 *      limit, take a picture out of the store too, also one received and deleted while the store was
 *      still opening; a browser with no store, or one that refuses to open it, keeps them for the page's
 *      life; records that are not pictures are left out
 *  Q12. the recording: a picture caught late goes into the strip as the recording has it (its lines in their
 *      places, the last at the bottom), once; one too weak for the live decoder is named under way, then
 *      found and kept when it ends, and not again; Look back (the strip's pictures left out): no other one,
 *      under way, nothing recorded yet, none in noise; a picture under a quarter long is not kept by the
 *      search either
 */
"use strict";
const { makeEnv } = require("./fakedom");
const E = require("./sstv_encode");

let failures = 0, checks = 0;
function check(cond, msg) { checks++; if (!cond) { failures++; console.log("FAIL " + msg); } }
function eq(a, b, msg) { const ja = JSON.stringify(a), jb = JSON.stringify(b); check(ja === jb, msg + ": got " + ja + ", want " + jb); }

const START = Date.UTC(2026, 10, 19, 18, 30, 0);
const LISTENING = "Listening for an SSTV picture: its header picks the mode, or its line timing if it is already under way.";
const R36_NAME = "Robot 36 · 14.230 MHz USB · 18:30Z";

// A stand-in for the browser's IndexedDB, as much of it as sstv.js uses: one database with one object
// store (its records under a key path), requests that answer a moment later (the fake clock). `db` is the
// browser's side, shared by the pages of one test: { recs: Map(key -> record), opens, fail, store, keyPath }.
function fakeIdb(e, db) {
  const later = (fn) => e.ctx.setTimeout(fn, 0);
  const answer = (result) => { const r = {}; later(() => { r.result = result; if (r.onsuccess) r.onsuccess({}); }); return r; };
  const os = (mode) => {
    const write = () => { if (mode !== "readwrite") throw new Error("a write in a read-only transaction"); };
    return {
      getAll: () => answer([...db.recs.values()].map((x) => Object.assign({}, x))),
      put: (rec) => {
        write();
        const k = rec[db.keyPath];
        if (typeof k !== "string" || !k) throw new Error("a record with no key");
        db.recs.set(k, Object.assign({}, rec));
        return answer(k);
      },
      delete: (k) => { write(); db.recs.delete(k); return answer(undefined); },
    };
  };
  return {
    open(name, version) {
      db.opens++; db.name = name; db.version = version;
      if (db.fail === "throw") throw new Error("no IndexedDB in this window");
      const req = {};
      later(() => {
        if (db.fail === "error") { if (req.onerror) req.onerror({}); return; }
        if (db.fail === "blocked") { if (req.onblocked) req.onblocked({}); return; }
        req.result = { createObjectStore: (n, o) => { db.store = n; db.keyPath = o.keyPath; db.auto = !!o.autoIncrement; },
                       transaction: (n, mode) => {
                         if (db.fail === "tx") throw new Error("the database is closing");
                         db.modes.push(mode);
                         return { objectStore: () => os(mode) };
                       } };
        if (!db.store && req.onupgradeneeded) req.onupgradeneeded({});
        if (req.onsuccess) req.onsuccess({});
      });
      return req;
    },
  };
}
const newDb = (o) => Object.assign({ recs: new Map(), opens: 0, fail: null, store: null, keyPath: "file", auto: false, modes: [] }, o || {});

function page(opts) {
  opts = opts || {};
  const e = makeEnv({ localStorage: opts.localStorage || {} });
  e.clock.now = opts.now || START;
  if (opts.idb) e.ctx.indexedDB = fakeIdb(e, opts.idb);     // this browser's store (Q11); without it, none
  e.el("div", { id: "modalRoot" });
  const grid = e.el("div", { id: "grid" }), park = e.el("div", { id: "tileParking" });
  const tile = e.el("div", { class: "grid-stack-item", "data-tile": "sstv" }, opts.parked ? park : grid);
  e.el("select", { id: "sstvMode" }, tile).appendChild(Object.assign(e.doc.createElement("option"), { value: "auto" }));
  e.doc.getElementById("sstvMode").value = "auto";
  e.el("button", { id: "sstvClear", type: "button" }, tile);
  e.el("button", { id: "sstvLook", type: "button" }, tile);
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
  FakeURL.createObjectURL = (b) => { if (b && b.bad) throw new TypeError("not a Blob"); return "blob:sstv/" + (++e.urls.made); };
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
  e.x = (i) => e.items()[i].querySelector("button.sstv-del");     // none: the strip has no X (Delete is in the viewer)
  e.del = (i) => { e.thumb(i).click(); e.doc.getElementById("sstvViewDelete").click(); };
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
    eq(e.x(0), null, "Q2 ... with no delete X on it (Delete is in its viewer)");
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
  // Q12: a picture already under way (no header heard): joined by its line timing, and said so
  {
    const e = page();
    const m = e.ctx.SSTV.byId("s1"), segs = E.picture(m, E.pic(m.width, m.height)), per = segs.length / m.height;
    e.feed(E.audio(segs.slice(Math.round(40 * per + per / 3), Math.round(120 * per)), { lead: 0.2 }));
    check(/^Receiving Scottie S1 \(joined under way\) · line \d+ of 256 · 14\.230 MHz USB$/.test(e.status()),
          "Q12 a picture joined under way: said in the status: " + e.status());
    eq(e.said[0], "SSTV picture starting, Scottie S1", "Q12 ... and announced");
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
  // Q9: Delete, in the viewer (the strip has no X of its own)
  {
    const e = page();
    for (let k = 0; k < 3; k++) { e.feed(pictureAudio(e, "r36", 70, { tail: 2.5 })); e.clock.advance(60000); }
    eq(e.items().length, 3, "Q9 three pictures");
    const names = e.alts();
    eq([0, 1, 2].map((i) => e.x(i)), [null, null, null], "Q9 no X on any picture");
    e.del(1);
    eq([e.alts(), e.urls.revoked, e.ctx.SstvPanel.summary()], [[names[0], names[2]], ["blob:sstv/2"], "2 pictures"],
       "Q9 the viewer's Delete deletes that picture, frees its PNG, and the summary follows");
    check(e.doc.activeElement === e.thumb(1), "Q9 ... the focus goes to the next one");
    check(e.said.includes("Deleted " + names[1].replace(" (part)", "")), "Q9 ... and it is announced");
    e.del(0);
    eq(e.alts(), [names[2]], "Q9 ... and another");
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
  // Q11: the strip is kept by this browser
  {
    const part = (e) => pictureAudio(e, "r36", 70, { tail: 2.5 });       // a picture that ended early, kept as a part
    const FILE = "TeleRig_SSTV_Robot-36_14.230MHz_20261119-183000Z.png", FILE_PART = FILE.replace("183000Z", "183100Z");
    const FILE_LATER = FILE.replace("183000Z", "193000Z");               // the one received an hour on
    const db = newDb();
    const e = page({ idb: db });
    e.clock.advance(1);
    eq([db.opens, db.name, db.version, db.store, db.keyPath, db.auto, e.items().length], [1, "telerig-sstv", 1, "pics", "file", false, 0],
       "Q11 the page opens this browser's store (made at the first visit: a picture under its file name); nothing kept yet");
    eq(db.modes, ["readonly"], "Q11 ... loading the strip only reads it (a write would hold up another tab's)");
    e.feed(pictureAudio(e, "r36"));
    eq([e.items().length, [...db.recs.keys()]], [1, [FILE]], "Q11 a picture received goes into the strip and into the store, under its file name");
    const rec = db.recs.get(FILE);
    eq([rec.name, rec.file, rec.at, rec.w, rec.h, rec.rows, rec.part, rec.blob && rec.blob.type, Object.keys(rec).length],
       [R36_NAME, FILE, START + 1, 320, 240, 240, false, "image/png", 8],
       "Q11 ... its record: the name, the PNG's file name, when it started, its size and lines, the PNG itself, nothing else");
    e.clock.advance(60000);
    e.feed(part(e));
    e.clock.advance(1);
    eq([e.alts(), [...db.recs.keys()]], [["Robot 36 · 14.230 MHz USB · 18:31Z (part)", R36_NAME], [FILE, FILE_PART]], "Q11 a second one, newest first; both kept");
    e.thumb(0).click();
    const partCap = e.dialog().querySelector(".sstv-view-cap").textContent;   // "... · N of 240 lines"
    e.key("Escape");

    // the page is loaded again (the same browser): the kept pictures come back
    const f = page({ idb: db, now: START + 3600000 });
    eq(f.items().length, 0, "Q11 a reload: before the store has answered, an empty strip");
    f.clock.advance(1);
    eq(f.alts(), ["Robot 36 · 14.230 MHz USB · 18:31Z (part)", R36_NAME], "Q11 ... then the kept pictures are back, newest first");
    eq([f.ctx.SstvPanel.summary(), f.urls.made, db.opens], ["2 pictures", 2, 2], "Q11 ... each from its stored PNG; the folded panel counts them");
    f.thumb(1).click();
    const d = f.dialog();
    check(!!d && d.querySelector(".sstv-view-cap").textContent === "Received 2026-11-19 18:30:00 UTC · 320 × 240"
          && f.doc.getElementById("sstvViewExport").getAttribute("download") === "TeleRig_SSTV_Robot-36_14.230MHz_20261119-183000Z.png",
          "Q11 ... with the time it was received and its PNG's own name, as before the reload");
    f.key("Escape");
    f.thumb(0).click();
    check(/· [1-9]\d* of 240 lines$/.test(partCap), "Q11 (the part's caption before the reload: " + partCap + ")");
    eq(f.dialog().querySelector(".sstv-view-cap").textContent, partCap, "Q11 ... a part still says when it came and how much of it");
    f.key("Escape");
    f.feed(pictureAudio(f, "r36"));
    eq(f.alts()[0], "Robot 36 · 14.230 MHz USB · 19:30Z", "Q11 a picture received after the reload goes on top");
    f.clock.advance(1);
    eq([...db.recs.keys()], [FILE, FILE_PART, FILE_LATER], "Q11 ... and into the store");
    // Delete (in the viewer) takes a picture out of the store too
    f.del(1);
    f.clock.advance(1);
    eq([f.alts(), [...db.recs.keys()]], [["Robot 36 · 14.230 MHz USB · 19:30Z", R36_NAME], [FILE, FILE_LATER]], "Q11 Delete takes it out of the strip and of the store");
    const g = page({ idb: db, now: START + 7200000 });
    g.clock.advance(1);
    eq(g.alts(), ["Robot 36 · 14.230 MHz USB · 19:30Z", R36_NAME], "Q11 ... so it stays deleted after the next reload");
    // the 12-picture limit: the oldest leave the store as they leave the strip
    for (let k = 0; k < 11; k++) { g.clock.advance(60000); g.feed(part(g)); }
    g.clock.advance(1);
    eq([g.items().length, db.recs.size, db.recs.has(FILE), db.recs.has(FILE_LATER)], [12, 12, false, true], "Q11 the strip keeps 12: the oldest left the store too");
    const h = page({ idb: db, now: START + 3 * 3600000 });
    h.clock.advance(1);
    eq([h.items().length, h.alts()[11]], [12, "Robot 36 · 14.230 MHz USB · 19:30Z"], "Q11 ... and a reload shows the same 12, the oldest last");
  }
  {
    const FILE = "TeleRig_SSTV_Robot-36_14.230MHz_20261119-183000Z.png";
    const png = { type: "image/png" };
    const old = { file: "old.png", at: START - 3600000, name: "Robot 36 · 7.171 MHz LSB · 17:30Z", w: 320, h: 240, rows: 240, part: false, blob: png };
    // a picture received and deleted while the store is still opening: put, then taken out, in that order
    const db = newDb();
    const e = page({ idb: db });
    e.feed(pictureAudio(e, "r36"));                          // before the first tick: the store is not open
    eq([e.items().length, db.recs.size], [1, 0], "Q11 received while the store was opening: in the strip, the store still to come");
    e.del(0);
    e.clock.advance(1);
    eq([e.items().length, db.recs.size], [0, 0], "Q11 ... and deleted before it opened: not kept");
    // a picture received while the store is still opening: on top of the kept ones
    const db2 = newDb();
    db2.recs.set(old.file, old);
    const f = page({ idb: db2 });
    f.feed(pictureAudio(f, "r36"));
    f.clock.advance(1);
    eq([f.alts(), [...db2.recs.keys()]], [[R36_NAME, old.name], ["old.png", FILE]], "Q11 received while the store was opening: kept, on top of the older ones");
    // two tabs of this browser receive the same picture: one record (its file name), gone for both when one deletes it
    const db4 = newDb();
    const a = page({ idb: db4 }), b = page({ idb: db4 });
    a.clock.advance(1); b.clock.advance(1);
    a.feed(pictureAudio(a, "r36")); b.feed(pictureAudio(b, "r36"));
    a.clock.advance(1); b.clock.advance(1);
    eq([[...db4.recs.keys()], a.items().length, b.items().length], [[FILE], 1, 1], "Q11 two tabs of this browser receive the same picture: one record");
    a.del(0);
    a.clock.advance(1);
    const c = page({ idb: db4 });
    c.clock.advance(1);
    eq([db4.recs.size, c.items().length], [0, 0], "Q11 ... deleted in one tab: out of the store, so neither has it after its next load");
    // records that are not pictures are left out, and the page still works
    const db3 = newDb();
    for (const r of [{ file: "a.png", at: START, name: "no PNG", w: 320, h: 240 }, { file: "b.png", at: 0, name: "no time", w: 320, h: 240, blob: png },
                     { file: "c.png", at: START, name: 7, w: 320, h: 240, blob: png }, { file: "e.png", at: START, name: "no width", w: 0, h: 240, blob: png },
                     { file: "g.png", at: START, name: "no height", w: 320, h: 0, blob: png },
                     { file: "h.png", at: START, name: "its PNG is none", w: 320, h: 240, rows: 240, part: false, blob: { bad: true } },
                     { file: "d.png", at: START, name: "A good one", w: 320, h: 256, rows: 256, part: false, blob: png },
                     { file: "i.png", at: START + 5, name: "A later one", w: 320, h: 256, rows: 256, part: false, blob: png }]) db3.recs.set(r.file, r);
    const g = page({ idb: db3 });
    g.clock.advance(1);
    eq(g.alts(), ["A later one", "A good one"], "Q11 records that are not pictures are left out; the rest, newest first");
  }
  for (const fail of ["throw", "error", "blocked", "tx"]) {
    // no store to be had: the strip lives as long as the page, and nothing breaks
    const db = newDb({ fail });
    const e = page({ idb: db });
    e.clock.advance(1);
    e.feed(pictureAudio(e, "r36"));
    e.clock.advance(1);
    eq([e.items().length, db.recs.size, e.status()], [1, 0, "Received Robot 36 (240 lines) at 18:30Z."], "Q11 the store can't be opened (" + fail + "): the picture is in the strip all the same");
    e.del(0);
    eq(e.items().length, 0, "Q11 ... and can be deleted");
  }
  // Q12: the recording (the last 5 minutes of audio): a picture caught late, one too weak to start, Look back
  {
    const late = (id, from, o) => {                       // a third into line `from`, no header
      const m = e0.ctx.SSTV.byId(id), segs = E.picture(m, E.pic(m.width, m.height)), per = segs.length / m.lines;
      return E.audio(segs.slice(Math.round(from * per + per / 3)), Object.assign({ lead: 1, tail: 3 }, o));
    };
    const e0 = page();
    // caught late: the live decoder joins it under way (its rows from the top); at its end the recording puts
    // every line it has in its place, the last at the bottom, in the strip instead
    e0.feed(late("r36", 96));
    eq([e0.status(), e0.alts()], ["Found Robot 36 in the recording: lines 98 to 240 of 240 (18:29Z).", [R36_NAME.replace("18:30Z", "18:29Z") + " (part)"]],
       "Q12 Robot 36 caught a third into line 97: the recording's copy, lines 98 to 240, in the strip (one picture)");
    check(e0.painted[e0.painted.length - 1] === 0 && e0.canvas.height === 240, "Q12 ... painted whole on the canvas");
    check(e0.dots.length >= 2, "Q12 ... a dot on SSTV's key");
    // Look back leaves the pictures in the strip out: nothing else here
    e0.doc.getElementById("sstvLook").click();
    check(/^No other SSTV picture in the last 0:\d\d of audio\.$/.test(e0.status()), "Q12 Look back, the one picture kept: " + e0.status());
    eq(e0.items().length, 1, "Q12 ... still one picture");
    // too weak to start: nothing live; the search while idle finds it once it has ended, and keeps it
    const e = page();
    const m = robot(e), weak = E.audio([...E.vis(m.vis), ...E.picture(m, E.pic(m.width, m.height))], { noise: 0.45, tail: 1 });
    e.feed(weak.subarray(0, 20 * 16000));
    eq([e.items().length, e.painted.length], [0, 0], "Q12 Robot 36 in heavy static: the live decoder shows nothing");
    check(/^Robot 36 under way in the static: it is decoded from the recording when it ends\.$/.test(e.status()), "Q12 ... named under way: " + e.status());
    e.doc.getElementById("sstvLook").click();
    check(/^Robot 36 under way in the static, so far lines \d+ to \d+ of 240: it is kept when it ends\.$/.test(e.status()), "Q12 Look back while it is under way: " + e.status());
    eq(e.items().length, 0, "Q12 ... not kept yet");
    e.feed(weak.subarray(20 * 16000));
    e.feed(noise(4, 0.45));
    check(/^Found Robot 36 in the recording: (all 240 lines|lines \d to 240 of 240) \(18:29Z\)\.$/.test(e.status()), "Q12 ... after it, found and kept: " + e.status());
    eq(e.items().length, 1, "Q12 ... one picture in the strip");
    e.feed(noise(25, 0.45));
    eq(e.items().length, 1, "Q12 ... and not again (the next search leaves it out)");
    // Look back: nothing recorded yet; noise only
    const f = page();
    f.doc.getElementById("sstvLook").click();
    eq(f.status(), "Nothing recorded yet: the recording runs while RX audio is on, the last 5 minutes.", "Q12 Look back before any audio");
    f.feed(noise(30, 0.3));
    f.doc.getElementById("sstvLook").click();
    eq([f.status(), f.items().length], ["No SSTV picture in the last 0:30 of audio.", 0], "Q12 Look back over 30 s of noise: none (nor while idle)");
    // a picture too short to keep (under a quarter) is not kept by the search either
    const g = page();
    g.feed(pictureAudio(g, "r36", 30, { tail: 25 }));
    eq(g.items().length, 0, "Q12 30 lines of 240: the search while idle doesn't keep what the live decoder wouldn't");
  }
  console.log(failures ? `${failures} of ${checks} checks FAILED` : `ALL PASS (${checks} checks)`);
  process.exit(failures ? 1 : 0);
})();
