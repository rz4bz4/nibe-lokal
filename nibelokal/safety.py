"""What this app is allowed to change, and what it refuses to touch.

The pump itself has no opinion: once Modbus write is enabled in menu 7.5.9,
every holding register is writable, including the ones that quietly turn your
heat pump into a very expensive electric radiator. So the gate lives here.

Three tiers:

  EVERYDAY  - the things you actually want from a phone. Accepted without
              confirm=true.
  GUARDED   - real settings with real consequences. Require an explicit
              confirm=true on the request, and are logged.
  BLOCKED   - never written by this app, at any tier. Change these on the
              pump's own display, where you can see what you are doing.

This file is a rule about /api/write: what it will accept, and with what
ceremony. The project's principle is "no write to the pump without an explicit
confirm", and the everyday tier is where that principle is spent, not where it
is upheld by some second layer. An earlier version of this docstring claimed
the second layer existed; it does not.

What is actually true of the app, checked against web/index.html rather than
remembered: every write that goes through /api/write - the heating step, every
row in the settings list - opens a dialog first, showing the register, the old
and the new value and how long to wait, and sends confirm=true whatever tier
the register is in. Two buttons do not: extra hot water and the ventilation
boost are one tap and no dialog, and they post to /api/hotwater and
/api/ventilation, which write 40226/40698 and 40105/40116-40119 with
confirmed=False. Those are everyday registers, and that is exactly what the
everyday tier is for: a boost that expires by itself, where the tap *is* the
confirm and a second dialog would only teach people to dismiss dialogs.

So the honest sentence is: nothing with consequences is written without an
explicit confirm. The everyday tier is the list of registers this project has
decided have none - which is also what lets something that is not the web app,
a script or a Homey flow or curl, start a ventilation boost without asserting
that it has understood consequences it does not have. Anything else is guarded,
and there the assertion is the point.

Addresses are NIBE coil addresses (4xxxx = holding). Sources for the risk
assessment are in docs/registers.md, including the registers that are close
relatives of blocked ones and stay guarded anyway - the 0/1 per-heat-pump
circulation and charge pump modes (40749, 40784 and their siblings) and 40767.

The tiers were first written against one S735, but this app supports the whole
S series and the models do not agree on addresses: the same setting sits at
40096 on an S735 and at 47138 on an SMO 40, and the S735's floor drying at
40121-40135 is 47276-47290 on an SMO 20. So a category the project has decided
is dangerous is blocked at every address any supported model uses for it, not
only at the S735's. Blocking an address a given model does not implement costs
that model nothing; leaving one open costs the model that does implement it. See
tests/test_safety.py, which walks every map the `nibe` package ships and fails
if a category is blocked on one model and reachable on another.
"""
from __future__ import annotations

from . import sv_number

# --- tier 1: everyday -------------------------------------------------------

