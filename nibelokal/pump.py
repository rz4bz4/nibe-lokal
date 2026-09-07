"""The heat pump as an object: grouped reads, guarded writes, a full backup."""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
import time

from .modbus import MAX_REGS_PER_QUERY, ModbusError, ModbusOffline, ModbusTCP
from .registry import Register, Registry
from . import safety
from . import sv_number

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
    32196,   # Class 1 alarm flag -- alarms.py needs it to catch an alarm the
             # pump raises without putting a number in 31976
    31029,   # Priority: what the compressor is doing right now. autotune reads
             # it to throw away the hours the pump was making hot water rather
             # than heating the house.
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

#: How long a register the pump refused stays out of the polled block before it
#: is offered another chance.
#:
#: Exception 1 is two different answers wearing one number: Modbus' generic
#: "illegal function" and -- measured on an S735-family pump -- "I do not
#: implement that register". A pump that is rebooting, or busy, answers the
#: first for a register it has. Dropping such a register for the life of the
#: process meant one bad second could hide it until somebody restarted the app,
#: and if that register was 31976 the alarm watcher stopped watching without
#: anything saying so. An hour is long enough that a genuinely absent register
#: costs one query an hour instead of one a minute, and short enough that a
#: reboot heals itself before anybody notices.
MISSING_RETRY_SECONDS = 3600.0


