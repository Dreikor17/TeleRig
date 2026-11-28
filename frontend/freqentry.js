/* TeleRig: type a frequency. In the VFO, click the operating band's digits (or focus them and press Enter or
 * Space): they become an entry. Enter (or its Tune key) tunes, Esc or a click elsewhere cancels. A phone's
 * keyboard may have no Enter at all (an iPhone's number pad), and a tap outside cancels, so the Tune key is
 * there to tap. The Controls panel's frequency
 * box reads what it is given the same way (parse).
 *
 *   What a typed frequency can look like (after the radios' own keypads, the Icom F-INP and the Yaesu keys):
 *     14.074, 14,074      MHz, with a point (a comma works as the point)
 *     7.074.50            the VFO's own grouping: MHz . kHz . the hundreds and tens of Hz (here 500 Hz)
 *     .230                a leading point keeps the MHz you are on (on 14.074 MHz: 14.230 MHz)
 *     7074, 1296          a bare number counts as MHz where this radio receives there, else as kHz (from
 *                         1000) or Hz (from 1000000): 7074 is 7.074 MHz, but 1296 on the IC-9700 is 1296 MHz
 *     7074k, 14.074 MHz   a unit says which (k / kHz, M / MHz, Hz)
 *   A frequency this radio can't receive (profile.rx_ranges, else its bands) is refused with the reason, and
 *   nothing is sent. Tuning goes through RadioControl.send({action: "set_freq", hz}), so the server's rules
 *   apply (operators; no retune under someone else's transmission).
 *
 * window.FreqEntry = { parse(text, curHz, ranges) -> {hz} | {error}, format(hz), rangesOf(profile),
 *                      open(row), close(), isOpen() }
 */
