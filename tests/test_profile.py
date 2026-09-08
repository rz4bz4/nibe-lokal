"""The register translation, checked against the maps it was read out of.

nibelokal/profile.py is a hand-built table. It cannot be generated -- NIBE
renamed nearly everything between the two generations, and matching the
addresses this app uses by title against the F750 map finds exactly one of 58 --
so what keeps it honest is this file.

Four things are asserted, and each of them is a way the table can rot:

* **The S path is the identity.** Not "mostly": every address the app uses
  comes back unchanged, because the author's own pump runs this and a
  translation there would be a silent behaviour change.
* **Every row exists and agrees.** For each F model the `nibe` package ships,
  every physical address in the table is in that model's map (or absent, which
  is fine and drops out by itself), and where it is present it has the same
  unit, the same division factor and the same writability as the S735 entry for
  the canonical address. A unit mismatch is how a temperature becomes a tenth
  of a temperature.
* **The inverse is a bijection.** The inverse map is what names a register in a
  backup taken from an F pump.
* **Nothing the app uses is unaccounted for.** Every address in pump.DASHBOARD,
  settings.ADDRESSES, every R_* in advisor.py and alarms.py, the fan, hot water
  and ventilation registers in pump.py, and every five-digit number in
  web/index.html is either in the table or in NO_F_EQUIVALENT with a reason.
  That is the one that fails the build when somebody adds an S-only literal
  somewhere, which is exactly what this whole exercise is about.

The `nibe` package is an optional dependency, so everything that needs a map
skips without it.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nibelokal import advisor, alarms, pump as pump_module, settings   # noqa: E402
from nibelokal.profile import (F_MODELS, F_TABLE, NO_F_EQUIVALENT,     # noqa: E402
                               S_MODELS, F_MARKER, S_MARKER, Profile,
                               generation_from_addresses)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    from nibe.heatpump import Model as NibeModel
except Exception:                                               # pragma: no cover
    NibeModel = None


def coils(name: str) -> dict:
    return {int(k): v for k, v in NibeModel[name].get_coil_data().items()}


def web_addresses() -> set[int]:
    """Every five-digit register number web/index.html names.

    Deliberately the same blunt grep the brief describes rather than something
    cleverer, because the page is the one place in this project where a
    register number is a string in a template and nothing checks it. A number
    that is not a register at all -- `setInterval(tick, 30000)` -- is caught
    here and excused by name in NO_F_EQUIVALENT, which is more useful than a
    regex that quietly skips it.
    """
    with open(os.path.join(REPO, "web", "index.html"), encoding="utf-8") as fh:
        text = fh.read()
    return {int(n) for n in re.findall(r"\b[34]\d{4}\b", text)
            if 30000 <= int(n) <= 49999}


def app_addresses() -> set[int]:
    """Every canonical register address this app actually uses."""
    used = set(pump_module.DASHBOARD) | set(settings.ADDRESSES)
    used |= set(pump_module.FAN_SPEED_REGISTER.values())
    used |= set(pump_module.FAN_RETURN_REGISTER.values())
    used |= {pump_module.R_MORE_HW, pump_module.R_MORE_HW_MINUTES,
             pump_module.R_VENT_MODE}
    used |= {alarms.R_ALARM, alarms.R_CLASS1}
    # Every R_* constant in advisor, by name rather than by list, so a new one
    # is covered the day it is added.
    for name in dir(advisor):
        if not name.startswith("R_"):
            continue
        value = getattr(advisor, name)
        if isinstance(value, int):
            used.add(value)
        elif isinstance(value, (list, tuple)):
            used.update(v for v in value if isinstance(v, int))
    used |= web_addresses()
    return used


class TheSPathIsTheIdentity(unittest.TestCase):
    """The author's pump runs this. Nothing may move."""

    def setUp(self):
        self.profile = Profile.for_model("S735")

    def test_generation_and_verified(self):
        self.assertEqual(self.profile.generation, "S")
        self.assertTrue(self.profile.verified)

    def test_the_table_is_empty(self):
        self.assertEqual(self.profile.canonical_to_physical, {})

    def test_every_address_the_app_uses_comes_back_unchanged(self):
        for address in sorted(app_addresses()):
            self.assertEqual(self.profile.physical(address), address)
            self.assertEqual(self.profile.canonical(address), address)
            self.assertTrue(self.profile.available(address))

    def test_an_address_nobody_has_heard_of_comes_back_unchanged_too(self):
        for address in (1, 40001, 47007, 48132, 99999):
            self.assertEqual(self.profile.physical(address), address)