EVERYDAY: dict[int, str] = {
    40226: "extra hot water, number of minutes",
    40698: "extra hot water on/off",
    40057: "hot water comfort mode (SMALL/MEDIUM/LARGE/SMART)",
    40105: "ventilation mode 0-4",
    40116: "return time, fan mode 4",
    40117: "return time, fan mode 3",
    40118: "return time, fan mode 2",
    40119: "return time, fan mode 1",
    40207: "room setpoint, climate system 1",
    40208: "room setpoint, climate system 2",
    40209: "room setpoint, climate system 3",
    40210: "room setpoint, climate system 4",
    40067: "periodic hot water interval, days",

    # --- the same everyday settings on the F generation ---------------------
    #
    # These tiers are consulted with the address that is actually written, and
    # on an F-series pump that is the physical address -- see profile.py. The
    # rows above are the S-series numbering and mean nothing there.
    #
    # This is not cosmetic. Two buttons in this app write without a dialog:
    # extra hot water and the ventilation boost both call
    # pump.write(confirmed=False). A register that is not in this dict falls
    # through to the guarded default at the bottom of tier(), and a guarded
    # write with no confirm is *refused*. So without these rows the ventilation
    # buttons on an F-series pump would not ask for confirmation -- they would
    # simply stop working, with a refusal quoting a register number the owner
    # has never seen.
    47041: "hot water comfort mode (ECONOMY/NORMAL/LUXURY/SMART) - 40057 on an "
           "F-series pump. Same four keys 0/1/2/4, different words",
    47260: "fan mode 0-4 - 40105 on an F-series pump. Written by the "
           "ventilation button with no dialog",
    47274: "return time, fan mode 1 - 40119 on an F-series pump",
    47273: "return time, fan mode 2 - 40118",
    47272: "return time, fan mode 3 - 40117",
    47271: "return time, fan mode 4 - 40116. These four are what make a "
           "ventilation boost self-cancelling, which is what earns the "
           "everyday tier in the first place",
    47398: "room setpoint, climate system 1 - 40207 on an F-series pump",
    47397: "room setpoint, climate system 2 - 40208",
    47396: "room setpoint, climate system 3 - 40209",
    47395: "room setpoint, climate system 4 - 40210",
    47051: "periodic hot water interval, days - 40067 on an F-series pump",
    # The F generation's whole extra-hot-water feature in one register: 0 off,
    # 1 = 3 h, 2 = 6 h, 3 = 12 h, 4 = a one-time increase. This app's own
    # button will not write it, because there is no honest way to turn "180
    # minutes" into one of those five without guessing (see
    # profile.NO_F_EQUIVALENT and pump.extra_hot_water). It is classified here
    # anyway, and everyday rather than guarded, for the same reason 40226 and
    # 40698 are: it counts down and stops by itself, and an owner who knows
    # which of the five they want should be able to say so from a script or a
    # Homey flow without asserting they understand consequences it does not
    # have.
    48132: "temporary luxury hot water, 0 off / 1 = 3 h / 2 = 6 h / 3 = 12 h / "
           "4 = one-time increase - what 40226 and 40698 are together on an "
           "F-series pump, as one enumeration instead of minutes and a switch",
}

# --- tier 2: guarded --------------------------------------------------------

