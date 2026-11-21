/* The Modem window (frontend/digital.js) and its FT8 tab (frontend/ft8.js), the REAL scripts on
 * tests/fakedom.js with a fake clock; the REAL decoder (ft8-decode.js) for one slot end to end. No browser, no
 * server, no socket, no radio. Run by tests/t_c1.py:
 *
 *     node tests/digital_test.js        -> exit 0 and "ALL PASS", or exit 1 naming each failure
 *
 *  D1. the mode keys: CW, RTTY, FT8, SSTV; CW first, one pane shown, a click or the arrow keys switch,
 *      the choice kept (a choice kept from before CW joined comes back too)
 *  D2. a hidden mode with news gets a dot; showing it clears the dot
 *  D3. while SSTV sends a picture, or CW a message, the window stays on that mode and the others can't be
 *      shown (its STOP stays in view)
 *  D4. the collapsed window's summary is the shown mode's; DigitalPanel.feed reaches RTTY, FT8 and SSTV
 *  D5. tr:modem (a mode come into view: CW starts its decoder then): the page opening on a mode with the
 *      window on the grid; a mode chosen (not the one shown again; not the 1 s redraw); the window held on
 *      SSTV while it sends; the window brought onto the grid (added, or a layout that places it); none
 *      off the grid, none for folding and opening the window, none for a layout with it on the grid all along
 *  D6. the text screens (CW's chat, RTTY's text, FT8's list) keep their place: at the end when hidden,
 *      shown again at the new end; scrolled back, shown again where they were left (the browser's reset of
 *      a hidden box ignored); a resize keeps either
 *  T1. FT8 slots: one decode per 15 s UTC slot, once all of it has arrived, 15 s of samples from the slot's
 *      start (the arrival clock), in a worker loaded from the page's own script
 *  T2. the list: a header per slot, rows by frequency (dB, DT, Hz, message), CQs marked, text never markup;
 *      CQ only (kept); Clear; at most 400 rows, the oldest slot dropped first
 *  T3. a slot heard for under 10 s is skipped; a gap of more than 2 s starts the clock again
 *  T4. a worker error: a note, and the next slot on the page; no Worker: the decoder on the page; RX off: the
 *      hint; off the grid: nothing kept, nothing decoded
 *  T5. end to end: a made FT8 slot through the real decoder (on the page), at the right DT and frequency
 *  T6. the FT8 status helps when nothing shows: the radio in a mode FT8 is not heard in is named; three
 *      slots in a row with nothing decoded say what to check; a decode clears that
 *  D7. receive audio off: the window says so under its keys, and its key presses the Audio panel's RX key
 *      (never turning RX off); the line follows RX within a second
 *  D8. "Tune to" in CW, FT8 and SSTV: that mode's frequencies from the Spots panel's list (SpotsPanel.calls),
 *      band and MHz (CW's named); choosing one tunes (SpotsPanel.tuneTo) and the menu goes back to its
 *      title; operators only; a radio with no such frequency has no menu; another radio, another menu
 */
"use strict";
const { makeEnv } = require("./fakedom");
const enc = require("./ft8_encode");

let failures = 0, checks = 0;
function check(cond, msg) { checks++; if (!cond) { failures++; console.log("FAIL " + msg); } }
function eq(a, b, msg) { const ja = JSON.stringify(a), jb = JSON.stringify(b); check(ja === jb, msg + ": got " + ja + ", want " + jb); }

