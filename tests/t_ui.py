"""B2 visual system: the vendored Lucide icons, the sprite builder, sprite injection and the emoji
sweep (spec §4.4, §7 B2).

`unit` tests read files only (no server). The `smoke` test starts a sim-only server on a free port and
checks that every page carries its icons inline (the sign-in pages never load /static).
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import harness
from harness import check, eq, test

REPO = Path(__file__).resolve().parent.parent
FRONT = REPO / "frontend"
TOOL = REPO / "tools" / "build_icons.py"
EMOJI = ("🔊", "🎤", "🌙", "⚙", "🌐", "☀")


def _tool():
    spec = importlib.util.spec_from_file_location("tr_build_icons", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_tool(args: list, extra_icon: str | None = None) -> subprocess.CompletedProcess:
    if extra_icon is None:
        cmd = [sys.executable, str(TOOL)] + args
    else:                                   # "add a bogus name to ICONS" without editing the file
        code = ("import importlib.util,sys;"
                f"s=importlib.util.spec_from_file_location('b',{str(TOOL)!r});m=importlib.util.module_from_spec(s);"
                f"s.loader.exec_module(m);m.ICONS.append({extra_icon!r});sys.exit(m.main({args!r}))")
        cmd = [sys.executable, "-c", code]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=str(REPO))


def _symbols(svg: str) -> list:
    return re.findall(r'<symbol id="i-([a-z0-9-]+)"', svg)


@test("unit")
def icons_sprites_build_and_are_current(ctx):
    r = _run_tool(["--check"])
    eq(r.returncode, 0, f"build_icons --check: {r.stdout}{r.stderr}")
    out = ctx.tmp("icons")
    r = _run_tool(["--out", str(out)])
    eq(r.returncode, 0, f"build_icons --out: {r.stdout}{r.stderr}")
    for name in ("sprite.svg", "sprite-auth.svg"):
        built = (out / name).read_text(encoding="utf-8")
        committed = (FRONT / "vendor" / "lucide" / name).read_text(encoding="utf-8").replace("\r\n", "\n")
        eq(built, committed, f"{name} differs from a fresh build")
    mod = _tool()
    eq(_symbols((out / "sprite.svg").read_text(encoding="utf-8")), mod.ICONS, "sprite.svg symbols")
    eq(_symbols((out / "sprite-auth.svg").read_text(encoding="utf-8")), mod.AUTH_ICONS, "sprite-auth.svg symbols")
    check("lucide-static 1.52.0" in (out / "sprite.svg").read_text(encoding="utf-8"), "version header")
    check((FRONT / "vendor" / "lucide" / "LICENSE").is_file(), "vendored LICENSE")
    for n in mod.ICONS:
        check((FRONT / "vendor" / "lucide" / "icons" / f"{n}.svg").is_file(), f"icons/{n}.svg")


@test("unit")
def icons_bogus_name_fails_naming_it(ctx):
    out = ctx.tmp("bogus")
    r = _run_tool(["--out", str(out)], extra_icon="bogus-icon-zz")
    check(r.returncode != 0, f"exit {r.returncode}: {r.stdout}{r.stderr}")
    check("bogus-icon-zz" in r.stderr, f"the message names the icon: {r.stderr!r}")
    check(not (out / "sprite.svg").exists(), "nothing written on failure")
    mod = _tool()
    try:
        mod.build(icons=mod.ICONS + ["no-such-icon"])
    except mod.IconError as e:
        check("no-such-icon" in str(e), str(e))
    else:
        raise harness.TestFailure("build() accepted a missing icon")
    try:                                           # the auth subset must come from ICONS
        mod.build(auth_icons=mod.AUTH_ICONS + ["plug-zz"])
    except mod.IconError as e:
        check("plug-zz" in str(e), str(e))
    else:
        raise harness.TestFailure("build() accepted an auth icon outside ICONS")


@test("unit")
def icons_every_reference_resolves(ctx):
    """Every #i-NAME the pages and scripts use exists in the sprite their page carries."""
    mod = _tool()
    full, auth = set(mod.ICONS), set(mod.AUTH_ICONS)
    pat = re.compile(r'#i-([a-z0-9-]+)')
    call = re.compile(r'(?:TR\.icon|setIcon)\([^"\')]*["\']([a-z0-9-]+)["\']')
    app_files = ["index.html", "tr.js", "app.js", "cwtool.js"]
    seen = set()
    for f in app_files:
        text = (FRONT / f).read_text(encoding="utf-8")
        names = set(pat.findall(text)) | set(call.findall(text))
        seen |= names
        missing = sorted(names - full)
        check(not missing, f"{f}: icons not in the sprite: {missing}")
    # names tr.js builds from data (toast kinds, theme): listed here so a rename cannot slip by
    for n in ("info", "triangle-alert", "octagon-x", "check", "x", "sun", "moon"):
        check(n in full, f"tr.js dynamic icon {n}")
    for f in ("login.html", "change-password.html"):
        text = (FRONT / f).read_text(encoding="utf-8")
        names = set(pat.findall(text))
        missing = sorted(names - auth)
        check(not missing, f"{f}: icons not in the auth sprite: {missing}")
        eq(text.count("<!--TR_AUTH_ICONS-->"), 1, f"{f} placeholder")
        check("/static/" not in text, f"{f} must not load /static (gated while signed out)")
    for n in ("eye", "eye-off", "sun", "moon"):                 # set dynamically by login.html
        check(n in auth, f"login.html dynamic icon {n}")
    idx = (FRONT / "index.html").read_text(encoding="utf-8")
    eq(idx.count("<!--TR_ICONS-->"), 1, "index.html placeholder")
    check(idx.index("<body>") < idx.index("<!--TR_ICONS-->"), "the sprite goes in <body>")
    check(len(seen) >= 10, f"suspiciously few icon references: {sorted(seen)}")