GUARDED: dict[int, str] = {
    40027: "heating curve, climate system 1 - a wrong curve costs power for weeks",
    40028: "heating curve, climate system 2",
    40029: "heating curve, climate system 3",
    40030: "heating curve, climate system 4",
    40031: "heating offset, climate system 1 - immediate effect on every radiator",
    40032: "heating offset, climate system 2",
    40033: "heating offset, climate system 3",
    40034: "heating offset, climate system 4",
    40035: "min supply temperature, climate system 1",
    40039: "max supply temperature, climate system 1",
    # All seven hot water start/stop temperatures. Three of them - the two for
    # the low comfort mode and the periodic increase - were missing from this
    # dict while docs/registers.md listed the whole run 40059-40065. They landed
    # on guarded anyway, by the default at the bottom of tier(); listing them
    # changes no tier, only the reason a refusal quotes.
    40059: "start temperature, hot water high",
    40060: "start temperature, hot water normal",
    40061: "start temperature, hot water low",
    40062: "stop temperature, hot water periodic increase (the legionella cycle)",
    40063: "stop temperature, hot water high",
    40064: "stop temperature, hot water normal",
    40065: "stop temperature, hot water low",
    40103: "max internal additional heat (kW of immersion heater)",
    40181: "permit additional heat for heating",
    40186: "auto mode, additional heat stop temperature",
    40238: "operating mode (0 auto, 1 manual, 2 ADDITION ONLY = pure electric heat)",
    45010: "supply line setpoint override, climate system 1",
    # Clearing an alarm is not a setting, it is throwing away the one piece of
    # evidence there is. The pump keeps running afterwards, usually on the
    # immersion heater, and the next person to look sees a healthy pump. This
    # app never resets an alarm by itself -- alarms.py has no write in it and
    # no button offers to -- but /api/write will write any writable register it
    # is asked to, and this one is writable. Guarded is what makes that a
    # decision: an explicit confirm=true, from somebody who typed the register
    # number, and refused outright with allow_guarded_writes: false.
    40023: "reset alarm - clears the alarm without fixing it, and the app then "
           "has nothing left to notice. Read what the alarm says first",
    40106: "exhaust air fan speed, mode 4 (%)",
    40107: "exhaust air fan speed, mode 3 (%)",
    40108: "exhaust air fan speed, mode 2 (%)",
    40109: "exhaust air fan speed, mode 1 (%)",
    40110: "exhaust air fan speed, normal (%)",
    # SG Ready, all of it, and none of it is an input this app should write
    # freely. 40761/40762/40763 are the pump's own SG Ready *menu* - "may SG
    # Ready affect the heating / the cooling / the hot water", u8 0/1, default
    # 1 on every S-series map. They decide what the SG Ready input is allowed
    # to touch; they are not that input. Until 2026-09-07 40761 sat in
    # EVERYDAY described as "SG Ready / smart grid input", so the one register
    # of the three that turns the heating influence off was the one written
    # without a confirm, while its two siblings needed one. Same feature, same
    # tier now.
    40761: "let SG Ready affect the heating (0/1) - the pump's own SG Ready menu, "
           "not the SG Ready input; 0 makes the input a no-op for heating",
    40762: "let SG Ready affect the cooling - sibling of 40761",
    40763: "let SG Ready affect the hot water - sibling of 40761",
    48282: "let SG Ready affect the heating - 40761 on an SMO 20/40 and the whole "
           "F generation, where the title reads 'SG Ready heating'",
    # The input itself. 43033 arms SG Ready over Modbus at all and 46009 says
    # which of the four SG Ready states to request. These are what an
    # electricity-price signal would actually drive, and what spot.py means
    # when it says the effect depends on settings this app cannot read back:
    # the amounts live behind 40761-40763 and 41053 and in menus that are not
    # on Modbus at all.
    43033: "activate SG Ready over Modbus - this is the SG Ready input, and what "
           "the pump does with it is configured on its own display",
    46009: "requested SG Ready operating mode, 0-3 - the state the input asks for; "
           "the map publishes no enumeration for it",
    41053: "max internal additional heat while SG Ready is running (kW) - the "
           "immersion heater ceiling 40103 is, under another name",

    # --- the same guarded settings on the F generation ----------------------
    #
    # Mirrors of the rows above at the addresses an F-series pump uses, for the
    # reason the whole file already gives for the SMO 20/40: a category this
    # project has decided is dangerous is named at every address a supported
    # model uses for it, not only at the S735's.
    #
    # Unlike the everyday block, none of these *changes* a tier: anything not
    # classified is guarded already. What they change is the sentence a refusal
    # quotes. "Register 47007 är en skyddad inställning: heating curve,
    # climate system 1" is a sentence somebody can act on; the empty reason
    # that the default produced is not.
    47007: "heating curve, climate system 1 - 40027 on an F-series pump. A wrong "
           "curve costs power for weeks",
    47006: "heating curve, climate system 2 - 40028",
    47005: "heating curve, climate system 3 - 40029",
    47004: "heating curve, climate system 4 - 40030",
    47011: "heating offset, climate system 1 - 40031 on an F-series pump. "
           "Immediate effect on every radiator",
    47010: "heating offset, climate system 2 - 40032",
    47009: "heating offset, climate system 3 - 40033",
    47008: "heating offset, climate system 4 - 40034",
    47015: "min supply temperature, climate system 1 - 40035 on an F-series pump",
    47014: "min supply temperature, climate system 2",
    47013: "min supply temperature, climate system 3",
    47012: "min supply temperature, climate system 4",
    47019: "max supply temperature, climate system 1 - 40039 on an F-series pump",
    47018: "max supply temperature, climate system 2",
    47017: "max supply temperature, climate system 3",
    47016: "max supply temperature, climate system 4",
    # The own curve's seven points and the point offset. Guarded on both
    # generations, and named here because the advisor writes them as a group.
    47026: "own curve point P1 (-30 C) - 40046 on an F-series pump",
    47025: "own curve point P2 (-20 C) - 40045",
    47024: "own curve point P3 (-10 C) - 40044",
    47023: "own curve point P4 (0 C) - 40043",
    47022: "own curve point P5 (+10 C) - 40042",
    47021: "own curve point P6 (+20 C) - 40041",
    # No temperature on this one, deliberately. NIBE's F750 and F1155 user
    # manuals show menu 1.9.7 with six points ending at +20; the seventh
    # register exists and what weather it governs does not appear in either
    # book. The S-series row above it says +30 because NIBE's menu 1.30.7 does.
    # See profile.OWN_CURVE_OUTDOOR and docs/f-series.md.
    47020: "own curve point P7, the warm end - 40040. NIBE's F-series manuals "
           "list six points (-30..+20); the outdoor temperature of the seventh "
           "is not documented",
    47027: "point offset, outdoor temperature - 40047 on an F-series pump",
    47028: "point offset, degrees - 40048",
    # The hot water temperatures. Both the start and the stop set, because on
    # the F generation they are seven writable registers in one run and the
    # S-series reason -- above roughly 50-55 C the compressor cannot get there
    # alone and the immersion heater finishes every cycle -- applies to all of
    # them. The S rows name the modes small/normal/high; the F map names the
    # same three economy/normal/luxury.
    47043: "start temperature, hot water luxury - 40059 in the S numbering, "
           "where the same mode is called 'high'",
    47044: "start temperature, hot water normal - 40060",
    47045: "start temperature, hot water economy - 40061, 'low' there",
    47046: "stop temperature, hot water periodic increase (the legionella "
           "cycle) - 40062 on an F-series pump",
    47047: "stop temperature, hot water luxury - 40063, 'high' there",
    47048: "stop temperature, hot water normal - 40064",
    47049: "stop temperature, hot water economy - 40065, 'low' there",
    47212: "max internal additional heat (kW of immersion heater) - 40103 on an "
           "F-series pump",
    47370: "permit additional heat for heating - 40181 on an F-series pump",
    47376: "auto mode, additional heat stop temperature - 40186",
    47137: "operating mode (0 auto, 1 manual, 2 ADDITION ONLY = pure electric "
           "heat) - 40238 on an F-series pump, and the same value 2",
    47265: "exhaust air fan speed, normal (%) - 40110 on an F-series pump. On an "
           "exhaust-air F730 or F750 the ventilation is the heat source",
    47264: "exhaust air fan speed, mode 1 (%) - 40109",
    47263: "exhaust air fan speed, mode 2 (%) - 40108",
    47262: "exhaust air fan speed, mode 3 (%) - 40107",
    47261: "exhaust air fan speed, mode 4 (%) - 40106",
    # SG Ready. 48282 is already listed above, from the S-series side, as the
    # SMO 20/40 and F-generation spelling of 40761. Its two siblings were
    # missing, which is the same half-a-pair mistake the note on 40761 is
    # about, pointing at the other generation.
    48283: "let SG Ready affect the cooling - 40762 on an F-series pump, and the "
           "sibling of the 48282 already listed above",
    48284: "let SG Ready affect the hot water - 40763",
    48914: "max internal additional heat while SG Ready is running (kW) - 41053 "
           "on an F-series pump; the immersion heater ceiling 47212 is, under "
           "another name",
    # 45171 exists on every F map and on no S map, so it was guarded by the
    # default at the bottom of tier() and by nothing anybody had decided. Its
    # S-series twin 40023 is listed above with the reason; the same reason
    # holds here, and the mirror image of the 45001 problem is why it matters
    # that it is written down: on the S generation 45001 is forced control and
    # blocked, on the F it is the read-only alarm number, and this is the
    # register next to it that really does clear an alarm. See
    # docs/f-series.md, "45001 means opposite things on the two generations".
    45171: "reset alarm - 40023 on an F-series pump. Clears the alarm without "
           "fixing it, and the app then has nothing left to notice. Read what "
           "the alarm says first",
    # Degree minutes, which the F generation publishes twice: 43005 in 16 bits
    # and 40940 in 32, same unit, same factor, same range, both marked
    # writable. Both are guarded already, by the default at the bottom of
    # tier(), and both were guarded with an empty reason -- so `nibelokal read
    # 40012` on an F pump refused a write with the sentence "Register 43005 ar
    # en skyddad installning: ." Naming them changes no tier; it changes a
    # refusal that says nothing into one somebody can act on.
    #
    # The reason is not the S-series one. Degree minutes is the pump's own
    # running account of how far behind the heating is, and writing it is
    # telling the compressor a lie about the weather: too negative starts it
    # now, too positive stops it. What is worse here than on the S series is
    # that nobody knows which of the two registers the regulation follows, or
    # whether writing either does anything at all rather than being overwritten
    # on the next control cycle -- see docs/f-series.md, "Degree minutes". A
    # guarded write is refused without a confirm, and that is the right
    # ceremony for a register whose effect is not established.
    43005: "degree minutes, 16-bit - 40012 on an F-series pump, and the one "
           "NIBE's own MODBUS 40 manual names. This is the pump's running "
           "account of how far behind the heating is; writing it starts or "
           "stops the compressor. Which of 43005 and 40940 the regulation "
           "follows is not established",
    # 40940 is two different registers, and the reason has to say so, because
    # this dict is consulted with the physical address and nothing above it
    # knows which pump is on the other end. On the F generation it is degree
    # minutes at full resolution, the other half of 43005. On every S map that
    # has it -- S735, S1155, SMO S40 -- it is EB103/104-GP12, a charge pump,
    # writable, and nothing to do with degree minutes. Guarded on both, which
    # it already was; one sentence for both, which it was not.
    40940: "degree minutes, 32-bit on an F-series pump - the other half of "
           "43005, full resolution, same consequence, and the same open "
           "question about which one the pump regulates on. On an S-series "
           "pump this same address is EB103/104-GP12, a charge pump, and "
           "setting a pump speed by hand is its own way to trip an alarm",
}

