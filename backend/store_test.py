"""JsonStore + settings tests — run: python -m backend.store_test

Everything happens in a scratch folder: <folder of $TELERIG_CONFIG>/store_test-<pid> when the
runner sets a scratch TELERIG_CONFIG, else a fresh system temp folder. No real config is touched;
no server, radio or socket is opened. Child processes are plain `python -c` workers on that folder.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from backend import auth, settings, store
from backend.store import JsonStore, StoreBusy, StoreCorrupt, StoreMissing

REPO = Path(__file__).resolve().parent.parent
_ok = True


def C(label, got, want):
    global _ok
    good = got == want
    _ok = _ok and good
    print(("PASS" if good else "FAIL"), label, "->", repr(got), "" if good else f"(want {want!r})")


def raises(fn, exc):
    try:
        fn()
    except exc:
        return True
    except Exception as e:
        return f"{type(e).__name__}: {e}"
    return False


def _base() -> Path:
    env = os.environ.get("TELERIG_CONFIG")
    if env:
        b = Path(env).resolve().parent / f"store_test-{os.getpid()}"
    else:
        b = Path(tempfile.mkdtemp(prefix="tr-store-test-"))
    for real in (Path.home() / ".telerig", Path.home() / ".radio-webop"):
        rp = str(real.resolve()).lower()
        if str(b.resolve()).lower().startswith(rp):
            raise SystemExit(f"REFUSING: scratch folder {b} is inside the real {real}")
    b.mkdir(parents=True, exist_ok=True)
    return b


BASE = _base()
_n = 0


def fresh(name: str) -> Path:
    global _n
    _n += 1
    d = BASE / f"{_n:02d}-{name}"
    d.mkdir(parents=True, exist_ok=True)
    return d / "data.json"


def child(code: str, *args) -> subprocess.Popen:
    pre = f"import sys, time, json; sys.path.insert(0, {str(REPO)!r})\nfrom backend.store import JsonStore\n"
    return subprocess.Popen([sys.executable, "-c", pre + code, *map(str, args)], cwd=str(REPO),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# ---- missing file -----------------------------------------------------------------------------
p = fresh("missing")
s = JsonStore(p)
C("missing file reads as {}", s.load(), {})
C("missing file status", s.status(), "missing")
C("a read creates nothing (no file, no lock file)", (p.exists(), s.lock_path.exists()), (False, False))
C("mutate(create=False) on a missing file -> StoreMissing", raises(lambda: s.mutate(lambda d: d.update(a=1)), StoreMissing), True)
C("... and still nothing was created", p.exists(), False)
s.mutate(lambda d: d.update(a=1), create=True)
C("mutate(create=True) creates the file", json.loads(p.read_text()), {"a": 1})
C("the lock file is persistent (kept after mutate)", s.lock_path.exists(), True)
C("load after mutate", s.load(), {"a": 1})
C("load returns a copy (caller edits don't leak into the cache)", (s.load().update(b=2), s.load())[1], {"a": 1})
C("mutate may return a new dict", s.mutate(lambda d: {"x": [1, 2]}), {"x": [1, 2]})

# ---- a corrupt file raises and its bytes are unchanged -------------------------------------------
p = fresh("corrupt")
bad = b'{"version": 2, "users": {'
p.write_bytes(bad)
s = JsonStore(p)
C("corrupt file: load raises StoreCorrupt", raises(s.load, StoreCorrupt), True)
C("corrupt file: status", s.status(), "corrupt")
C("corrupt file: mutate raises StoreCorrupt", raises(lambda: s.mutate(lambda d: d.update(a=1), create=True), StoreCorrupt), True)
C("corrupt file: bytes unchanged", p.read_bytes(), bad)
C("corrupt file: no temp files left", sorted(x.name for x in p.parent.iterdir() if ".tmp-" in x.name), [])
for label, data in (("a JSON array", b"[1, 2]"), ("a JSON string", b'"x"'), ("invalid UTF-8", b'{"a": "\xff"}')):
    p2 = fresh("corrupt2")
    p2.write_bytes(data)
    s2 = JsonStore(p2)
    C(f"corrupt ({label}) raises", raises(s2.load, StoreCorrupt), True)
    C(f"corrupt ({label}) bytes unchanged", p2.read_bytes(), data)
p3 = fresh("bom")
p3.write_bytes(b'\xef\xbb\xbf{"a": 1}')
C("a UTF-8 BOM (Notepad) is accepted", JsonStore(p3).load(), {"a": 1})
p4 = fresh("unreadable")
p4.mkdir()                                    # exists but cannot be read as a file
s4 = JsonStore(p4)
C("an existing but unreadable store is corrupt, never treated as empty", raises(s4.load, StoreCorrupt), True)
C("unreadable: status", s4.status(), "corrupt")
C("unreadable: mutate refuses", raises(lambda: s4.mutate(lambda d: d.update(a=1), create=True), StoreCorrupt), True)

# validation failure on load = corrupt, file untouched
def need_version(d):
    if d.get("version") != 2:
        raise ValueError("version must be 2")

p = fresh("validate")
p.write_text('{"version": 1}')
s = JsonStore(p, validate=need_version)
C("validator failure on load -> StoreCorrupt", raises(s.load, StoreCorrupt), True)
C("validator failure: detail names the problem", "version must be 2" in s.detail(), True)
C("validator failure: bytes unchanged", p.read_text(), '{"version": 1}')

# a good file going corrupt at runtime keeps the cache, then recovers when fixed
clk = FakeClock()
p = fresh("runtime")
p.write_text('{"gen": 1}')
s = JsonStore(p, clock=clk)
C("runtime: initial load", s.load(), {"gen": 1})
p.write_text('{"gen": ')
clk.t += 2.1
C("runtime: corrupt rewrite detected -> raises", raises(s.load, StoreCorrupt), True)
C("runtime: the last good cache is kept internally", s._data, {"gen": 1})
C("runtime: still raising while unchanged", (clk.__setattr__("t", clk.t + 0.6), raises(s.load, StoreCorrupt))[1], True)
p.write_text('{"gen": 3}')
clk.t += 2.1
C("runtime: fixed file unlocks without a restart", (s.load(), s.status()), ({"gen": 3}, "ok"))

# ---- an exception before os.replace leaves the original --------------------------------------------
p = fresh("atomic")
s = JsonStore(p, validate=need_version)
s.mutate(lambda d: d.update(version=2, n=1), create=True)
orig = p.read_bytes()


def boom(d):
    d["n"] = 99
    raise ValueError("fn failed")


C("fn raising propagates", raises(lambda: s.mutate(boom), ValueError), True)
C("fn raising: file unchanged", p.read_bytes(), orig)
C("fn raising: cache unchanged", s.load(), {"version": 2, "n": 1})
C("validator rejecting the result propagates", raises(lambda: s.mutate(lambda d: d.update(version=3)), ValueError), True)
C("validator rejecting: file unchanged", p.read_bytes(), orig)
C("non-JSON-serialisable result propagates", raises(lambda: s.mutate(lambda d: d.update(x=object())), TypeError), True)
C("non-serialisable: file unchanged", p.read_bytes(), orig)

real_replace = store.os.replace


def failing_replace(a, b):
    raise OSError("simulated failure before replace")


store.os.replace = failing_replace
try:
    C("os.replace failing propagates", raises(lambda: s.mutate(lambda d: d.update(n=2)), OSError), True)
finally:
    store.os.replace = real_replace
C("os.replace failing: file unchanged", p.read_bytes(), orig)
C("os.replace failing: cache unchanged", s.load(), {"version": 2, "n": 1})
C("os.replace failing: temp file removed", sorted(x.name for x in p.parent.iterdir() if ".tmp-" in x.name), [])
real_fsync = store.os.fsync
store.os.fsync = lambda fd: (_ for _ in ()).throw(OSError("simulated fsync failure"))
try:
    C("fsync failing propagates", raises(lambda: s.mutate(lambda d: d.update(n=3)), OSError), True)
finally:
    store.os.fsync = real_fsync
C("fsync failing: file unchanged", p.read_bytes(), orig)
C("a later mutate still works", s.mutate(lambda d: d.update(n=4))["n"], 4)

# ---- two subprocesses x 200 increments = 400 -------------------------------------------------------
p = fresh("concurrent")
INC = """
s = JsonStore(sys.argv[1])
for i in range(200):
    s.mutate(lambda d: d.update(n=d.get("n", 0) + 1), create=True)
    time.sleep(0.001)
