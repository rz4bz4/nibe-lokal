# The F series, unverified

**Nobody who wrote this has an F-series pump.** Everything below is read out of
NIBE's own manuals, NIBE's own register maps and other people's projects. None
of it has been seen working. It ships because the alternative was to say no to
the question, and because being wrong in public with the sources written down
is a faster way to become right than waiting for a pump to arrive.

Read [What is established and what is not](#what-is-established-and-what-is-not)
before you trust any of it, and
[issue #1](ISSUE-f-series.md) if you have an F750 and half an hour.

## What the F series is to this app

The F generation — F370, F470, F730, F750, F1145, F1155, F1245, F1255, F1345,
F1355, and the SMO 20/40 and VVM indoor units built on the same platform — is
the one before the S series. Every concept this app uses exists on an F750:
heating curve, offset, own curve, hot water comfort mode, ventilation mode,
extra hot water, degree minutes, alarms. They sit at different register numbers,
some of them mean slightly different things, and one of them — extra hot water —
is a different shape entirely.

So the app grows a translation table and a `generation` setting, and everything
above the Modbus layer stays as it was. That is the whole idea, and it is the
easy part. The hard parts are the two below: getting to the pump at all, and
knowing which of the differences are real.

## Getting there: pump, MODBUS 40, gateway, LAN

An F750 has an RJ45 socket on the back. It is not the way in. NIBE's own wiring
list calls it `W130 Nätverkskabel för NIBE Uplink` — it speaks to NIBE's cloud
and nothing else. There is no Modbus TCP on an F-series pump.

Modbus arrives as an accessory:

    F750  ──RS485──  MODBUS 40  ──RS485──  gateway  ──Ethernet──  your LAN

**MODBUS 40**, NIBE part number **067 144** (RSK 625 08 05). NIBE's F750
installer manual describes it in one sentence: *"MODBUS 40 gör att styrning och
övervakning av F750 kan göras med en DUC (dataundercentral) i fastigheter.
Kommunikationen sker då med hjälp av MODBUS-RTU."*
([IHB SV 2218-2, p. 53](https://assetstore.nibe.se/hcms/v2.4/entity/document/330612/storage/MzMwNjEyLzAvbWFzdGVy/download/Installat%C3%B6rshandbok_Fr%C3%A5nluftsv%C3%A4rmepumpar_F750_531385-2.pdf))
It is a board that goes inside the pump and presents a Modbus RTU slave on a
screw terminal.

What you must set on the pump, all from NIBE's MODBUS 40 installer manual:

- **Activate it** in menu **5.2** "Systeminställningar" — menu **5.2.4** on an
  F1345, F1355, SMO 40, VVM 225, VVM 310, VVM 320, VVM 325 and VVM 500.
  (The SMO 20 and SMO 40 belong here and not with the S series. Their register
  maps carry **48852** *Modbus40 Word Swap* and **48889** *MODBUS40 Disable
  LOG.SET* — the accessory's own registers — and no Modbus TCP registers at
  all, and the `nibe` package's README offers a TCP connection *"for S models"*
  only. `config.example.yaml` said they had built-in Modbus TCP; they do not,
  and it no longer says so.)
- **Pump software above 3000.** Menu 3.1 "serviceinfo" shows it. Below that,
  MODBUS 40 does not answer at all.
- **MODBUS 40 software version 7 or higher**, also in menu 3.1. Version 10 or
  higher if you want to change the slave address; version 11 or higher if you
  want the word-swap setting.

The serial settings are not settings. NIBE: *"Modbus40 has fixed settings that
cannot be changed"* — **RTU, 9600 baud, 8 bits, no parity, 1 stop bit**. There
is no faster option and no 19200.

The **slave address** is **1** (0x01), fixed, up to and including MODBUS 40
v.7. From v.10 it is selectable 1–247 in menu **5.3.11**, and that requires pump
software **5539** (**4150R7** on an F1345). Menu 5.3.11 only appears if the
module is new enough to have it.

**Word swap** also lives in menu 5.3.11, and only appears from MODBUS 40 v.11.
It decides the order of the two 16-bit registers that make up a 32-bit value.
Get it wrong and every 32-bit register — degree minutes at 40940, the energy
meters, the compressor hour counters — reads a large and stable-looking nonsense
number, while every 16-bit register is fine. That failure looks like a bad
register map, and it is not.

**What the factory setting is, NIBE's own two documents disagree about**, and
this is worth spelling out because the app used to take the losing side.

- The **MODBUS 40 installer manual** says *"Factory setting: Big Endian"*, which
  reads as the high word at the lower address.
- **NIBE's register database** — ModbusManager's, republished as
  [yozik04/nibe](https://github.com/yozik04/nibe) — publishes the setting as a
  register: **48852 "Modbus40 Word Swap", u8, writable, default 1**, info text
  *"If set; swapping the words in 32-bit variables when value requested via
  'read holding register' commando"*. Swapped, from a factory default of 1, is
  the **low** word first. It is on **all seventeen** F maps the package ships —
  F370, F470, F730, F750, F1145, F1155, F1245, F1255, F1345, F1355, the SMO
  20/40 and the five VVMs — and on none of the S ones, which is what you would
  expect of a register that belongs to the MODBUS 40 accessory.
- The **`nibe` package** decodes `word_swap=True` as the low word first and
  tells users to set it False only if they turned the swap *off* in 5.3.11 —
  i.e. it also treats swapped-on as the shipped state.
- **Home Assistant**'s `nibe_heatpump` integration defaults `word_swap` to True.

Three sources to one — and two of the three share a lineage, because Home
Assistant's integration is built on the `nibe` package, so call it two
independent readings against one. Still: the one is a sentence in a manual about
what "Big Endian" means, and the other side is NIBE's own register database plus
the software most F-series owners are actually running. So
**this app's default on the F series is the low word first**, the same as the S
series — and, because the setting is a register, it would rather not rely on a
default at all: `nibelokal` **reads 48852 once at startup** on an F profile and
uses what the pump says (1 → low word first, 0 → high word first). If the
register is missing from the map or does not answer, the default stands. Every
backup header records which of the two happened, as `profile.word_order_source`:
`read`, `assumed`, or `configured` when `word_swap` in `config.yaml` overrode
both. **None of this has been checked against a real pump**, which is why the
app asks rather than asserts, and why 48852's value is one of the questions in
[issue #1](ISSUE-f-series.md).

## What MODBUS 40 can and cannot do

This is the question that decides whether this app's design survives contact
with an F-series pump, so it gets NIBE's own table rather than a paraphrase.
From the MODBUS 40 installer manual, section *The Modbus command*:

| Function | Description | Register address | No. of registers | Max timeout |
|---|---|---|---|---|
| 0x03 | Read holding registers | 40001–65534 **included in** LOG.SET | 1–20 | 0.5 s |
| 0x03 | Read holding registers | 40001–65534 **not included in** LOG.SET | 1–2 \* | 2.1 s |
| 0x10 | Write multiple registers | 40001–65534 | 1–2 \* | 2.1 s |
| 0x2B | Read device identification | — | — | 0.5 s |

\* two Modbus registers are used for a 32-bit parameter.

Five things follow, and the first is the one that matters.

**Any register can be read without configuring anything.** The manual is
explicit, twice. On reading: *"Manual readout is time consuming and only one
value at a time can be read, max timeout 2,1 s. **The parameter does not have to
be included in the LOG.SET file.**"* On writing: *"Only one value can be entered
at a time. **The parameter does not have to be included in the LOG.SET file.**"*
So LOG.SET — the file you build in NIBE's ModbusManager, save to a USB stick and
feed the pump through menu USB → loggning — is a **cache, not a whitelist**. It
holds at most 20 registers, they refresh twice a second, and they are the only
ones you may ask for more than two of in one request. Everything else is still
readable, one or two registers at a time, slowly.

This app's design therefore bends rather than breaks. It does not need
ModbusManager to work at all. It does need to be much less impatient, and on an
F profile it is: `nibelokal/pump.py` asks for **one register per request** with
no gap-reading, two words only for a 32-bit register, and a per-request timeout
of at least 2.5 s. Those three numbers come from `nibelokal/profile.py` rather
than from a module constant, so the S series keeps the twenty-register batches
it has always used.

**It is slow, and the arithmetic is worth doing before you are surprised by
it.** The 2.1 s is a *maximum timeout* in NIBE's table, not a measured
round-trip; NIBE's own FAQ separately says to set the timeout to 2100 ms and
*"the delay between polls"* to 1000 ms for manual reading. Taking those two
numbers at face value:

    20 dashboard registers an F750 answers    20 x 2.1 s   ≈ 42 s, up to 62 s with the 1 s spacing
    the same 20 with 20 of them in LOG.SET     1 x 0.5 s    ≈ 0.5 s, in one request
    the 646 registers in the F750 map          646 x 2.1 s  ≈ 23 min, up to 34 min with the spacing

(Twenty and not the twenty-five in `pump.DASHBOARD`: five of them have no
F-series equivalent and are never asked for. 646 is the size of the F750 map in
the `nibe` package, counted; a backup walks all of it.)

The shipped `poll_seconds: 60` is therefore the floor on an F pump rather than
a comfortable default — forty seconds of bus time inside a sixty-second poll.
Either raise it or put the dashboard registers in a LOG.SET file. A backup is
minutes, not seconds, and it holds the bus for all of them — do not run one
while you are waiting for a poll. `config.example.yaml` says the same thing
next to `poll_seconds`, which is where somebody will actually read it.

**Writing is function 16 only.** NIBE, in the MODBUS 40 FAQ: *"Modbus40 uses
commando type 'Write Multiple registers'. 'Write Single registers' does not work
in the Modbus40."* A client that sends function 6 gets nothing, on every
register, and the symptom is that reads work and writes do nothing at all.

**Some registers refuse to be written, and NIBE will not list them here.** The
manual says a value can be updated *"if the heat pump/indoor module permits
it"*, and that *"the values that can be updated are in ModbusManager"* — which
is the same database the `nibe` package's `write: true` flag comes from. So the
map is the best available answer to "may I write this", and it is an answer from
a database rather than from the pump.

**Function 0x2B tells you what you are talking to.** Read device identification
returns *"label (e.g. 'NIBE'), product code (e.g. 'F1245') and software version
(e.g. 5539)"*. That is a free model check, and worth more on the F series than
on the S, because here the register map and the accessory firmware can disagree
with each other. `python3 -m nibelokal status` asks for it and prints what came
back as its **last** line — best effort, asked last and with a one-second
timeout of its own. It is optional in the specification, and a device may
implement "no" by staying silent rather than by answering an exception; asked
first at the ordinary timeout with the ordinary retry, that costs the command
two timeouts and a dropped socket before a single register is read. A pump that
does not answer prints `not answered` and everything above the line is
unaffected.

### What users report that NIBE does not document

One thread is worth more than the rest, because the people in it were doing
exactly this: [Modbus and Nibe Modbus40](https://forum.logicmachine.net/showthread.php?tid=903&pid=13184)
on the LogicMachine forum. Their reports, in their words:

- *"The modbus40 module reguire 2.1 sec between reading of single register."*
  So the 2.1 s is spacing, not just a timeout. One participant settled on a
  2.2 s read delay and a 15 s timeout for 31 parameters.
- *"32 bit registers must be in the log.set"* — reading a 32-bit value that is
  not in LOG.SET returned CRC errors, while 16-bit registers read fine. NIBE's
  table allows 1–2 registers outside LOG.SET and footnotes that 32-bit
  parameters use two; these users could not make that work. If it holds, the
  practical rule is that **32-bit registers belong in LOG.SET**, or you read a
  16-bit alias where one exists (degree minutes has one: 43005).
- *"maximum log.set registers is 20"*, and *"if you create log.set then nibe
  timeout is only 0,5 sec"* — both agreeing with the manual.

None of that is NIBE's word, and none of it has been reproduced here.

### Unknown

- Whether a single client can hold a TCP connection to a gateway open for
  months without MODBUS 40 or the gateway dropping it, and what this app should
  do when it does. On the S series the answer is "the pump drops sessions
  occasionally, reconnect and back off". Nobody has told us the F answer.
- Whether MODBUS 40 raises an alarm on the pump when its master goes quiet.
  The openHAB binding warns that on the *other* protocol — nibegw, below — *"a
  telegram from the heat pump must be acknowledged, otherwise the heat pump will
  raise an alarm and go into the alarm state"*. Whether the Modbus RTU side has
  any equivalent watchdog is not documented anywhere we found, and it is the
  single question most worth an owner's answer.

## Framing: which kind of gateway, and why it matters

MODBUS 40 speaks Modbus **RTU** on a wire. This app speaks TCP. Something has to
sit in between, and the two kinds of box in between are not interchangeable.

- **Transparent serial forwarding.** The gateway is a wire with a socket on the
  end. Whatever bytes arrive on TCP go out of the serial port unchanged. The
  payload is therefore a raw RTU frame: address, function, data, **CRC16**. This
  is what Home Assistant's Modbus integration calls `rtuovertcp`, *"TCP/IP
  connection with rtu framer, used when connection to modbus forwarders"*.
- **Modbus TCP ↔ RTU protocol conversion.** The gateway is a Modbus TCP server.
  It takes an MBAP header (transaction id, protocol id, length, unit id), no
  CRC, and builds the RTU frame itself on the serial side. Home Assistant calls
  this `tcp`, *"TCP/IP connection with socket framer"*.

Send MBAP to a transparent gateway and MODBUS 40 sees a frame with a bad CRC and
says nothing. Send raw RTU to a converting gateway and it rejects the header.
Both failures look identical from here: a connection that opens and then times
out. That is what the `framing` key is for.

    framing: rtu   # transparent gateway — this app builds the RTU frame and the CRC
    framing: tcp   # converting gateway, or a real Modbus TCP device (every S-series pump)

Common boxes, and which they are:

| Box | Does transparent | Does conversion | What to set |
|---|---|---|---|
| Waveshare RS485 TO ETH (B) / POE ETH (B) | yes, and it is the **default**: *"By default, the data between the serial port and network port is transparently transmitted"* | yes: *"If you need to convert Modbus TCP to RTU, you need to select the conversion protocol as 'Modbus TCP<-->RTU' in the device settings dialog box"* | either; `framing: rtu` out of the box |
| USR / PUSR USR-TCP232-410s, -304 | yes | yes — work mode *"TCP server, Modbus TCP"* in the web interface | either; `framing: tcp` if you set Modbus TCP mode |
| Elfin EW11 / EW11A | yes | yes — it has an explicit protocol setting with a Modbus option | either; match the setting you chose |

The table says "either" three times because these are all configurable, and the
honest advice is not "buy this one" but **look at the box's own setting and set
`framing` to match it**. If you have no idea which mode a gateway is in, try
`rtu` first: it is the default on the Waveshare, and it is the mode that needs
nothing configured.

There is one known-bad combination worth naming, because somebody already lost
an evening to it — and this page used to describe it wrongly. Home Assistant's
`nibe_heatpump` integration does **not** require RFC2217: the `nibe` library it
is built on takes `tcp://`, `serial://` *and* `rfc2217://` URLs, and RFC2217 is
one option among three. What happened in
[home-assistant/core#140425](https://github.com/home-assistant/core/issues/140425)
is that a user with a VVM 500, a MODBUS 40 and a Waveshare RS485-to-Ethernet
tried the RFC2217 form and found the Waveshare *"does not seem to support
RFC2217 or BINARY mode"* (the integration's own docs also say *"Support for
RCU-based communication is currently untested"*). The lesson is about the
gateway and the URL scheme, not about the integration. This app speaks plain TCP
to either kind of gateway and never RFC2217, so a box that does not implement it
costs nothing here.

### nibegw, and why this app does not support it

There is a second, better-travelled route to an F-series pump, and it is not
Modbus. **nibegw** is firmware for an Arduino, an ESP or a Raspberry Pi that
pretends to *be* a MODBUS 40 — it sits on the pump's RS485 bus, receives the
telegrams the pump broadcasts, acknowledges them, and forwards them over UDP.
It is what openHAB's `nibeheatpump` binding and the Home Assistant integration
mostly use, and it is genuinely good: the pump pushes its data instead of being
polled, so the 2.1 s problem above does not arise.

**This app does not support it and is not going to.** Not because it is worse,
but because it is a different protocol at every level: NIBE's own framing on the
wire, UDP on ports 9999 and 10000 instead of TCP, an acknowledgement the gateway
must send or the pump raises an alarm, and a push model rather than a request
model. Nothing in `nibelokal/modbus.py` would be reused. It would be a second
transport with its own failure modes, and this project's whole claim is that it
is small enough to still work in five years.

If you already run nibegw, use
[Home Assistant's Nibe integration](https://www.home-assistant.io/integrations/nibe_heatpump/)
or [openHAB's binding](https://www.openhab.org/addons/bindings/nibeheatpump/).
They are better at this than this app will ever be. The README says the same
thing about the S series.

## Where the two generations differ

Every row was checked against NIBE's own F750 manuals and the F750 map in
[yozik04/nibe](https://github.com/yozik04/nibe), which is the map this app uses
when you set `model:` rather than `register_csv:`. **Source** says where the
claim comes from; **confidence** says how much of it is reading versus
inference.

| Thing | S735 | F750 | Menu on F | Source | Confidence |
|---|---|---|---|---|---|
| Heating curve | 40027, 0–15, default 5 | **47007**, 0–15, default 9 | 1.9.1 | map; UHB menu 1.9.1 | High |
| Curve 0 = own curve | yes | **yes** | 1.9.1 → 1.9.7 | UHB, verbatim: *"Värmekurva 0 innebär att egen kurva (meny 1.9.7) används."* | High |
| Heating offset | 40031, −10…+10 | **47011**, −10…+10, default 0 | 1.1 | map; IHB SE 1540-3 menu 1.1, *"temperatur (förskjutning av värmekurva)"*, factory −1 | High |
| Offset step size | ~2.5 °C supply per step | **~2.5 °C supply per step** | — | IHB SE 1540-3, verbatim: *"en kurvförskjutning på +2 steg höjer framledningstemperaturen med 5 °C vid alla utetemperaturer"* — word for word the S735 sentence | High |
| Own curve points | 40046…40040 = P1…P7 | **47026…47020 = P1…P7**, same defaults (45, 40, 35, 32, 26, 15, 15) | 1.9.7 | map | High |
| Own curve outdoor temps | −30, −20, −10, 0, +10, +20, +30 | **−30, −20, −10, 0, +10, +20 for P1…P6.** P7 not shown | 1.9.7 | UHB menu 1.9.7 lists exactly six rows; F1155 UHB lists the same six | **P1–P6 high; P7 unknown** — see below |
| Point offset | — | **47027** outdoor point, −40…+30 | 1.9.8 | map; UHB menu 1.9.8 | High |
| Hot water comfort | 40057: Small / Medium / Large / Smart Control (0,1,2,4) | **47041: ekonomi / normal / lyx / Smart Control (0,1,2,4)** | 2.2 | map info text says *"Setting in menu 2.2"*; UHB: *"'ekonomi', 'normal' eller 'lyx'"* | High |
| Extra hot water | 40226 minutes (u16) + 40698 on/off | **48132 'Temporary Lux', an enumeration: 0=off, 1=3 h, 2=6 h, 3=12 h, 4=one time increase** | 2.1 | map; UHB menu 2.1: *"Inställningsområde: 3, 6 och 12 timmar, samt läge 'från'"* | **High for 0–3, low for 4** — see below |
| Ventilation mode | 40105, 0–4 | **47260 'Fan Mode', 0–4, mappings Normal / Fan mode 1–4** | 1.2 | map; UHB menu 1.2: *"normal samt hastighet 1-4"* | High |
| Per-mode fan % | 40106…40110 = speed 4, 3, 2, 1, normal | **47261…47265 = speed 4, 3, 2, 1, normal** — the *same descending order* | 5.1.5 | map; IHB SE 1540-3 menu 5.1.5, *"normal samt hastighet 1-4, 0 – 100 %"* | High |
| Do modes 1–2 reduce airflow | yes: measured 0 % and 30 % against 70 % normal | **yes, by the map's own defaults: 1 = 0 %, 2 = 30 %, normal = 65 %, 3 = 80 %, 4 = 100 %** | 5.1.5 | map defaults, identical to the S735 map's | High as a default; the installer may have changed all five |
| Fan return time | 40116…40119, 1–24 h | **47271…47274, 1–99 h** | 1.9.6 | map | High |
| Degree minutes | 40012, s32 | **43005 s16 and 40940 s32, both present, both `write: true`** | 4.9.3 | map; MODBUS 40 manual's example list names *"Degree minutes 43005"*; UHB menu 4.9.3 range −3000…3000 matches 43005 scaled | **Which one it regulates on: unknown** — see below |
| Alarm number | 31976, s16, read-only | **45001 'Alarm', s16, read-only** — *"the alarm number of the most severe current alarm"* | — | map | High |
| Alarm reset | 40023, writable | **45171 'Alarm Reset', u8, writable, *"Reset alarm by setting value 1"*** | — | map | High |
| "Class 1 alarm" flag | 32196 | **does not exist** on any F map the `nibe` package ships | — | map | High |
| Alarm code meanings | `alarms_s.json`, 478 codes | **`alarms_f.json`, 461 codes — a different set of numbers** | — | NIBE's alarm search, F route | High, and important. `alarms.py` picks the table by `profile.generation`, and refuses to look a code up at all when the generation is unknown |
| Holiday | status flag only (40020, 45391) | **whole block: 48043 activated, 48044/48045 dates, 48047 hot water mode, 48048 fan mode, 48051 room temp** | 4.7 | map | High, and unused — this app does not do holiday mode on either generation |

Four rows need more than a table cell.

**The seventh own-curve point.** The register map has seven, P1 to P7, on both
generations and with identical defaults. NIBE's F750 user manual shows menu
1.9.7 with exactly six rows: *framledningstemp. vid −30, −20, −10, 0, 10, 20 °C*.
The F1155 user manual shows the same six, and immediately below it the own
*cooling* curve with its own five rows, so the heating list is not obviously cut
off by the page. Either the menu scrolls and +30 °C is below the fold, or the F
series really has six points and P7 is spare. The defaults are consistent with
the first reading — P6 and P7 are both 15 °C, which is what you would set at
+20 and +30 — but that is inference, not a manual. **Do not assume an outdoor
temperature for P7 on an F pump.** P1–P6 at −30 to +20 in ten-degree steps is
NIBE's own figure and can be relied on.

**"One time increase" on 48132.** The map gives value 4 as *One time increase*.
The F750 user manual's menu 2.1 lists only *3, 6 och 12 timmar, samt läge
"från"*. So either value 4 is reachable over Modbus but not from that menu, or
it belongs to another model on the same map. Writing 4 to an F750 is a guess.
Values 0–3 are NIBE's own menu.

The bigger point about this row: **on an F pump, extra hot water is not a number
of minutes.** The S-series app asks "how many minutes" and writes 40226. There
is no such register on an F750. The three durations are the options, and
anything the app offers beyond them is a lie about what the pump can do.

**Degree minutes.** Both registers exist and both are marked writable. NIBE's
MODBUS 40 manual, in its short example list of parameter addresses, names
*"Degree minutes — 43005"*, and the pump's own menu 4.9.3 has a settable
*"aktuellt värde"* over −3000…3000, which is exactly 43005's range once its
factor of 10 is applied. 40940 is titled *"Degree Minutes (32 bit) — full
resolution"*. What none of that establishes is **which one the regulation
actually follows when you write to it**, or whether writing either one does
anything at all rather than being overwritten on the next control cycle. Read
43005; it is 16-bit, so it does not need LOG.SET or the right word-swap setting.
Do not write either until somebody has checked.

**Word swap is a setting, so the app asks for it rather than assuming it.**
`word_swap` in config.yaml: empty means *ask the pump* — read 48852 at startup
on an F profile, fall back to the low word first if it does not answer — and
`true`/`false` is an owner who has looked at menu 5.3.11 and outranks both. It
reaches `Register.decode` and `Register.encode` through the profile and the
registry, in one place, so a read and a write cannot end up using opposite
orders. See the word-swap section above for why the default changed sides, and
`profile.word_order_source` in any backup header for which answer was used.

**45001 means opposite things on the two generations.** On every S-generation
map in the `nibe` package it is *"Activate forced control"*, writable — the
service menu's manual override, and one of the addresses this project
[blocks outright](registers.md#blocked). On every F-generation map it is
*"Alarm"*, read-only. Same number, and one of the two is a register you must
never write while the other is one you read every minute. This is the clearest
argument there is for the translation table being chosen by generation rather
than by address, and for `generation` never being guessed from an ambiguous
`model` string.

The mirror image is worse. **45171, "Alarm Reset", exists on every F map and on
no S map**, so it was in none of the tiers in [registers.md](registers.md) and
landed where everything unclassified lands: guarded, writable if you ask
explicitly — guarded by a default rather than by anybody's decision. Both it and
its S-series twin 40023 are now listed explicitly, with the reason, so the tier
is a decision on either generation.

The sentence in `nibelokal/alarms.py` that this row was written against —
*"40023 is never written here, and there is no code path that could"* — was half
right and has been corrected. The module really does contain no write; what is
not true is that nothing could. `/api/write` writes any writable register it is
asked to, and both of these are writable. What is actually enforced is the
tier: an explicit `confirm: true`, and a refusal outright when
`allow_guarded_writes: false`.

## What is established and what is not

**Established from NIBE's own documents**, and quoted above with the document
named: the hardware chain and the part number; the serial settings and that they
are fixed; the slave address and where it becomes configurable; the firmware
versions; that any register can be read and written without LOG.SET; the
register/timeout table; that function 6 does not work; that word swap exists,
lives in menu 5.3.11 and appears from MODBUS 40 v.11; the menu numbers; the
curve-0-means-own-curve rule; the offset's size; the hot water comfort names;
the temporary-lux durations; the ventilation modes.

**Word swap's factory value is *not* on that list any more.** NIBE says one
thing in the MODBUS 40 manual ("Big Endian") and the opposite in its own
register database (48852, default 1, "swapping the words"), and no reading of
those two documents settles it — see the word-swap section above. The app
follows the register, reads 48852 at startup rather than trusting either, and
records in every backup header whether the order was read or assumed.

**Established from the register map** (`yozik04/nibe`, which is ModbusManager's
database in another form), which is good evidence about numbering and types and
no evidence at all about behaviour: every register number in the table above,
the enumerations, the ranges and the defaults.

**Reported by users and not reproduced here**: the 2.1 s spacing being real
rather than a timeout, and 32-bit registers outside LOG.SET failing with CRC
errors. The app acts on the first (one register per request on an F profile)
and does not try to fix the second: a CRC failure on one register marks that
register missing and leaves it out of the poll for an hour, the same way a
register the pump refuses is treated, rather than raising out of the poll every
minute. If the reports are right, the symptom on an F pump is a handful of
32-bit registers listed under `missing_registers` on /api/status and the rest
of the dashboard working.

**Not established, and the docs should not pretend otherwise**:

- Nothing at all end to end. No F-series pump has been read by this app.
- **Which word order a real MODBUS 40 ships in**, and therefore whether reading
  48852 at startup gives the right answer or merely a confident one. NIBE's
  manual and NIBE's register database disagree; the app sides with the database
  and asks the pump, and nobody has stood next to an F750 and compared menu
  5.3.11 with what 48852 answered. A single owner's reply settles it.
- The outdoor temperature of own-curve point P7.
- Whether `48132` value 4 does anything on an F750.
- Which degree-minute register the pump regulates on, and whether writing either
  has an effect.
- Whether MODBUS 40 alarms or drops the link when its master goes quiet.
- Whether the severity this project derives for the 461 F alarm codes is right;
  see `_meta.severity_confidence` in `nibelokal/data/alarms_f.json`, which puts
  the expected disagreement at about one code in forty and names 48 codes where
  NIBE's own text is too thin to classify at all.
- What the app's poll actually costs on a real MODBUS 40, as opposed to what the
  arithmetic above says it should.

## How to help

If you have an F-series pump, four things, in increasing order of effort. The
first one alone is worth more than everything on this page.

**1. Export your register list — from ModbusManager, not from the pump.** An
F-series pump has no register export in its USB menu; that is an S-series thing,
and this page used to send people looking for it. The file comes from NIBE's own
Windows tool: **ModbusManager → File → Export to file**, with your model
selected. Point `register_csv:` at the result. That file is the only map
guaranteed to match your unit and its firmware, it makes every "High" in the
table above irrelevant for you, and pasting it into the issue makes them
irrelevant for the next person.

The two exports do not look alike, and the app tells them apart by itself: a
ModbusManager export has four lines of preamble, semicolons, whole register
numbers (`40004`, `47007`) in an `ID` column and a `Mode` column of `R` / `R/W`,
where the S-series USB export has *offsets* and a `Register type` column. A
number at or above 30001 cannot be an offset — the largest offset the address
space allows is 25533 — so nothing has to be configured. It also carries no
model name, which is fine: the app works the generation out from the map (40027
is the heating curve on an S, 47007 on an F), and only needs `generation: F` in
`config.yaml` if your export somehow has both markers or neither.

**2. Run `status` and paste what comes back.**

```bash
python3 -m nibelokal status
```

Whether it answers, whether the numbers are plausible, and — if the 32-bit ones
are absurd — whether menu 5.3.11's word swap fixes them.

**3. Answer the questions in [issue #1](ISSUE-f-series.md)** that you can answer
by walking to the pump and looking at its display. Does menu 1.9.7 scroll to a
seventh row at +30 °C? What does menu 2.1 offer? What are the five fan
percentages in menu 5.1.5? None of that needs this app installed.

**4. Time it.** How long does one read of a register that is not in your
LOG.SET actually take? That single number decides what `poll_seconds` should
default to on an F pump, and right now the default is arithmetic from a
timeout.

## Four things that behave differently on an F pump

Not differences between the pumps — differences in what this app does, because
of them. All four are things somebody would otherwise find out by being
surprised.

**The daily automatic backup is off, and that is deliberate.** On an S-series
pump `auto_backup_hours` defaults to 24 and a full snapshot is a few seconds of
Modbus TCP. Here the same snapshot is every register in the map at one request
each — 20 to 35 minutes for a 646-register F750 — and it is taken *by the
polling thread*, which holds the pump's only bus for all of it. Every poll in
that window is skipped, the page goes stale for half an hour, and an alarm
raised meanwhile is noticed when the backup finishes. So on an F profile an
unset `auto_backup_hours` resolves to 0, `serve` says so on the console, and
`nibelokal backup` prints the estimate before it starts. Setting
`auto_backup_hours: 24` yourself still works and is a perfectly reasonable thing
to want overnight; it is being the *default* that is not.

**A wrong `unit` looks exactly like a gateway that is not there.** A MODBUS 40
that is not being addressed does not answer "wrong slave", it answers nothing —
so the socket opens and every request times out, which is the same symptom as
the wrong `framing` and as a MODBUS 40 that was never activated in menu 5.2. The
offline message on the RTU path names all three rather than guessing between
them. The slave address is **1**, fixed, up to MODBUS 40 v.7; **1–247** from
v.10, set in menu **5.3.11**.

**Ten alarm codes were reclassified by hand.** The severity in `alarms_f.json`
is derived by a rule over NIBE's Swedish text, and the rule reads NIBE's *long*
text — which ten codes do not have, or have only in its generic form. All ten
fell through to `warning`, wrongly in both directions:

| Code | Text | Was | Now |
|---|---|---|---|
| 175 | Uppstart av mjukstartskortet pågår | warning | **info** |
| 183, 233, 234, 235 | Avfrostning pågår | warning | **info** |
| 270 | Förvärm. av kpr pågår | warning | **info** |
| 998 | startar | warning | **info** |
| 220 | Högtryckslarm | warning | **alarm** |
| 221 | Lågtryckslarm | warning | **alarm** |
| 222 | Motorskyddslarm | warning | **alarm** |

The seven are the pump narrating normal operation: at the default
`alarm_min_severity: warning`, an exhaust-air F750 would have pushed a
notification for every defrost, several times a day, all winter. The three are
faults that stop the compressor, and at `alarm_min_severity: alarm` nobody would
have been woken by them. The rule and every code it moved are written down in
`_meta.hand_reclassified` in the file. **These ten are the only codes in that
file a person has read**; the other 451 are as the machine left them, and about
one in forty of those is expected to be classified differently from how you
would classify it.

**32-bit registers may read as nonsense, and there is one thing to check.** See
the word-swap section above: the app reads 48852 at startup and uses its answer,
but if your pump's menu 5.3.11 and its 48852 disagree — or if the register does
not answer — `word_swap: false` in `config.yaml` is the override. Every backup
header says which way round the snapshot was taken.

Replies in Swedish are welcome; the repo is in English because the code is.

## Sources

- NIBE, *MODBUS 40 Installatörshandbok / Installer manual* (multi-language,
  IHB 031725) — the register/timeout table, LOG.SET, word swap, menu 5.2 and
  5.3.11, the fixed serial settings.
  [PDF](https://nibe.ua/files/3/documents/instructions%20for%20accessories/%D0%86%D0%BD%D1%81%D1%82%D1%80%D1%83%D0%BA%D1%86%D1%96%D1%8F%20%D0%BC%D0%BE%D0%BD%D1%82%D0%B0%D0%B6%D0%BD%D0%B8%D0%BA%D0%B0%20NIBE%20MODBUS%2040%20(EN).pdf)
- NIBE, *FAQ: MODBUS 40* (1321-2) — function 16 only, address, the 2100/1000 ms
  timing, full register numbers.
  [PDF](https://installer.nibe.eu/download/18.47aa975e18a8b43315f342a/1696946128721/FAQ%20Modbus%2040.pdf)
- NIBE, *Installatörshandbok NIBE F750* (IHB SV 2218-2, 531385) — MODBUS 40 as
  an accessory, its part number, and the menu overview.
  [PDF](https://assetstore.nibe.se/hcms/v2.4/entity/document/330612/storage/MzMwNjEyLzAvbWFzdGVy/download/Installat%C3%B6rshandbok_Fr%C3%A5nluftsv%C3%A4rmepumpar_F750_531385-2.pdf)
- NIBE, *Installatörshandbok NIBE F750* (IHB SE 1540-3, 331464) — an older
  edition of the same book, and the one that spells out the menus this table
  needs: 1.1 and the offset's size, 4.9.3 gradminutinställning, 5.1.5
  fläkthast. frånluft.
  [PDF](https://cdn.jseducation.se/files/pages/nibe750install-2.pdf)
- NIBE, *Användarhandbok NIBE F750* (UHB SE 1301-1, 231367) — menus 1.2, 1.9.1,
  1.9.7, 1.9.8, 2.1, 2.2, and curve 0 meaning own curve.
  [PDF](https://www.rskdatabasen.se/infodocs/DOS/dos_30_6251130.pdf)
- NIBE, *Användarhandbok NIBE F1155* (UHB SV 2008-9, 231546) — the cross-check
  on menu 1.9.7's six points.
  [PDF](https://assetstore.nibe.se/hcms/v2.3/entity/document/35761/storage/MDM1NzYxLzAvbWFzdGVy)
- NIBE's alarm-code search, F route —
  [larmkoder](https://www.nibe.eu/sv-se/support/larmkoder), see
  `_meta.source_api` in `nibelokal/data/alarms_f.json`.
- [yozik04/nibe](https://github.com/yozik04/nibe) — the F-series register maps,
  the serial-Modbus example at 9600 with slave id 1, and the `word_swap` flag
  that pointed at menu 5.3.11.
- [openHAB nibeheatpump binding](https://www.openhab.org/addons/bindings/nibeheatpump/)
  — nibegw, the 20-register data telegram, and the acknowledgement the pump
  requires.
- [Home Assistant Modbus integration](https://www.home-assistant.io/integrations/modbus/)
  — the `tcp` versus `rtuovertcp` framing distinction, in the words most people
  will have met it in.
- [home-assistant/core#140425](https://github.com/home-assistant/core/issues/140425)
  — VVM 500 + MODBUS 40 + Waveshare, and RFC2217.
- [LogicMachine forum, *Modbus and Nibe Modbus40*](https://forum.logicmachine.net/showthread.php?tid=903&pid=13184)
  — the user reports about timing and 32-bit registers.
- [Waveshare RS485 TO ETH (B) wiki](https://www.waveshare.com/wiki/RS485_TO_ETH_(B))
  and [PUSR USR-TCP232-410s Modbus TCP FAQ](https://www.pusr.com/support/faq/basic-test-for-modbus-rtu-to-modbus-tcp-function-of-serial-to-ethernet-converter.html)
  — the two gateway modes, in the vendors' own words.
