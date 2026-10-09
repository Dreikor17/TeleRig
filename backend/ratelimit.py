"""Login rate limiting (spec §2.15, F7, AR-20): sliding 15-minute windows of failure times
(monotonic), three keys, checked BEFORE the password is verified.

  K1  per source          20 for "direct-local", 5 for a non-loopback IP. A loopback IP (a proxy
                          that adds no X-Forwarded-For) is NOT keyed: one noisy user behind it must
                          not lock out everyone else.
  K2  per (user, source)  10.
  K3  per user            30 failures from anywhere trip a user lock with exponential backoff
                          (15 -> 30 -> 60 -> 120 -> 240 min; the level decays after 24 h without a
                          trip). The lock is BYPASSED for direct-local requests and for a "known
                          clean" source: one with no failures in the window that this user signed
                          in from successfully within 30 days (kept in memory, <= 8 per user,
                          non-loopback IPs only).

The source `src` is "direct-local" when auth.direct_local(scope), else auth.client_ip(scope).
Success clears K2 for (user, src) and records src as known. A restart, an admin unlock or
`--reset-admin` (meta.limiter_reset) clears keys. Swept every 5 minutes (expired windows go).

Memory is bounded per table: at most MAX_KEYS keys in each of K1, K2 and K3. A flood of failed
logins for junk names / from many addresses must not be able to flush a real user's K3 count (and
so dodge the per-user lock), so the tables never evict each other's keys, and when a table is full:
  - K1 / K2 drop the key whose last failure is oldest (LRU);
  - K3 drops the key with the FEWEST failures in the window (ties: the oldest last failure). A user
    with c failures is dropped only when every other K3 key holds at least c, i.e. after about
    MAX_KEYS * c failed logins inside one window (scrypt-bound: far beyond reach), and a junk name
    can hold at most K3_LIMIT - 1 (its 30th failure trips its own lock and empties its window).
  - An active user lock (self._locks) is never evicted; only the sweep drops it, once it has
    expired and decayed.
Standard library only; thread-safe; no I/O.
"""
from __future__ import annotations

import ipaddress
import math
import threading
import time
from collections import OrderedDict, deque
from typing import Callable, Optional

WINDOW = 15 * 60.0
K1_LIMIT_IP = 5
K1_LIMIT_LOCAL = 20
K2_LIMIT = 10
K3_LIMIT = 30
BACKOFF_MIN = (15, 30, 60, 120, 240)
DECAY = 24 * 3600.0
KNOWN_TTL = 30 * 86400.0
KNOWN_MAX = 8
MAX_KEYS = 10000
SWEEP_EVERY = 300.0
DIRECT_LOCAL = "direct-local"


def _is_loopback_ip(src: str) -> bool:
    try:
        ip = ipaddress.ip_address(src)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def k1_applies(src: str) -> bool:
    return bool(src) and not _is_loopback_ip(src)


class Limiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time):
        self._clock, self._wall = clock, wall
        self._mu = threading.Lock()
        # each table is kept in last-failure order (oldest first): _push moves a key to the end
        self._k1: OrderedDict = OrderedDict()   # src -> deque[t]
        self._k2: OrderedDict = OrderedDict()   # (user, src) -> deque[t]
        self._k3: OrderedDict = OrderedDict()   # user -> deque[t]
        self._locks: dict = {}          # user -> {"until", "level", "last_trip"}
        self._known: dict = {}          # user -> OrderedDict[src -> t]
        self._last_sweep = clock()

    # -- internals (hold self._mu) -----------------------------------------------------------
    def _window(self, table: dict, key) -> deque:
        dq = table.get(key)
        if dq is None:
            return deque()
        cut = self._clock() - WINDOW
        while dq and dq[0] <= cut:
            dq.popleft()
        return dq

    def _retry(self, dq: deque, limit: int) -> float:
        """Seconds until the window holds fewer than `limit` failures (0 if it already does)."""
        if len(dq) < limit:
            return 0.0
        t = dq[len(dq) - limit]
        return max(0.0, t + WINDOW - self._clock())

    def _push(self, table: OrderedDict, key) -> deque:
        dq = table.get(key)
        if dq is None:
            self._make_room(table)               # make room BEFORE the new key exists
            dq = table[key] = deque()
        else:
            table.move_to_end(key)               # last-failure order
        dq.append(self._clock())
        return dq

    def _make_room(self, table: OrderedDict) -> None:
        """At most MAX_KEYS keys in THIS table; the other tables are never touched (a flood of K1 /
        K2 keys can't push a K3 count out). K1 / K2: the oldest last failure goes. K3: the fewest
        failures in the window goes (ties: the oldest), so a real user's count survives a flood of
        junk names."""
        if len(table) < MAX_KEYS:
            return
        cut = self._clock() - WINDOW
        while table:                             # expired windows first: they sit at the front
            k, dq = next(iter(table.items()))
            if dq and dq[-1] > cut:
                break
            del table[k]
        while len(table) >= MAX_KEYS:
            victim = self._k3_victim() if table is self._k3 else next(iter(table))
            del table[victim]

    def _k3_victim(self):
        cut = self._clock() - WINDOW
        best, best_n = None, math.inf
        for k, dq in self._k3.items():           # oldest last failure first, so ties go to the oldest
            while dq and dq[0] <= cut:
                dq.popleft()
            if len(dq) < best_n:
                best, best_n = k, len(dq)
                if best_n == 0:
                    break
        return best

    def _sweep_locked(self) -> None:
        cut = self._clock() - WINDOW
        for tab in (self._k1, self._k2, self._k3):
            for k in [k for k, dq in tab.items() if not dq or dq[-1] <= cut]:
                del tab[k]
        now = self._clock()
        for u in [u for u, lk in self._locks.items()
                  if lk["until"] <= now and now - lk["last_trip"] > DECAY]:
            del self._locks[u]
        for u, od in list(self._known.items()):
            for s in [s for s, t in od.items() if now - t > KNOWN_TTL]:
                del od[s]
            if not od:
                del self._known[u]
        self._last_sweep = now

    def _known_clean(self, user: str, src: str) -> bool:
        od = self._known.get(user)
        if not od or src not in od or self._clock() - od[src] > KNOWN_TTL:
            return False
        return not self._window(self._k1, src) and not self._window(self._k2, (user, src))

    # -- API ---------------------------------------------------------------------------------
    def check(self, user: Optional[str], src: str, *, direct_local: bool = False) -> float:
        """0 when this attempt may be verified, else seconds to wait (429 retry_after)."""
        with self._mu:
            if self._clock() - self._last_sweep >= SWEEP_EVERY:
                self._sweep_locked()
            wait = 0.0
            if k1_applies(src):
                limit = K1_LIMIT_LOCAL if src == DIRECT_LOCAL else K1_LIMIT_IP
                wait = max(wait, self._retry(self._window(self._k1, src), limit))
            if user:
                wait = max(wait, self._retry(self._window(self._k2, (user, src)), K2_LIMIT))
                lk = self._locks.get(user)
                if lk and lk["until"] > self._clock() and not direct_local \
                        and not self._known_clean(user, src):
                    wait = max(wait, lk["until"] - self._clock())
            return wait

    def fail(self, user: Optional[str], src: str, *, k1: bool = True, k2: bool = True,
             k3: bool = True) -> bool:
        """Record one failure. Returns True when this failure tripped the K3 user lock."""
        with self._mu:
            if k1 and k1_applies(src):
                self._push(self._k1, src)
            if not user:
                return False
            if k2:
                self._push(self._k2, (user, src))
            if not k3:
                return False
            dq = self._push(self._k3, user)
            self._window(self._k3, user)
            now = self._clock()
            lk = self._locks.get(user)
            if lk and lk["until"] > now:
                return False
            if len(dq) >= K3_LIMIT:
                if lk is None or now - lk["last_trip"] > DECAY:
                    level = 0
                else:
                    level = min(lk["level"] + 1, len(BACKOFF_MIN) - 1)
                self._locks[user] = {"until": now + BACKOFF_MIN[level] * 60.0, "level": level,
                                     "last_trip": now}
                dq.clear()
                return True
            return False

    def success(self, user: str, src: str) -> None:
        with self._mu:
            self._k2.pop((user, src), None)
            if src != DIRECT_LOCAL and k1_applies(src):
                od = self._known.setdefault(user, OrderedDict())
                od.pop(src, None)
                od[src] = self._clock()
                while len(od) > KNOWN_MAX:
                    od.popitem(last=False)

    def clear_user(self, user: str) -> None:
        """Admin unlock / account change: K2 and K3 keys and the lock (known sources are kept)."""
        with self._mu:
            for k in [k for k in self._k2 if k[0] == user]:
                del self._k2[k]
            self._k3.pop(user, None)
            self._locks.pop(user, None)

    def clear_ip(self, src: str) -> None:
        with self._mu:
            self._k1.pop(src, None)

    def clear_all(self) -> None:
        with self._mu:
            self._k1.clear()
            self._k2.clear()
            self._k3.clear()
            self._locks.clear()
            self._known.clear()

    def sweep(self) -> None:
        with self._mu:
            self._sweep_locked()

    def locked_until(self, user: str) -> Optional[float]:
        """Wall-clock epoch seconds until which the user is locked, or None."""
        with self._mu:
            lk = self._locks.get(user)
            now = self._clock()
            if not lk or lk["until"] <= now:
                return None
            return self._wall() + (lk["until"] - now)

    def lockouts(self) -> dict:
        """{users: [{key, count, until, level}], ips: [{key, count, until}]} (until = epoch s)."""
        with self._mu:
            now, wall = self._clock(), self._wall()
            users = []
            for u, lk in sorted(self._locks.items()):
                if lk["until"] > now:
                    users.append({"key": u, "count": len(self._window(self._k3, u)),
                                  "until": wall + lk["until"] - now, "level": lk["level"]})
            ips = []
            for src in sorted(self._k1):
                dq = self._window(self._k1, src)
                limit = K1_LIMIT_LOCAL if src == DIRECT_LOCAL else K1_LIMIT_IP
                if len(dq) >= limit:
                    ips.append({"key": src, "count": len(dq), "until": wall + self._retry(dq, limit)})
            return {"users": users, "ips": ips}

    def stats(self) -> dict:
        with self._mu:
            return {"k1": len(self._k1), "k2": len(self._k2), "k3": len(self._k3),
                    "locks": len(self._locks), "known": sum(len(v) for v in self._known.values())}


limiter = Limiter()          # the server's instance (in memory; a restart clears it, R17)
