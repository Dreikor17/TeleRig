"""C1 review carry-over fixes (spec §7 C1). The behaviour is tested on the REAL frontend code in a Node vm
(tests/c1_ui_test.js on tests/fakedom.js; the CW STOP race in tests/stop_arm_test.js, run by t_admin_ui);
these unit tests run it and pin the wiring. They read files only: no server, no radio, no TX.

1. cwtool.js: a tap on the CW STOP control never starts a transmission (stop_arm_test.js S9-S16).
2. account.js: only a 2xx answer is a sign-out; 401 / 403 / 429 / 5xx / network are handled honestly.
3. admin.js: a temporary password is shown exactly once and scrubbed on the next render.
4. app.js: the top-bar STOP uses the shared TR.stopArm / TR.ownerKey (no private copy of the rule).
5. waterfall.js: the scope edge frequencies are drawn once (the HTML #lblLeft/#lblRight).
6. style.css / layout.js: the default PTT tile fits with and without the ATAS row (G-15, D-11).
7. tr.js / login.html: the 4401 close reason role_changed.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from harness import check, eq, test

REPO = Path(__file__).resolve().parent.parent
FRONT = REPO / "frontend"


def _read(name: str) -> str:
    return (FRONT / name).read_text(encoding="utf-8")


def _strip_comments(js: str) -> str:
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"(?m)(^|\s)//.*$", r"\1", js)


@test("unit")
def c1_ui_fixes_in_node_vm(ctx):
    """account.js sign-out, admin.js one-time secret, app.js top-bar STOP, waterfall.js edge labels and the
    tr.js role_changed close, each on the real script in a Node vm (no browser, no server)."""
    r = subprocess.run(["node", str(REPO / "tests" / "c1_ui_test.js")], capture_output=True, text=True,
                       timeout=120, cwd=str(REPO))
    eq(r.returncode, 0, f"c1_ui_test.js: {r.stdout}{r.stderr}")
    check("ALL PASS" in r.stdout, f"c1_ui_test.js output: {r.stdout}")
    for f in ("account.js", "admin.js", "app.js", "tr.js", "waterfall.js", "cwtool.js", "layout.js", "bridge.js",
              "device.js", "recorder.js", "spots.js", "sstv.js", "sstv-tx.js", "ft8-decode.js", "ft8.js", "rtty.js", "digital.js"):
        rr = subprocess.run(["node", "--check", str(FRONT / f)], capture_output=True, text=True, timeout=60)
        eq(rr.returncode, 0, f"node --check {f}: {rr.stderr}")


@test("unit")
def ptt_hotkey_in_node_vm(ctx):
    """app.js PTT hotkey: the PTT button's fail-safe rule for a key. A press while this tab owns or claims
    ptt always unkeys; keying is held back by the re-key guard, another owner, a hidden tab, an open
    dialog / menu / Arrange mode, a text field (for a key that types), auto-repeat and chords; hold to
    talk unkeys on key-up and on window blur. The real tr.js + app.js in a Node vm against an in-memory
    fake server (no browser, no server, no radio)."""
    r = subprocess.run(["node", str(REPO / "tests" / "ptt_hotkey_test.js")], capture_output=True, text=True,
                       timeout=180, cwd=str(REPO))
    eq(r.returncode, 0, f"ptt_hotkey_test.js: {r.stdout}{r.stderr}")
    check("ALL PASS" in r.stdout, f"ptt_hotkey_test.js output: {r.stdout}")
    app = _strip_comments(_read("app.js"))
    m = re.search(r"function onPttKeyDown\(e\) \{(.*?)\n  \}", app, re.S)
    check(m, "app.js onPttKeyDown")
    body = m.group(1)
    i_rel, i_press = body.find("pttRelease()"), body.find("pttPress(0)")
    check(0 <= i_rel < i_press, "onPttKeyDown order: the release path first, then the shared pttPress (guard, busy, hidden page)")
    check('send({ action: "ptt", tx: true })' not in body, "the hotkey never sends a ptt claim of its own: it goes through pttPress")


@test("unit")
def device_and_bridge_ui_in_node_vm(ctx):
    """device.js (full screen + screen wake lock, the PTT hotkey dialog) and bridge.js (the WSJT-X bridge
    indicator: states, the panel per role, pause / resume) on the real scripts in a Node vm, with fake
    Fullscreen / Wake Lock APIs (no browser, no server, no radio)."""
    r = subprocess.run(["node", str(REPO / "tests" / "device_bridge_ui_test.js")], capture_output=True, text=True,
                       timeout=180, cwd=str(REPO))
    eq(r.returncode, 0, f"device_bridge_ui_test.js: {r.stdout}{r.stderr}")
    check("ALL PASS" in r.stdout, f"device_bridge_ui_test.js output: {r.stdout}")
    for f in ("bridge.js", "device.js"):
        js = _strip_comments(_read(f))
        check("innerHTML" not in js and "insertAdjacentHTML" not in js and "outerHTML" not in js, f"{f} never parses text as HTML")
        check("eval(" not in js and "new Function" not in js, f"{f} has no eval")


@test("unit")
def ptt_failsafe_in_node_vm(ctx):
    """app.js PTT (final review round 3): while this tab owns or claims ptt, every press unkeys (any
    e.detail, keyboard, touch, pen); only keying is held back (idle detail > 1, a key's auto-repeat, the
    re-key guard). A VOX / CW / PTT claim granted while the tab is hidden is released. The real tr.js +
    app.js in a Node vm against an in-memory fake server (no browser, no server, no radio)."""
    r = subprocess.run(["node", str(REPO / "tests" / "ptt_failsafe_test.js")], capture_output=True, text=True,
                       timeout=180, cwd=str(REPO))
    eq(r.returncode, 0, f"ptt_failsafe_test.js: {r.stdout}{r.stderr}")
    check("ALL PASS" in r.stdout, f"ptt_failsafe_test.js output: {r.stdout}")
    app = _strip_comments(_read("app.js"))
    m = re.search(r"function pttPress\(detail\) \{(.*?)\n  \}", app, re.S)
    check(m, "app.js pttPress")
    body = m.group(1)
    i_rel, i_guard, i_key = body.find("pttRelease()"), body.find("pttGuardUntil"), body.find("tx: true")
    check(0 <= i_rel < i_guard < i_key, "pttPress order: release first (never refused), then the guard, then the key")
    check("e.detail > 1) return" not in app, "no click handler drops a press by its detail before the release path")


@test("unit")
def c1_cw_stop_never_transmits_wiring(ctx):
    cw = _strip_comments(_read("cwtool.js"))
    # the STOP role: kept while CW is on the air (poll + tap), checked before the TX path, extended by a guarded tap
    check("const STOP_GUARD_MS = 3000;" in cw, "the STOP guard window")
    m = re.search(r'txBtn\.addEventListener\("click", \(\) => \{(.*?)\n    \}\);', cw, re.S)
    check(m, "the CW TX/STOP click handler")
    h = m.group(1)
    i_stop, i_guard, i_send = h.find("if (st.cw_tx)"), h.find("inStopRole(now)"), h.find('action: "cw_tx"')
    check(0 <= i_stop < i_guard < i_send, "order: STOP path, then the guard, then the one cw_tx send")
    check("stopGuardUntil = 0" not in cw, "no path resets the guard (a later tap of the burst would transmit)")
    eq(cw.count('action: "cw_tx"'), 1, "cwtool sends cw_tx from exactly one place")
    check(re.search(r"if \(tx\) keepStopRole\(now\);", cw), "the state poll keeps the STOP role while CW is on the air")
    check("guard ||" in cw, "the button is inert during the guard window")


@test("unit")
def c1_top_bar_stop_uses_the_shared_helper(ctx):
    app = _strip_comments(_read("app.js"))
    check("TR.stopArm(" in app, "app.js builds its top-bar STOP on TR.stopArm")
    check('if (topStop.tap(o) === "fire") send({ action: "tx_stop" });' in app, "tx_stop only on fire")
    for gone in ("function ownerKey", "stopArmedFor", "stopArmed ="):
        check(gone not in app, f"app.js still has its own copy of the arm rule ({gone})")
    eq(app.count('action: "tx_stop"'), 2, "app.js sends tx_stop from the top-bar STOP and the owner's tune stop only")


@test("unit")
def c1_signout_and_one_time_secret_wiring(ctx):
    acc = _strip_comments(_read("account.js"))
    check('outcome = "ok"' in acc and "if (outcome === \"ok\") { location.replace(\"/login?reason=signed_out\"); return true; }" in acc,
          "only a 2xx answer navigates as signed out")
    check('"/login?reason=expired"' in acc, "a 401 on sign-out-everywhere does not claim a sign-out everywhere")
    check("TR.cancelLeave" in acc and "TR.cancelLeave = function" in _read("tr.js"), "a failed sign-out hands held closes back")
    adm = _strip_comments(_read("admin.js"))
    m = re.search(r"function secretPanel\(\) \{(.*?)\n  \}", adm, re.S)
    check(m and "secret = null;" in m.group(1).split("const lines", 1)[0], "secretPanel takes the secret (shown once)")
    check(re.search(r"function select\(id, opts\) \{[^}]*scrubSecret\(\);", adm, re.S), "every render scrubs the shown secret")
    check("function onClosed() {" in adm and "scrubSecret();" in adm.split("function onClosed() {", 1)[1][:300],
          "closing the dialog scrubs it")


@test("unit")
def c1_scope_edges_drawn_once(ctx):
    wf = _strip_comments(_read("waterfall.js"))
    m = re.search(r"drawOverlay\(\) \{(.*?)\n    \}\n", wf, re.S)
    check(m, "drawOverlay")
    body = m.group(1)
    check("flo / 1e6" not in body and "fhi / 1e6" not in body, "the canvas no longer draws the edge frequencies")
    check("(tuned / 1e6).toFixed(dec)" in body, "the centre (tuned) label stays on the canvas")
    html, app = _read("index.html"), _read("app.js")
    check('id="lblLeft"' in html and 'id="lblRight"' in html, "the HTML edge labels stay")
    check('$("lblLeft").textContent = formatFreq(lo);' in app, "app.js fills them")


@test("unit")
def c1_ptt_tile_fits_with_and_without_atas(ctx):
    """G-15 / D-11: one compact ATAS row. Arithmetic over the CSS the browser check measured (B2b/C1 report):
    desktop default tile body at 1280x800 = 84 px content, at 1280x720 = 69 px."""
    css = _read("style.css")
    rule = lambda sel: re.search(re.escape(sel) + r"\{([^}]*)\}", css)
    row = rule(".tile-ptt #atasTune")
    btn = rule(".tile-ptt #atasTune .atas-btn")
    hint = rule(".tile-ptt #atasTune .atas-hint")
    check(row and "padding:3px 6px" in row.group(1), f"compact ATAS row padding: {row and row.group(1)}")
    check(btn and "padding:3px 10px" in btn.group(1), f"compact ATAS steps: {btn and btn.group(1)}")
    check(hint and "white-space:nowrap" in hint.group(1) and "text-overflow:ellipsis" in hint.group(1), "the hint stays on one line")
    check("@container (max-width:240px){ .tile-ptt #atasTune .atas-hint{display:none} }" in css, "narrow tile: no hint")
    m = re.search(r"@container \(max-height:80px\)\{(.*?)\n\}", css, re.S)
    check(m and "min-height:34px" in m.group(1) and "gap:4px" in m.group(1), "short desktop body: PTT 34 px, gap 4 px")
    # row = 15 px glyph (line-height 1) + 2*3 padding + 2 key border + 2*3 row padding + 2 row border = 31 px
    row_px = 15 + 6 + 2 + 6 + 2
    eq(row_px, 31, "ATAS row height")
    check(44 + 6 + row_px <= 84, "1280x800: PTT 44 + gap 6 + ATAS fits the 84 px body")
    check(34 + 4 + row_px <= 69, "1280x720: PTT 34 + gap 4 + ATAS fits the 69 px body")
    lay = _read("layout.js")
    check("bodyPxAlt: 84, bodyPxAltPhone: 104" in lay, "layout.js minimums follow the compact row (desktop) and the touch row (phone)")
    check('return (d === "phone" && t.bodyPxAltPhone) || t.bodyPxAlt;' in lay, "the phone minimum is device-aware")
    # desktop minH at the 1280x800 cell (28 px) with ATAS == the default h 5, so the default never sits below its minimum
    eq(-(-(30 + 84 + 16 + 8 + 2) // 28), 5, "desktop minH with ATAS at 28 px cells")
    eq(-(-(38 + 104 + 16 + 8 + 2) // 32), 6, "phone rows with ATAS (§3.6: 6 with the ATAS row)")


@test("unit")
def c1_role_changed_close_reason(ctx):
    tr, login = _read("tr.js"), _read("login.html")
    check('"role_changed"' in tr.split("LOGIN_REASONS", 1)[1].split("\n", 1)[0], "tr.js passes reason=role_changed to /login")
    check('reason === "role_changed" ? "role_changed" : "session_revoked"' in tr, "the 4401 cap keeps role_changed")
    check('role_changed: "Your role changed' in login, "login.html explains it")


# ---- C1 documentation (spec §7 C1) ---------------------------------------------------------------
def _doc(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


@test("unit")
def c1_docs_tx_safety_contract_matches_f6(ctx):
    d = _doc("docs/ADDING-A-RADIO.md")
    check("ANY client drop while keyed unkeys" not in d, "the pre-F6 rule is gone")
    for must in ("the TX owner's drop unkeys immediately", "a silent owner is unkeyed after 6 s",
                 "other clients' drops never unkey", "never unkeyed by a browser drop",
                 "only for radios that report their", "**Reporting TX state.**", "1C 00",
                 "a lost or stale read never keeps a carrier up", "IC‑728",
                 "120 s session clock", "`tx_route=True`", "`tx_tot=True`", "only an explicit STOP stops"):
        check(must in d, f"ADDING-A-RADIO.md says: {must}")
    p = _doc("backend/profiles.py").split('"""', 2)[1]
    check("RF power 0% on connect" not in p and "read (not forced)" in p, "profiles.py: power is read, not forced")
    check("Icom TX‑state limitation" not in d and "does not read TX state" not in p,
          "the Phase 1 Icom TX-state limitation is gone (the 1C 00 read)")
    for must in ("owner's drop unkeys at once", "after 6 s", "other clients' drops never", "120 s session clock",
                 "handlers read TX state", "Icom CI-V `1C 00`", "tx_route"):
        check(must in p, f"profiles.py docstring says: {must}")
    cw = _read("cwtool.js").split("*/", 1)[0]
    check("NO transmit; it never keys" not in cw and "CW TX:" in cw and "DOES key the radio" in cw,
          "cwtool.js header: the coder CAN key the radio via CW TX for operators")


