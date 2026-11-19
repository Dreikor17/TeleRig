/* TeleRig Spots panel: POTA activators, WWFF activators and DX cluster spots (HamQTH), newest first; a
 * click tunes the radio. The server fetches the sources (backend/spots.py: only while a page asks,
 * never more often than each source allows); this page asks GET /api/spots every POLL_MS while the
 * panel is on the grid and the page is visible, and filters the list itself.
 *
 *   tune: set_freq, then set_mode with the spot's mode in this radio's own names (CW -> CW or CW-USB,
 *   SSB -> USB / LSB by band, digital -> DATA-USB or USB, FM / AM), the same WS actions the
 *   frequency box and the mode keys send, so the server's rules apply (operators, no retune under
 *   someone else's transmission). A spot outside the radio's bands, or seen by a viewer, is shown
 *   but cannot be clicked. The spot clicked stays at the top ("Tuned"), whatever the filters and
 *   after the sources drop it, until the radio is tuned more than 3 kHz away from it.
 *
 * Text from the sources is shown with textContent only. window.SpotsPanel = { summary() } (layout.js).
 */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const list = $("spotsList"), status = $("spotsStatus"), modeSel = $("spotsMode"), bandChk = $("spotsBand");
  if (!list || !status) return;
  const srcBtns = [...document.querySelectorAll(".spots-src[data-src]")];

  const POLL_MS = 30000;
  const KEY = "telerig.spots";
  const SRC_LABEL = { pota: "POTA", wwff: "WWFF", dx: "DX" };
  let data = { spots: [], sources: {}, now: 0 };
  let timer = 0, inflight = false, lastFetch = 0;

  // ---- the filters, kept by this browser ----
  const filt = (function () {
    let f = null;
    try { f = JSON.parse(localStorage.getItem(KEY) || "null"); } catch (_) { f = null; }
    const src = f && Array.isArray(f.src) ? f.src.filter((s) => s in SRC_LABEL) : ["pota", "wwff", "dx"];
    return { src: src.length ? src : ["pota", "wwff", "dx"], mode: f && typeof f.mode === "string" ? f.mode : "",
             band: !!(f && f.band) };
  })();
  function keep() { try { localStorage.setItem(KEY, JSON.stringify(filt)); } catch (_) {} }

  // ---- the radio ----
  const ctl = () => window.RadioControl || null;
  function radioState() { try { return (ctl() && ctl().state()) || {}; } catch (_) { return {}; } }
  function profile() {
    const st = radioState();
    let rs = [];
    try { rs = (ctl() && ctl().radios()) || []; } catch (_) { rs = []; }
    const sel = $("radioSel");
    const id = (st.connected && st.radio) || (sel && sel.value) || "";
    return rs.find((p) => p.id === id) || null;
  }
  const bandOf = (p, hz) => (p && Array.isArray(p.bands) ? p.bands.find((b) => hz >= b.lo && hz <= b.hi) : null) || null;
  const canTune = () => { try { return !!TR.can("operate"); } catch (_) { return false; } };
  // the spot's mode in this radio's names; "" = leave the mode alone
  function radioMode(p, spot) {
    const have = (p && Array.isArray(p.modes)) ? p.modes : [];
    const pick = (...names) => names.find((n) => have.includes(n)) || "";
    const usb = spot.hz >= 10e6 || (spot.hz >= 5.25e6 && spot.hz <= 5.45e6);   // 60 m is USB too
    switch (spot.mode) {
      case "CW": return pick("CW", "CW-USB");               // a Yaesu's normal CW is CW-USB on every band
      case "SSB": return pick(usb ? "USB" : "LSB");
      case "DIGI": return pick("DATA-USB", "USB");
      case "FM": return pick("FM");
      case "AM": return pick("AM");
      default: return "";
    }
  }
  function tune(spot) {
    const c = ctl(), p = profile();
    if (!c || !canTune() || !bandOf(p, spot.hz)) return;
    c.send({ action: "set_freq", hz: spot.hz });
    const m = radioMode(p, spot);
    if (m && m !== radioState().mode_name) c.send({ action: "set_mode", mode: m });
    picked = { key: keyOf(spot), spot, hz: spot.hz, at: Date.now() };
    try { sessionStorage.setItem(PICK_KEY, JSON.stringify({ key: picked.key, spot, hz: spot.hz })); } catch (_) {}
    try { TR.srLive("Tuned to " + spot.call + ", " + fmtFreq(spot.hz)); } catch (_) {}
    render();
    const body = list && list.closest(".tile-body");         // the panel back at its top, where the pin is
    if (body) body.scrollTop = 0;
  }

  // ---- the spot you are on: the one clicked, pinned at the top until the radio is tuned away from it
  // (more than AWAY_HZ off the frequency it was tuned to; the first SETTLE_MS let the radio get there).
  // The newest copy of it is shown, so a station that moves shows its new frequency up there.
  const AWAY_HZ = 3000, SETTLE_MS = 3000, PICK_KEY = "telerig.spots.picked";
  const keyOf = (s) => s.src + "|" + s.call + "|" + (s.ref || "");
  let picked = null;
  try {                                                     // this tab's pin survives a reload
    const v = JSON.parse(sessionStorage.getItem(PICK_KEY) || "null");
    if (v && typeof v.key === "string" && v.spot && typeof v.spot.hz === "number") picked = { key: v.key, spot: v.spot, hz: v.hz, at: 0 };
  } catch (_) { picked = null; }
  function current() {
    if (!picked) return null;
    const fresh = (data.spots || []).find((s) => keyOf(s) === picked.key);
    if (fresh) picked.spot = fresh;
    const st = radioState();
    if (Date.now() - picked.at > SETTLE_MS && st.connected && st.freq && Math.abs(st.freq - picked.hz) > AWAY_HZ) {
      picked = null;
      try { sessionStorage.removeItem(PICK_KEY); } catch (_) {}
    }
    return picked ? picked.spot : null;
  }

  // ---- formatting ----
  function fmtFreq(hz) {                                   // 14.275.0 (kHz to 100 Hz, like a spot)
    const khz = Math.round(hz / 100) / 10;
    const mhz = Math.floor(khz / 1000), rest = (khz - mhz * 1000).toFixed(1).padStart(5, "0");
    return mhz + "." + rest;
  }
  function age(iso) {
    const t = Date.parse(iso);
    if (!isFinite(t)) return "";
    const s = Math.max(0, Math.round(((data.now ? data.now * 1000 : Date.now()) - t) / 1000));
    return s < 60 ? "now" : s < 3600 ? Math.round(s / 60) + "m" : Math.round(s / 3600) + "h";
  }
  const modeLabel = (m) => (m === "DIGI" ? "DATA" : m || "?");

  // ---- the list ----
  function shown() {
    const p = profile(), st = radioState();
    const here = filt.band && st.freq ? bandOf(p, st.freq) : null;
    return (data.spots || []).filter((s) => filt.src.includes(s.src)
      && (!filt.mode || (filt.mode === "FMAM" ? (s.mode === "FM" || s.mode === "AM") : s.mode === filt.mode))
      && (!filt.band || (here && s.hz >= here.lo && s.hz <= here.hi)));
  }
  function cell(cls, text) { const e = document.createElement("span"); e.className = cls; e.textContent = text; return e; }
  function render() {
    for (const b of srcBtns) {
      const on = filt.src.includes(b.dataset.src);
      b.classList.toggle("on", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    }
    if (modeSel && modeSel.value !== filt.mode) modeSel.value = filt.mode;
    if (bandChk) bandChk.checked = filt.band;
    const pin = current(), pinKey = pin ? keyOf(pin) : null;
    let rows = shown();
    if (pin) rows = [pin].concat(rows.filter((s) => keyOf(s) !== pinKey));   // first, whatever the filters
    const p = profile(), op = canTune();
    list.textContent = "";
    for (const s of rows) {
      const ok = op && !!bandOf(p, s.hz), on = keyOf(s) === pinKey;
      const row = document.createElement(ok ? "button" : "div");
      row.className = "spot spot-" + s.src + (ok ? "" : " off") + (on ? " picked" : "");
      if (on) row.setAttribute("aria-current", "true");
      if (ok) { row.type = "button"; row.addEventListener("click", () => tune(s)); }
      row.title = (on ? "Tuned to " : ok ? "Tune to " : (op ? "Not on this radio: " : "")) + s.call + " " + fmtFreq(s.hz) + " " + modeLabel(s.mode) +
        (s.ref ? " · " + s.ref : "") + (s.name ? " · " + s.name : "") + (s.note ? " · " + s.note : "") +
        (s.spotter ? " · spotted by " + s.spotter : "") + (on ? " · " + (SRC_LABEL[s.src] || "") : "");
      row.append(cell("sp-age", age(s.time)), cell("sp-freq", fmtFreq(s.hz)), cell("sp-mode", modeLabel(s.mode)),
                 cell("sp-call", s.call), cell("sp-ref", [s.ref, s.name].filter(Boolean).join(" ")),
                 cell("sp-src", on ? "Tuned" : SRC_LABEL[s.src] || ""));
      list.appendChild(row);
    }
    let msg = "";
    const src = data.sources || {};
    const bad = filt.src.filter((k) => src[k] && !src[k].ok).map((k) => SRC_LABEL[k]);
    if (!lastFetch) msg = "Loading spots…";
    else if (!rows.length) msg = (data.spots || []).length ? "No spots match the filters." : "No spots right now.";
    else msg = rows.length + (rows.length === 1 ? " spot" : " spots");
    if (bad.length) msg += " " + bad.join(", ") + (bad.length === 1 ? " is" : " are") + " not answering.";
    if (!op && rows.length) msg += " Operators can click a spot to tune.";
    if (status.textContent !== msg) status.textContent = msg;
  }

  // ---- polling: only while the panel is on the grid and the page is visible ----
  function visible() {
    let v = false;
    try { v = !!(window.RadioLayout && RadioLayout.visible && RadioLayout.visible("spots")); } catch (_) { v = false; }
    return v && !document.hidden;
  }
  async function poll(force) {
    if (inflight || !visible()) return;
    if (!force && Date.now() - lastFetch < POLL_MS - 500) return;
    inflight = true;
    try {
      const j = await TR.api("/api/spots?src=" + encodeURIComponent(filt.src.join(",")), { noRedirect: true });
      if (j && Array.isArray(j.spots)) data = j;
    } catch (_) {
      data = Object.assign({}, data, { sources: Object.fromEntries(filt.src.map((k) => [k, { ok: false }])) });
    } finally {
      inflight = false;
      lastFetch = Date.now();
      render();
    }
  }
  function schedule() {
    if (timer) { clearInterval(timer); timer = 0; }
    if (visible()) { poll(false); timer = setInterval(() => poll(false), 5000); }
  }

  for (const b of srcBtns) b.addEventListener("click", () => {
    const k = b.dataset.src, i = filt.src.indexOf(k);
    if (i >= 0) { if (filt.src.length > 1) filt.src.splice(i, 1); } else filt.src.push(k);
    keep(); render(); poll(true);
  });
  if (modeSel) modeSel.addEventListener("change", () => { filt.mode = modeSel.value; keep(); render(); });
  if (bandChk) bandChk.addEventListener("change", () => { filt.band = bandChk.checked; keep(); render(); });
  document.addEventListener("tr:tile", (e) => { if (e.detail && e.detail.id === "spots") schedule(); });
  document.addEventListener("tr:layout", schedule);
  document.addEventListener("visibilitychange", schedule);
  setInterval(render, 30000);                               // the ages move on
  // tuned away from the pinned spot? (redrawn only then, so keyboard focus in the list stays put)
  setInterval(() => { if (picked) { current(); if (!picked) render(); } }, 2000);
  render();
  schedule();

  window.SpotsPanel = { summary: () => (lastFetch ? shown().length + " spots" : "") };
})();
