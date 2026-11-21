"""B4 account and admin UI (spec §5.2-§5.4, §7 B4): the Remote dialog is gone, the account / admin
scripts are wired and bind their menu items, admin.js talks only to the /api/admin/* routes the server
has, text is never parsed as HTML, the sudo retry and the notice buttons exist, and (smoke) the page
serves both scripts. The role-change close reason (`role_changed`) is covered by tx/admin T21 and the
backend ws_gate_test.

`unit` tests read files only. The `smoke` test starts a sim-only server on a free port; no radio is
connected and no TX command is sent. The interactive behaviour is verified in the browser (B4 report).
"""
from __future__ import annotations

import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

import harness
from harness import check, eq, test

REPO = Path(__file__).resolve().parent.parent
FRONT = REPO / "frontend"
REMOTE_IDS = ("remoteBtn", "remoteModal", "remoteClose", "remoteBody")
B4_ALLOW_MISSING = "remoteBtn,remoteModal,remoteClose,remoteBody,rxDev,micDev,cwClose,scopeHint,cwHint"   # cwClose: the CW panel is a Modem mode; scopeHint: the step keys took its place


def _read(name: str) -> str:
    return (FRONT / name).read_text(encoding="utf-8")


class _Tags(HTMLParser):
    """Every start tag as (tag, attrs, [ids/tags of its open ancestors])."""
    VOID = ("input", "br", "img", "meta", "link", "source", "hr", "area", "base", "col", "embed", "param", "track", "wbr")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags: list = []
        self._stack: list = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.tags.append((tag, a, list(self._stack)))
        if tag not in self.VOID:
            self._stack.append(a.get("id") or tag)

    def handle_startendtag(self, tag, attrs):     # <use .../>, <input .../>: no children, nothing to pop
        self.tags.append((tag, dict(attrs), list(self._stack)))

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if self._stack:
            self._stack.pop()


def _tags(html: str) -> list:
    p = _Tags()
    p.feed(html)
    return p.tags


@test("unit")
def b4_remote_dialog_is_gone(ctx):
    html, app, css = _read("index.html"), _read("app.js"), _read("style.css")
    for i in REMOTE_IDS:
        check(f'id="{i}"' not in html, f"#{i} still in index.html")
        check(f'"{i}"' not in app, f"app.js still looks up #{i}")
    for gone in ("/api/remote_status", "/api/set_password", '"/api/tailscale_serve"', "/api/tailscale_serve_off\"",
                 "remoteModal", "rmPwSet"):
        check(gone not in app, f"app.js still has {gone}")
    body = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for gone in (".rm-", ".modal-overlay", ".modal-card", ".modal-body"):
        check(gone not in body, f"style.css still has {gone}")
    # exactly one #modalRoot, a <body> child outside the grid / parking, before the scripts
    tags = _tags(html)
    roots = [t for t in tags if t[1].get("id") == "modalRoot"]
    eq(len(roots), 1, "one #modalRoot")
    check("gridWrap" not in roots[0][2] and "tileParking" not in roots[0][2], f"#modalRoot outside the grid: {roots[0][2]}")
    check(html.index('id="modalRoot"') < html.index("<script src="), "#modalRoot comes before the scripts")
    # the B4 id audit (§3.3 step 10, AR-56): every other id of the v0.2.25 index.html (the page Phase 1
    # started from; `dev` until the v0.3.0 release moved it) is still there once
    r = subprocess.run([sys.executable, str(REPO / "tests" / "check_ids.py"), "--allow-missing", B4_ALLOW_MISSING,
                        "--base-ref", "1d6ea32"], capture_output=True, text=True, cwd=str(REPO), timeout=60)
    eq(r.returncode, 0, f"check_ids: {r.stdout}{r.stderr}")


