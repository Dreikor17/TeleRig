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
TILE_IDS = ["vfo", "ptt", "audio", "meter", "scope", "controls", "cw"]
B3_ALLOW_MISSING = "rxDev,micDev"


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
    eq([t.nodes[k][1].get("data-tile") for k in items], TILE_IDS, "seven tiles, registry order")
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
              "/static/bandplan.js", "/static/layout.js", "/static/app.js", "/static/cwtool.js"], "script order")
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
    want = {"controls": (0, 0, 3, 24), "vfo": (3, 0, 4, 5), "ptt": (7, 0, 2, 5), "audio": (9, 0, 3, 5),
            "meter": (3, 5, 9, 4), "scope": (3, 9, 9, 15), "cw": (9, 9, 3, 15)}
    for i in items:
        eq((i["x"], i["y"], i["w"], i["h"]), want[i["id"]], f"§3.6 {i['id']}")
    eq([i["id"] for i in items if i["hidden"]], ["cw"], "only CW starts hidden")
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
    eq([r[0] for r in sorted(ranks, key=lambda r: int(r[1]))], ["vfo", "ptt", "scope", "meter", "audio", "controls", "cw"],
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
def check_ids_passes_for_b3(ctx):
    r = subprocess.run([sys.executable, str(REPO / "tests" / "check_ids.py"), "--allow-missing", B3_ALLOW_MISSING],
                       capture_output=True, text=True, cwd=str(REPO), timeout=60)
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
    m = re.search(r"^ASSETS = \((.*?)\)$", srv, re.M)
    check(m, "server.py ASSETS")
    eq(re.findall(r'"([^"]+)"', m.group(1)), ["style.css", "tr.js", "waterfall.js", "bandplan.js", "layout.js",
                                               "app.js", "cwtool.js"], "ASSETS (cache-busted, §3.15)")
    for f in ("layout.js", "app.js", "cwtool.js", "tr.js", "waterfall.js"):
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
    r = c.get("/static/layout.js")
    eq(r.status, 200, "/static/layout.js")
