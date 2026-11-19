"""B3 layout (spec §3, §4.2, §7 B3): the vendored GridStack, the tile markup and parking, the script
order, layout.js's registry against the server's bounds, the id audit, the console CSS that tiles
replaced, and (smoke) that the page serves the grid assets.

`unit` tests read files only. The `smoke` test starts a sim-only server on a free port; no radio is
connected and no TX command is sent. The interactive behaviour (arrange, gates, collapse, phone ranks,
fallback) is verified in the browser; see the B3 report.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

import harness
from harness import check, eq, test

REPO = Path(__file__).resolve().parent.parent
FRONT = REPO / "frontend"
GS = FRONT / "vendor" / "gridstack"
GS_SHA256 = {                                     # spec §7 B3 verify 0 (gridstack@14.0.0, byte for byte)
    "gridstack-all.js": "27ae971fd981c0341affc051c793d7e5417f4a785bbfef7dd88ff7f1139d8dcf",
    "gridstack-all.js.LICENSE.txt": "8cde80b1402a3509cbc74e8cc68d3419b842bcdb9cd68d0163e61cebee9b1e45",
    "gridstack.min.css": "74efc82159c5e8d28cb2cebd9f912be9d728bb07fa83896a6b62f4a843207126",
    "LICENSE": "28d28a6e0b5c6ebca8759b162a644c31cea28aab591060c0d95c7737d8456764",
}
EXO2 = FRONT / "vendor" / "exo2"
EXO2_SHA256 = {                                   # Google Fonts exo2 v26, latin subset (+ its OFL), byte for byte
    "exo2-latin.woff2": "4a259dde317e08aa5d37e6eb684e222ae833516b2a0fccba36ee5e36224f16be",
    "exo2-latin-italic.woff2": "26a4dc84a13ae17366fcd188e354530db070234c038370df9ee47f1b0d611a7a",
    "OFL.txt": "7f95a8eeecc6904ffc81cd10cc8ebc7fcee72a67922924bf45f02dff03b88127",
}
TILE_IDS = ["vfo", "ptt", "audio", "meter", "scope", "controls", "cw", "recorder", "spots", "sstv"]
# the per-step id allow-list of tests/check_ids.py (§3.3 step 10): since B4 the Remote dialog's ids are
# gone as well (AR-56)
B3_ALLOW_MISSING = "remoteBtn,remoteModal,remoteClose,remoteBody,rxDev,micDev"
# The page Phase 1 started from: v0.2.25. The audit used to read `dev`, which was that page until the
# v0.3.0 release moved dev to v0.3.0 itself; pinned so the Phase 1 audit keeps its meaning.
PHASE1_BASE_REF = "1d6ea32"


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


class _Tree(HTMLParser):
    """A tiny element tree: (tag, attrs, parent index) for every start tag, in document order."""
    VOID = {"meta", "link", "input", "br", "img", "hr", "source", "use", "path", "circle", "rect"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.nodes, self.stack = [], []

    def handle_starttag(self, tag, attrs):
        parent = self.stack[-1] if self.stack else -1
        self.nodes.append((tag, dict(attrs), parent))
        if tag not in self.VOID:
            self.stack.append(len(self.nodes) - 1)

    def handle_startendtag(self, tag, attrs):
        parent = self.stack[-1] if self.stack else -1
        self.nodes.append((tag, dict(attrs), parent))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.nodes[self.stack[i]][0] == tag:
                del self.stack[i:]
                return


def _tree() -> _Tree:
    t = _Tree()
    t.feed((FRONT / "index.html").read_text(encoding="utf-8"))
    return t


def _by_id(t: _Tree, i: str) -> int:
    hits = [k for k, (_, a, _) in enumerate(t.nodes) if a.get("id") == i]
    eq(len(hits), 1, f"#{i} exactly once")
    return hits[0]


def _ancestors(t: _Tree, k: int) -> list:
    out, p = [], t.nodes[k][2]
    while p >= 0:
        out.append(p)
        p = t.nodes[p][2]
    return out


def _classes(node) -> set:
    return set((node[1].get("class") or "").split())


@test("unit")
def exo2_vendored_byte_for_byte(ctx):
    """The radio-screen theme's font: the two latin WOFF2 files and the OFL, unchanged, kept as bytes by
    .gitattributes, credited in NOTICE, and the only font files style.css's @font-face rules load."""
    for name, want in EXO2_SHA256.items():
        eq(_sha(EXO2 / name), want, f"vendor/exo2/{name} sha256")
    for name in ("exo2-latin.woff2", "exo2-latin-italic.woff2"):
        eq((EXO2 / name).read_bytes()[:4], b"wOF2", f"{name} is a WOFF2 file")
    check("SIL Open Font License" in (EXO2 / "OFL.txt").read_text(encoding="utf-8"), "the OFL text")
    attrs = (REPO / ".gitattributes").read_text(encoding="utf-8")
    check(re.search(r"^frontend/vendor/exo2/\*\* -text$", attrs, re.M), ".gitattributes keeps the font bytes as-is")
    notice = (REPO / "NOTICE").read_text(encoding="utf-8")
    check("Exo 2" in notice and "frontend/vendor/exo2/" in notice and "Open Font License" in notice, "NOTICE Exo 2 entry")
    css = (FRONT / "style.css").read_text(encoding="utf-8")
    urls = re.findall(r'@font-face\{[^}]*url\("([^"]+)"\)', css)
    eq(sorted(urls), ["/static/vendor/exo2/exo2-latin-italic.woff2", "/static/vendor/exo2/exo2-latin.woff2"],
       "the @font-face rules load the vendored files")
    for u in urls:
        check((FRONT / u[len("/static/"):]).is_file(), f"{u} exists")
    check("fonts.googleapis" not in css and "fonts.gstatic" not in css, "no font is loaded from the internet")


