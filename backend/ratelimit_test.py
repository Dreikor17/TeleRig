"""Login rate-limiter tests — run: python -m backend.ratelimit_test

The three keys of §2.15 (F7, AR-20) with a fake monotonic clock: per-source K1 (5 for an IP, 20
for direct-local, never for a loopback IP), per-(user, source) K2 (10), per-user K3 (30, then a
lock with exponential backoff that decays after 24 h), the K3 bypass for direct-local and known
clean sources, clears, the key cap and the sweep. Pure in-memory: no file, socket or server.
"""
from backend import ratelimit
from backend.ratelimit import DIRECT_LOCAL, Limiter

_ok = True


def C(label, got, want):
    global _ok
    good = got == want
    _ok = _ok and good
    print(("PASS" if good else "FAIL"), label, "->", repr(got), "" if good else f"(want {want!r})")


class Clock:
    def __init__(self):
        self.t = 10_000.0

    def __call__(self):
        return self.t


def new():
    clk = Clock()
    return Limiter(clock=clk, wall=lambda: 1_700_000_000.0 + clk.t), clk


# ---- K1 per source --------------------------------------------------------------------------------
L, clk = new()
for i in range(5):
    C(f"K1 IP: attempt {i + 1} allowed", L.check(None, "203.0.113.5"), 0.0)
    L.fail(None, "203.0.113.5")
w = L.check("ADMIN", "203.0.113.5")
C("K1 IP: 6th attempt refused (even for another user)", w > 0, True)
C("K1 IP: retry_after is the time until the oldest failure leaves the 15-min window", round(w), 900)
C("K1 IP: another IP is unaffected", L.check("ADMIN", "203.0.113.6"), 0.0)
clk.t += 901
C("K1 IP: window slides (allowed again after 15 min)", L.check(None, "203.0.113.5"), 0.0)

L, clk = new()
for i in range(19):
    L.fail(f"U{i:02d}", DIRECT_LOCAL)
C("K1 direct-local: 19 failures still allowed", L.check("ADMIN", DIRECT_LOCAL, direct_local=True), 0.0)
L.fail("U19", DIRECT_LOCAL)
C("K1 direct-local: the 21st attempt is refused (limit 20)", L.check("ADMIN", DIRECT_LOCAL, direct_local=True) > 0, True)
C("K1 direct-local does not affect a real IP", L.check("ADMIN", "198.51.100.7"), 0.0)

L, clk = new()
for i in range(50):
    L.fail(f"Y{i}", "127.0.0.1")
C("K1 is never applied to a loopback IP (a proxy without XFF)", L.check("Z", "127.0.0.1"), 0.0)
C("... nor to ::1 / IPv4-mapped loopback", (ratelimit.k1_applies("::1"), ratelimit.k1_applies("::ffff:127.0.0.1")), (False, False))
C("K1 applies to direct-local and real IPs", (ratelimit.k1_applies(DIRECT_LOCAL), ratelimit.k1_applies("10.0.0.5")), (True, True))

# ---- K2 per (user, source) ------------------------------------------------------------------------
L, clk = new()
for i in range(10):
    L.fail("X", "127.0.0.9")                                  # loopback source: K1 not applied
C("K2: 10 failures for X from one source -> X refused there", L.check("X", "127.0.0.9") > 0, True)
C("K2: another user from the same source is fine", L.check("Y", "127.0.0.9"), 0.0)
C("K2: X from another source is fine", L.check("X", "198.51.100.10"), 0.0)
L.success("X", "198.51.100.10")
C("K2: success elsewhere does not clear the blocked pair", L.check("X", "127.0.0.9") > 0, True)
L.success("X", "127.0.0.9")
C("K2: success from that source clears the pair", L.check("X", "127.0.0.9"), 0.0)