class TheFTableIsConsistentWithItself(unittest.TestCase):
    def test_the_inverse_is_a_bijection(self):
        profile = Profile.for_model("F750")
        self.assertEqual(len(profile.physical_to_canonical), len(F_TABLE))
        for canonical, physical in F_TABLE.items():
            self.assertEqual(profile.physical(canonical), physical)
            self.assertEqual(profile.canonical(physical), canonical)

    def test_two_canonical_addresses_may_not_share_one_physical(self):
        # Asserted as a constructor rule as well as a property of the table, so
        # that an edit that breaks it fails loudly at startup rather than
        # producing a backup where one register is named after another.
        with self.assertRaises(ValueError):
            Profile("F", "X", {40027: 47007, 40028: 47007})

    def test_an_unmapped_address_falls_back_to_itself(self):
        profile = Profile.for_model("F750")
        # 48132 exists only on the F generation. It has no canonical number,
        # and working by its own is exactly what should happen.
        self.assertEqual(profile.physical(48132), 48132)
        self.assertTrue(profile.available(48132))

    def test_the_addresses_with_no_equivalent_are_refused_outright(self):
        profile = Profile.for_model("F750")
        for address in NO_F_EQUIVALENT:
            self.assertFalse(profile.available(address),
                             "%d has no F meaning and must not fall back to "
                             "the identity" % address)
            self.assertTrue(profile.why_unavailable(address),
                            "%d is excused without a reason" % address)

    def test_nothing_is_both_mapped_and_excused(self):
        self.assertEqual(set(), set(F_TABLE) & set(NO_F_EQUIVALENT))


@unittest.skipIf(NibeModel is None, "the nibe package is not installed")
class TheFTableAgreesWithTheMaps(unittest.TestCase):
    """Row by row, against every F map the package ships.

    The comparison is against the S735 entry for the canonical address, which
    is the map the S-series literals in this app were written against.
    """

    @classmethod
    def setUpClass(cls):
        cls.s735 = coils("S735")

    def _check(self, model: str):
        f = coils(model)
        s = self.s735
        seen = 0
        for canonical, physical in sorted(F_TABLE.items()):
            entry = f.get(physical)
            if entry is None:
                # Absent from this model's map. Not a failure: registry.get()
                # returns None and the app drops the register, which is how an
                # F1155 ends up with no exhaust fan rows.
                continue
            seen += 1
            mine = s.get(canonical)
            if mine is None:
                continue
            # Units. Two *different* units is the failure worth catching: it
            # means the register does not hold what the canonical one holds,
            # and a degree becomes a tenth of a degree with nothing to show
            # for it. One side leaving the unit blank is not that -- it is a
            # gap in a published map, and the maps have them: the SMO 20's
            # 43005 carries no unit where the S735's 40012 says "DM", same
            # register, same factor, same title.
            a_unit = (mine.get("unit") or "").strip()
            b_unit = (entry.get("unit") or "").strip()
            if a_unit and b_unit:
                self.assertEqual(
                    a_unit, b_unit,
                    "%s: %d -> %d, unit %r vs %r (%r vs %r)"
                    % (model, canonical, physical, a_unit, b_unit,
                       mine.get("title"), entry.get("title")))
            self.assertEqual(
                int(mine.get("factor") or 1), int(entry.get("factor") or 1),
                "%s: %d -> %d, division factor differs (%r vs %r)"
                % (model, canonical, physical, mine.get("title"), entry.get("title")))
            self.assertEqual(
                bool(mine.get("write")), bool(entry.get("write")),
                "%s: %d -> %d, one is writable and the other is not (%r vs %r)"
                % (model, canonical, physical, mine.get("title"), entry.get("title")))
            # Enumerations. Three cases, and only one of them is a failure.
            #
            # An enum on one side and a plain number on the other is fine and
            # happens: the F map gives mappings to several registers the S map
            # leaves bare (the ventilation mode, the room sensor switch, night
            # cooling), which changes how a value reads back and not what it
            # means.
            #
            # Two enums may use different *words* -- 40057 is Small/Medium/
            # Large on an S735 and Economy/Normal/Luxury on an F750, and the
            # four hot water stop temperatures confirm that those are the same
            # three modes in the same order.
            #
            # What may not differ is the *keys*, because the key is what a
            # write carries. A key the app would offer and the pump would
            # reject is a broken setting. For a read-only register a superset
            # is harmless -- 31029 Prio gains "Pool 2" and "Transfer" on the F
            # maps -- so only the writable ones have to match exactly.
            a, b = mine.get("mappings"), entry.get("mappings")
            if a and b:
                if mine.get("write"):
                    self.assertEqual(
                        set(a), set(b),
                        "%s: %d -> %d is writable and the two enumerations do "
                        "not use the same keys (%r vs %r)"
                        % (model, canonical, physical, a, b))
                else:
                    self.assertEqual(
                        set(), set(a) - set(b),
                        "%s: %d -> %d, the F map is missing values the S map "
                        "publishes (%r vs %r)"
                        % (model, canonical, physical, a, b))
        self.assertGreater(seen, 30, "%s matched almost nothing" % model)

    def test_f750(self):
        self._check("F750")

    def test_f1155(self):
        self._check("F1155")

    def test_every_other_f_map(self):
        for model in sorted(F_MODELS):
            with self.subTest(model=model):
                self._check(model)

    def test_the_f750_map_has_every_row(self):
        """F750 is the reference: the table was read out of it.

        A row missing here is a typo, not a model difference.
        """
        f = coils("F750")
        missing = sorted(p for p in F_TABLE.values() if p not in f)
        self.assertEqual([], missing)


