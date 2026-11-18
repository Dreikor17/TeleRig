#!/usr/bin/env python3
"""Build TeleRig's inline icon sprites from the vendored Lucide SVGs (stdlib only).

    .venv\\Scripts\\python.exe tools\\build_icons.py            write both sprites
    .venv\\Scripts\\python.exe tools\\build_icons.py --check    only verify they are up to date

Source: frontend/vendor/lucide/icons/<name>.svg, lucide-static 1.52.0 (ISC; the Feather-derived icons
are MIT, see frontend/vendor/lucide/LICENSE). The SVGs were downloaded once from
https://cdn.jsdelivr.net/npm/lucide-static@1.52.0/icons/<name>.svg and are committed; nothing is
fetched at build time or at runtime.

Output (both committed, both injected inline by the server, so icons never need a network request):
  frontend/vendor/lucide/sprite.svg       every icon in ICONS        -> index.html  (<!--TR_ICONS-->)
  frontend/vendor/lucide/sprite-auth.svg  the AUTH_ICONS subset      -> login.html / change-password.html
                                                                        (<!--TR_AUTH_ICONS-->; the auth
                                                                        pages never load /static)
Each icon becomes <symbol id="i-NAME" viewBox="0 0 24 24" ...>; pages use <svg><use href="#i-NAME"/></svg>.

OWN_ICONS are TeleRig's own drawings (frontend/icons/<name>.svg, same format, the app's license);
they go into sprite.svg after the Lucide icons and are checked by the same rules.

Exit codes: 0 ok; 1 a listed icon file is missing or malformed (named in the message), or --check
found a stale sprite; 2 usage error.
"""
from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

LUCIDE_VERSION = "1.52.0"
ROOT = Path(__file__).resolve().parent.parent
LUCIDE = ROOT / "frontend" / "vendor" / "lucide"
ICON_DIR = LUCIDE / "icons"
OWN_DIR = ROOT / "frontend" / "icons"

# The fixed icon list (spec §4.4). Add a name here (and its SVG under icons/) to make it available.
ICONS = [
    # brand
    "radio-tower",
    # tiles
    "radio", "mic", "volume-2", "gauge", "audio-waveform", "sliders-horizontal", "keyboard",
    # chrome
    "chevron-up", "chevron-down", "x", "arrow-up", "arrow-down", "plus", "rotate-ccw", "check",
    "layout-grid", "sun", "moon", "code-xml", "shield", "user", "users", "log-out", "log-in",
    "key-round", "plug", "unplug", "globe", "history", "copy", "triangle-alert", "info", "octagon-x",
    "square", "eye", "eye-off", "ellipsis", "lock",
]
# TeleRig's own icons (frontend/icons/): the WSJT-X bridge, full screen, pause / resume.
OWN_ICONS = ["tr-bridge", "tr-fullscreen", "tr-fullscreen-exit", "tr-pause", "tr-play"]
# The sign-in / change-password pages get only these (inline, no /static).
AUTH_ICONS = ["radio-tower", "log-in", "key-round", "triangle-alert", "info", "eye", "eye-off", "sun", "moon"]

SVG_NS = "{http://www.w3.org/2000/svg}"
ALLOWED_TAGS = {"path", "circle", "rect", "line", "polyline", "polygon", "ellipse"}
ALLOWED_ATTRS = {"d", "cx", "cy", "r", "rx", "ry", "x", "y", "x1", "y1", "x2", "y2", "width", "height",
                 "points", "fill", "stroke", "stroke-width", "transform"}
SYMBOL_ATTRS = ('viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
                'stroke-linecap="round" stroke-linejoin="round"')


class IconError(Exception):
    """A listed icon is missing or not a plain Lucide SVG."""


def _symbol(name: str, src: Path) -> str:
    """One <symbol> for an icon file: the root <svg> stripped, only plain shape children kept."""
    try:
        text = src.read_text(encoding="utf-8").replace("\r\n", "\n")
    except OSError as e:
        raise IconError(f"cannot read {src}: {e}") from None
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise IconError(f"{name}: not well-formed XML ({e})") from None
    if root.tag != SVG_NS + "svg" or root.get("viewBox") != "0 0 24 24":
        raise IconError(f"{name}: root is not a 24x24 <svg>")
    parts = []
    for child in root:
        tag = child.tag[len(SVG_NS):] if child.tag.startswith(SVG_NS) else child.tag
        if tag not in ALLOWED_TAGS or len(child):
            raise IconError(f"{name}: unexpected element <{tag}>")
        attrs = []
        for k, v in child.attrib.items():
            if k not in ALLOWED_ATTRS:
                raise IconError(f"{name}: unexpected attribute {k!r} on <{tag}>")
            if not re.fullmatch(r"[A-Za-z0-9 .,\-]*", v):
                raise IconError(f"{name}: unexpected characters in {k}={v!r}")
            attrs.append(f'{k}="{v}"')
        parts.append(f"<{tag} {' '.join(attrs)}/>")
    if not parts:
        raise IconError(f"{name}: no shapes")
    return f'<symbol id="i-{name}" {SYMBOL_ATTRS}>{"".join(parts)}</symbol>'


