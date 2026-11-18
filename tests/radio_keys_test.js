/* The lit BAND and MODE keys, tested on the REAL frontend (tr.js + app.js) in a Node vm on tests/fakedom.js.
 * No browser, no server, no socket, no radio: the "server" is a fake WebSocket this test writes frames
 * into and a fetch stub whose /api/radios answer the test releases by hand. Run by tests/t_c1.py (unit):
 *
 *     node tests/radio_keys_test.js     -> exit 0 and "ALL PASS", or exit 1 naming each failure
 *
 *  K1. a reload while a radio is connected: its state frame arrives BEFORE /api/radios answers (the
 *      keys don't exist yet). Once the profile renders, its band and mode keys are lit. (They used to
 *      stay dark until the freq or the mode changed: renderRadio rebuilt them unlit.)
 *  K2. the usual order (profiles first, then the state) lights them too, and a new freq / mode moves
 *      the light; a freq outside every band lights no band key.
 *  K3. nothing is lit from a state that isn't the rendered radio's own: a disconnected server's
 *      stale state, or another radio picked while disconnected.
 *  K4. the server switches the radio (G-4): the new profile's keys are lit from that same frame.
 */
"use strict";
const { makeEnv } = require("./fakedom");

let failures = 0, checks = 0;
function check(cond, msg) { checks++; if (!cond) { failures++; console.log("FAIL " + msg); } }
function eq(a, b, msg) { const ja = JSON.stringify(a), jb = JSON.stringify(b); check(ja === jb, msg + ": got " + ja + ", want " + jb); }

const PROFILES = [
  { id: "rig_a", make: "Test", name: "Rig A", has_scope: true, default_step: 100,
    bands: [{ name: "40m", lo: 7000000, hi: 7300000 }, { name: "20m", lo: 14000000, hi: 14350000 }],
    modes: ["LSB", "USB", "CW", "FM"], steps: [{ v: 10, label: "10 Hz" }, { v: 100, label: "100 Hz" }] },
  { id: "rig_b", make: "Test", name: "Rig B", has_scope: false, default_step: 1000,
    bands: [{ name: "20m", lo: 14000000, hi: 14350000 }, { name: "2m", lo: 144000000, hi: 148000000 }],
    modes: ["USB", "CW", "FM"], steps: [{ v: 1000, label: "1 kHz" }] },
];

function jsonResponse(status, body) { return { ok: status >= 200 && status < 300, status, json: async () => body }; }

// the page, booted as a reload: tr.js + app.js run, the socket opens, /api/radios is asked for and waits
function pageEnv(status) {
  const sockets = [];
  function FakeWS() { this.readyState = 1; this.sent = []; this.send = (d) => this.sent.push(d); this.close = () => {}; sockets.push(this); }
  let release = null;
  const radiosAnswer = new Promise((r) => { release = () => r(jsonResponse(200, { radios: PROFILES })); });
  const fetch = (url) => (String(url).split("?")[0] === "/api/radios" ? radiosAnswer
    : Promise.resolve(jsonResponse(404, { ok: false, error: "not_found" })));
  const e = makeEnv({ WebSocket: FakeWS, fetch });
  e.ctx.Scope = function () { return new Proxy({ meta: {} }, { get: (t, k) => (k in t ? t[k] : () => []) }); };
  for (const id of ["bandRow", "modeRow", "radioSel", "step", "toasts"]) e.el("div", { id });
  e.load("tr.js");
  e.load("app.js");
  const ws = sockets[sockets.length - 1];
  e.frame = (f) => ws.onmessage({ data: JSON.stringify(f) });
  e.frame({ type: "hello", v: 1, conn: 3, user: "ME", role: "operator", perms: ["view", "listen", "operate"],
            status: status || {}, tx: { owner: null }, hb_interval: 2 });
  e.releaseRadios = async () => { release(); for (let i = 0; i < 6; i++) await e.flush(); };
  e.lit = (kind) => e.doc.querySelectorAll(".key." + kind).filter((b) => b.classList.contains("active"))
    .map((b) => b.dataset[kind]);
  e.keys = (kind) => e.doc.querySelectorAll(".key." + kind).map((b) => b.dataset[kind]);
  return e;
}
const state = (o) => Object.assign({ type: "state", connected: true, radio: "rig_a", freq: 14074000, mode_name: "USB",
  filter: 1, main: { freq: o && o.freq || 14074000 }, sub: { freq: 0 }, active_band: "main" }, o || {});