@test("unit")
def c1_docs_remote_access_readme_roadmap_notice(ctx):
    ra = _doc("docs/REMOTE-ACCESS.md")
    check("no login" not in ra.lower() and "🌐" not in ra, "REMOTE-ACCESS.md: no v1 'no login' / Remote-button text")
    for must in ("`run.bat --reset-admin`", "`python3 run.py --reset-admin`", "setup code", "only on the host PC",
                 "Tailscale Serve", "Do NOT use Tailscale Funnel", "Allowed names", "Allowed origins",
                 "exact-origin rule", "X-Forwarded-Host", "behind_proxy", "loopback isn't the host PC",
                 "`*.ts.net`", "`~/.telerig`", "`~/.radio-webop`", "migrated password", "Lockouts",
                 "**8**", "role_changed", "Turn on"):
        check(must in ra, f"REMOTE-ACCESS.md covers: {must}")
    rd = _doc("README.md")
    check("## Sign-in & users" in rd, "README: Sign-in & users section")
    for gone in ("no login", "optional\nshared-password", "optional shared password", "🌐 Remote", "the local host is exempt"):
        check(gone not in rd, f"README no longer says: {gone!r}")
    for must in ("**viewer**", "**operator**", "**admin**", "Arrange mode", "**Administration**", "setup code"):
        check(must in rd, f"README mentions {must}")
    rm = _doc("ROADMAP.md")
    done = rm.split("## Done", 1)[1].split("## Planned", 1)[0]
    check("### Phase 1" in done and done.index("### Phase 1") < done.index("### v0.2.25"), "ROADMAP: Phase 1 is the newest Done entry")
    for must in ("Modular panels", "Multi-user sign-in", "One TX owner"):
        check(must in done, f"ROADMAP Done: {must}")
    check("today there is no login" not in rm and "*shipped (Phase 1)*" in rm, "ROADMAP: Authentication marked shipped")
    nt = _doc("NOTICE")
    check("GridStack" in nt and "Lucide" in nt, "NOTICE lists GridStack and Lucide")
    check("permanent Source button in the top bar" in nt and "sign-in" in nt, "NOTICE: the AGPL §13 source links")


