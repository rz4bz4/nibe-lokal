# Which registers this app will change, and why

Once Modbus write is enabled in menu 7.5.9, a NIBE S-series pump accepts writes
to **every** holding register. It will not stop you setting the operating mode to
"additional heat only" in July, and it will not tell you afterwards. The bill
tells you, six weeks later.

So the gate lives in `nibelokal/safety.py`, and this is the reasoning behind it.
Addresses are NIBE coil addresses as the `nibe` package numbers them (4xxxx =
holding register; subtract 40001 for the wire address).

## Everyday

Written without ceremony. These are reversible, bounded, and the pump undoes
most of them by itself.

| Register | Setting | Why it is safe |
|---|---|---|
| 40226 / 40698 | Extra hot water, minutes and on/off | Counts down and stops. This app caps it at 24 h regardless of what the map allows. |
| 40057 | Hot water comfort mode | Four documented options; the pump validates. |
| 40105 | Ventilation mode 0–4 | Returns to normal on its own — see the return-time registers below. |
| 40116–40119 | Return time for fan modes 4–1 | Bounded 1–24 h. This is what makes a ventilation boost self-cancelling. |
| 40207–40210 | Room setpoint per climate system | Ordinary thermostat behaviour, range-checked. |
| 40067 | Periodic hot water interval | Days between legionella cycles. |
| 40761 | SG Ready | Designed to be driven externally. |

## Guarded

Real settings with real consequences. They need `confirm: true`, are checked
against the register's own min/max, and land in the write log.

**Cost you money if wrong**

| Register | Setting | What goes wrong |
|---|---|---|
| 40027–40030 | Heating curve per climate system | A curve one step too steep is a permanently overheated house. You will not notice until the bill. |
| 40031–40034 | Heating offset | Same, but immediate. |
| 40103 | Max internal additional heat (kW) | This is the immersion heater. Raising it is buying electric heat. |
| 40181 | Permit additional heat for heating | Lets the immersion heater in at all. |
| 40186 | Additional heat stop temperature, auto mode | Set too high and the immersion heater runs in mild weather. |
| 40238 | Operating mode | **Value 2 is "additional heat only"** — the single most expensive register on the pump. |
| 40059–40065 | Hot water start/stop temperatures | Above roughly 50–55 °C the compressor cannot get there alone and the immersion heater finishes the job every cycle. |
| 45010 | Supply line setpoint override | Overrides the calculated supply temperature outright. 80 °C is a legal value here. |
| 40035 / 40039 | Min / max supply temperature | The ceiling that keeps 45010 and a bad curve in check. |

**Affect the house rather than the bill**

| Register | Setting | What goes wrong |
|---|---|---|
| 40106–40110 | Fan speed per ventilation mode, in percent | On an exhaust-air pump the ventilation *is* the heat source. See the warning below. |

## Blocked

Never written by this app. Change them on the pump's display, where you can see
the whole picture and the pump can warn you.

| Register | Setting | Why not |
|---|---|---|
| 40089 / 40090 | Min / max compressor frequency | Outside the compressor's designed window: wear, then alarms. |
| 40096 | Heating medium pump operating mode | Stopping circulation while the compressor runs is a high-pressure alarm. |
| 40696 | Manual heating medium pump speed | Same failure, by a different route. |
| 42741 + 42742 | AUX function via Modbus | A meta-register: write a function id to one, on/off to the other. Ids 4 and 5 block the compressor. One typo stops the heating, silently. |
| 40121–40135 | Floor drying programme | Runs 20–70 °C for weeks. Never from a phone. |
| 41100–41140 | Smart energy source / electricity price control | Misconfigured, this is constant additional heat. |
| 45218–45230, 45988 | External sensor value injection | Feeds the pump a value you supply instead of a real sensor. If this app stops, the pump keeps regulating on a frozen number forever. |
| 40943–40944, 44165–44175 | Compressor frequency blocking bands | Same category as the frequency limits. |

**Anything not listed anywhere is treated as guarded**, not as free. A register
nobody has thought about is not a register to write casually.

## Two things the tiers cannot protect you from

