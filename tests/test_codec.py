"""Tests for everything that can be checked without a heat pump.

Run: python3 -m unittest discover tests   (no pytest, no dependencies)

These exist because the decoding and the range check are where a silent bug
turns into a real setting on real hardware. Two of the cases below are
regressions: `-1` minutes of extra hot water used to encode as 65535, and a
CSV exported from the pump used to make every register 16-bit.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nibelokal.registry import Register, Registry           # noqa: E402
from nibelokal import safety                                # noqa: E402


def reg(**kw):
    base = dict(address=40001, title="test", size="s16", factor=1)
    base.update(kw)
    return Register(**base)


class Decoding(unittest.TestCase):
    def test_signed_16(self):
        self.assertEqual(reg(size="s16", factor=10).decode([0x0200]), 51.2)
        self.assertEqual(reg(size="s16", factor=10).decode([0xFFF6]), -1.0)

    def test_signed_8_in_a_16_bit_register(self):
        # The pump may sign-extend into the high byte or leave it clear.
        self.assertEqual(reg(size="s8").decode([0xFFFF]), -1)
        self.assertEqual(reg(size="s8").decode([0x00FF]), -1)
        self.assertEqual(reg(size="s8").decode([0x0005]), 5)

    def test_unsigned_8(self):
        self.assertEqual(reg(size="u8").decode([0x0003]), 3)

    def test_32_bit_low_word_first(self):
        # NIBE TIF EN 2608 p.6: the lower address carries the low word.
        r = reg(size="s32", factor=10)
        self.assertEqual(r.decode([0x0064, 0x0000]), 10.0)
        self.assertEqual(r.decode([0xFF9C, 0xFFFF]), -10.0)

    def test_invalid_sentinels_become_none(self):
        self.assertIsNone(reg(size="s16").decode([0x8000]))
        self.assertIsNone(reg(size="u16").decode([0xFFFF]))
        self.assertIsNone(reg(size="s8").decode([0xFF80]))
        self.assertIsNone(reg(size="u32").decode([0xFFFF, 0xFFFF]))

    def test_a_legal_value_is_not_mistaken_for_the_sentinel(self):
        self.assertEqual(reg(size="s16", factor=10).decode([0x8001]), -3276.7)

    def test_enum_mapping(self):
        r = reg(size="s8", mappings={"0": "Small", "1": "Medium", "2": "Large"})
        self.assertEqual(r.decode([0x0002]), "Large")

    def test_round_trip(self):
        for size, factor, value in [("s16", 10, -12.3), ("s32", 10, -3000.0),
                                    ("u16", 1, 65000), ("s8", 1, -40), ("u32", 100, 12345.67)]:
            r = reg(size=size, factor=factor)
            self.assertAlmostEqual(r.decode(r.encode(value)), value, places=2,
                                   msg="%s factor %d" % (size, factor))


class Encoding(unittest.TestCase):
    def test_refuses_values_the_type_cannot_hold(self):
        # Regression: this used to mask to 65535 and be written to the pump.
        with self.assertRaises(ValueError):
            reg(size="u16").encode(-1)
        with self.assertRaises(ValueError):
            reg(size="u16").encode(100000)
        with self.assertRaises(ValueError):
            reg(size="s8").encode(300)

    def test_scaling_applies(self):
        self.assertEqual(reg(size="s16", factor=10).encode(51.2), [512])

    def test_enum_accepts_label_and_key(self):
        r = reg(size="s8", mappings={"0": "Small", "2": "Large"})
        self.assertEqual(r.coerce("Large"), 2)
        self.assertEqual(r.coerce(2), 2)
        with self.assertRaises(ValueError):
            r.coerce(3)                      # not one of the pump's own options
        with self.assertRaises(ValueError):
            r.coerce("Enormous")


class Scaling(unittest.TestCase):
    def test_min_max_default_are_scaled_like_values(self):
        # The map gives these as raw values; a value is already divided.
        r = reg(size="s16", factor=10, min=0.0, max=800.0, default=530.0)
        self.assertEqual((r.min, r.max, r.default), (0.0, 80.0, 53.0))

    def test_unscaled_when_factor_is_one(self):
        r = reg(size="u8", factor=1, min=0.0, max=4.0)
        self.assertEqual((r.min, r.max), (0.0, 4.0))


class Clamping(unittest.TestCase):
    def test_negative_values_inside_the_range_are_allowed(self):
        # Regression: a heating offset of -1 (the pump's own default) used to be
        # refused as "above the maximum of 10".
        r = reg(address=40031, size="s8", factor=1, min=-10.0, max=10.0)
        self.assertEqual(safety.clamp(r, -1.0), -1.0)

    def test_out_of_range_is_refused(self):
        r = reg(size="s16", factor=10, min=0.0, max=80.0)
        with self.assertRaises(safety.Refused):
            safety.clamp(r, 95.0)


class Tiers(unittest.TestCase):
    def test_blocked_beats_everything(self):
        self.assertEqual(safety.tier(40089), "blocked")
        with self.assertRaises(safety.Refused):
            safety.check(40089, confirmed=True)

    def test_guarded_needs_confirmation(self):
        self.assertEqual(safety.tier(40027), "guarded")
        with self.assertRaises(safety.Refused):
            safety.check(40027, confirmed=False)
        safety.check(40027, confirmed=True)          # must not raise

    def test_unknown_registers_are_guarded_not_free(self):
        self.assertEqual(safety.tier(49999), "guarded")
        with self.assertRaises(safety.Refused):
            safety.check(49999, confirmed=False)

    def test_blocked_ranges(self):
        self.assertEqual(safety.tier(40130), "blocked")   # floor drying
        self.assertEqual(safety.tier(45220), "blocked")   # external sensor injection

    def test_everyday_passes(self):
        safety.check(40226, confirmed=False)


class CsvLoading(unittest.TestCase):
    # The column names NIBE's own "Export all registers" writes.
    HEADER = ("Title\tRegister type\tRegister\tDivision factor\tUnit\t"
              "Size of variable\tMin value\tMax value\tDefault value\n")

    def _write(self, rows, encoding="utf-8"):
        fh = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding=encoding)
        fh.write(self.HEADER)
        fh.writelines(rows)
        fh.close()
        return fh.name

    def test_numeric_sizes_are_understood(self):
        # Regression: the pump writes "3" for s32, not "s32". Everything used to
        # fall back to s16, so compressor hours read as a negative number.
        path = self._write([
            "Total run time compressor\tMODBUS_INPUT_REGISTER\t1087\t1\th\t3\t0\t0\t0\n",
            "Hot water top\tMODBUS_INPUT_REGISTER\t8\t10\t°C\t2\t0\t0\t0\n",
        ])
        try:
            r = Registry.from_csv(path)
            self.assertEqual(r.get(31088).size, "s32")
            self.assertEqual(r.get(31088).count, 2)
            self.assertEqual(r.get(30009).size, "s16")
            self.assertEqual(r.get(31088).decode([0xF000, 0x0001]), 126976)
        finally:
            os.unlink(path)

    def test_latin1_export_is_readable(self):
        path = self._write(
            ["Hot water top\tMODBUS_INPUT_REGISTER\t8\t10\t°C\t2\t0\t0\t0\n"],
            encoding="latin-1")
        try:
            self.assertEqual(Registry.from_csv(path).get(30009).unit, "°C")
        finally:
            os.unlink(path)

    def test_equal_min_and_max_means_no_range(self):
        # The export writes 0/0 for "unspecified". Kept literally, every write
        # of anything but zero would be refused.
        path = self._write(
            ["More hot water\tMODBUS_HOLDING_REGISTER\t225\t1\t\t5\t0\t0\t0\n"])
        try:
            reg_ = Registry.from_csv(path).get(40226)
            self.assertIsNone(reg_.min)
            self.assertIsNone(reg_.max)
        finally:
            os.unlink(path)

    def test_holding_and_input_are_separate_address_spaces(self):
        path = self._write([
            "An input\tMODBUS_INPUT_REGISTER\t8\t10\t°C\t2\t0\t0\t0\n",
            "A setting\tMODBUS_HOLDING_REGISTER\t8\t10\t°C\t2\t0\t0\t0\n",
        ])
        try:
            r = Registry.from_csv(path)
            self.assertIn(30009, r.registers)
            self.assertIn(40009, r.registers)
            self.assertTrue(r.get(40009).writable)
            self.assertFalse(r.get(30009).writable)
        finally:
            os.unlink(path)


class Addressing(unittest.TestCase):
    def test_wire_addresses(self):
        self.assertEqual((reg(address=30009).kind, reg(address=30009).wire), (3, 8))
        self.assertEqual((reg(address=40226).kind, reg(address=40226).wire), (4, 225))


class Blocking(unittest.TestCase):
    def test_offsets_stay_correct_with_a_32_bit_register_in_the_block(self):
        from nibelokal.pump import _blocks
        regs = [reg(address=31026, size="s32"), reg(address=31028, size="s16")]
        blocks = _blocks(sorted(regs, key=lambda r: r.wire))
        self.assertEqual(len(blocks), 1)
        block = blocks[0]
        start = block[0].wire
        self.assertEqual([r.wire - start for r in block], [0, 2])

    def test_a_wide_gap_splits_the_block(self):
        from nibelokal.pump import _blocks
        regs = [reg(address=40001), reg(address=40100)]
        self.assertEqual(len(_blocks(regs)), 2)

    def test_a_block_never_exceeds_the_query_limit(self):
        from nibelokal.pump import _blocks
        from nibelokal.modbus import MAX_REGS_PER_QUERY
        regs = [reg(address=40001 + i) for i in range(60)]
        for block in _blocks(regs):
            span = block[-1].wire + block[-1].count - block[0].wire
            self.assertLessEqual(span, MAX_REGS_PER_QUERY)


class RateLimit(unittest.TestCase):
    def test_the_budget_holds_under_threads(self):
        # Regression: without its own lock this measured 560 registers/s against
        # a documented limit of 100.
        import threading
        import time
        from nibelokal.modbus import _Budget

        budget, spent, lock = _Budget(limit=100), [], threading.Lock()

        def worker():
            for _ in range(4):
                budget.spend(20)
                with lock:
                    spent.append(time.monotonic())

        threads = [threading.Thread(target=worker) for _ in range(6)]
        start = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        worst = max(
            sum(1 for t in spent if now - 1.0 < t <= now) * 20
            for now in spent
        )
        self.assertLessEqual(worst, 120, "spent %d registers in one second" % worst)
        # 24 calls of 20 registers at 100/s cannot finish faster than ~4 s.
        self.assertGreater(time.monotonic() - start, 3.0)


class ValuesThatAreNotNumbers(unittest.TestCase):
    """json.loads("1e999") is float("inf"), and it reached the encoder.

    A JSON body is not a form: `{"value": 1e999}` is valid JSON, parses to a
    float Python is perfectly happy with, and then int(round(inf)) raises
    OverflowError -- which is not a ValueError, so it sailed past the router's
    "a bad value is a 400" arm and left as a 502 with a stack trace blaming the
    pump. A value no register can hold is a bad request.
    """

    def test_infinity_is_refused_by_a_plain_register(self):
        for value in (float("inf"), float("-inf"), float("nan")):
            with self.assertRaises(ValueError, msg=repr(value)):
                reg(size="s16", factor=10).coerce(value)

    def test_and_by_a_mapped_one(self):
        r = reg(size="s8", mappings={"0": "Small", "2": "Large"})
        for value in (float("inf"), float("nan")):
            with self.assertRaises(ValueError, msg=repr(value)):
                r.coerce(value)

    def test_encode_refuses_it_too(self):
        with self.assertRaises(ValueError):
            reg(size="s16", factor=10).encode(float("inf"))

    def test_it_is_a_value_error_and_not_an_overflow_error(self):
        # The distinction is the whole point: server.py answers 400 for
        # ValueError and 502 for anything else.
        try:
            reg(size="s16").coerce(float("inf"))
        except ValueError:
            pass
        except Exception as exc:                          # noqa: BLE001
            self.fail("coerce raised %s, which the router answers 502 for"
                      % type(exc).__name__)

    def test_a_huge_but_finite_number_is_still_refused_by_the_range(self):
        with self.assertRaises(ValueError):
            reg(size="s16", factor=1).encode(10 ** 20)

    def test_ordinary_values_still_pass(self):
        self.assertEqual(reg(size="s16", factor=10).coerce("51.2"), 51.2)
        self.assertEqual(reg(size="s16", factor=10).coerce(0), 0.0)
        self.assertFalse(reg(size="u8").coerce(False))


if __name__ == "__main__":
    unittest.main()