const TUNE_TO = "Tune to" + String.fromCharCode(0x2026);     // a "Tune to" menu's title, as index.html has it
function page(opts) {
  opts = opts || {};
  const e = makeEnv({ localStorage: opts.localStorage || {} });
  e.ctx.BigInt = BigInt; e.ctx.Uint32Array = Uint32Array;
  const grid = e.el("div", { id: "grid" });
  e.parking = e.el("div", { id: "tileParking" });            // layout.js's: a tile off the grid
  const tile = e.el("div", { class: "grid-stack-item", "data-tile": "sstv" }, opts.offGrid ? e.parking : grid);
  e.modem = [];                                              // tr:modem, heard from the start (the page opening on a mode)
  e.doc.addEventListener("tr:modem", (ev) => e.modem.push(ev.detail && ev.detail.mode));
  if (opts.ro) {                                             // a ResizeObserver the test drives (D6)
    e.ros = [];
    e.ctx.ResizeObserver = function (cb) { this.cb = cb; e.ros.push(this); };
    e.ctx.ResizeObserver.prototype.observe = function (el) { this.el = el; };
  }
  const modes = e.el("div", { class: "dm-modes" }, tile);
  // "receive audio is off" and its key; the Audio panel's RX key, which it presses
  e.rxBar = e.el("div", { id: "dmRx", hidden: true }, tile);
  e.rxKey = e.el("button", { id: "dmRxOn", type: "button" }, e.rxBar);
  e.audioClicks = 0;
  e.el("button", { id: "audioBtn", type: "button" }).addEventListener("click", () => { e.audioClicks++; e.on = !e.on; });
  e.keys = {};
  for (const [m, id] of [["cw", "dmCw"], ["rtty", "dmRtty"], ["ft8", "dmFt8"], ["sstv", "dmSstv"]]) e.keys[m] = e.el("button", { id, type: "button" }, modes);
  e.panes = { cw: e.el("div", { id: "dmPaneCw" }, tile), rtty: e.el("div", { id: "dmPaneRtty" }, tile), ft8: e.el("div", { id: "dmPaneFt8" }, tile),
              sstv: e.el("div", { id: "dmPaneSstv" }, tile) };
  // the "Tune to" menus: a select whose first option is its title
  e.go = {};
  for (const [m, id] of [["cw", "cwGo"], ["ft8", "ft8Go"], ["sstv", "sstvGo"]]) {
    e.go[m] = e.el("select", { id, hidden: true }, e.panes[m]);
    e.el("option", { value: "" }, e.go[m]).textContent = TUNE_TO;
    e.go[m].value = "";
  }
  e.perms = new Set(opts.viewer ? ["view", "listen"] : ["view", "listen", "operate"]);
  e.ctx.TR = { can: (x) => e.perms.has(x) };
  e.tuned = [];
  if (opts.calls) {                                          // spots.js's side of "Tune to"
    e.callRows = opts.calls;
    e.ctx.SpotsPanel = { calls: (pick) => e.callRows.filter((r) => !pick || pick(r)), tuneTo: (x) => { e.tuned.push(x); return true; } };
  }
  e.ft8Strip = e.el("canvas", { id: "ft8Strip" }, e.panes.ft8);
  e.cq = e.el("input", { id: "ft8CqOnly", type: "checkbox" }, e.panes.ft8);
  e.clear = e.el("button", { id: "ft8Clear", type: "button" }, e.panes.ft8);
  e.status = e.el("p", { id: "ft8Status" }, e.panes.ft8);
  e.list = e.el("div", { id: "ft8List" }, e.panes.ft8);
  if (opts.ro) { e.cwOut = e.el("div", { id: "cwOut" }, e.panes.cw); e.rttyText = e.el("div", { id: "rttyText" }, e.panes.rtty); }
  e.script = e.el("script", { src: "/static/ft8-decode.js?v=42" });
  e.grid = grid; e.tile = tile;
  e.on = !opts.rxOff;
  e.taps = [];
  e.ctx.RadioAudio = { on: () => e.on, tapPcm: (fn) => { e.taps.push(fn); return () => {}; } };
  e.st = { connected: true, mode_name: "USB" };
  e.ctx.RadioControl = { state: () => e.st };
  e.workers = [];
  if (opts.worker !== false) {
    e.ctx.Worker = function (url) { this.url = url; this.posted = []; e.workers.push(this); };
    e.ctx.Worker.prototype.postMessage = function (m, transfer) { this.posted.push({ m, transfer }); };
  }
  if (opts.realDecoder) e.load("ft8-decode.js");
  if (opts.stubs) {
    e.fed = [];
    for (const name of ["SstvPanel", "RttyPanel", "Ft8Panel"]) {
      e.ctx[name] = { summary: () => name + " summary", feed: (p, r) => e.fed.push([name, p.length, r]) };
    }
    e.ctx.SstvTx = { summary: () => (e.txSummary || ""), busy: () => !!e.sending };
    e.ctx.CwPanel = { summary: () => "CwPanel summary", busy: () => !!e.cwSending };   // cwtool.js (no PCM feed)
  } else e.load("ft8.js");
  e.load("digital.js");
  e.click = (el) => el.dispatchEvent(new e.Event("click", { bubbles: true }));
  e.key = (el, key) => { const ev = new e.Event("keydown", { bubbles: true }); ev.key = key; el.dispatchEvent(ev); };
  e.rows = () => e.list.querySelectorAll(".ft8-row").map((r) => r.children.map((c) => c.textContent).join(" "));
  // the radio's audio, paced in real time: 20 ms frames, the clock moving with them
  e.play = (pcm, rate) => {
    rate = rate || 16000;
    const n = rate / 50;
    for (let i = 0; i < pcm.length; i += n) {
      e.clock.advance(20);
      const f = pcm.subarray(i, i + n);
      const i16 = f instanceof Int16Array ? f : Int16Array.from(f, (v) => Math.max(-32767, Math.min(32767, Math.round(v * 32768))));
      for (const t of e.taps) t(i16, rate);
    }
  };
  return e;
}
const shown = (e) => Object.keys(e.panes).filter((m) => !e.panes[m].hidden);

