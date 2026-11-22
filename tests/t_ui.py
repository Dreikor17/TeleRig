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
    eq(_symbols((out / "sprite.svg").read_text(encoding="utf-8")), mod.ICONS + mod.OWN_ICONS, "sprite.svg symbols")
    eq(_symbols((out / "sprite-auth.svg").read_text(encoding="utf-8")), mod.AUTH_ICONS, "sprite-auth.svg symbols")
    check("lucide-static 1.52.0" in (out / "sprite.svg").read_text(encoding="utf-8"), "version header")
    check((FRONT / "vendor" / "lucide" / "LICENSE").is_file(), "vendored LICENSE")
    for n in mod.ICONS:
        check((FRONT / "vendor" / "lucide" / "icons" / f"{n}.svg").is_file(), f"icons/{n}.svg")
    for n in mod.OWN_ICONS:                        # TeleRig's own drawings (never mixed into the vendored set)
        check((FRONT / "icons" / f"{n}.svg").is_file(), f"frontend/icons/{n}.svg")
        check(n.startswith("tr-") and not (FRONT / "vendor" / "lucide" / "icons" / f"{n}.svg").exists(), f"{n} is not a Lucide name")


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
    full, auth = set(mod.ICONS) | set(mod.OWN_ICONS), set(mod.AUTH_ICONS)
    pat = re.compile(r'#i-([a-z0-9-]+)')
    call = re.compile(r'(?:TR\.icon|setIcon)\([^"\')]*["\']([a-z0-9-]+)["\']')
    named = re.compile(r'\bicon:\s*"([a-z0-9-]+)"')          # admin.js: btn(label, {icon: "copy"})
    app_files = ["index.html", "tr.js", "layout.js", "app.js", "cwtool.js", "account.js", "bridge.js", "device.js",
                 "admin.js"]
    seen = set()
    for f in app_files:
        text = (FRONT / f).read_text(encoding="utf-8")
        names = set(pat.findall(text)) | set(call.findall(text)) | set(named.findall(text))
        seen |= names
        missing = sorted(names - full)
        check(not missing, f"{f}: icons not in the sprite: {missing}")
    # names tr.js builds from data (toast kinds, theme): listed here so a rename cannot slip by
    for n in ("info", "triangle-alert", "octagon-x", "check", "x", "sun", "moon", "radio"):
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
    for f in ("index.html", "app.js", "tr.js", "cwtool.js", "login.html", "change-password.html", "account.js", "admin.js"):
        text = (FRONT / f).read_text(encoding="utf-8")
        hits = [e for e in EMOJI if e in text]
        check(not hits, f"{f} still contains {hits}")


@test("unit")
def theme_head_script_and_tokens(ctx):
    """The theme class lives on <html>, set before the first paint; every palette (night, day and the
    radio screen) defines every token. Day keeps the dark instrument screens; the radio-screen theme
    IS the radio's screen, so it may restyle them (and must define them)."""
    idx = (FRONT / "index.html").read_text(encoding="utf-8")
    head = idx.split("</head>", 1)[0]
    check('classList.add("day")' in head and head.index('classList.add("day")') < head.index("style.css"),
          "the head theme script runs before the stylesheet")
    check('t==="screen"' in head and 'classList.add("screen")' in head
          and head.index('classList.add("screen")') < head.index("style.css"),
          "the head script sets the radio-screen theme before the stylesheet")
    css = (FRONT / "style.css").read_text(encoding="utf-8")
    dark = re.search(r":root\{(.*?)\n\}", css, re.S).group(1)
    light = re.search(r":root\.day\{(.*?)\n\}", css, re.S).group(1)
    screen = re.search(r":root\.screen\{(.*?)\n\}", css, re.S).group(1)
    themed = ["--bg", "--surface", "--surface-head", "--surface-2", "--field", "--border", "--border-strong",
              "--txt", "--txt-dim", "--txt-faint", "--txt-disabled", "--accent", "--accent-hi", "--accent-solid",
              "--accent-tint", "--accent-line", "--danger", "--danger-hi", "--danger-tint", "--warn", "--warn-hi",
              "--info", "--info-hi", "--hover", "--key", "--key-hi", "--key-edge", "--num", "--shadow-card",
              "--backdrop", "--scrollthumb"]
    for t in themed:
        check(re.search(re.escape(t) + r"\s*:", dark), f"dark palette lacks {t}")
        check(re.search(re.escape(t) + r"\s*:", light), f"light palette lacks {t}")
        check(re.search(re.escape(t) + r"\s*:", screen), f"radio-screen palette lacks {t}")
    for t in ("--tft-bg", "--tft-digit", "--scope-bg", "--meter-track"):    # instrument: never overridden by day
        check(re.search(re.escape(t) + r"\s*:", dark), f"instrument token {t}")
        check(not re.search(re.escape(t) + r"\s*:", light), f"{t} must not change in day mode")
        check(re.search(re.escape(t) + r"\s*:", screen), f"the radio-screen theme defines {t}")
    # the radio-screen look: its own font, softkey tokens, and component rules that hang off html.screen
    for t in ("--font-ui", "--sk", "--sk-sel", "--sk-tx", "--sk-tx-on", "--vfo-band", "--screen-label"):
        check(re.search(re.escape(t) + r"\s*:", screen), f"radio-screen palette lacks {t}")
    check('"Exo 2"' in screen, "the radio-screen theme uses Exo 2")
    for rule in (".screen .key.active", ".screen #pttBtn.tx-mine", ".screen .bandrow.active",
                 ".screen .band-freq .fd-lo", ".screen .led{display:none}", ".screen .utc-clock{"):
        check(rule in css, f"radio-screen rule {rule}")
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
        for sel in ((":root", ":root.day", ":root.screen") if name == "style.css" else (":root", ":root.day")):
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