STALE = re.compile(r"no login|local_exempt|set_password|host_rx|host_tx|rw_session|python run\.py --reset-admin")
# every remaining hit is intentional: the v2 password setters and the admin reset route, the legacy cookie
# that login/logout expire, the removed host-audio actions (denied, I2), comments/tests about the removals,
# and the migration notes in REMOTE-ACCESS.md
STALE_OK = {
    "backend/accounts.py": ("def set_password(", "def admin_set_password("),
    "backend/account_api.py": ('"""set_password with a precomputed hash',),
    "backend/admin_api.py": ("reset_password", "admin_set_password"),
    "backend/auth.py": ('LEGACY_COOKIES = ("tr_session", "rw_session")',),
    "backend/policy.py": ('REMOVED = frozenset({"host_rx", "host_tx"})',),
    "backend/server.py": ("REMOVED (host_rx / host_tx)", "like /api/set_password."),
    "frontend/admin.js": ('"/reset_password"',),
    "docs/REMOTE-ACCESS.md": ("`rw_session` cookies", "`/api/set_password` are gone"),
}


@test("unit")
def c1_no_stale_auth_terms(ctx):
    files = sorted({*REPO.glob("backend/*.py"), *REPO.glob("frontend/*.js"), *REPO.glob("frontend/*.html"),
                    *REPO.glob("docs/*.md"), REPO / "run.py", REPO / "README.md"})
    bad = []
    for f in files:
        rel = f.relative_to(REPO).as_posix()
        if rel.endswith("_test.py"):
            continue                                  # unit tests name the removed things on purpose
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if STALE.search(line) and not any(ok in line for ok in STALE_OK.get(rel, ())):
                bad.append(f"{rel}:{n}: {line.strip()[:100]}")
    eq(bad, [], "unexpected stale auth terms")