@unittest.skipIf(NibeModel is None, "the nibe package is not installed")
class TheGenerationListsPartitionTheModels(unittest.TestCase):
    """S_MODELS and F_MODELS are lists, so something has to check them.

    The decisive fact is one register: an F map has 47007 `Heat Curve S1` and
    no 40027; an S map has 40027 and no 47007. If the `nibe` package adds a
    model, this fails rather than letting Profile.for_model fall through to
    whichever branch happens to be written last.
    """

    def test_no_model_is_in_both_lists(self):
        self.assertEqual(set(), S_MODELS & F_MODELS)

    def test_every_model_is_in_exactly_one_list(self):
        for model in NibeModel:
            if model.name == "CUSTOM":
                continue
            with self.subTest(model=model.name):
                self.assertIn(model.name, S_MODELS | F_MODELS)

    def test_the_lists_match_what_the_maps_say(self):
        for model in NibeModel:
            if model.name == "CUSTOM":
                continue
            data = coils(model.name)
            with self.subTest(model=model.name):
                if model.name in F_MODELS:
                    self.assertIn(F_MARKER, data)
                    self.assertNotIn(S_MARKER, data)
                else:
                    self.assertIn(S_MARKER, data)
                    self.assertNotIn(F_MARKER, data)

    def test_the_ambiguous_names_landed_where_the_maps_put_them(self):
        """The four that the name does not decide, said out loud.

        SMO 20 and SMO 40 have built-in Modbus TCP, which is what makes them
        look like S-series models in a list sorted by how you reach them --
        and they number their registers the F way. SMO S40 is the opposite: an
        S in the middle of the name, and S numbering. The VVMs split on the
        same letter.
        """
        self.assertIn("SMO20", F_MODELS)
        self.assertIn("SMO40", F_MODELS)
        self.assertIn("SMOS40", S_MODELS)
        self.assertIn("VVM500", F_MODELS)
        self.assertIn("VVMS500", S_MODELS)


class ChoosingAProfile(unittest.TestCase):
    def test_a_model_name_decides(self):
        self.assertEqual(Profile.for_model("F750").generation, "F")
        self.assertEqual(Profile.for_model("s735").generation, "S")
        self.assertEqual(Profile.for_model("SMO 20").generation, "F")
        self.assertEqual(Profile.for_model("smo-s40").generation, "S")

    def test_an_explicit_generation_wins(self):
        # The CSV case: a map exported from the pump has no model name in it.
        self.assertEqual(Profile.for_model("", "F").generation, "F")
        self.assertEqual(Profile.for_model("", "f").generation, "F")
        self.assertEqual(Profile.for_model("S735", "F").generation, "F")

    def test_neither_is_an_error_that_says_what_to_do(self):
        with self.assertRaises(ValueError) as caught:
            Profile.for_model("")
        message = str(caught.exception)
        self.assertIn("generation", message)
        self.assertIn(str(S_MARKER), message)
        self.assertIn(str(F_MARKER), message)

    def test_an_unknown_model_is_an_error_rather_than_a_guess(self):
        with self.assertRaises(ValueError):
            Profile.for_model("F2040")

    def test_a_nonsense_generation_is_an_error(self):
        with self.assertRaises(ValueError):
            Profile.for_model("S735", "X")


