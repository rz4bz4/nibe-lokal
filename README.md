# nibe-lokal

A small web app for a NIBE S-series heat pump that talks **directly to the pump
over Modbus TCP on your own network**. No cloud account, no subscription, no
myUplink. Your settings and your history stay in files you own.

It does six things:

- **Shows the pump** — temperatures, hot water, ventilation, fan speed, degree
  minutes, compressor hours, alarms. Four tabs — five once electricity prices
  are configured, since that tab is hidden until they are — built for a phone,
  installable on the home screen as a PWA.
- **Does the things you actually reach for a phone for** — extra hot water before
  a bath, more ventilation for a few hours that goes back on its own, and nudging
  the heat up or down a step.
- **Helps you get the heat curve right**, which is the part everyone gets wrong.
  Two questions — is it too cold or too warm, and *when* — turn into one concrete
  register change, with the reasoning shown. See "Heating advice" below. Some
  three dozen further settings are editable directly, grouped and explained in
  plain language rather than as raw register numbers. That is a deliberate
  selection, not the whole pump: an S735 exposes 562 writable registers, and
  most of them are commissioning settings that belong on the pump's display.
  `nibelokal/settings.py` is the list and says why each one is on it.
- **Backs up every setting** to a timestamped JSON file — automatically, once a
  day — and diffs two snapshots so you can see what changed since June. A backup
  you have to remember to take is one you will not have when you need it, and you
  need it right after changing something you should not have.
- **Pushes alarms to your phone.** NIBE's own 478 S-series alarm texts, in
  Swedish, verbatim, sent once per alarm rather than once per minute. 289 of the
  478 also carry the sentence from NIBE's longer text that tells you what to do;
  the other 189 have no such sentence at NIBE, and the app shows nothing rather
  than inventing one. Eleven codes have no short text either — for those you get
  the number and the fact that it fired. The severity that decides whether a code
  is worth waking you is this project's reading of NIBE's Swedish wording, not
  something NIBE publishes. This is the one thing myUplink gave away free that
  actually matters when something breaks at two in the morning.
