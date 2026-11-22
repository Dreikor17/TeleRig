/* The page themes (night, day, radio screen), tested on the REAL tr.js and waterfall.js in a Node vm
 * (tests/fakedom.js). No browser, no server, no radio. Run by tests/t_af_scope.py (unit group):
 *
 *     node tests/theme_test.js  -> exit 0 and "ALL PASS", or exit 1 naming each failure
 *
 *  A. the theme button steps night -> day -> radio screen -> night: <html> / <body> classes, the
 *     stored choice, the icon, the button's label and the menu item's text, one tr:theme event each
 *  B. the stored choice is applied at load (an unknown value is night); applyTheme(true / false) still
 *     means day / night; another tab's choice is followed and not stored again
 *  C. the UTC clock: "hh:mmZ" from UTC, with a datetime attribute
 *  D. the scope: night and day draw exactly the colours they always did; the radio screen recolours
 *     the waterfall floor, spectrum, trace, passband and tuned marker; unknown names are night
 */
"use strict";
const { makeEnv } = require("./fakedom");

let failures = 0, checks = 0;
function check(cond, msg) { checks++; if (!cond) { failures++; console.log("FAIL " + msg); } }
function eq(a, b, msg) { const ja = JSON.stringify(a), jb = JSON.stringify(b); check(ja === jb, msg + ": got " + ja + ", want " + jb); }

// ---- a page with the theme controls -----------------------------------------------------------------
function page(stored) {
  const winListeners = {};
  const e = makeEnv({ localStorage: stored === undefined ? {} : { "telerig.theme": stored } });
  e.ctx.addEventListener = (t, fn) => { (winListeners[t] = winListeners[t] || []).push(fn); };
  for (const id of ["themeBtn", "umTheme"]) {
    const b = e.el("button", { id });
    const svg = e.el("svg", {}, b);
    e.el("use", { href: "#i-moon" }, svg);
    if (id === "umTheme") { const s = e.el("span", {}, b); s.textContent = "Theme"; }
  }
  e.el("time", { id: "utcClock" });
  for (const id of ["userBtn", "userMenu", "topbar"]) e.el("div", { id });
  e.events = [];
  e.doc.addEventListener("tr:theme", (ev) => e.events.push(ev.detail));
  e.load("tr.js");
  e.domReady();
  e.TR = e.ctx.TR;
  e.html = e.doc.documentElement;
  e.btn = (id) => e.doc.querySelector("#" + id);
  e.icon = (id) => e.btn(id).querySelector("use").getAttribute("href");
  e.stored = () => e.ctx.localStorage.getItem("telerig.theme");
  e.storageEvent = (v) => { for (const fn of winListeners.storage || []) fn({ key: "telerig.theme", newValue: v }); };
  return e;
}
const classes = (el) => ["day", "screen"].filter((c) => el.classList.contains(c));

// ---- A. the button steps through the three themes ---------------------------------------------------------
{
  const e = page();
  eq(e.TR.theme(), "night", "A no stored choice: night");
  eq(classes(e.html), [], "A night: no class on <html>");
  eq(e.icon("themeBtn"), "#i-moon", "A night icon");
  eq(e.btn("themeBtn").getAttribute("aria-label"), "Theme: Night. Switch to Day", "A night label names the next theme");
  e.events.length = 0;
  const steps = [["day", "#i-sun", "Day", "Radio screen"], ["screen", "#i-radio", "Radio screen", "Night"],
                 ["night", "#i-moon", "Night", "Day"]];
  for (const [want, icon, name, next] of steps) {
    e.btn("themeBtn").click();
    eq(e.TR.theme(), want, "A click -> " + want);
    eq(classes(e.html), want === "night" ? [] : [want], "A <html> class for " + want);
    eq(classes(e.doc.body), want === "night" ? [] : [want], "A <body> class for " + want);
    eq(e.stored(), want, "A stored " + want);
    eq(e.icon("themeBtn"), icon, "A button icon for " + want);
    eq(e.icon("umTheme"), icon, "A menu icon for " + want);
    eq(e.btn("themeBtn").getAttribute("aria-label"), "Theme: " + name + ". Switch to " + next, "A label for " + want);
    eq(e.btn("themeBtn").title, "Theme: " + name + ". Switch to " + next, "A title for " + want);
    eq(e.btn("umTheme").querySelector("span").textContent, "Theme: " + name, "A menu text for " + want);
  }
  eq(e.events, [{ day: true, theme: "day" }, { day: false, theme: "screen" }, { day: false, theme: "night" }],
     "A one tr:theme event per change, with the theme name (day stays for old listeners)");
  e.btn("umTheme").click();
  eq(e.TR.theme(), "day", "A the phone menu item steps the same way");
  eq([e.TR.nextTheme("night"), e.TR.nextTheme("day"), e.TR.nextTheme("screen"), e.TR.nextTheme("bogus")],
     ["day", "screen", "night", "day"], "A nextTheme order (an unknown theme counts as night)");
}

