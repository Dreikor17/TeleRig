/* The CW chat's received bubbles (frontend/cwtool.js): a pause of 5 s or more in the received text starts
 * a new bubble. The REAL tr.js + cwtool.js in a Node vm on tests/fakedom.js, with a stand-in for the
 * neural decoder's Worker (it answers what the test says the model heard) and for the audio capture
 * worklet. No browser, no model, no radio; nothing here keys anything. Run by tests/t_c1.py (unit):
 *
 *     node tests/cw_chat_test.js        -> exit 0 and "ALL PASS", or exit 1 naming each failure
 *
 *  W1. text decoded close together stays in one bubble
 *  W2. new text after a pause of 5 s or more (the signal still there, nothing decoded) is a new bubble,
 *      from its live preview on; a shorter pause is not
 *  W3. Clear empties the chat and forgets the pause
 *  W4. in the Modem window (the panel is its CW pane): committed text is news for the CW key, a live
 *      preview alone is not
 *  W5. CwPanel.summary(): the latest text heard, the decoder on / off, this page's CW on the air;
 *      CwPanel.busy(): only this page's own CW message on the air
 *  W6. the scope's CW button: the window shown and on CW, the decoder started (a running one keeps its
 *      chat); with CW in view it toggles the decoder; while SSTV sends a picture nothing changes. The
 *      window collapsed: the decoder keeps listening; the window removed: it stops
 *  W7. CW decodes by default: CW coming into view (tr:modem) turns Decode on, another mode doesn't; Decode
 *      off stays off until CW next comes into view; a running decoder keeps its chat. The decoder never
 *      makes the page's audio (an audio context made before a click stays paused, and the mic would send
 *      silence): with none, no capture; once there is one (RX), the capture joins at the next tick. Play
 *      (a click) makes it, and the capture joins before the sidetone starts. A capture that can't be made
 *      is said once and tried again only when Decode is turned on again
 *  W8. the chat scrolls inside the window: at its end it follows new text; scrolled back, new text doesn't
 *      move it; nothing new, nothing written again (a selection in it lasts)
 *  W9. with the REAL Modem window (digital.js): the page opening on CW, on the grid, decodes with no click;
 *      opening on another mode it waits for CW; Decode off, another mode and back: on again; folded and
 *      opened again: still off; the window off the grid: off, brought onto it showing CW: on; removed:
 *      off, added again: on; a layout rebuilt around it: no change
 */
"use strict";
const { makeEnv } = require("./fakedom");

let failures = 0, checks = 0;
function check(cond, msg) { checks++; if (!cond) { failures++; console.log("FAIL " + msg); } }
function eq(a, b, msg) { const ja = JSON.stringify(a), jb = JSON.stringify(b); check(ja === jb, msg + ": got " + ja + ", want " + jb); }

const SR = 3200, HOP = 48;                                   // == cwtool.js SR / the worker's hop
const CW_IDS = { button: ["cwToolBtn", "cwDecodeBtn", "cwClear", "cwPlay", "cwTx"], div: ["cwHead", "cwHint", "cwOut", "cwStatus", "cwTxHint", "cwTxRow"],
                 input: ["cwSend", "cwWpmSet", "cwToneSet"] };

