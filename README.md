# TeleRig

*Formerly **Radio WebOp**.*

A browser-based control panel for **Icom CI-V** and **Yaesu CAT** transceivers,
with a live spectrum scope + waterfall. One app, multiple radios — pick your model
from a dropdown and the bands, modes, steps and protocol details all follow.
Currently ships profiles for the **IC-9700**, **IC-7300MK2**, **IC-728**, **Yaesu FT-991A**
and **FT-891**; adding a radio is just a new profile.

It includes a built-in **simulator**, so the whole interface and waterfall run
with no radio attached. Connect over **USB** (CI-V or CAT) or **LAN** (Icom
RS-BA1) when you're ready. Everyone **signs in** — viewers, operators and admins —
and only one operator transmits at a time.

> This repo was previously two apps (`icom-webop` for the IC-9700 and
> `ic7300mk2-webop`). They were merged into **Radio WebOp**, since renamed **TeleRig**.

## Features

- **Multi-radio** — choose the model; bands, modes, steps and the control details
  come from that radio's profile (`backend/profiles.py`). The UI adapts, showing
  only the controls a rig actually supports.
- **Live bandscope + waterfall** — decoded from the Icom CI-V `27 00` scope (475
  points), with the tuned-frequency marker and the receive passband shaded over it.
  Radios with no scope over the control link (the Yaesu CATs) get an **audio (AF)
  spectrum** scope built from their RX audio instead.
- **Tuning** — main dial, **tap-to-select and drag/slide on the waterfall**,
  mouse-wheel, selectable step, and direct frequency entry.
- **Control** — band, mode, filter, VFO A/B / A=B / swap, RX DSP (NB / NR / notch)
  and TX (mic / COMP / VOX / monitor / RIT / split), where the radio supports them.
- **Full SET-menu tab** — the radio's own menu, read and written live over the
  control link (Icom `1A 05`, Yaesu `EX`); connection/transmit-sensitive items are
  admin-only and confirm-gated before writing (viewers can read the values).
- **Audio** — RX in the browser and mic TX (16 kHz mono), either over **LAN**
  (RS-BA1, for networked Icoms — MOD Input auto-routed on connect and restored on
  disconnect) or via the **host PC's sound card** for USB/serial radios (e.g. an
  FT-891 on a Digirig).
- **PTT** tap-to-toggle (a short hold on touch screens) with a **failsafe time-out** — a
  client countdown, an independent server-side auto-unkey and a 120 s session limit, plus
  release on screen-lock / disconnect. A hardware TX time-out is set on connect as a backstop.
- **One transmitter, many users** — accounts with roles (viewer / operator / admin); one
  connection owns TX at a time, the others see **ON AIR · *callsign*** and get "busy", and any
  operator can stop a transmission with a two-tap **STOP**. Every TX session is logged.
- **Levels** — AF, RF, SQL, RF power and mic gain; a real-time S-meter and TX
  meters. Power and levels are **read from the rig on connect**, so the panel
  mirrors the radio rather than overriding it.
- **Overlay tools** — an ARRL/FCC **band-plan overlay** and a neural **CW
  decoder/encoder** over the waterfall.
- **Modular panels** — VFO, Transmit, Audio, Meter, Scope, Controls and CW are cards you can
  move, resize, collapse, hide and add back (**Arrange** mode); the layout is saved per user,
  separately for desktop and phone.
- **Mobile-friendly** responsive layout; remembers the radio + connection per
  radio (transport, ports, and audio devices).

## Transports

- **USB** — a COM port, speaking Icom **CI-V** or Yaesu **CAT**.
- **LAN (network)** — Icom's RS-BA1 UDP protocol (control 50001 / CI-V 50002 /
  audio 50003), for radios with an Ethernet port (IC-9700, IC-7300MK2). The Yaesu
  radios are COM-only.
- **Simulator** — auto-connects on load for operators (until you pick a real radio + transport).

## Install & run (Windows)

**One-click:**

