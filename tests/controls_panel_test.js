/* The Controls panel's rows that come and go per radio and per mode (frontend/app.js), on the REAL tr.js +
 * app.js in a Node vm on tests/fakedom.js. The markup is built here as index.html has it (the parts this test
 * reads), /api/radios answers with profiles shaped like backend/profiles.py's to_json(), and state frames come
 * in on a fake socket. No browser, no server, no radio; nothing here transmits. Run by tests/t_c1.py (unit):
 *
 *     node tests/controls_panel_test.js   -> exit 0 and "ALL PASS", or exit 1 naming each failure
 *
 *  C1. the manual notch's width keys: an Icom's WIDE / MID / NAR; a Yaesu's WIDE and NAR (it has no MID),
 *      their row hidden until the radio has answered its IF NOTCH WIDTH item (state mnotch_w_ok) and again
 *      when it answers something else; the key lit is the radio's width; a click sends mnotch_w with that
 *      width; a radio without the manual notch: no row
 *  C2. a Yaesu's WIDTH, CONTOUR and APF rows each in the modes they work in, their group only while one of
 *      them shows (FM: none; USB: WIDTH and CONTOUR; AM: CONTOUR; CW: all three); an Icom: none of it
 *  C3. the scan / FAST row (a Yaesu's ext_ops) and the antenna tuner row (has_tuner), per radio
 */
"use strict";
const { makeEnv } = require("./fakedom");

let failures = 0, checks = 0;
function check(cond, msg) { checks++; if (!cond) { failures++; console.log("FAIL " + msg); } }
function eq(a, b, msg) { const ja = JSON.stringify(a), jb = JSON.stringify(b); check(ja === jb, msg + ": got " + ja + ", want " + jb); }

const DSP = ["nb", "nr", "anotch", "mnotch"];
const caps = (o) => Object.assign({ rx_dsp: DSP, tx_funcs: ["comp", "vox", "mon"], meters: ["S", "PO"], mnotch_widths: [0, 1, 2],
                                     ext_ops: false, tuner: false, ptt: true }, o);
const PROFILES = [
  { id: "icom", make: "Icom", name: "Icom", protocol: "civ", has_scope: true, has_tuner: true, default_step: 10,
    bands: [{ name: "20", lo: 14000000, hi: 14350000 }], modes: ["USB", "CW", "FM"], steps: [{ v: 10, label: "10 Hz" }],
    capabilities: caps({ tuner: true }) },
  { id: "yaesu", make: "Yaesu", name: "Yaesu", protocol: "yaesu", has_scope: false, has_tuner: true, default_step: 10,
    bands: [{ name: "20", lo: 14000000, hi: 14350000 }], modes: ["USB", "AM", "CW-USB", "FM"], steps: [{ v: 10, label: "10 Hz" }],
    capabilities: caps({ mnotch_widths: [0, 2], ext_ops: true, tuner: true }) },
  { id: "old", make: "Icom", name: "Old", protocol: "civ", has_scope: false, has_tuner: false, default_step: 10,
    bands: [{ name: "20", lo: 14000000, hi: 14350000 }], modes: ["USB"], steps: [{ v: 10, label: "10 Hz" }],
    capabilities: caps({ rx_dsp: [], tx_funcs: [], meters: [], tuner: false }) },
];
const jsonResponse = (status, body) => ({ ok: status >= 200 && status < 300, status, json: async () => body });

async function page() {
  const sockets = [];
  function FakeWS() { this.readyState = 1; this.sent = []; this.send = (d) => this.sent.push(JSON.parse(d)); this.close = () => {}; sockets.push(this); }
  const fetch = (url) => Promise.resolve(String(url).split("?")[0] === "/api/radios"
    ? jsonResponse(200, { radios: PROFILES }) : jsonResponse(404, { ok: false, error: "not_found" }));
  const e = makeEnv({ WebSocket: FakeWS, fetch });
  for (const id of ["bandRow", "modeRow", "toasts"]) e.el("div", { id });
  e.el("select", { id: "radioSel" });
  // the DSP tab's RX DSP group: the notch keys and their width row (index.html)
  const dsp = e.el("div", { class: "grp", id: "dspGrp" });
  e.el("button", { class: "key tog", "data-act": "rxfunc", "data-fn": "mnotch", id: "mnotchBtn" }, dsp);
  e.mnwRow = e.el("div", { class: "row step-row", id: "mnwRow" }, dsp);
  e.mnw = [0, 1, 2].map((w) => e.el("button", { class: "key mnw", "data-act": "mnotch_w", "data-width": String(w) }, e.mnwRow));
  // a Yaesu's WIDTH / CONTOUR / APF group, its CW group, the VFO's scan row, the TX group's tuner row
  const shape = e.el("div", { class: "grp", id: "shapeGrp" });
  for (const id of ["widthRow", "contourRow", "apfRow"]) e.el("div", { class: "row", id }, shape);
  e.el("div", { class: "grp", id: "cwGrp" });
  e.el("div", { class: "row", id: "opsRow" });
  e.el("div", { class: "row", id: "tunerRow" });
  e.load("tr.js");
  e.load("waterfall.js");
  e.ctx.Scope = function () { return new Proxy({ meta: {} }, { get: (t, k) => (k in t ? t[k] : () => []) }); };
  e.load("app.js");
  const ws = sockets[sockets.length - 1];
  e.frame = (f) => ws.onmessage({ data: JSON.stringify(f) });
  e.frame({ type: "hello", v: 1, conn: 3, user: "ME", role: "operator", perms: ["view", "listen", "operate", "tx"],
            status: {}, tx: { owner: null }, hb_interval: 2 });
  for (let i = 0; i < 8; i++) await e.flush();
  e.$ = (id) => e.doc.getElementById(id);
  e.shown = (el) => el.style.display !== "none" && !el.hidden;
  e.keys = () => e.mnw.filter((b) => b.style.display !== "none").map((b) => b.textContent || ["WIDE", "MID", "NAR"][+b.dataset.width]);
  e.lit = () => e.mnw.filter((b) => b.classList.contains("active")).map((b) => +b.dataset.width);
  e.state = async (o) => {
    e.frame(Object.assign({ type: "state", connected: true, radio: "icom", freq: 14074000, mode_name: "USB", filter: 1,
      main: { freq: 14074000 }, sub: { freq: 0 }, active_band: "main", mnotch_w: 0 }, o));
    for (let i = 0; i < 4; i++) await e.flush();
  };
  e.sent = () => ws.sent.filter((m) => m.action === "mnotch_w");
  e.click = (el) => el.dispatchEvent(new e.Event("click", { bubbles: true }));
  return e;
}