@test("unit")
def band_and_mode_keys_lit_after_reload_in_node_vm(ctx):
    """app.js: the BAND and MODE keys are lit after a reload while a radio is connected, even when its
    state frame beats /api/radios (renderRadio used to rebuild them unlit, dark until the freq or mode
    changed); never from a state that isn't the rendered radio's own. The real tr.js + app.js in a Node
    vm (tests/radio_keys_test.js; no browser, no server, no radio)."""
    r = subprocess.run(["node", str(REPO / "tests" / "radio_keys_test.js")], capture_output=True, text=True,
                       timeout=180, cwd=str(REPO))
    eq(r.returncode, 0, f"radio_keys_test.js: {r.stdout}{r.stderr}")
    check("ALL PASS" in r.stdout, f"radio_keys_test.js output: {r.stdout}")


@test("unit")
def vfo_main_screen_and_typed_frequency_in_node_vm(ctx):
    """The VFO (look A, an Icom-style main screen with the meter along its bottom) and typing a frequency, on
    the real tr.js + app.js + freqentry.js in a Node vm (tests/vfo_test.js): the parser (MHz, the VFO's
    grouping, a leading point, bare numbers by the radio's coverage, units, refusals), the small sub-kHz
    digits, the filter width, the offsets and the lit strip, click / Enter to type, Enter tunes and Esc
    cancels, the Controls box reads the same way, the meter (S on receive, the TX meter from the CI-V
    reference's marks on transmit, a Yaesu's raw reading, the peak). Static: freqentry.js only ever sends
    set_freq and never parses text as HTML."""
    r = subprocess.run(["node", str(REPO / "tests" / "vfo_test.js")], capture_output=True, text=True,
                       timeout=180, cwd=str(REPO))
    eq(r.returncode, 0, f"vfo_test.js: {r.stdout}{r.stderr}")
    check("ALL PASS" in r.stdout, f"vfo_test.js output: {r.stdout}")
    js = _strip_comments(_read("freqentry.js"))
    for bad in ("innerHTML", "insertAdjacentHTML", "outerHTML", "eval(", "new Function", "fetch(", "WebSocket"):
        check(bad not in js, f"freqentry.js never uses {bad}")
    eq(sorted(set(re.findall(r'action: "([a-z_]+)"', js))), ["set_freq"], "freqentry.js sends only set_freq")


