"""The heat pump as an object: grouped reads, guarded writes, a full backup."""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading

from .modbus import MAX_REGS_PER_QUERY, ModbusError, ModbusOffline, ModbusTCP
from .registry import Register, Registry
from . import safety

log = logging.getLogger("nibelokal.pump")

# The handful of registers the dashboard shows. Everything else is available
# through /api/register, but this is what gets polled every cycle.
# Registers the dashboard polls. Anything a given pump does not implement is
# dropped automatically after the first read, so this list can be generous.
DASHBOARD = [
    # Verified against an S735-family pump on 2026-09-06. Registers a given pump
    # does not implement are dropped automatically after the first read, so this
    # list can name more than any one pump has.
    30002,   # Outdoor temperature (BT1)
    30006,   # Supply line (BT2)
    30008,   # Return line (BT3)
    30009,   # Hot water top (BT7)
    30010,   # Hot water charging (BT6)
    30020,   # Exhaust air (BT20)
    30117,   # Room average temperature, climate system 1 (BT50)
    31047,   # Compressor frequency
    31079,   # More hot water status
    31975,   # Fan speed
    31976,   # Alarm number
    32134,   # Exhaust air fan speed (GQ2)
    40012,   # Degree minutes
    40057,   # Hot water mode
    40067,   # Periodic hot water interval
    40105,   # Ventilation mode
    40110,   # Exhaust air fan speed, normal
    40226,   # More hot water, minutes
    31026,   # Total run time, additional heat
    31028,   # Power, internal additional heat
    31088,   # Total run time, compressor
    31092,   # Total run time, compressor hot water
    31535,   # Compressor, number of starts
]

FAN_SPEED_REGISTER = {0: 40110, 1: 40109, 2: 40108, 3: 40107, 4: 40106}
FAN_RETURN_REGISTER = {1: 40119, 2: 40118, 3: 40117, 4: 40116}

R_MORE_HW_MINUTES = 40226
R_MORE_HW = 40698
R_VENT_MODE = 40105


