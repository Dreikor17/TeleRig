# Sign-in and remote access

TeleRig controls a real transmitter, so **everyone signs in** — on the host PC too — and every
account has a role. The network is still the outer boundary: for use away from home the
recommended path is **Tailscale Serve** (tailnet-only HTTPS), which an admin turns on from the app.

| Role | Can |
|---|---|
| **viewer** | watch and listen: the panel, scope, meters, RX audio, the SET-menu values (read only) |
| **operator** | everything a viewer can, plus operate and **transmit** (PTT, VOX, TUNE, CW), non-critical menu writes, reconnect the current radio on its pinned connection, disconnect |
| **admin** | everything an operator can, plus users, allowed names/origins, remote access, radio and audio pins, critical menu writes, the activity log and lockouts (Administration) |

New accounts are **viewers** unless the admin picks another role. One connection at a time owns the
transmitter; the others see who is on the air (ON AIR chip) and get "busy" until they finish. Any
operator can stop someone else's transmission with the two-tap **STOP** (tap, then tap again within
3 s); that is recorded in the activity log.

## First sign-in (on the radio PC)

A new install has one account, **admin** with the password **admin**. That default password works
**only on the host PC itself** (`http://localhost:<port>` or `127.0.0.1`, not by name and not through a proxy) and
**only with the one-time setup code** the launcher prints:

```
TeleRig -> http://localhost:8700
  Login required for everyone.
  First run: sign in on THIS PC as admin / admin with setup code K7QM-4XPA-9TRB — the browser
  opens http://localhost:8700/login#setup=K7QM-4XPA-9TRB for you. You will choose a new password.
```

The launcher opens that address, so the sign-in page already has the code: type `admin` / `admin`,
then choose your own password (8–72 characters; not the user name, not a common password). The code
is cleared once the default password is gone.

- From **any other device** the default password is refused exactly like any other wrong password
  (it also counts toward the lockout), so nobody on the network can claim a fresh install.
- On the host PC without the code you get "Enter the setup code shown in the TeleRig window on this
  PC." The sign-in page shows a setup-code field.
- Headless or no browser on the host: set the password from a console with the recovery command
  below instead.

Then open **Administration** (the shield in the top bar, or the user menu) → **Users** and add an
account for each operator, by callsign (2–32 letters, digits, `/`, `_`, `-`; shown in capitals).
TeleRig generates a temporary password, **shown once** — pass it on; the user picks their own at
their first sign-in (or type one yourself and untick "Require a new password").

## Recovery: `--reset-admin`

Forgot the admin password, locked yourself out, or the account file is damaged? On the host PC run

| Windows | elsewhere |
|---|---|
| `run.bat --reset-admin` | `python3 run.py --reset-admin` |

(`run.bat` uses the app's own `.venv`; the long form is `.venv\Scripts\python.exe run.py --reset-admin`.
A plain `python run.py` usually lacks the dependencies on Windows.) It asks for a new password for
**ADMIN** twice and then:

- sets it (no forced change), clears the setup code and the "migrated password" flag;
- **clears every lockout** (all sources, all accounts);
- lists any other admin accounts and offers to demote them to operator (`--demote-other-admins`
  does all of them);
- if `auth.json` is corrupt, offers to move it aside and start a fresh account store; if
  `config.json` can't be parsed, offers to move it aside too;
- works while the server runs: ADMIN's open sessions end and the change applies within about a second.

It never starts the server. `--password-stdin` (one line on stdin, for scripts) is refused unless
`TELERIG_CONFIG` is set or you add `--real-config`.

## Sessions

- A sign-in lasts **30 days** on that browser (a fixed expiry, not renewed by use).
- **Sign out** (user menu) ends this browser's session; the token is revoked on the server.
  **Sign out everywhere** ends every session of your account, on every device.
- An admin can reset a user's password, change their role, sign them out everywhere or delete the
  account; that user's open pages end at once. The sign-in page then says why:
  - `signed_out` — "Signed out." (you, from this or another tab);
  - `session_revoked` — "Your session was ended by an administrator." (a password change or reset,
    sign out everywhere, a deleted account, `--reset-admin`);
  - `expired` — "Your session expired." (30 days, or the session was already gone);
  - `role_changed` — "Your role changed — sign in again." (an admin changed your role).