class DerivingTheGenerationFromTheMapItself(unittest.TestCase):
    """A CSV exported from a pump carries no model name, but it carries 40027.

    `register_csv` on its own is a complete configuration and
    config.example.yaml has said so since before the generations split -- the
    F-series work turned it into a startup error. The map is the thing that
    knows: an S map has 40027 and no 47007, an F map has 47007 and no 40027,
    and that partitions all 32 models the `nibe` package ships (see
    TheGenerationListsPartitionTheModels above).
    """

    def test_the_s_marker_alone_means_s(self):
        self.assertEqual("S", generation_from_addresses([30002, S_MARKER, 40031]))

    def test_the_f_marker_alone_means_f(self):
        self.assertEqual("F", generation_from_addresses([40004, F_MARKER, 47011]))

    def test_both_is_refused_and_names_both(self):
        with self.assertRaises(ValueError) as caught:
            generation_from_addresses([S_MARKER, F_MARKER])
        message = str(caught.exception)
        self.assertIn(str(S_MARKER), message)
        self.assertIn(str(F_MARKER), message)
        self.assertIn("generation", message)

    def test_neither_is_refused_and_names_both(self):
        with self.assertRaises(ValueError) as caught:
            generation_from_addresses([30002, 40031], "some.csv")
        message = str(caught.exception)
        self.assertIn(str(S_MARKER), message)
        self.assertIn(str(F_MARKER), message)
        self.assertIn("some.csv", message)

    def test_it_agrees_with_the_model_lists_on_every_map(self):
        if NibeModel is None:
            self.skipTest("the nibe package is not installed")
        for model in NibeModel:
            name = model.name.upper()
            if name not in F_MODELS and name not in S_MODELS:
                continue
            expected = "F" if name in F_MODELS else "S"
            addresses = [int(a) for a in model.get_coil_data()]
            self.assertEqual(expected, generation_from_addresses(addresses), name)


class EverythingTheAppUsesIsAccountedFor(unittest.TestCase):
    """The one that fails the build when a new S-only literal appears.

    A register named anywhere in this app either has an F equivalent or is
    explicitly recorded as having none, with a reason somebody wrote down. The
    third option -- nobody looked -- is what this test removes.
    """

    def test_every_address_is_mapped_or_excused(self):
        unaccounted = sorted(a for a in app_addresses()
                             if a not in F_TABLE and a not in NO_F_EQUIVALENT)
        self.assertEqual(
            [], unaccounted,
            "these register numbers are used by the app but nobody has said "
            "what they mean on an F-series pump. Add them to F_TABLE, or to "
            "NO_F_EQUIVALENT with a reason: %s" % unaccounted)

    def test_the_collector_actually_found_the_app(self):
        # A regex that stops matching would make the test above pass by
        # finding nothing, which is the failure mode of every test like it.
        used = app_addresses()
        self.assertGreater(len(used), 60)
        self.assertIn(40027, used)          # the heating curve
        self.assertIn(30002, used)          # the outdoor sensor
        self.assertIn(31976, used)          # the alarm number
        self.assertGreater(len(web_addresses()), 50)

    def test_nothing_is_excused_that_is_not_used(self):
        """NO_F_EQUIVALENT is a record of decisions, not a scratchpad."""
        stale = sorted(set(NO_F_EQUIVALENT) - app_addresses())
        self.assertEqual([], stale)

    @unittest.skipIf(NibeModel is None, "the nibe package is not installed")
    def test_the_dangerous_excuses_really_are_other_registers(self):
        """The three that made `available()` necessary.

        40020, 40079 and 40167 are real registers on an F750 holding something
        else. If a future map stops saying so, the reasons written next to them
        are wrong and should be rewritten rather than left standing.
        """
        f = coils("F750")
        for address in (40020, 40079, 40167):
            self.assertIn(address, f)
            self.assertIn(address, NO_F_EQUIVALENT)