@test("unit")
def scope_never_tunes_by_scrolling_and_step_keys_in_node_vm(ctx):
    """Tuning from the Scope window (tests/scope_tune_test.js, the real tr.js + app.js in a Node vm): nothing
    that scrolls tunes on the scope (no wheel, no finger or pen moving across it: scrolling the page over
    the scope used to retune the radio); a tap or a click still goes to the frequency under it and a mouse
    drag still pans; the step keys in the scope's bar step the frequency (a tap one step at its click, a
    held key every 100 ms until it is let go, cancelled, slid off, hidden, or 30 s are up); operators only,
    not while arranging; the step is one setting with two pickers, kept per radio. Static: the scope has no
    wheel listener and pans for the mouse only; the step keys queue the frequency's tune and send nothing
    themselves; the page and the styles carry what the script relies on."""
    r = subprocess.run(["node", str(REPO / "tests" / "scope_tune_test.js")], capture_output=True, text=True,
                       timeout=180, cwd=str(REPO))
    eq(r.returncode, 0, f"scope_tune_test.js: {r.stdout}{r.stderr}")
    check("ALL PASS" in r.stdout, f"scope_tune_test.js output: {r.stdout}")
    js = _strip_comments(_read("app.js"))
    eq(re.findall(r'(\w+)\.addEventListener\("wheel"', js), ["el", "bandsEl"],
       "app.js: the wheel acts on a focused slider and on the VFO's digits only, never on the scope")
    check('if (scMoved > 6 && scType === "mouse") {' in js, "app.js: only a mouse drag pans the scope")
    key = js.split("function stepKey(btn, dir) {", 1)
    check(len(key) == 2, "app.js stepKey()")
    body = key[1].split("\n  stepKey($(\"stepDn\"), -1);", 1)[0]
    check("queueTune(dir * step)" in body and "send(" not in body and "action:" not in body,
          "the step keys queue the frequency's tune delta and send nothing themselves")
    check('stepKey($("stepDn"), -1);' in js and 'stepKey($("stepUp"), 1);' in js, "- steps down, + up")
    html = (FRONT / "index.html").read_text(encoding="utf-8")
    m = re.search(r'<span class="sc-step" id="scopeStepCtl" data-gate="operate"[^>]*>(.*?)</span>', html, re.S)
    check(m, "index.html: the step keys, operators only (data-gate)")
    ids = re.findall(r'<(button|select)\b[^>]*\bid="(\w+)"', m.group(1) if m else "")
    eq(ids, [("button", "stepDn"), ("select", "scopeStep"), ("button", "stepUp")], "... -, the step, +")
    for bid in ("stepDn", "stepUp"):
        tag = re.search(r'<button\b[^>]*\bid="' + bid + r'"[^>]*>', html).group(0)
        check('type="button"' in tag and "aria-label=" in tag and "data-act" not in tag and "data-tx" not in tag,
              f"#{bid}: a plain labelled button (no radio action of its own): {tag}")
    check('id="scopeHint"' not in html and "scroll it too" not in html, "the bar's old hint (scroll to tune) is gone")
    check('<span class="sth-mouse">click or drag the waterfall to tune</span><span class="sth-touch">tap the waterfall to tune</span>' in html,
          "the waterfall's hint: a mouse clicks or drags, a finger taps")
    css = re.sub(r"/\*.*?\*/", "", (FRONT / "style.css").read_text(encoding="utf-8"), flags=re.S)
    check("#scopeWrap{touch-action:pan-y}" in css, "style.css: a swipe up or down over the scope scrolls the page")
    k = re.search(r"\.sc-step-key\{([^}]*)\}", css)
    check(k and "touch-action:none" in k.group(1) and "user-select:none" in k.group(1) and "-webkit-touch-callout:none" in k.group(1),
          f"style.css: a held step key doesn't scroll the page, select its label or open the long-press menu: {k and k.group(1)}")
    check("@media (hover:none){.sth-mouse{display:none}.sth-touch{display:inline}}" in css, "style.css: the finger's hint on a touch screen")
    phone = css.split("@media (max-width:680px){", 1)[1]
    check(re.search(r"\.sc-step \.sc-step-key\{[^}]*min-height:44px", phone), "style.css: thumb-sized step keys on a phone")


@test("unit")
def recorder_panel_in_node_vm(ctx):
    """recorder.js: MP3 through LAME from the radio's own PCM (RadioAudio.tapPcm, before VOL: mono, the
    radio's 16 kHz, 48 kb/s, dropouts filled with silence), WebM/Opus via MediaRecorder on the pre-volume RX
    bus only without the encoder; only while RX audio is on; the level bar, the time, the list with
    downloads named after the radio / freq / mode / UTC start, delete, the stop when RX audio goes off,
    the leave-page guard; "Also save a lossless WAV" (kept per browser, locked while recording): the same
    PCM sample for sample in a 16-bit mono WAV with the MP3's dropout silence, a second download, the
    guard until both are saved. The real script in a Node vm with a fake LAME, PCM tap, audio graph and
    MediaRecorder (tests/recorder_test.js). Static: it never sends a frame, never parses text as HTML and
    never touches the speakers' path."""
    r = subprocess.run(["node", str(REPO / "tests" / "recorder_test.js")], capture_output=True, text=True,
                       timeout=180, cwd=str(REPO))
    eq(r.returncode, 0, f"recorder_test.js: {r.stdout}{r.stderr}")
    check("ALL PASS" in r.stdout, f"recorder_test.js output: {r.stdout}")
    js = _strip_comments(_read("recorder.js"))
    for bad in (".send(", "innerHTML", "insertAdjacentHTML", "outerHTML", "eval(", "new Function", "destination",
                "WebSocket", "fetch("):
        check(bad not in js, f"recorder.js never uses {bad}")
    check("A.tapPcm(" in js and "lamejs.Mp3Encoder(1, rate, MP3_KBPS)" in js, "recorder.js encodes the radio's PCM to MP3")
    check("A.bus()" in js and "createMediaStreamDestination" in js, "recorder.js falls back to recording the RX bus")
    html = (FRONT / "index.html").read_text(encoding="utf-8")
    check(re.search(r'<label class="rec-opt"[^>]*><input type="checkbox" id="recWav"> Also save a lossless WAV</label>', html),
          "the Recorder has the lossless WAV box")


@test("unit")
def rx_volume_db_scale_with_headroom_in_node_vm(ctx):
    """app.js VOL: a dB scale with headroom (0 = off, 1..100 = -36 .. +24 dB, 60 = 0 dB = the audio as the
    radio sends it, the old top), a limiter before the speakers, the dB readout, kept per browser (junk
    ignored); the tools' RX bus stays before VOL; RadioAudio.tapPcm hands the Recorder each frame's own
    samples. The real tr.js + app.js in a Node vm with a fake audio context (tests/volume_test.js)."""
    r = subprocess.run(["node", str(REPO / "tests" / "volume_test.js")], capture_output=True, text=True,
                       timeout=180, cwd=str(REPO))
    eq(r.returncode, 0, f"volume_test.js: {r.stdout}{r.stderr}")
    check("ALL PASS" in r.stdout, f"volume_test.js output: {r.stdout}")


