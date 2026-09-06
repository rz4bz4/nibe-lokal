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
}

# Ranges of addresses that are blocked wholesale.
BLOCKED_RANGES: list[tuple[int, int, str]] = [
    (40121, 40135, "floor drying programme - runs 20-70 C for weeks, never start this from a phone"),
    (41100, 41140, "smart energy source / electricity price control - misconfigured means "
                   "constant additional heat"),
    (45218, 45230, "external sensor value injection - if this app stops, the pump keeps "
                   "regulating on a frozen fake reading"),
    (45988, 45988, "external sensor value injection - same"),
    (44165, 44175, "compressor frequency blocking bands"),
    (40943, 40944, "compressor frequency blocking bands"),
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
