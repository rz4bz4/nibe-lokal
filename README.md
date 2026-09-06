# nibe-lokal

A small web app for a NIBE S-series heat pump that talks **directly to the pump
over Modbus TCP on your own network**. No cloud account, no subscription, no
myUplink. Your settings and your history stay in files you own.

It does four things:

- **Shows the pump** — temperatures, hot water, ventilation, fan speed, degree
  minutes, compressor hours, alarms. Five tabs, built for a phone, installable
  on the home screen as a PWA.
- **Does the things you actually reach for a phone for** — extra hot water before
  a bath, more ventilation for a few hours that goes back on its own, and nudging
  the heat up or down a step.
- **Helps you get the heat curve right**, which is the part everyone gets wrong.
  Two questions — is it too cold or too warm, and *when* — turn into one concrete
  register change, with the reasoning shown. See "Heating advice" below. Every
  setting the pump exposes is also editable directly, grouped and explained in
  plain language rather than as raw register numbers.
- **Backs up every setting** to a timestamped JSON file — automatically, once a
  day — and diffs two snapshots so you can see what changed since June. A backup
  you have to remember to take is one you will not have when you need it, and you
  need it right after changing something you should not have.

It is deliberately boring: Python standard library plus one optional package for
the register map. No framework, no build step, no container. It should still run
in five years without anyone touching its dependencies.

> **The web UI and the heating advice are in Swedish.** The README, the CLI and
> the API are in English. NIBE's home market being what it is, that made sense
> for the author; translations are welcome.

> **This writes to a heat pump.** A wrong setting can cost you money for weeks
> before you notice, or damage the installation. The app refuses the settings
> most likely to do that (see below), but the responsibility is yours, and this
> may affect your warranty. No warranty is given — see [LICENSE](LICENSE).

## Who this is for