@test("unit")
def spots_panel_in_node_vm(ctx):
    """spots.js: asks /api/spots only while the panel is on the grid (not again before 30 s); the list
    newest first with text kept as text; a click tunes (set_freq, then the mode in this radio's names:
    SSB by band, CW / CW-USB, DATA-USB / USB with the Icom DATA switch, FM, AM; and the filter for it: an
    Icom FIL slot, a Yaesu NAR off + WIDTH, nothing on the IC-728; each only when the radio isn't there
    yet; an unknown mode tunes the frequency only); spots off the radio's bands, or for a viewer, can't be
    clicked; the filters work and are kept; a silent source is named. The real tr.js + spots.js in a
    Node vm (tests/spots_ui_test.js), on its own profiles and again on the real ones (to_json() of every
    profile in backend/profiles.py). Static: no HTML parsing, only the tuning actions are sent."""
    import json
    import sys
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from backend import profiles
    real = ctx.tmp("profiles") / "profiles.json"
    real.write_text(json.dumps({pid: p.to_json() for pid, p in profiles.PROFILES.items()}), encoding="utf-8")
    for args in ([], ["--profiles", str(real)]):
        r = subprocess.run(["node", str(REPO / "tests" / "spots_ui_test.js"), *args], capture_output=True, text=True,
                           timeout=180, cwd=str(REPO))
        eq(r.returncode, 0, f"spots_ui_test.js {' '.join(args)}: {r.stdout}{r.stderr}")
        check("ALL PASS" in r.stdout, f"spots_ui_test.js {' '.join(args)} output: {r.stdout}")
    js = _strip_comments(_read("spots.js"))
    for bad in ("innerHTML", "insertAdjacentHTML", "outerHTML", "eval(", "new Function"):
        check(bad not in js, f"spots.js never uses {bad}")
    eq(sorted(set(re.findall(r'action: "([a-z_]+)"', js))),
       ["data_mode", "narrow", "set_filter", "set_freq", "set_mode", "width"],
       "spots.js sends only the tuning actions: freq, mode, DATA, filter, NAR, WIDTH")


@test("unit")
def sstv_receive_in_node_vm(ctx):
    """sstv.js (receive only): the decoder against a reference encoder (tests/sstv_encode.js) in every
    mode, in noise, mistuned, from a station with a fast clock, joined mid-picture, never started by
    noise, an unknown VIS reported, a lost signal ended (tests/sstv_test.js); the panel on the real
    script: RX hint, painting, status, the PNG strip, off-grid, the mode menu, Clear
    (tests/sstv_panel_test.js). Static: it never sends a radio action and never parses text as HTML."""
    for name in ("sstv_test.js", "sstv_panel_test.js"):
        r = subprocess.run(["node", str(REPO / "tests" / name)], capture_output=True, text=True, timeout=300, cwd=str(REPO))
        eq(r.returncode, 0, f"{name}: {r.stdout}{r.stderr}")
        check("ALL PASS" in r.stdout, f"{name} output: {r.stdout}")
    js = _strip_comments(_read("sstv.js"))
    for bad in ("innerHTML", "insertAdjacentHTML", "outerHTML", "eval(", "new Function", ".send(", "WebSocket", "fetch("):
        check(bad not in js, f"sstv.js never uses {bad}")


@test("unit")
def cw_chat_splits_rx_after_a_pause_in_node_vm(ctx):
    """cwtool.js: received text after a pause of 5 s or more (new characters, committed or in the live
    preview) starts a new RX bubble; text committed later stays where it showed; Clear forgets the pause.
    CW decodes by default (CW coming into view turns Decode on, with the real Modem window too); the
    chat follows new text only at its end. The real tr.js + cwtool.js (+ digital.js) in a Node vm with a
    stand-in decoder worker (tests/cw_chat_test.js). Static: the decoder never makes the page's audio
    context (one made before a click stays paused, and the mic would send silence): only Play does."""
    r = subprocess.run(["node", str(REPO / "tests" / "cw_chat_test.js")], capture_output=True, text=True,
                       timeout=180, cwd=str(REPO))
    eq(r.returncode, 0, f"cw_chat_test.js: {r.stdout}{r.stderr}")
    check("ALL PASS" in r.stdout, f"cw_chat_test.js output: {r.stdout}")
    js = _strip_comments(_read("cwtool.js"))
    eq(js.count(".ensure()"), 1, "cwtool.js makes the page's audio in one place")
    play = js.split("function playMorse(text) {", 1)
    check(len(play) == 2 and ".ensure()" in play[1].split("\n  }\n", 1)[0], "... Play's click (playMorse), never the decoder")


@test("unit")
def af_asks_before_the_radio_speaker_goes_up_in_node_vm(ctx):
    """AF is the radio's own speaker (in the room with the radio): it starts at 0 on every connect
    (backend/af_zero_test.py), and raising it from 0 asks first: nothing is sent while dragging;
    "Keep it at 0" puts it back and sends nothing; "Turn it up" sends the value released at, then AF
    moves freely until it is back at 0; AF already up at the radio, or any other level, never asks.
    The real tr.js + app.js in a Node vm (tests/af_warn_test.js)."""
    r = subprocess.run(["node", str(REPO / "tests" / "af_warn_test.js")], capture_output=True, text=True,
                       timeout=180, cwd=str(REPO))
    eq(r.returncode, 0, f"af_warn_test.js: {r.stdout}{r.stderr}")
    check("ALL PASS" in r.stdout, f"af_warn_test.js output: {r.stdout}")


