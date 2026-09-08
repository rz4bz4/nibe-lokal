"""Tests for the write gate, and for the model-blindness it is easy to fall into.

The tiers in nibelokal/safety.py were first written against one S735. The app
supports the whole S series, and the models do not number the same setting the
same way: floor drying is 40121-40135 on an S735 and 47276-47290 on an SMO 20,
the heating medium pump's operating mode is 40096 on one and 47138 on the other.
A gate that blocks only the author's addresses is a gate that quietly opens on
somebody else's pump, and nothing in the app would say so.

So the substantial test here is not "is 40121 blocked". It walks every register
map the `nibe` package ships, picks out every writable register whose *title*
puts it in a category this project has decided is dangerous, and fails if any of
them is reachable. Titles rather than addresses, because the address is exactly
the thing that varies.

The rest guards the parts that go wrong when someone edits the file: a widened
range swallowing an everyday register, two ranges disagreeing about why an
address is blocked, and the default-to-guarded rule that makes an unclassified
register safe rather than free.

Everything that needs the `nibe` package skips without it -- it is an optional
dependency, and the app runs from a CSV exported from the pump instead.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nibelokal import safety                                    # noqa: E402
from nibelokal.safety import Refused                            # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    from nibe.heatpump import Model as NibeModel
except Exception:                                               # pragma: no cover
    NibeModel = None


def all_maps():
    """{model name: {address: coil dict}} for every map the package ships.

    CUSTOM is skipped: it is an empty placeholder for a user-supplied map, not a
    pump.
    """
    maps = {}
    for model in NibeModel:
        if model.name == "CUSTOM":
            continue
        maps[model.name] = {int(k): v for k, v in model.get_coil_data().items()}
    return maps


# The categories this project has decided are dangerous, as title patterns.
#
# Each pattern is written against the titles the maps actually use, including
# the places where two models spell one thing differently ("heating medium pump"
# and "heat medium pump", "Fuse" and "Transformer ratio"). If the `nibe` package
# adds a model that names one of these something new, this test does not catch
# it -- but it does catch every model the package ships today, which is the
# failure that was actually there.
#
# A pattern has to describe the *category*, not the S735's phrasing of it. The
# pump-mode pattern below said "heat(ing) medium pump" and nothing else, so it
# could not see the brine pump's identical selector at 40097 and 47139 even
# though every map agrees they are the same kind of register with the same
# enumeration. Widening a pattern is not free: everything it newly matches has
# to be blocked, or the first test in EveryModel fails. That is the point --
# widening it is how you find out what the gate was missing.
CATEGORIES = {
    "compressor frequency window":
        r"^(min|max)\.? compressor frequency$",
    "compressor frequency blocking bands":
        r"blockfreq",
    # Circulation pumps the compressor depends on: the heating medium pump on
    # the warm side, the brine pump on the cold one. Matched by the shape of
    # the register rather than by which loop it is in -- "operating mode",
    # "operational mode" or "op. mode", then the pump. The F-series map spells
    # out the enumeration these share (10 intermittent, 20 continuous,
    # 30 economy, 40 auto) in the info text of 47138 and 47139.
    #
    # It deliberately does not reach the per-heat-pump circulation and charge
    # pump modes -- "Operating mode circulation pump heating heat pump 1"
    # (40784), "Op. mode charge pump (EB101)" (40749) and their siblings. Those
    # are a plain 0-1 with no mapping and no info text in any map the package
    # ships, so nothing says whether 0 is *off* or *intermittent*, and those
    # two readings differ by exactly the hazard. They stay guarded; the
    # reasoning is in docs/registers.md.
    "circulation pump operating mode and manual speed":
        r"^(operating|operational|op\.) mode (heat(ing)? medium|brine( medium)?) "
        r"pump(, cooling)?$"
        r"|^heat(ing)? medium pump manual speed$"
        r"|^manual heat(ing)? medium pump speed$"
        r"|^speed of circulation pump for heating$",
    "AUX selectors driven over Modbus":
        r"^aux from modbus$|^input aux\d+$",
    "main fuse and current transformer ratio":
        r"^fuse$|^(current )?transformer ratio$",
    "forced control":
        r"forced control",
    "floor drying programme":
        r"^floor drying",
    # "Max difference, SES priority 1 energy source" on an S-series map and
    # "Max diff. SES prio 1." on a VVM one are Smart Energy Source under an
    # abbreviation nothing else in any map uses. Matching only the spelt-out
    # name missed four writable registers on twenty models.
    "smart energy source":
        r"smart energy source|\bses\b",
    "external sensor injection":
        r"^external reading|^external sensors?,",
    "start guide state":
        r"start guide",
    "externally calculated supply":
        r"^modbus use ext\. calc supply$|^modbus external control$",
    "forcing the inverter":
        r"^(initiate inverter|force initiated inverter|force inverter init)$",
    "emergency mode additional heat":
        r", emergency mode$",
    # "(SPA), heating influence" is Smart Price Adaption under an abbreviation,
    # and matching only the spelt-out name left the whole of its menu -- the
    # per-circuit activations and the influence knobs, 40845-40852 and 40903 --
    # guarded while the switch above them was blocked.
    "smart price adaption":
        r"^energy price \d|smart price adaption|\(spa\)",
}


@unittest.skipIf(NibeModel is None, "the nibe package is not installed")
class EveryModel(unittest.TestCase):
    """The gate has to hold on models the author does not own."""

    @classmethod
    def setUpClass(cls):
        cls.maps = all_maps()

    def test_every_dangerous_category_is_blocked_on_every_model(self):
        reachable = []
        for category, pattern in CATEGORIES.items():
            rx = re.compile(pattern, re.I)
            for model, coils in self.maps.items():
                for address, coil in coils.items():
                    # Only writes are gated, so a read-only register at a
                    # blocked address costs nothing and proves nothing.
                    if not coil.get("write"):
                        continue
                    if not rx.search(coil["title"]):
                        continue
                    if safety.tier(address) != "blocked":
                        reachable.append(
                            "%s: %d %r on %s is %s"
                            % (category, address, coil["title"], model,
                               safety.tier(address))
                        )
        self.assertEqual([], sorted(set(reachable)))

    def test_the_categories_actually_match_something(self):
        """A typo in a pattern above would make the test above pass vacuously."""
        for category, pattern in CATEGORIES.items():
            rx = re.compile(pattern, re.I)
            hits = sum(
                1
                for coils in self.maps.values()
                for coil in coils.values()
                if coil.get("write") and rx.search(coil["title"])
            )
            self.assertGreater(hits, 0, "%s matches no writable register" % category)

    def test_blocking_costs_no_writable_register_outside_a_category(self):
        """What the blocked ranges cost, stated as a list rather than a hope.

        Widening a range to cover another model's addresses is free only if
        nothing else lives there. This pins the exceptions: every writable
        register the gate blocks whose title is not one of the categories above.
        A new entry here means a range grew over somebody's working feature, and
        the diff should say why.
        """
        known = {
            # Blocked on purpose although they are not dangerous in themselves:
            # on the measured S735 the map and the pump disagree about this
            # block. See the comment in safety.py. All three are writable on
            # the S735 and S735C and on no other map, so the measurement covers
            # every model the block reaches -- which is what 41103 and 41104,
            # writable on twelve and four maps, did not.
            (41100, "Reduced ventilation"),
            (41101, "High outdoor temperature"),
            (41102, "OEK"),
        }
        rx = re.compile("|".join(CATEGORIES.values()), re.I)
        collateral = set()
        for coils in self.maps.values():
            for address, coil in coils.items():
                if not coil.get("write"):
                    continue
                if safety.tier(address) != "blocked":
                    continue
                if not rx.search(coil["title"]):
                    collateral.add((address, coil["title"]))
        self.assertEqual(set(), collateral - known)

    def test_nothing_in_the_everyday_tier_is_an_sg_ready_register(self):
        """The mistake fix A undid, stated so it cannot come back.

        40761 sat in EVERYDAY described as "SG Ready / smart grid input". Every
        S-series map titles it *Heating (SG Ready)*: the pump's own menu saying
        whether SG Ready may touch the heating, sibling of 40762 Cooling and
        40763 Hot water, both of which were guarded the whole time. The input
        is 43033 and 46009, and neither was in any tier by name.
        """
        rx = re.compile(r"sg.?ready", re.I)
        offenders = set()
        for coils in self.maps.values():
            for address, coil in coils.items():
                if address in safety.EVERYDAY and rx.search(coil["title"]):
                    offenders.add((address, coil["title"]))
        self.assertEqual(set(), offenders)

    def test_the_sg_ready_influence_switches_share_one_tier(self):
        """40761, 40762 and 40763 are one setting per circuit. One tier.

        They are the same register three times over -- u8 0/1, default 1, one
        each for heating, cooling and hot water -- so a gate that treats one
        differently from the others is not reasoning about the feature, it is
        reasoning about an address.
        """
        tiers = {a: safety.tier(a) for a in (40761, 40762, 40763)}
        self.assertEqual(1, len(set(tiers.values())), tiers)
        # And the same setting at the address the F generation uses for it.
        self.assertEqual(safety.tier(40761), safety.tier(48282))

    def test_no_everyday_or_guarded_register_is_blocked_on_any_model(self):
        """The tiers must not contradict each other, whichever model is loaded."""
        for address in list(safety.EVERYDAY) + list(safety.GUARDED):
            self.assertNotEqual(
                "blocked", safety.tier(address),
                "%d is listed as an everyday or guarded setting but a range blocks it"
                % address,
            )


class Ranges(unittest.TestCase):
    """These hold with or without the nibe package."""

    def test_no_everyday_or_guarded_address_falls_inside_a_range(self):
        for lo, hi, why in safety.BLOCKED_RANGES:
            for address in list(safety.EVERYDAY) + list(safety.GUARDED):
                self.assertFalse(
                    lo <= address <= hi,
                    "range %d-%d (%s) swallows %d, which is listed as everyday or "
                    "guarded" % (lo, hi, why, address),
                )

    def test_no_blocked_single_is_also_everyday_or_guarded(self):
        for address in safety.BLOCKED:
            self.assertNotIn(address, safety.EVERYDAY)
            self.assertNotIn(address, safety.GUARDED)

    def test_everyday_and_guarded_do_not_overlap(self):
        self.assertEqual(set(), set(safety.EVERYDAY) & set(safety.GUARDED))

    def test_overlapping_ranges_agree_about_why(self):
        """Two ranges may cover an address only if they give the same reason.

        reason() returns whichever comes first in the list, so an overlap with
        two different explanations means the message a user gets depends on
        list order. That is a bug waiting for the next edit.
        """
        ranges = safety.BLOCKED_RANGES
        for i, (lo1, hi1, why1) in enumerate(ranges):
            for lo2, hi2, why2 in ranges[i + 1:]:
                if lo1 <= hi2 and lo2 <= hi1:
                    self.assertEqual(
                        why1, why2,
                        "ranges %d-%d and %d-%d overlap with different reasons"
                        % (lo1, hi1, lo2, hi2),
                    )

    def test_ranges_are_the_right_way_round(self):
        for lo, hi, why in safety.BLOCKED_RANGES:
            self.assertLessEqual(lo, hi, why)

    def test_every_blocked_address_has_a_reason(self):
        for address, why in safety.BLOCKED.items():
            self.assertTrue(why.strip(), address)
            self.assertTrue(safety.reason(address).strip())
        for lo, hi, why in safety.BLOCKED_RANGES:
            self.assertTrue(why.strip(), (lo, hi))
            self.assertTrue(safety.reason(lo).strip())


class Tiers(unittest.TestCase):

    def test_unclassified_defaults_to_guarded(self):
        """A register nobody has thought about is not a register to write freely."""
        unclassified = 31337
        self.assertNotIn(unclassified, safety.EVERYDAY)
        self.assertNotIn(unclassified, safety.GUARDED)
        self.assertNotIn(unclassified, safety.BLOCKED)
        self.assertEqual("guarded", safety.tier(unclassified))
        with self.assertRaises(Refused):
            safety.check(unclassified, confirmed=False)
        safety.check(unclassified, confirmed=True)

    def test_blocked_beats_everything(self):
        self.assertEqual("blocked", safety.tier(40089))
        with self.assertRaises(Refused):
            safety.check(40089, confirmed=True)
        with self.assertRaises(Refused):
            safety.check(40089, confirmed=True, allow_guarded=False)

    def test_everyday_needs_no_confirmation(self):
        self.assertEqual("everyday", safety.tier(40226))
        safety.check(40226, confirmed=False)
        safety.check(40226, confirmed=False, allow_guarded=False)

    def test_guarded_needs_confirmation_and_can_be_switched_off(self):
        self.assertEqual("guarded", safety.tier(40031))
        with self.assertRaises(Refused):
            safety.check(40031, confirmed=False)
        safety.check(40031, confirmed=True)
        with self.assertRaises(Refused):
            safety.check(40031, confirmed=True, allow_guarded=False)

    def test_the_refusal_says_which_register_and_why(self):
        with self.assertRaises(Refused) as caught:
            safety.check(40121, confirmed=True)
        message = str(caught.exception)
        self.assertIn("40121", message)
        self.assertIn("floor drying", message.lower())


def doc_addresses(section):
    """Every address the tables under one "## " heading in docs/registers.md name.

    Only the first column is read, so prose in a "why" cell that mentions a
    register does not count as listing it. That first column holds things like
    "42741 + 42742" and "41106-41140, 41173-41176", with en dashes; this reads
    both forms. Lines that are not table rows are ignored, which is what lets the
    Blocked section carry paragraphs about registers it deliberately does not
    block without those addresses being read back as claims.
    """
    path = os.path.join(REPO, "docs", "registers.md")
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    in_table = False
    addresses = set()
    for line in lines:
        if line.startswith("## "):
            in_table = line.strip() == section
            continue
        if not in_table or not line.startswith("|"):
            continue
        cell = line.split("|")[1]
        if set(cell.strip()) <= set("- :") or "Register" in cell:
            continue                                    # header or separator row
        for lo, hi in re.findall(r"(\d{5})\s*[-–—]\s*(\d{5})", cell):
            addresses.update(range(int(lo), int(hi) + 1))
        cell = re.sub(r"\d{5}\s*[-–—]\s*\d{5}", " ", cell)
        addresses.update(int(n) for n in re.findall(r"\d{5}", cell))
    return addresses


def doc_blocked_addresses():
    return doc_addresses("## Blocked")


def code_blocked_addresses():
    addresses = set(safety.BLOCKED)
    for lo, hi, _ in safety.BLOCKED_RANGES:
        addresses.update(range(lo, hi + 1))
    return addresses


class TheFSeriesRowsAreClassifiedAsIntended(unittest.TestCase):
    """The tiers are consulted with the address that is actually written.

    On an F-series pump that is the physical address, and the S-series rows
    mean nothing there. Two of the app's buttons write with confirm=False, so a
    register missing from EVERYDAY does not get confirmed anyway -- it gets
    *refused*, quoting a number the owner has never seen.
    """

    #: Canonical -> physical for the everyday registers, spelled out here
    #: rather than imported from the table, so that a change to the table has
    #: to be made in two places on purpose.
    EVERYDAY_PAIRS = {
        40057: 47041,       # hot water comfort mode
        40067: 47051,       # periodic hot water interval
        40105: 47260,       # ventilation / fan mode
        40116: 47271, 40117: 47272, 40118: 47273, 40119: 47274,   # return times
        40207: 47398,       # room setpoint, climate system 1
    }

    GUARDED_PAIRS = {
        40027: 47007,       # heating curve
        40031: 47011,       # heating offset
        40035: 47015,       # min supply
        40039: 47019,       # max supply
        40046: 47026,       # own curve P1
        40040: 47020,       # own curve P7
        40062: 47046, 40063: 47047, 40064: 47048, 40065: 47049,   # hot water
        40103: 47212,       # max internal additional heat
        40110: 47265,       # exhaust fan normal
        40238: 47137,       # operating mode
    }

    def test_the_everyday_rows_really_are_everyday(self):
        for canonical, physical in sorted(self.EVERYDAY_PAIRS.items()):
            with self.subTest(canonical=canonical):
                self.assertEqual(safety.tier(canonical), "everyday")
                self.assertEqual(
                    safety.tier(physical), "everyday",
                    "%d is everyday and %d is not, so a button that writes it "
                    "without a dialog would be refused" % (canonical, physical))

    def test_an_everyday_write_needs_no_confirm_at_either_address(self):
        for physical in sorted(self.EVERYDAY_PAIRS.values()):
            with self.subTest(physical=physical):
                safety.check(physical, confirmed=False)     # must not raise

    def test_the_guarded_rows_are_guarded_and_say_why(self):
        for canonical, physical in sorted(self.GUARDED_PAIRS.items()):
            with self.subTest(canonical=canonical):
                self.assertEqual(safety.tier(physical), "guarded")
                self.assertTrue(
                    safety.reason(physical),
                    "%d falls through to the guarded default, so a refusal "
                    "quotes an empty reason" % physical)

    def test_a_guarded_write_is_still_refused_without_a_confirm(self):
        for physical in sorted(self.GUARDED_PAIRS.values()):
            with self.subTest(physical=physical):
                with self.assertRaises(Refused):
                    safety.check(physical, confirmed=False)

    def test_the_f_hot_water_boost_is_everyday(self):
        # 48132 is the F generation's whole extra-hot-water feature. This app's
        # own button will not write it, but it counts down and stops by itself,
        # which is the property the everyday tier is about.
        self.assertEqual(safety.tier(48132), "everyday")
        self.assertIn("3 h", safety.reason(48132))

    def test_no_f_row_landed_inside_a_blocked_range_by_accident(self):
        # The floor drying programme is 47276-47291 on the F generation, and
        # the fan registers sit just below it. A range that grew by two would
        # swallow the ventilation boost.
        for address in list(self.EVERYDAY_PAIRS.values()) + [48132]:
            with self.subTest(address=address):
                self.assertNotEqual(safety.tier(address), "blocked")


class DocumentationMatchesTheCode(unittest.TestCase):
    """docs/registers.md is what people read before they trust the gate.

    It drifted once already -- roughly a dozen addresses safety.py blocked were
    missing from the table -- and a table that is quietly incomplete is worse
    than no table, because it invites the reader to conclude a register is
    writable. There is no generator: the file is prose with reasons in it. So
    this is the thing that keeps them honest.
    """

    def test_the_blocked_table_lists_exactly_what_the_code_blocks(self):
        in_doc = doc_blocked_addresses()
        in_code = code_blocked_addresses()
        self.assertEqual(
            set(), in_code - in_doc,
            "blocked in safety.py but missing from the Blocked table in "
            "docs/registers.md",
        )
        self.assertEqual(
            set(), in_doc - in_code,
            "listed as blocked in docs/registers.md but not blocked by safety.py",
        )

    def test_the_table_was_actually_found(self):
        self.assertGreater(len(doc_blocked_addresses()), 50)

    def test_the_everyday_table_lists_exactly_the_everyday_dict(self):
        self.assertEqual(set(safety.EVERYDAY), doc_addresses("## Everyday"))

    def test_the_guarded_table_lists_exactly_the_guarded_dict(self):
        """The looser half of the same drift.

        A register missing from GUARDED still lands on guarded, because that is
        what tier() does with anything it has not been told about -- so this one
        cannot be caught by writing to the pump, only by reading both files. The
        seven hot water temperatures 40059-40065 were listed as a range here and
        as four addresses in the code for exactly that reason.
        """
        self.assertEqual(set(safety.GUARDED), doc_addresses("## Guarded"))


if __name__ == "__main__":
    unittest.main()