**If you already run Home Assistant, use its
[Nibe integration](https://www.home-assistant.io/integrations/nibe_heatpump/)
instead.** It is more capable than this, has more people looking after it, and
this project borrows its register maps anyway.

This is for the case where you want the three or four things you actually use —
extra hot water, a ventilation boost, a nudge to the heat, and your own history
— on a phone, without running a home automation platform to get them.

## What you need

- A NIBE S-series heat pump (S735, S1155, S1255, S320, SMO40 …) with software
  **2.2.1 or later**, on the same LAN as the machine running this.
- Python 3.10 or newer.
- **Modbus TCP enabled on the pump**: on its display, menu **7.5.9 Modbus
  TCP/IP**. Note the two switches in that menu:
  - *"Reading Modbus only"* must be **off** if you want the app to change
    anything. Reading works either way.
  - *"IP address restriction"* — set it to the machine running this app. Worth
    doing: Modbus has no authentication of its own.
- The pump only accepts connections from private address ranges (10/8, 172.16/12,
  192.168/16), so this runs on your LAN, not in a cloud.

Find the pump's address in your router's DHCP list — it shows up as
`NIBE-<serial number>`.

## Install

```bash
git clone https://github.com/rz4bz4/nibe-lokal.git
cd nibe-lokal
# On Debian, Raspberry Pi OS and Homebrew Python, pip needs a venv:
#   python3 -m venv .venv && . .venv/bin/activate
pip install nibe                 # optional but recommended, see "The register map"
cp config.example.yaml config.yaml
$EDITOR config.yaml              # set `host` and `model` (or `register_csv`)
python3 -m nibelokal status      # does it answer?
python3 -m nibelokal serve       # http://localhost:8377/
```

### On a phone

Open the address in Safari or Chrome and use *Add to Home Screen*. It then
behaves like an app: own icon, no address bar, and the shell is cached by a
service worker so it opens instantly (it still needs the network to reach the
pump).

One catch worth knowing before you try: **service workers only run on HTTPS or
localhost.** Over plain `http://192.168.x.y:8377` the home-screen shortcut works
but the caching does not. The easy fix, if you use Tailscale, is to let it put a
real certificate in front:

```bash
tailscale serve --bg --https=443 http://127.0.0.1:8377
```

That gives you `https://<machine>.<tailnet>.ts.net` — a proper certificate, works
from anywhere on your tailnet, and nothing is exposed to the public internet. Add
that hostname to `allowed_hosts` in `config.yaml`, or the Host check will refuse
it. A reverse proxy with any other certificate does the same job.

```bash
python3 -m unittest discover tests    # 53 tests, no pump required
```

There is also a browser smoke test that clicks through a running instance. It
only reads and opens panels — it changes nothing on the pump:

```bash
pip install playwright && playwright install chromium
python3 tests/ui_smoke.py              # against a running `serve`
```

To keep it running, use whatever your machine already has — `systemd`, `launchd`,
`docker run --network host`, a `screen` session. There is nothing special about
this process.

## The register map

Register numbers differ between models **and between firmware versions of the
same model**. The app can get the map from either of two places:

1. **Your own pump.** Menu 7.5.9 → *"Export all registers"* onto a USB stick.
   Point `register_csv` at the resulting CSV. This is the only map guaranteed to
   match your unit — use it if you can.
2. **The `nibe` package** (`pip install nibe`), which ships the same maps the
   Home Assistant integration uses. Set `model:` to yours. Convenient, and what
   most people will start with.

If the map does not match your pump, registers read *plausible nonsense* rather
than failing loudly. That is the failure mode to watch for. Registers your pump
does not implement are detected on first read and skipped from then on.

## What it will and will not change

Once Modbus write is enabled, the pump accepts writes to **every** holding
register, including the ones that quietly turn a heat pump into an expensive
electric radiator. So the app keeps its own gate (`nibelokal/safety.py`), in
three tiers:

| Tier | What is in it | Behaviour |
|---|---|---|
| **Everyday** | extra hot water, hot water comfort mode, ventilation mode and its return time, room setpoint, periodic hot water interval, SG Ready | written freely |
| **Guarded** | heating curve and offset, supply temperature limits, hot water start/stop temperatures, immersion heater power, operating mode, fan speed percentages | require `confirm: true`, are range-checked against the register's own min/max, and are logged |
| **Blocked** | compressor frequency limits, heating medium pump mode, floor drying, AUX-over-Modbus, external sensor value injection, smart-price control | never written by this app |

Anything not explicitly classified is treated as **guarded**, not as free.
[docs/registers.md](docs/registers.md) lists every register in each tier and the
reasoning behind it.

Two things worth knowing before you change a setting from a phone:

- **A successful read-back is not proof the pump acted on it.** Some settings are
  accepted, stored, and then ignored by the regulation. Check an effect variable
  (calculated supply temperature, degree minutes) rather than trusting the echo.
- **Ventilation modes are not ordered low to high.** On one measured S735, normal
  is 70 %, mode 1 is 0 % and mode 2 is 30 % — modes 1 and 2 *reduce* ventilation.
  The app reads the percentages out of the pump before choosing a mode, so "more
  air" cannot silently become "no air". Do not hardcode a mode number.

On an exhaust-air pump the ventilation *is* the heat source. A fan left at 0 %
means no heat source, a frosting evaporator, and condensation in the house.

## Heating advice

Setting a heat curve by hand is genuinely awful: the feedback loop is a day long,
four knobs look like they do the same thing, and the pump tells you nothing about
which one is wrong. The app asks two questions instead.

**Is the house too cold or too warm, and when?** That second question is the
whole game:

| When it's wrong | What's actually wrong | What the app suggests |
|---|---|---|
| In all weather | The curve sits too low or too high | Offset, one step (≈ 1 °C indoors) |
| Only when it's cold out | The curve's slope | Curve +1, or the own-curve points that bracket the current outdoor temperature |
| Only in mild weather | The mild end of the curve | A flatter curve **and** offset the other way, applied together |

Turning the offset up in November is what makes the house too warm in March. That
is the mistake the table above exists to prevent.

It is not a model and makes no predictions. Alongside the rule it uses what the
pump can say about itself, and several of those facts silently invalidate the
obvious advice:

- **No room sensor?** Then the room setpoint is writable but regulates nothing,
  and the app says so instead of letting you turn a knob that does nothing.
- **Curve set to 0** means own curve. Setting it to 1–15 throws your points away.
  The app refuses to suggest crossing that line in either direction.
- **Immersion heater running?** Raising the heat then buys more electric heat, not
  more heat pump. The app refuses, and if the pump reports it is prioritising hot
  water it says to come back in half an hour.
- **A curve point already at max supply** is a setting the pump will not act on.
  Suggesting it would look like it worked and change nothing.

Every suggestion is one step, and the app says to wait before the next one — a
day for radiators, two for underfloor heating, which is what the `emitters`
setting in `config.yaml` is for. Suggestions that only make sense together are
applied together, in one request that either does all of it or none.

## Backups

```bash
python3 -m nibelokal backup --note "before touching the heating curve"
python3 -m nibelokal diff backup/nibe-20260601-090000.json backup/latest.json
```

The app also takes one on its own every `auto_backup_hours` (24 by default), so
there is always something to go back to.

`backup` reads every register the pump answers on and writes
`backup/nibe-<timestamp>.json` plus a `latest.json` symlink. `diff` compares two
snapshots and lists **only the settings** that changed — measurements move
constantly and are filtered out.

Snapshots are plain JSON with the register title, unit, min/max, default and
value, so they stay readable without this tool. They are worth keeping in git.

## History

The app polls the dashboard registers every 60 seconds into a SQLite file. That
is the history that myUplink's paid tier sells you, in a file you own, for as
long as `history_days` says. It costs a few tens of MB a year.

Do not poll harder than the pump allows: NIBE documents **max 100 registers per
second and 20 registers per query**, and the app enforces both. Community reports
suggest hammering it can wedge the pump's Modbus service until a reboot.

## What myUplink does that this does not

Worth knowing before you cancel the subscription:

| | myUplink | Here |
|---|---|---|
| Alarm push notification | yes | shows a banner when you open the app; no push |
| Weekly schedules | yes | no — but the pump's own display has them (menu 1.x) |
| Holiday / away mode | yes | no |
| History and graphs | yes, while you pay | yes, in a file you own, for as long as you like |
| Remote access | yes | via VPN or Tailscale |
| Firmware updates, NIBE support | yes | no — done from the pump's display |

The alarm notification is the one that matters. A pump typically alarms on a
winter night and stops heating; myUplink pushes to your phone, this shows you a
red banner the next time you open it. If that gap matters to you, keep the
subscription or wire the alarm register into whatever notifier you already run.

## Security

Modbus has no authentication at all — anyone who can reach port 502 can change
every setting. Assume the same about this app:

- Bind to `127.0.0.1` and put it behind something, or leave it on a LAN you trust.
- Set `auth_token` in `config.yaml` to require a token
  (`X-Auth-Token` header, or `?token=` once, which the app remembers).
- The app refuses requests whose `Host` header is not localhost, its bind
  address, this machine's own name, or a bare IP address (an IP cannot be
  DNS-rebound, and reaching the app at `192.168.1.5` is the normal case), and
  requires `Content-Type: application/json` on writes. Together those stop a web page you happen to open
  from POSTing to your heat pump through your own browser, and stop DNS
  rebinding. If you front it with a proxy under another name, add that name to
  `allowed_hosts`.