def _sprite(names: list, symbols: dict, label: str) -> str:
    head = (f"<!-- TeleRig {label}: Lucide icons (lucide-static {LUCIDE_VERSION}, ISC; Feather-derived icons MIT, "
            f"frontend/vendor/lucide/LICENSE); the tr-* icons are TeleRig's own (frontend/icons). "
            f"Generated by tools/build_icons.py - do not edit. -->\n")
    body = "\n".join(symbols[n] for n in names)
    return (head + '<svg xmlns="http://www.w3.org/2000/svg" style="display:none" aria-hidden="true" focusable="false">\n'
            + body + "\n</svg>\n")


def build(icons: list | None = None, auth_icons: list | None = None, icon_dir: Path | None = None,
          own_icons: list | None = None, own_dir: Path | None = None) -> dict:
    """{filename: text} for both sprites. Raises IconError naming every missing/bad icon."""
    icons = list(ICONS if icons is None else icons)
    auth_icons = list(AUTH_ICONS if auth_icons is None else auth_icons)
    icon_dir = ICON_DIR if icon_dir is None else Path(icon_dir)
    own_icons = list(OWN_ICONS if own_icons is None else own_icons)
    own_dir = OWN_DIR if own_dir is None else Path(own_dir)
    problems = []
    clash = sorted(set(own_icons) & set(icons))
    if clash:
        problems.append("name(s) in both ICONS and OWN_ICONS: " + ", ".join(clash))
    dupes = sorted({n for n in icons if icons.count(n) > 1})
    if dupes:
        problems.append("duplicate name(s) in ICONS: " + ", ".join(dupes))
    not_listed = [n for n in auth_icons if n not in icons]
    if not_listed:
        problems.append("AUTH_ICONS not in ICONS: " + ", ".join(not_listed))
    bad_names = [n for n in icons if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", n)]
    if bad_names:
        problems.append("invalid icon name(s): " + ", ".join(repr(n) for n in bad_names))
    missing = [n for n in icons if n not in bad_names and not (icon_dir / f"{n}.svg").is_file()]
    if missing:
        problems.append("missing icon file(s): " + ", ".join(
            f"{n} ({(icon_dir / (n + '.svg')).as_posix()})" for n in missing))
    symbols = {}
    for n in icons:
        if n in missing or n in bad_names or n in symbols:
            continue
        try:
            symbols[n] = _symbol(n, icon_dir / f"{n}.svg")
        except IconError as e:
            problems.append(str(e))
    for n in own_icons:                                # TeleRig's own drawings: same rules
        if n in symbols:
            continue
        if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", n):
            problems.append(f"invalid icon name: {n!r}")
        elif not (own_dir / f"{n}.svg").is_file():
            problems.append(f"missing icon file(s): {n} ({(own_dir / (n + '.svg')).as_posix()})")
        else:
            try:
                symbols[n] = _symbol(n, own_dir / f"{n}.svg")
            except IconError as e:
                problems.append(str(e))
    if problems:
        raise IconError("; ".join(problems))
    return {"sprite.svg": _sprite(icons + [n for n in own_icons if n not in icons], symbols, "icon sprite"),
            "sprite-auth.svg": _sprite(auth_icons, symbols, "sign-in icon sprite")}


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the inline Lucide icon sprites.")
    ap.add_argument("--check", action="store_true", help="verify the committed sprites are up to date; write nothing")
    ap.add_argument("--out", default=None, help="output folder (default: frontend/vendor/lucide)")
    args = ap.parse_args(argv)
    try:
        files = build()
    except IconError as e:
        print(f"build_icons: ERROR: {e}", file=sys.stderr)
        return 1
    out = Path(args.out) if args.out else LUCIDE
    stale = []
    for fname, text in files.items():
        dst = out / fname
        if args.check:
            try:
                cur = dst.read_text(encoding="utf-8").replace("\r\n", "\n")
            except OSError:
                cur = None
            if cur != text:
                stale.append(fname)
            continue
        out.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(text.encode("utf-8"))      # LF, UTF-8, byte-for-byte deterministic
    if stale:
        print("build_icons: stale sprite(s): " + ", ".join(stale) + " - run tools/build_icons.py", file=sys.stderr)
        return 1
    n_auth = files["sprite-auth.svg"].count("<symbol ")
    n_all = files["sprite.svg"].count("<symbol ")
    verb = "up to date" if args.check else "wrote"
    print(f"build_icons: {verb} sprite.svg ({n_all} icons) and sprite-auth.svg ({n_auth} icons), "
          f"lucide-static {LUCIDE_VERSION}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
