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
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nibelokal import pump as pump_module                     # noqa: E402
from nibelokal.modbus import ModbusError                      # noqa: E402
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

    def read(self, kind, address, count):
        self.reads.append(address)
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
