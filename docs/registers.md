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

Never written by this app, on any model. Change them on the pump's display,
where you can see the whole picture and the pump can warn you.

The table is the whole list, and `tests/test_safety.py` fails if it stops being:
it compares the addresses named in the first column against `BLOCKED` and
`BLOCKED_RANGES` in `nibelokal/safety.py` in both directions. There is no
generator — the reasons are the point, and a generator cannot write them — so a
test is what keeps the two in step.

| Register | Setting | Why not |
|---|---|---|
| 40089 / 40090 | Min / max compressor frequency | Outside the compressor's designed window: wear, then alarms. |
| 40096 | Heating medium pump operating mode | Stopping circulation while the compressor runs is a high-pressure alarm. |
| 40219 / 40696 | Circulation pump speed for heating, manual heating medium pump speed | Same failure, by a different route. |
| 40104 / 40981 | Main fuse rating, current transformer ratio | Together these are the current limiting. Raising the fuse or misstating the transformer ratio removes the protection that keeps the house's main fuse intact. |
| 40212–40216, 40768, 41556–41558 | AUX input function selectors (AUX1–AUX9) | The same selector as 42741. Function ids 4 and 5 block the compressor. |
| 42741 + 42742 | AUX function via Modbus | A meta-register: write a function id to one, on/off to the other. Ids 4 and 5 block the compressor. One typo stops the heating, silently. |
| 40121–40135 | Floor drying programme | Runs 20–70 °C for weeks. Never from a phone. |
| 40904 / 40905 | Initiate inverter, force initiated inverter | Service registers for commissioning the inverter, not settings. |
| 40943–40948, 44158–44175, 45296–45297 | Compressor frequency blocking bands | Same category as the frequency limits. Three separate runs because the maps put the built-in compressor (EB101), EB102 and the S1155/S1255's start/stop pair in three different places. |
| 41100–41105 | Reduced ventilation, high outdoor temperature, OEK, smart home room control, standby speed for the brine pump (EP14) | Not dangerous in themselves. Blocked because on the measured S735 the register map and the pump disagree here — 41100 is typed 0–1 and reads 25, and 41103 answers a Modbus exception. A map that is wrong about a register's type is not one to write through. This is the one place the gate knowingly costs a working feature: 41104, the brine pump's standby speed, is writable on an S1155/S1255 and S1156/S1256 and is blocked only because it shares a block this app does not trust. Set it on the pump's display, or move it out of the range in `safety.py`. |
| 41106–41140, 41173–41176, 41209–41212, 41245–41248, 41281–41284, 41327–41328 | Smart energy source / electricity price control | Misconfigured, this is constant additional heat. Not contiguous: the tariff calendars sit in four separate quads, and 41327–41328 sit forty addresses past the last of them. Those two are the degree-minute differences at which the pump hands over to a lower-priority energy source — on most installations, the immersion heater. They are titled *Max difference, SES priority 1 energy source*, so a search for “smart energy source” does not find them; they are writable on every S-series map including the S735 these tiers were written against. |
| 42743 | Start guide state | Writing 0 leaves the pump's display stuck in the start guide. |
| 43029 / 43059 | Immersion heater power and additional heat step, emergency mode | What the pump falls back to when everything else has failed. Not a setting to get wrong from a phone. |
| 45001, 45027–45031 | Forced control | The service menu's manual override. 45001 is the switch; on an S1155/S1255, S320/S325 and an SMO S40, 45027–45031 drive the same outputs one relay at a time — the immersion heater AZ30-EB17, the reversing valve QN37 open and closed, and the circulation pumps GQ2 and GQ3. |
| 45009 | Follow externally calculated supply | Hands the supply temperature to whatever is on the other end of Modbus. If this app stops, the pump keeps regulating on a frozen setpoint. |
| 45209–45230, 45987–45988, 46004–46007 | External sensor value injection, flags and values | Feeds the pump a value you supply instead of a real sensor. There is no dead-man's switch: if this app stops, the pump keeps regulating on a frozen number forever. 45988 is BT50, the room temperature the curve regulates against — the closest one of these to the house. The activation flags are blocked with the values: a flag left at 1 with a stale value behind it is the same failure. |
| 40844–40852, 40903, 46015–46061 | Smart Price Adaption: on/off, its per-circuit activations and influence settings, and its 24 hourly price slots | The whole menu rather than the switch alone — 40846 sets how hard it may push the heating (1–10) and 40903 the hot water (1–4). See the appendix below. |