- Changing your own password keeps this tab signed in and signs out your other devices.
- **Per-user limit:** each account may have **8** open TeleRig pages/connections at once (**24** for
  a migrated shared admin account, below); the server takes at most 32, of which viewers may use 24,
  so operators and admins always get in. Over the limit the page says "Too many open TeleRig tabs or
  viewers — close one" and retries every 10 s.
- Admin changes (users, access lists, turning Serve on, radio/audio pins) need a recent password
  check: 10 minutes after the last one the app asks "Confirm your password to continue" and retries.

## Lockouts

Failed sign-ins are counted over a 15-minute window:

| Key | Limit |
|---|---|
| per source address | 5 failures (20 from the host PC itself; not counted for a proxy's 127.0.0.1) |
| per account + source | 10 failures |
| per account, all sources | 30 failures, then the account is locked for 15 min, doubling up to 4 h on repeats (forgotten after 24 h without one) |

A locked source or account gets "Too many attempts. Try again in m:ss", even with the right
password. The account-wide lock never applies **on the host PC**, nor from a device that signed in
to that account successfully in the last 30 days and has no recent failures, so an attacker can't
keep you locked out. Admins see a notice in the top bar and the list in **Administration →
Lockouts**, with **Unlock** buttons. Ways back in:

1. sign in on the host PC, or from a device you used before;
2. ask an admin to unlock you (Lockouts, or the Users row);
3. run the recovery command (it clears every lockout) — or restart the server, which forgets them.

## Upgrading from Radio WebOp (v0.2.x)

- **Config folder.** TeleRig keeps its settings in `~/.telerig` (on Windows
  `C:\Users\<you>\.telerig`). On the first start it **copies** `~/.radio-webop` there once if that
  exists and `~/.telerig` doesn't; the old folder is left as it was, so an older version still runs
  from it. `TELERIG_CONFIG` (or the old `RADIO_WEBOP_CONFIG`) overrides the location (it names the
  `config.json` inside the folder).
- **Everyone signs in again.** Old browser sessions (`tr_session` / `rw_session` cookies) are not
  accepted. The v1 "no password" mode and `/api/set_password` are gone.
- **No password set before** (the default): you get the fresh-install admin / admin + setup code above.
- **A shared remote-access password was set before (migrated password).** It becomes the **admin**
  account's password, unchanged — sign in as `admin` with it. Until you change it, that password
  is treated like v1 treated it:
  - **on the host PC** it has full admin rights;
  - **from any other device** it only has **operator** rights (operate and transmit, as before),
    and it can't change the admin password or "sign out everywhere" (the app says "Change the
    shared admin password on the host PC itself, or run run.bat --reset-admin").

  An amber notice reminds admins. Change it on the host PC (user menu → Change password), or run the
  recovery command; then create individual accounts so the old shared password can retire. Until
  then the shared admin account may have 24 pages open at once.
- **Tailscale Serve was on (`behind_proxy`) and no password was set.** At start-up TeleRig clears a
  stale `behind_proxy` flag when Tailscale says Serve isn't running for this port. If Serve really
  runs, every visitor — the host PC included — arrives through the proxy, so nobody counts as "on the
  host PC" and the default password can't be used: run the recovery command once to set the admin
  password. The same goes for a migrated password while Serve runs: it only has operator rights
  there, so run the recovery command once to get admin access. (The launcher says so in both cases.)
- **Public host names.** With an empty allowed-names list only local-style names are accepted (see
  below). If you reached v1 by a public DNS name that isn't in the list, the page now answers
  "Bad Host header": open TeleRig on the host PC (`http://localhost:<port>`) → Administration →
  Access and add the name.
- **Downgrading** to v0.2.25 or older restores v1 behaviour: the old version reads its own
  `~/.radio-webop` folder (left as it was), and the v1 keys in the copied `config.json` are never
  deleted (a copy is also in `config.v1.bak.json`).

## Remote access with Tailscale Serve (recommended)

[Tailscale Serve](https://tailscale.com/kb/1312/serve) puts TeleRig behind a tailnet-only HTTPS
proxy with an automatic `*.ts.net` certificate. HTTPS is also what the browser needs for the
**microphone** (TX audio): `getUserMedia` only works over HTTPS or on `localhost`.

**From the app (admin):** Administration → **Remote**. It detects Tailscale and shows the state.

- **Turn on** runs `tailscale serve --bg <port>` for you. It is offered only **on the host PC
  itself** (opened as `http://localhost:<port>`) and only to an admin with a recent password check.
  When Tailscale confirms Serve is running, TeleRig marks itself `behind_proxy` and adds your
  `https://<name>.ts.net` address to the allowed origins. The page shows that address with a
  Copy button.
- **Turn off** (any admin) runs `tailscale serve reset`; `behind_proxy` is cleared only when
  Tailscale confirms Serve is off. A flag left behind with Serve off shows a **Clear flag** button.
  A flag you set by hand with `"behind_proxy_source": "manual"` (see
  [Reverse proxies and tunnels](#reverse-proxies-and-tunnels-loopback-isnt-the-host-pc)) is never
  called stale and is never cleared by TeleRig — not at start-up, not by **Turn off**, not by
  **Clear flag**.

**By hand (the same thing):**

1. Install Tailscale on the host PC and each client; sign them into the **same tailnet**.
2. In the Tailscale admin console enable **MagicDNS** and **HTTPS Certificates** (once per tailnet).
3. Run TeleRig on loopback only — no firewall holes:
   ```
   run.bat --host 127.0.0.1 --no-browser            (Windows)
   python3 run.py --host 127.0.0.1 --no-browser     (elsewhere)
   ```
4. `tailscale serve --bg 8700` (persists across reboots).
5. Open the URL `tailscale serve status` prints, e.g. `https://radio-pc.tailNNNN.ts.net/`, from any
   tailnet device, and sign in.

With Serve turned on from the app, TeleRig is marked `behind_proxy`, so no Serve visitor counts as
"on the host PC".
(Serve normally adds `X-Forwarded-For`; when it does, the lockout keys and the activity log show each
visitor's tailnet address instead of 127.0.0.1.) If you set Serve up by hand, also put
`"behind_proxy": true, "behind_proxy_source": "tailscale"` in `config.json` (TeleRig then clears it
by itself at a start when Serve is no longer running) and add the `https://<name>.ts.net` origin
under Access — or let **Turn on** do both. To stop: `tailscale serve reset`, or the targeted
`tailscale serve --https=443 8700 off`.

⚠️ **Bind the app to `127.0.0.1` when fronting it with Serve.** The default `0.0.0.0` *also*
listens in plain HTTP on the LAN and the raw Tailscale IP (`http://<ip>:8700`), where passwords
cross the network unencrypted and which bypasses the proxy's TLS.

### Do NOT use Tailscale Funnel

`tailscale funnel` exposes the service to the **public internet**. A sign-in page in front of a
transmitter is not a reason to invite the whole internet to guess passwords. Never funnel TeleRig;
Serve (tailnet-only) is the right tool.

## Allowed names and allowed origins (Administration → Access)

Two separate checks protect against other web pages driving your radio (DNS rebinding and cross-site
requests). The list in Access holds two kinds of entry:

- **Allowed names** (`radio-pc`, `shack.example.org`) — host names the browser may use to reach
  TeleRig (the **Host check**).
- **Allowed origins** (`https://shack.example.org`, `https://radio-pc.tailnet.ts.net:443`) — a full
  `scheme://name[:port]` that may send commands (the **Origin check**). An origin also allows its name.

`localhost`, `*.localhost` and IP addresses always pass the Host check and can't be listed.

**Host check.** With an **empty list**, TeleRig accepts `localhost`, IP addresses and *local-style*
names only: single-label names (`radio-pc`), `*.local`, `*.lan`, `*.home.arpa`, `*.internal` and
`*.ts.net`. A public name (`shack.example.org`) must be added first; the "Bad Host header" page says
so. Once the list has entries, only those names (plus `localhost` and IPs) are accepted.

**Origin check (the exact-origin rule).** Every WebSocket and every request that changes something
must carry an `Origin` whose scheme, **name and port** match the address the browser used, or that
equals an allowed **origin** exactly. A bare allowed *name* never relaxes this: a page served from
the same host on another port (another web app on `:8077` or `:8443`) can't drive TeleRig. Command-line
tools must send `Origin: http://<host>:<port>` themselves.

**Behind a reverse proxy.** When the proxy rewrites `Host` (nginx `proxy_set_header Host $host` drops
a non-default port), TeleRig compares the Origin with `X-Forwarded-Host` / `X-Forwarded-Port` — only
when the proxy connects from the same machine (a loopback peer). Otherwise add the public origin
(e.g. `https://shack.example.org`) under Access. The refusal says which origin and which host didn't
match.

**Saving can't lock you out:** Access refuses a list that would no longer accept the browser you are
saving from ("Saving this list would lock this browser out …"); add this browser's address first.

## Reverse proxies and tunnels: loopback isn't the host PC

TeleRig treats a request as "on the host PC" (*direct-local*) only when it comes from `127.0.0.1`/`::1`,
addressed as `localhost` (or `127.0.0.1` / `[::1]`), with **no** forwarding headers, and `behind_proxy`
is off. That decides the
default-password setup, **Turn on** for Serve, the migrated-password admin rights and the lockout source.

**Any relay that ends on 127.0.0.1 without adding a forwarding header defeats that heuristic:** a stock
nginx `proxy_pass` with Host rewritten to `localhost`, `ssh -R`, `ngrok tcp`, `tailscale serve --tcp`,
`socat`, `netsh interface portproxy`. Remote visitors then look exactly like the host PC. It is never
enough on its own — the default password still needs the setup code, and the lockout still caps
guesses — but don't rely on it:

- front TeleRig only with **Tailscale Serve** (HTTPS) or a proxy that sets **`X-Forwarded-For`**
  (and `X-Forwarded-Host` / `X-Forwarded-Proto`), and
- for anything else, set `"behind_proxy": true, "behind_proxy_source": "manual"` in `config.json`
  (in the config folder above), so no visitor is ever treated as the host PC. Use the recovery
  command for admin tasks that need the host PC. TeleRig never changes a `manual` flag (Remote shows
  it as "set by hand in config.json", with no stale warning and no **Clear flag**); to undo it,
  edit `config.json` again.

When a sign-in on the host PC is refused as "not on this PC", the sign-in page's "Trouble signing in
on this PC?" note names the reason: the server is marked behind a proxy, a forwarding header was
present, or the page was opened by name instead of `http://localhost:<port>`.

## Troubleshooting "can't connect over MagicDNS"

With `run.bat` / `python3 run.py` bound to `0.0.0.0` (the default), a refused or blocked MagicDNS
connection — when you're *not* using Serve — is almost always one of these:

1. **Browser forced `https://` against a plain-HTTP server.** `https://radio-pc...:8700` on a plain
   HTTP instance makes uvicorn log "Invalid HTTP request received" and the page fails. Use Tailscale
   Serve (HTTPS at the proxy, HTTP to the app) or run TeleRig with `--ssl-certfile/--ssl-keyfile`.
   Match the scheme to how the server runs.
2. **Windows Firewall blocking the port / python.exe.** The first run usually triggers a Windows
   Defender Firewall prompt for Python; if it was dismissed, inbound 8700 is blocked on the
   Tailscale interface. Serve avoids this (the app listens on loopback; the proxy is the Tailscale
   daemon). For direct access:

   ```
   netsh advfirewall firewall add rule name="TeleRig 8700" ^
     dir=in action=allow protocol=TCP localport=8700
   ```

3. **HTTPS-cert expectation on the MagicDNS name.** `https://host` (no port) works only with HTTPS
   Certificates + MagicDNS and Serve (or a real certificate). `https://host:8700` does not get a
   certificate by magic; it reaches plain HTTP.
4. **"Bad Host header".** The name you used is not local-style and not in Administration → Access
   (see above). Add it from the host PC.
5. **Hitting the wrong thing on loopback.** A `127.0.0.1`-only process is not reachable over MagicDNS
   by design. With Serve that's exactly what you want — the app stays on loopback and the proxy
   bridges the tailnet.
