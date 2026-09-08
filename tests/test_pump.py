"""Tests for Pump: the guarded write, the everyday actions, the missing set.

Every case here is a defect that reached a released version and would have hit
a real house: a conditional write that refused every time on a mapped register,
a ventilation call that reported a return time it never wrote, a register the
pump refused once and never asked for again, and a rollback that left half a
pair applied. None of them raise; all of them do the wrong thing quietly, which
is why they need tests rather than a smoke run.

No sockets: FakeModbus stands in for ModbusTCP and remembers every word it was
given, which is the only way to tell "wrote it" from "said it wrote it".
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nibelokal import pump as pump_module                     # noqa: E402
from nibelokal import safety                                  # noqa: E402
from nibelokal.modbus import (ModbusCorrupt, ModbusError,     # noqa: E402
                              ModbusOffline)
from nibelokal.profile import F_TABLE, Profile                # noqa: E402
from nibelokal.pump import FAN_RETURN_REGISTER, Pump          # noqa: E402
from nibelokal.registry import Register, Registry             # noqa: E402
from nibelokal.safety import Refused                          # noqa: E402


HOT_WATER_MODES = {"0": "Economy", "1": "Medium", "2": "Large"}


def registry() -> Registry:
    regs = {
        # A mapped register: reads back "Medium", writes as 1.
        40057: Register(address=40057, title="Varmvattenbehov", size="s16",
                        writable=True, mappings=dict(HOT_WATER_MODES)),
        40031: Register(address=40031, title="Värmeoffset", size="s16",
                        writable=True, min=-10, max=10),
        40027: Register(address=40027, title="Värmekurva", size="s16",
                        writable=True, min=0, max=15),
        40105: Register(address=40105, title="Ventilationsläge", size="s16",
                        writable=True, min=0, max=4),
        40110: Register(address=40110, title="Fläkt normal", size="s16",
                        writable=True, min=0, max=100),
        30002: Register(address=30002, title="Utetemperatur", size="s16", factor=10),
    }
    for mode, address in pump_module.FAN_SPEED_REGISTER.items():
        regs.setdefault(address, Register(address=address, title="Fläkt %d" % mode,
                                          size="s16", writable=True, min=0, max=100))
    for mode, address in FAN_RETURN_REGISTER.items():
        regs.setdefault(address, Register(address=address, title="Återgång %d" % mode,
                                          size="s16", writable=True, min=1, max=24))
    return Registry(regs, "test")


class FakeModbus:
    """Enough of ModbusTCP to write to, read back and fail on demand."""

    def __init__(self, words=None):
        self.words = dict(words or {})
        self.writes = []
        self.refuse_write_at = None      # wire address whose write raises
        self.refuse_read_after = None    # wire address whose read-back raises
        self.read_errors = {}            # wire address -> ModbusError to raise
        self.reads = []
        # Every read as (address, count). `reads` keeps only the addresses,
        # which is what the older cases assert on; how MANY registers one
        # request asked for is the whole question on an F-series pump, where
        # NIBE allows one.
        self.requests = []

    def read(self, kind, address, count):
        self.reads.append(address)
        self.requests.append((address, count))
        if address in self.read_errors:
            raise self.read_errors[address]
        return [self.words.get(address + i, 0) for i in range(count)]

    def write(self, address, values):
        if address == self.refuse_write_at:
            raise ModbusError(3, "writing")
        self.writes.append((address, list(values)))
        for i, v in enumerate(values):
            self.words[address + i] = v
        if address == self.refuse_read_after:
            # The write landed; only the answer to the read-back is lost.
            self.read_errors[address] = ModbusError(6, "reading")


def build(words=None, **kw) -> Pump:
    p = Pump("192.0.2.10", 502, 1, registry(), **kw)
    p.mb = FakeModbus(words)
    return p


class ExpectOnAMappedRegister(unittest.TestCase):
    """40057 reads as "Medium" and writes as 1. Both are legal in `expect`.

    Regression: write_all decoded `before` to the label and coerced `expect` to
    the key, so the two never matched and every conditional write of a mapped
    register was refused -- with the memorable text "Medium nu, Medium då".
    write() had the opposite half-fix: a label worked, an int key was refused.
    40057, 40238 and every other enum register were affected.
    """

    def setUp(self):
        # wire address of 40057 is 56; the pump currently says Medium (1).
        self.pump = build({56: 1})

    def test_write_all_accepts_the_label(self):
        result = self.pump.write_all(
            [{"address": 40057, "value": "Large", "expect": "Medium"}], confirmed=True)
        self.assertFalse(result["partial"])
        self.assertEqual(result["changes"][0]["after"], "Large")

    def test_write_all_accepts_the_key(self):
        result = self.pump.write_all(
            [{"address": 40057, "value": 2, "expect": 1}], confirmed=True)
        self.assertFalse(result["partial"])
        self.assertEqual(result["changes"][0]["after"], "Large")

    def test_write_accepts_the_key(self):
        result = self.pump.write(40057, 2, confirmed=True, expect=1)
        self.assertEqual(result["after"], "Large")

    def test_write_accepts_the_label(self):
        result = self.pump.write(40057, 2, confirmed=True, expect="Medium")
        self.assertEqual(result["after"], "Large")

    def test_a_genuine_change_is_still_refused_on_both_paths(self):
        # And the refusal says what it found, in the form the caller reads it.
        with self.assertRaises(Refused) as caught:
            self.pump.write(40057, 2, confirmed=True, expect="Economy")
        self.assertIn("Medium", str(caught.exception))
        self.assertIn("Economy", str(caught.exception))
        with self.assertRaises(Refused):
            self.pump.write_all(
                [{"address": 40057, "value": 2, "expect": 0}], confirmed=True)
        self.assertEqual(self.pump.mb.writes, [], "nothing may be written")

    def test_a_plain_register_still_compares_by_number(self):
        p = build({30: 3})                      # 40031 -> wire 30, offset 3
        p.write(40031, 4, confirmed=True, expect=3)
        with self.assertRaises(Refused):
            p.write(40031, 5, confirmed=True, expect=3)


class Ventilation(unittest.TestCase):
    """The return time must be written, not merely reported.

    Regression: the web app passes the JSON body's `mode` through as it
    arrived, so "2" out of a phone passed int(mode) as a validation and then
    failed `mode in FAN_RETURN_REGISTER`, whose keys are ints. Only 40105 was
    written; the answer still said hours: 3, and the fan then ran at mode 2
    with whatever return time the pump happened to have.
    """

    def setUp(self):
        # Normal 70 %, mode 1 = 0 %, mode 2 = 30 %: this household's own pump.
        speeds = {pump_module.FAN_SPEED_REGISTER[0]: 70,
                  pump_module.FAN_SPEED_REGISTER[1]: 0,
                  pump_module.FAN_SPEED_REGISTER[2]: 30,
                  pump_module.FAN_SPEED_REGISTER[3]: 90,
                  pump_module.FAN_SPEED_REGISTER[4]: 100}
        words = {}
        for address, percent in speeds.items():
            words[(address % 10000) - 1] = percent
        self.pump = build(words)

    def _written(self):
        return {address for address, _ in self.pump.mb.writes}

    def test_a_string_mode_still_writes_the_return_time(self):
        result = self.pump.ventilate(mode="2", hours=5)
        self.assertEqual(result["mode"], 2)
        self.assertEqual(result["hours"], 5)
        self.assertIn((FAN_RETURN_REGISTER[2] % 10000) - 1, self._written())

    def test_an_int_mode_behaves_the_same(self):
        result = self.pump.ventilate(mode=2, hours=5)
        self.assertEqual(result["hours"], 5)
        self.assertIn((FAN_RETURN_REGISTER[2] % 10000) - 1, self._written())

    def test_mode_zero_reports_no_return_time_because_it_writes_none(self):
        result = self.pump.ventilate(mode="0")
        self.assertIsNone(result["hours"])
        self.assertNotIn((FAN_RETURN_REGISTER[1] % 10000) - 1, self._written())

    def test_a_mode_that_is_not_a_number_is_refused_in_swedish(self):
        with self.assertRaises(Refused) as caught:
            self.pump.ventilate(mode="uppåt")
        self.assertIn("0–4", str(caught.exception))

    def test_out_of_range_is_still_refused(self):
        with self.assertRaises(Refused):
            self.pump.ventilate(mode="9")

    def test_refusals_are_swedish(self):
        with self.assertRaises(Refused) as caught:
            self.pump.ventilate(hours=99)
        self.assertIn("timmar", str(caught.exception))


class MissingRegisters(unittest.TestCase):
    """A pump that refuses a register once must be asked again.

    Regression: `_missing` was permanent, and exception 1 means both "I do not
    implement that" and Modbus' generic "illegal function" -- which is what a
    rebooting pump answers for a register it has. One bad second hid the
    register until the process restarted, and if that register was 31976 the
    alarm watching stopped with nothing in /api/status to show it.
    """

    def setUp(self):
        self.pump = build({1: 34})
        # 30002 -> wire 1. The pump refuses it, once.
        self.pump.mb.read_errors[1] = ModbusError(1, "reading")

    def test_it_is_dropped_from_the_next_poll(self):
        self.pump.read_many([30002])
        self.assertEqual(self.pump.mb.reads, [1])
        self.pump.read_many([30002])
        self.assertEqual(self.pump.mb.reads, [1], "asked again inside the window")

    def test_it_is_reported_so_somebody_can_see_it(self):
        self.pump.read_many([30002])
        missing = self.pump.missing()
        self.assertEqual([m["address"] for m in missing], [30002])
        self.assertGreater(missing[0]["retry_in"], 0)

    def test_it_recovers_by_itself(self):
        self.pump.read_many([30002])
        # An hour later, and the pump has finished rebooting.
        self.pump._missing[30002] -= pump_module.MISSING_RETRY_SECONDS + 1
        del self.pump.mb.read_errors[1]
        data = self.pump.read_many([30002])
        self.assertEqual(data[30002]["value"], 3.4)
        self.assertEqual(self.pump.missing(), [],
                         "a register that answers is no longer missing")


class RollbackOfAHalfAppliedPair(unittest.TestCase):
    """write_all's whole promise: both changes, or neither.

    Regression: when write i succeeded and its read-back failed, the rollback
    put back prepared[:i] -- everything before it -- and left write i standing,
    which is exactly the half-applied state the docstring promises to prevent.
    The advisor's mild-weather advice is such a pair (curve down, offset up),
    and half of it makes the house colder in weather it was never meant to
    touch.
    """

    def _pair(self):
        # 40027 -> wire 26 (curve 5), 40031 -> wire 30 (offset 0)
        return build({26: 5, 30: 0})

    def test_a_failed_read_back_rolls_back_the_write_that_landed(self):
        p = self._pair()
        p.mb.refuse_read_after = 30            # the offset write lands, the read fails
        result = p.write_all([{"address": 40027, "value": 4},
                              {"address": 40031, "value": 1}], confirmed=True)
        self.assertTrue(result["partial"])
        self.assertEqual(p.mb.words[30], 0, "the offset was left at 1")
        self.assertEqual(p.mb.words[26], 5, "the curve was left at 4")
        self.assertEqual(len(result["rolled_back"]), 2)
        self.assertFalse(result["rollback_failed"])

    def test_a_refused_write_rolls_back_only_what_was_written(self):
        p = self._pair()
        p.mb.refuse_write_at = 30              # the offset write never happens
        result = p.write_all([{"address": 40027, "value": 4},
                              {"address": 40031, "value": 1}], confirmed=True)
        self.assertTrue(result["partial"])
        self.assertEqual(p.mb.words[26], 5, "the curve was left at 4")
        self.assertEqual(len(result["rolled_back"]), 1)

    def test_a_bad_address_is_a_value_error_not_a_type_error(self):
        # ValueError is what the server turns into 400; a TypeError became a
        # 502 with a stack trace for a request that was simply malformed.
        p = self._pair()
        for change in ({"address": None, "value": 1}, {"address": "x", "value": 1},
                       "not an object"):
            with self.assertRaises(ValueError):
                p.write_all([change], confirmed=True)


def f750_registry() -> Registry:
    """An F750's map, holding only the registers this file exercises.

    Keyed by PHYSICAL addresses, which is what a real F-series map is: the
    `nibe` package's F750 map has 47007 and no 40027. Anything that reaches
    this map without going through the profile is asking for a register that
    does not exist here, which is the point.
    """
    regs = {}
    for physical, title, kw in [
        (47007, "Heat Curve S1", dict(writable=True, min=0, max=15)),
        (47011, "Heat Offset S1", dict(writable=True, min=-10, max=10)),
        (47041, "Hot water comfort mode", dict(
            writable=True, mappings={"0": "Economy", "1": "Normal",
                                     "2": "Luxury", "4": "Smart Control"})),
        (47260, "Fan Mode", dict(writable=True, min=0, max=4)),
        (40004, "BT1 Outdoor Temperature", dict(factor=10)),
        (43005, "Degree Minutes (16 bit)", dict(writable=True)),
        (45001, "Alarm", dict()),
    ]:
        regs[physical] = Register(address=physical, title=title, size="s16", **kw)
    # One 32-bit register, because the two questions that only 32-bit registers
    # raise are both F-series questions: a request may be two words for one of
    # them, and which of the two words comes first is a setting on the pump
    # (menu 5.3.11) rather than a fact. 43420 is the compressor's hour counter,
    # canonical 31088.
    regs[43420] = Register(address=43420, title="Tot. op.time compr. EB100-EP14",
                           size="s32", unit="h")
    for mode, canonical in pump_module.FAN_SPEED_REGISTER.items():
        physical = F_TABLE[canonical]
        regs[physical] = Register(address=physical, title="Exhaust Fan speed %d" % mode,
                                  size="s16", writable=True, min=0, max=100)
    for mode, canonical in FAN_RETURN_REGISTER.items():
        physical = F_TABLE[canonical]
        regs[physical] = Register(address=physical, title="Fan return time %d" % mode,
                                  size="s16", writable=True, min=1, max=99)
    return Registry(regs, "test F750")


def build_f750(words=None) -> Pump:
    p = Pump("192.0.2.11", 502, 1, f750_registry(),
             profile=Profile.for_model("F750"))
    p.mb = FakeModbus(words)
    return p


class AnFSeriesPumpAnsweredByCanonicalAddresses(unittest.TestCase):
    """The whole design, from the outside.

    Everything above pump.py goes on saying 40027 and 40031. The pump answers
    on 47007 and 47011, which is where the wire addresses come from. Nothing in
    between knows.
    """

    def setUp(self):
        # Wire addresses: 47007 -> 7006, 47011 -> 7010, 40004 -> 3.
        self.pump = build_f750({7006: 0, 7010: 2, 3: -42, 4004: 1})

    def test_a_read_by_the_canonical_address_lands_on_the_physical_one(self):
        data = self.pump.read_many([40027, 40031, 30002])
        self.assertEqual(data[40027]["value"], 0)
        self.assertEqual(data[40031]["value"], 2)
        self.assertEqual(data[30002]["value"], -4.2)
        # Read from the F-series wire addresses, and nothing else.
        self.assertIn(7006, self.pump.mb.reads)
        self.assertIn(3, self.pump.mb.reads)

    def test_the_answer_is_keyed_by_the_canonical_address(self):
        # Because that is what the web app, the poller and the history are
        # holding. A dict keyed by 47007 would silently lose every reading.
        data = self.pump.read_many([40027])
        self.assertEqual(list(data), [40027])

    def test_a_write_by_the_canonical_address_goes_to_the_physical_one(self):
        result = self.pump.write(40031, 3, confirmed=True)
        self.assertEqual([a for a, _ in self.pump.mb.writes], [7010])
        self.assertEqual(result["address"], 40031, "the caller's own number")
        self.assertEqual(result["physical"], 47011, "and the pump's")
        self.assertEqual(result["title"], "Heat Offset S1")

    def test_write_all_reports_both_numbers_too(self):
        result = self.pump.write_all([{"address": 40027, "value": 3},
                                      {"address": 40031, "value": 3}],
                                     confirmed=True)
        self.assertFalse(result["partial"])
        self.assertEqual([c["address"] for c in result["changes"]], [40027, 40031])
        self.assertEqual([c["physical"] for c in result["changes"]], [47007, 47011])

    def test_a_register_with_no_f_equivalent_is_refused_by_name(self):
        # 40020 is a real F750 register holding an evaporator temperature. The
        # honest answer is "this app cannot address that here", not a reading.
        self.assertIsNone(self.pump.register(40020))
        with self.assertRaises(KeyError) as caught:
            self.pump.read(40020)
        self.assertIn("no equivalent", str(caught.exception))

    def test_the_physical_number_is_available_for_a_page_to_show(self):
        self.assertEqual(self.pump.physical(40027), 47007)
        self.assertEqual(self.pump.register(40027).address, 47007)


class TheEverydayButtonsOnAnFSeriesPump(unittest.TestCase):
    """Both buttons write with confirmed=False, so a missing tier row refuses.

    That is the failure this looks for: not a wrong value, but a button that
    stops working with a message quoting a register nobody recognises.
    """

    def setUp(self):
        words = {}
        for mode, percent in {0: 70, 1: 0, 2: 30, 3: 90, 4: 100}.items():
            physical = F_TABLE[pump_module.FAN_SPEED_REGISTER[mode]]
            words[(physical % 10000) - 1] = percent
        self.pump = build_f750(words)

    def test_ventilate_is_not_refused(self):
        result = self.pump.ventilate(direction="up", hours=5)
        self.assertEqual(result["mode"], 3, "the lowest mode above normal")
        self.assertEqual(result["hours"], 5)
        written = {a for a, _ in self.pump.mb.writes}
        self.assertIn((F_TABLE[pump_module.R_VENT_MODE] % 10000) - 1, written)
        self.assertIn((F_TABLE[FAN_RETURN_REGISTER[3]] % 10000) - 1, written)

    def test_the_fan_percentages_are_read_from_the_f_registers(self):
        speeds = self.pump.fan_speeds()
        self.assertEqual(speeds[0], 70)
        self.assertEqual(speeds[1], 0, "mode 1 reduces ventilation here too")

    def test_the_hot_water_comfort_mode_is_not_refused(self):
        result = self.pump.write(40057, 2)      # no confirm: everyday tier
        self.assertEqual(result["after"], "Luxury")
        self.assertEqual(result["physical"], 47041)

    def test_extra_hot_water_says_why_rather_than_writing_the_wrong_thing(self):
        # There is no minutes register on the F generation, only 48132 with its
        # five fixed durations. Refused, in Swedish, naming where the setting
        # actually is.
        with self.assertRaises(Refused) as caught:
            self.pump.extra_hot_water(minutes=180)
        message = str(caught.exception)
        self.assertIn("48132", message)
        self.assertIn("engångshöjning", message)
        self.assertEqual(self.pump.mb.writes, [], "nothing may be written")

    def test_a_guarded_register_still_needs_a_confirm(self):
        with self.assertRaises(Refused) as caught:
            self.pump.write(40031, 3)
        # And the refusal quotes the number on the owner's own pump.
        self.assertIn("47011", str(caught.exception))


class TheMissingSetIsKeyedByOneNumbering(unittest.TestCase):
    """backup reads by physical address, read_many by canonical.

    Both feed the same `_missing` dict, and on an F750 the physical 40012 is
    BT3 return temperature while the canonical 40012 is degree minutes. One
    dict holding both would let a backup that failed on the first suppress
    polling of the second, which is a register the advisor reads.
    """

    def test_a_refusal_during_a_backup_is_recorded_under_the_canonical_name(self):
        pump = build_f750()
        reg = pump.registry.get(43005)                  # degree minutes, wire 3004
        pump.mb.read_errors[reg.wire] = ModbusError(1, "reading")
        pump._read_pairs([(reg.address, reg)])
        self.assertIn(40012, pump._missing, "recorded under the app's own number")
        self.assertNotIn(43005, pump._missing)
        self.assertEqual([m["address"] for m in pump.missing()], [40012])
        self.assertEqual(pump.missing()[0]["physical"], 43005)

    def test_a_physical_address_does_not_suppress_a_different_canonical_one(self):
        pump = build_f750({3: -42})
        outdoor = pump.registry.get(40004)               # canonical 30002
        # A refusal on the F-series *physical* 40004 must not stop the app
        # asking for the canonical 40004 -- which on an F pump is nothing this
        # app addresses, but the shape of the bug is the point.
        pump._missing.clear()
        pump.mb.read_errors[outdoor.wire] = ModbusError(2, "reading")
        pump.read_many([30002])
        self.assertIn(30002, pump._missing)


class OneRegisterPerRequestOnAnFPump(unittest.TestCase):
    """MODBUS 40 allows one register per request, and this asks for one.

    NIBE's own table (docs/f-series.md, "What MODBUS 40 can and cannot do"):
    a read of registers not in the module's 20-entry LOG.SET file is limited to
    1-2 registers, 2 only because a 32-bit parameter occupies two, with a 2.1 s
    maximum timeout. The app batches up to 20 with gap-reading on an S-series
    pump; doing that here is a request the accessory does not answer, which
    looks exactly like a pump that is not there.

    So the assertion is blunt: on an F profile, no request ever asks for more
    than one register.
    """

    def _one_register_each(self, pump):
        for address, count in pump.mb.requests:
            self.assertLessEqual(count, 2, "request at %d asked for %d words"
                                 % (address, count))
        # And two words only ever for a 32-bit register, never for two.
        by_wire = {r.wire: r for r in pump.registry.all()}
        for address, count in pump.mb.requests:
            reg = by_wire.get(address)
            if reg is not None:
                self.assertEqual(count, reg.count,
                                 "request at %d asked for %d words for a %s "
                                 "register" % (address, count, reg.size))

    def test_the_dashboard_is_read_one_register_at_a_time(self):
        pump = build_f750()
        pump.read_many([40027, 40031, 40057, 40105, 30002, 40012, 31088])
        self._one_register_each(pump)
        self.assertGreaterEqual(len(pump.mb.requests), 7,
                                "seven registers, so at least seven requests")

    def test_adjacent_registers_are_not_joined_either(self):
        # 47007 and 47011 are four apart, which the S-series gap of 3 would not
        # bridge -- but the fan speeds 47261-47265 are contiguous, and are
        # exactly what a batching read would swallow in one request.
        pump = build_f750()
        pump.fan_speeds()
        self.assertEqual([c for _, c in pump.mb.requests], [1, 1, 1, 1, 1])

    def test_a_32_bit_register_is_the_documented_exception(self):
        pump = build_f750()
        pump.read_many([31088])
        self.assertEqual(pump.mb.requests, [(3419, 2)])

    def test_a_backup_reads_the_whole_map_one_at_a_time(self):
        import tempfile
        pump = build_f750()
        with tempfile.TemporaryDirectory() as tmp:
            pump.backup(tmp)
        self._one_register_each(pump)
        self.assertEqual(len(pump.mb.requests), len(pump.registry.all()),
                         "one request per register in the map, and no fewer")

    def test_an_s_pump_still_batches_exactly_as_it_did(self):
        # The other half of the promise: nothing about the S path changes.
        pump = build()
        pump.read_many([pump_module.FAN_SPEED_REGISTER[m] for m in (0, 1, 2, 3, 4)])
        self.assertEqual(len(pump.mb.requests), 1, "one query of five, as before")
        self.assertEqual(pump.mb.requests[0][1], 5)

    def test_the_limits_come_from_the_profile(self):
        self.assertEqual(Profile("S").max_regs_per_query, 20)
        self.assertEqual(Profile("S").read_gap, 3)
        self.assertEqual(Profile("F").max_regs_per_query, 1)
        self.assertEqual(Profile("F").read_gap, 0)

    def test_an_f_pump_gets_at_least_the_documented_timeout(self):
        # NIBE's 2.1 s is a maximum for one request. A config that tightens
        # `timeout` for an S-series LAN must not tighten it below that here.
        pump = Pump("192.0.2.11", 502, 1, f750_registry(), timeout=1.0,
                    profile=Profile.for_model("F750"))
        self.assertGreaterEqual(pump.mb.timeout, 2.5)
        # And an S-series pump keeps exactly the timeout it was given.
        self.assertEqual(Pump("192.0.2.10", 502, 1, registry(),
                              timeout=1.0).mb.timeout, 1.0)


class ThirtyTwoBitWordOrder(unittest.TestCase):
    """Which half of a 32-bit value comes first is a setting, not a fact.

    Low word first on both generations by default: NIBE's own TIF on the S
    series, and on the F series the factory value of register 48852 "Modbus40
    Word Swap", which is 1 = swapped = low word first. NIBE's MODBUS 40 manual
    says the factory setting is Big Endian and disagrees with its own register
    map about it; see profile.LOW_WORD_FIRST for why the map wins and why the
    app asks the pump rather than settling it here. Get it wrong and every
    32-bit register reads a large, stable-looking nonsense number while every
    16-bit register is perfect -- which looks like a bad register map and is
    not.
    """

    #: 100 000 hours: 0x000186A0, so the two words are 0x0001 and 0x86A0 and
    #: nothing is symmetric about them.
    HOURS = 100000
    LOW, HIGH = 0x86A0, 0x0001

    def test_an_f_pump_reads_low_word_first_by_default(self):
        # 48852's factory value is 1, "swapping the words in 32-bit variables",
        # which is the low word at the lower address -- the same as the S
        # series, and the opposite of what this defaulted to before.
        pump = build_f750({3419: self.LOW, 3420: self.HIGH})
        self.assertEqual(pump.read(31088), self.HOURS)

    def test_an_f_pump_reads_high_word_first_when_told_to(self):
        pump = Pump("192.0.2.11", 502, 1, f750_registry(),
                    profile=Profile.for_model("F750", word_swap="false"))
        pump.mb = FakeModbus({3419: self.HIGH, 3420: self.LOW})
        self.assertEqual(pump.read(31088), self.HOURS)

    def test_an_s_pump_reads_low_word_first(self):
        regs = registry().registers
        regs[31088] = Register(address=31088, title="Total run time compressor",
                               size="s32", unit="h")
        pump = Pump("192.0.2.10", 502, 1, Registry(regs, "test"))
        pump.mb = FakeModbus({1087: self.LOW, 1088: self.HIGH})
        self.assertEqual(pump.read(31088), self.HOURS)

    def test_the_other_order_would_be_nonsense_rather_than_wrong_by_a_little(self):
        # Said out loud because it is why this matters: the wrong order does
        # not produce a slightly wrong number, it produces one nobody could
        # mistake for an hour count -- if they look. Here it is not even
        # positive: the register is signed, so the swapped words land in the
        # top bit and 100 000 hours reads as minus two thousand million.
        pump = build_f750({3419: self.HIGH, 3420: self.LOW})
        self.assertGreater(abs(pump.read(31088)), 2_000_000_000)

    def test_a_write_uses_the_same_order_as_the_read(self):
        regs = f750_registry().registers
        regs[43420] = Register(address=43420, title="counter", size="s32",
                               writable=True)
        pump = Pump("192.0.2.11", 502, 1, Registry(regs, "test F750"),
                    profile=Profile.for_model("F750"))
        pump.mb = FakeModbus()
        pump.write(31088, self.HOURS, confirmed=True)
        self.assertEqual(pump.mb.writes, [(3419, [self.LOW, self.HIGH])])
        self.assertEqual(pump.read(31088), self.HOURS)

    def test_config_overrides_the_generation_default(self):
        # menu 5.3.11 is a setting, so word_swap: false is an owner saying what
        # their own pump is set to.
        pump = Pump("192.0.2.11", 502, 1, f750_registry(),
                    profile=Profile.for_model("F750", word_swap="false"))
        pump.mb = FakeModbus({3419: self.HIGH, 3420: self.LOW})
        self.assertEqual(pump.read(31088), self.HOURS)

    def test_the_profile_reports_which_it_used(self):
        # In the backup's own header, so a snapshot full of absurd 32-bit
        # values can be recognised for what it is.
        self.assertEqual(Profile("S").as_dict()["low_word_first"], True)
        self.assertEqual(Profile("F").as_dict()["low_word_first"], True)
        self.assertFalse(Profile("F").as_dict()["word_swap_configured"])
        self.assertTrue(Profile("F", word_swap=True).as_dict()["word_swap_configured"])
        self.assertEqual(Profile("F").as_dict()["word_order_source"], "assumed")
        self.assertEqual(Profile("F", word_swap=False).as_dict()["word_order_source"],
                         "configured")


class ACorruptFrameCostsOneRegisterAndNotThePoll(unittest.TestCase):
    """A bad CRC on one register must not take the whole poll down.

    Users report a MODBUS 40 answering CRC errors for 32-bit registers that are
    not in its LOG.SET file, on a pump that is otherwise perfectly reachable
    (docs/f-series.md). This app does not try to fix that. What it must do is
    treat the register as missing -- the hourly retry every other missing
    register gets -- rather than raise out of the poll every sixty seconds.
    """

    def _pump(self):
        pump = build_f750({7006: 5, 7010: 2})
        pump.mb.read_errors[3419] = ModbusCorrupt(
            "Bad CRC on the RTU frame from the pump.")
        return pump

    def test_the_other_registers_are_still_read(self):
        pump = self._pump()
        data = pump.read_many([31088, 40027, 40031])
        self.assertEqual(data[40027]["value"], 5)
        self.assertIn("error", data[31088])

    def test_the_register_goes_into_the_hourly_retry(self):
        pump = self._pump()
        pump.read_many([31088])
        self.assertIn(31088, pump._missing, "recorded under the app's own number")
        missing = pump.missing()
        self.assertEqual([m["address"] for m in missing], [31088])
        self.assertEqual(missing[0]["physical"], 43420)
        self.assertAlmostEqual(missing[0]["retry_in"],
                               pump_module.MISSING_RETRY_SECONDS, delta=5)

    def test_it_is_not_asked_for_again_inside_the_window(self):
        pump = self._pump()
        pump.read_many([31088])
        before = len(pump.mb.requests)
        pump.read_many([31088])
        self.assertEqual(len(pump.mb.requests), before)

    def test_a_dead_link_is_still_a_dead_link(self):
        # The distinction this rests on: ModbusOffline means the pump is not
        # there, and marking twenty-five registers missing one at a time would
        # turn that into twenty-five quiet little errors and a page that looks
        # like a working pump with no values.
        pump = build_f750()
        pump.mb.read_errors[7006] = ModbusOffline("The pump closed the connection.")
        with self.assertRaises(ModbusOffline):
            pump.read_many([40027])
        self.assertEqual(pump._missing, {})


class TheTierIsDecidedOnWhatIsActuallyWritten(unittest.TestCase):
    def test_the_f_everyday_registers_are_everyday(self):
        for canonical in (40057, 40067, 40105, 40116, 40117, 40118, 40119, 40207):
            physical = F_TABLE[canonical]
            with self.subTest(canonical=canonical):
                self.assertEqual(safety.tier(canonical), "everyday")
                self.assertEqual(safety.tier(physical), "everyday",
                                 "%d is everyday but %d is not, so the button "
                                 "would be refused on an F pump"
                                 % (canonical, physical))

    def test_the_f_curve_registers_are_guarded_with_a_reason(self):
        for canonical in (40027, 40031, 40035, 40039, 40062, 40063, 40064, 40065):
            physical = F_TABLE[canonical]
            with self.subTest(canonical=canonical):
                self.assertEqual(safety.tier(physical), "guarded")
                self.assertTrue(safety.reason(physical),
                                "%d is guarded by default rather than on "
                                "purpose" % physical)


class ABackupSkipsRegistersThePumpAlreadyRefused(unittest.TestCase):
    """Regression: `backup` stopped filtering, and cost twice.

    `backup` used to read through `read_many`, which skips a register the pump
    has refused. When it moved to `_read_pairs` -- correctly, because a snapshot
    is in the pump's own numbering -- it lost the filter. Two things went wrong,
    and the second is the one that mattered: every refused register cost the
    snapshot an extra request (measured at four more on a map with three), and,
    because `_read_block` records a refusal with the time it happened, the daily
    automatic snapshot reset every refused register's hourly retry clock. A
    register the pump does not implement was then retried once a day instead of
    once an hour, which is the opposite of what MISSING_RETRY_SECONDS is for.
    """

    def setUp(self):
        self.pump = build()
        # 40027 is at wire 26 and this pump refuses it -- exception 1, which is
        # what a real S735 answers for a register it does not implement.
        self.pump.mb.read_errors[26] = ModbusError(1, "reading")

    def _backup(self):
        directory = tempfile.mkdtemp()
        with open(self.pump.backup(directory, "test"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_the_refused_register_is_asked_for_once_and_then_skipped(self):
        self.pump.read_many([40027])
        self.assertEqual([40027], [m["address"] for m in self.pump.missing()])
        self.pump.mb.requests.clear()
        self._backup()
        self.assertNotIn(26, [address for address, _count in self.pump.mb.requests])

    def test_the_snapshot_leaves_a_skipped_register_out(self):
        # Not "records it as failed": out. That is what the released version
        # did -- read_many never returned a row for it, so the loop skipped it
        # -- and `counts` says so, in_map counting it and read/failed not.
        self.pump.read_many([40027])
        snapshot = self._backup()
        self.assertNotIn(40027, [e["address"] for e in snapshot["registers"]])
        self.assertEqual(len(self.pump.registry), snapshot["counts"]["in_map"])

    def test_a_register_refused_during_the_backup_is_recorded_with_its_error(self):
        snapshot = self._backup()
        row = [e for e in snapshot["registers"] if e["address"] == 40027]
        self.assertEqual(1, len(row))
        self.assertIn("error", row[0])
        self.assertEqual(1, snapshot["counts"]["failed"])

    def test_a_backup_does_not_push_a_running_retry_clock_forward(self):
        self.pump.read_many([40027])
        first = dict(self.pump._missing)
        self._backup()
        self._backup()
        self.assertEqual(first, self.pump._missing)


class TheWordOrderIsAskedForRatherThanAssumed(unittest.TestCase):
    """48852 "Modbus40 Word Swap" is a register, so the pump can be asked.

    NIBE's MODBUS 40 manual says the factory word order is Big Endian; NIBE's
    own register map gives 48852 a factory value of 1, "swapping the words in
    32-bit variables". They cannot both be right, and the pump in front of you
    is the only thing that knows what it is actually set to. See
    profile.LOW_WORD_FIRST.
    """

    def _pump(self, answer=None, word_swap=""):
        regs = f750_registry().registers
        if answer is not None:
            regs[48852] = Register(address=48852, title="Modbus40 Word Swap",
                                   size="u8", writable=True, min=0, max=1)
        p = Pump("192.0.2.11", 502, 1, Registry(regs, "test F750"),
                 profile=Profile.for_model("F750", "", word_swap))
        p.mb = FakeModbus({} if answer is None else {8851: answer})
        return p

    def test_one_means_swapped_which_is_the_low_word_first(self):
        p = self._pump(answer=1)
        self.assertEqual("read", p.resolve_word_order())
        self.assertTrue(p.profile.low_word_first)
        self.assertTrue(p.registry.get(43420).low_word_first)

    def test_zero_means_the_high_word_first(self):
        p = self._pump(answer=0)
        self.assertEqual("read", p.resolve_word_order())
        self.assertFalse(p.profile.low_word_first)
        self.assertFalse(p.registry.get(43420).low_word_first)

    def test_a_register_that_does_not_answer_leaves_the_default(self):
        p = self._pump(answer=1)
        p.mb.read_errors[8851] = ModbusError(2, "reading")
        self.assertEqual("assumed", p.resolve_word_order())
        self.assertTrue(p.profile.low_word_first)

    def test_a_map_without_the_register_leaves_the_default(self):
        p = self._pump(answer=None)
        self.assertEqual("assumed", p.resolve_word_order())
        self.assertTrue(p.profile.low_word_first)

    def test_config_beats_the_register(self):
        # An owner who walked to menu 5.3.11 outranks a register read, for the
        # same reason a measurement beats a default.
        p = self._pump(answer=1, word_swap="false")
        self.assertEqual("configured", p.resolve_word_order())
        self.assertFalse(p.profile.low_word_first)
        self.assertEqual([], p.mb.reads)

    def test_an_s_pump_asks_nothing(self):
        p = build()
        self.assertEqual("assumed", p.resolve_word_order())
        self.assertEqual([], p.mb.reads)
        self.assertTrue(p.profile.low_word_first)

    def test_the_backup_header_says_read_or_assumed(self):
        p = self._pump(answer=0)
        p.resolve_word_order()
        directory = tempfile.mkdtemp()
        with open(p.backup(directory, ""), encoding="utf-8") as fh:
            snapshot = json.load(fh)
        self.assertEqual("read", snapshot["profile"]["word_order_source"])
        self.assertFalse(snapshot["profile"]["low_word_first"])


class SwedishRefusals(unittest.TestCase):
    """These land in a toast in a Swedish UI, so they are Swedish."""

    def test_a_read_only_register(self):
        p = build()
        with self.assertRaises(Refused) as caught:
            p.write(30002, 5, confirmed=True)
        self.assertNotIn("read-only", str(caught.exception))
        self.assertIn("skriva", str(caught.exception))

    def test_extra_hot_water_out_of_range(self):
        p = build()
        with self.assertRaises(Refused) as caught:
            p.extra_hot_water(minutes=99999)
        self.assertIn("minuter", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