# ---- K3 per user, lock with backoff, bypasses -------------------------------------------------------
L, clk = new()
L.success("X", "198.51.100.200")                              # a known source (earlier good sign-in)
tripped = [L.fail("X", f"203.0.113.{i}") for i in range(1, 31)]
C("K3: the 30th failure trips the user lock (only that one)", (tripped.count(True), tripped[-1]), (1, True))
C("K3: a fresh unknown IP is refused", L.check("X", "203.0.113.99") > 0, True)
C("K3: lock is 15 min at level 0", round(L.check("X", "203.0.113.99")), 900)
C("K3 bypass: a known clean source", L.check("X", "198.51.100.200"), 0.0)
C("K3 bypass: direct-local", L.check("X", DIRECT_LOCAL, direct_local=True), 0.0)
C("K3: other users are unaffected", L.check("Y", "203.0.113.99"), 0.0)
C("locked_until is a wall-clock time ~15 min ahead",
  round(L.locked_until("X") - (1_700_000_000.0 + clk.t)), 900)
lo = L.lockouts()
C("lockouts() lists the user lock", [(u["key"], u["level"]) for u in lo["users"]], [("X", 0)])
C("lockouts() lists the K1-locked IPs (none: 1 failure each)", lo["ips"], [])
L.fail("X", "198.51.100.200")
C("a known source with a fresh failure is no longer 'clean'", L.check("X", "198.51.100.200") > 0, True)
clk.t += 901
for i in range(30):
    L.fail("X", f"192.0.2.{i}")
C("K3 backoff: second trip within 24 h -> level 1 (30 min)", round(L.check("X", "192.0.2.200")), 1800)
clk.t += 1801
for i in range(30):
    L.fail("X", f"192.0.2.{100 + i}")
C("K3 backoff: third trip -> level 2 (60 min)", round(L.check("X", "192.0.2.250")), 3600)
clk.t += 3601 + 24 * 3600 + 1
for i in range(30):
    L.fail("X", f"198.18.0.{i}")
C("K3 decay: after 24 h without a trip the level resets to 15 min", round(L.check("X", "198.18.1.1")), 900)
L.clear_user("X")
C("clear_user unlocks", L.check("X", "198.18.1.1"), 0.0)
C("K3 levels cap at 240 min", ratelimit.BACKOFF_MIN[-1], 240)

# known sources: <= 8 per user, direct-local and loopback never recorded
L, clk = new()
for i in range(12):
    L.success("Q", f"198.51.100.{i}")
L.success("Q", DIRECT_LOCAL)
L.success("Q", "127.0.0.1")
C("known sources: at most 8 per user, newest kept, no direct-local/loopback",
  list(L._known["Q"]), [f"198.51.100.{i}" for i in range(4, 12)])
clk.t += 30 * 86400 + 1
L.sweep()
C("known sources expire after 30 days", "Q" in L._known, False)

# check-before-verify: a limited key refuses even the right password (the caller never verifies)
L, clk = new()
for i in range(5):
    L.fail("ADMIN", "203.0.113.50")
C("K1 tripped -> check() > 0 regardless of the password", L.check("ADMIN", "203.0.113.50") > 0, True)

# clears
L, clk = new()
for i in range(5):
    L.fail("A", "203.0.113.1")
L.clear_ip("203.0.113.1")
C("clear_ip clears K1", L.check(None, "203.0.113.1"), 0.0)
for i in range(30):
    L.fail("A", f"203.0.113.{i}")
L.clear_all()
C("clear_all clears every key and lock", (L.check("A", "203.0.113.200"), L.stats()),
  (0.0, {"k1": 0, "k2": 0, "k3": 0, "locks": 0, "known": 0}))

# key cap
L, clk = new()
real_max = ratelimit.MAX_KEYS
ratelimit.MAX_KEYS = 50
try:
    for i in range(200):
        clk.t += 0.01
        L.fail(f"U{i}", f"10.0.{i // 250}.{i % 250}")
    st = L.stats()
    C("at most MAX_KEYS keys (oldest evicted)", st["k1"] + st["k2"] + st["k3"] <= 50, True)
    C("the newest failure is still counted", L._k3.get("U199") is not None, True)
finally:
    ratelimit.MAX_KEYS = real_max

# sweep drops old windows
L, clk = new()
L.fail("S", "203.0.113.77")
clk.t += 901
L.sweep()
C("sweep drops expired windows", L.stats()["k1"] + L.stats()["k2"] + L.stats()["k3"], 0)

print("\nALL PASS" if _ok else "\nSOME FAILED")
raise SystemExit(0 if _ok else 1)