**Ventilation modes are not ordered low to high.** On one measured S735, normal
is 70 %, mode 1 is 0 %, mode 2 is 30 %, mode 3 is 80 % and mode 4 is 100 %. Modes
1 and 2 *reduce* ventilation. An app that hardcodes "mode 1 = boost" turns the
fan off. This app reads the percentages out of the pump and picks the mode by
what they say, every time.

On an exhaust-air pump that matters more than it sounds: the ventilation is where
the heat comes from. A fan left at 0 % means no heat source, a frosting
evaporator, and condensation in the house.

**A successful read-back is not proof the pump acted.** Some settings are
accepted, stored, and then ignored by the regulation — Home Assistant issue
[#154450](https://github.com/home-assistant/core/issues/154450) is exactly this,
on a room setpoint. Every write in this app reads the value back, and that is
worth something, but the honest check is an effect variable: calculated supply
temperature, degree minutes, compressor frequency.

## If you disagree with these tiers

They are one file. `EVERYDAY`, `GUARDED`, `BLOCKED` and `BLOCKED_RANGES` in
`nibelokal/safety.py` are plain dictionaries — move a register between them, or
set `allow_guarded_writes: false` in `config.yaml` to leave only the everyday
tier. Just move things deliberately, and read the row above before you do.

## Appendix: what the heating offset actually does

The offset (register 40031, menu 1.1.1) is the control people reach for first
and understand least, so this is what it does, with sources.

### What NIBE documents

From the S735 installer manual (IHB SV 2220-1, p. 31), repeated verbatim in the
S1155 and S1255 manuals and in NIBE's own FAQ:

> An offset of the heating curve means that the supply temperature changes by the
> same amount for all outdoor temperatures, e.g. a curve offset of +2 steps
> increases the supply temperature by 5 °C at all outdoor temperatures.

Three things follow, all documented:

- **Plus is warmer.** Menu 1.1.1: "To increase or decrease the indoor
  temperature, increase or decrease the value in the display."
- **It is a parallel shift**, not a change of slope. NIBE uses the word
  *parallellförskjutning* explicitly elsewhere, describing what SG Ready does.
- **The limits cut it off.** "Because the supply temperature cannot be calculated
  higher than the set maximum value or lower than the set minimum value, the
  heating curve flattens out at these temperatures" (p. 31). Min supply is menu
  1.30.4, max is 1.30.6.

On size, NIBE gives two different numbers for two different things, and both are
approximate: about **2.5 °C of supply temperature per step** (the example above),
and about **one degree indoors per step** — with the caveat that "the number of
steps required to change the indoor temperature by one degree depends on your
heating system. Usually one step is enough but in some cases several may be
required" (p. 37).

### What NIBE does not document

**Whether the offset still applies when the curve is set to 0** — that is, when
your own curve points are in force. Seven NIBE manuals were checked; the own-curve
section (menu 1.30.7) says nothing about it in either direction.

**How quickly the calculated supply temperature follows a change.** Nothing in
any manual describes ramping or filtering of register 31018.

### What we measured

On an S735-family pump running an own curve (curve = 0), min supply 26 °C, no room
sensor, 13 °C outdoors, holding each offset for six minutes and sampling every ten
seconds:

| Offset | Calculated supply |
|---|---|
| −6 | 26.0 °C — pinned exactly at min supply |
| 0 | 30.4 °C |
| +6 | 39.0 °C |

So on this pump: **the offset does apply to an own curve**, plus is warmer, and
one step is worth roughly **1.5 °C of supply temperature** — the same order as
NIBE's example, not the same number. Their text says "e.g.", not "equals".

**The ramp is the practical finding.** After setting +6 the value moved 29.5 →
30.3 → 32.8 → 35.5 → 38.1 → 39.0 over about five minutes. An earlier measurement
that read the register ten seconds after each write concluded 0.2 °C per step and
was wrong by a factor of eight. If you check whether a write "worked" by reading
the calculated supply straight afterwards, you will measure the ramp, not the
setting.

Downwards, −6 hit 26.0 °C immediately and stayed there — the minimum supply, doing
exactly what the manual says it does. On an own curve already sitting near its
floor in mild weather, lowering the offset does nothing at all.