@test("unit")
def ui_has_no_emoji_icons(ctx):
    for f in ("index.html", "app.js", "tr.js", "cwtool.js", "login.html", "change-password.html"):
        text = (FRONT / f).read_text(encoding="utf-8")
        hits = [e for e in EMOJI if e in text]
        check(not hits, f"{f} still contains {hits}")


@test("unit")
def theme_head_script_and_tokens(ctx):
    """The theme class lives on <html>, set before the first paint; both palettes define every token."""
    idx = (FRONT / "index.html").read_text(encoding="utf-8")
    head = idx.split("</head>", 1)[0]
    check('classList.add("day")' in head and head.index('classList.add("day")') < head.index("style.css"),
          "the head theme script runs before the stylesheet")
    css = (FRONT / "style.css").read_text(encoding="utf-8")
    dark = re.search(r":root\{(.*?)\n\}", css, re.S).group(1)
    light = re.search(r":root\.day\{(.*?)\n\}", css, re.S).group(1)
    themed = ["--bg", "--surface", "--surface-head", "--surface-2", "--field", "--border", "--border-strong",
              "--txt", "--txt-dim", "--txt-faint", "--txt-disabled", "--accent", "--accent-hi", "--accent-solid",
              "--accent-tint", "--accent-line", "--danger", "--danger-hi", "--danger-tint", "--warn", "--warn-hi",
              "--info", "--info-hi", "--hover", "--key", "--key-hi", "--key-edge", "--num", "--shadow-card",
              "--backdrop", "--scrollthumb"]
    for t in themed:
        check(re.search(re.escape(t) + r"\s*:", dark), f"dark palette lacks {t}")
        check(re.search(re.escape(t) + r"\s*:", light), f"light palette lacks {t}")
    for t in ("--tft-bg", "--tft-digit", "--scope-bg", "--meter-track"):    # instrument: never overridden
        check(re.search(re.escape(t) + r"\s*:", dark), f"instrument token {t}")
        check(not re.search(re.escape(t) + r"\s*:", light), f"{t} must not change in day mode")
    check("body.day{" not in css, "the old body.day palette is gone")
    check("[hidden]{display:none!important}" in css, "global [hidden] rule")


def _palette(text: str, selector: str) -> dict:
    """{--token: value} of one `selector{...}` block (the first one) in a stylesheet text."""
    m = re.search(re.escape(selector) + r"\s*\{(.*?)\}", text, re.S)
    check(m, f"no {selector} block")
    body = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
    return {k: v.strip() for k, v in re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);?", body)}


def _lum(hexcol: str) -> float:
    h = hexcol.strip().lstrip("#")
    check(re.fullmatch(r"[0-9a-fA-F]{6}", h), f"not a #rrggbb colour: {hexcol!r}")
    ch = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in ch]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _ratio(a: str, b: str) -> float:
    la, lb = _lum(a), _lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