print("done")
"""
t0 = time.monotonic()
procs = [child(INC, p) for _ in range(2)]
outs = [pr.communicate(timeout=180) for pr in procs]
codes = [pr.returncode for pr in procs]
C("two workers exited cleanly", codes, [0, 0])
if codes != [0, 0]:
    print("\n".join(o[1][-1500:] for o in outs))
C("two subprocesses x 200 increments = 400", JsonStore(p).load().get("n"), 400)
print(f"      (concurrent increments took {time.monotonic() - t0:.1f}s)")

# ---- a process that dies holding the OS lock releases it ---------------------------------------------
p = fresh("deadlock")
JsonStore(p).mutate(lambda d: d.update(n=1), create=True)
HOLD = """
s = JsonStore(sys.argv[1])
with s.hold_lock():
    print("LOCKED", flush=True)
    time.sleep(120)
"""
pr = child(HOLD, p)
line = pr.stdout.readline().strip()
C("holder child has the lock", line, "LOCKED")
quick = JsonStore(p, lock_timeout=0.3)
C("while held: mutate -> StoreBusy", raises(lambda: quick.mutate(lambda d: d.update(n=2)), StoreBusy), True)
C("while held: file unchanged", JsonStore(p).load(), {"n": 1})
pr.kill()
pr.wait(timeout=30)
t0 = time.monotonic()
res = raises(lambda: JsonStore(p).mutate(lambda d: d.update(n=3)), Exception)
C("after the holder died: the next mutate succeeds (no stale-lock breaking)", res, False)
C("after the holder died: value written", JsonStore(p).load(), {"n": 3})
C("the lock file was never deleted", (p.parent / (p.name + ".lock")).exists(), True)
print(f"      (lock re-acquired {time.monotonic() - t0:.2f}s after the kill)")

# ---- a same-size rewrite (gen 1 -> 2) is detected within 2 s by the hash check ----------------------
# deterministic (fake clock)
clk = FakeClock()
p = fresh("samesize-fake")
p.write_text('{"gen": 1}')
s = JsonStore(p, clock=clk)
C("same-size (fake clock): initial", s.load()["gen"], 1)
st0 = os.stat(p)
with open(p, "r+b") as f:
    f.write(b'{"gen": 2}')
os.utime(p, ns=(st0.st_atime_ns, st0.st_mtime_ns))
C("same-size: stat signature unchanged (only the hash can see it)",
  store._file_sig(os.stat(p)) == store._file_sig(st0), True)
clk.t += 0.6
C("same-size: not visible to the 0.5 s stat check", s.load()["gen"], 1)
clk.t += 1.45                       # 2.05 s after the last hash
C("same-size: seen by the 2 s hash check", s.load()["gen"], 2)

# real time
p = fresh("samesize-real")
p.write_text('{"gen": 1}')
s = JsonStore(p)
C("same-size (real time): initial", s.load()["gen"], 1)
st0 = os.stat(p)
t_rw = time.monotonic()
with open(p, "r+b") as f:
    f.write(b'{"gen": 2}')
os.utime(p, ns=(st0.st_atime_ns, st0.st_mtime_ns))
seen = None
while time.monotonic() - t_rw < 4.0:
    if s.load()["gen"] == 2:
        seen = time.monotonic() - t_rw
        break
    time.sleep(0.02)
C("same-size (real time): detected within the 2 s hash interval (+0.25 s scheduling margin)", seen is not None and seen <= 2.25, True)
print(f"      (detected after {seen if seen is None else round(seen, 2)}s)")

# ---- change hook --------------------------------------------------------------------------------
clk = FakeClock()
calls = []
p = fresh("hook")
s = JsonStore(p, clock=clk, on_change=lambda old, new: calls.append((old, new)))
s.mutate(lambda d: d.update(gen=1), create=True)
C("hook fires on mutate", calls[-1:], [({}, {"gen": 1})])
JsonStore(p).mutate(lambda d: d.update(gen=2))           # another writer (e.g. the CLI)
clk.t += 0.6
s.load()
C("hook fires on an external change (stat path)", calls[-1:], [({"gen": 1}, {"gen": 2})])
n = len(calls)
s.mutate(lambda d: None)                                   # no content change
C("no hook for an unchanged write", len(calls), n)
s.on_change = lambda old, new: 1 / 0
C("a failing hook never breaks a write", raises(lambda: s.mutate(lambda d: d.update(gen=3)), Exception), False)


# ---- settings: lenient config.json (AR-51) -------------------------------------------------------------
def use_config(path: Path) -> None:
    os.environ["TELERIG_CONFIG"] = str(path)
    os.environ.pop("RADIO_WEBOP_CONFIG", None)
    auth._reset_for_tests()
    settings._reset_for_tests()


saved_env = os.environ.get("TELERIG_CONFIG")
cfg = fresh("settings").with_name("config.json")
raw_cfg = {
    "session_secret": "ab" * 32,
    "allowed_hosts": ["Radio-PC:8700", "HTTPS://PC.Tailnet.TS.NET", "http://Radio.LAN:8080/",
                      "radio-pc", "192.168.1.5", "localhost", "pc.localhost", "http://127.0.0.1:80",
                      "bad host!", 5, "https://x.example/path"],
    "behind_proxy": "yes",                                   # wrongly typed
    "some_future_key": {"anything": True},                   # unknown: ignored
}
cfg.write_text(json.dumps(raw_cfg, indent=2))
before = cfg.read_bytes()
use_config(cfg)
err = io.StringIO()
with contextlib.redirect_stderr(err):
    st = settings.status()
    v = settings.view()
    from backend import accounts
    init_ok = raises(accounts.ensure_initialized, Exception)
text = err.getvalue()
C("settings: wrongly typed key does NOT lock (status ok)", st, "ok")
C("settings: ensure_initialized does not raise", init_ok, False)
C("settings: allowlist normalised (lowercase, port dropped from names, explicit origin ports, junk dropped)",
  v.allowed_hosts, ("radio-pc", "https://pc.tailnet.ts.net:443", "http://radio.lan:8080"))
C("settings: allowed_names = hostnames of names and origins", sorted(v.allowed_names),
  ["pc.tailnet.ts.net", "radio-pc", "radio.lan"])
C("settings: allowed_origins = full origins only", sorted(v.allowed_origins),
  ["http://radio.lan:8080", "https://pc.tailnet.ts.net:443"])
C("settings: wrongly typed behind_proxy -> default false", v.behind_proxy, False)
C("settings: a warning names the wrongly typed key", "behind_proxy" in text, True)
C("settings: warnings for the dropped entries", all(w in text for w in ("192.168.1.5", "localhost", "bad host!")), True)
C("settings: nothing written back", cfg.read_bytes(), before)
err2 = io.StringIO()
with contextlib.redirect_stderr(err2):
    settings.view()
    settings.allowed_names()
C("settings: warnings are printed once, not on every read", err2.getvalue(), "")
print("      (warnings printed:\n        " + text.strip().replace("\n", "\n        ") + ")")

# accessor shapes + credentials never surface from radio_transports
cfg.write_text(json.dumps({"host_audio": {"ft991a": {"rx": "Mic (USB Audio CODEC)", "tx": 7}},
                           "radio_transports": {"ic9700": {"transport": "lan", "host": "192.0.2.4", "port": 50001,
                                                           "user": "u", "password": "p"},
                                                "ft991a": {"transport": "serial", "port": "COM7", "baud": 38400},
                                                "bad": {"transport": "carrier-pigeon"}},
                           "last_radio": "ic9700", "behind_proxy": True, "behind_proxy_source": "tailscale"}))
settings._reset_for_tests()
with contextlib.redirect_stderr(io.StringIO()):
    C("settings: host_audio pins (bad device type dropped)", settings.host_audio(),
      {"ft991a": {"rx": "Mic (USB Audio CODEC)", "tx": None}})
    C("settings: radio_transports drop credentials and invalid kinds", settings.radio_transports(),
      {"ic9700": {"transport": "lan", "host": "192.0.2.4", "port": 50001},
       "ft991a": {"transport": "serial", "port": "COM7", "baud": 38400}})
    C("settings: last_radio / behind_proxy / source",
      (settings.last_radio(), settings.behind_proxy(), settings.behind_proxy_source()), ("ic9700", True, "tailscale"))

# unparsable config.json = corrupt (locked mode), bytes untouched; missing = defaults, not created
cfg.write_bytes(b'{"allowed_hosts": [')
settings._reset_for_tests()
C("settings: unparsable config.json -> corrupt", settings.status(), "corrupt")
C("settings: ensure_initialized raises StoreCorrupt", raises(accounts.ensure_initialized, StoreCorrupt), True)
C("settings: view() raises StoreCorrupt", raises(settings.view, StoreCorrupt), True)
C("settings: corrupt bytes unchanged", cfg.read_bytes(), b'{"allowed_hosts": [')
cfg2 = fresh("nocfg").with_name("config.json")
use_config(cfg2)
C("settings: missing config.json -> defaults", (settings.status(), settings.allowed_hosts(), settings.behind_proxy()),
  ("missing", (), False))
C("settings: missing config.json is never created by reads", cfg2.exists(), False)
C("settings: auth reads (host/origin/proxy checks) create no file",
  (auth.host_ok("radio-pc:8790"), settings.behind_proxy(), cfg2.exists()), (True, False, False))

# ---- mutate(timeout=, move_corrupt=) (A3: best-effort writes and the --reset-admin repair) -----------
p = fresh("move-corrupt")
p.write_bytes(b'{"broken": ')
s = JsonStore(p)
C("move_corrupt: a corrupt store is renamed aside and fn starts from {}",
  s.mutate(lambda d: d.update(fresh=True), move_corrupt=True), {"fresh": True})
C("move_corrupt: the backup holds the corrupt bytes", s.last_backup.read_bytes(), b'{"broken": ')
C("move_corrupt: the backup is named <name>.corrupt-<UTC>", s.last_backup.name.startswith("data.json.corrupt-"), True)
C("move_corrupt: a healthy store is not moved", (s.mutate(lambda d: d.update(n=1), move_corrupt=True),
                                                 sorted(x.name for x in p.parent.iterdir() if "corrupt" in x.name)),
  ({"fresh": True, "n": 1}, [s.last_backup.name]))
pr = child(HOLD, p)
C("timeout: holder child has the lock", pr.stdout.readline().strip(), "LOCKED")
t0 = time.monotonic()
C("timeout: mutate(timeout=0.2) gives up quickly with StoreBusy",
  raises(lambda: JsonStore(p).mutate(lambda d: d.update(n=9), timeout=0.2), StoreBusy), True)
C("timeout: ... within ~0.2 s, not the 5 s default", time.monotonic() - t0 < 1.5, True)
pr.kill()
pr.wait(timeout=30)

# normalize_entry edge cases (the allowlist rules)
for e, want in (("Radio-PC", ("name", "radio-pc")), ("radio-pc:8700", ("name", "radio-pc")),
                ("pc.ts.net.", ("name", "pc.ts.net")), ("https://pc.ts.net", ("origin", "https://pc.ts.net:443")),
                ("http://pc.lan", ("origin", "http://pc.lan:80")), ("http://pc.lan:8443/", ("origin", "http://pc.lan:8443"))):
    C(f"normalize_entry({e!r})", settings.normalize_entry(e), want)
for e in ("localhost", "LOCALHOST:80", "a.localhost", "10.0.0.1", "[::1]", "::1", "http://[::1]:80",
          "http://localhost:8790", "ftp://x.example", "https://x.example/p", "https://u@x.example", "", "a b", "radio-pc:x"):
    C(f"normalize_entry({e!r}) rejected", raises(lambda: settings.normalize_entry(e), ValueError), True)


# ---- the exact Origin rule (I15) against the normalised allowlist ----------------------------------------
def scope(host, peer="127.0.0.1", **hdrs):
    h = [(b"host", host.encode())] + [(k.replace("_", "-").encode(), v.encode()) for k, v in hdrs.items()]
    return {"type": "http", "headers": h, "client": (peer, 50000), "server": ("127.0.0.1", 8790), "scheme": "http"}


cfg3 = fresh("origin").with_name("config.json")
cfg3.write_text(json.dumps({"allowed_hosts": ["radio-pc", "https://pc.tailnet.ts.net"]}))
use_config(cfg3)
cases = [
    ("same host+port", "http://127.0.0.1:8790", scope("127.0.0.1:8790"), True),
    ("other port on the same host", "http://127.0.0.1:8077", scope("127.0.0.1:8790"), False),
    ("other scheme default port", "https://127.0.0.1", scope("127.0.0.1:8790"), False),
    ("missing origin", None, scope("127.0.0.1:8790"), False),
    ("null origin", "null", scope("127.0.0.1:8790"), False),
    ("file: origin", "file://", scope("127.0.0.1:8790"), False),
    ("evil.example", "http://evil.example", scope("127.0.0.1:8790"), False),
    ("IPv6 same host+port", "http://[::1]:8790", scope("[::1]:8790"), True),
    ("IPv6 other port", "http://[::1]:8791", scope("[::1]:8790"), False),
    ("bare allowlisted name never relaxes Origin", "http://radio-pc:8077", scope("radio-pc:8790"), False),
    ("bare allowlisted name, same host+port", "http://radio-pc:8790", scope("radio-pc:8790"), True),
    ("allowlisted full origin", "https://pc.tailnet.ts.net", scope("127.0.0.1:8790"), True),
    ("allowlisted origin, other port", "https://pc.tailnet.ts.net:8443", scope("pc.tailnet.ts.net:8790"), False),
    ("allowlisted origin, other scheme", "http://pc.tailnet.ts.net:443", scope("127.0.0.1:8790"), False),
    # round 3: the scheme counts, and the default port comes from the REQUEST's effective scheme
    ("Host without port + X-Forwarded-Proto https from loopback", "https://radio.lan",
     scope("radio.lan", x_forwarded_proto="https"), True),
    ("... the same request, http:// Origin refused", "http://radio.lan",
     scope("radio.lan", x_forwarded_proto="https"), False),
    ("Host without port on plain http: an https Origin is refused", "https://radio.lan", scope("radio.lan"), False),
    ("Host without port on plain http: http://name (port 80) passes", "http://radio.lan", scope("radio.lan"), True),
    ("Serve: Host pc2.tailnet.ts.net + X-Forwarded-Proto https", "https://pc2.tailnet.ts.net",
     scope("pc2.tailnet.ts.net", x_forwarded_proto="https"), True),
    ("Serve: ... http://pc2.tailnet.ts.net refused", "http://pc2.tailnet.ts.net",
     scope("pc2.tailnet.ts.net", x_forwarded_proto="https"), False),
    ("Serve: ... https://pc2.tailnet.ts.net:8443 refused", "https://pc2.tailnet.ts.net:8443",
     scope("pc2.tailnet.ts.net", x_forwarded_proto="https"), False),
    ("https://127.0.0.1:8790 on a plain-http server refused", "https://127.0.0.1:8790", scope("127.0.0.1:8790"), False),
    ("X-Forwarded-Proto from a non-loopback peer ignored (https refused)", "https://radio.lan",
     scope("radio.lan", peer="192.0.2.9", x_forwarded_proto="https"), False),
    ("X-Forwarded-Proto from a non-loopback peer ignored (http passes)", "http://radio.lan",
     scope("radio.lan", peer="192.0.2.9", x_forwarded_proto="https"), True),
    ("X-Forwarded-Host/Port/Proto from loopback", "https://radio.lan:8443",
     scope("127.0.0.1:8790", x_forwarded_host="radio.lan", x_forwarded_port="8443", x_forwarded_proto="https"), True),
    ("X-Forwarded-Host/Port without -Proto: the request is http, an https Origin is refused", "https://radio.lan:8443",
     scope("127.0.0.1:8790", x_forwarded_host="radio.lan", x_forwarded_port="8443"), False),
    ("X-Forwarded-Host ignored from a non-loopback peer", "https://radio.lan:8443",
     scope("127.0.0.1:8790", peer="192.0.2.9", x_forwarded_host="radio.lan", x_forwarded_port="8443",
           x_forwarded_proto="https"), False),
    ("X-Forwarded-Port fills a Host without a port", "https://radio.lan:8443",
     scope("radio.lan", x_forwarded_port="8443", x_forwarded_proto="https"), True),
    ("X-Forwarded-Port never overrides the Host's own port", "https://radio.lan:443",
     scope("radio.lan:8443", x_forwarded_port="443", x_forwarded_proto="https"), False),
    ("X-Forwarded-Port from a non-loopback peer ignored", "http://radio.lan:8443",
     scope("radio.lan", peer="192.0.2.9", x_forwarded_port="8443"), False),
    ("path in Origin refused", "http://127.0.0.1:8790/x", scope("127.0.0.1:8790"), False),
    ("malformed port refused", "http://127.0.0.1:99999", scope("127.0.0.1:8790"), False),
    ("malformed Host refused", "http://127.0.0.1:8790", scope("127.0.0.1:abc"), False),
]
for label, origin, sc, want in cases:
    C(f"origin_ok: {label}", auth.origin_ok(origin, sc), want)

# restore the runner's environment
if saved_env is None:
    os.environ.pop("TELERIG_CONFIG", None)
else:
    os.environ["TELERIG_CONFIG"] = saved_env
auth._reset_for_tests()
settings._reset_for_tests()

if _ok:
    shutil.rmtree(BASE, ignore_errors=True)
else:
    print(f"scratch kept at {BASE}")
print("\nALL PASS" if _ok else "\nSOME FAILED")
raise SystemExit(0 if _ok else 1)