// ---- D1: the mode keys ----
{
  const e = page({ stubs: true });
  eq(shown(e), ["cw"], "D1 CW first, the only pane shown");
  eq(["cw", "rtty", "ft8", "sstv"].map((m) => [e.keys[m].getAttribute("aria-selected"), e.keys[m].tabIndex]),
     [["true", 0], ["false", -1], ["false", -1], ["false", -1]], "D1 the tablist: CW selected, the others out of the tab order");
  e.click(e.keys.ft8);
  eq([shown(e), e.ctx.localStorage.getItem("telerig.digital.mode"), e.ctx.DigitalPanel.mode()], [["ft8"], "ft8", "ft8"], "D1 a click shows FT8, kept");
  e.key(e.keys.ft8, "ArrowRight");
  eq(shown(e), ["sstv"], "D1 ArrowRight from FT8: SSTV");
  e.key(e.keys.sstv, "ArrowRight");
  eq(shown(e), ["cw"], "D1 ArrowRight from SSTV wraps to CW");
  e.key(e.keys.cw, "ArrowLeft");
  eq(shown(e), ["sstv"], "D1 ArrowLeft wraps back to SSTV");
  const f = page({ stubs: true, localStorage: { "telerig.digital.mode": "rtty" } });
  eq(shown(f), ["rtty"], "D1 the kept mode comes back");
  const s = page({ stubs: true, localStorage: { "telerig.digital.mode": "sstv" } });
  eq(shown(s), ["sstv"], "D1 a mode kept from before CW joined comes back");
  const g = page({ stubs: true, localStorage: { "telerig.digital.mode": "<img>" } });
  eq(shown(g), ["cw"], "D1 a kept value that isn't a mode: CW");
}

// ---- D2, D3, D4 ----
{
  const e = page({ stubs: true });
  e.doc.dispatchEvent(new e.ctx.CustomEvent("tr:digital", { detail: { mode: "ft8" } }));
  check(e.keys.ft8.classList.contains("news") && !e.keys.cw.classList.contains("news"), "D2 FT8 has news while CW is shown: a dot on FT8");
  e.doc.dispatchEvent(new e.ctx.CustomEvent("tr:digital", { detail: { mode: "cw" } }));
  check(!e.keys.cw.classList.contains("news"), "D2 news for the mode shown: no dot");
  e.click(e.keys.ft8);
  check(!e.keys.ft8.classList.contains("news"), "D2 showing FT8 clears its dot");
  e.click(e.keys.sstv);
  e.sending = true;
  e.clock.advance(1000);
  check(e.keys.cw.disabled && e.keys.rtty.disabled && e.keys.ft8.disabled && !e.keys.sstv.disabled, "D3 a picture going out: CW, RTTY and FT8 can't be chosen");
  check(/picture is being sent: STOP it first/.test(e.keys.rtty.title), "D3 ... and say why: " + e.keys.rtty.title);
  eq([e.ctx.DigitalPanel.show("rtty"), e.ctx.DigitalPanel.show("cw"), shown(e)], [false, false, ["sstv"]], "D3 ... show() refuses too");
  e.sending = false;
  e.clock.advance(1000);
  check(!e.keys.cw.disabled && !e.keys.rtty.disabled && !e.keys.ft8.disabled, "D3 the picture done: every mode again");
  e.click(e.keys.cw);
  e.cwSending = true;
  e.clock.advance(1000);
  check(e.keys.rtty.disabled && e.keys.ft8.disabled && e.keys.sstv.disabled && !e.keys.cw.disabled, "D3 a CW message going out: the others can't be chosen");
  check(/CW is being sent: STOP it first/.test(e.keys.sstv.title), "D3 ... and say why: " + e.keys.sstv.title);
  eq([e.ctx.DigitalPanel.show("ft8"), shown(e)], [false, ["cw"]], "D3 ... show() refuses too");
  e.cwSending = false;
  e.clock.advance(1000);
  e.click(e.keys.ft8);
  e.cwSending = true;                                      // this page's CW on the air while FT8 is shown (a race)
  e.clock.advance(1000);
  eq([shown(e), e.ctx.DigitalPanel.mode()], [["cw"], "cw"], "D3 the mode sending comes into view, its STOP with it");
  e.cwSending = false;
  e.clock.advance(1000);
  check(!e.keys.rtty.disabled && !e.keys.ft8.disabled && !e.keys.sstv.disabled, "D3 the message done: every mode again");
  e.click(e.keys.sstv);
  e.txSummary = "TX Robot 36 0:12";
  eq(e.ctx.DigitalPanel.summary(), "TX Robot 36 0:12", "D4 SSTV shown: the picture going out");
  e.txSummary = "";
  eq(e.ctx.DigitalPanel.summary(), "SstvPanel summary", "D4 ... else the picture coming in");
  e.click(e.keys.rtty);
  eq(e.ctx.DigitalPanel.summary(), "RttyPanel summary", "D4 RTTY shown: RTTY's");
  e.click(e.keys.ft8);
  eq(e.ctx.DigitalPanel.summary(), "Ft8Panel summary", "D4 FT8 shown: FT8's");
  e.click(e.keys.cw);
  eq(e.ctx.DigitalPanel.summary(), "CwPanel summary", "D4 CW shown: CW's");
  e.ctx.DigitalPanel.feed(new Int16Array(320), 16000);
  eq(e.fed, [["SstvPanel", 320, 16000], ["RttyPanel", 320, 16000], ["Ft8Panel", 320, 16000]],
     "D4 DigitalPanel.feed reaches RTTY, FT8 and SSTV (CW listens on the audio graph)");
  e.ctx.DigitalPanel.feed(new Int16Array(0), 16000);
  eq(e.fed.length, 3, "D4 an empty frame: nothing");
}

