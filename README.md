# nibe-lokal

A small web app for a NIBE S-series heat pump that talks **directly to the pump
over Modbus TCP on your own network**. No cloud account, no subscription, no
myUplink. Your settings and your history stay in files you own.

It does three things:

- **Shows the pump** — temperatures, hot water, ventilation, fan speed, degree
  minutes, compressor hours. Installable on a phone home screen as a PWA.
- **Does the two things you actually reach for a phone for** — extra hot water
  before a bath, and more ventilation for a few hours that goes back on its own.
- **Backs up every setting** to a timestamped JSON file, and diffs two snapshots
  so you can see what changed since June.

It is deliberately boring: Python standard library plus one optional package for
the register map. No framework, no build step, no container. It should still run
in five years without anyone touching its dependencies.

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
pip install nibe                 # optional but recommended, see "The register map"
cp config.example.yaml config.yaml
$EDITOR config.yaml              # set `host` to your pump's IP
python3 -m nibelokal status      # does it answer?
python3 -m nibelokal serve       # http://localhost:8377/
```

On a phone: open the address in Safari or Chrome and use *Add to Home Screen*.
It then behaves like an app, offline shell and all.

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
| **Everyday** | extra hot water, hot water comfort mode, ventilation mode and its return time, room setpoint, SG Ready | written freely |
| **Guarded** | heating curve and offset, supply temperature limits, hot water start/stop temperatures, immersion heater power, operating mode, fan speed percentages | require `confirm: true`, are range-checked against the register's own min/max, and are logged |
| **Blocked** | compressor frequency limits, heating medium pump mode, floor drying, AUX-over-Modbus, external sensor value injection, smart-price control | never written by this app |

Anything not explicitly classified is treated as **guarded**, not as free.

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

## Backups

```bash
python3 -m nibelokal backup --note "before touching the heating curve"
python3 -m nibelokal diff backup/nibe-20260601-090000.json backup/latest.json
```

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

## Security

Modbus has no authentication at all — anyone who can reach port 502 can change
every setting. Assume the same about this app:

- Bind to `127.0.0.1` and put it behind something, or leave it on a LAN you trust.
- Set `auth_token` in `config.yaml` to require a token
  (`X-Auth-Token` header, or `?token=` once, which the app remembers).
- Set the **IP address restriction** in the pump's menu 7.5.9.
- Do not expose port 8377 to the internet. Use a VPN or Tailscale if you want it
  from outside; a heat pump is not a thing to publish.

## Configuration

Every key in `config.example.yaml` can also be set as an environment variable
with a `NIBE_` prefix (`NIBE_HOST`, `NIBE_LISTEN_PORT`, `NIBE_TOKEN`).

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
which also powers the Home Assistant integration. If you already run Home
Assistant, use that integration instead — it is more capable than this and has
more people looking after it. This exists for the case where you want the two or
three things you actually use, on a phone, without running a home automation
platform to get them.

Register semantics and the protocol limits come from NIBE's own
[Modbus S-Series](https://installer.nibe.eu/download/18.47aa975e18a8b43315f342c/1696946129027/Modbus%20S-Series.pdf)
technical document.

## Not affiliated with NIBE

This is an independent project. NIBE is a trademark of NIBE Energy Systems, used
here only to say which pumps this talks to. Nothing here is endorsed by them, and
using it is between you and your warranty.

## Licence

MIT — see [LICENSE](LICENSE).
