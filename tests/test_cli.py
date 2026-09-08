"""The command line: what `status`, `read` and `backup` print, and in what order.

Three things here are decisions rather than formatting.

`status` asks the pump what it is *last*. Function 0x2B is optional in the
Modbus specification and a device may implement "no" by saying nothing, so
asking first can cost a timeout before a single register the command was run for
has been read. The dashboard read must come out byte-identical to what it was
before identification existed, and the identification line must be the last
thing on the page.

`read` on an F-series pump has three answers, not two: the register, "not in the
map", and "this app refuses to translate that one, and here is why". The third
was printing as the second, which sends somebody looking through their own
documentation for a register that is right there holding something else.

`backup` on an F pump says how long it is about to take. One register per
request at 2.1 s is twenty minutes of a command that prints nothing until it
finishes.

`build` is here too, for the case a released version accepted and the F-series
work refused: a `register_csv` with no `model` and no `generation`.
"""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nibelokal import __main__ as cli                        # noqa: E402
from nibelokal.modbus import ModbusError                     # noqa: E402
from nibelokal.profile import Profile                        # noqa: E402
from nibelokal.pump import Pump                              # noqa: E402
from nibelokal.registry import Register, Registry            # noqa: E402


class FakeModbus:
    """Enough of ModbusTCP for the CLI, remembering the order it was called in."""

    def __init__(self, words=None, device_id=None):
        self.words = dict(words or {})
        self.calls = []
        self._device_id = device_id

    def read(self, kind, address, count):
        self.calls.append(("read", address, count))
        return [self.words.get(address + i, 0) for i in range(count)]

    def write(self, address, values):                        # pragma: no cover
        self.calls.append(("write", address, list(values)))

    def device_id(self):
        self.calls.append(("device_id",))
        if self._device_id is None:
            raise ModbusError(1, "reading the device identification")
        return self._device_id


def s_pump(device_id=None) -> Pump:
    regs = {
        30002: Register(address=30002, title="Utetemperatur", size="s16", factor=10),
        30009: Register(address=30009, title="Varmvatten topp", size="s16", factor=10),
    }
    p = Pump("192.0.2.10", 502, 1, Registry(regs, "test"))
    p.mb = FakeModbus({1: 34, 8: 512}, device_id)
    return p


def f_pump() -> Pump:
    regs = {
        40004: Register(address=40004, title="BT1 Outdoor Temperature",
                        size="s16", factor=10),
    }
    p = Pump("192.0.2.11", 502, 1, Registry(regs, "test F750"),
             profile=Profile.for_model("F750"))
    p.mb = FakeModbus({3: 34})
    return p


def run(pump, *args) -> str:
    """One CLI command against `pump`, returning what it printed."""
    original = cli.build
    cli.build = lambda a: (pump, {"backup_dir": tempfile.mkdtemp(),
                                  "poll_seconds": 60}, ".")
    buffer = io.StringIO()
    try:
        with redirect_stdout(buffer):
            cli.main(["-c", "unused.yaml"] + list(args))
    finally:
        cli.build = original
    return buffer.getvalue()


class StatusAsksWhatThePumpIsLast(unittest.TestCase):
    def test_the_registers_are_read_before_anything_else(self):
        pump = s_pump()
        run(pump, "status")
        self.assertEqual("read", pump.mb.calls[0][0])
        self.assertEqual(("device_id",), pump.mb.calls[-1])

    def test_the_identification_line_is_the_last_line(self):
        pump = s_pump({"vendor": "NIBE", "product": "S735", "revision": "1234"})
        lines = [line for line in run(pump, "status").splitlines() if line.strip()]
        self.assertIn("30002", lines[0])
        self.assertIn("device id", lines[-1])
        self.assertIn("S735", lines[-1])

    def test_a_pump_that_refuses_the_function_still_prints_its_registers(self):
        pump = s_pump(device_id=None)
        out = run(pump, "status")
        self.assertIn("30002", out)
        self.assertIn("not answered", out.splitlines()[-1])