LAMEJS_SHA256 = "15d285e2587b3bdbfd18a68de6ce07cc074f7480a82c3815da2dc1c348ec6df4"   # lamejs@1.2.1 lame.min.js


@test("unit")
def lamejs_vendored_byte_for_byte(ctx):
    """The Recorder's MP3 encoder: lamejs 1.2.1 lame.min.js exactly as npm publishes it (LAME asks for it
    unmodified, as a separate file), kept as bytes by .gitattributes, credited in NOTICE and in the
    Recorder panel with a link to LAME's site, loaded from /static (never from the internet)."""
    lame = FRONT / "vendor" / "lamejs" / "lame.min.js"
    eq(_sha(lame), LAMEJS_SHA256, "vendor/lamejs/lame.min.js sha256 (lamejs@1.2.1, unmodified)")
    attrs = (REPO / ".gitattributes").read_text(encoding="utf-8")
    check(re.search(r"^frontend/vendor/lamejs/\*\* -text$", attrs, re.M), ".gitattributes keeps the encoder bytes as-is")
    notice = (REPO / "NOTICE").read_text(encoding="utf-8")
    check("lamejs" in notice and "frontend/vendor/lamejs/lame.min.js" in notice and "LGPL-3.0" in notice
          and "lame.sourceforge.io" in notice, "NOTICE lamejs / LAME entry")
    html = (FRONT / "index.html").read_text(encoding="utf-8")
    check(re.search(r'class="rec-credit">[^<]*<a href="https://lame\.sourceforge\.io"', html), "the Recorder credits LAME with a link")
    check("cdn.jsdelivr" not in html and "unpkg" not in html, "nothing loads from a CDN")


@test("unit")
def gridstack_vendored_byte_for_byte(ctx):
    for name, want in GS_SHA256.items():
        eq(_sha(GS / name), want, f"vendor/gridstack/{name} sha256")
    js = (GS / "gridstack-all.js").read_text(encoding="utf-8")
    check('GDRev="14.0.0"' in js, "the bundle is GridStack 14.0.0")
    check(js.rstrip().endswith("//# sourceMappingURL=gridstack-all.js.map"), "the trailing sourceMappingURL line is kept")
    attrs = (REPO / ".gitattributes").read_text(encoding="utf-8")
    check(re.search(r"^frontend/vendor/gridstack/\*\* -text$", attrs, re.M), ".gitattributes keeps the vendored bytes as-is")
    notice = (REPO / "NOTICE").read_text(encoding="utf-8")
    check("GridStack.js" in notice and "frontend/vendor/gridstack/" in notice and "14.0.0" in notice
          and "Alain Dumesny" in notice and "MIT" in notice, "NOTICE GridStack entry")