// ---- D5: tr:modem, a mode come into view ----
{
  const tileEv = (e, d) => e.doc.dispatchEvent(new e.ctx.CustomEvent("tr:tile", { detail: Object.assign({ id: "sstv" }, d) }));
  const layoutEv = (e) => e.doc.dispatchEvent(new e.ctx.CustomEvent("tr:layout", { detail: { device: "desktop" } }));
  const e = page({ stubs: true });
  eq(e.modem, ["cw"], "D5 the page opening on CW, the window on the grid: CW in view");
  eq(page({ stubs: true, localStorage: { "telerig.digital.mode": "rtty" } }).modem, ["rtty"], "D5 ... on the kept mode: that mode");
  e.click(e.keys.ft8);
  eq(e.modem, ["cw", "ft8"], "D5 FT8 chosen: FT8 in view");
  e.click(e.keys.ft8); e.clock.advance(3000);
  eq(e.modem, ["cw", "ft8"], "D5 the FT8 key again, the 1 s redraws: nothing new");
  e.key(e.keys.ft8, "ArrowRight");
  eq(e.modem.slice(2), ["sstv"], "D5 ArrowRight: SSTV in view");
  e.click(e.keys.cw);
  e.sending = true; e.clock.advance(1000);
  eq(e.modem.slice(3), ["cw", "sstv"], "D5 the window held on SSTV while it sends a picture: SSTV in view");
  e.sending = false; e.clock.advance(1000);
  e.click(e.keys.cw);
  const n = e.modem.length;
  tileEv(e, { visible: true, collapsed: true }); tileEv(e, { visible: true, collapsed: false }); e.clock.advance(1000);
  eq(e.modem.length, n, "D5 the window folded and opened again: nothing new (its modes kept listening)");
  layoutEv(e);
  eq(e.modem.length, n, "D5 a layout with the window on the grid all along: nothing new");
  e.parking.appendChild(e.tile); tileEv(e, { visible: false });
  e.clock.advance(3000);
  eq(e.modem.length, n, "D5 the window removed: nothing (nothing in view)");
  e.grid.appendChild(e.tile); tileEv(e, { visible: true });
  eq(e.modem.slice(n), ["cw"], "D5 the window added again: its mode in view");
  const f = page({ stubs: true, offGrid: true });
  f.click(f.keys.rtty); f.clock.advance(3000);
  eq(f.modem, [], "D5 the window off the grid: nothing, whatever is chosen");
  f.grid.appendChild(f.tile); layoutEv(f);
  eq(f.modem, ["rtty"], "D5 a layout that places the window on the grid: its mode in view");
}