// the CW panel's elements, the page's stand-ins (nothing is ever sent), and the page's audio: with
// opts.audio false there is none until something makes it (RadioAudio.ensure, as a click does, or e.makeAudio)
function cwParts(e, parent, opts) {
  e.el("div", { id: "cwTool" }, parent);
  for (const [tag, ids] of Object.entries(CW_IDS)) for (const id of ids) e.el(tag, tag === "input" ? { id } : { id, type: "button" }, parent);
  e.doc.getElementById("cwWpmSet").value = "18"; e.doc.getElementById("cwToneSet").value = "600";
  e.st = { connected: true, mode_name: "CW" };
  e.ctx.RadioControl = { send: () => { throw new Error("nothing is sent here"); }, state: () => e.st };
  e.ctx.RadioTx = { owner: () => null, nextSeq: () => 1 };
  e.workers = 0;
  e.ctx.Worker = function () { e.worker = this; e.workers++; this.posted = []; this.postMessage = (m) => this.posted.push(m); };
  e.node = null; e.nodes = 0; e.bused = []; e.started = []; e.addModules = 0; e.ensured = 0;
  e.ctx.AudioWorkletNode = function () { e.node = this; e.nodes++; this.port = {}; this.connect = () => {}; this.disconnect = () => {}; };
  // opts.slowModule: the capture worklet's module loads when the test says (e.moduleLoaded()); a second
  // load of it would fail as the browser's does (its processor name is already taken)
  let loads = 0;
  const addModule = () => {
    e.addModules++;
    if (opts.addModuleFails) return Promise.reject(new Error("no worklet here"));
    if (++loads > 1 && opts.slowModule) return Promise.reject(new Error("cw-capture is already registered"));
    if (!opts.slowModule) return Promise.resolve();
    return new Promise((r) => { e.moduleLoaded = r; });
  };
  const actx = {
    currentTime: 0, destination: {},
    audioWorklet: { addModule },
    createOscillator: () => ({ type: "", frequency: { value: 0 }, connect() {}, start() { e.started.push({ capture: !!e.node }); }, stop() {} }),
    createGain: () => ({ gain: { value: 0, setValueAtTime() {}, linearRampToValueAtTime() {} }, connect() {} }),
  };
  const bus = { connect: (n) => e.bused.push(n) };
  let audio = opts.audio !== false;
  e.makeAudio = () => { audio = true; };
  e.ctx.RadioAudio = { ensure() { e.ensured++; audio = true; }, ctx: () => (audio ? actx : null), bus: () => (audio ? bus : null),
                       on: () => true, state: () => ({}), tapPcm: () => () => {} };
  e.click = (el) => el.dispatchEvent(new e.Event("click", { bubbles: true }));
  e.dec = e.doc.getElementById("cwDecodeBtn");
  e.decOn = () => e.dec.getAttribute("aria-pressed") === "true" && e.dec.classList.contains("on");
  e.settle = async () => { for (let i = 0; i < 5; i++) await e.flush(); };
  e.modem = (m) => e.doc.dispatchEvent(new e.ctx.CustomEvent("tr:modem", { detail: { mode: m } }));
  e.tileEv = (detail) => e.doc.dispatchEvent(new e.ctx.CustomEvent("tr:tile", { detail: Object.assign({ id: "sstv" }, detail) }));
}

async function page(opts) {
  opts = opts || {};
  const e = makeEnv();
  const tile = e.el("div", { class: "grid-stack-item", "data-tile": "sstv" });   // the Modem window; the panel is its CW pane
  cwParts(e, tile, opts);
  e.load("tr.js");
  e.load("cwtool.js");
  chat(e);
  if (opts.decode === false) return e;                       // Decode as the page left it (W7)
  e.click(e.dec);
  await e.settle();
  e.ready();
  return e;
}

