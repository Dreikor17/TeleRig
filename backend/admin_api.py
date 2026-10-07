"""Admin API (spec §2.17). Step A3 adds only the read-only `GET /api/admin/users` (admin), so the
F17 rule "a migrated ADMIN off the host PC is an operator" can be verified against a real admin
route (T54). Step A5 adds the rest: reauth, user create/patch/reset/signout/unlock/delete,
sessions, lockouts, allowed hosts, audio and radio pins, Tailscale (moved from server.py), audit.
"""
from __future__ import annotations

from typing import Callable

from starlette.responses import JSONResponse

from . import accounts
from .policy import requires
from .ratelimit import limiter

# server.py binds these (it owns the Hub and the TX state).
online_count: Callable[[str], int] = lambda user: 0
on_air: Callable[[str], bool] = lambda user: False


@requires("admin")
async def api_users(request):
    p = request.state.principal
    out = []
    for name, u in sorted(accounts.users().items()):
        out.append({"uid": accounts.uid(name), "user": name, "role": u.role,
                    "must_change": u.must_change, "default_pw": u.default_pw,
                    "migrated_v1": u.migrated_v1, "created": u.created, "created_by": u.created_by,
                    "pw_changed": u.pw_changed, "last_login": u.last_login,
                    "locked_until": limiter.locked_until(name), "online": online_count(name),
                    "on_air": on_air(name)})
    return JSONResponse({"me": p.user, "users": out}, headers={"Cache-Control": "no-store"})