@test("unit")
def index_tiles_parking_and_script_order(ctx):
    t = _tree()
    grid, parking = _by_id(t, "grid"), _by_id(t, "tileParking")
    check({"grid-stack", "tr-grid"} <= _classes(t.nodes[grid]), "#grid.grid-stack.tr-grid")
    check(not [k for k, n in enumerate(t.nodes) if n[2] == grid], "#grid starts EMPTY (S8)")
    eq(t.nodes[t.nodes[grid][2]][1].get("id"), "gridWrap", "#grid sits in #gridWrap")
    pa = t.nodes[parking][1]
    check("hidden" in pa and pa.get("aria-hidden") == "true", "#tileParking hidden + aria-hidden")
    check(parking not in _ancestors(t, grid) and grid not in _ancestors(t, parking), "#tileParking is outside #grid")
    items = [k for k, n in enumerate(t.nodes) if "grid-stack-item" in _classes(n)]
    eq([t.nodes[k][1].get("data-tile") for k in items], TILE_IDS, "ten tiles, registry order")
    for k in items:
        tid = t.nodes[k][1]["data-tile"]
        eq(t.nodes[k][2], parking, f"{tid}: a direct child of #tileParking")
        eq(t.nodes[k][1].get("gs-id"), tid, f"{tid}: gs-id")
        kids = [j for j, n in enumerate(t.nodes) if n[2] == k]
        eq(len(kids), 1, f"{tid}: one content section")
        sec = t.nodes[kids[0]]
        check(sec[0] == "section" and {"grid-stack-item-content", "tile"} <= _classes(sec), f"{tid}: section.tile")
        eq(sec[1].get("aria-labelledby"), "th-" + tid, f"{tid}: aria-labelledby")
        inner = [j for j, n in enumerate(t.nodes) if n[2] == kids[0]]
        eq([t.nodes[j][0] for j in inner], ["header", "div"], f"{tid}: header + body")
        check("tile-head" in _classes(t.nodes[inner[0]]), f"{tid}: .tile-head first (S3)")
        check("tile-body" in _classes(t.nodes[inner[1]]), f"{tid}: .tile-body")
        fits = [j for j, n in enumerate(t.nodes) if n[2] == inner[1]]
        check(len(fits) == 1 and "tile-fit" in _classes(t.nodes[fits[0]]), f"{tid}: exactly one .tile-fit")
        btns = [j for j in range(len(t.nodes)) if inner[0] in _ancestors(t, j) and t.nodes[j][0] == "button"]
        cls = set().union(*[_classes(t.nodes[j]) for j in btns])
        check({"tile-collapse", "tile-remove", "tile-up", "tile-down"} <= cls, f"{tid}: header buttons {cls}")
        for j in btns:
            eq(t.nodes[j][1].get("type"), "button", f"{tid}: header buttons are type=button")
    # spec §3.3: moved blocks keep their ids; #hoverFreq became a <body> child after #bandTip; CW has no hidden
    hover, tip, sw = _by_id(t, "hoverFreq"), _by_id(t, "bandTip"), _by_id(t, "scopeWrap")
    check(sw not in _ancestors(t, hover), "#hoverFreq is no longer inside #scopeWrap")
    eq(t.nodes[hover][2], t.nodes[tip][2], "#hoverFreq is a sibling of #bandTip")
    check(hover > tip, "#hoverFreq follows #bandTip")
    check("hidden" not in t.nodes[_by_id(t, "cwTool")][1], "#cwTool carries no hidden attribute (AR-27)")
    for i in ("cwStatus", "cwClose", "cwDecodeBtn", "audioAvail"):
        check(any("tile-actions" in _classes(t.nodes[a]) for a in _ancestors(t, _by_id(t, i))), f"#{i} in .tile-actions")
    eq(t.nodes[_by_id(t, "cwDecodeBtn")][1].get("aria-pressed"), "false", "#cwDecodeBtn aria-pressed")
    check(t.nodes[_by_id(t, "meterBlock")][1].get("class") == "meter", "#meterBlock is the .meter")
    for i, attr in (("meterSel", "data-gate"), ("scopeOnlyCtl", "data-gate"), ("pttBtn", "data-tx"),
                    ("atasTune", "data-tx"), ("micBtn", "data-tx"), ("micMeter", "data-tx"), ("cwTx", "data-tx")):
        check(attr in t.nodes[_by_id(t, i)][1], f"#{i} keeps {attr}")
    check("hidden" not in t.nodes[_by_id(t, "arrangeBtn")][1], "#arrangeBtn is bound (no hidden)")
    check("hidden" not in t.nodes[_by_id(t, "umArrange")][1], "#umArrange is bound (no hidden)")
    bar = t.nodes[_by_id(t, "arrangeBar")][1]
    check(bar.get("role") == "toolbar" and "hidden" in bar, "#arrangeBar role=toolbar, hidden")
    for i in ("arrangeHelp", "arrangeAdd", "arrangeReset", "arrangeDone", "addPanel"):
        _by_id(t, i)
    # no console wrappers left; script + stylesheet order (§3.15)
    html = (FRONT / "index.html").read_text(encoding="utf-8")
    for gone in ('class="radio"', 'class="scopepane"', 'class="toprow"'):
        check(gone not in html, f"{gone} removed")
    srcs = re.findall(r'<script src="([^"]+)"', html)
    eq(srcs, ["/static/vendor/gridstack/gridstack-all.js?v=14.0.0", "/static/tr.js", "/static/waterfall.js",
              "/static/bandplan.js", "/static/layout.js", "/static/app.js", "/static/cwtool.js",
              "/static/vendor/lamejs/lame.min.js?v=1.2.1", "/static/recorder.js", "/static/spots.js", "/static/sstv.js",
              "/static/sstv-tx.js", "/static/account.js", "/static/bridge.js", "/static/device.js", "/static/admin.js"], "script order")
    check(not re.search(r"<script>\s*/\*\s*tabbed control panels", html), "the inline tab switcher moved to layout.js")
    css = re.findall(r'<link rel="stylesheet" href="([^"]+)"', html)
    eq(css, ["/static/vendor/gridstack/gridstack.min.css?v=14.0.0", "/static/style.css"], "stylesheets")