class ReadSaysWhyARegisterHasNoEquivalent(unittest.TestCase):
    def test_an_f_pump_gets_the_reason_and_not_not_in_map(self):
        # 40020 IS a register on an F750 -- EB100-BT16 Evaporator temp -- which
        # is exactly why this app refuses to translate the S-series holiday
        # status onto it, and exactly why "(not in map)" is the wrong answer.
        out = run(f_pump(), "read", "40020")
        self.assertNotIn("(not in map)", out)
        self.assertIn("no F-series equivalent", out)
        self.assertIn("Evaporator", out)

    def test_a_register_the_map_simply_lacks_still_says_not_in_map(self):
        out = run(f_pump(), "read", "49999")
        self.assertIn("(not in map)", out)

    def test_a_register_that_exists_reads_normally(self):
        out = run(f_pump(), "read", "30002")
        self.assertIn("BT1 Outdoor Temperature", out)
        self.assertIn("3.4", out)

    def test_an_s_pump_is_unchanged(self):
        # Nothing has no equivalent on an S pump, so the extra branch is dead
        # there and the output is the line it always was. NO_F_EQUIVALENT is a
        # fact about an F profile and not about the address: an S-series owner
        # asking about a register their own map lacks must not be answered with
        # a paragraph about what it is on an F750.
        out = run(s_pump(), "read", "40020")
        self.assertIn("(not in map)", out)
        self.assertNotIn("equivalent", out)


class BackupSaysHowLongItWillTakeOnAnFPump(unittest.TestCase):
    def test_an_f_pump_is_warned_before_it_starts(self):
        out = run(f_pump(), "backup")
        self.assertIn("one request each", out)
        self.assertIn("2.1 s", out)
        # And before the snapshot line, not after it.
        self.assertLess(out.index("one request each"), out.index("Snapshot:"))

    def test_an_s_pump_says_nothing_extra(self):
        out = run(s_pump(), "backup")
        self.assertNotIn("one request each", out)
        self.assertTrue(out.startswith("Snapshot:"), out[:60])


class BuildDerivesTheGenerationFromACsv(unittest.TestCase):
    """`register_csv` with no `model` and no `generation` is a whole config.

    config.example.yaml says model is "required unless register_csv is set", a
    released version accepted exactly that, and the F-series work turned it into
    a startup error. The map is what knows: 40027 is the heating curve on an S
    and 47007 on an F.
    """

    class Args:
        def __init__(self, config):
            self.config = config

    def _config(self, rows):
        directory = tempfile.mkdtemp()
        csv_path = os.path.join(directory, "registers.csv")
        with open(csv_path, "w", encoding="utf-8") as fh:
            fh.write("Title,ID,Mode\n" + rows)
        path = os.path.join(directory, "config.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("host: 192.0.2.10\nregister_csv: %s\n" % csv_path)
        return path

    def test_an_s_map_gives_an_s_profile(self):
        pump, _cfg, _base = cli.build(self.Args(self._config(
            "Heating curve,40027,R/W\nOutdoor,30002,R\n")))
        self.assertEqual("S", pump.profile.generation)

    def test_an_f_map_gives_an_f_profile(self):
        pump, _cfg, _base = cli.build(self.Args(self._config(
            "Heat Curve S1,47007,R/W\nBT1,40004,R\n")))
        self.assertEqual("F", pump.profile.generation)

    def test_a_map_with_both_markers_is_refused_naming_them(self):
        with self.assertRaises(SystemExit) as caught:
            cli.build(self.Args(self._config(
                "Heating curve,40027,R/W\nHeat Curve S1,47007,R/W\n")))
        self.assertIn("40027", str(caught.exception))
        self.assertIn("47007", str(caught.exception))

    def test_a_map_with_neither_is_refused_naming_them(self):
        with self.assertRaises(SystemExit) as caught:
            cli.build(self.Args(self._config("Outdoor,30002,R\n")))
        self.assertIn("40027", str(caught.exception))
        self.assertIn("47007", str(caught.exception))

    def test_an_explicit_generation_still_wins(self):
        path = self._config("Outdoor,30002,R\n")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("generation: F\n")
        pump, _cfg, _base = cli.build(self.Args(path))
        self.assertEqual("F", pump.profile.generation)


if __name__ == "__main__":
    unittest.main()