// the model's side of the chat: e.ready() (the worker loaded), e.sec / e.say (below), e.bubbles()
function chat(e) {
  e.ready = () => e.worker.onmessage({ data: { type: "ready", hop: HOP } });
  // one second of the radio's audio (a steady signal: never "quiet") and the decode tick, then the model's
  // answer to the decode it asked for (as the real worker does, one at a time): `text` heard, and with
  // `commit` a settled word gap as late as cwtool allows (its tail guard), when the audio is long enough
  let answered = 0;
  e.sec = (text, commit) => {
    e.node.port.onmessage({ data: new Float32Array(SR).fill(0.05) });
    e.clock.advance(1000);
    const d = e.worker.posted.filter((m) => m.type === "decode").slice(-1)[0];
    if (!d || d.id === answered) return false;
    answered = d.id;
    let len = d.audio.length;                                // the real audio, before the zero padding
    while (len > 0 && d.audio[len - 1] === 0) len--;
    const gap = len - Math.round(1.25 * SR) - 200;           // == cwtool TAIL_GUARD, a little inside it
    let ws = [], cs = [];
    const can = commit && gap >= 2 * SR + 100;               // == cwtool MIN_CONFIRMED
    if (can) {
      const f = Math.floor(gap / HOP) - 1;
      cs = commit.split("").map((ch, i) => ({ char: ch, endFrame: Math.min(f - 2, 10 + i) })).concat([{ char: " ", endFrame: f + 2 }]);
      ws = [{ startFrame: f, endFrame: f + 2 }];
    }
    e.worker.onmessage({ data: { type: "result", id: d.id, text, wordSpaceSpans: ws, characterSpans: cs } });
    return !!can;
  };
  e.say = (text) => { for (let i = 0; i < 8; i++) if (e.sec(text, text)) return; throw new Error("never committed: " + text); };
  e.bubbles = () => {
    const html = e.doc.getElementById("cwOut").innerHTML || "";
    return [...html.matchAll(/<div class="cw-msg rx">(.*?)<\/div><\/div>/g)].map((m) => m[1].replace(/<[^>]+>/g, ""));
  };
}

// W9: the REAL Modem window around the CW panel. opts.onGrid false: the window starts in the parking
// (layout.js's #tileParking), off the grid; opts.mode: the mode the window kept
function modemPage(opts) {
  opts = opts || {};
  const e = makeEnv({ localStorage: opts.mode ? { "telerig.digital.mode": opts.mode } : {} });
  e.grid = e.el("div", { id: "grid" });
  e.parking = e.el("div", { id: "tileParking" });
  e.tile = e.el("div", { class: "grid-stack-item", "data-tile": "sstv" }, opts.onGrid === false ? e.parking : e.grid);
  const modes = e.el("div", { class: "dm-modes" }, e.tile);
  e.keys = {};
  for (const [m, id] of [["cw", "dmCw"], ["rtty", "dmRtty"], ["ft8", "dmFt8"], ["sstv", "dmSstv"]]) e.keys[m] = e.el("button", { id, type: "button" }, modes);
  const panes = {};
  for (const [m, id] of [["cw", "dmPaneCw"], ["rtty", "dmPaneRtty"], ["ft8", "dmPaneFt8"], ["sstv", "dmPaneSstv"]]) panes[m] = e.el("div", { id }, e.tile);
  cwParts(e, panes.cw, opts);
  e.load("tr.js");
  e.load("cwtool.js");
  e.load("digital.js");
  chat(e);
  e.move = (where) => where.appendChild(e.tile);             // layout.js parks a tile, or places it on the grid
  e.layoutEv = () => e.doc.dispatchEvent(new e.ctx.CustomEvent("tr:layout", { detail: { device: "desktop" } }));
  return e;
}