// ---- B. stored choice, legacy booleans, other tabs ------------------------------------------------------
{
  const s = page("screen");
  eq(s.TR.theme(), "screen", "B stored radio screen is applied at load");
  eq(s.icon("themeBtn"), "#i-radio", "B ... with its icon");
  eq(page("day").TR.theme(), "day", "B stored day is applied at load");
  eq(page("purple").TR.theme(), "night", "B an unknown stored value is night");
  const e = page();
  e.TR.applyTheme(true);
  eq(e.TR.theme(), "day", "B applyTheme(true) = day");
  check(e.TR.isDay(), "B isDay() in day");
  e.TR.applyTheme(false);
  eq(e.TR.theme(), "night", "B applyTheme(false) = night");
  e.TR.applyTheme("screen", false);
  eq([e.TR.theme(), e.stored()], ["screen", "night"], "B persist=false changes the page, not the stored choice");
  check(!e.TR.isDay(), "B isDay() is false in the radio screen");
  e.storageEvent("day");
  eq([e.TR.theme(), e.stored()], ["day", "night"], "B another tab chose day: followed, not stored again");
  e.storageEvent("screen");
  eq(e.TR.theme(), "screen", "B another tab chose the radio screen");
}

// ---- C. the UTC clock ----------------------------------------------------------------------------------
{
  const e = page();
  eq(e.TR.utcClock(Date.UTC(2026, 10, 18, 21, 6, 59)), "21:06Z", "C hh:mmZ in UTC");
  eq(e.btn("utcClock").textContent, "21:06Z", "C shown in #utcClock");
  eq(e.btn("utcClock").getAttribute("datetime"), "2026-11-18T21:06:59.000Z", "C with a datetime attribute");
  eq(e.TR.utcClock(Date.UTC(2026, 0, 2, 3, 4)), "03:04Z", "C zero-padded");
}