@test("unit")
def b4_menu_items_are_bound_by_their_scripts(ctx):
    html, acc, adm = _read("index.html"), _read("account.js"), _read("admin.js")
    tags = {t[1].get("id"): t for t in _tags(html) if t[1].get("id")}
    for i in ("adminBtn", "umAdmin", "umChangePw", "umSignoutAll", "umSignout"):
        check(i in tags, f"#{i} exists")
        # fail-safe: hidden in the markup, so a script that failed to load never leaves a dead control
        check("hidden" in tags[i][1], f"#{i} starts hidden (its script unhides it when it binds it)")
    for i in ("adminBtn", "umAdmin"):
        eq(tags[i][1].get("data-perm"), "admin", f"#{i} data-perm=admin (role channel, §4.6)")
    check(re.search(r'bind\("umChangePw",', acc) and re.search(r'bind\("umSignoutAll",', acc) and re.search(r'bind\("umSignout",', acc),
          "account.js binds the three account items")
    check("el.hidden = false" in acc and "el.hidden = false" in adm, "the scripts unhide what they bind")
    check('bind("adminBtn")' in adm and 'bind("umAdmin")' in adm, "admin.js binds #adminBtn and #umAdmin")
    # the user menu's sign-outs land on the sign-in page with the reason the login page knows
    check('"/api/logout"' in acc and '"/api/me/signout_all"' in acc and '"/api/me/password"' in acc, "account routes")
    check('"/login?reason=signed_out"' in acc, "sign-outs land on /login?reason=signed_out")
    login = _read("login.html")
    for reason in ("signed_out", "role_changed", "session_revoked"):
        check(reason + ":" in login, f"login.html explains reason={reason}")
    tr = _read("tr.js")
    check('"role_changed"' in tr.split("LOGIN_REASONS", 1)[1].split("\n", 1)[0], "tr.js passes reason=role_changed on")


def _route_patterns() -> list:
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from backend import admin_api
    return [(r.path, sorted(r.methods or [])) for r in admin_api.routes()]


@test("unit")
def b4_admin_js_uses_only_existing_admin_routes(ctx):
    adm = _read("admin.js")
    routes = sorted({p for p, _ in _route_patterns()})
    used = set()
    # "/api/admin/users/" + u.uid + "/reset_password" -> /api/admin/users/{uid}/reset_password
    for m in re.finditer(r'"(/api/admin/[a-z_/]*)(?:\?[^"]*)?"', adm):
        lit = m.group(1)
        if lit.endswith("/"):
            t = re.match(r'\s*\+\s*[A-Za-z_][\w.]*(?:\([^)]*\))?\s*(?:\+\s*"(/[a-z_]+)")?', adm[m.end():m.end() + 80])
            check(t, f"admin.js: {lit} is not followed by a uid expression")
            used.add(lit + "{uid}" + (t.group(1) or ""))
        else:
            used.add(lit)
    check(len(used) >= 15, f"suspiciously few admin routes found in admin.js: {sorted(used)}")
    known = set(routes)
    for path in sorted(used):
        check(path in known, f"admin.js uses {path}, which is not an /api/admin route ({sorted(known)})")
    # every §2.17 route has a UI (except the ones only the server calls)
    for path in routes:
        check(path in used, f"no UI for {path}")
    for gone in ('"/api/remote_status"', '"/api/tailscale_serve"', '"/api/set_password"', '"/api/audio_devices"'):
        check(gone not in adm, f"admin.js uses the removed route {gone}")


@test("unit")
def b4_text_is_never_parsed_as_html(ctx):
    for f in ("account.js", "admin.js"):
        src = re.sub(r"/\*.*?\*/", "", _read(f), flags=re.S)        # the comments say "never innerHTML"
        src = re.sub(r"(?m)^\s*//.*$|\s//\s.*$", "", src)
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            check(bad not in src, f"{f} uses {bad} (§5.4: all text with textContent)")
    acc = _read("account.js")
    check("el.textContent = String(v)" in acc and "el.append(c instanceof Node ? c : String(c))" in acc,
          "TR.h puts text in as text nodes")