// ---- D6: the text screens keep their place ----
{
  const e = page({ stubs: true, ro: true });
  eq(["cwOut", "rttyText", "ft8List"].map((id) => e.ros.filter((r) => r.el && r.el.id === id).length), [1, 1, 1],
     "D6 CW's chat, RTTY's text and FT8's list are each watched for being shown again");
  const L = e.list, ro = e.ros.find((r) => r.el === L);
  const scrolled = (top) => { L.scrollTop = top; L.dispatchEvent(new e.Event("scroll", { bubbles: false })); };
  const hide = () => { L.clientHeight = 0; scrolled(0); };   // hidden: the browser drops the place (and may say so)
  const show = (h) => { L.clientHeight = h; ro.cb([]); };    // shown again, or resized: the observer
  L.scrollHeight = 1000; L.clientHeight = 200; scrolled(800);  // at the end
  hide(); L.scrollHeight = 1400;                              // text came in while hidden
  show(200);
  eq(L.scrollTop, 1400, "D6 at the end when hidden: shown again at the new end");
  scrolled(300);                                              // scrolled back, to read
  hide(); L.scrollHeight = 1800;
  show(200);
  eq(L.scrollTop, 300, "D6 scrolled back when hidden: shown again where it was left");
  show(260);
  eq(L.scrollTop, 300, "D6 ... a resize keeps that place");
  scrolled(1600);                                             // 1800 - 1600 - 200: the end again
  L.scrollHeight = 1900; show(300);
  eq(L.scrollTop, 1900, "D6 at the end, a resize: still at the end");
}

// ---- T1: the slots ----
const RATE = 16000;
const ramp = (n) => { const a = new Int16Array(n); for (let i = 0; i < n; i++) a[i] = (i % 20000) - 10000; return a; };
{
  const e = page();
  eq(e.taps.length >= 1, true, "T1 the FT8 tab taps the radio's audio");
  check(/the first slot is decoded after/.test(e.status.textContent), "T1 before any slot: " + e.status.textContent);
  // the clock starts at 1 000 000 ms: slots at 990 000 (heard from 1 000 000: 5 s), 1 005 000, 1 020 000 ...
  e.play(ramp(RATE * 20.2), RATE);                         // until 1 020 200: not yet (300 ms of grace)
  eq(e.workers.reduce((n, w) => n + w.posted.length, 0), 0, "T1 nothing yet: the 990 000 slot was heard for 5 s, the next isn't over + 0.3 s");
  e.play(ramp(RATE * 0.2), RATE);
  const w = e.workers[0];
  eq([e.workers.length, w && w.url], [1, "/static/ft8-decode.js?v=42"], "T1 a worker, from the page's own script (its ?v=)");
  const p = w && w.posted[0];
  check(p && p.m.rate === RATE && p.m.samples.length === RATE * 15, "T1 one slot: 15 s at 16 kHz");
  // the sample that arrived at 1 005 000 ms is sample (1 005 000 - 1 000 000) * 16 = 80 000 of the stream
  check(p && p.m.samples[0] === ((80000 % 20000) - 10000) / 32768 && p.m.samples[1] === ((80001 % 20000) - 10000) / 32768,
        "T1 ... from the slot's start (" + (p && p.m.samples[0]) + ")");
  check(p && p.transfer && p.transfer[0] === p.m.samples.buffer, "T1 ... handed over, not copied");
  check(/Decoding the 00:16:45 slot/.test(e.status.textContent), "T1 the panel says so: " + e.status.textContent);
  e.play(ramp(RATE * 5), RATE);
  eq(w.posted.length, 1, "T1 no second post while the first is decoding, and none twice for a slot");
  w.onmessage({ data: { id: p.m.id, decodes: [
    { snr: -12, dt: 0.3, freq: 1500, text: "W9XYZ K1ABC -12" },
    { snr: -3, dt: 0.1, freq: 700, text: "CQ K1ABC FN42" },
    { snr: 4, dt: -0.2, freq: 2210, text: "<b>bold</b> K1ABC" },
  ] } });
  // ---- T2: the list ----
  eq(e.list.querySelectorAll(".ft8-slot").map((s) => s.textContent), ["00:16:45 UTC"], "T2 a header for the slot");
  eq(e.rows(), ["-3 0.1 700 CQ K1ABC FN42", "-12 0.3 1500 W9XYZ K1ABC -12", "+4 -0.2 2210 <b>bold</b> K1ABC"],
     "T2 the rows by frequency: dB, DT, Hz, the message");
  eq(e.list.querySelectorAll(".ft8-row").map((r) => r.classList.contains("cq")), [true, false, false], "T2 a CQ is marked");
  eq(e.list.querySelectorAll("b").length, 0, "T2 a message is text, never markup");
  check(/3 messages at 00:16:45 UTC/.test(e.status.textContent), "T2 the status: " + e.status.textContent);
  eq(e.ctx.Ft8Panel.summary(), "FT8 3 at 00:16", "T2 the summary");
  eq(e.ctx.Ft8Panel.markers().map((m) => m.hz), [1525, 725, 2235], "T2 the strip marks each message's middle");
  e.cq.checked = true;
  e.cq.dispatchEvent(new e.Event("change", { bubbles: true }));
  check(e.list.classList.contains("cq-only") && e.ctx.localStorage.getItem("telerig.ft8.cq") === "1", "T2 CQ only: the list filters, kept");
  // the next slot (1 020 000) is due at 1 035 300; then 400 rows at most
  e.play(ramp(RATE * 10), RATE);
  const p2 = w.posted[1];
  check(!!p2, "T2 the next slot goes to the worker");
  w.onmessage({ data: { id: p2.m.id, decodes: [] } });
  check(/Nothing decoded in the 00:17:00 slot/.test(e.status.textContent), "T2 an empty slot: " + e.status.textContent);
  eq(e.list.querySelectorAll(".ft8-slot").length, 1, "T2 ... and no header for it");
  let id = 0;
  for (let s = 0; s < 9; s++) {
    e.play(ramp(RATE * 15), RATE);
    const q = w.posted[w.posted.length - 1];
    if (q.m.id === id) continue;
    id = q.m.id;
    w.onmessage({ data: { id, decodes: Array.from({ length: 50 }, (_, k) => ({ snr: -10, dt: 0, freq: 300 + 50 * k, text: "K" + s + " W9XYZ " + k })) } });
  }
  const n = e.list.querySelectorAll(".ft8-row").length;
  check(n <= 400 && n >= 350, "T2 at most 400 rows kept: " + n);
  check(!e.rows().some((r) => /CQ K1ABC FN42/.test(r)), "T2 ... the oldest slot went first");
  e.click(e.clear);
  eq([e.list.children.length, e.ctx.Ft8Panel.markers().length], [0, 0], "T2 Clear empties the list and the marks");
}