(async function main() {
  // C1: the notch width keys
  {
    const e = await page();
    await e.state({ radio: "icom", mnotch_w: 1 });
    eq([e.keys(), e.shown(e.mnwRow), e.lit()], [["WIDE", "MID", "NAR"], true, [1]], "C1 an Icom: WIDE / MID / NAR, MID lit as the radio says");
    e.click(e.mnw[2]);
    eq(e.sent(), [{ action: "mnotch_w", width: 2 }], "C1 ... a click on NAR sends its width");
    await e.state({ radio: "yaesu", mnotch_w: 0, mnotch_w_ok: false });
    eq([e.keys(), e.shown(e.mnwRow)], [["WIDE", "NAR"], false], "C1 a Yaesu: WIDE and NAR (no MID); the row hidden until its IF NOTCH WIDTH answers");
    await e.state({ radio: "yaesu", mnotch_w: 2, mnotch_w_ok: true });
    eq([e.shown(e.mnwRow), e.lit()], [true, [2]], "C1 ... answered: the row, NAR lit");
    {
      const n = e.sent().length;
      e.click(e.mnw[0]);
      eq(e.sent().slice(n), [{ action: "mnotch_w", width: 0 }], "C1 ... a click on WIDE sends width 0");
    }
    await e.state({ radio: "yaesu", mnotch_w: 2, mnotch_w_ok: false });
    eq(e.shown(e.mnwRow), false, "C1 ... answered with something else: hidden again");
    await e.state({ radio: "old" });
    eq(e.shown(e.mnwRow), false, "C1 a radio without the manual notch: no row");
    await e.state({ radio: "icom" });
    eq([e.keys(), e.shown(e.mnwRow)], [["WIDE", "MID", "NAR"], true], "C1 back on the Icom (no mnotch_w_ok in its state): its three keys");
  }
  // C2: a Yaesu's WIDTH / CONTOUR / APF rows
  {
    const e = await page();
    const rows = () => ["widthRow", "contourRow", "apfRow"].filter((id) => e.shown(e.$(id)));
    const by = async (mode) => { await e.state({ radio: "yaesu", mode_name: mode }); return [rows(), e.shown(e.$("shapeGrp"))]; };
    eq([await by("FM"), await by("USB"), await by("AM"), await by("CW-USB")],
       [[[], false], [["widthRow", "contourRow"], true], [["contourRow"], true], [["widthRow", "contourRow", "apfRow"], true]],
       "C2 a Yaesu: FM none (the group hidden), USB WIDTH + CONTOUR, AM CONTOUR, CW all three");
    eq(e.shown(e.$("cwGrp")), true, "C2 ... its CW group in CW");
    await e.state({ radio: "icom", mode_name: "CW" });
    eq([rows(), e.shown(e.$("shapeGrp")), e.shown(e.$("cwGrp"))], [[], false, false], "C2 an Icom: none of it");
  }
  // C3: the scan / FAST row and the tuner row
  {
    const e = await page();
    await e.state({ radio: "yaesu" });
    eq([e.shown(e.$("opsRow")), e.shown(e.$("tunerRow"))], [true, true], "C3 a Yaesu: the scan row, the tuner row");
    await e.state({ radio: "icom" });
    eq([e.shown(e.$("opsRow")), e.shown(e.$("tunerRow"))], [false, true], "C3 an Icom with a tuner: no scan row, the tuner row");
    await e.state({ radio: "old" });
    eq([e.shown(e.$("opsRow")), e.shown(e.$("tunerRow"))], [false, false], "C3 a radio with neither: neither");
  }
  console.log(failures ? `${failures} of ${checks} checks FAILED` : `ALL PASS (${checks} checks)`);
  process.exit(failures ? 1 : 0);
})().catch((err) => { console.log("FAIL (crash) " + (err && err.stack || err)); process.exit(1); });