@test("unit")
def b4_sudo_notices_guard_and_host_audio_link(ctx):
    adm, acc, app = _read("admin.js"), _read("account.js"), _read("app.js")
    # §2.18: reauth_required -> prompt -> /api/admin/reauth -> the call retried ONCE
    m = re.search(r"async function api\(url, opts\) \{(.*?)\n  \}", adm, re.S)
    check(m, "admin.js api() helper")
    body = m.group(1)
    check('e.code === "reauth_required"' in body and "await sudoLayer.ask()" in body, "reauth_required opens the prompt")
    eq(body.count("TR.api(url"), 2, "one call + exactly one retry")
    check("Confirm your password to continue" in adm and '"/api/admin/reauth"' in adm, "sudo prompt")
    check("noRedirect: true" in adm.split('"/api/admin/reauth"', 1)[1][:200], "a wrong reauth password never logs the admin out")
    # §5.4 double-submit guard with an 8 s fallback; result line cleared after 4 s
    check("8000" in adm and "[data-mut]" in adm and "4000" in adm, "pending guard + result line timing")
    # §4.5 notice buttons
    for key, tab in (("lockouts", '"lockouts"'), ("behind_proxy_stale", '"remote"'), ("audio_default", '"audio"')):
        check(re.search(r"TR\.noticeActions\." + key + r" = \{ label: \"[^\"]+\", fn: \(\) => open\(" + tab, adm),
              f"TR.noticeActions.{key} opens {tab}")
    # G-5: #hostAudioInfo is a link to Admin -> Radio & audio for admins
    m = re.search(r"function applyHostAudioInfo\(\) \{(.*?)\n  \}", app, re.S)
    check(m, "applyHostAudioInfo")
    check('TR.can("audio_pin")' in m.group(1) and 'window.TRAdmin.open("audio")' in m.group(1), "the admin link")
    # a modal never opens over this tab's own transmission (it would cover the top-bar STOP)
    check("TR.txBlocksModal = function" in acc and "ownedOrPending()" in acc, "TR.txBlocksModal")
    check(adm.count("TR.txBlocksModal()") >= 1 and acc.count("TR.txBlocksModal()") >= 1, "both modals check it before opening")
    check('document.addEventListener("tr:tx"' in acc and "TR.isTxOwner()" in acc, "safety net: an open modal closes when this tab keys")
    # the Activity STOP is two-tap, bound to the armed owner (TR.stopArm, the same helper the CW STOP uses
    # and stop_arm_test.js exercises), acts on the LIVE owner, and sends tx_stop over the WS only on "fire"
    check('"data-tx": true' in adm and "TR.stopArm(" in adm, "two-tap Activity STOP uses TR.stopArm")
    check('if (arm.tap(o) === "fire" && window.RadioControl) window.RadioControl.send({ action: "tx_stop" });' in adm,
          "the Activity STOP sends tx_stop only when the same owner was tapped twice")
    eq(adm.count('action: "tx_stop"'), 1, "admin.js sends tx_stop from exactly one place")
    check("const o = liveOwner();" in adm and "onAir.arm.check(o);" in adm, "the Activity STOP follows the live owner")
    # Remote: Turn on only when can_serve_on (F9); the stale-flag action only with stale_flag (AR-34)
    check("s.can_serve_on" in adm and "s.stale_flag" in adm, "Remote tab rules")
    for f in ("account.js", "admin.js"):
        r = subprocess.run(["node", "--check", str(FRONT / f)], capture_output=True, text=True, timeout=60)
        eq(r.returncode, 0, f"node --check {f}: {r.stderr}")


@test("smoke")
def b4_index_serves_account_and_admin_js(ctx):
    srv = ctx.server()
    c = harness.ready_admin(srv)
    r = c.get("/")
    eq(r.status, 200, "/")
    html = r.text
    for f in ("account.js", "admin.js"):
        check(re.search(r'<script src="/static/' + re.escape(f) + r'\?v=\d+"></script>', html), f"{f} is served cache-busted")
        s = c.get("/static/" + f)
        eq(s.status, 200, f"/static/{f}")
    check('<div id="modalRoot"></div>' in html, "#modalRoot in the page")
    for i in REMOTE_IDS:
        check(f'id="{i}"' not in html, f"#{i} is gone from the served page")
    eq(c.get("/static/admin.js").text.count("window.TRAdmin = { open, close"), 1, "admin.js content")
    # a viewer gets the scripts too (the menu items are bound for everyone; the admin entries are hidden by role)
    pw = harness.api_mk_user(c, "V1", "viewer")
    v = srv.client()
    eq(harness.login(v, "V1", pw).status, 200, "viewer login")
    vh = v.get("/").text
    check('<body data-role="viewer">' in vh and "/static/admin.js?v=" in vh, "viewer page")
    eq(v.get("/api/admin/users").status, 403, "the admin API stays admin-only")


@test("unit")
def b4_two_tap_stop_is_bound_to_the_armed_owner(ctx):
    """tr.js TR.stopArm + the REAL cwtool.js click handler in a Node vm (fake DOM, fake clock, a recording
    send stub; no browser, no server): a second tap sends tx_stop only while the SAME owner still sends
    CW; an owner change disarms; a tap that belongs to a STOP double-tap never starts a transmission."""
    r = subprocess.run(["node", str(REPO / "tests" / "stop_arm_test.js")], capture_output=True, text=True,
                       timeout=60, cwd=str(REPO))
    eq(r.returncode, 0, f"stop_arm_test.js: {r.stdout}{r.stderr}")
    check("ALL PASS" in r.stdout, f"stop_arm_test.js output: {r.stdout}")
    cw = _read("cwtool.js")
    check("stopArm.tap(o)" in cw and 'r === "fire") rc.send({ action: "tx_stop" })' in cw, "cwtool.js uses TR.stopArm")
    check('document.addEventListener("tr:tx"' in cw and "stopArm.check(" in cw, "cwtool.js disarms on an owner change")
