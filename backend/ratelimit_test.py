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

# key cap: at most MAX_KEYS keys per table
L, clk = new()
real_max = ratelimit.MAX_KEYS
ratelimit.MAX_KEYS = 50
try:
    for i in range(200):
        clk.t += 0.01
        L.fail(f"U{i}", f"10.0.{i // 250}.{i % 250}")
    st = L.stats()
    C("at most MAX_KEYS keys in each table", (st["k1"] <= 50, st["k2"] <= 50, st["k3"] <= 50), (True, True, True))
    C("the newest failure is still counted", L._k3.get("U199") is not None, True)
    L2, clk2 = new()
    L2.fail(None, "198.51.100.1")                                 # K1: refreshed later -> kept
    L2.fail(None, "198.51.100.2")                                 # K1: the oldest -> evicted first
    clk2.t += 1
    L2.fail(None, "198.51.100.1")
    for i in range(49):
        clk2.t += 0.01
        L2.fail(None, f"203.0.113.{i}")
    C("K1 / K2 evict the oldest LAST failure (LRU)", ("198.51.100.1" in L2._k1, "198.51.100.2" in L2._k1), (True, False))

    # round 3: a flood of junk names can't flush a real user's K3 count (the per-user lock still trips)
    L, clk = new()
    for i in range(29):                                           # 29 wrong passwords for A2, from 29 sources
        clk.t += 0.1
        L.fail("A2", f"203.0.113.{i + 1}")
    for n in range(10 * 50):                                      # junk names from many sources: 10x the cap
        clk.t += 0.01
        L.fail(f"JUNK{n}", f"2001:db8::{n + 1:x}")
    C("flood: A2's K3 count survived 10x MAX_KEYS junk failures", len(L._k3.get("A2") or ()), 29)
    C("flood: the K3 table stayed bounded", len(L._k3) <= 50, True)
    C("flood: the 30th A2 failure trips the lock", L.fail("A2", "203.0.114.1"), True)
    C("flood: a fresh source for A2 is refused", L.check("A2", "203.0.114.99") > 0, True)
    for n in range(10 * 50):                                      # another flood while A2 is locked
        clk.t += 0.01
        L.fail(f"JUNK2-{n}", f"2001:db8:1::{n + 1:x}")
    C("flood: an active lock is never evicted", (L.check("A2", "203.0.114.98") > 0,
                                                  [u["key"] for u in L.lockouts()["users"]]), (True, ["A2"]))

    # fewest failures first: a key with c failures goes only when every other K3 key holds >= c
    L, clk = new()
    for i in range(3):
        L.fail("REAL", f"198.18.0.{i}")                          # REAL: 3 failures, the OLDEST key
    for n in range(49):
        clk.t += 0.01
        for j in range(2):
            L.fail(f"J{n}", f"198.18.{n + 1}.{j}")               # 49 junk keys with 2 failures each
    L.fail("NEW", "198.18.200.1")                                 # the table is full: one key must go
    C("K3 evicts a 2-failure junk key, never the older 3-failure key", ("REAL" in L._k3, len(L._k3)), (True, 50))
    C("... and the newest key is kept", "NEW" in L._k3, True)
    L, clk = new()
    L.fail("OLD1", "198.18.0.1")
    for n in range(49):
        clk.t += 0.01
        L.fail(f"K{n}", f"198.18.1.{n}")
    clk.t += 0.01
    L.fail("NEWEST", "198.18.2.1")
    C("K3 ties (one failure each): the oldest last failure goes", ("OLD1" in L._k3, "K0" in L._k3), (False, True))
    L, clk = new()
    L.fail("STALE", "198.18.3.1")
    clk.t += 901                                                  # STALE's window expired
    for n in range(49):
        clk.t += 0.01
        L.fail(f"S{n}", f"198.18.4.{n}")
    L.fail("ONE-MORE", "198.18.5.1")
    C("an expired K3 window goes first (the sweep)", ("STALE" in L._k3, len(L._k3)), (False, 50))
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
