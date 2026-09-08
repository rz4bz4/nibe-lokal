"""The heat pump as an object: grouped reads, guarded writes, a full backup.

This is where the two register numberings meet. Every caller above this module
speaks **canonical** (S-series) addresses -- see nibelokal/profile.py for why --
and every address handed to the register map, and from there to the wire, is a
**physical** one. The translation happens in `Pump.register()` and nowhere
else; on an S-series pump it is the identity, so nothing about that pump's
behaviour changes.

The rule for reading this file: an `address` argument is canonical, a
`reg.address` is physical, and the two are the same number on an S pump.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
import time

from .modbus import ModbusCorrupt, ModbusError, ModbusOffline, ModbusTCP
from .profile import Profile
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

#: The F generation's whole extra-hot-water feature, in one register: 0 off,
#: 1 = 3 h, 2 = 6 h, 3 = 12 h, 4 = a one-time increase. There is no minutes
#: register to map 40226 onto and no separate switch to map 40698 onto, so
#: `extra_hot_water` says so rather than rounding a request to the nearest
#: three hours. Named here so the refusal can point at it.
F_TEMPORARY_LUX = 48132

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
                 allow_guarded: bool = True, timeout: float = 5.0,
                 profile: Profile | None = None, framing: str = "tcp"):
        self.registry = registry
        self.allow_guarded = allow_guarded
        # No profile means the S series, which is the identity -- so a Pump
        # built the way it was before this existed behaves exactly as it did.
        self.profile = profile if profile is not None else Profile("S")
        # Which of the two 16-bit words of a 32-bit value comes first is a
        # property of the pump, so it is settled here, once, for every register
        # in the map -- rather than at each decode and each encode, which is
        # two places that have to agree. This sets the default; on an F pump
        # `resolve_word_order` below then asks the pump's own register 48852 and
        # sets it again from the answer. See nibelokal/profile.py.
        self.registry.set_word_order(self.profile.low_word_first)
        # The transport's own floor, never a ceiling: an F-series request that
        # NIBE gives 2.1 s cannot be given the S series' comfortable 5 s and
        # then timed out at 1 s because somebody tightened `timeout` in
        # config.yaml. See profile.MIN_REQUEST_TIMEOUT.
        timeout = max(float(timeout), self.profile.request_timeout)
        self.mb = ModbusTCP(host, port, unit, timeout, framing)
        self.host = host
        self.port = port
        self._lock = threading.RLock()
        # Registers this pump refused, and when. Keyed by CANONICAL address,
        # because that is what callers ask for and what missing() reports. A
        # dict rather than a set: see MISSING_RETRY_SECONDS -- these expire,
        # and /api/status shows them.
        self._missing: dict[int, float] = {}

    def resolve_word_order(self) -> str:
        """Ask an F-series pump which way round its 32-bit registers are.

        One register, once, at startup: 48852 "Modbus40 Word Swap", whose
        factory value in every F map is 1 = swapped = the low word first. It is
        worth a request because the sources disagree -- NIBE's MODBUS 40 manual
        says the factory setting is Big Endian, the register map and two other
        implementations say the opposite -- and because the pump is the only
        one of them that knows what *this* pump is set to. See
        profile.LOW_WORD_FIRST.

        Does nothing at all on an S-series pump: the order there is NIBE's TIF,
        not a setting, and no S map has the register. Does nothing when
        `word_swap` is in config.yaml. Never raises: a pump that cannot be
        reached at startup is a pump the next poll will report on properly, and
        an unreadable 48852 leaves the default in place -- said out loud, in the
        log and in every backup header, as "assumed" rather than "read".

        Returns the word in `profile.word_order_source`, for the caller that
        wants to print it.
        """
        source = self.profile.word_order_source
        if self.profile.generation != "F" or self.profile.word_swap is not None:
            return source
        from .profile import F_WORD_SWAP_REGISTER
        reg = self.registry.get(F_WORD_SWAP_REGISTER)
        if reg is None:
            log.info("word order: register %d is not in %s; assuming %s",
                     F_WORD_SWAP_REGISTER, self.registry.source,
                     _word_order_words(self.profile.low_word_first))
            return self.profile.word_order_source
        try:
            value = reg.decode(self.mb.read(reg.kind, reg.wire, reg.count))
        except (ModbusError, ModbusOffline, OSError, ValueError) as exc:
            log.info("word order: register %d did not answer (%s); assuming %s",
                     F_WORD_SWAP_REGISTER, exc,
                     _word_order_words(self.profile.low_word_first))
            return self.profile.word_order_source
        if self.profile.set_word_order_from_register(value) is None:
            log.info("word order: register %d answered %r, which is neither 0 "
                     "nor 1; assuming %s", F_WORD_SWAP_REGISTER, value,
                     _word_order_words(self.profile.low_word_first))
            return self.profile.word_order_source
        # The registry was set from the default in __init__; set it again from
        # the answer, in the one place that owns it.
        self.registry.set_word_order(self.profile.low_word_first)
        log.info("word order: register %d (%s) answered %r, so 32-bit registers "
                 "are read %s", F_WORD_SWAP_REGISTER, reg.title, value,
                 _word_order_words(self.profile.low_word_first))
        return self.profile.word_order_source

    # -- the boundary ----------------------------------------------------

    def register(self, address: int) -> Register | None:
        """The register a canonical address resolves to on this pump.

        The single place canonical becomes physical. Everything that used to
        call `pump.registry.get(address)` calls this instead, because
        `registry.get` on an F pump takes a number this app never has: an
        untranslated 40031 there is not "missing", it is the room temperature
        of climate system 3.

        None means the same thing it has always meant -- this pump has no such
        register -- and every caller already handles it.
        """
        if not self.profile.available(address):
            return None
        return self.registry.get(self.profile.physical(address))

    def physical(self, address: int) -> int:
        """What `address` is called on this pump's own display and paperwork."""
        return self.profile.physical(address)

    def _no_such_register(self, address: int) -> KeyError:
        """The error for an address this pump cannot reach, saying which."""
        why = self.profile.why_unavailable(address)
        if why:
            return KeyError("register %d has no equivalent on an %s-series pump: %s"
                            % (address, self.profile.generation, why))
        physical = self.profile.physical(address)
        where = ("%d" % address if physical == address
                 else "%d (%d on this pump)" % (address, physical))
        return KeyError("register %s is not in the map (%s)"
                        % (where, self.registry.source))

    # -- reading ---------------------------------------------------------

    def read(self, address: int):
        reg = self.register(address)
        if reg is None:
            raise self._no_such_register(address)
        words = self.mb.read(reg.kind, reg.wire, reg.count)
        return reg.decode(words)

    def read_many(self, addresses: list[int]) -> dict[int, dict]:
        """Read a set of registers, batching contiguous runs into single queries.

        Canonical addresses in, canonical addresses out. The Register objects
        in between are physical, so each one is carried alongside the canonical
        address it answers for rather than being asked for its own number
        afterwards.
        """
        now = time.time()
        wanted: list[tuple[int, Register]] = []
        for a in addresses:
            r = self.register(a)
            if r is not None and not self._is_missing(a, now):
                wanted.append((a, r))
        return self._read_pairs(wanted)

    def _read_pairs(self, pairs: list[tuple[int, Register]]) -> dict[int, dict]:
        """Read (reported address, physical register) pairs into one dict."""
        out: dict[int, dict] = {}
        for kind in (3, 4):
            group = sorted([p for p in pairs if p[1].kind == kind],
                           key=lambda p: p[1].wire)
            # How much may go in one request, and how far it may read through a
            # gap to get there, is the transport's rule and not this app's --
            # so it comes from the profile. On an S pump these are 20 and 3,
            # which is what this line has always done; on an F pump they are 1
            # and 0, and every register is its own request. See profile.py.
            for block in _blocks(group, self.profile.read_gap,
                                 self.profile.max_regs_per_query):
                self._read_block(kind, block, out)
        return out

    def _read_block(self, kind: int, block: list[tuple[int, Register]],
                    out: dict) -> None:
        start = block[0][1].wire
        span = block[-1][1].wire + block[-1][1].count - start
        try:
            words = self.mb.read(kind, start, span)
        except ModbusError as exc:
            if len(block) == 1:
                # A single register the pump does not have. Remember it and stop
                # asking. Measured on an S735-family pump: it answers exception 1
                # (illegal function), not the textbook 2 (illegal data address),
                # for registers it does not implement -- so treat both as absent.
                if exc.code in (1, 2):
                    self._mark_missing(block[0][0])
                out[block[0][0]] = {"error": str(exc)}
                return
            # A hole somewhere in the block: fall back to reading each one.
            for pair in block:
                self._read_block(kind, [pair], out)
            return
        except ModbusCorrupt as exc:
            # The frame arrived and its CRC did not match, twice. Users report
            # a MODBUS 40 answering exactly this for 32-bit registers that are
            # not in its LOG.SET file, on a pump that is otherwise perfectly
            # reachable -- see docs/f-series.md, "What users report that NIBE
            # does not document". This app does not try to fix that; what it
            # must not do is let one unreadable register take the whole poll
            # down every minute. So it is treated exactly as a register the
            # pump refuses: recorded under the canonical address, reported by
            # /api/status, and offered another chance in an hour by the same
            # MISSING_RETRY_SECONDS as every other missing register.
            #
            # Only a single-register block, and only ModbusCorrupt -- a plain
            # ModbusOffline is a link that is down, and marking every register
            # missing one by one would turn "the pump is unreachable" into
            # twenty-five quiet little errors.
            if len(block) == 1:
                self._mark_missing(block[0][0])
                out[block[0][0]] = {"error": str(exc)}
                return
            for pair in block:
                self._read_block(kind, [pair], out)
            return
        for address, reg in block:
            off = reg.wire - start
            try:
                out[address] = {"value": reg.decode(words[off:off + reg.count])}
            except Exception as exc:                      # noqa: BLE001
                out[address] = {"error": "decode failed: %s" % exc}

    def _mark_missing(self, address: int) -> None:
        """Remember that this register was refused, and when. Once.

        Keyed CANONICAL, always. `read_many` reports by canonical address and
        `backup` by physical, so without the translation the one dict would
        hold both -- and on an F pump the physical 40012 (BT3 return) would then
        suppress polling of the canonical 40012 (degree minutes), which is a
        different register entirely. `canonical()` is the identity on an
        S-series pump and for any address with no entry, so this changes
        nothing there.

        `setdefault` and not assignment: the timestamp is the start of an
        hour-long clock (MISSING_RETRY_SECONDS), and a running clock must not
        be pushed forward by something that read the register again anyway. It
        cannot normally happen -- every caller that reads a set of registers
        skips the missing ones first -- but it did: `backup` stopped filtering,
        so the daily automatic snapshot re-read every refused register and reset
        every clock, and a register the pump refuses was then retried once a day
        instead of once an hour. The filter is back; this is the belt to its
        braces, and it costs nothing.
        """
        self._missing.setdefault(self.profile.canonical(address), time.time())

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
            reg = self.register(address)
            row = {
                "address": address,
                "title": reg.title if reg else str(address),
                "since": at,
                "retry_in": round(max(0.0, MISSING_RETRY_SECONDS - (now - at)), 1),
            }
            physical = self.profile.physical(address)
            if physical != address:
                row["physical"] = physical
            out.append(row)
        return out

    # -- writing ---------------------------------------------------------

    def write(self, address: int, value, confirmed: bool = False, expect=None) -> dict:
        reg = self.register(address)
        if reg is None:
            raise self._no_such_register(address)
        if not reg.writable:
            # reg.address, not `address`: on an F pump the number the owner can
            # look up in their own documentation is the physical one, and every
            # refusal in this file quotes that consistently.
            raise safety.Refused("%s (%d) går inte att skriva till."
                                 % (reg.title, reg.address))

        # The tier is decided on the PHYSICAL address, because that is what is
        # actually written and because the tier tables name every address any
        # supported model uses for a dangerous setting. reg.address is that
        # number on both generations.
        safety.check(reg.address, confirmed, self.allow_guarded)

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

        log.info("write %d (%s): %r -> %r (read back %r)", reg.address, reg.title,
                 before, value, after)
        return self._write_result(address, reg, value, before, after)

    def _write_result(self, address: int, reg: Register, value, before, after) -> dict:
        """One write, as the API reports it.

        `address` stays canonical so an older cached index.html goes on working
        against a newer server; `physical` is added when it differs, so an
        F-series owner can see the number their own pump's documentation uses.
        """
        out = {
            "address": address,
            "title": reg.title,
            "requested": value,
            "before": before,
            "after": after,
            "unit": reg.unit,
            "tier": safety.tier(reg.address),
            # A successful read-back is not proof the pump acted on it -- some
            # settings are accepted and then ignored. Verified separately.
            "verified": after is not None,
        }
        if reg.address != address:
            out["physical"] = reg.address
        return out

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
            reg = self.register(address)
            if reg is None:
                raise self._no_such_register(address)
            if not reg.writable:
                raise safety.Refused("%s (%d) går inte att skriva till."
                                     % (reg.title, reg.address))
            safety.check(reg.address, confirmed, self.allow_guarded)
            value = reg.coerce(change.get("value"))
            if not reg.mappings:
                safety.clamp(reg, float(value))
            reg.encode(value)
            # The canonical address rides along so the answer can report it:
            # reg.address is physical, and the web app is holding the other
            # number.
            prepared.append((reg, value, change.get("expect"), address))

        with self._lock:
            # Every precondition first, so a stale `expect` on the last change
            # cannot happen after the first one is already written.
            befores = []
            for reg, _value, expect, _canonical in prepared:
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
            for i, (reg, value, _expect, canonical) in enumerate(prepared):
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
                done.append(self._write_result(canonical, reg, value,
                                               befores[i], after))
                log.info("write %d (%s): %r -> %r", reg.address, reg.title,
                         befores[i], after)
        return {"changes": done, "partial": False}

    def _rollback(self, applied, befores) -> tuple[list[str], list[str]]:
        """Put back what was already written. Best effort, reported honestly."""
        rolled, failed = [], []
        for (reg, _value, _expect, _canonical), before in zip(reversed(applied),
                                                              reversed(befores)):
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
                "Kunde inte läsa normalfläktnivån (register %d), så appen kan inte "
                "avgöra vilket läge som betyder mer luft på den här pumpen."
                % self.profile.physical(FAN_SPEED_REGISTER[0])
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
        if self.register(R_MORE_HW_MINUTES) is None or self.register(R_MORE_HW) is None:
            # An F-series pump. It has the feature -- one register, 48132, an
            # enumeration of 3, 6 and 12 hours -- but not as a number of
            # minutes plus a switch, and this app will not round a request for
            # 40 minutes up to three hours without saying so. Refused rather
            # than approximated, and the refusal says where the setting is.
            raise safety.Refused(
                "Den här pumpen har inte extra varmvatten som ett antal minuter. "
                "På F-serien är hela funktionen ett register (%d) med fasta lägen: "
                "av, 3 h, 6 h, 12 h eller en engångshöjning. Appen gissar inte vilket "
                "av dem du menade – välj det på pumpens display, eller skriv registret "
                "direkt." % F_TEMPORARY_LUX
            )
        if off:
            return {"off": True, "writes": [self.write(R_MORE_HW, 0),
                                            self.write(R_MORE_HW_MINUTES, 0)]}
        return {"off": False, "minutes": minutes,
                "writes": [self.write(R_MORE_HW_MINUTES, minutes),
                           self.write(R_MORE_HW, 1)]}

    # -- backup ------------------------------------------------------------

    def backup(self, directory: str, note: str = "") -> str:
        """Read every register in the map and write a timestamped JSON snapshot.

        This one works in PHYSICAL addresses, deliberately, and is the reason
        `_read_pairs` exists. The register map holds physical registers, so
        handing their addresses to `read_many` would translate numbers that are
        already translated: on an F pump the map's 40012 is BT3 return
        temperature, and read_many would take it for the canonical degree
        minutes and read 43005 instead. A snapshot is of the pump as the pump
        numbers it -- which is also what its owner's own documentation says --
        and each entry gains `canonical` where this app calls it something
        else, so `nibelokal diff` and a reader can find their way back.

        Registers this pump has already refused are skipped, which is what
        `read_many` does for the poll and what this did before it stopped going
        through `read_many`. It matters twice: a snapshot of a 1200-register map
        would otherwise spend one request per refused register on every run --
        measured at four extra requests on a map with three refused ones -- and,
        worse, the daily automatic snapshot would re-read them all and so keep
        every hourly retry clock permanently reset. A skipped register is absent
        from `registers` and from `counts.read`, exactly as before; a register
        refused *during* this backup is recorded with its error, as before.
        """
        os.makedirs(directory, exist_ok=True)
        started = dt.datetime.now().astimezone()
        now = time.time()
        registers = self.registry.all()
        # Physical addresses in, `_missing` keyed canonical -- see _mark_missing.
        data = self._read_pairs(
            [(reg.address, reg) for reg in registers
             if not self._is_missing(self.profile.canonical(reg.address), now)])

        entries = []
        for reg in registers:
            row = data.get(reg.address)
            if row is None:
                continue
            entry = reg.as_dict()
            canonical = self.profile.canonical(reg.address)
            if canonical != reg.address:
                entry["canonical"] = canonical
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
            # Which numbering the addresses in this snapshot are in. Additive:
            # a snapshot taken before this key existed is an S-series one, and
            # `nibelokal diff` compares two snapshots of the same pump anyway.
            "profile": self.profile.as_dict(),
            "counts": {
                "in_map": len(registers),
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


def _word_order_words(low_first: bool) -> str:
    """The word order in English, for a log line. See profile.LOW_WORD_FIRST."""
    return "low word first" if low_first else "high word first"


def _blocks(pairs: list[tuple[int, Register]], gap: int = 3,
            max_regs: int = 20) -> list[list]:
    """Group registers into runs of at most `max_regs` words.

    Small gaps are read through -- one query of 20 beats four queries of 1 --
    and a block that hits a non-existent register is retried one by one.

    Both limits are the transport's, and both are arguments rather than
    constants because the two generations do not share them: an S-series pump
    over Modbus TCP takes 20 registers in a request, a MODBUS 40 takes one
    (docs/f-series.md). With max_regs=1 and gap=0 every register is its own
    block, and a 32-bit register is the single documented exception -- it still
    goes out as its two words, because that is what NIBE's own table means by
    "1-2". The defaults are the S-series values, so a caller that does not pass
    them gets the behaviour this function has always had.

    Takes (reported address, register) pairs rather than bare registers: the
    grouping is by the *physical* wire address, which is the register's own,
    while the answer has to come back under the address the caller asked for.
    """
    out: list[list] = []
    current: list = []
    for pair in pairs:
        reg = pair[1]
        if not current:
            current = [pair]
            continue
        start = current[0][1].wire
        end = current[-1][1].wire + current[-1][1].count
        if reg.wire - end <= gap and (reg.wire + reg.count - start) <= max_regs:
            current.append(pair)
        else:
            out.append(current)
            current = [pair]
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