class Pump:
    def __init__(self, host: str, port: int, unit: int, registry: Registry,
                 allow_guarded: bool = True, timeout: float = 5.0):
        self.registry = registry
        self.allow_guarded = allow_guarded
        self.mb = ModbusTCP(host, port, unit, timeout)
        self.host = host
        self.port = port
        self._lock = threading.RLock()
        # Registers this pump refused, and when. A dict rather than a set: see
        # MISSING_RETRY_SECONDS -- these expire, and /api/status shows them.
        self._missing: dict[int, float] = {}

    # -- reading ---------------------------------------------------------

    def read(self, address: int):
        reg = self.registry.get(address)
        if reg is None:
            raise KeyError("register %d is not in the map (%s)" % (address, self.registry.source))
        words = self.mb.read(reg.kind, reg.wire, reg.count)
        return reg.decode(words)

    def read_many(self, addresses: list[int]) -> dict[int, dict]:
        """Read a set of registers, batching contiguous runs into single queries."""
        now = time.time()
        wanted: list[Register] = []
        for a in addresses:
            r = self.registry.get(a)
            if r is not None and not self._is_missing(a, now):
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
                    self._missing[block[0].address] = time.time()
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

    def _is_missing(self, address: int, now: float | None = None) -> bool:
        """True while this register is being skipped. Expires; see the constant."""
        at = self._missing.get(address)
        if at is None:
            return False
        if (time.time() if now is None else now) - at >= MISSING_RETRY_SECONDS:
            # Its turn again. Removed rather than merely ignored, so a register
            # that answers this time stops being reported as missing at all.
            self._missing.pop(address, None)
            return False
        return True

    def missing(self) -> list[dict]:
        """Registers currently being skipped, for /api/status to show.

        A register that quietly disappeared from every poll is invisible
        otherwise, and 31976 disappearing means the alarm watching stopped.
        """
        now = time.time()
        out = []
        for address, at in sorted(self._missing.items()):
            if now - at >= MISSING_RETRY_SECONDS:
                continue
            reg = self.registry.get(address)
            out.append({
                "address": address,
                "title": reg.title if reg else str(address),
                "since": at,
                "retry_in": round(max(0.0, MISSING_RETRY_SECONDS - (now - at)), 1),
            })
        return out

    # -- writing ---------------------------------------------------------

    def write(self, address: int, value, confirmed: bool = False, expect=None) -> dict:
        reg = self.registry.get(address)
        if reg is None:
            raise KeyError("register %d is not in the map" % address)
        if not reg.writable:
            raise safety.Refused("%s (%d) går inte att skriva till." % (reg.title, address))

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
            # Optimistic concurrency: a phone that has had the page open for a
            # day may be stepping from a value the display has since changed,
            # which moves the heat the opposite way from the button pressed.
            if expect is not None and not _same(before, _as_read(reg, expect)):
                # Including the case where `before` could not be read at all:
                # a conditional write whose condition is unknown is not one.
                raise safety.Refused(
                    "%s har ändrats sedan du läste den (%s nu, %s då). Läs om och "
                    "försök igen så du vet vad du ändrar från."
                    % (reg.title, "okänt" if before is None else before,
                       _as_read(reg, expect))
                )
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

    def write_all(self, changes: list[dict], confirmed: bool = False) -> dict:
        """Apply several writes as one unit.

        The advisor's mild-weather advice is a curve change and an offset change
        that cancel out in cold weather. Half of that pair is worse than neither.

        So: validate everything, then read every `before` and check every
        `expect` under one lock, then write. If a write still fails partway --
        the pump can refuse one -- roll back what was already written and say so.
        Reporting a half-applied pair as a plain error, which an earlier version
        did, leaves the house in exactly the state this function exists to avoid.
        """
        prepared = []
        for change in changes:
            if not isinstance(change, dict):
                raise ValueError("each change must be an object with an address")
            try:
                address = int(change["address"])
            except (TypeError, ValueError):
                # A bad address is a bad request. int(None) is a TypeError,
                # which used to surface as a 502 with a stack trace.
                raise ValueError("%r is not a register address"
                                 % (change.get("address"),))
            reg = self.registry.get(address)
            if reg is None:
                raise KeyError("register %d is not in the map" % address)
            if not reg.writable:
                raise safety.Refused("%s (%d) går inte att skriva till."
                                     % (reg.title, address))
            safety.check(address, confirmed, self.allow_guarded)
            value = reg.coerce(change.get("value"))
            if not reg.mappings:
                safety.clamp(reg, float(value))
            reg.encode(value)
            prepared.append((reg, value, change.get("expect")))

        with self._lock:
            # Every precondition first, so a stale `expect` on the last change
            # cannot happen after the first one is already written.
            befores = []
            for reg, _value, expect in prepared:
                before = None
                try:
                    before = reg.decode(self.mb.read(reg.kind, reg.wire, reg.count))
                except (ModbusError, ModbusOffline):
                    pass
                if expect is not None and not _same(before, _as_read(reg, expect)):
                    raise safety.Refused(
                        "%s har ändrats sedan du läste den (%s nu, %s då). Läs om och "
                        "försök igen så du vet vad du ändrar från."
                        % (reg.title, "okänt" if before is None else before,
                           _as_read(reg, expect))
                    )
                befores.append(before)

            done: list[dict] = []
            for i, (reg, value, _expect) in enumerate(prepared):
                written = False
                try:
                    self.mb.write(reg.wire, reg.encode(value))
                    # From here on the pump has already changed. A read-back
                    # that then fails is a lost answer, not an unwritten
                    # register -- rolling back only prepared[:i] left write i
                    # standing, which is exactly the half-applied pair this
                    # function exists to prevent.
                    written = True
                    after = reg.decode(self.mb.read(reg.kind, reg.wire, reg.count))
                except Exception as exc:                   # noqa: BLE001
                    upto = i + 1 if written else i
                    rolled, failed_back = self._rollback(prepared[:upto], befores[:upto])
                    return {
                        "changes": done,
                        "partial": True,
                        "rolled_back": rolled,
                        "rollback_failed": failed_back,
                        "error": "%s: %s" % (reg.title, exc),
                    }
                done.append({
                    "address": reg.address, "title": reg.title, "requested": value,
                    "before": befores[i], "after": after, "unit": reg.unit,
                    "tier": safety.tier(reg.address), "verified": after is not None,
                })
                log.info("write %d (%s): %r -> %r", reg.address, reg.title,
                         befores[i], after)
        return {"changes": done, "partial": False}

    def _rollback(self, applied, befores) -> tuple[list[str], list[str]]:
        """Put back what was already written. Best effort, reported honestly."""
        rolled, failed = [], []
        for (reg, _value, _expect), before in zip(reversed(applied), reversed(befores)):
            if before is None:
                failed.append("%s (visste inte tidigare värde)" % reg.title)
                continue
            try:
                self.mb.write(reg.wire, reg.encode(before))
                rolled.append("%s tillbaka till %s" % (reg.title, before))
            except Exception as exc:                       # noqa: BLE001
                failed.append("%s: %s" % (reg.title, exc))
                log.error("rollback failed for %d: %s", reg.address, exc)
        return rolled, failed

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
            raise safety.Refused("Återgångstiden måste vara mellan 1 och 24 timmar.")
        if mode is not None:
            # Converted, not merely validated. The web app sends whatever the
            # JSON body carried, and "2" out of a phone passed int(mode) as a
            # check and then failed `mode in FAN_RETURN_REGISTER` -- so the
            # return time was never written while the answer still said
            # "hours: 3", and the fan sat at mode 2 for whatever return time
            # the pump happened to have.
            try:
                mode = int(mode)
            except (TypeError, ValueError):
                raise safety.Refused("Ventilationsläget måste vara ett tal 0–4.")
            if not 0 <= mode <= 4:
                raise safety.Refused("Ventilationsläget måste vara 0–4.")
        speeds = self.fan_speeds()
        normal = speeds.get(0)
        if normal is None:
            raise safety.Refused(
                "Kunde inte läsa normalfläktnivån (register 40110), så appen kan inte "
                "avgöra vilket läge som betyder mer luft på den här pumpen."
            )
        if mode is None:
            if direction == "normal":
                mode = 0
            elif direction == "down":
                lower = {m: p for m, p in speeds.items() if m and p is not None and p < normal}
                if not lower:
                    raise safety.Refused(
                        "Inget läge ger mindre luft än normalläget (%s %%)."
                        % sv_number(normal, None))
                mode = max(lower, key=lambda m: lower[m])
            else:
                higher = {m: p for m, p in speeds.items() if m and p is not None and p > normal}
                if not higher:
                    raise safety.Refused(
                        "Inget läge ger mer luft än normalläget (%s %%)."
                        % sv_number(normal, None))
                mode = min(higher, key=lambda m: higher[m])

        results = []
        if mode in FAN_RETURN_REGISTER:
            results.append(self.write(FAN_RETURN_REGISTER[mode], hours))
        results.append(self.write(R_VENT_MODE, mode))
        return {
            "mode": mode,
            "percent": speeds.get(mode),
            "normal_percent": normal,
            # The return time is only written for a mode that has a return
            # register, so this is what was set -- not what was asked for.
            "hours": hours if mode in FAN_RETURN_REGISTER else None,
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
                "Extra varmvatten är begränsat till 1–%d minuter i den här appen."
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
        # Write then rename: a crash halfway through must not leave a truncated
        # snapshot that looks like a real one and breaks `diff`.
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, indent=1, ensure_ascii=False)
        os.replace(tmp, path)

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


def _as_read(reg: Register, expect):
    """`expect` in the form decode() would have handed it back.

    An enum register reads as its label ("Medium") and writes as its key (1),
    so a caller may legitimately send either. Comparing the raw `expect` with
    the decoded `before` made a conditional write on a mapped register refuse
    every time -- with the memorable text "Medium nu, Medium då" -- while the
    single-write path refused only the key form. Normalise once, here, so both
    paths compare like with like.
    """
    if not reg.mappings:
        return expect
    try:
        key = reg.coerce(expect)
    except (TypeError, ValueError):
        # Not a value this register takes at all, so it cannot be what the
        # register reads as either. Left alone, and the comparison fails --
        # which is the honest answer to "I expected something impossible".
        return expect
    return reg.mappings.get(str(key), key)


def _same(a, b) -> bool:
    try:
        return abs(float(a) - float(b)) < 1e-9
    except (TypeError, ValueError):
        return str(a) == str(b)


def _differs(value, default) -> bool:
    try:
        return abs(float(value) - float(default)) > 1e-9
    except (TypeError, ValueError):
        return str(value) != str(default)