@unittest.skipIf(NibeModel is None, "the nibe package is not installed")
class WhatTheTranslationBuysOnAnF750(unittest.TestCase):
    """The measurement that started this, as a test.

    Before the profile existed, settings.py found 13 of its 36 rows on an F750
    and pump.DASHBOARD 2 of its 25 -- and the heating advice regulated on
    registers the pump does not have. These numbers are the reason the file
    exists, so a change that quietly undoes it should fail here.
    """

    def test_most_of_the_dashboard_now_resolves(self):
        f = coils("F750")
        profile = Profile.for_model("F750")
        found = [a for a in pump_module.DASHBOARD
                 if profile.available(a) and profile.physical(a) in f]
        self.assertGreaterEqual(len(found), 19,
                                "was 2 of 25 before the profile existed")

    def test_most_of_the_settings_list_now_resolves(self):
        f = coils("F750")
        profile = Profile.for_model("F750")
        found = [a for a in settings.ADDRESSES
                 if profile.available(a) and profile.physical(a) in f]
        self.assertGreaterEqual(len(found), 34,
                                "was 13 of 36 before the profile existed")

    def test_the_advisor_reaches_every_register_it_regulates_on(self):
        f = coils("F750")
        profile = Profile.for_model("F750")
        for address in advisor.READ:
            with self.subTest(address=address):
                if not profile.available(address):
                    # Only 40167 is in advisor's reach and excused, and it is
                    # read for the diagnosis rather than written.
                    self.assertIn(address, NO_F_EQUIVALENT)
                    continue
                self.assertIn(profile.physical(address), f,
                              "the advisor reads %d, which resolves to %d and is "
                              "not on an F750" % (address, profile.physical(address)))


class TheSettingsRowForTheSeventhCurvePoint(unittest.TestCase):
    """A label that names a temperature is a claim.

    "+30 °C ute" is NIBE's own menu 1.30.7 on the S series. On the F series it
    is inference from register order and identical defaults, against two user
    manuals that show menu 1.9.7 with six rows -- so on an F pump the row says
    what it knows and the explanation says what it does not. See
    docs/f-series.md, "The seventh own-curve point".
    """

    class FakePump:
        def __init__(self, profile):
            self.profile = profile
            self._regs = {}

        def register(self, address):
            from nibelokal.registry import Register
            physical = self.profile.physical(address)
            return Register(address=physical, title="P7", size="s8", unit="°C",
                            writable=True, min=5, max=80)

    def _row(self, generation):
        pump = self.FakePump(Profile.for_generation(generation))
        values = {a: {"value": 20} for a in settings.ADDRESSES}
        groups = settings.build(pump, values)
        rows = [r for g in groups for r in g["rows"] if r["address"] == 40040]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_the_s_series_row_still_names_plus_thirty(self):
        row = self._row("S")
        self.assertIn("+30", row["label"].replace("−", "-"))

    def test_the_f_series_row_names_no_temperature(self):
        row = self._row("F")
        self.assertNotIn("30", row["label"])
        self.assertEqual(row["label"], "Egen kurva P7")

    def test_and_says_why_in_the_row_itself(self):
        why = self._row("F")["why"]
        self.assertIn("sex punkter", why)
        self.assertIn("1.9.7", why)
        self.assertIn("inte belagt", why)

    def test_every_other_row_is_word_for_word_the_same(self):
        # The override is one row. A generation-dependent wording that quietly
        # spread would be worse than the claim it was fixing.
        pump_s = self.FakePump(Profile.for_generation("S"))
        pump_f = self.FakePump(Profile.for_generation("F"))
        values = {a: {"value": 20} for a in settings.ADDRESSES}
        rows_s = {r["address"]: (r["label"], r["why"])
                  for g in settings.build(pump_s, values) for r in g["rows"]}
        rows_f = {r["address"]: (r["label"], r["why"])
                  for g in settings.build(pump_f, values) for r in g["rows"]}
        differ = [a for a in rows_s if rows_s[a] != rows_f.get(a)]
        self.assertEqual(differ, [40040])


if __name__ == "__main__":
    unittest.main()