// ---- T3: too little heard; a gap ----
{
  const e = page();
  e.clock.advance(11000);                                  // 1 011 000: 6 s into the 1 005 000 slot
  e.play(ramp(RATE * 9.5), RATE);                          // to 1 020 500: that slot heard for 9 s
  eq(e.workers.reduce((n, w) => n + w.posted.length, 0), 0, "T3 a slot heard for under 10 s: skipped");
  e.play(ramp(RATE * 15), RATE);                           // to 1 035 500: the 1 020 000 slot in full
  eq(e.workers[0] && e.workers[0].posted.length, 1, "T3 the next, heard in full: decoded");
  const w = e.workers[0], before = w.posted.length;
  w.onmessage({ data: { id: w.posted[0].m.id, decodes: [] } });
  e.clock.advance(3000);                                   // the audio stops for 3 s, comes back at 1 038 500
  e.play(ramp(RATE * 15), RATE);                           // the 1 035 000 slot: heard from 1 038 500, 11.5 s
  const last = w.posted[w.posted.length - 1];
  check(w.posted.length === before + 1 && last.m.samples.slice(0, RATE * 3).every((v) => v === 0) && last.m.samples[RATE * 4] !== 0,
        "T3 after a 3 s gap the clock starts again: the slot's unheard 3.5 s are silence, then the audio");
}

// ---- T4: errors, no worker, RX off, off the grid ----
{
  const e = page();
  const slot = () => e.play(ramp(RATE * 15), RATE);
  e.play(ramp(RATE * 20.5), RATE);
  const w = e.workers[0];
  w.onerror({});
  check(/could not be decoded/.test(e.status.textContent), "T4 a worker error: a note: " + e.status.textContent);
  e.ctx.FT8 = { decodeSlot: (s, r) => [{ snr: -5, dt: 0, freq: 1000, text: "CQ W9XYZ EN53" }] };
  slot();
  e.clock.advance(1);
  eq(e.rows(), ["-5 0.0 1000 CQ W9XYZ EN53"], "T4 ... the next slot decoded on the page instead");
  const f = page({ worker: false });
  f.ctx.FT8 = { decodeSlot: () => [{ snr: 1, dt: 0.4, freq: 1234, text: "K1ABC W9XYZ RR73" }] };
  f.play(ramp(RATE * 20.5), RATE);
  f.clock.advance(1);
  eq(f.rows(), ["+1 0.4 1234 K1ABC W9XYZ RR73"], "T4 no Worker in this browser: the decoder on the page");
  const g = page({ rxOff: true });
  eq(g.status.textContent, "", "T4 RX off: nothing here (the window's own line says so)");
  const h = page();
  h.grid.removeChild(h.tile);
  h.play(ramp(RATE * 40), RATE);
  eq(h.workers.length, 0, "T4 off the grid: nothing kept, nothing decoded");
}

