"""What this app is allowed to change, and what it refuses to touch.

The pump itself has no opinion: once Modbus write is enabled in menu 7.5.9,
every holding register is writable, including the ones that quietly turn your
heat pump into a very expensive electric radiator. So the gate lives here.

Three tiers:

  EVERYDAY  - the things you actually want from a phone. Written freely.
  GUARDED   - real settings with real consequences. Require an explicit
              confirm=true on the request, and are logged.
  BLOCKED   - never written by this app, at any tier. Change these on the
              pump's own display, where you can see what you are doing.

Addresses are NIBE coil addresses (4xxxx = holding). Sources for the risk
assessment are in docs/registers.md.

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
    40761: "SG Ready / smart grid input",
    40067: "periodic hot water interval, days",
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
    40059: "start temperature, hot water high",
    40060: "start temperature, hot water normal",
    40063: "stop temperature, hot water high",
    40064: "stop temperature, hot water normal",
    40103: "max internal additional heat (kW of immersion heater)",
    40181: "permit additional heat for heating",
    40186: "auto mode, additional heat stop temperature",
    40238: "operating mode (0 auto, 1 manual, 2 ADDITION ONLY = pure electric heat)",
    45010: "supply line setpoint override, climate system 1",
    40106: "exhaust air fan speed, mode 4 (%)",
    40107: "exhaust air fan speed, mode 3 (%)",
    40108: "exhaust air fan speed, mode 2 (%)",
    40109: "exhaust air fan speed, mode 1 (%)",
    40110: "exhaust air fan speed, normal (%)",
}

# --- tier 3: blocked --------------------------------------------------------

BLOCKED: dict[int, str] = {
    40089: "min compressor frequency - outside the compressor's window means wear and alarms",
    40090: "max compressor frequency - same",
    40096: "heating medium pump operating mode - stopping circulation during compressor "
           "operation triggers high-pressure alarms",
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
    48567: "initiate inverter - 40904 on an F750. This app cannot reach an F-series "
           "pump at all (they need a MODBUS 40 over RS485, not TCP), but the rule "
           "reads better with no exceptions than with one",
    48568: "force inverter initiation - 40905 on an F750, and the second half of the "
           "pair 48567 is the first half of. Blocking one of two registers that do "
           "the same job is the mistake this whole section is about",
    48755: "current transformer ratio - 40981 on an SMO 40",
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

    # 41100-41105 were blocked by accident and stay blocked on purpose. On the
    # measured S735 the map and the pump disagree here: 41100 is typed u8 with
    # range 0-1 and reads 25, and 41103 answers a Modbus exception. A register
    # whose map cannot even be trusted about its own type is not one to write.
    (41100, 41105, "the register map and the pump disagree in this block on the measured "
                   "S735 - wrong type, wrong range, or not implemented at all"),

    # NIBE's own Smart Price Adaption. 46015-46061 (stride 2) are 24 hourly
    # "Energy price" slots that default to INT32_MAX, i.e. unset. They look like
    # an external price feed, but nothing NIBE publishes says what unit the s32
    # holds, which day the 24 slots refer to, or whether the pump honours them
    # without a myUplink account. They schedule the compressor. That is the
    # wrong combination to guess at. 40844 arms the whole feature, and its
    # strength - the degree of effect, 1-10 for heating - is not on Modbus at
    # all, so this app could switch it on and then neither read nor set how hard
    # it pushes. Same objection this app makes to SG Ready. Use the offset
    # instead: bounded, reversible, and in units you already read on the display.
    (40844, 40844, "Smart Price Adaption on/off - its strength is not exposed on Modbus, "
                   "so this app cannot see or set how hard it would push"),
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
    """Raise Refused unless this write is permitted."""
    t = tier(address)
    if t == "blocked":
        raise Refused(
            "Register %d is blocked by this app: %s. Change it on the pump's display."
            % (address, reason(address))
        )
    if t == "guarded":
        if not allow_guarded:
            raise Refused(
                "Register %d is a protected setting (%s) and guarded writes are disabled "
                "in config.yaml." % (address, reason(address))
            )
        if not confirmed:
            raise Refused(
                "Register %d is a protected setting: %s. Send confirm=true if you really "
                "mean it." % (address, reason(address))
            )


def clamp(register, value: float) -> float:
    """Keep a numeric value inside the register's own documented range."""
    if register.min is not None and value < register.min:
        raise Refused(
            "%s: %s is below the pump's minimum of %s" % (register.title, value, register.min)
        )
    if register.max is not None and value > register.max:
        raise Refused(
            "%s: %s is above the pump's maximum of %s" % (register.title, value, register.max)
        )
    return value