async function main() {
  // K1: the reload race
  {
    const e = pageEnv({ connected: true, radio: "rig_b" });
    e.frame(state({ radio: "rig_b", freq: 14074000, mode_name: "USB" }));
    eq(e.keys("band"), [], "K1: no band keys before /api/radios answers");
    await e.releaseRadios();
    eq(e.keys("band"), ["20m", "2m"], "K1: the connected radio's profile is rendered");
    eq(e.lit("band"), ["20m"], "K1: its band key is lit with no further state frame");
    eq(e.lit("mode"), ["USB"], "K1: its mode key is lit with no further state frame");
    e.frame(state({ radio: "rig_b", freq: 145500000, mode_name: "FM" }));
    eq([e.lit("band"), e.lit("mode")], [["2m"], ["FM"]], "K1: the next change moves the light");
  }
  // K2: the usual order, and the light following the radio
  {
    const e = pageEnv({ connected: true, radio: "rig_a" });
    await e.releaseRadios();
    eq(e.lit("band").concat(e.lit("mode")), [], "K2: nothing lit before any state");
    e.frame(state({ freq: 7074000, mode_name: "LSB" }));
    eq([e.lit("band"), e.lit("mode")], [["40m"], ["LSB"]], "K2: state after the profile");
    e.frame(state({ freq: 14200000, mode_name: "CW" }));
    eq([e.lit("band"), e.lit("mode")], [["20m"], ["CW"]], "K2: a new freq and mode move the light");
    e.frame(state({ freq: 10100000, mode_name: "CW" }));
    eq([e.lit("band"), e.lit("mode")], [[], ["CW"]], "K2: a freq outside every band lights no band key");
  }
  // K3: never lit from a state that isn't this radio's own
  {
    const e = pageEnv({ connected: false, radio: null });
    e.frame(state({ connected: false, radio: null, freq: 14074000, mode_name: "USB" }));
    await e.releaseRadios();
    eq(e.lit("band").concat(e.lit("mode")), [], "K3: a disconnected server's stale state lights nothing on render");
  }
  {
    const e = pageEnv({ connected: false, radio: null });
    await e.releaseRadios();
    e.frame(state({ connected: false, radio: null, freq: 14074000, mode_name: "USB" }));
    e.doc.getElementById("radioSel").value = "rig_b";
    e.doc.getElementById("radioSel").dispatchEvent(new e.Event("change", { bubbles: true }));
    for (let i = 0; i < 4; i++) await e.flush();
    eq(e.keys("band"), ["20m", "2m"], "K3: the radio picked while disconnected is rendered");
    eq(e.lit("band").concat(e.lit("mode")), [], "K3: ...with no band or mode lit from the old radio's state");
  }
  // K4: the server switches the radio (another admin): the new keys are lit from that frame
  {
    const e = pageEnv({ connected: true, radio: "rig_a" });
    await e.releaseRadios();
    e.frame(state({ freq: 7074000, mode_name: "LSB" }));
    e.frame(state({ radio: "rig_b", freq: 145500000, mode_name: "FM" }));
    eq(e.keys("band"), ["20m", "2m"], "K4: the server's radio is rendered");
    eq([e.lit("band"), e.lit("mode")], [["2m"], ["FM"]], "K4: its band and mode keys are lit");
  }
  console.log(failures ? `${failures} of ${checks} checks FAILED` : `ALL PASS (${checks} checks)`);
  process.exit(failures ? 1 : 0);
}
main().catch((err) => { console.log("FAIL (crash) " + (err && err.stack || err)); process.exit(1); });