def _js_array(src: str, name: str) -> list:
    m = re.search(r"const " + name + r" = \[([^\]]*)\]", src)
    check(m, f"layout.js {name}")
    return re.findall(r'"([a-z]+)"', m.group(1))


@test("unit")
def layout_js_registry_matches_the_server(ctx):
    src = (FRONT / "layout.js").read_text(encoding="utf-8")
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from backend import layout_store
    eq(_js_array(src, "TILE_IDS"), list(layout_store.TILE_IDS), "layout.js TILE_IDS == layout_store.TILE_IDS")
    eq(TILE_IDS, list(layout_store.TILE_IDS), "registry order")
    # the §3.6 desktop default: stored unchanged by the server and filling exactly 24 rows, no overlap
    m = re.search(r"desktop: \{ column: 12, items: \[(.*?)\] \},", src, re.S)
    check(m, "DEFAULT.desktop in layout.js")
    items = []
    for mm in re.finditer(r'\{ id: "(\w+)", x: (\d+), y: (\d+), w: (\d+), h: (\d+)(, hidden: true)? \}', m.group(1)):
        items.append({"id": mm.group(1), "x": int(mm.group(2)), "y": int(mm.group(3)), "w": int(mm.group(4)),
                      "h": int(mm.group(5)), "hidden": bool(mm.group(6)), "collapsed": False})
    eq(sorted(i["id"] for i in items), sorted(TILE_IDS), "every tile in the desktop default")
    want = {"controls": (0, 0, 3, 18), "vfo": (3, 0, 4, 5), "ptt": (7, 0, 2, 5), "audio": (9, 0, 3, 5),
            "meter": (3, 5, 9, 4), "scope": (3, 9, 9, 15), "cw": (9, 9, 3, 15), "recorder": (0, 18, 3, 6),
            "spots": (9, 9, 3, 15), "sstv": (9, 9, 3, 15)}
    for i in items:
        eq((i["x"], i["y"], i["w"], i["h"]), want[i["id"]], f"§3.6 {i['id']}")
    eq([i["id"] for i in items if i["hidden"]], ["cw", "spots", "sstv"], "only CW, Spots and SSTV start hidden")
    stored = layout_store.validate("desktop", {"column": 12, "items": items})
    eq(stored["items"], items, "the server stores the default unchanged (no clamping)")
    vis = [i for i in items if not i["hidden"]]
    cells = set()
    for i in vis:
        for x in range(i["x"], i["x"] + i["w"]):
            for y in range(i["y"], i["y"] + i["h"]):
                check((x, y) not in cells, f"default overlap at {x},{y}")
                cells.add((x, y))
    eq(len(cells), 12 * 24, "the visible default fills exactly 12 x 24 cells")
    pm = re.search(r"phone: \{ column: 1, items: \[(.*?)\] \},", src, re.S)
    check(pm, "DEFAULT.phone in layout.js")
    ranks = re.findall(r'\{ id: "(\w+)", y: (\d+)', pm.group(1))
    eq([r[0] for r in sorted(ranks, key=lambda r: int(r[1]))], ["vfo", "ptt", "scope", "meter", "audio", "controls", "recorder", "cw", "spots", "sstv"],
       "§3.6 phone ranks")
    # the bounds layout.js falls back to are the server's table (it normally reads boot.layout_bounds)
    fb = re.search(r"desktop: \{ cols: (\d+), y_max: (\d+), h_max: (\d+), bottom_max: (\d+), dw_max: (\d+) \}", src)
    check(fb, "fallback bounds in layout.js")
    eq([int(x) for x in fb.groups()], [layout_store.BOUNDS["desktop"][k] for k in ("cols", "y_max", "h_max", "bottom_max", "dw_max")],
       "fallback desktop bounds")
    check("HEAD_ROWS = 2" in src, "fixed collapsed slot (AR-58)")
    for banned in (".save(", ".load(", "addWidget(", "removeAll(", "batchUpdate(", "columnOpts", ".compact(", '.on("change"'):
        check(banned not in src, f"layout.js never uses {banned} (§3.4)")
    check("alwaysShowResizeHandle: true" in src, "visible resize handles in arrange mode (S0 note 1)")
    check("setAnimation(anim(), true)" in src, "deferred animation restore (S0 note 2)")