@test("unit")
def rf_power_slider_is_in_the_tx_tab(ctx):
    """The PWR slider (RF power) lives in the Controls TX tab, in the TX group before MIC; the RX tab's
    LEVELS group keeps AF / RF / SQL only."""
    html = (FRONT / "index.html").read_text(encoding="utf-8")
    def panel(name):
        m = re.search(r'<div class="panel[^"]*" data-p="' + name + r'">(.*?)</div><!-- /' + name + r' -->', html, re.S)
        check(m is not None, f"index.html has the {name} panel")
        return m.group(1) if m else ""
    rx, tx = panel("rx"), panel("tx")
    eq(html.count('id="rfpwr"'), 1, "one PWR slider")
    check('id="rfpwr"' in tx and 'id="rfpwr"' not in rx, "PWR is in the TX tab, not the RX tab")
    start = tx.find('id="txGrp"')
    end = tx.find('<div class="grp"', start)                     # the next group (not its grp-title)
    body = tx[start:end if end > 0 else len(tx)] if start >= 0 else ""
    check(0 <= body.find('id="rfpwr"') < body.find('id="mic"'), "... in the TX group, before MIC")
    eq(re.findall(r'<input type="range"[^>]*id="([a-z_]+)"', rx), ["af", "rf", "sql"], "the RX tab's sliders: AF, RF, SQL")


@test("unit")
def sstv_transmit_in_node_vm(ctx):
    """sstv-tx.js (one picture, operator-triggered, on the PTT rules): the modes offered fit the 120 s
    PTT session (117 s on the air at most); the encoder equals the reference encoder sample for sample
    and decodes; Send takes the TX path, waits for the server's ready, sends ONE ptt claim, streams in
    real time only after the grant, releases after the tail has played; STOP at every step; a lost
    claim, a closed panel, busy / denied / no ready / a device error / a claim that can't be sent key
    nothing; never for a viewer, without PTT, or in CW / RTTY / DV / C4FM (tests/sstv_tx_test.js).
    Static: its only radio action is ptt, claimed in one place; it never parses text as HTML."""
    r = subprocess.run(["node", str(REPO / "tests" / "sstv_tx_test.js")], capture_output=True, text=True,
                       timeout=600, cwd=str(REPO))
    eq(r.returncode, 0, f"sstv_tx_test.js: {r.stdout}{r.stderr}")
    check("ALL PASS" in r.stdout, f"sstv_tx_test.js output: {r.stdout}")
    js = _strip_comments(_read("sstv-tx.js"))
    for bad in ("innerHTML", "insertAdjacentHTML", "outerHTML", "eval(", "new Function", "WebSocket", "fetch(", "XMLHttpRequest"):
        check(bad not in js, f"sstv-tx.js never uses {bad}")
    eq(sorted(set(re.findall(r'action: "([a-z_]+)"', js))), ["ptt"], "sstv-tx.js sends no radio action but ptt")
    eq(js.count('action: "ptt", tx: true'), 1, "sstv-tx.js claims ptt in one place only")
    check('action: "ptt", tx: true, sstv: job.m.id' in js, "sstv-tx.js's claim names the picture's mode")


@test("unit")
def digital_modes_window_in_node_vm(ctx):
    """The Modem window (digital.js; the tile id stays "sstv"): CW, RTTY, FT8 and SSTV, one mode shown, every
    decoder listening, a dot for news in a hidden mode, no other mode while a picture or a CW message goes
    out; CW is the CW panel moved in (cwtool.js: tests/cw_chat_test.js), the CW tile gone; RTTY (rtty.js:
    tests/rtty_test.js) and FT8 (ft8-decode.js against a real recording and WSJT-X's own decodes of it:
    tests/ft8_test.js; ft8.js, the slots, the worker and the list: tests/digital_test.js). Static: RTTY and
    FT8 decode only (no radio action, no socket, no fetch) and never parse text as HTML; the decoder carries
    ft8_lib's MIT notice, and the test recording its own."""
    for name in ("ft8_test.js", "rtty_test.js", "digital_test.js"):
        r = subprocess.run(["node", str(REPO / "tests" / name)], capture_output=True, text=True, timeout=600, cwd=str(REPO))
        eq(r.returncode, 0, f"{name}: {r.stdout}{r.stderr}")
        check("ALL PASS" in r.stdout, f"{name} output: {r.stdout}")
    for f in ("digital.js", "rtty.js", "ft8.js", "ft8-decode.js"):
        js = _strip_comments(_read(f))
        for bad in ("innerHTML", "insertAdjacentHTML", "outerHTML", "eval(", "new Function", ".send(", "WebSocket", "fetch(",
                    "XMLHttpRequest"):
            check(bad not in js, f"{f} never uses {bad}")
        check(not re.search(r'action:\s*"', js), f"{f} sends no radio action")
    dec = _read("ft8-decode.js")
    check("Copyright (c) 2018 Kārlis Goba" in dec and "MIT License" in dec, "ft8-decode.js carries ft8_lib's MIT notice")
    notice = (REPO / "tests" / "data" / "ft8" / "NOTICE")
    check(notice.is_file() and "Kārlis Goba" in notice.read_text(encoding="utf-8"), "tests/data/ft8/NOTICE names the recording's source")
    html = (FRONT / "index.html").read_text(encoding="utf-8")
    for must in ('id="dmCw"', 'id="dmRtty"', 'id="dmFt8"', 'id="dmSstv"',
                 '<div id="dmPaneCw" role="tabpanel" aria-labelledby="dmCw">',
                 '<div id="dmPaneRtty" role="tabpanel" aria-labelledby="dmRtty" hidden>',
                 '<div id="dmPaneFt8" role="tabpanel" aria-labelledby="dmFt8" hidden>',
                 '<div id="dmPaneSstv" role="tabpanel" aria-labelledby="dmSstv" hidden>', 'gs-id="sstv" data-tile="sstv"',
                 'id="th-sstv">MODEM<'):
        check(must in html, f"index.html: {must}")
    keys = re.findall(r'class="dm-mode[^"]*" id="dm(\w+)"', html)
    eq(keys, ["Cw", "Rtty", "Ft8", "Sstv"], "the mode keys: CW, RTTY, FT8, SSTV")
    check('gs-id="cw"' not in html and 'id="cwClose"' not in html, "the CW panel and its close button are gone")
    # the window is as tall as its panel, never its text: the text screens take the room left and scroll
    # inside (a window that grew with its text made the whole panel scroll, the keys and controls with it)
    css = re.sub(r"/\*.*?\*/", "", (FRONT / "style.css").read_text(encoding="utf-8"), flags=re.S)
    for rule in (".tile-digital>.tile-fit{display:flex;flex-direction:column;height:100%}",
                 "#dmPaneCw:not([hidden]),#dmPaneRtty:not([hidden]),#dmPaneFt8:not([hidden]){display:flex;flex-direction:column;flex:1 1 auto;min-height:0}",
                 "#dmPaneCw>.tool-panel{flex:1 1 auto;height:auto;min-height:0}"):
        check(rule in css, f"style.css: {rule}")
    check(not re.search(r"\.tile-digital>\.tile-fit\{[^}]*min-height:100%", css), "the Modem window doesn't grow with its content")
    for sel, scroll in ((".dm-screen", "overflow:auto"), (".cw-out", "overflow-y:auto")):
        m = re.search(r"(?:^|\})\s*" + re.escape(sel) + r"\{([^}]*)\}", css)
        check(m and "flex:1 1 auto" in m.group(1) and scroll in m.group(1) and re.search(r"min-height:\d+px", m.group(1)),
              f"{sel}: takes the room left, scrolls inside, keeps a usable height: {m and m.group(1)}")