**The same settings, at the addresses other supported models use.** The tiers
above were first written against one S735, and the models do not agree on
numbering: floor drying is 40121–40135 on an S735 and 47276–47290 on an SMO 20,
and the heating medium pump's operating mode is 40096 on one and 47138 on the
other. Blocking an address a model does not implement costs that model nothing;
leaving one open costs the model that does implement it.

| Register | Setting | Which models use it |
|---|---|---|
| 40683, 47138, 48085, 48130, 48456 | Heating medium pump operating mode and manual speed | 40683 on an SMO S40 and VVM S500. 47138 is the F-generation numbering and is on all seventeen of those maps — the F series, the SMO 20 and SMO 40, and the VVM 225/310/320/325/500 — of which only the two SMOs can be reached over TCP. 48085 and 48130 are on the SMO 20, SMO 40, VVM 310 and VVM 500, and are two writable registers for the same speed. 48456 is the same operating mode again, for cooling, with the same two settings — 10 intermittent, 20 continuous — and exists only on the F series. |
| 47214 / 48755 | Main fuse rating, transformer ratio | The F-generation numbering, so the same seventeen maps as 47138 for the fuse; 48755 is on sixteen of them, every one except the SMO 20. Of those the SMO 20 and SMO 40 are the ones this app can reach. |
| 47276–47291 | Floor drying programme, plus its timer | Again all seventeen F-generation maps, the SMO 20 and SMO 40 among them. |
| 48567 / 48568 | Initiate inverter, force inverter initiation | F750 only, and the same pair as 40904 / 40905 above. This app cannot reach an F-series pump at all — they need a MODBUS 40 over RS485 rather than the TCP this speaks — but the rule reads better with no exceptions than with one, and blocking one of two registers that do the same job is worse than blocking neither. |
| 48979–49009, 49208–49209 | Smart energy source | SMO 40 and the F-generation VVMs, where the feature is one contiguous run rather than the S-series' scattered quads. 48976, *Smart home room control*, sits just below it and is deliberately left guarded: a different feature that happens to be a neighbour. 49208–49209 are 41327–41328 again on a VVM 225/310/320/325/500, abbreviated further to *Max diff. SES prio 1.* — the same two settings, the same 1–25 °C, the same consequence. |

**Anything not listed anywhere is treated as guarded**, not as free. A register
nobody has thought about is not a register to write casually.