@test("unit")
def recorder_tile_goes_under_controls_in_node_vm(ctx):
    """layout.js: the Recorder sits under Controls in the default, and a layout saved before it existed
    gets it there too (a full-height Controls gives up its 6 rows; a short one keeps its height; the
    phone puts it right after Controls; hidden with Controls hidden; a removed Recorder stays removed).
    The real tr.js + layout.js in a Node vm with a stand-in GridStack (tests/layout_recorder_test.js)."""
    r = subprocess.run(["node", str(REPO / "tests" / "layout_recorder_test.js")], capture_output=True, text=True,
                       timeout=180, cwd=str(REPO))
    eq(r.returncode, 0, f"layout_recorder_test.js: {r.stdout}{r.stderr}")
    check("ALL PASS" in r.stdout, f"layout_recorder_test.js output: {r.stdout}")


@test("unit")
def check_ids_passes_for_b3(ctx):
    r = subprocess.run([sys.executable, str(REPO / "tests" / "check_ids.py"), "--allow-missing", B3_ALLOW_MISSING,
                        "--base-ref", PHASE1_BASE_REF], capture_output=True, text=True, cwd=str(REPO), timeout=60)
    eq(r.returncode, 0, f"check_ids: {r.stdout}{r.stderr}")
    if str(REPO / "tests") not in sys.path:
        sys.path.insert(0, str(REPO / "tests"))
    import check_ids
    base = '<div id="a"></div><div id="b"></div><div id="rxDev"></div>'
    eq(check_ids.audit(base, '<p id="a"></p><p id="b"></p>', ["rxDev"]), [], "allowed missing id")
    check(check_ids.audit(base, '<p id="a"></p>', ["rxDev"]) == ["missing id: b"], "a lost id is reported")
    check(any("duplicate id: a" in p for p in check_ids.audit(base, '<p id="a"></p><i id="a"></i><b id="b"></b>', ["rxDev"])),
          "a duplicated id is reported")
    check(any("never had" in p for p in check_ids.audit(base, '<p id="a"></p><b id="b"></b>', ["rxDve"])),
          "a typo in --allow-missing is reported")


@test("unit")
def scripts_tile_mode_and_cwdiag(ctx):
    cw = (FRONT / "cwtool.js").read_text(encoding="utf-8")
    diag = cw.split("window.__cwdiag", 1)[1]
    check(not re.search(r"committed\s*\.", diag), "__cwdiag no longer reads the undeclared `committed`")
    check(re.search(r"committed\s*:", diag), "__cwdiag still reports a `committed` field")
    check("messages.map((m) => m.text)" in diag, "__cwdiag reads the chat log instead (§3.18)")
    check('panel.closest(".grid-stack-item, #tileParking")' in cw, "cwtool.js tile mode (§3.18)")
    app = (FRONT / "app.js").read_text(encoding="utf-8")
    check(".scopepane" not in app, "no .scopepane selectors left in app.js")
    check('$("meterBlock")' in app and '$("micMeter")' in app, "app.js uses #meterBlock / #micMeter")
    check("Layout().setRadio(p)" in app, "renderRadio hands the profile to the layout (gates)")
    srv = (REPO / "backend" / "server.py").read_text(encoding="utf-8")
    m = re.search(r"^ASSETS = \((.*?)\)\s*$", srv, re.M | re.S)
    check(m, "server.py ASSETS")
    eq(re.findall(r'"([^"]+)"', m.group(1)), ["style.css", "tr.js", "waterfall.js", "bandplan.js", "layout.js",
                                               "app.js", "cwtool.js", "recorder.js", "spots.js", "sstv.js", "sstv-tx.js", "account.js", "bridge.js",
                                               "device.js", "admin.js"],
       "ASSETS (cache-busted, §3.15)")
    for f in ("layout.js", "app.js", "cwtool.js", "recorder.js", "spots.js", "sstv.js", "sstv-tx.js", "tr.js", "waterfall.js", "account.js", "bridge.js", "device.js",
              "admin.js"):
        r = subprocess.run(["node", "--check", str(FRONT / f)], capture_output=True, text=True, timeout=60)
        eq(r.returncode, 0, f"node --check {f}: {r.stderr}")


