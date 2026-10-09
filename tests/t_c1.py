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
    for f in ("account.js", "admin.js", "app.js", "tr.js", "waterfall.js", "cwtool.js", "layout.js"):
        rr = subprocess.run(["node", "--check", str(FRONT / f)], capture_output=True, text=True, timeout=60)
        eq(rr.returncode, 0, f"node --check {f}: {rr.stderr}")


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
    check("(tuned / 1e6).toFixed(4)" in body, "the centre (tuned) label stays on the canvas")
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
                 "only for radios that report their", "Icom TX‑state limitation", "1C 00",
                 "120 s session clock", "`tx_route=True`", "`tx_tot=True`", "only an explicit STOP stops"):
        check(must in d, f"ADDING-A-RADIO.md says: {must}")
    p = _doc("backend/profiles.py").split('"""', 2)[1]
    check("RF power 0% on connect" not in p and "read (not forced)" in p, "profiles.py: power is read, not forced")
    for must in ("owner's drop unkeys at once", "after 6 s", "other clients' drops never", "120 s session clock",
                 "does not read TX state", "tx_route"):
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