def _css_rules(css: str) -> list:
    """[(enclosing @media prelude or "", selector, {property: value})] for every style rule."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    rules, stack, buf = [], [], ""
    for ch in css:
        if ch == "{":
            stack.append(" ".join(buf.split()))
            buf = ""
        elif ch == "}":
            check(stack, "unbalanced '}' in style.css")
            prelude = stack.pop()
            if buf.strip() and not prelude.startswith("@"):
                media = next((p for p in reversed(stack) if p.startswith("@media")), "")
                decls = {}
                for d in buf.split(";"):
                    if ":" in d:
                        k, v = d.split(":", 1)
                        decls[k.strip()] = " ".join(v.split())
                rules.append((media, prelude, decls))
            buf = ""
        else:
            buf += ch
    check(not stack, "unbalanced '{' in style.css")
    return rules


@test("unit")
def onair_chip_layout_rules(ctx):
    """B2b round 1: the ON-AIR chip is never squeezed on desktop. Its no-wrap, basis-0 sizing (text cut
    with an ellipsis) is phone-only (< 900 px); at >= 900 px it keeps its content width and the top bar
    wraps instead (the two-row bar also while ON AIR at 1440-1799 px), capped so it never leaves the
    brand's row; STOP never shrinks out of / extends past the chip."""
    rules = _css_rules((FRONT / "style.css").read_text(encoding="utf-8"))

    def find(selector, media=""):
        return [d for m, s, d in rules if m == media and selector in [x.strip() for x in s.split(",")]]

    PHONE, TWO_ROW, ON_AIR_TWO_ROW = ("@media (max-width:899px)", "@media (max-width:1439px)",
                                      "@media (min-width:1440px) and (max-width:1799px)")
    base = find(".onair")
    eq(len(base), 1, "one base .onair rule")
    b = base[0]
    eq(b.get("flex"), "0 1 auto", "desktop chip: content width, the bar wraps instead of squeezing it")
    eq(b.get("min-width"), "0")
    check(b.get("max-width", "").startswith("calc(100% -"), f"desktop chip cap: {b.get('max-width')!r}")
    # the basis-0 / max-content sizing (which squeezed the text to 0 px at 1440-1600 px) is phone-only
    for media, sel, d in rules:
        if ".onair" in [x.strip() for x in sel.split(",")]:
            squeezing = d.get("max-width") == "max-content" or d.get("flex", "").endswith(" 0") \
                or d.get("flex-basis") == "0"
            check(not squeezing or media == PHONE, f"squeezing .onair sizing outside the phone block: {media!r} {d}")
    phone = {k: v for d in find(".onair", PHONE) for k, v in d.items()}      # later rules win
    check(phone.get("flex", "").endswith(" 0") and phone.get("max-width") == "max-content",
          f"phone chip keeps the no-wrap sizing: {phone}")
    # STOP: never shrinks below its content by flex, but is capped at the chip's own width
    stop = find(".onair-stop")
    eq(len(stop), 1)
    eq(stop[0].get("flex"), "0 0 auto", "STOP never flex-shrinks")
    check(stop[0].get("max-width", "").startswith("calc(100% -"), f"STOP capped inside the chip: {stop[0]}")
    txt = find(".onair-txt")
    check(txt and txt[0].get("min-width") == "0" and txt[0].get("text-overflow") == "ellipsis", f".onair-txt: {txt}")
    # desktop two-row bar: no order for the chip (it stays right after the brand, as in the markup)
    for media, sel, d in rules:
        if media in (TWO_ROW, ON_AIR_TWO_ROW) and ".onair" in sel:
            check("order" not in d, f"the desktop two-row bar reorders the chip: {sel} {d}")
    on_air = ".topbar:has(#onAir:not([hidden]))"
    eq(find(on_air + " .conn", ON_AIR_TWO_ROW), [{"display": "contents"}], "ON-AIR two-row bar at 1440-1799 px")
    ctl = find(on_air + " .conn-controls", ON_AIR_TWO_ROW)
    check(ctl and ctl[0].get("flex") == "1 1 100%", f"connection form on its own row while ON AIR: {ctl}")
    idx = (FRONT / "index.html").read_text(encoding="utf-8")
    check(re.search(r'<div class="tb-brand">.*?</div>\s*<div class="onair" id="onAir"', idx, re.S),
          "the chip must follow the brand in the markup (the desktop bar keeps that order)")
    # phone: the account button is its icon alone, a square like the connection toggle's, the icon in its
    # middle (the desktop rule is a flex row for the name and the badge, which left the icon in a corner)
    user = {k: v for d in find(".tb-btn.user", PHONE) for k, v in d.items()}
    tb = {k: v for d in find(".tb-btn") for k, v in d.items()}
    eq([user.get("display"), user.get("place-items"), user.get("width"), user.get("padding")], ["inline-grid", "center", tb.get("width"), "0"],
       "phone: the account button is the toggle's square, its icon centred")


