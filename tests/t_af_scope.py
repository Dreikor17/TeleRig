"""CW on the audio (AF) scope of a radio with no scope over CAT (FT-891, FT-991A, IC-728): the frequency
map is tested on the REAL waterfall.js in a Node vm (tests/af_scope_test.js); these unit tests run it and
pin the app.js wiring. The Yaesu menu reads behind it are backend/yaesu_cw_af_test.py. They read files
only: no server, no radio, no TX.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from harness import check, eq, test

REPO = Path(__file__).resolve().parent.parent
FRONT = REPO / "frontend"


@test("unit")
def af_scope_cw_map_in_node_vm(ctx):
    """waterfall.js AfMap: in CW every side x display setting x pitch reads true, the marker sits at the
    pitch and a tap lands the signal on it; SSB / data unchanged; the marker is drawn at the pitch."""
    r = subprocess.run(["node", str(REPO / "tests" / "af_scope_test.js")], capture_output=True, text=True,
                       timeout=120, cwd=str(REPO))
    eq(r.returncode, 0, f"af_scope_test.js: {r.stdout}{r.stderr}")
    check("ALL PASS" in r.stdout, f"af_scope_test.js output: {r.stdout}")
    rr = subprocess.run(["node", "--check", str(FRONT / "waterfall.js")], capture_output=True, text=True, timeout=60)
    eq(rr.returncode, 0, f"node --check waterfall.js: {rr.stderr}")


@test("unit")
def af_scope_wiring_in_app_js(ctx):
    """app.js draws the AF scope from AfMap (no private sideband list left) and a scope tap adds the CW
    retune, so the tapped signal lands on the marker."""
    app = (FRONT / "app.js").read_text(encoding="utf-8")
    m = re.search(r"function startAfScope\(srcNode\) \{(.*?)\n  \}\n", app, re.S)
    check(m, "startAfScope")
    body = m.group(1) if m else ""
    check("AfMap.view(state.freq || 0, AF_MAX, mode, AfMap.cw(currentRadio, state, mode))" in body,
          "the AF frame is placed by AfMap (sideband + CW pitch)")
    check("const lsb = view.lsb;" in body, "the FFT bins mirror exactly when AfMap says LSB side")
    check("scope.pushSweep(view, data);" in body, "the frame is pushed with AfMap's meta (marker in CW)")
    check('"CW-LSB", "RTTY-LSB"' not in body, "no private sideband list is left in app.js")
    check("scope.freqAtX(scStartPx) + (scope.meta.retune || 0)" in app, "a scope tap adds the CW retune")


@test("unit")
def static_view_passband_in_node_vm(ctx):
    """The RX passband on the scope (tests/passband_test.js): waterfall.js Passband (a Yaesu's width from
    its WIDTH code by mode and NAR, the CAT manuals' table; an Icom's filter_bw; AM / FM unknown), the
    static band-plan view of a radio with no scope drawing it on its sideband with its width beside it
    (never on a sweep or the audio scope), and app.js handing the scope that width on every state frame;
    one WIDTH table for the scope, the WIDTH keys and spots.js."""
    r = subprocess.run(["node", str(REPO / "tests" / "passband_test.js")], capture_output=True, text=True,
                       timeout=120, cwd=str(REPO))
    eq(r.returncode, 0, f"passband_test.js: {r.stdout}{r.stderr}")
    check("ALL PASS" in r.stdout, f"passband_test.js output: {r.stdout}")
    app = (FRONT / "app.js").read_text(encoding="utf-8")
    check("scope.meta.filterBw = Passband.hz(currentRadio, s);" in app, "app.js gives the scope Passband's width")
    check("scope.meta.filterBw = s.filter_bw;" not in app, "... not filter_bw (never updated on a Yaesu)")
    check("const SH_VALID = Passband.SH;" in app and "[17, 3000]" not in app, "app.js's WIDTH keys read the one table")
    wf = (FRONT / "waterfall.js").read_text(encoding="utf-8")
    m = re.search(r"showStatic\(center, span\) \{(.*?)\n    \}\n", wf, re.S)
    check(m and "still: true" in m.group(1), "showStatic marks the band-plan view")
    spots = (FRONT / "spots.js").read_text(encoding="utf-8")
    check("window.Passband" in spots and ".shCode(" in spots and "[17, 3000]" not in spots, "spots.js reads the same table")


@test("unit")
def page_themes_night_day_radio_screen(ctx):
    """The theme button steps night -> day -> radio screen (tr.js), and the scope's own colours follow
    (waterfall.js Scope.setTheme; night and day unchanged), in a Node vm (tests/theme_test.js); app.js
    hands the theme to the scope at load and on every change."""
    r = subprocess.run(["node", str(REPO / "tests" / "theme_test.js")], capture_output=True, text=True,
                       timeout=120, cwd=str(REPO))
    eq(r.returncode, 0, f"theme_test.js: {r.stdout}{r.stderr}")
    check("ALL PASS" in r.stdout, f"theme_test.js output: {r.stdout}")
    app = (FRONT / "app.js").read_text(encoding="utf-8")
    check("scope.setTheme(TR.theme())" in app, "app.js sets the scope's theme at load")
    check('document.addEventListener("tr:theme", (e) => scope.setTheme(' in app, "app.js follows theme changes")
    html = (FRONT / "index.html").read_text(encoding="utf-8")
    check('<time id="utcClock" class="utc-clock"' in html, "the UTC clock element is in the top bar")


@test("unit")
def band_plan_strip_above_the_scope(ctx):
    """The band plan has its own strip just above the scope (#bandStrip): drawn there, never over the
    spectrum trace, aligned with the scope, looked up there (tests/scope_strip_test.js), and wired up
    in index.html / style.css / app.js."""
    r = subprocess.run(["node", str(REPO / "tests" / "scope_strip_test.js")], capture_output=True, text=True,
                       timeout=120, cwd=str(REPO))
    eq(r.returncode, 0, f"scope_strip_test.js: {r.stdout}{r.stderr}")
    check("ALL PASS" in r.stdout, f"scope_strip_test.js output: {r.stdout}")
    html = (FRONT / "index.html").read_text(encoding="utf-8")
    i_strip, i_wrap = html.find('<canvas id="bandStrip"'), html.find('<div class="scope" id="scopeWrap">')
    check(0 < i_strip < i_wrap and "<" not in html[i_strip + 30:i_wrap].replace("</canvas>", "").strip()[:1],
          "index.html: the strip sits right above the scope")
    check('class="band-strip" hidden' in html, "the strip starts hidden (shown while BAND PLAN is on)")
    css = (FRONT / "style.css").read_text(encoding="utf-8")
    check(".band-strip[hidden]{display:none}" in css, "style.css: a hidden strip takes no space")
    app = (FRONT / "app.js").read_text(encoding="utf-8")
    check('new Scope($("spectrum"), $("waterfall"), $("overlay"), $("scopeWrap"), $("bandStrip"))' in app,
          "app.js hands the strip to the scope")
    check("if (strip) strip.hidden = !on;" in app, "BAND PLAN shows / hides the strip")