// ---- T5: the real decoder, end to end ----
{
  const e = page({ worker: false, realDecoder: true });
  // the clock starts 5 s before a slot: the message starts 0.5 s into it (DT 0), at 1000 Hz, -8 dB
  const buf = new Float32Array(RATE * 21);
  enc.noise(buf, RATE, 0.05, 31);
  enc.add(buf, RATE, enc.tones(enc.pack("CQ W9XYZ EN53")), { f0: 1000, start: 5.5, snr: -8, sigma: 0.05 });
  e.play(buf, RATE);
  e.clock.advance(1);
  const r = e.rows();
  eq(r.map((x) => x.split(" ").slice(3).join(" ")), ["CQ W9XYZ EN53"], "T5 a made slot, decoded by the real decoder on the page");
  const [db, dt, hz] = (r[0] || "").split(" ").map(Number);
  check(Math.abs(dt) <= 0.15 && Math.abs(hz - 1000) <= 3 && Math.abs(db + 8) <= 4, `T5 ... at DT ${dt}, ${hz} Hz, ${db} dB`);
}

// ---- T6: the status helps when nothing shows ----
{
  const e = page();
  e.st = { connected: true, mode_name: "FM" };
  e.clock.advance(500);
  eq(e.status.textContent, "The radio is in FM: FT8 is heard in USB or DATA-USB (Tune to sets it).", "T6 the radio in FM: said, with what to do");
  for (const m of ["DATA-USB", "USB"]) {
    e.st = { connected: true, mode_name: m }; e.clock.advance(500);
    check(/the first slot is decoded after/.test(e.status.textContent), "T6 in " + m + " it listens: " + e.status.textContent);
  }
  e.st = { connected: false, mode_name: "FM" }; e.clock.advance(500);
  check(/the first slot is decoded after/.test(e.status.textContent), "T6 no radio connected: no word about its mode");
  e.st = { connected: true, mode_name: "USB" };
  // slots with nothing decoded
  const answer = (decodes) => { const w = e.workers[0], j = w.posted[w.posted.length - 1]; w.onmessage({ data: { id: j.m.id, decodes } }); };
  e.play(ramp(RATE * 20.5), RATE); answer([]);
  check(/^Nothing decoded in the \d\d:\d\d:\d\d slot\.$/.test(e.status.textContent), "T6 one slot with nothing: said for that slot: " + e.status.textContent);
  e.play(ramp(RATE * 15), RATE); answer([]);
  check(/^Nothing decoded in the /.test(e.status.textContent), "T6 two: still only that");
  e.play(ramp(RATE * 15), RATE); answer([]);
  eq(e.status.textContent, "Nothing decoded for 45 s. Check the frequency (Tune to), the mode and this computer's clock.", "T6 three in a row: what to check");
  e.play(ramp(RATE * 15), RATE); answer([]);
  check(/^Nothing decoded for 60 s\. Check/.test(e.status.textContent), "T6 ... and it keeps counting: " + e.status.textContent);
  e.play(ramp(RATE * 15), RATE); answer([{ snr: -3, dt: 0.1, freq: 700, text: "CQ K1ABC FN42" }]);
  check(/^1 message at /.test(e.status.textContent), "T6 a decode: back to the count: " + e.status.textContent);
  e.play(ramp(RATE * 15), RATE); answer([]);
  check(/^Nothing decoded in the /.test(e.status.textContent), "T6 ... and the run of empty slots starts again");
}