- **Knows things the pump does not** — indoor temperature from your own sensors,
  hourly electricity prices, and the SMHI forecast. The first two feed
  suggestions it never applies on its own: given weeks of indoor readings it will
  say whether the heat curve's height or its slope is the thing that is wrong,
  and given prices it will lay out which hours to heat in and which to coast,
  paired inside one day. The forecast is shown and not used — nothing computes
  against it — because a panel you read before you act is a smaller promise than
  a model, and the honest description of what is there. Those three feeds, the
  alarm push above and the curve autotuning are five optional integrations; with
  none of them configured the app is exactly what it was before. See
  [Optional integrations](#optional-integrations).

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
python3 -m unittest discover tests    # no pump required
```

Nothing in the suite touches a pump or the network. The ones that walk every
register map the `nibe` package ships skip themselves if it is not installed.

There are also two browser smoke tests, and both only read and open panels —
neither changes anything on the pump. `tests/ui_smoke_new.py` is the one to
reach for: it starts its own server answering invented but realistic JSON, so it
needs no pump, no Homey, no Tibber and no SMHI, and it runs the app through
seven states — everything configured, everything configured but empty, nothing
configured, an owner's real curve, integrations that do not answer, a slow
server, and a pump that will not read. It writes screenshots to `_shots/`.

```bash
pip install playwright && playwright install chromium
python3 tests/ui_smoke_new.py          # starts its own stub server
python3 tests/ui_smoke.py              # against a running `serve`, needs a pump
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
| **Everyday** | extra hot water, hot water comfort mode, ventilation mode and its return time, room setpoint, periodic hot water interval | accepted without `confirm: true` |
| **Guarded** | heating curve and offset, supply temperature limits, hot water start/stop temperatures, immersion heater power, operating mode, fan speed percentages, the whole SG Ready menu | require `confirm: true`, are range-checked against the register's own min/max, and are logged |
| **Blocked** | compressor frequency limits, heating medium and brine pump modes, floor drying, AUX-over-Modbus, external sensor value injection, smart-price control | never written by this app |

Anything not explicitly classified is treated as **guarded**, not as free.
[docs/registers.md](docs/registers.md) lists every register in each tier and the
reasoning behind it.

**What that means from a phone.** Every write that goes through `/api/write` —
the heating step on the Värme tab, every row in the settings list — opens a
dialog first, showing the register, the value before and after and how long to
wait for an effect, and sends `confirm: true` whatever tier the register is in.
Two buttons are deliberately one tap and no dialog: extra hot water and the
ventilation boost. They post to `/api/hotwater` and `/api/ventilation`, which
write only everyday registers, and both undo themselves — the hot water after
the minutes you asked for, the fan after its return time. So the promise is not
"the app confirms every write"; it is **nothing with consequences is written
without an explicit confirm**, and the everyday tier is the list of registers
this project has decided have none. That list is also what lets something which
is not the app — a script, a Homey flow, `curl` — start a ventilation boost
without asserting that it understands consequences it does not have.

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
| In all weather | The curve sits too low or too high | Offset, one step |
| Only when it's cold out | The curve's slope | Curve +1, or the own-curve points that bracket the current outdoor temperature |
| Only in mild weather | The mild end of the curve | A flatter curve **and** offset the other way, applied together |

Turning the offset up in November is what makes the house too warm in March. That
is the mistake the table above exists to prevent.

### What the offset actually does

NIBE's own wording, from the S735 installer manual (IHB SV 2220-1, p. 31) and
repeated in their FAQ:

> An offset of the heating curve means that the supply temperature changes by the
> same amount for all outdoor temperatures, e.g. a curve offset of +2 steps
> increases the supply temperature by 5 °C at all outdoor temperatures.

So: **plus is warmer**, it is a parallel shift rather than a change of slope, and
NIBE's worked example is about 2.5 °C of supply temperature per step. Separately
they put it at roughly one degree indoors per step, while noting that "the number
of steps required to change the indoor temperature by one degree depends on your
heating system" — underfloor heating and radiators do not answer the same way.

Two things cut the effect short, both documented: the calculated supply
temperature is never allowed below **min supply** (menu 1.30.4) or above **max
supply** (menu 1.30.6), so a curve already sitting on either limit will not move.

**One thing NIBE does not document at all:** whether the offset still applies when
the curve is set to 0, i.e. when your own curve points are in force. Seven NIBE
manuals say nothing about it either way. Measured on one S735-family pump running
an own curve, it does apply.

**And a trap worth knowing:** the calculated supply temperature *ramps* to a new
offset over about five minutes. Reading it ten seconds after a write measures the
ramp, not the setting — an early measurement here got 0.2 °C per step that way and
was wrong by more than an order of magnitude. A later six-minute run got 1.5 °C
per step and was *also* short: the value was still climbing at 0.6 °C a minute
when the run ended. The app uses NIBE's 2.5 °C, which is also what this pump's
owner puts it at after years of living with it.
[docs/registers.md](docs/registers.md) has the measurement, what it does and does
not establish, and the sources.

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

## Optional integrations

Five things the app can do if you give it a credential, and does not do
otherwise. **All five are optional and the app is complete without them.** A
fresh clone with none of them configured starts, polls the pump, serves the web
app and gates writes exactly as described above. There is no nag, no placeholder
and no degraded mode: an integration you have not configured simply does not
render — its panel is absent, not empty. If one you *have* configured is down,
slow or misconfigured, its own endpoint says so in Swedish and the rest of the
app carries on. None of them can stop the pump being read or written.

Four of the five need a credential. All four live in `config.yaml` in plain
text — see [Security](#security) before you put them there. SMHI needs none.

Every key below is also settable as an environment variable with a `NIBE_`
prefix (`NIBE_TIBBER_TOKEN`, and so on). The authoritative list, with the
defaults and the reasoning, is `config.example.yaml`; this section is the setup.

### Indoor temperature, from a Homey

**What it gives you.** The pump only knows the room temperature if a room sensor
is wired to it, and most installations have none — which is why the room setpoint
in the web app is often a knob that regulates nothing. If you run a Homey Pro, it
already knows what every room is doing. The app reads those sensors, averages
them, shows the number, stores it in the history, and hands it to the curve
autotuning below. It never writes anything to Homey.

```yaml
homey_host: "192.168.1.20"           # IP or hostname; http:// is added if missing
homey_token: "..."                   # local API key, see below
homey_devices: "Sovrum, Vardagsrum"  # device names or ids, comma separated
homey_max_age_minutes: 60
homey_timeout: 5.0
homey_cache_seconds: 120
```

**The credential** is a *local* API key made on the Homey itself, not the Athom
cloud login: my.homey.app → Settings → System (Advanced) → API keys → New API
key, scope `homey.device.readonly` (add `homey.zone.readonly` if you want zone
names). It is shown once.

Set `homey_devices`. Leaving it empty means every device that reports a
temperature, which is convenient for a first look and almost never right: door
and window sensors sit in the draught on an outside wall and read several degrees
off the room they are nominally in. `homey_max_age_minutes` exists because a
sensor with a flat battery does not disappear — Homey keeps serving its last
value indefinitely — and a reading frozen at 24 °C in June would otherwise hold
the heating down all winter.

**If you skip it:** no indoor panel, no indoor history, and the curve autotuning
below has nothing to work from and says so.

### Weather forecast, from SMHI

**What it gives you.** A panel showing what is about to happen, next to what
the pump's own outdoor sensor says is happening now. A house with hours of
thermal inertia is better served by the first of those.

**The heating advice does not read it.** `/api/advice` is not passed the
forecast and neither `advisor.py` nor `autotune.py` touches it; both work from
the pump's own outdoor temperature and from the history. The forecast is
something for you to look at before you act on a suggestion, not an input to
it. Wiring it in would be a real feature and is not one this does yet.

```yaml
weather_lat: 59.3293
weather_lon: 18.0686
weather_ttl_minutes: 60
weather_timeout: 8.0
```

**No credential.** SMHI's open point forecast is free and unauthenticated; it
needs only the coordinates of the house. Decimal degrees, dot as the decimal
point, and do not swap them — a longitude in the latitude field is a point
outside SMHI's model and answers 404. The forecast covers Sweden and its
surroundings; elsewhere the model has nothing to say.

**If you skip it:** no forecast panel, and nothing else changes — the advice
works from the pump's own outdoor sensor either way.

### Electricity prices, from Tibber

**What it gives you.** Hourly spot prices, and an advisory plan that shifts
heating into the cheap hours. The plan shifts load, it never sheds it: every hour
it heats a step extra is paid back by an hour it coasts, within the same day, so
the day's offsets sum to zero. Only useful on an hourly variable contract.

```yaml
tibber_token: "..."
tibber_home_id: ""          # only if your Tibber account has several homes
spot_timeout: 10.0
spot_cache_seconds: 900
spot_min_spread: 0.15       # kr/kWh; below this the day is too flat to bother
spot_cheap_rank: 0.25
spot_expensive_rank: 0.75
spot_max_offset: 1
spot_max_pairs: 4
```

**The credential** is a personal API token from
[developer.tibber.com](https://developer.tibber.com) — log in with your Tibber
account and take the access token. Read-only is enough.

**It is advisory. Nothing in this app writes the heating offset by itself.** The
plan is a table you can act on or ignore. `spot_max_offset` is clamped to 1
whatever you write there, because one step of register 40031 is already about
2.5 °C of supply temperature.

**If you skip it:** no price panel and no plan. The heating advice is unaffected;
it never depended on prices.

### Alarm push, via Pushover

**What it gives you.** A notification when the pump raises an alarm, and another
when it clears. This matters because a pump that has quietly dropped to the
immersion heater still keeps the house warm — the first sign is the electricity
bill six weeks later.

**Watching is always on and needs no credential.** The alarm register is polled
with everything else, every alarm and all-clear is written to the database, and
`/api/alarms` and the web app show them with NIBE's own text. Only the push off
the machine needs Pushover.

```yaml
pushover_token: "..."             # application token
pushover_user: "..."              # user key
alarm_notify: true
alarm_min_severity: "warning"     # info | warning | alarm
alarm_emergency_priority: true
alarm_retry_seconds: 300
alarm_expire_seconds: 10800
alarm_debounce_seconds: 900
```

**The credentials** are both from [pushover.net](https://pushover.net): log in,
the **user key** is on the front page, then *Create an Application/API Token* for
the **application token**. Leave either empty and nothing is sent. Pushover is a
paid app, one-off, per platform.

`alarm_debounce_seconds` is the flap guard. A sensor sitting right on its limit
raises and clears the same alarm on alternate polls, which at a 60 s poll is two
pushes a minute; at most three notifications per code go out inside the window
and the rest arrive as one message saying how many times it has switched.

`alarm_emergency_priority` sends priority 2 — repeating until acknowledged — for
codes classified as real alarms. Turn it off if being woken at 03:00 by a sensor
fault is worse than finding out at breakfast. Note that the severity doing that
classifying is derived by this project from NIBE's Swedish wording, not published
by NIBE; `docs/` and the `_meta` block in `nibelokal/data/alarms_s.json` say
exactly how.

**If you skip it:** alarms are still recorded and still shown in the web app. You
just have to look.

### Heat curve autotuning

**What it gives you.** Given weeks of history it proposes at most one small
change, and says whether the curve's *height* or its *slope* is the thing that is
wrong — which is the question the heating advice asks you to answer from memory.
Here it is answered from the data.

```yaml
autotune_target_indoor: 21.0
autotune_days: 30
```

**No credential**, but it needs an indoor temperature, which the pump does not
have — so in practice it needs the Homey integration above, and enough history
for the weather to have varied. `autotune_target_indoor` is optional: left
unset, the analysis measures against the room setpoint on the pump's own
display, which is the closest thing to a stated wish that exists without this
key, and `/api/autotune` says which of the two it used in `target_source`. Set
it when the pump's setpoint is not what you actually want the house to hold —
on a pump with no room sensor that number regulates nothing, so it is easy to
have left at something you never meant.

**Advisory only: it never writes.** Like everything else in the heating advice,
the change it proposes is one step, and it tells you how long to wait.

**If you skip it:** no autotuning panel. The two-question heating advice above is
unaffected and does not depend on it.

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
long as `history_days` says.

**It is not small, and the size is arithmetic rather than a guess.** One row per
register per poll, and each row costs about 57 bytes once the two indexes on
`readings` are counted. So:

    rows  = registers × 86400 ÷ poll_seconds × days
    bytes = rows × 57

The shipped defaults are the 25 registers in `DASHBOARD` (`nibelokal/pump.py`)
at `poll_seconds: 60`, which is 36 000 rows a day: **about 21 MB per ten days,
750 MB and 13 million rows a year**, and about 830 MB in the steady state at the
shipped `history_days: 400`. Measured by building the table, not estimated.
SQLite handles that size without complaint — the index on `ts` exists so the
daily prune does not full-scan it — but it is a real amount of disk on a Pi with
an SD card, and it is more than twenty times the "few tens of MB a year" an
earlier version of this paragraph claimed. Cut `history_days`, lengthen
`poll_seconds`, or shorten `DASHBOARD`; each is linear in the figures above, so
recompute rather than trusting the three bolded numbers if you have changed any
of the three.

Do not poll harder than the pump allows: NIBE documents **max 100 registers per
second and 20 registers per query**, and the app enforces both. Community reports
suggest hammering it can wedge the pump's Modbus service until a reboot.

## What myUplink does that this does not

Worth knowing before you cancel the subscription:

| | myUplink | Here |
|---|---|---|
| Alarm push notification | yes | yes, via Pushover, with NIBE's own alarm texts |
| Weekly schedules | yes | no, deliberately — see below |
| Holiday / away mode | yes | no, deliberately — see below |
| History and graphs | yes, while you pay | yes, in a file you own, for as long as you like |
| Remote access | yes | via VPN or Tailscale |
| Electricity price control | Smart Price Adaption, paid tier | yes, from your own Tibber account, advisory only — it proposes an offset, it does not write one |
| Weather forecast | yes | yes, from SMHI directly |
| Indoor temperature | only if a room sensor is wired to the pump | from your own sensors via Homey, if you have one |
| Firmware updates, NIBE support | yes | no — done from the pump's display |

Weekly schedules and holiday mode are not implemented and are not planned. Set
them on the pump's display, where they work with no subscription and nothing of
ours in the path. They are not in myUplink's public API either, so the gap is not
one this app could close by talking to NIBE differently — and on the S series
Modbus does not expose the settings at all. There is no weekly-schedule register
anywhere in the S-series map, and holiday has only a status flag (40020, plus
45391 for away mode) with no dates, temperatures or fan modes behind it. The
F-series map has the whole holiday block at 48043–48051; the S series simply does
not. Toggling a status the pump's own calendar also drives is a good way to end
up with two things fighting over the same setting, so the app leaves those two
registers where everything unclassified lands: guarded, writable only if you ask
for it explicitly.

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

### The credentials in config.yaml

Configuring the [optional integrations](#optional-integrations) puts four
secrets in `config.yaml` **in plain text**: the Homey local API key, the Tibber
personal token, and the Pushover application token and user key. There is no
keyring, no encryption and no indirection — the file is read as it is written.

What that means in practice:

- **Set the file mode.** `chmod 600 config.yaml`, owned by the user the service
  runs as. On a shared machine the default umask leaves it world-readable, and
  `auth_token` is in the same file as everything above.
- **Do not commit it.** `config.yaml` is in `.gitignore` and should stay there.
  `config.example.yaml` is the one that is tracked, and it has no values in it.
  If you have already committed a config with tokens, rotating the tokens is the
  fix; deleting the file in a later commit is not.
- **Prefer the environment if your supervisor gives you somewhere better to put
  it.** Every key also reads from `NIBE_HOMEY_TOKEN`, `NIBE_TIBBER_TOKEN`,
  `NIBE_PUSHOVER_TOKEN`, `NIBE_PUSHOVER_USER`, and a systemd
  `EnvironmentFile=` with mode 600 is a slightly better place than the repo.
- **Know what each one is worth if it leaks.** The Homey key is read-only in the
  scope suggested above, but it is a key to your house's sensors. The Tibber
  token reads your consumption and prices, and is tied to your electricity
  account. The Pushover pair lets someone send notifications to your phone. None
  of them can reach the heat pump — that is what `auth_token` and the pump's own
  IP restriction are for — but none of them are throwaway either.

SMHI needs no credential, so there is nothing to protect there.

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