@test("unit")
def contrast_white_on_accent_and_auth_text(ctx):
    """B2b: white text on --accent-solid (and its hover) is WCAG AA (>= 4.5:1) in both themes, in the
    app and on both sign-in pages; the auth pages' footer and hint use --txt-dim (>= 4.5:1), never
    --txt-faint (below 3:1)."""
    css = (FRONT / "style.css").read_text(encoding="utf-8")
    sheets = {"style.css": css}
    for f in ("login.html", "change-password.html"):
        sheets[f] = (FRONT / f).read_text(encoding="utf-8")
    for name, text in sheets.items():
        for sel in (":root", ":root.day"):
            p = _palette(text, sel)
            r = _ratio("#ffffff", p["--accent-solid"])
            check(r >= 4.5, f"{name} {sel}: white on --accent-solid {p['--accent-solid']} is {r:.2f}:1")
            if name == "style.css":
                r = _ratio("#ffffff", p["--accent-solid-hover"])
                check(r >= 4.5, f"{sel}: white on --accent-solid-hover is {r:.2f}:1")
                r = _ratio(p["--sel-fg"], p["--sel-bg"])
                check(r >= 4.5, f"{sel}: --sel-fg on --sel-bg is {r:.2f}:1")
            else:
                for bg in ("--bg", "--surface-2"):
                    r = _ratio(p["--txt-dim"], p[bg])
                    check(r >= 4.5, f"{name} {sel}: --txt-dim on {bg} is {r:.2f}:1")
    # hovers on the solid fills go darker (never to --accent, which is 2.5:1 with white at night)
    check(".ctl-btn.primary:hover{background:var(--accent-solid-hover)" in css, "primary hover token")
    check(".ver-update:hover{background:var(--accent-solid-hover)}" in css, "update pill hover token")
    for f in ("login.html", "change-password.html"):
        text = sheets[f]
        check("footer{" in text and "color:var(--txt-faint)" not in text, f"{f}: text set in --txt-faint")


@test("unit")
def phone_only_items_hidden_on_desktop(ctx):
    """B2b: `.menu [role=menuitem]{display:flex}` out-specifies a plain `.phone-only{display:none}`, so
    the desktop hide is an !important rule at >= 900 px; [hidden] stays the global !important rule."""
    css = (FRONT / "style.css").read_text(encoding="utf-8")
    check(re.search(r"@media \(min-width:900px\)\{\s*\.phone-only\{display:none!important\}\s*\}", css),
          "no >= 900 px !important hide for .phone-only")
    check("[hidden]{display:none!important}" in css, "global [hidden] rule")
    idx = (FRONT / "index.html").read_text(encoding="utf-8")
    items = re.findall(r'<[^>]*class="[^"]*\bphone-only\b[^"]*"[^>]*>', idx)
    check(len(items) >= 3, f"phone-only items: {items}")
    for tag in items:
        check('role="menuitem"' in tag, f"a phone-only element outside the user menu: {tag}")


@test("smoke")
def pages_carry_their_icons_inline(ctx):
    srv = ctx.server()
    anon = harness.Client(srv.port)
    r = anon.get("/login")
    eq(r.status, 200, "/login")
    html = r.text
    mod = _tool()
    eq(_symbols(html), mod.AUTH_ICONS, "/login carries the auth sprite")
    check("<!--TR_AUTH_ICONS-->" not in html and "/static/" not in html, "/login placeholder / static refs")
    r = anon.get("/static/style.css")
    check(r.status in (303, 401), f"/static is gated while signed out (got {r.status})")
    c = srv.client()
    r = harness.admin_login(c, srv.tmp)
    eq(r.status, 200, "first-run login")
    r = c.get("/change-password")
    eq(r.status, 200, "/change-password")
    eq(_symbols(r.text), mod.AUTH_ICONS, "/change-password carries the auth sprite")
    c = harness.ready_admin(srv, c)
    r = c.get("/")
    eq(r.status, 200, "/")
    html = r.text
    eq(_symbols(html), mod.ICONS, "/ carries the full sprite")
    check("<!--TR_ICONS-->" not in html, "index placeholder replaced")
    check('<body data-role="admin">' in html, "body data-role intact")
    body = html.split('<body data-role="admin">', 1)[1]
    check(body.lstrip().startswith("<!-- TeleRig icon sprite"), "the sprite is the first thing in <body>")