@test("unit")
def console_css_replaced_by_tiles(ctx):
    css = (FRONT / "style.css").read_text(encoding="utf-8")
    body = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for gone in (".scopepane", ".toprow", ".radio{", ".radio {"):
        check(gone not in body, f"console rule {gone} removed")
    for rule in (".tile .controls{display:flex;flex-direction:column;gap:7px;columns:auto}",
                 ".tile .controls .grp{margin:0;padding:6px 8px}",
                 ".tile .controls .grp-title{margin-bottom:4px;font-size:9px}",
                 ".tile .controls .key{padding:6px 5px;font-size:12px;min-width:30px}",
                 ".tile .controls .step-row{margin-top:4px}",
                 ".controls .span-val{min-width:0;font-size:12px}",       # the two AR-30 rules (B2) are kept
                 ".controls .step-row{flex-wrap:wrap}",
                 ".tr-grid{isolation:isolate}",
                 ".tr-grid:not(.tr-ready)>.grid-stack-item{visibility:hidden}",
                 ".grid-stack-item.is-collapsed>.tile{bottom:auto;height:auto}",
                 "body.tr-nogrid{height:auto;overflow:auto}",
                 "body.tr-nogrid .tile-scope{height:60vh}",
                 ".tile-vfo{container-type:size}",
                 "[hidden]{display:none!important}"):
        check(rule in css, f"missing rule: {rule}")
    check(not re.search(r"\.gs-\d", body), "never style .gs-N classes (S12)")
    tile_rule = re.search(r"\n\.tile\{([^}]*)\}", body)
    check(tile_rule and not re.search(r"transform|filter|contain", tile_rule.group(1)), "no transform/filter/contain on .tile")


def _rule(body: str, selector: str):
    m = re.search(r"(?:^|[}\n])\s*" + re.escape(selector) + r"\{([^}]*)\}", body)
    return m.group(1) if m else None


@test("unit")
def tile_focus_ring_inside_clip_and_capped_fit_scrolls(ctx):
    """B3 review round 1: (1) the header focus ring is drawn INSIDE the header, because .tile clips
    (overflow:hidden) and the header fills its padding box; (2) a phone content-fit tile whose content
    is taller than the phone h_max gets .fit-capped and its body scrolls instead of clipping."""
    css = (FRONT / "style.css").read_text(encoding="utf-8")
    body = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    tile = _rule(body, ".tile")
    check(tile and "overflow:hidden" in tile, ".tile clips (the reason the ring must be inset)")
    head = _rule(body, ".tile-head:focus-visible")
    check(head, ".tile-head:focus-visible has its own rule")
    check("outline:2px solid var(--accent)" in head, "header ring: 2px accent outline")
    m = re.search(r"outline-offset:(-?\d+)px", head)
    check(m and int(m.group(1)) <= -2, f"header ring is inset (outline-offset <= -2px): {head}")
    btn = _rule(body, ".tile-btn:focus-visible")
    check(btn and "outline:2px solid var(--accent)" in btn, ".tile-btn keeps its own focus ring")
    check(not re.search(r"\.tile-head:focus-visible\s*,", body), "no grouped rule gives the header an outset ring")
    fit = _rule(body, "body[data-device=phone] .tile.fit .tile-body")
    check(fit and "overflow:visible" in fit, "content-fit bodies follow their content on phone")
    cap = _rule(body, "body[data-device=phone] .tile.fit.fit-capped .tile-body")
    check(cap and "overflow:auto" in cap, "a capped content-fit body scrolls")
    check("overscroll-behavior" not in cap, "the capped body keeps the phone overscroll-behavior:auto (no trap)")
    ph = _rule(body, "body[data-device=phone] .tile-body")
    check(ph and "overscroll-behavior:auto" in ph, "phone bodies: overscroll-behavior:auto (AR-37)")
    src = (FRONT / "layout.js").read_text(encoding="utf-8")
    fp = src.split("function fitPhone()", 1)
    check(len(fp) == 2, "layout.js fitPhone()")
    fp = fp[1].split("\n  }\n", 1)[0]
    check('classList.toggle("fit-capped", want > B.phone.h_max)' in fp, "fitPhone marks content taller than h_max")
    check("clamp(want, m, B.phone.h_max)" in fp, "the fitted height is still clamped to the phone h_max (AR-26)")


def _css_rules(css: str) -> list:
    """(at-rule context, selector list, declarations) for every style rule, comments removed. @media,
    @container and @supports nest; other at-rules (@property, @keyframes) are skipped."""
    body = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out = []

    def walk(s: str, ctx: tuple) -> None:
        i = 0
        while True:
            j = s.find("{", i)
            if j < 0:
                return
            head = s[i:j].split(";")[-1].strip()
            depth, k = 1, j + 1
            while k < len(s) and depth:
                depth += {"{": 1, "}": -1}.get(s[k], 0)
                k += 1
            inner = s[j + 1:k - 1]
            if head.startswith(("@media", "@container", "@supports")):
                walk(inner, ctx + (head,))
            elif not head.startswith("@"):
                decls = {}
                for d in inner.split(";"):
                    name, _, value = d.partition(":")
                    if name.strip():
                        decls[name.strip()] = value.strip()
                out.append((ctx, [x.strip() for x in head.split(",")], decls))
            i = k

    walk(body, ())
    return out