1. Double-click **`install.bat`** — it checks for **Python** (installs 3.12 via `winget`
   if you don't have it), creates a local environment, installs everything, and adds a
   **"TeleRig" desktop shortcut**. Re-run it anytime to **update**.
2. Double-click the **desktop shortcut** (or **`run.bat`**) to start it — your browser
   opens at <http://localhost:8700>. On the first run it opens the sign-in page with a
   one-time setup code; see [Sign-in & users](#sign-in--users).

No Python or command line needed. `install.bat` is safe to run again; on a git checkout it
also pulls the latest code before updating dependencies.

<details><summary>Prefer the command line?</summary>

```
pip install -r requirements.txt
python3 run.py
```
</details>

Sign in, pick your **radio model**, choose a transport (Simulator / a COM port / LAN), and Connect.

- **USB:** pick the COM port + baud and Connect (use the **?** next to the model
  for that radio's exact radio-side settings — CI-V address / CAT rate, etc.).
- **LAN:** on the radio set Network Function ON + a network user/password, leave
  it on (or in networked standby), then choose LAN, enter the IP / user /
  password, Connect.
- **Remote/mobile:** binds `0.0.0.0` by default, so it's reachable over your LAN, a
  VPN, or a port-forward. RX and control work over plain HTTP; only the **browser
  mic** (for TX / "Mic In") needs a secure context (HTTPS). For HTTPS, either put it
  behind a reverse proxy / TLS tunnel, or serve TLS **directly on its own port**:

  ```
  python run.py --ssl-certfile cert.pem --ssl-keyfile key.pem
  ```

  (or set `TELERIG_SSL_CERT` / `TELERIG_SSL_KEY`; the older `RADIO_WEBOP_SSL_*` names still work). Then open the `https://`
  address. The WebSocket follows the page scheme automatically (`ws://` / `wss://`).
  Note: HTTPS sent to a plain-HTTP instance is rejected as an invalid request — match
  the scheme to how the server is running.

  **Recommended for remote use: Tailscale Serve.** An admin can turn it on from
  **Administration → Remote** (on the host PC itself): it detects Tailscale and runs
  `tailscale serve` for you. The safest easy path is to keep the app on loopback
  (`--host 127.0.0.1`) and front it with that tailnet-only HTTPS reverse proxy, which
  auto-provisions a `*.ts.net` certificate (and so satisfies the mic/TX secure-context
  requirement). By hand:

  ```
  run.bat --host 127.0.0.1 --no-browser        (python3 run.py ... elsewhere)
  tailscale serve --bg 8700
  ```

  Then open the printed `https://<host>.ts.net/` URL from any tailnet device and sign in —
  no firewall holes, no self-signed cert. Do **not** use `tailscale funnel` (which
  exposes it to the public internet). See [docs/REMOTE-ACCESS.md](docs/REMOTE-ACCESS.md).

## Sign-in & users

Everyone signs in, on the radio PC too. Each account has a **role**:

- **viewer** — watch and listen (panel, scope, meters, RX audio, menu values); can't change the radio;
- **operator** — operate and **transmit**, connect/disconnect the current radio;
- **admin** — operator + users, access lists, remote access, radio/audio pins, critical menu
  settings, the activity log and lockouts.

**First sign-in, on the radio PC.** A new install has one account, `admin` / `admin`, which works
only on the host PC itself (`http://localhost:8700`) together with the one-time **setup code** the
TeleRig window prints; the launcher opens the sign-in page with the code filled in, and you choose
a new password right away. From other devices the default password never works. Forgot the admin
password or locked out? On the host PC run `run.bat --reset-admin` (Windows) or
`python3 run.py --reset-admin` (elsewhere). Upgrading from Radio WebOp, an old shared remote-access
password becomes the admin password — with admin rights only on the host PC until you change it
there. Details: [docs/REMOTE-ACCESS.md](docs/REMOTE-ACCESS.md).

**Arrange mode.** The grid icon in the top bar (on a phone: the user menu → *Arrange panels*)
unlocks the layout: drag a card by its header, resize it from its edges, use the arrow keys
(Shift = resize), **Add panel** brings a hidden card back and **Reset** returns to the default (with
Undo). Press Esc or *Done* to finish. Collapsing a card (its chevron) works any time. Your layout is
stored on the server for your account, one for desktop and one for phone.

**Administration** (the shield in the top bar, or the user menu; admins only): **Users** (add by
callsign, role, temporary password shown once, reset password, sign out everywhere, delete),
**Access** (allowed host names and origins), **Remote** (Tailscale Serve), **Radio & audio** (the
radio's pinned connection and the host sound cards), **Activity** (who is connected, who
transmitted, the security log, download) and **Lockouts**. The user menu also has *Change password*,
*Sign out* and *Sign out everywhere*.

## Architecture

```
backend/
  profiles.py     RadioProfile registry — per-model bands, modes, filters,
                  capabilities + SET-menu table (add a radio here)
  civ.py          Icom CI-V protocol: framing, BCD freq/levels, modes, 27 00 scope
  radio.py        Icom controller: live state, scope, audio, actions
  yaesu.py        Yaesu CAT controller (same surface; COM-only, AF scope)
  transport.py    SerialTransport (USB) + profile-driven simulators
  lan.py          LanTransport — Icom RS-BA1 UDP (control/serial/audio + login)
  hostaudio.py    host sound-card capture/playback for USB/serial radios
  menu_engine.py  SET-menu codec (Icom 1A 05 / Yaesu EX); menus/<id>_menu.py tables
  accounts.py     user accounts (auth.json): scrypt passwords, tokens, migration, --reset-admin
  auth.py         who is asking: session cookie, Host / Origin checks, host-PC detection, roles
  policy.py       what each role may do: the WebSocket action table and route rules
  txarb.py        the TX arbiter: one owner, heartbeats, the 120 s session clock, STOP
  audio_router.py RX/TX audio routing (only the TX owner's mic reaches the radio)
  account_api.py  sign-in, password change, sign-out, per-user layouts
  admin_api.py    Administration: users, access lists, remote, pins, activity, lockouts
  hub.py, store.py, settings.py, layout_store.py, audit.py, ratelimit.py
                  connections, atomic JSON stores, config.json, layouts, audit logs, lockouts
  tailscale.py    Tailscale Serve helper for Administration → Remote
  server.py       Starlette/uvicorn — /api/radios, connect/ports, WebSocket
frontend/         adaptive multi-radio panel; controls render from the profile
                  (tr.js session layer, layout.js panel grid, account.js / admin.js dialogs,
                  login.html / change-password.html)
```

See [ROADMAP.md](ROADMAP.md) for what's next and [CHANGELOG.md](CHANGELOG.md).

## Notes

Independent project, not affiliated with Icom. The LAN protocol is a clean-room
implementation informed by the open-source
[wfview](https://gitlab.com/eliggett/wfview) and
[kappanhang](https://github.com/nonoo/kappanhang). Everyone signs in (accounts with
roles, lockouts after repeated failures) and an always-on cross-site / DNS-rebinding guard
checks every request — but it is still a transmitter on the network, so keep the port off the
public internet: bind it to loopback and use Tailscale Serve (Administration → Remote), a VPN,
or the firewall. See [docs/REMOTE-ACCESS.md](docs/REMOTE-ACCESS.md). Manufacturer CI-V / CAT reference PDFs
are not redistributed (see `docs/README.md`).

## License

**AGPL-3.0-only** — see [LICENSE](LICENSE) and [NOTICE](NOTICE). The project is AGPL
because its **neural CW decoder** bundles the **DeepCW** model from
[e04/deepcw-engine](https://github.com/e04/deepcw-engine), which is AGPL-3.0. If you
host a modified copy, AGPL section 13 requires offering users the corresponding source
(the app links to it from the top bar, the user menu and the sign-in page).
The in-browser ONNX runtime ([onnxruntime-web](https://github.com/microsoft/onnxruntime))
is MIT.