// ---- D. the scope's colours ------------------------------------------------------------------------------
const w = makeEnv();
w.load("waterfall.js");
const { Scope } = w.ctx;
function rec() {
  const calls = [], grads = [];
  const t = {
    fillStyle: null, strokeStyle: null,
    fillRect: (...a) => calls.push(["fillRect", t.fillStyle, ...a]),
    clearRect: () => {}, beginPath: () => {}, moveTo: () => {}, lineTo: () => {}, closePath: () => {},
    stroke: () => calls.push(["stroke", t.strokeStyle]), fill: () => calls.push(["fill", t.fillStyle]),
    fillText: (s) => calls.push(["fillText", t.fillStyle, String(s)]), measureText: (s) => ({ width: String(s).length * 6 }),
    createLinearGradient: () => { const g = { stops: [] }; g.addColorStop = (o, c) => g.stops.push([o, c]); grads.push(g); return g; },
    putImageData: () => {}, drawImage: () => {},
  };
  return { ctx: t, calls, grads };
}
function scope() {
  const s = rec(), wf = rec(), ov = rec();
  const sc = Object.create(Scope.prototype);
  Object.assign(sc, { W: 400, specH: 120, wfH: 160, scopeH: 280, sctx: s.ctx, wctx: wf.ctx, octx: ov.ctx, stctx: null, strip: null,
                      wrap: { clientWidth: 400 }, rowBuf: { data: new Uint8ClampedArray(400 * 4) }, maxHold: null,
                      meta: { mode: 1, lower: 7.0039e6, upper: 7.0539e6, tuned: 7.0289e6, filterBw: 2400 },
                      bandplan: null, showBandplan: false, opMode: "LSB", lastData: null,
                      theme: "night", pal: Scope.PALETTES.night });
  sc.lut = new Uint8Array(256 * 3);
  return { sc, s, wf, ov };
}
function draw(t) {
  const x = scope();
  if (t) x.sc.setTheme(t);
  x.sc.lastData = Array.from({ length: 400 }, (_, i) => 30 + (i % 17));
  x.s.calls.length = 0; x.ov.calls.length = 0; x.s.grads.length = 0;
  x.sc.drawSpectrum(x.sc.lastData);
  x.sc.drawOverlay();
  return {
    bg: x.s.calls.find((c) => c[0] === "fillRect")[1],
    strokes: x.s.calls.filter((c) => c[0] === "stroke").map((c) => c[1]),
    fill: x.s.grads[0] && x.s.grads[0].stops.map((st) => st[1]),
    overlay: x.ov.calls.filter((c) => c[0] === "stroke" || c[0] === "fill" || c[0] === "fillRect").map((c) => c[1]),
    text: x.ov.calls.filter((c) => c[0] === "fillText").map((c) => c[1]),
  };
}
{
  const P = Scope.PALETTES;
  check(P.day === P.night, "D day and night share the instrument colours");
  // the colours the scope always drew (before themes existed): night and day must not move; since 11-21 the
  // tuned label stands on a plate of the scope's own colour (the last overlay colour)
  const before = { bg: "#020c14", strokes: ["rgba(20,90,130,.45)", "#5ff0c8", "rgba(255,200,120,.55)"],
                   fill: ["rgba(60,210,170,.55)", "rgba(20,90,120,.10)"],
                   overlay: ["rgba(90,200,255,.16)", "rgba(120,220,255,.5)", "#ff9a2e", "#ff9a2e", "rgba(2,12,20,.85)"], text: ["#ffb15a"] };
  eq(draw(), before, "D night draws exactly the old colours");
  eq(draw("day"), before, "D day draws exactly the old colours");
  eq(draw("purple"), before, "D an unknown theme draws night");
  eq(draw("screen"), { bg: "#000000", strokes: ["rgba(70,80,106,.55)", "#ffffff", "rgba(156,203,255,.5)"],
                       fill: ["rgba(220,228,245,.55)", "rgba(140,150,175,.10)"],
                       overlay: ["rgba(255,255,255,.12)", "rgba(255,255,255,.45)", "#3bd16f", "#3bd16f", "rgba(0,0,0,.85)"], text: ["#5be38a"] },
     "D the radio screen: black spectrum, white trace, green tuned marker");
  // the waterfall colormap and floor
  const x = scope();
  x.sc.setTheme("screen");
  eq(Array.from(x.sc.lut.slice(0, 3)), [12, 26, 102], "D radio screen: the waterfall floor is deep blue");
  eq(Array.from(x.sc.lut.slice(255 * 3, 256 * 3)), [255, 240, 230], "D ... and the strongest signal near white");
  eq(x.wf.calls.filter((c) => c[0] === "fillRect").map((c) => c[1]), ["#0c1a66"], "D the waterfall restarts on the new floor");
  eq(x.sc.theme, "screen", "D the scope knows its theme");
  const n = x.wf.calls.length;
  x.sc.setTheme("screen");
  eq(x.wf.calls.length, n, "D the same theme again repaints nothing");
  x.sc.setTheme("day");
  eq(Array.from(x.sc.lut.slice(0, 3)), [2, 6, 18], "D back to day: the old navy floor");
  eq(x.wf.calls.filter((c) => c[0] === "fillRect").map((c) => c[1]).pop(), "#020c14", "D ... and the old waterfall floor");
  // the LUT itself for night is the one the scope always had (spot-check the stops)
  const y = scope();
  y.sc.setTheme("screen"); y.sc.setTheme("night");
  const at = (i) => Array.from(y.sc.lut.slice(i * 3, i * 3 + 3));
  eq([at(0), at(255)], [[2, 6, 18], [255, 245, 230]], "D night colormap ends unchanged");
}

console.log(failures ? `${failures} of ${checks} checks FAILED` : `ALL PASS (${checks} checks)`);
process.exit(failures ? 1 : 0);