READOUT_EM = 7.4      # the widest VFO readout, 12 characters (1296.200.000), in em (measured 7.04 em)
FLOOR_PX = 16         # §4.2: the digits never go below 16 px


def _vfo_digit_problems(css: str) -> list:
    """Why the VFO digits could be clipped, overlapped or cut off (B3 review round 2), or [] if they can't."""
    rules, probs = _css_rules(css), []

    def get(sel, ctx_pred=lambda c: not c):
        return [d for c, sels, d in rules if sel in sels and ctx_pred(c)]

    # 1. nothing between the digits and the tile body hides overflow (the round-2 regression was
    #    `#bands.dual .b-center{overflow:hidden}`): the digits column, the row, the band list, the fit box
    #    (the readout itself: see 4, `.tile-vfo .b-center .band-freq{overflow:visible}`)
    for c, sels, d in rules:
        for s in sels:
            if re.search(r"(\.b-center|\.bandrow(\.active)?|#bands(\.dual)?|\.bands|\.tile-vfo>\.tile-fit)$", s):
                for prop in ("overflow", "overflow-x"):
                    if d.get(prop) in ("hidden", "clip"):
                        probs.append(f"{s} {{{prop}:{d[prop]}}} in {c or 'top level'} can cut the digits off")
    # 2. the digits column is an inline-size container whose floor holds the widest readout at 16 px
    bc = get(".tile-vfo .b-center")
    if not bc or bc[-1].get("container-type") != "inline-size":
        probs.append(".tile-vfo .b-center is not an inline-size container (the digits can't size to their column)")
    else:
        mw = bc[-1].get("min-width", "")
        m = re.fullmatch(r"calc\(([\d.]+) \* ([\d.]+)px\)|([\d.]+)px", mw)
        px = (float(m.group(1)) * float(m.group(2)) if m.group(1) else float(m.group(3))) if m else 0.0
        if px + 1e-6 < READOUT_EM * FLOOR_PX:
            probs.append(f".tile-vfo .b-center min-width {mw!r} < {READOUT_EM} em at {FLOOR_PX} px")
    # 3. every container-sized digit font is capped by its own column (N cqw * 7.4 em <= 100 % of it),
    #    keeps the §4.2 tile-width term (--vfo-wcap = 7.4cqw of the tile body) and the 16 px floor
    want = {".b-center .band-freq", ".bandrow.active .b-center .band-freq", "#bands:not(.dual) .bandrow.active .b-center .band-freq"}
    seen = set()
    for c, sels, d in rules:
        fs = d.get("font-size", "")
        if "cq" not in fs or not any(s.endswith(".band-freq") for s in sels):
            continue
        seen.update(s for s in sels if s in want)
        cqw = [float(x) for x in re.findall(r"([\d.]+)cqw", fs)]
        if not cqw or max(cqw) * READOUT_EM > 100 + 1e-6:
            probs.append(f"{sels} font-size {fs!r}: no column cap N cqw with N * {READOUT_EM} <= 100")
        if "var(--vfo-wcap)" not in fs or f"clamp({FLOOR_PX}px," not in fs:
            probs.append(f"{sels} font-size {fs!r}: the §4.2 width term or the {FLOOR_PX} px floor is missing")
    if seen != want:
        probs.append(f"digit font rules missing: {sorted(want - seen)}")
    if not re.search(r'@property --vfo-wcap\{syntax:"<length>";inherits:true;', css):
        probs.append("--vfo-wcap is not a registered <length> (it would resolve against the digits column)")
    if [d.get("--vfo-wcap") for d in get("#bands")] != ["7.4cqw"]:
        probs.append("#bands{--vfo-wcap:7.4cqw} (the §4.2 tile-width term) is missing")
    # 4. a row is never narrower than its content and its side tracks never shrink below their content
    row = get(".tile-vfo .bandrow")
    if not any(d.get("min-width") == "min-content" for d in row):
        probs.append(".tile-vfo .bandrow{min-width:min-content} is missing (a too-narrow tile must scroll, not overlap)")
    if not any(d.get("grid-template-columns") == "minmax(min-content,auto) minmax(auto,1fr) minmax(min-content,auto)" for d in row):
        probs.append(".tile-vfo .bandrow tracks: the mode box / side column can shrink below their content")
    fq = get(".tile-vfo .b-center .band-freq")
    if not fq or fq[-1].get("overflow") != "visible":
        probs.append(".tile-vfo .b-center .band-freq{overflow:visible} is missing (the phone ellipsis rule would clip)")
    # 5. one-line (short, dual watch) rows: the digits never shrink; when narrow, labels step aside
    short = lambda c: any("max-height:120px" in x for x in c)
    if not any(d.get("flex") == "0 0 auto" for d in get("#bands.dual .b-center .band-freq", short)):
        probs.append("short dual rows: #bands.dual .b-center .band-freq{flex:0 0 auto} is missing")
    hidden = {s for c, sels, d in rules if any("max-height:120px" in x and "max-width:320px" in x for x in c)
              and d.get("display") == "none" for s in sels}
    for s in ("#bands.dual .band-fil", "#bands.dual .b-vfo"):
        if s not in hidden:
            probs.append(f"short narrow (<= 320 px) dual rows: {s} does not step aside")
    # 6. the TX/RX tag is never hidden by a layout rule; width-only queries never reach inside .b-center
    for c, sels, d in rules:
        if d.get("display") == "none" and any(re.search(r"(\.tx(?![-\w])|#txTag)", s) for s in sels):
            probs.append(f"{sels} hides the TX/RX tag ({c})")
        if any(x.startswith("@container") and "width" in x and "height" not in x for x in c) \
                and any(re.search(r"\.(band-freq|ind-strip|ind)(?![-\w])", s) for s in sels):
            probs.append(f"{sels} in a width-only {c}: inside .b-center that queries .b-center itself")
    return probs