def _s_scale_pct(label: str) -> float:
    """Where an S-meter label's value sits on renderMeter's bar (0..240): S units are 120/9 each (S9 =
    120, the middle; backend radio.smeter_label), dB over S9 run 120..241 for +0..+60 (capped at the end)."""
    if label.startswith("+"):
        return min(100.0, (120 + int(label[1:]) * (241 - 120) / 60) / 240 * 100)
    return int(label.lstrip("S")) * 120 / 9 / 240 * 100


@test("unit")
def level_meters_colour_by_the_scale(ctx):
    """The S-meter's and the mic bar's colours belong to the SCALE, not to the fill's length: each bar is
    its own inline-size container and its fill's gradient is one bar wide (background-size 100cqw), so a
    low reading is all green and amber / red show only as the reading climbs. (The gradient used to be
    stretched over the fill, so every reading, however weak, ended in red.) The S-meter's green runs to
    S9, amber at +20, red from +40, and its scale labels sit at the values they name in every theme."""
    css = (FRONT / "style.css").read_text(encoding="utf-8")
    rules = _css_rules(css)

    def decl(selector, media=""):
        out = {}
        for m, s, d in rules:
            if m == media and selector in [x.strip() for x in s.split(",")]:
                out.update(d)                                  # later rules win
        return out

    idx = (FRONT / "index.html").read_text(encoding="utf-8")
    check('<div class="meter-bar"><div class="meter-fill" id="meterFill"></div></div>' in idx,
          "#meterFill is the meter bar's only child (the container its gradient is sized to)")
    check('<div class="mic-bar"><div class="mic-bar-fill" id="micLevel"></div></div>' in idx,
          "#micLevel is the mic bar's only child")
    for bar, fill in ((".meter-bar", ".meter-fill"), (".mic-bar", ".mic-bar-fill")):
        eq(decl(bar).get("container-type"), "inline-size", f"{bar} is its own size container")
        f = decl(fill)
        check("linear-gradient(" in f.get("background", ""), f"{fill} keeps its level gradient: {f}")
        eq(f.get("background-size"), "100cqw 100%", f"{fill}: the gradient is one bar wide, not one fill wide")
    # any rule that re-sets a fill's background to a gradient sizes it again (the background shorthand
    # resets background-size to auto, which would stretch it over the fill once more)
    for media, sel, d in rules:
        if any(x.strip().endswith((".meter-fill", ".mic-bar-fill")) for x in sel.split(",")):
            if "gradient(" in d.get("background", "") + d.get("background-image", ""):
                eq(d.get("background-size"), "100cqw 100%", f"{media} {sel}: a gradient fill sized to the bar")
    labels = re.findall(r"<span>([^<]+)</span>",
                        re.search(r'<div class="meter-scale">(.*?)</div>', idx, re.S).group(1))
    eq(labels, ["S1", "3", "5", "7", "9", "+20", "+40", "+60"], "the S-meter scale labels")
    labels[0] = labels[0].lstrip("S")
    names = ["S" + x if not x.startswith("+") else x for x in labels]
    pos = {n: _s_scale_pct(n) for n in names}
    g = decl(".meter-fill")["background"]
    turns = {"green ends at S9": (r"var\(--tft-pill-fg\) 0 ([\d.]+)%", "S9"),
             "amber at +20": (r"var\(--tft-amber\) ([\d.]+)%", "+20"),
             "red from +40": (r"#f87171 ([\d.]+)%", "+40")}
    for what, (rx, at) in turns.items():
        m = re.search(rx, g)
        check(m and abs(float(m.group(1)) - pos[at]) < 0.06, f"S-meter {what} ({pos[at]:.1f} %): {g}")
    # the labels: at their values (not spread evenly), in the base rules, so every theme shows them there
    eq(decl(".meter-scale").get("position"), "relative", ".meter-scale positions its labels")
    eq(decl(".meter-scale span").get("position"), "absolute", ".meter-scale labels")
    for i, n in enumerate(names, 1):
        left = decl(f".meter-scale span:nth-child({i})").get("left", "")
        check(left.endswith("%") and abs(float(left[:-1]) - pos[n]) < 0.06,
              f"label {n} at {pos[n]:.1f} % of the bar (css left: {left!r})")
    for m, s, d in rules:
        if ".meter-scale" in s and "display" in d:
            check(d["display"] not in ("flex", "grid"), f"{s}: the labels are positioned, not spread ({d})")