@test("unit")
def sstv_on_air_times_agree_with_the_server(ctx):
    """backend/sstv.py's time on the air per mode (the server's TX limit of one picture) is the page's own
    count (sstv-tx.js onAirSeconds) rounded up to 10 ms, for every mode the page knows; every radio that
    can key holds one picture at most 177 s (3 s inside its 3-minute TOT); and the page offers exactly
    the modes the server accepts there."""
    import json
    import sys
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from backend import profiles, sstv
    from backend.radio import ptt_hold_max
    js = ("const fs=require('fs'),vm=require('vm'),path=require('path');"
          "const ctx={window:{},Math,Map,Int16Array,Array,Object};vm.createContext(ctx);"
          "for(const f of ['sstv.js','sstv-tx.js'])vm.runInContext(fs.readFileSync(path.join(@FRONT@,f),'utf8'),ctx);"
          "const S=ctx.window.SSTV,T=ctx.window.SstvTx;"
          "console.log(JSON.stringify({all:S.MODES.map(m=>[m.id,T.onAirSeconds(m)]),offered:T.modes().map(m=>m.id)}));"
          ).replace("@FRONT@", json.dumps(str(FRONT)))
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=120, cwd=str(REPO))
    eq(r.returncode, 0, f"node: {r.stdout}{r.stderr}")
    got = json.loads(r.stdout)
    page = dict(got["all"])
    eq(sorted(page), sorted(sstv.ON_AIR_S), "backend/sstv.py knows exactly the modes the page knows")
    for mid, s in got["all"]:
        py = sstv.ON_AIR_S.get(mid)
        check(py is not None and -1e-6 <= py - s < 0.01, f"{mid}: the server's {py} s is the page's {s:.4f} s, rounded up")
    holds = {pid: ptt_hold_max(p) for pid, p in profiles.PROFILES.items() if p.capabilities.ptt}
    eq(sorted(set(holds.values())), [177], "every radio that can key holds one picture at most 177 s")
    eq(got["offered"], [mid for mid, _ in got["all"] if sstv.fits(mid, 177)],
       "the page offers exactly the modes the server accepts at 177 s")


@test("unit")
def generated_tx_audio_path_in_node_vm(ctx):
    """app.js RadioAudio.txBegin / txPush / txEnd (for sstv-tx.js): the server's mic flag on and off, the
    audio as the mic's binary frames (exactly the samples), nothing before begin or after end, no claim
    (it keys nothing), a viewer refused, a dropped connection gives the path back; the server's
    {type: mic, ready} and TX device errors reach the page as tr:mic / tr:error (tests/tx_audio_test.js)."""
    r = subprocess.run(["node", str(REPO / "tests" / "tx_audio_test.js")], capture_output=True, text=True,
                       timeout=180, cwd=str(REPO))
    eq(r.returncode, 0, f"tx_audio_test.js: {r.stdout}{r.stderr}")
    check("ALL PASS" in r.stdout, f"tx_audio_test.js output: {r.stdout}")


@test("unit")
def every_page_has_its_tab_icon(ctx):
    """Each page declares TeleRig's tab icon inline (the header's radio-tower mark as a data: SVG), so a
    browser never asks for /favicon.ico (a 404 in the server log) and the sign-in page shows it too
    (it can't load /static before sign-in)."""
    for name in ("index.html", "login.html", "change-password.html"):
        html = (FRONT / name).read_text(encoding="utf-8")
        check(html.count('<link rel="icon" href="data:image/svg+xml,') == 1, f"{name} declares the inline tab icon")
        check("favicon.ico" not in html, f"{name} doesn't point at /favicon.ico")