# --- tier 3: blocked --------------------------------------------------------

BLOCKED: dict[int, str] = {
    40089: "min compressor frequency - outside the compressor's window means wear and alarms",
    40090: "max compressor frequency - same",
    40096: "heating medium pump operating mode - stopping circulation during compressor "
           "operation triggers high-pressure alarms",
    40097: "brine pump operating mode - the same selector on the cold side, on an "
           "S1155/S1255 and S1156/S1256. Same u8, same enumeration the F-series map "
           "spells out for its twin 47139 (10 intermittent, 20 continuous, "
           "30 economy, 40 auto), same failure with the pressures the other way up",
    40696: "manual heating medium pump speed - same risk",
    42741: "AUX function selector via Modbus - can silently block the compressor",
    42742: "AUX on/off via Modbus - same",
    40104: "main fuse rating - this is what current limiting protects; raising it "
           "removes the protection",
    40981: "current transformer ratio - same protection, from the other end",
    45001: "forced control - the service menu's manual override of compressor and valves",
    40904: "initiate inverter",
    40905: "force initiated inverter",
    42743: "start guide state - writing 0 leaves the pump's display stuck in the "
           "start guide",
    45009: "follow externally calculated supply - if this app stops, the pump keeps "
           "regulating on a frozen setpoint",
    40219: "circulation pump speed for heating - same risk as the pump operating mode",
    43029: "immersion heater power in emergency mode",
    43059: "additional heat step in emergency mode - what 43029 is on an SMO S40, "
           "which has no immersion heater of its own",

    # The same settings, at the addresses other supported models use for them.
    # The SMO 20 and SMO 40 are in this app's model list and number almost
    # nothing the way an S735 does; a gate that only knows one model's numbering
    # is not a gate.
    40683: "manual heating medium pump speed - 40696 on an SMO S40 and a VVM S500",
    47138: "heating medium pump operating mode - 40096 on an SMO 20/40",
    48456: "heating medium pump operating mode for cooling - the same selector as "
           "47138 with the same two settings, 10 intermittent and 20 continuous, "
           "applied to cooling operation. F-series only",
    47214: "main fuse rating - 40104 on an SMO 20/40",
    48085: "heat medium pump manual speed - 40696 on an SMO 20/40",
    48130: "manual heat medium pump speed - the SMO 20/40's second name for the "
           "same thing as 48085; both are writable",
    48567: "initiate inverter - 40904 on an F750. Reachable since the F-series "
           "profile and RTU framing landed, and blocked for the same reason as 40904",
    48568: "force inverter initiation - 40905 on an F750, and the second half of the "
           "pair 48567 is the first half of. Blocking one of two registers that do "
           "the same job is the mistake this whole section is about",
    48755: "current transformer ratio - 40981 on an SMO 40",
    47139: "brine pump operating mode - 40097 in the F-generation numbering, on the "
           "six F1x45/F1x55 maps. Same selector, same hazard, same tier",
}

