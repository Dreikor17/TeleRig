/* TeleRig Spots panel: POTA activators, WWFF activators and DX cluster spots (HamQTH), newest first; a
 * click tunes the radio. The server fetches the sources (backend/spots.py: only while a page asks,
 * never more often than each source allows); this page asks GET /api/spots every POLL_MS while the
 * live list is in view (the panel on the grid, the page visible, the LIVE key chosen), and filters the
 * list itself.
 *
 *   tune: set_freq, then the spot's mode in this radio's own names (CW -> CW or CW-USB, SSB -> USB /
 *   LSB by band, digital -> DATA-USB, or USB with the Icom DATA switch on, FM / AM; SSTV, a category of
 *   its own, as SSB) and the filter for that mode (FILTER below: an Icom FIL slot, a Yaesu NAR off + WIDTH; the IC-728 has no filter
 *   command, so the mode only, and its CW-NAR stays). Each is sent only when the radio is not there
 *   already, with the same WS actions the frequency box, the mode / DATA keys and the RX filter keys
 *   send, so the server's rules apply (operators, no retune under someone else's transmission). A
 *   spot whose mode is not known tunes the frequency only. A spot outside the radio's bands, or seen
 *   by a viewer, is shown but cannot be clicked. A CW or SSTV spot shows that mode in the Modem window
 *   too (modemTo below). The spot clicked stays at the top ("Tuned"), whatever
 *   the filters and after the sources drop it, until the radio is tuned more than 3 kHz away from it.
 *
 * Three views, the keys at the top (this browser keeps the choice with the filters):
 *   LIVE     the spots, as above.
 *   CALLING  the fixed frequencies of callfreqs.js (each band's calling frequencies and FT8 dial
 *            frequency, SSTV, W1AW, a few things to listen to) that are on this radio's bands, by band,
 *            lowest first, then the time signals where the radio receives them. A click tunes as a
 *            spot's does; a row with a Modem mode (CW, FT8, SSTV) shows that mode in the Modem window too.
 *            Nothing is asked of the server while it shows.
 *   MEMORY   the operator's own frequencies for this radio, lowest first, kept by the server for the
 *            account (GET / POST / DELETE /api/me/memories), so every device has them. A radio the account
 *            has not had the presets for (GET's seeded: popular SSB frequencies, backend
 *            memory_store.PRESETS, at 2.4 kHz) gets them once (POST /api/me/memories/presets): they are
 *            memories like any other, and one deleted never comes back. SAVE takes the
 *            radio as it is (frequency, the mode by its own name, the filter as the radio says it: an
 *            Icom's FIL slot and DATA switch, a Yaesu's NAR and WIDTH) and asks for a name: the
 *            frequency is filled in and selected, so typing replaces it; SAVE again (or Enter) keeps
 *            it, Esc or the X drops it. A click on a memory brings all of that back, with the same
 *            actions a spot sends, each only when the radio is not there already; a memory may be
 *            anywhere the radio receives (freqentry.js's coverage), not only on its ham bands. In FM a
 *            memory also keeps the repeater shift and tone (DV: the shift), as far as the radio has them:
 *            simplex / + / -, the offset where the radio can set it (the IC-9700), TONE / TSQL with the
 *            CTCSS tone, DCS with its code (Yaesu); a click sets them every time, after the mode. Its X
 *            deletes it at the second tap (the first arms it for 4 s). Operators only: a viewer has no
 *            memories.
 *   In CALLING and MEMORY the two filters (mode, this band) apply, and the row the radio is on (within
 *   ON_HZ) is marked "Tuned", however it got there. The mark follows the radio in place: nothing is
 *   pinned or moved, and a row with the keyboard focus keeps it.
 *
 * Text from the sources and the names of memories are shown with textContent only.
 * window.SpotsPanel = { summary(), view() } (layout.js), here(hz) (app.js: the VFO names the calling
 * frequency the radio is on).
 */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const list = $("spotsList"), status = $("spotsStatus"), modeSel = $("spotsMode"), bandChk = $("spotsBand");
  if (!list || !status) return;
  const srcBtns = [...document.querySelectorAll(".spots-src[data-src]")];
  const VIEWS = ["live", "call", "mem"];                    // the keys' order; the first is the default
  const tabs = { live: $("spotsTabLive"), call: $("spotsTabCall"), mem: $("spotsTabMem") };
  const hasTabs = VIEWS.every((v) => tabs[v]);
  const credits = { live: $("spotsCredit"), call: $("callCredit") };
  const memForm = $("memForm"), memName = $("memName"), memSave = $("memSave"), memCancel = $("memCancel");
  // the list scrolls inside the panel, under its keys and filters (style.css .tile-spots): each view keeps
  // its own place in it

  const POLL_MS = 30000;
  const KEY = "telerig.spots";
  const SRC_LABEL = { pota: "POTA", wwff: "WWFF", dx: "DX" };
  let data = { spots: [], sources: {}, now: 0 };
  let timer = 0, inflight = false, lastFetch = 0;

  // ---- the view and the filters, kept by this browser ----
  const filt = (function () {
    let f = null;
    try { f = JSON.parse(localStorage.getItem(KEY) || "null"); } catch (_) { f = null; }
    const src = f && Array.isArray(f.src) ? f.src.filter((s) => s in SRC_LABEL) : ["pota", "wwff", "dx"];
    return { src: src.length ? src : ["pota", "wwff", "dx"], mode: f && typeof f.mode === "string" ? f.mode : "",
             band: !!(f && f.band), view: hasTabs && f && VIEWS.includes(f.view) ? f.view : VIEWS[0] };
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
  const toast = (msg, kind) => { try { TR.toast(msg, kind); } catch (_) {} };
  const say = (msg) => { try { TR.srLive(msg); } catch (_) {} };
  // the spot's mode in this radio's names; "" = leave the mode alone. SSB takes the sideband the source
  // named (spot.sub "USB" / "LSB"), else the band's: LSB below 10 MHz, USB above and on 60 m.
  function radioMode(p, spot) {
    const have = (p && Array.isArray(p.modes)) ? p.modes : [];
    const pick = (...names) => names.find((n) => have.includes(n)) || "";
    const usb = spot.hz >= 10e6 || (spot.hz >= 5.25e6 && spot.hz <= 5.45e6);   // 60 m is USB too
    const sb = spot.sub === "USB" || spot.sub === "LSB" ? spot.sub : "";
    switch (spot.mode) {
      case "CW": return pick("CW", "CW-USB");               // a Yaesu's normal CW is CW-USB on every band
      case "SSB": return pick(sb || (usb ? "USB" : "LSB"));
      // SSTV in the sideband it is sent in; a Yaesu in its data mode (FILTER below says why)
      case "SSTV": return p && p.protocol === "yaesu" ? pick(usb ? "DATA-USB" : "DATA-LSB", usb ? "USB" : "LSB") : pick(usb ? "USB" : "LSB");
      case "DIGI": return pick("DATA-USB", "USB");
      case "FM": return pick("FM");
      case "AM": return pick("AM");
      default: return "";
    }
  }
  // The filter for the spot's mode: [Icom FIL slot, Yaesu WIDTH in Hz with NAR off (0: the mode's
  // fixed filter, NAR off only)]. The slot widths are the radio's own (IC-9700 / IC-7300MK2 manuals: SSB
  // FIL1 3.0 kHz, FIL2 2.4 kHz, CW FIL2 500 Hz, SSB-D FIL1 3.0 kHz, AM FIL1 9 kHz, FM FIL1 15 kHz); AM and
  // FM get their normal FIL1 (on FIL2 / FIL3 an Icom FM transmits narrow), as a Yaesu's NAR off does (AM
  // 9 kHz, FM 16 kHz). A Yaesu's DATA width defaults to 500 Hz: 3 kHz hears the whole FT8 / FT4 subband
  // (and the RTTY tab's tones, 2125 / 2295 Hz). SSTV is sent 3 kHz wide (its tones run 1100-2300 Hz). On
  // a Yaesu in its data mode: the FT-991A / FT-891 take a data mode's transmit audio from the rear USB /
  // DATA jack (DATA IN SELECT, default REAR), but voice SSB's from the front mic (SSB MIC SELECT, default
  // MIC), so a picture sent from TeleRig goes out in the data mode. On an Icom in the voice mode, FIL1: a
  // data mode takes its transmit audio from the radio's own DATA MOD setting, which TeleRig leaves alone
  // (on the LAN it routes the voice modes' DATA OFF MOD), so a picture sent from there could go out silent.
  const FILTER = { SSB: [2, 2400], SSTV: [1, 3000], CW: [2, 500], DIGI: [1, 3000], AM: [1, 0], FM: [1, 0] };
  // What tunes the spot after set_freq: the mode, the Icom DATA switch and the filter, each only when
  // the radio is not already there. A mode change re-sends a Yaesu's NAR and WIDTH: the radio keeps its
  // own per mode, and a code means another width in another mode. Unknown mode: nothing.
  function settings(p, spot, st) {
    let mode = radioMode(p, spot);
    const f = FILTER[spot.mode], PB = window.Passband;
    if (!p || !mode || !f) return [];
    // the IC-728's narrow CW is a mode (its optional FL-100 filter): kept when chosen, never chosen here
    if (mode === "CW" && st.mode_name === "CW-NAR" && p.modes.includes("CW-NAR")) mode = "CW-NAR";
    const caps = p.capabilities || {}, out = [];
    const moved = mode !== st.mode_name;
    if (moved) out.push({ action: "set_mode", mode });
    if (p.protocol === "yaesu") {
      const nar = moved || (st.narrow || 0) > 0;
      if (caps.narrow && nar) out.push({ action: "narrow", on: false });
      const code = caps.ext_ops && f[1] && PB ? PB.shCode(mode, false, f[1]) : -1;
      if (code >= 0 && (nar || PB.hz(p, st) !== f[1])) out.push({ action: "width", code });
    } else if (caps.tab_rx !== false) {                 // Icom FIL1-3 (on the RX tab; the IC-728 has none)
      const on = (st.data || 0) > 0, digi = spot.mode === "DIGI";
      // DATA off before the filter (a new mode already turned it off), on after it (on the new slot)
      if (caps.data_mode && !digi && on && !moved) out.push({ action: "data_mode", on: false });
      if (st.filter !== f[0]) out.push({ action: "set_filter", filter: f[0] });
      if (caps.data_mode && digi && (moved || !on)) out.push({ action: "data_mode", on: true });
    }
    return out;
  }
  // A spot or a calling frequency ({hz, mode, listen?, dial?}): the frequency (a calling row's dial where
  // it has one: an RTTY row tunes below its signal), then what its mode needs. False when this page may
  // not tune there (a viewer, a frequency off the radio's bands; a listen row, the time signals: off where
  // the radio receives): nothing is sent.
  function sendTune(x) {
    const c = ctl(), p = profile(), hz = typeof x.dial === "number" ? x.dial : x.hz;
    if (!c || !canTune() || !(x.listen ? covers(p, hz) : bandOf(p, hz))) return false;
    sendSeq(c, p, hz, settings(p, x, radioState()));
    return true;
  }
  // The frequency, then what the mode needs, with the frequency again right after a mode change: the
  // FT-991A and FT-891 show CW with the pitch added (CW FREQ DISPLAY, default PITCH OFFSET: the display,
  // which CAT reads and sets, moves by the pitch when the mode changes between SSB and CW), and an Icom
  // does the same with SSB/CW Synchronous Tuning on. Without it a click from CW to an SSB spot landed the
  // pitch (about 700 Hz) off, and the voice sounded wrong until retuned.
  function sendSeq(c, p, hz, list) {
    c.send({ action: "set_freq", hz });
    for (const m of list) {
      c.send(m);
      if (m.action === "set_mode") c.send({ action: "set_freq", hz });
    }
  }
  // Where a live spot is tuned, and the Modem window's mode for it. An FT8 spot inside a band's FT8 window
  // (callfreqs.js: its dial and the 3 kHz above it) tunes that dial, so the whole window is heard and
  // decoded, wherever in it the spot was; an RTTY spot puts its signal on the RTTY tab's tones (taken as
  // the signal's center, as the W1AW rows are); CW and SSTV show their mode; anything else, the spot as is.
  const FT8_WINDOW_HZ = 3000;
  function livePlan(s) {
    if (s.mode === "DIGI" && s.sub === "FT8") {
      for (const g of CALLS) for (const r of g.rows) {
        if (r.modem === "ft8" && s.hz >= r.hz && s.hz <= r.hz + FT8_WINDOW_HZ) return { dial: r.hz, modem: "ft8" };
      }
      return { dial: s.hz, modem: "ft8" };
    }
    if (s.mode === "DIGI" && s.sub === "RTTY") return { dial: s.hz - RTTY_CENTER_HZ, modem: "rtty" };
    return { dial: s.hz, modem: MODEM_OF[s.mode] || "" };
  }
  function tune(spot) {
    const plan = livePlan(spot);
    if (!sendTune(Object.assign({}, spot, { dial: plan.dial }))) return;
    picked = { key: keyOf(spot), spot, hz: plan.dial, at: Date.now() };     // pinned while the radio stays near the dial
    try { sessionStorage.setItem(PICK_KEY, JSON.stringify({ key: picked.key, spot, hz: plan.dial })); } catch (_) {}
    say("Tuned to " + spot.call + ", " + fmtFreq(spot.hz) + modemSay(modemTo(plan.modem)));
    render();
    list.scrollTop = 0;                                     // the list back at its top, where the pin is
  }

  // ---- a pick that names a Modem mode (a calling row's CW, FT8 or SSTV, a CW or SSTV spot): the Modem
  // window (digital.js) shows that mode, to decode what the radio now hears. Receive only: nothing is keyed,
  // and a mode sending (an SSTV picture, CW) keeps the window (DigitalPanel.show says no). A folded window
  // opens; a closed one stays closed and shows that mode when it is opened. Returns the mode shown, or "".
  const MODEM_OF = { CW: "cw", SSTV: "sstv" }, MODEM_LABEL = { cw: "CW", ft8: "FT8", rtty: "RTTY", sstv: "SSTV" };
  function modemTo(m) {
    const D = window.DigitalPanel;
    if (!MODEM_LABEL[m] || !D || typeof D.show !== "function") return "";
    let ok = false;
    try { ok = D.show(m) === true; } catch (_) { ok = false; }
    if (!ok) return "";
    try {
      const L = window.RadioLayout, t = document.querySelector('.grid-stack-item[data-tile="sstv"]');
      const onGrid = !!(t && t.parentElement && t.parentElement.id === "grid");
      if (onGrid && L && typeof L.visible === "function" && !L.visible("sstv") && typeof L.showTile === "function") L.showTile("sstv");
    } catch (_) {}
    return m;
  }
  const modemSay = (m) => (m ? "; the Modem window shows " + MODEM_LABEL[m] : "");

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
  const spotLabel = (s) => (s.mode === "DIGI" && s.sub ? s.sub : modeLabel(s.mode));   // a data spot by its own mode: FT8, RTTY
  function cell(cls, text) { const e = document.createElement("span"); e.className = cls; e.textContent = text; return e; }

  // ---- the two filters every view has ----
  const modeOk = (m) => !filt.mode || (filt.mode === "FMAM" ? (m === "FM" || m === "AM") : m === filt.mode);
  // "this band": the band of the radio's frequency, or null (no radio, off its bands): nothing matches then
  function bandHere(p, st) { return filt.band && st.freq ? bandOf(p, st.freq) : null; }
  const bandOk = (here, hz) => !filt.band || !!(here && hz >= here.lo && hz <= here.hi);
  const bandSig = (here) => (filt.band ? (here ? here.lo + "-" + here.hi : "none") : "all");

  // ---- the live list ----
  function shown() {
    const here = bandHere(profile(), radioState());
    return (data.spots || []).filter((s) => filt.src.includes(s.src) && modeOk(s.mode) && bandOk(here, s.hz));
  }
  function renderLive() {
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
      row.title = (on ? "Tuned to " : ok ? "Tune to " : (op ? "Not on this radio: " : "")) + s.call + " " + fmtFreq(s.hz) + " " + spotLabel(s) +
        (s.ref ? " · " + s.ref : "") + (s.name ? " · " + s.name : "") + (s.note ? " · " + s.note : "") +
        (s.spotter ? " · spotted by " + s.spotter : "") + (on ? " · " + (SRC_LABEL[s.src] || "") : "");
      row.append(cell("sp-age", age(s.time)), cell("sp-freq", fmtFreq(s.hz)), cell("sp-mode", spotLabel(s)),
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

  // ---- CALLING and MEMORY: a list that stays put, with the row the radio is on marked ----
  // A row is {hz, el, src, on, title(on)}. The list is built again only when what it shows changes (the
  // radio, its band under "this band", the filters, the role, the memories); otherwise only the mark moves.
  const ON_HZ = 100, FOLLOW_MS = 500;                       // "on" a row: within 100 Hz; the radio is looked at twice a second
  function markRows(rows) {
    const st = radioState(), f = st.connected ? st.freq || 0 : 0;
    for (const c of rows) {
      const on = !!f && Math.abs(f - c.hz) <= ON_HZ;
      if (on === c.on) continue;
      c.on = on;
      c.el.classList.toggle("picked", on);
      if (on) c.el.setAttribute("aria-current", "true"); else c.el.removeAttribute("aria-current");
      c.src.textContent = on ? "Tuned" : "";
      c.el.title = c.title(on);
    }
  }
  function setStatus(msg) { if (status.textContent !== msg) status.textContent = msg; }

  // ---- the Calling view: callfreqs.js, the bands this radio has (a listen group: where it receives) ----
  // A row: {hz (shown), dial (what is tuned: hz, or for an RTTY row its signal at the RTTY tab's tones),
  // mode (what tunes it), cat (its tag and the mode filter's: SSTV for an SSTV row, else the mode), name,
  // note, modem (the Modem window's mode for it, or ""), listen}. The Tuned mark and the VFO's line follow
  // the dial.
  // RTTY_CENTER_HZ == rtty.js CENTER: the RTTY tab listens on mark 2125 / space 2295 Hz, so an RTTY row's
  // signal goes 2210 Hz above the dial, in the data mode (USB side: the tab reads mark as the higher tone)
  const RTTY_CENTER_HZ = 2210;
  const CALLS = (Array.isArray(window.CALL_FREQS) ? window.CALL_FREQS : []).map((g) => ({
    band: String(g.band), listen: g.listen === true,
    rows: (Array.isArray(g.rows) ? g.rows : []).map(([khz, mode, name, note, modem]) => {
      const hz = Math.round(khz * 1000);
      // an HF SSTV row tunes as SSTV (its 3 kHz filter); the space station's pictures stay FM
      return { hz, dial: modem === "rtty" && mode === "DIGI" ? hz - RTTY_CENTER_HZ : hz,
               mode: modem === "sstv" && mode === "SSB" ? "SSTV" : mode, cat: modem === "sstv" ? "SSTV" : mode,
               name: String(name), note: String(note || ""), modem: MODEM_LABEL[modem] ? modem : "", listen: g.listen === true };
    }),
  }));
  const onRadio = (p, r) => (r.listen ? covers(p, r.hz) : !!bandOf(p, r.hz));
  let callSig = null, callRows = [];
  function renderCall() {
    const p = profile(), op = canTune(), here = bandHere(p, radioState());
    const sig = [p ? p.id : "", op ? 1 : 0, filt.mode, bandSig(here)].join("|");
    if (sig !== callSig) {
      callSig = sig;
      callRows = [];
      list.textContent = "";
      for (const g of CALLS) {
        const rows = g.rows.filter((r) => onRadio(p, r) && modeOk(r.cat) && bandOk(here, r.hz));
        if (!rows.length) continue;
        const h = document.createElement("h3");
        h.className = "spots-bandh";
        h.textContent = g.band;
        list.appendChild(h);
        for (const r of rows) {
          const row = document.createElement(op ? "button" : "div"), src = cell("sp-src", "");
          const title = (on) => (on ? "Tuned to " : op ? "Tune to " : "") + r.name + " " + fmtFreq(r.hz) + " " + modeLabel(r.cat) + (r.note ? " · " + r.note : "");
          row.className = "spot spot-call";
          if (op) {
            row.type = "button";
            row.addEventListener("click", () => { if (sendTune(r)) say("Tuned to " + r.name + ", " + fmtFreq(r.hz) + modemSay(modemTo(r.modem))); });
          }
          row.title = title(false);
          row.append(cell("sp-freq", fmtFreq(r.hz)), cell("sp-mode", modeLabel(r.cat)), cell("sp-call", r.name), cell("sp-ref", r.note), src);
          list.appendChild(row);
          callRows.push({ hz: r.dial, el: row, src, on: false, title });   // marked where it tunes
        }
      }
      const n = callRows.length;
      setStatus((n ? n + (n === 1 ? " frequency" : " frequencies")
        : !p ? "Loading the radio's bands…"
        : (filt.mode || filt.band) ? "No calling frequencies match the filters." : "No calling frequencies on this radio's bands.")
        + (!op && n ? " Operators can click one to tune." : ""));
    }
    markRows(callRows);
  }

  // ---- the Memory view: this account's own frequencies for this radio (/api/me/memories) ----
  const MEM_API = "/api/me/memories", MEM_FRESH_MS = 30000, NAME_MAX = 32, ARM_MS = 4000;
  // radios: {radio id: [{id, hz, mode, name, data?, filter?, narrow?, width?, shift?, offset?, tone?, ctcss?,
  // dcs?}]}; seq counts every change
  // of it here; draft: the radio as it was when SAVE was pressed, waiting for its name
  const mem = { radios: {}, max: 100, seq: 0, at: 0, loaded: false, error: false, loading: false, busy: false, draft: null,
                seeded: new Set(), seeding: new Set() };      // the radios that have had their presets; asked for on this page
  function adopt(radio, j) {                                // a write's answer carries that radio's whole list
    if (!j || !Array.isArray(j.items)) return;
    mem.radios[radio] = j.items;
    mem.seq++;
  }
  async function loadMem(force) {
    if (mem.loading || !canTune()) return;
    if (!force && mem.at && Date.now() - mem.at < MEM_FRESH_MS) return;
    mem.loading = true;
    try {
      const j = await TR.api(MEM_API, { noRedirect: true });
      mem.radios = (j && j.radios && typeof j.radios === "object") ? j.radios : {};
      mem.seeded = new Set(j && Array.isArray(j.seeded) ? j.seeded : []);
      if (j && j.max > 0) mem.max = j.max;
      mem.error = !!(j && j.memories_error);
      mem.loaded = true;
      mem.at = Date.now();
    } catch (_) {
      mem.error = true;                                     // what was read before stays; asked again when next shown
      mem.at = 0;
    } finally {
      mem.loading = false;
      mem.seq++;
      if (filt.view === "mem") render();
    }
  }
  // a radio's presets, asked for once on this page (a refusal waits for the next page load): the server
  // adds them and notes the radio, so the next GET lists it as seeded
  async function seedPresets(radio) {
    mem.seeding.add(radio);
    try {
      adopt(radio, await TR.api(MEM_API + "/presets", { json: { radio }, noRedirect: true }));
      mem.seeded.add(radio);
    } catch (_) {}                                          // nothing added: the list stays as it is
    if (filt.view === "mem") render();
  }
  function memList(p) {                                     // this radio's, lowest first
    const l = p && Array.isArray(mem.radios[p.id]) ? mem.radios[p.id] : [];
    return l.filter((m) => m && typeof m.hz === "number" && typeof m.mode === "string" && typeof m.name === "string")
      .slice().sort((a, b) => a.hz - b.hz || (a.id || 0) - (b.id || 0));
  }
  // a memory's mode as the mode filter knows modes (a spot's), and as its row says it (an Icom's DATA: USB-D)
  function memFamily(m) {
    const n = m.mode;
    if (/^CW/.test(n)) return "CW";
    if (/^(FM|C4FM|DV|DATA-FM|WFM)/.test(n)) return "FM";
    if (/^AM/.test(n)) return "AM";
    if (m.data || /^(DATA|RTTY|PSK)/.test(n)) return "DIGI";
    return n === "USB" || n === "LSB" ? "SSB" : "";
  }
  const memMode = (m) => m.mode + (m.data ? "-D" : "");
  function memFilter(p, m) {                                // the filter it keeps, for its title: " · FIL2", " · NAR · 500 Hz"
    if (p.protocol !== "yaesu") return typeof m.filter === "number" ? " · FIL" + m.filter : "";
    const PB = window.Passband;
    const hz = PB && typeof m.width === "number" ? PB.hz(p, { mode_name: m.mode, narrow: m.narrow ? 1 : 0, width: m.width }) : 0;
    return (m.narrow ? " · NAR" : "") + (hz ? " · " + PB.label(hz) : "");
  }
  // may the radio be tuned there: its receive coverage (freqentry.js: the profile's rx_ranges, else its bands)
  function covers(p, hz) {
    let r = [];
    try { r = (p && window.FreqEntry && FreqEntry.rangesOf(p)) || []; } catch (_) { r = []; }
    return r.length ? r.some((x) => Array.isArray(x) && hz >= x[0] && hz <= x[1]) : !!bandOf(p, hz);
  }
  // a Yaesu reports narrow FM / AM as a mode of its own (FM-N): the mode of the radio's list, narrow
  function plainMode(p, st) {
    const modes = p && Array.isArray(p.modes) ? p.modes : [];
    let mode = String(st.mode_name || ""), narrow = (st.narrow || 0) > 0;
    if (!modes.includes(mode) && /-N$/.test(mode) && modes.includes(mode.slice(0, -2))) { mode = mode.slice(0, -2); narrow = true; }
    return { mode, narrow, known: modes.includes(mode) };
  }
  // ---- repeater shift and tone: kept by a memory in FM (the shift in DV too, for a D-STAR repeater) ----
  // The memory says it the radio's way round: shift "simplex" | "+" | "-" (an Icom's duplex 0 / 2 DUP+ /
  // 1 DUP-, a Yaesu's repeater shift 0 / 1 + / 2 -), the offset in Hz where the radio can set it (the
  // IC-9700, CI-V 0D; other radios keep their own per band), tone "off" | "tone" | "tsql" | "dcs" |
  // "dcs-enc" (the radio's tone mode 0..4: an Icom has the first three) with the CTCSS tone in tenths of
  // Hz or the DCS code. The tables are app.js's (window.FmTones): the radio takes a tone by its index.
  const FM_MODES = ["FM", "DATA-FM", "C4FM"], SHIFT_MODES = FM_MODES.concat(["DV"]);
  const TONES = ["off", "tsql", "tone", "dcs", "dcs-enc"];          // index = the radio's tone_mode
  const TONE_LABEL = { tone: "TONE", tsql: "TSQL", dcs: "DCS", "dcs-enc": "DCS-ENC" };
  const OFFSET_MAX = 99999900, OFFSET_10M = 10000000, GHZ = 1000000000;
  const canShift = (p) => { const c = p.capabilities || {}; return p.protocol === "yaesu" ? !!c.fm_tone : !!c.duplex; };
  const canDcs = (p) => (p.capabilities || {}).fm_dcs !== false;
  function fmTables() { const t = window.FmTones; return t && Array.isArray(t.ctcss) && Array.isArray(t.dcs) ? t : null; }
  const ctcssAt = (i) => { const T = fmTables(), s = T && T.ctcss[i]; return typeof s === "string" ? Math.round(parseFloat(s) * 10) : 0; };
  const dcsAt = (i) => { const T = fmTables(), s = T && T.dcs[i]; return typeof s === "string" ? parseInt(s, 10) : 0; };
  const ctcssIdx = (v) => { const T = fmTables(); return T ? T.ctcss.findIndex((s) => Math.round(parseFloat(s) * 10) === v) : -1; };
  const dcsIdx = (v) => { const T = fmTables(); return T ? T.dcs.findIndex((s) => parseInt(s, 10) === v) : -1; };
  // the radio's shift and tone into the memory m (its mode already chosen); only what the radio reports
  function captureRpt(p, st, m) {
    const caps = p.capabilities || {}, yaesu = p.protocol === "yaesu";
    const v = yaesu ? st.rpt_shift : st.duplex;
    if (SHIFT_MODES.includes(m.mode) && canShift(p) && [0, 1, 2].includes(v)) {
      const plus = yaesu ? v === 1 : v === 2;                // a Yaesu's 1 is +, an Icom's 2 is DUP+
      m.shift = !v ? "simplex" : plus ? "+" : "-";
      const off = Math.floor((st.offset || 0) / 100) * 100;
      // the IC-9700 takes the 10 MHz digit on its 1.2 GHz band only (an offset it can't take is not kept)
      if (m.shift !== "simplex" && caps.duplex_offset && off > 0 && off <= OFFSET_MAX && (off < OFFSET_10M || m.hz >= GHZ)) m.offset = off;
    }
    const tone = TONES[st.tone_mode];
    if (!FM_MODES.includes(m.mode) || !caps.fm_tone || !tone || (/^dcs/.test(tone) && !canDcs(p))) return;
    if (tone === "tone" || tone === "tsql") {
      const hz = ctcssAt(st.tone_freq);
      if (hz) { m.tone = tone; m.ctcss = hz; }
    } else if (tone !== "off") {
      const code = dcsAt(st.dcs_code);
      if (code) { m.tone = tone; m.dcs = code; }
    } else m.tone = "off";
  }
  // What brings a memory's shift and tone back, after its mode: sent every time (a band change brings that
  // band's own shift and tone, so the radio's state before the click says nothing about after it). The
  // offset before the shift, the tone or code before the tone mode (no squelch opening on the old tone).
  function recallRpt(p, m) {
    const caps = p.capabilities || {}, out = [];
    if (["simplex", "+", "-"].includes(m.shift) && canShift(p)) {
      if (typeof m.offset === "number" && caps.duplex_offset) out.push({ action: "offset", hz: m.offset });   // the server keeps one only with + or -
      if (p.protocol === "yaesu") out.push({ action: "rpt_shift", value: m.shift === "+" ? 1 : m.shift === "-" ? 2 : 0 });
      else out.push({ action: "duplex", mode: m.shift === "+" ? 2 : m.shift === "-" ? 1 : 0 });
    }
    const v = TONES.indexOf(m.tone);
    if (v < 0 || !caps.fm_tone || (v > 2 && !canDcs(p))) return out;
    if (m.tone === "tone" || m.tone === "tsql") {
      const i = ctcssIdx(m.ctcss);
      if (i < 0) return out;                                // a tone the table doesn't have: the tone is left as it is
      out.push({ action: "tone_freq", idx: i });
    } else if (m.tone !== "off") {
      const i = dcsIdx(m.dcs);
      if (i < 0) return out;
      out.push({ action: "dcs_code", idx: i });
    }
    out.push({ action: "tone_mode", value: v });
    return out;
  }
  // how a memory's row says it: [shift, tone] ("+600 kHz", "TSQL 100.0"; "" for simplex, no tone or
  // nothing kept). The row leaves the tone's " Hz" off (it fits the narrowest panel beside "Tuned"); its
  // title has it, and says simplex and no tone too
  const MINUS = "−";
  const offsetLabel = (hz) => (hz >= 1e6 ? +(hz / 1e6).toFixed(4) + " MHz" : +(hz / 1e3).toFixed(1) + " kHz");
  function rptParts(m, hz) {
    const shift = m.shift === "+" || m.shift === "-"
      ? (m.shift === "+" ? "+" : MINUS) + (typeof m.offset === "number" ? offsetLabel(m.offset) : " shift") : "";
    const tone = (m.tone === "tone" || m.tone === "tsql") && typeof m.ctcss === "number" ? TONE_LABEL[m.tone] + " " + (m.ctcss / 10).toFixed(1) + (hz ? " Hz" : "")
      : (m.tone === "dcs" || m.tone === "dcs-enc") && typeof m.dcs === "number" ? TONE_LABEL[m.tone] + " " + String(m.dcs).padStart(3, "0") : "";
    return [shift, tone];
  }
  function rptTitle(m) {
    const [shift, tone] = rptParts(m, true);
    return [shift || (m.shift === "simplex" ? "simplex" : ""), tone || (m.tone === "off" ? "no tone" : "")].filter(Boolean).map((t) => " · " + t).join("");
  }

  // What a memory keeps of the radio as it is: the frequency, the mode by its own name, the filter as the
  // radio says it (an Icom's FIL slot and DATA switch; a Yaesu's NAR, and its WIDTH code in the modes that
  // have one) and, in FM, the repeater shift and tone (captureRpt). null: nothing to keep (no radio
  // connected, a mode that is not in the radio's list).
  function capture(p, st) {
    if (!p || !st.connected || !(st.freq > 0)) return null;
    const cur = plainMode(p, st), caps = p.capabilities || {}, PB = window.Passband;
    if (!cur.known) return null;
    const m = { hz: st.freq, mode: cur.mode };
    if (p.protocol === "yaesu") {
      if (caps.narrow) m.narrow = cur.narrow;
      const code = st.width || 0;
      if (caps.ext_ops && PB && PB.hz(p, { mode_name: cur.mode, narrow: cur.narrow ? 1 : 0, width: code }) > 0) m.width = code;
    } else {
      if (caps.tab_rx !== false && [1, 2, 3].includes(st.filter)) m.filter = st.filter;
      if (caps.data_mode) m.data = (st.data || 0) > 0;
    }
    captureRpt(p, st, m);
    return m;
  }
  // What brings the radio back to a memory after set_freq, each only when the radio is not there already.
  // A changed mode has its own filter on the radio, so the filter is sent again with it.
  function recall(p, m, st) {
    const cur = plainMode(p, st), caps = p.capabilities || {}, out = [];
    const moved = m.mode !== cur.mode;
    if (moved && p.modes.includes(m.mode)) out.push({ action: "set_mode", mode: m.mode });
    if (p.protocol === "yaesu") {
      const nar = typeof m.narrow === "boolean" && (moved || m.narrow !== cur.narrow);
      if (caps.narrow && nar) out.push({ action: "narrow", on: m.narrow });
      if (caps.ext_ops && typeof m.width === "number" && (moved || nar || m.width !== (st.width || 0))) out.push({ action: "width", code: m.width });
    } else if (caps.tab_rx !== false) {
      const on = (st.data || 0) > 0, want = !!m.data;
      // DATA off before the filter (a new mode already turned it off), on after it (on the new slot)
      if (caps.data_mode && !want && on && !moved) out.push({ action: "data_mode", on: false });
      if (typeof m.filter === "number" && (moved || st.filter !== m.filter)) out.push({ action: "set_filter", filter: m.filter });
      if (caps.data_mode && want && (moved || !on)) out.push({ action: "data_mode", on: true });
    }
    return out.concat(recallRpt(p, m));
  }
  function tuneMem(radio, m) {
    const c = ctl(), p = profile();
    if (!c || !canTune() || !p || p.id !== radio) return;   // a row drawn for another radio tunes nothing
    sendSeq(c, p, m.hz, recall(p, m, radioState()));
    say("Tuned to " + m.name + ", " + fmtFreq(m.hz));
  }

  // delete: the X arms at the first tap ("Delete?"), deletes at the second; it lets go after ARM_MS or
  // when the focus leaves it
  let armed = null;                                         // {btn, name, t}
  function setDel(btn, name, arm) {
    const label = arm ? "Delete " + name + "? Tap again" : "Delete " + name;
    btn.classList.toggle("arm", arm);
    btn.textContent = arm ? "Delete?" : "×";
    btn.setAttribute("aria-label", label);
    btn.title = label;
  }
  function disarm() {
    if (!armed) return;
    const a = armed;
    armed = null;
    clearTimeout(a.t);
    setDel(a.btn, a.name, false);
  }
  async function delMem(radio, m) {
    if (mem.busy) return;
    const at = memRows.findIndex((r) => r.id === m.id);
    mem.busy = true;
    try {
      adopt(radio, await TR.api(MEM_API + "?radio=" + encodeURIComponent(radio) + "&id=" + encodeURIComponent(m.id), { method: "DELETE" }));
      say("Deleted " + m.name);
    } catch (e) {
      toast((e && e.message) || "The memory could not be deleted.", "error");
    } finally {
      mem.busy = false;
      if (filt.view === "mem") {
        render();
        const next = memRows[at] || memRows[at - 1];        // the focus goes to the row that took its place
        if (next) next.del.focus(); else if (memSave) memSave.focus();
      }
    }
  }

  let memSig = null, memRows = [];                          // rows as markRows wants them, plus {id, del}
  function renderMem() {
    const p = profile(), op = canTune(), here = bandHere(p, radioState());
    if (op && p && mem.loaded && !mem.error && !mem.seeded.has(p.id) && !mem.seeding.has(p.id)) seedPresets(p.id);
    const sig = [p ? p.id : "", op ? 1 : 0, filt.mode, bandSig(here), mem.seq].join("|");
    if (sig !== memSig) {
      memSig = sig;
      disarm();
      memRows = [];
      list.textContent = "";
      const all = op ? memList(p) : [];
      for (const m of all) {
        if (!modeOk(memFamily(m)) || !bandOk(here, m.hz)) continue;
        const ok = covers(p, m.hz), radio = p.id;
        const wrap = document.createElement("div"), row = document.createElement(ok ? "button" : "div");
        const del = document.createElement("button"), src = cell("sp-src", "");
        const rpt = rptParts(m).filter(Boolean).join(" · ");       // a repeater's shift and tone: a second line
        const title = (on) => (on ? "Tuned to " : ok ? "Tune to " : "Not on this radio: ") + m.name + " " + fmtFreq(m.hz) + " " + memMode(m) + memFilter(p, m) + rptTitle(m);
        wrap.className = "spot-mem";
        row.className = "spot spot-call" + (rpt ? " rpt" : "") + (ok ? "" : " off");
        if (ok) { row.type = "button"; row.addEventListener("click", () => tuneMem(radio, m)); }
        row.title = title(false);
        row.append(cell("sp-freq", fmtFreq(m.hz)), cell("sp-mode", memMode(m)), cell("sp-call", m.name));
        if (rpt) row.append(cell("sp-ref", rpt));
        row.append(src);
        del.type = "button";
        del.className = "mem-del";
        setDel(del, m.name, false);
        del.addEventListener("click", () => {
          if (armed && armed.btn === del) { disarm(); delMem(radio, m); return; }
          disarm();
          setDel(del, m.name, true);
          armed = { btn: del, name: m.name, t: setTimeout(disarm, ARM_MS) };
        });
        del.addEventListener("blur", () => { if (armed && armed.btn === del) disarm(); });
        wrap.append(row, del);
        list.appendChild(wrap);
        memRows.push({ hz: m.hz, el: row, src, on: false, title, id: m.id, del });
      }
      const n = memRows.length;
      setStatus(!op ? "Operators can save and recall memories."
        : !p ? "Loading the radio's bands…"
        : n ? n + (n === 1 ? " memory" : " memories")
        : all.length ? "No memories match the filters."
        : mem.error ? "The memories can't be read right now."
        : !mem.loaded ? "Loading memories…"
        : "No memories for this radio yet. SAVE keeps the frequency it is on.");
    }
    markRows(memRows);
  }
  // SAVE: the first press takes the radio as it is and asks for the name (the frequency, selected, so
  // typing replaces it); the second, or Enter, keeps it
  function openDraft() {
    const p = profile(), st = radioState();
    if (!canTune() || !memName) return;
    const item = capture(p, st);
    if (!item) { toast(st.connected ? "This mode can't be kept as a memory." : "Connect a radio first: a memory keeps the frequency it is on.", "warn"); return; }
    if (memList(p).length >= mem.max) { toast("This radio's memories are full (" + mem.max + "). Delete one first.", "warn"); return; }
    mem.draft = { radio: p.id, item };
    memName.value = fmtFreq(item.hz);
    syncBar();
    memName.focus();
    try { memName.select(); memName.setSelectionRange(0, memName.value.length); } catch (_) {}
  }
  function closeDraft(focus) {
    if (!mem.draft) return;
    mem.draft = null;
    if (memName) memName.value = "";
    syncBar();
    if (focus && memSave) memSave.focus();
  }
  async function saveDraft() {
    const d = mem.draft;
    if (!d || mem.busy) return;
    const typed = String(memName.value || "").replace(/[\u0000-\u001f\u007f]+/g, " ").trim();
    // at most NAME_MAX characters, whole ones (half of an emoji is no character: the server refuses it);
    // nothing left: the frequency
    const name = Array.from(typed).slice(0, NAME_MAX).join("").trim() || fmtFreq(d.item.hz);
    mem.busy = true;
    syncBar();
    try {
      adopt(d.radio, await TR.api(MEM_API, { json: { radio: d.radio, item: Object.assign({}, d.item, { name }) } }));
      mem.busy = false;
      closeDraft(true);
      say("Saved " + name + ", " + fmtFreq(d.item.hz));
    } catch (e) {
      toast((e && e.message) || "The memory could not be saved.", "error");   // the name stays, to try again
    } finally {
      mem.busy = false;
      syncBar();
      if (filt.view === "mem") render();
    }
  }
  if (memForm) memForm.addEventListener("submit", (e) => { e.preventDefault(); if (mem.draft) saveDraft(); else openDraft(); });
  if (memCancel) memCancel.addEventListener("click", () => closeDraft(true));
  if (memName) memName.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    e.preventDefault(); e.stopPropagation();
    closeDraft(true);
  });

  // ---- the keys and the filters as they are set; what only one view has is hidden in the others ----
  function syncBar() {
    const live = filt.view === "live", memView = filt.view === "mem", naming = memView && !!mem.draft;
    for (const b of srcBtns) {
      const on = filt.src.includes(b.dataset.src);
      b.classList.toggle("on", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
      b.hidden = !live;                                     // the sources are the live list's
    }
    if (modeSel && modeSel.value !== filt.mode) modeSel.value = filt.mode;
    if (bandChk) bandChk.checked = filt.band;
    if (hasTabs) {
      for (const v of VIEWS) {
        const on = v === filt.view, k = tabs[v];
        k.classList.toggle("on", on);
        k.setAttribute("aria-selected", on ? "true" : "false");
        k.tabIndex = on ? 0 : -1;
      }
      list.setAttribute("aria-labelledby", tabs[filt.view].id);
    }
    if (credits.live) credits.live.hidden = !live;
    if (credits.call) credits.call.hidden = filt.view !== "call";
    if (memForm) {
      memForm.hidden = !memView || !canTune();              // SAVE and the name: operators, in the Memory view
      memForm.classList.toggle("naming", naming);
      if (memName) memName.hidden = !naming;
      if (memCancel) memCancel.hidden = !naming;
      if (memSave) {
        memSave.disabled = mem.busy;
        memSave.title = naming ? "Keep it under this name" : "Save the frequency the radio is on";
      }
    }
  }
  function render() {
    syncBar();
    if (filt.view === "call") { callSig = null; renderCall(); }
    else if (filt.view === "mem") { memSig = null; renderMem(); }
    else renderLive();
  }

  // ---- asking the server: the spots only while the live list is in view (the panel on the grid, the
  // page visible); the memories when their view comes into view and what was read is 30 s old ----
  function onPage() {
    let v = false;
    try { v = !!(window.RadioLayout && RadioLayout.visible && RadioLayout.visible("spots")); } catch (_) { v = false; }
    return v && !document.hidden;
  }
  const visible = () => onPage() && filt.view === "live";
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
      if (filt.view === "live") render();                   // an answer that comes after the view changed waits there
    }
  }
  function schedule() {
    if (timer) { clearInterval(timer); timer = 0; }
    if (visible()) { poll(false); timer = setInterval(() => poll(false), 5000); }
    if (filt.view === "mem" && onPage()) loadMem(false);
  }

  // ---- the view keys (a tablist: the arrow keys too); each view keeps its place in the list ----
  const scrollAt = { live: 0, call: 0, mem: 0 };
  function show(v) {
    if (!hasTabs || !VIEWS.includes(v)) return false;
    if (v === filt.view) return true;
    scrollAt[filt.view] = list.scrollTop;
    mem.draft = null;                                       // a name being typed belongs to the view left
    filt.view = v;
    keep(); render(); schedule();
    list.scrollTop = scrollAt[v];
    return true;
  }
  if (hasTabs) for (const v of VIEWS) {
    tabs[v].addEventListener("click", () => show(v));
    tabs[v].addEventListener("keydown", (e) => {
      const d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
      if (!d) return;
      e.preventDefault();
      const n = VIEWS[(VIEWS.indexOf(v) + d + VIEWS.length) % VIEWS.length];
      if (show(n)) tabs[n].focus();
    });
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
  setInterval(() => { if (filt.view === "live") render(); }, 30000);   // the ages move on
  // tuned away from the pinned spot? (redrawn only then, so keyboard focus in the list stays put)
  setInterval(() => { if (picked) { current(); if (!picked && filt.view === "live") render(); } }, 2000);
  // CALLING and MEMORY follow the radio: its frequency, its band, another radio, the role
  setInterval(() => {
    if (!onPage()) return;
    if (filt.view === "call") renderCall();
    else if (filt.view === "mem") {
      if (memForm && memForm.hidden === canTune()) syncBar();    // the role changed: SAVE comes or goes
      if (!mem.loaded && !mem.error) loadMem(false);             // an operator now: their memories
      renderMem();
    }
  }, FOLLOW_MS);
  render();
  schedule();

  // ---- what a frequency is, for the VFO's line under the digits (app.js): a calling frequency of
  // callfreqs.js on this radio's bands (within ON_HZ of it, as its row would be marked Tuned; a time
  // signal where the radio receives it, by its own name), else null (the VFO then names the band plan's
  // segment). Not your memories: the VFO doesn't name them (the operator, 2026-11-22), so nothing here
  // reads them.
  function here(hz) {
    if (!(hz > 0)) return null;
    const p = profile();
    for (const g of CALLS) {
      const r = g.rows.find((x) => Math.abs(x.dial - hz) <= ON_HZ && onRadio(p, x));
      if (r) return { kind: "call", name: g.listen ? r.name : g.band + " " + r.name, note: r.note };
    }
    return null;
  }

  window.SpotsPanel = {
    summary: () => (filt.view === "live" && lastFetch ? shown().length + " spots" : ""),
    view: () => filt.view,
    here,
  };
})();