class Pump:
    def __init__(self, host: str, port: int, unit: int, registry: Registry,
                 allow_guarded: bool = True, timeout: float = 5.0):
        self.registry = registry
        self.allow_guarded = allow_guarded
        self.mb = ModbusTCP(host, port, unit, timeout)
        self.host = host
        self.port = port
        self._lock = threading.RLock()
        self._missing: set[int] = set()   # registers this pump answered "illegal address" for

    # -- reading ---------------------------------------------------------

    def read(self, address: int):
        reg = self.registry.get(address)
        if reg is None:
            raise KeyError("register %d is not in the map (%s)" % (address, self.registry.source))
        words = self.mb.read(reg.kind, reg.wire, reg.count)
        return reg.decode(words)

    def read_many(self, addresses: list[int]) -> dict[int, dict]:
        """Read a set of registers, batching contiguous runs into single queries."""
        wanted: list[Register] = []
        for a in addresses:
            r = self.registry.get(a)
            if r is not None and a not in self._missing:
                wanted.append(r)

        out: dict[int, dict] = {}
        for kind in (3, 4):
            group = sorted([r for r in wanted if r.kind == kind], key=lambda r: r.wire)
            for block in _blocks(group):
                self._read_block(kind, block, out)
        return out

    def _read_block(self, kind: int, block: list[Register], out: dict) -> None:
        start = block[0].wire
        span = block[-1].wire + block[-1].count - start
        try:
            words = self.mb.read(kind, start, span)
        except ModbusError as exc:
            if len(block) == 1:
                # A single register the pump does not have. Remember it and stop
                # asking. Measured on an S735-family pump: it answers exception 1
                # (illegal function), not the textbook 2 (illegal data address),
                # for registers it does not implement -- so treat both as absent.
                if exc.code in (1, 2):
                    self._missing.add(block[0].address)
                out[block[0].address] = {"error": str(exc)}
                return
            # A hole somewhere in the block: fall back to reading each one.
            for reg in block:
                self._read_block(kind, [reg], out)
            return
        for reg in block:
            off = reg.wire - start
            try:
                out[reg.address] = {"value": reg.decode(words[off:off + reg.count])}
            except Exception as exc:                      # noqa: BLE001
                out[reg.address] = {"error": "decode failed: %s" % exc}

    # -- writing ---------------------------------------------------------

    def write(self, address: int, value, confirmed: bool = False) -> dict:
        reg = self.registry.get(address)
        if reg is None:
            raise KeyError("register %d is not in the map" % address)
        if not reg.writable:
            raise safety.Refused("%s (%d) is read-only." % (reg.title, address))

        safety.check(address, confirmed, self.allow_guarded)

        # Order matters: normalise, then range-check the real value, THEN encode.
        # Checking the encoded words instead reads a masked two's-complement
        # integer -- -1 minutes arrives as 65535 and sails past every max.
        value = reg.coerce(value)
        if not reg.mappings:
            safety.clamp(reg, float(value))
        words = reg.encode(value)

        with self._lock:
            before = None
            try:
                before = reg.decode(self.mb.read(reg.kind, reg.wire, reg.count))
            except (ModbusError, ModbusOffline):
                pass
            self.mb.write(reg.wire, words)
            after = reg.decode(self.mb.read(reg.kind, reg.wire, reg.count))

        log.info("write %d (%s): %r -> %r (read back %r)", address, reg.title, before, value, after)
        return {
            "address": address,
            "title": reg.title,
            "requested": value,
            "before": before,
            "after": after,
            "unit": reg.unit,
            "tier": safety.tier(address),
            # A successful read-back is not proof the pump acted on it -- some
            # settings are accepted and then ignored. Verified separately.
            "verified": after is not None,
        }

    # -- everyday actions -------------------------------------------------

    def fan_speeds(self) -> dict[int, float | None]:
        """Percent per ventilation mode, read from the pump.

        The modes are NOT ordered low-to-high by themselves. On this household's
        pump, normal is 70 %, mode 1 is 0 % and mode 2 is 30 % -- so modes 1 and
        2 REDUCE ventilation. Always read before choosing, or "more air" silently
        becomes "no air".
        """
        data = self.read_many(list(FAN_SPEED_REGISTER.values()))
        out: dict[int, float | None] = {}
        for mode, addr in FAN_SPEED_REGISTER.items():
            v = data.get(addr, {}).get("value")
            out[mode] = float(v) if isinstance(v, (int, float)) else None
        return out

    def ventilate(self, direction: str = "up", hours: int = 3, mode: int | None = None) -> dict:
        hours = int(hours)
        if not 1 <= hours <= 24:
            raise safety.Refused("The return time must be between 1 and 24 hours.")
        if mode is not None and not 0 <= int(mode) <= 4:
            raise safety.Refused("Ventilation mode must be 0..4.")
        speeds = self.fan_speeds()
        normal = speeds.get(0)
        if normal is None:
            raise safety.Refused(
                "Could not read the normal fan speed (register 40110), so I cannot tell "
                "which mode means 'more air' on this pump."
            )
        if mode is None:
            if direction == "normal":
                mode = 0
            elif direction == "down":
                lower = {m: p for m, p in speeds.items() if m and p is not None and p < normal}
                if not lower:
                    raise safety.Refused("No mode gives less air than normal (%g %%)." % normal)
                mode = max(lower, key=lambda m: lower[m])
            else:
                higher = {m: p for m, p in speeds.items() if m and p is not None and p > normal}
                if not higher:
                    raise safety.Refused("No mode gives more air than normal (%g %%)." % normal)
                mode = min(higher, key=lambda m: higher[m])

        results = []
        if mode in FAN_RETURN_REGISTER:
            results.append(self.write(FAN_RETURN_REGISTER[mode], hours))
        results.append(self.write(R_VENT_MODE, mode))
        return {
            "mode": mode,
            "percent": speeds.get(mode),
            "normal_percent": normal,
            "hours": hours if mode else None,
            "speeds": speeds,
            "writes": results,
        }

    #: Nobody needs more than a day of forced hot water from a phone, whatever
    #: the register map does or does not say about the range.
    MAX_EXTRA_HOT_WATER_MINUTES = 24 * 60

    def extra_hot_water(self, minutes: int = 180, off: bool = False) -> dict:
        minutes = int(minutes)
        if not off and not 1 <= minutes <= self.MAX_EXTRA_HOT_WATER_MINUTES:
            raise safety.Refused(
                "Extra hot water is limited to 1..%d minutes by this app."
                % self.MAX_EXTRA_HOT_WATER_MINUTES
            )
        if off:
            return {"off": True, "writes": [self.write(R_MORE_HW, 0),
                                            self.write(R_MORE_HW_MINUTES, 0)]}
        return {"off": False, "minutes": minutes,
                "writes": [self.write(R_MORE_HW_MINUTES, minutes),
                           self.write(R_MORE_HW, 1)]}

    # -- backup ------------------------------------------------------------

    def backup(self, directory: str, note: str = "") -> str:
        """Read every register in the map and write a timestamped JSON snapshot."""
        os.makedirs(directory, exist_ok=True)
        started = dt.datetime.now().astimezone()
        addresses = [r.address for r in self.registry.all()]
        data = self.read_many(addresses)

        entries = []
        for reg in self.registry.all():
            row = data.get(reg.address)
            if row is None:
                continue
            entry = reg.as_dict()
            entry["value"] = row.get("value")
            if "error" in row:
                entry["error"] = row["error"]
            entries.append(entry)

        ok = [e for e in entries if "error" not in e]
        settings = [e for e in ok if e["writable"]]
        changed = [e for e in settings
                   if e.get("default") is not None and e.get("value") is not None
                   and _differs(e["value"], e["default"])]

        snapshot = {
            "taken_at": started.isoformat(),
            "note": note,
            "pump": {"host": self.host, "port": self.port},
            "register_map": self.registry.source,
            "counts": {
                "in_map": len(addresses),
                "read": len(ok),
                "failed": len(entries) - len(ok),
                "settings": len(settings),
                "differ_from_default": len(changed),
            },
            "registers": entries,
        }
        stamp = started.strftime("%Y%m%d-%H%M%S")
        path = os.path.join(directory, "nibe-%s.json" % stamp)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, indent=1, ensure_ascii=False)

        latest = os.path.join(directory, "latest.json")
        try:
            if os.path.islink(latest) or os.path.exists(latest):
                os.remove(latest)
            os.symlink(os.path.basename(path), latest)
        except OSError:
            pass
        return path


def _blocks(regs: list[Register], gap: int = 3) -> list[list[Register]]:
    """Group registers into runs of at most MAX_REGS_PER_QUERY words.

    Small gaps are read through -- one query of 20 beats four queries of 1 --
    and a block that hits a non-existent register is retried one by one.
    """
    out: list[list[Register]] = []
    current: list[Register] = []
    for reg in regs:
        if not current:
            current = [reg]
            continue
        start = current[0].wire
        end = current[-1].wire + current[-1].count
        if reg.wire - end <= gap and (reg.wire + reg.count - start) <= MAX_REGS_PER_QUERY:
            current.append(reg)
        else:
            out.append(current)
            current = [reg]
    if current:
        out.append(current)
    return out


def _differs(value, default) -> bool:
    try:
        return abs(float(value) - float(default)) > 1e-9
    except (TypeError, ValueError):
        return str(value) != str(default)