# Ranges of addresses that are blocked wholesale.
BLOCKED_RANGES: list[tuple[int, int, str]] = [
    (40212, 40216, "AUX input function selectors - the same selector as 42741, which "
                   "can silently block the compressor"),
    (40768, 40768, "AUX input function selector - same"),
    (41556, 41558, "AUX input function selectors - same"),
    (40121, 40135, "floor drying programme - runs 20-70 C for weeks, never start this from a phone"),
    (47276, 47291, "floor drying programme - the same programme at the addresses the "
                   "SMO 20/40 map uses, plus its timer at 47291"),

    # Forced control. 45001 (Activate forced control) was blocked from the
    # start; on an S1155/S1255, S320/S325 and an SMO S40 the individual outputs
    # it drives are writable one by one at 45027-45031 - the immersion heater
    # relay AZ30-EB17, the reversing valve QN37 open and closed, and the two
    # circulation pumps GQ2 and GQ3. Blocking the switch and leaving the wires
    # is not a gate.
    (45027, 45031, "forced control of individual outputs (AZ30-EB17/QN37/GQ2/GQ3) - "
                   "the service menu's manual override, one relay at a time"),

    # External sensor injection. Each of these feeds the pump a temperature this
    # app supplies in place of a real sensor, and the pump has no dead-man's
    # switch: if the app stops, crashes, or is unplugged, the pump goes on
    # regulating on the last number it was handed, forever.
    #
    # Each sensor has two registers, an activation flag and a value, and where
    # they sit relative to each other is not one rule. An earlier version of
    # this comment claimed "the flag sits one below the value"; that is true in
    # the 45987/45988 and 46004-46007 blocks and false in the 452xx block, where
    # the flags are their own run - BT1/BT25/BT71/BT5/BT6/BT7 at 45209-45214 and
    # BT52 at 45217, nine below their values at 45218-45223 and 45226. So both
    # runs are blocked outright rather than derived from each other. The flags
    # matter as much as the values: a flag left at 1 with a stale value behind it
    # is the failure this whole block exists to prevent.
    #
    # 45209-45214 are writable on an S1156/S1256, an SMO S40 and - 45211 alone -
    # a VVM S500; on an S735 the same flags are read-only mirrors at
    # 35209-35214. Blocked on every model, because a map that does not list a
    # register is a map, not a promise.
    (45209, 45230, "external sensor injection: the activation flags and the values for "
                   "BT1 outdoor, BT5/BT6/BT7, BT25, BT52, BT71 - the pump keeps "
                   "regulating on a frozen fake reading if this app stops"),
    (45987, 45988, "external sensor injection: BT50, the room temperature the heating "
                   "curve regulates against - same failure, closest to the house"),
    # 46004 is the BT68 activation flag and was reachable until 2026-09-06: the
    # range started at 46005, its value. (The map calls 46007 "value BT68" as
    # well, next to 46006 "BT69 activated"; one of those two titles is wrong in
    # every S-series export. Both are blocked, so it does not matter here.)
    (46004, 46007, "external sensor injection (BT68, BT69) - flags and values, same"),

    # Smart Energy Source. The old range started at 41100, which was simply
    # wrong: 41100-41105 are Reduced ventilation, High outdoor temperature, OEK
    # and Smart home room control. SES itself starts at 41106 and is not
    # contiguous - the tariff calendars sit in four scattered quads.
    (41106, 41140, "smart energy source / electricity price control - misconfigured means "
                   "constant additional heat"),
    (41173, 41176, "smart energy source, fixed tariff calendar"),
    (41209, 41212, "smart energy source, OPT10 tariff calendar"),
    (41245, 41248, "smart energy source, shunt additional heat tariff calendar"),
    (41281, 41284, "smart energy source, external step additional heat tariff calendar"),
    # 41327-41328 are SES too, and the titles say so only in the abbreviation:
    # "Max difference, SES priority 1 energy source". They are the degree-minute
    # differences at which the pump hands over to a lower-priority source, which
    # on most installations means the immersion heater. Writable on all fifteen
    # S-series maps including the S735 the tiers were written against, so this
    # one was not model blindness - it was the range stopping at 41284.
    (41327, 41328, "smart energy source, the differences at which it hands over to a "
                   "lower-priority source - set these wrong and additional heat runs "
                   "constantly"),
    # The same feature on an SMO 40, where it is one contiguous run. 48976 sits
    # just below it and is left guarded on purpose: it is Smart home room
    # control, a different feature that happens to be a neighbour.
    (48979, 49009, "smart energy source / electricity price control at the addresses the "
                   "SMO 40 map uses - misconfigured means constant additional heat"),
    # 41327-41328 again, on a VVM 225/310/320/325/500, where the abbreviation is
    # shortened further still: "Max diff. SES prio 1." and "Max diff. SES.other
    # prios". Same registers, same range 1.0-25.0 C, same consequence.
    (49208, 49209, "smart energy source, the differences at which it hands over to a "
                   "lower-priority source, at the addresses the VVM map uses"),

    # 41100-41102 were blocked by accident and stay blocked on purpose. On the
    # measured S735 the map and the pump disagree here: 41100 is typed u8 with
    # range 0-1 and reads 25. A register whose map cannot even be trusted about
    # its own type is not one to write. Those three - Reduced ventilation, High
    # outdoor temperature, OEK - exist as writable registers on the S735 and
    # S735C and nowhere else, so the measured reason covers exactly the models
    # the range now reaches.
    #
    # The range ran to 41105 until 2026-09-07, and that stretched one pump's
    # measurement over two registers it does not describe. 41103 "Smart home
    # room control" is writable on twelve maps and 41104 "Speed, brine pump,
    # standby mode (EP14)" on four; the S735 quirk is not why either was
    # blocked there. Both are now guarded, which is what the same features are
    # already at their other addresses: Smart home room control is 48976 on an
    # SMO 40 and left guarded on purpose (see the note under 48979 below), and
    # the brine pump's other two speeds, 40223 and 40860, are guarded on the
    # very models 41104 is writable on. 41105 is not writable on any map the
    # package ships, so releasing it releases nothing.
    (41100, 41102, "the register map and the pump disagree in this block on the measured "
                   "S735 - wrong type, wrong range, or not implemented at all"),

    # NIBE's own Smart Price Adaption. 46015-46061 (stride 2) are 24 hourly
    # "Energy price" slots that default to INT32_MAX, i.e. unset. They look like
    # an external price feed, but nothing NIBE publishes says what unit the s32
    # holds, which day the 24 slots refer to, or whether the pump honours them
    # without a myUplink account. They schedule the compressor. That is the
    # wrong combination to guess at. Use the offset instead: bounded,
    # reversible, and in units you already read on the display.
    #
    # 40844 arms the feature; 40845-40852 and 40903 are the rest of its menu -
    # heating, hot water and cooling activated, the area code, and how hard each
    # is allowed to push. An earlier version of this comment gave as its
    # decisive reason that the degree of effect "is not on Modbus at all". That
    # was wrong: 40846 is (SPA), heating influence, s8, 1-10, and 40903 is
    # (SPA), hot water influence, s8, 1-4, both writable, both present on the
    # S735 these tiers were written against. The knobs are on Modbus. What is
    # not is the unit of the price they act on, which is the reason that holds.
    # They are blocked with the switch rather than under it: arming SPA on the
    # display and then setting its influence from here reaches the same feature
    # by the other end.
    (40844, 40844, "Smart Price Adaption on/off - the price feed it follows has an "
                   "undocumented unit and an undocumented day, so this app will not "
                   "arm it"),
    (40845, 40852, "Smart Price Adaption's own menu - heating, hot water and cooling "
                   "activated, the area code, and how hard each is allowed to push"),
    (40903, 40903, "Smart Price Adaption, how hard it may push hot water (1-4) - the "
                   "same menu as 40845-40852, fifty-one addresses above the end of "
                   "it"),
    (46015, 46061, "Smart Price Adaption hourly price slots - undocumented unit and "
                   "undocumented day, feeding compressor scheduling"),
    # Compressor frequency blocking bands. Four separate runs, because the maps
    # put EB101, EB102 and the S1155/S1255's start/stop pair in three different
    # places and only the EB102-EB108 run is contiguous.
    (44158, 44175, "compressor frequency blocking bands (EB102-EB108); 44158 is the "
                   "second band for EB102 on an SMO S40, nowhere near the first"),
    (40943, 40948, "compressor frequency blocking bands - the two activation flags, and "
                   "the start and stop of band 1 on an S1155/S1255"),
    (45296, 45297, "compressor frequency blocking bands for EB101 - the built-in "
                   "compressor on everything except an S735"),
]