- Set the **IP address restriction** in the pump's menu 7.5.9.
- Do not expose port 8377 to the internet. Use a VPN or Tailscale if you want it
  from outside; a heat pump is not a thing to publish.

## Configuration

Every key in `config.example.yaml` can also be set as an environment variable
with a `NIBE_` prefix: `NIBE_HOST`, `NIBE_LISTEN_PORT`, `NIBE_AUTH_TOKEN`,
`NIBE_ALLOWED_HOSTS`, and so on.

### Keeping it running

There is nothing special about the process — any supervisor will do. A systemd
unit, for a machine that already has the repo in `/opt/nibe-lokal`:

```ini
[Unit]
Description=nibe-lokal
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 -m nibelokal -c /opt/nibe-lokal/config.yaml serve
WorkingDirectory=/opt/nibe-lokal
Restart=always
RestartSec=10
User=nibe

[Install]
WantedBy=multi-user.target
```

On macOS the same thing is a launchd plist with `RunAtLoad` and `KeepAlive` set
to true, `ProgramArguments` pointing at the same command, and
`WorkingDirectory` at the repo.

## Commands

```
python3 -m nibelokal status                 # the dashboard registers, once
python3 -m nibelokal read 30009 40105       # specific registers
python3 -m nibelokal search "hot water"     # search the map
python3 -m nibelokal search fan --writable
python3 -m nibelokal backup [--note ...]
python3 -m nibelokal diff OLD.json NEW.json
python3 -m nibelokal serve [--listen 0.0.0.0] [--port 8377]
```

## When it does not work

**"Cannot reach the heat pump"** — Modbus TCP is off. Pump display, menu 7.5.9.
Check the pump answers ping first; if it does, this is the menu, not the network.

**Reads work, writes fail with "illegal function"** — *"Reading Modbus only"* is
on in menu 7.5.9.

**Values are nonsense** — the register map does not match your pump. Export the
CSV from the pump itself and use that.

**`Connection reset by peer`** — the pump drops Modbus sessions occasionally,
particularly after a firmware update. The app reconnects and backs off; if it is
constant, reboot the pump.

**No unit answers** — try `unit: 0` instead of `1`. Both are in use in the wild.

## Prior art and thanks

The register maps come from [yozik04/nibe](https://github.com/yozik04/nibe),
which also powers the Home Assistant integration.

Register semantics and the protocol limits come from NIBE's own
[Modbus S-Series](https://installer.nibe.eu/download/18.47aa975e18a8b43315f342c/1696946129027/Modbus%20S-Series.pdf)
technical document.

## Not affiliated with NIBE

This is an independent project. NIBE is a trademark of NIBE Energy Systems, used
here only to say which pumps this talks to. Nothing here is endorsed by them, and
using it is between you and your warranty.

## Licence

MIT — see [LICENSE](LICENSE).