(function () {
  "use strict";
  const MAX_HZ = 10e9;

  // 7074500 -> "7.074.500" (the VFO's grouping)
  function format(hz) {
    const h = Math.max(0, Math.round(+hz || 0));
    const mhz = Math.floor(h / 1e6), khz = Math.floor((h % 1e6) / 1e3), rest = h % 1e3;
    return mhz + "." + String(khz).padStart(3, "0") + "." + String(rest).padStart(3, "0");
  }
  function fmtRange([lo, hi]) {
    const m = (hz) => String(Math.round(hz / 1e3) / 1e3);
    return m(lo) + "-" + m(hi) + " MHz";
  }
  function inside(hz, ranges) {
    if (!(hz > 0) || hz > MAX_HZ) return false;
    if (!Array.isArray(ranges) || !ranges.length) return true;
    return ranges.some((r) => Array.isArray(r) && hz >= r[0] && hz <= r[1]);
  }
  // the radio's receive coverage: profile.rx_ranges, else its bands' edges, else anything up to 10 GHz
  function rangesOf(p) {
    if (p && Array.isArray(p.rx_ranges) && p.rx_ranges.length) return p.rx_ranges;
    if (p && Array.isArray(p.bands) && p.bands.length) return p.bands.map((b) => [b.lo, b.hi]);
    return [];
  }

  function parse(text, curHz, ranges) {
    let t = String(text == null ? "" : text).trim().toLowerCase().replace(/\s+/g, "").replace(/,/g, ".");
    if (!t) return { error: "Type a frequency, e.g. 14.074" };
    let unit = "";
    const u = /(mhz|khz|hz|m|k)$/.exec(t);
    if (u) { unit = u[1] === "hz" ? "Hz" : u[1][0] === "m" ? "M" : "k"; t = t.slice(0, -u[1].length); }
    if (!/^[0-9.]+$/.test(t) || !/[0-9]/.test(t)) return { error: "Only digits and a point: 14.074, 7074 or .230" };
    const parts = t.split(".");
    let hz = NaN;
    if (parts.length >= 3) {                                  // 7.074.50: MHz . kHz . what is under the kHz
      if (unit && unit !== "M") return { error: "Grouped as MHz.kHz.Hz, e.g. 7.074.50" };
      const mhz = parts[0], khz = parts[1], low = parts.slice(2).join("");
      if (khz.length !== 3 || low.length > 3) return { error: "The kHz in three digits: 7.074.50" };
      const m = mhz === "" ? Math.floor((+curHz || 0) / 1e6) : +mhz;
      hz = m * 1e6 + +khz * 1e3 + +(low.padEnd(3, "0") || 0);
    } else if (t[0] === "." && !unit) {                       // .230: the MHz you are on, the kHz you type
      if (!(+curHz > 0)) return { error: "Type the MHz too, e.g. 14.230" };
      hz = Math.floor(+curHz / 1e6) * 1e6 + Math.round(parseFloat("0" + t) * 1e6);
    } else {
      const v = parseFloat(t);
      if (!isFinite(v)) return { error: "Only digits and a point: 14.074, 7074 or .230" };
      if (unit) hz = Math.round(v * (unit === "M" ? 1e6 : unit === "k" ? 1e3 : 1));
      else {
        // a bare number: MHz where this radio receives there; kHz or Hz only for a number too big to be MHz
        // here (a small one with a point, 144.2 on an HF radio, is a mistake to say, not 144.2 kHz to tune)
        hz = Math.round(v * 1e6);
        if (!inside(hz, ranges) && v >= 1000 && inside(Math.round(v * 1e3), ranges)) hz = Math.round(v * 1e3);
        else if (!inside(hz, ranges) && v >= 1e6 && inside(Math.round(v), ranges)) hz = Math.round(v);
      }
    }
    if (!(hz > 0)) return { error: "Type a frequency above 0" };
    if (!inside(hz, ranges)) {
      const where = Array.isArray(ranges) && ranges.length ? ranges.map(fmtRange).join(", ") : "";
      return { error: format(hz) + " MHz is outside this radio" + (where ? " (" + where + ")" : "") };
    }
    return { hz };
  }

  // ---- the VFO's entry ----
  const ctl = () => window.RadioControl || null;
  function radioState() { try { return (ctl() && ctl().state()) || {}; } catch (_) { return {}; } }
  function profile() {
    const st = radioState();
    let rs = [];
    try { rs = (ctl() && ctl().radios()) || []; } catch (_) { rs = []; }
    const sel = document.getElementById("radioSel");
    const id = (st.connected && st.radio) || (sel && sel.value) || "";
    return rs.find((p) => p.id === id) || null;
  }
  const HELP = "Type MHz (14.074), kHz (14074) or .230 for the kHz alone; Enter tunes, Esc cancels";
  let cur = null;                                             // { row, freqEl, wrap, input, hint }

  function hintOf(text) {
    if (!String(text || "").trim()) return { text: HELP, err: false };
    const r = parse(text, radioState().freq, rangesOf(profile()));
    return r.error ? { text: r.error, err: true } : { text: "Enter tunes to " + format(r.hz) + " MHz, Esc cancels", err: false };
  }
  function show(h) {
    if (!cur) return;
    cur.hint.textContent = h.text;
    cur.hint.classList.toggle("err", !!h.err);
  }
  function open(row) {
    if (!row) return false;
    if (cur && cur.row === row) { cur.input.focus(); return true; }
    close();
    const freqEl = row.querySelector(".band-freq"), center = row.querySelector(".b-center");
    if (!freqEl || !center) return false;
    const wrap = document.createElement("label"), input = document.createElement("input"), unit = document.createElement("span");
    const hint = document.createElement("p");
    wrap.className = "vfo-entry";
    input.type = "text";
    input.setAttribute("inputmode", "decimal");
    input.setAttribute("autocomplete", "off");
    input.setAttribute("spellcheck", "false");
    input.setAttribute("enterkeyhint", "go");             // a phone keyboard's action key says Go and sends Enter
    input.setAttribute("aria-label", "Frequency to tune to");
    input.setAttribute("aria-describedby", "vfoEntryHint");
    const hz = radioState().freq;
    if (hz > 0) input.setAttribute("placeholder", format(hz));
    const go = document.createElement("button");
    go.type = "button";
    go.className = "key sm ve-go";
    go.textContent = "Tune";
    go.setAttribute("aria-label", "Tune to the frequency typed");
    // the tap must not take the focus from the entry first: its blur would cancel before the click
    go.addEventListener("pointerdown", (e) => e.preventDefault());
    go.addEventListener("mousedown", (e) => e.preventDefault());
    go.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); tune(); });
    unit.className = "ve-unit";
    unit.textContent = "MHz";
    hint.className = "ve-hint";
    hint.id = "vfoEntryHint";
    hint.setAttribute("aria-live", "polite");
    try { wrap.style.fontSize = getComputedStyle(freqEl).fontSize; } catch (_) {}   // the digits' own size
    wrap.appendChild(input);
    wrap.appendChild(unit);
    wrap.appendChild(go);
    freqEl.hidden = true;
    center.appendChild(wrap);
    row.appendChild(hint);
    row.classList.add("typing");
    cur = { row, freqEl, wrap, input, hint, go };
    input.addEventListener("input", () => show(hintOf(input.value)));
    input.addEventListener("keydown", onKey);
    input.addEventListener("blur", (e) => { if (e && e.relatedTarget === go) return; if (cur && cur.input === input) setTimeout(() => { if (cur && cur.input === input) close(); }, 0); });
    show(hintOf(""));
    input.focus();
    return true;
  }
  function onKey(e) {
    if (!cur) return;
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(true); return; }
    if (e.key !== "Enter") return;
    e.preventDefault();
    e.stopPropagation();
    tune();
  }
  function tune() {
    if (!cur) return;
    const r = parse(cur.input.value, radioState().freq, rangesOf(profile()));
    if (r.error) { show({ text: r.error, err: true }); return; }
    const c = ctl();
    if (c) c.send({ action: "set_freq", hz: r.hz });
    try { TR.srLive("Tuning to " + format(r.hz) + " MHz"); } catch (_) {}
    close(true);
  }
  // back to the digits; `refocus` puts the focus back on them (Enter / Esc), not on a click elsewhere
  function close(refocus) {
    const c = cur;
    if (!c) return;
    cur = null;
    if (c.wrap.parentNode) c.wrap.parentNode.removeChild(c.wrap);
    if (c.hint.parentNode) c.hint.parentNode.removeChild(c.hint);
    c.row.classList.remove("typing");
    c.freqEl.hidden = false;
    if (refocus && typeof c.freqEl.focus === "function") c.freqEl.focus();
  }

  window.FreqEntry = { parse, format, rangesOf, open, close, isOpen: () => !!cur, row: () => (cur ? cur.row : null) };
})();