class Refused(Exception):
    """The write was refused before it reached the pump."""


def tier(address: int) -> str:
    """Return 'everyday', 'guarded' or 'blocked' for a register address."""
    if address in BLOCKED:
        return "blocked"
    for lo, hi, _ in BLOCKED_RANGES:
        if lo <= address <= hi:
            return "blocked"
    if address in EVERYDAY:
        return "everyday"
    if address in GUARDED:
        return "guarded"
    # Anything not explicitly classified is treated as guarded, not as free.
    return "guarded"


def reason(address: int) -> str:
    if address in BLOCKED:
        return BLOCKED[address]
    for lo, hi, why in BLOCKED_RANGES:
        if lo <= address <= hi:
            return why
    return GUARDED.get(address) or EVERYDAY.get(address) or ""


def check(address: int, confirmed: bool, allow_guarded: bool = True) -> None:
    """Raise Refused unless this write is permitted.

    The refusal text is Swedish because it is not a log line: it goes straight
    into a toast on the phone of whoever just pressed the button. The reason
    itself stays English -- it is the tier tables above, which are the file a
    person edits, and translating those would leave the code and the message
    disagreeing about what a register is called.
    """
    t = tier(address)
    if t == "blocked":
        raise Refused(
            "Register %d är spärrat i den här appen: %s. Ändra det på pumpens display."
            % (address, reason(address))
        )
    if t == "guarded":
        if not allow_guarded:
            raise Refused(
                "Register %d är en skyddad inställning (%s) och skyddade skrivningar är "
                "avstängda i config.yaml." % (address, reason(address))
            )
        if not confirmed:
            raise Refused(
                "Register %d är en skyddad inställning: %s. Skicka confirm=true om du "
                "verkligen menar det." % (address, reason(address))
            )


def clamp(register, value: float) -> float:
    """Keep a numeric value inside the register's own documented range."""
    if register.min is not None and value < register.min:
        raise Refused(
            "%s: %s är under pumpens minimum på %s"
            % (register.title, sv_number(value, None), sv_number(register.min, None))
        )
    if register.max is not None and value > register.max:
        raise Refused(
            "%s: %s är över pumpens maximum på %s"
            % (register.title, sv_number(value, None), sv_number(register.max, None))
        )
    return value