(async function main() {
  // W1 + W2
  {
    const e = await page();
    e.sec(); e.sec("CQ"); e.say("CQ CQ");
    e.sec("DE N0"); e.say("DE N0CALL");
    eq(e.bubbles(), ["CQ CQ DE N0CALL"], "W1 text close together: one bubble");
    for (let i = 0; i < 3; i++) e.sec("");                    // 3 s: signal, nothing decoded
    e.sec("K");
    eq(e.bubbles(), ["CQ CQ DE N0CALL K"], "W2 a 4 s pause: still the same bubble (live preview)");
    e.say("K");
    for (let i = 0; i < 6; i++) e.sec("");                    // 6 s: signal, nothing decoded
    e.sec("N1CALL");
    eq(e.bubbles(), ["CQ CQ DE N0CALL K", "N1CALL"], "W2 a 6 s pause: the new characters open a new bubble");
    e.say("N1CALL DE");
    eq(e.bubbles(), ["CQ CQ DE N0CALL K", "N1CALL DE"], "W2 ... and their text is committed there");
    // W3
    e.doc.getElementById("cwClear").dispatchEvent(new e.Event("click", { bubbles: true }));
    eq(e.bubbles(), [], "W3 Clear empties the chat");
    for (let i = 0; i < 6; i++) e.sec("");
    e.sec("TE"); e.say("TEST"); e.sec("IN"); e.say("ING");
    eq(e.bubbles(), ["TEST ING"], "W3 ... and forgets the pause: the next text is one bubble");
  }
  // a commit long after its text first showed does not move it to a new bubble
  {
    const e = await page();
    e.sec(); e.sec("CQ"); e.say("CQ");
    e.sec("QRZ");
    for (let i = 0; i < 6; i++) e.sec("QRZ");                 // the same text for 6 s (no new characters)
    e.say("QRZ");
    eq(e.bubbles(), ["CQ QRZ"], "W2 text committed 6 s after it showed stays in the bubble it showed in");
  }
  // W4: news for the window's CW key
  {
    const e = await page();
    const news = [];
    e.doc.addEventListener("tr:digital", (ev) => news.push(ev.detail && ev.detail.mode));
    e.sec(); e.sec("CQ");
    eq(news, [], "W4 a live preview alone: no news");
    e.say("CQ CQ");
    check(news.length > 0 && news.every((m) => m === "cw"), "W4 committed text: news for CW's key: " + JSON.stringify(news));
  }
  // W5: the collapsed window's summary; busy = this page's own CW message on the air
  {
    const e = await page();
    const P = e.ctx.CwPanel;
    eq([P.summary(), P.busy()], ["CW decoder on", false], "W5 the decoder on, nothing heard yet; not busy");
    e.sec(); e.say("CQ CQ DE N0CALL");
    eq(P.summary(), "CW CQ CQ DE N0CALL", "W5 the latest text heard");
    e.say("N0CALL K");
    eq([e.bubbles(), P.summary()], [["CQ CQ DE N0CALL N0CALL K"], "CW DE N0CALL N0CALL K"], "W5 ... its last 18 characters");
    e.click(e.dec);
    eq([P.summary(), e.decOn()], ["CW decoder off", false], "W5 Decode off: says so");
    e.st = Object.assign({}, e.st, { cw_tx: true });
    e.ctx.TR.isTxOwner = () => false;
    eq([P.busy(), P.summary()], [false, "CW decoder off"], "W5 someone else's CW on the air: not this page's");
    e.ctx.TR.isTxOwner = () => true;
    eq([P.busy(), P.summary()], [true, "CW on the air"], "W5 this page's CW message on the air: busy (the window stays on CW)");
  }
  // W6: the scope's CW button and the window (stand-ins for RadioLayout and DigitalPanel)
  {
    const e = await page();                                  // page() turned the decoder on
    const calls = [];
    let vis = false, mode = "ft8", lock = false;
    e.ctx.RadioLayout = { visible: (id) => (calls.push(["visible", id]), vis), showTile: (id) => (calls.push(["showTile", id]), vis = true) };
    e.ctx.DigitalPanel = { mode: () => mode, show: (m) => { calls.push(["show", m]); if (lock) return false; mode = m; return true; } };
    const scopeBtn = e.doc.getElementById("cwToolBtn");
    e.click(e.dec);
    check(!e.decOn(), "W6 (Decode off to start with)");
    e.click(scopeBtn);
    eq(calls, [["visible", "sstv"], ["showTile", "sstv"], ["show", "cw"]], "W6 the scope's CW button: the window shown, on CW");
    check(e.decOn() && scopeBtn.classList.contains("active"), "W6 ... and the decoder started, Decode lit");
    calls.length = 0;
    e.click(scopeBtn);
    eq([calls, e.decOn()], [[["visible", "sstv"]], false], "W6 CW in view: the button toggles the decoder");
    e.click(e.dec);
    e.sec(); e.say("CQ");
    mode = "rtty";
    e.click(scopeBtn);
    eq([mode, e.decOn(), e.bubbles()], ["cw", true, ["CQ"]], "W6 another mode shown, the decoder running: CW in view, its chat kept");
    e.click(e.dec);
    mode = "sstv"; lock = true;
    e.click(scopeBtn);
    eq([mode, e.decOn()], ["sstv", false], "W6 SSTV sending a picture: the window stays on it, no decoder started");
    lock = false; mode = "cw";
    e.click(e.dec);
    e.doc.dispatchEvent(new e.ctx.CustomEvent("tr:tile", { detail: { id: "sstv", visible: true, collapsed: true } }));
    check(e.decOn(), "W6 the window collapsed: the decoder keeps listening");
    e.doc.dispatchEvent(new e.ctx.CustomEvent("tr:tile", { detail: { id: "spots", visible: false } }));
    check(e.decOn(), "W6 another panel removed: no matter");
    e.doc.dispatchEvent(new e.ctx.CustomEvent("tr:tile", { detail: { id: "sstv", visible: false } }));
    check(!e.decOn(), "W6 the window removed: the decoder stops");
  }
  // W7: CW decodes by default; the decoder never makes the page's audio
  {
    const e = await page({ decode: false, audio: false });
    eq([e.decOn(), e.workers], [false, 0], "W7 (CW not in view yet: Decode off, no model loading)");
    e.modem("ft8");
    eq([e.decOn(), e.workers], [false, 0], "W7 another mode in view: CW's decoder stays off");
    e.modem("cw");
    eq([e.decOn(), e.workers], [true, 1], "W7 CW in view: Decode on, the model loading");
    await e.settle();
    eq([e.ensured, e.nodes, e.addModules], [0, 0, 0], "W7 ... no audio made for it, no capture before the page has audio");
    e.ready();
    e.clock.advance(3000); await e.settle();
    eq([e.ensured, e.nodes], [0, 0], "W7 ticks with no audio: still nothing made");
    e.makeAudio();                                            // RX turned on: the page makes its audio
    e.clock.advance(1000); await e.settle();
    check(e.nodes === 1 && e.bused.length === 1 && e.bused[0] === e.node, "W7 the page's audio is there: the capture joins the RX bus at the next tick");
    e.clock.advance(3000); await e.settle();
    eq(e.nodes, 1, "W7 ... once");
    e.sec(); e.say("CQ");
    e.modem("cw");
    eq([e.decOn(), e.bubbles(), e.nodes, e.workers], [true, ["CQ"], 1, 1], "W7 CW in view while decoding: nothing starts again, the chat kept");
    e.click(e.dec);
    check(!e.decOn(), "W7 Decode off by hand");
    e.tileEv({ visible: true, collapsed: true }); e.tileEv({ visible: true, collapsed: false });
    e.clock.advance(3000);
    check(!e.decOn(), "W7 ... stays off (the window folded and opened again)");
    e.modem("rtty");
    check(!e.decOn(), "W7 ... and with another mode in view");
    e.modem("cw");
    check(e.decOn(), "W7 CW in view again: Decode on again");
  }
  {
    const e = await page({ decode: false, audio: false });
    e.modem("cw"); await e.settle();
    e.doc.getElementById("cwSend").value = "E";
    e.click(e.doc.getElementById("cwPlay"));
    await e.settle();
    eq([e.ensured, e.nodes], [1, 1], "W7 Play is a click: it makes the page's audio, and the decoder's capture joins");
    eq(e.started, [{ capture: true }], "W7 ... before the sidetone starts (the self-test hears all of it)");
  }
  {
    const e = await page({ decode: false, slowModule: true });
    e.modem("cw"); await e.settle();
    e.ready();
    e.clock.advance(3000); await e.settle();                 // three ticks while the module still loads
    eq([e.addModules, e.nodes], [1, 0], "W7 the capture's module loading: loaded once, whatever asks meanwhile");
    e.moduleLoaded(); await e.settle();
    eq([e.addModules, e.nodes, e.doc.getElementById("cwStatus").textContent], [1, 1, "ready"], "W7 ... then one capture, no failure said");
  }
  {
    const e = await page({ decode: false, addModuleFails: true });
    e.modem("cw"); await e.settle();
    eq([e.doc.getElementById("cwStatus").textContent, e.addModules, e.nodes], ["audio capture unavailable", 1, 0], "W7 the capture can't be made: said");
    e.clock.advance(5000); await e.settle();
    eq(e.addModules, 1, "W7 ... not tried again at every tick");
    e.click(e.dec); e.click(e.dec); await e.settle();
    eq(e.addModules, 2, "W7 ... until Decode is turned on again");
  }
  // W8: the chat scrolls inside the window
  {
    const e = await page();
    const out = e.doc.getElementById("cwOut");
    let html = out.innerHTML, writes = 0;
    Object.defineProperty(out, "innerHTML", { get: () => html, set: (v) => { html = String(v); writes++; } });
    Object.defineProperty(out, "scrollHeight", { get: () => 100 + html.length });   // the chat grows with its text
    out.clientHeight = 100;
    e.sec(); e.say("CQ CQ");
    check(writes > 0 && out.scrollTop >= out.scrollHeight - out.clientHeight, "W8 at its end: the chat follows new text");
    out.scrollTop = 0;                                        // scrolled back, to read
    e.say("DE N0CALL");
    eq([e.bubbles(), out.scrollTop], [["CQ CQ DE N0CALL"], 0], "W8 scrolled back: new text doesn't move it");
    out.scrollTop = out.scrollHeight - out.clientHeight;      // back at the end
    e.sec("K");
    check(out.scrollTop >= out.scrollHeight - out.clientHeight, "W8 back at the end: it follows again (the live preview too)");
    e.say("K");
    const w = writes;
    e.sec(""); e.sec("");
    eq(writes, w, "W8 nothing new: the chat isn't written again");
  }
  // W9: the real Modem window
  {
    const e = modemPage();
    check(e.decOn(), "W9 the page opening on CW (the window on the grid): Decode on, no click");
    e.click(e.dec);
    e.click(e.keys.ft8);
    check(!e.decOn(), "W9 Decode off, FT8 shown: off");
    e.click(e.keys.cw);
    check(e.decOn(), "W9 CW shown again: on");
    e.click(e.keys.cw);
    check(e.decOn(), "W9 the CW key again: no change");
    e.click(e.dec);
    e.tileEv({ visible: true, collapsed: true }); e.tileEv({ visible: true, collapsed: false });
    e.clock.advance(3000);
    check(!e.decOn(), "W9 Decode off, the window folded and opened: still off");
    e.layoutEv(); e.clock.advance(1000);
    check(!e.decOn(), "W9 a layout rebuilt with the window on the grid all along: still off");
    e.move(e.parking); e.tileEv({ visible: false });
    e.move(e.grid); e.tileEv({ visible: true });
    check(e.decOn(), "W9 the window removed and added again, showing CW: on");
    e.move(e.parking); e.tileEv({ visible: false });
    check(!e.decOn(), "W9 the window removed: off");
  }
  {
    const e = modemPage({ mode: "ft8" });
    check(!e.decOn(), "W9 the page opening on FT8: CW waits");
    e.click(e.keys.cw);
    check(e.decOn(), "W9 ... until CW is shown: on");
  }
  {
    const e = modemPage({ onGrid: false });
    check(!e.decOn(), "W9 the window off the grid: off");
    e.clock.advance(3000);
    check(!e.decOn(), "W9 ... still off (nothing in view)");
    e.move(e.grid); e.layoutEv();
    check(e.decOn(), "W9 a layout that brings the window onto the grid, showing CW: on");
  }
  console.log(failures ? `${failures} of ${checks} checks FAILED` : `ALL PASS (${checks} checks)`);
  process.exit(failures ? 1 : 0);
})().catch((err) => { console.log("FAIL (crash) " + (err && err.stack || err)); process.exit(1); });