@test("unit")
def vfo_digits_fit_their_column_and_are_never_clipped(ctx):
    """B3 review round 2: at 900-955 px windows the IC-9700 dual-watch VFO cut the last digits off
    (`#bands.dual .b-center{overflow:hidden}` + side items keeping their room). The digits column is now an
    inline-size container the digits size themselves to (never larger than the §4.2 formula), with a floor
    that holds the widest readout at 16 px; rows never shrink below their content, so a too-narrow tile
    scrolls in its body; nothing in the chain hides overflow; labels step aside, the TX/RX tag never does.
    The pixel geometry (right edge of #mainFreq <= right edge of .b-center at 900x700 / 950x600 and 16 other
    sizes) is asserted in the browser; see the B3 report (fix round 2)."""
    css = (FRONT / "style.css").read_text(encoding="utf-8")
    probs = _vfo_digit_problems(css)
    check(not probs, "VFO digits: " + "; ".join(probs))
    # the parser and the checks see real rules (a self-test, so an empty list means something)
    rules = _css_rules(css)
    check(any(c and "max-height:120px" in c[-1] for c, _, _ in rules), "the parser sees the VFO container queries")
    check(_vfo_digit_problems(css.replace(".tile-vfo .b-center{container-type:inline-size;", ".tile-vfo .b-center{"))
          and _vfo_digit_problems(css + "\n@container (max-height:120px){#bands.dual .b-center{overflow:hidden}}")
          and _vfo_digit_problems(css + "\n@container (max-width:300px){#bands .tx{display:none}}"),
          "the checks catch a lost container, a hidden-overflow digits column and a hidden TX/RX tag")


@test("smoke")
def index_serves_the_grid_assets(ctx):
    srv = ctx.server()
    c = harness.ready_admin(srv)
    r = c.get("/")
    eq(r.status, 200, "/")
    html = r.text
    check('/static/vendor/gridstack/gridstack-all.js?v=14.0.0"' in html, "the GridStack bundle is loaded")
    check(re.search(r'/static/layout\.js\?v=\d+"', html), "layout.js is cache-busted like the other assets")
    check('id="tileParking"' in html and 'id="grid"' in html, "grid + parking in the page")
    m = re.search(r'<script id="tr-boot" type="application/json">(.*?)</script>', html)
    boot = json.loads(m.group(1))
    caps = {x["id"]: x["tile_caps"] for x in boot["radios"]}
    eq(caps["ic728"], {"ptt": False, "meters": False, "dual_watch": False}, "IC-728 tile caps")
    eq(caps["ic9700"], {"ptt": True, "meters": True, "dual_watch": True}, "IC-9700 tile caps")
    eq(boot["layout_bounds"]["desktop"]["bottom_max"], 200, "bounds shipped in the bootstrap")
    for name in ("gridstack-all.js", "gridstack.min.css"):
        r = c.get("/static/vendor/gridstack/" + name)
        eq(r.status, 200, name)
        eq(hashlib.sha256(r.body).hexdigest(), GS_SHA256[name], f"{name} served byte for byte")
    for name, want in EXO2_SHA256.items():                     # the radio-screen theme's font
        r = c.get("/static/vendor/exo2/" + name)
        eq(r.status, 200, name)
        eq(hashlib.sha256(r.body).hexdigest(), want, f"{name} served byte for byte")
    r = c.get("/static/layout.js")
    eq(r.status, 200, "/static/layout.js")