Four that stay **guarded** although they are close relatives of blocked ones,
because blocking them would cost a working feature and the risk is not the same:
40767 *Set compressor frequency, cooling* (a bounded 1–100 % setpoint, not a
limit on the compressor's window), 45344 *Silent mode, max. frequency 2 (EB101)*
on an S2125 (a comfort setting with its own menu), 42756 *Inverter fault reset*
(a recovery action rather than a way to force the inverter on), and 40859
*Maximum speed of circulation pump for heating*. That last one is the closest
call: it is the same pump as the blocked 40219, but its own range starts at
50 %, so unlike 40219 — which goes down to 1 % — it cannot be used to stop the
circulation the compressor depends on.

One more family stays **guarded**, and that one is a judgement call rather than a
clear reading. On an S320/S325, S330/S332, S2125, SMO S40 and the VVM S series,
each connected heat pump has its own circulation pump operating mode: 40784 and
40783 for heating, 40800 and 40799 for hot water, 40816 for pool, 40833 and 40832
for cooling. By title they belong with the blocked 40096. By content they may
not. 40096 runs 10–40, the four values NIBE documents as intermittent,
continuous, economy and auto; these are a plain 0–1 with no mapping and no info
text in any map the `nibe` package ships, and nothing there says whether 0 means
*off* or *intermittent*. Those two readings differ by exactly the hazard 40096 is
blocked for. Blocking on the worse one would cost a working setting on models the
author does not own, so they are guarded rather than blocked: `confirm: true`,
range-checked, logged. If you know which it is on an S320, that is a good issue to
open.

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

### What we measured, and why it is a floor rather than a number

On an S735-family pump running an own curve (curve = 0), min supply 26 °C, no
room sensor, 13 °C outdoors, holding each offset for six minutes and sampling
every ten seconds:

| Offset | Calculated supply after six minutes |
|---|---|
| −6 | 26.0 °C — pinned exactly at min supply |
| 0 | 30.4 °C |
| +6 | 39.0 °C |

Two things this does establish, and they are the ones worth having:

- **The offset does apply to an own curve.** NIBE's manuals do not say either
  way, and this pump answers the question: plus is warmer, on curve 0.
- **The value ramps.** After setting +6 the calculated supply moved 29.5 → 30.3
  → 32.8 → 35.5 → 38.1 → 39.0 over about five minutes. An earlier run that read
  the register ten seconds after each write concluded 0.2 °C per step and was
  wrong by more than an order of magnitude.

What it does **not** establish is the size of a step. Dividing 13 °C by six
steps gives 1.5 °C, which is tidy and wrong: at the six-minute cutoff the value
was still climbing at roughly 0.6 °C per minute. The run was stopped before the
ramp had flattened, so 1.5 °C/step is a lower bound on a value that had not
finished arriving — not a measurement of it. NIBE documents about 2.5 °C, and
this pump's owner puts it at about 2.5 °C from years of living with it. Those
two agree; the short measurement is the odd one out, and the short measurement
is the one with a known defect.

**So: about 2.5 °C of supply temperature per step**, per NIBE and per the
owner. This app uses 2.5. A proper measurement would hold each offset for at
least half an hour and take the value only after it has been flat for several
samples; if you do that on your own pump, the number you get is better than the
number here.

Downwards, −6 hit 26.0 °C immediately and stayed there — the minimum supply,
doing exactly what the manual says it does. On an own curve already sitting near
its floor in mild weather, lowering the offset does nothing at all.

### One thing that could have explained the discrepancy, and did not

NIBE firmware 4.0.10 notes: "Removed the use of heat curves and offset when
Smart Room Comfort running under normal conditions." If Smart Room Comfort were
active, the offset would have been partly bypassed and a small measured effect
would be expected. It is not active on this pump: Smart Room Comfort regulates
on a room sensor, and register 40203 (*use room sensor, climate system 1*) reads
0, with no external BT50 being written (45988 is unset). Checked read-only,
2026-09-06. The explanation for the small number is the ramp, not the firmware.

## Appendix: Smart Price Adaption, and why this app does not use it

This pump exposes NIBE's own price-following feature over Modbus: register
**40844** (*Activated (Smart Price Adaption)*, writable, 0/1, currently 0),
**31919** (*Operating mode*, read-only, currently 10, no published
enumeration), **40845–40852** and **40903** (the feature's own menu: what it may
act on and how hard, all writable), and **46015–46061** in steps of two —
twenty-four slots titled
*Energy price 00:00 – 01:00* through *23:00 – 00:00*, writable s32, each
defaulting to 2147483647, which is INT32_MAX and the conventional "unset".

Twenty-four writable hourly price slots defaulting to a sentinel look exactly
like an external price feed, and NIBE's marketing pages do mention manual price
entry. If that inference is right, this app could drive NIBE's own optimiser
without a myUplink subscription, which would be the neatest possible answer to
the question this whole project exists to answer.

It is still an inference, and these are the reasons it is not acted on:

- NIBE's official S-series Modbus document (M12676EN) does not list 40844,
  31919 or 46015–46061 at all. They appear only in per-model ModbusManager
  exports.
- Nothing published says what unit the s32 holds — öre, thousandths of a
  currency unit, something else — nor which day the twenty-four slots refer to,
  nor when they roll over.
- At least one S-series owner has dumped every register looking for a way to
  pass in electricity prices over Modbus and reported finding none.
A fourth reason stood here until 2026-09-07 and was wrong. It said the *degree of
effect* — how hard SPA is allowed to push, 1–10 for heating and 1–4 for hot water
— did not appear in the register map at all, so this app could arm the feature and
then neither read nor set its strength. It does appear, and on the pump these
tiers were written against: **40846** *(SPA), heating influence*, s8, range 1–10,
default 5, writable, and **40903** *(SPA), hot water influence*, s8, range 1–4,
default 2, writable. Next to them sit **40845** *heating activated*, **40847**
*hot water activated*, **40850** *cooling activated*, **40849** and **40851**, the
pool and cooling influences, and **40852** *(SPA), area*. The knobs are all there.
What is missing is the number they act on, which is the objection above and the
one the decision actually rests on.

So 40844–40852, 40903 and 46015–46061 are blocked — the switch, the whole menu
behind it, and the price slots. The menu goes with the switch because arming SPA
on the pump's display and then setting its influence to 10 from here is the same
feature reached from the other end, and blocking one half of a pair is the mistake
the forced-control note above is about.

Price following is done with the heating offset instead: ±1 step, symmetric,
summing to zero over a day, bounded, reversible, and expressed in the same units
already on the pump's own display. Nothing was written to any of these registers
to find this out.

If you know what the unit of 46015 is, that is a good issue to open.