// ---- D7: receive audio off ----
{
  const e = page({ stubs: true, rxOff: true });
  eq(e.rxBar.hidden, false, "D7 RX off: the line under the keys shows");
  e.click(e.rxKey);
  eq([e.audioClicks, e.on, e.rxBar.hidden], [1, true, true], "D7 its key presses the Audio panel's RX key: RX on, the line gone");
  e.click(e.rxKey);
  eq([e.audioClicks, e.on], [1, true], "D7 pressed again with RX on (a double tap): RX is not turned off");
  e.on = false; e.clock.advance(1000);
  eq(e.rxBar.hidden, false, "D7 RX turned off elsewhere: the line is back within a second");
  e.on = true; e.clock.advance(1000);
  eq(e.rxBar.hidden, true, "D7 RX turned on elsewhere: gone within a second");
  const f = page({ stubs: true });
  eq(f.rxBar.hidden, true, "D7 RX on from the start: no line");
}

// ---- D8: "Tune to" ----
{
  const HF = [
    { band: "40 m", hz: 7030000, mode: "CW", name: "QRP calling" }, { band: "40 m", hz: 7074000, mode: "DIGI", name: "FT8" },
    { band: "40 m", hz: 7171000, mode: "SSB", name: "SSTV" }, { band: "20 m", hz: 14060000, mode: "CW", name: "QRP calling" },
    { band: "20 m", hz: 14074000, mode: "DIGI", name: "FT8" }, { band: "20 m", hz: 14100000, mode: "CW", name: "NCDXF beacons" },
    { band: "20 m", hz: 14230000, mode: "SSB", name: "SSTV" }, { band: "20 m", hz: 14285000, mode: "SSB", name: "QRP calling" },
  ];
  const VHF = [{ band: "2 m", hz: 144174000, mode: "DIGI", name: "FT8" }, { band: "23 cm", hz: 1296174000, mode: "DIGI", name: "FT8" },
               { band: "2 m", hz: 146520000, mode: "FM", name: "FM calling" }];
  const e = page({ stubs: true, calls: HF });
  const T = TUNE_TO;
  const texts = (m) => e.go[m].options.map((o) => o.textContent);
  const choose = (m, v) => { e.go[m].value = v; e.go[m].dispatchEvent(new e.Event("change", { bubbles: true })); };
  eq(texts("ft8"), [T, "40 m  7.074", "20 m  14.074"], "D8 FT8's menu: its title, then each band's FT8 frequency");
  eq(texts("sstv"), [T, "40 m  7.171", "20 m  14.230"], "D8 SSTV's menu");
  eq(texts("cw"), [T, "40 m  7.030  QRP calling", "20 m  14.060  QRP calling", "20 m  14.100  NCDXF beacons"], "D8 CW's menu: the CW rows, each named");
  eq(["cw", "ft8", "sstv"].map((m) => [e.go[m].hidden, e.go[m].value]), [[false, ""], [false, ""], [false, ""]], "D8 the menus show, on their title");
  choose("ft8", "1");
  eq([e.tuned, e.go.ft8.value], [[{ band: "20 m", hz: 14074000, mode: "DIGI", name: "FT8" }], ""], "D8 a choice tunes to that row, and the menu is back on its title");
  choose("cw", "2");
  eq(e.tuned.length === 2 && e.tuned[1].hz, 14100000, "D8 CW's third entry: the beacons");
  choose("sstv", "");
  eq(e.tuned.length, 2, "D8 the title itself tunes nothing");
  e.perms.delete("operate"); e.clock.advance(1000);
  eq(["cw", "ft8", "sstv"].map((m) => e.go[m].hidden), [true, true, true], "D8 a viewer has no menus (within a second of the role)");
  choose("ft8", "1");
  eq(e.tuned.length, 2, "D8 ... and nothing to choose");
  e.perms.add("operate"); e.clock.advance(1000);
  eq([e.go.ft8.hidden, texts("ft8").length], [false, 3], "D8 an operator again: back");
  e.callRows = VHF; e.clock.advance(1000);
  eq([texts("ft8"), e.go.ft8.hidden, e.go.sstv.hidden, e.go.cw.hidden], [[T, "2 m  144.174", "23 cm  1296.174"], false, true, true],
     "D8 another radio: its own FT8 frequencies, and no menu where it has none");
  choose("ft8", "1");
  eq(e.tuned[2] && e.tuned[2].hz, 1296174000, "D8 ... and a choice is the new radio's row");
  const none = page({ stubs: true });
  eq(["cw", "ft8", "sstv"].map((m) => none.go[m].hidden), [true, true, true], "D8 no list to choose from (the Spots panel's script missing): no menus");
}

console.log(failures ? `\n${failures} of ${checks} checks FAILED` : `\nALL PASS (${checks} checks)`);
process.exit(failures ? 1 : 0);