@test("unit")
def meter_reading_holds_its_place(ctx):
    """The meter's reading (in the VFO tile) starts at the same place whatever its length: left-aligned in a
    box as wide as its longest text (S9+60 on receive, COMP 25.5 dB on transmit), so "S9" does not move when
    "+0" or "+20" joins it (it was right-aligned, and the S value moved back and forth across S9)."""
    rules = _css_rules((FRONT / "style.css").read_text(encoding="utf-8"))

    def decl(selector):
        out = {}
        for m, sel, d in rules:
            if m == "" and selector in [x.strip() for x in sel.split(",")]:
                out.update(d)
        return out
    v = decl(".meter-val")
    eq(v.get("text-align"), "left", ".meter-val is left-aligned")
    ch = lambda d: float(d.get("min-width", "0ch").rstrip("ch") or 0) if d.get("min-width", "").endswith("ch") else 0.0
    check(ch(v) >= len("S9+60") + 1, f".meter-val min-width holds S9+60 with room: {v.get('min-width')}")
    check(ch(decl(".meter.tx .meter-val")) >= len("COMP 25.5 dB"), "on transmit it holds COMP 25.5 dB")
    for m, sel, d in rules:
        if "meter-val" in sel:
            check(d.get("text-align", "left") == "left", f"{m} {sel}: no rule right-aligns the reading again")


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
    eq(_symbols(html), mod.ICONS + mod.OWN_ICONS, "/ carries the full sprite (Lucide + TeleRig's own icons)")
    check("<!--TR_ICONS-->" not in html, "index placeholder replaced")
    check('<body data-role="admin">' in html, "body data-role intact")
    body = html.split('<body data-role="admin">', 1)[1]
    check(body.lstrip().startswith("<!-- TeleRig icon sprite"), "the sprite is the first thing in <body>")
