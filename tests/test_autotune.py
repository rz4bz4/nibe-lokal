"""Tests for the long-horizon curve tuning, against synthetic histories.

The autotuner writes nothing, but what it proposes ends up as a real change to a
real house that answers two days later. So the cases worth testing are the ones
where a number would look plausible and be worthless: a slope fitted across
three degrees of weather, an offset proposed for a house that is only cold when
it is cold outside, and a July history where every reading is the sun's doing.

Each history is built one night at a time, because that is the unit the module
reasons in: one point per night, no daytime samples.
"""
import os
import random
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nibelokal import autotune                                 # noqa: E402
from nibelokal.advisor import (EMITTERS, R_OFFSET,             # noqa: E402
                               R_OWN_CURVE, emitters)

TARGET = 21.0


def settings(offset=0, curve=0, points=(45, 45, 45, 37, 33, 23, 15),
             min_supply=26.0, max_supply=58.0):
    """The heating settings as advisor.diagnose() reports them for this pump."""
    return {
        "offset": offset,
        "curve": curve,
        "own_curve": list(points),
        "min_supply": min_supply,
        "max_supply": max_supply,
    }


def _ts(day, hour, minute=0):
    # Local time on purpose: the night window is a wall-clock window, and
    # mktime normalises a day number past the end of the month.
    return time.mktime((2026, 1, day, hour, minute, 0, 0, 0, -1))


def nights(outdoors, error, noise=0.0, seed=7, dm=-60.0, compressor="Heating",
           indoor=True, first_day=10, samples_per_night=16, step_min=30):
    """A history of full nights: 22:00 to 06:00, one outdoor temperature each.

    `error` is indoor minus target as a function of outdoor temperature -- the
    thing the module is supposed to recover.
    """
    rnd = random.Random(seed)
    rows = []
    for i, out in enumerate(outdoors):
        # Noise per night, not per sample: within one night the house really is
        # that temperature, and averaging 16 samples would hide the scatter the
        # statistics are supposed to notice.
        bias = rnd.gauss(0.0, noise) if noise else 0.0
        base = _ts(first_day + i, 22)
        for k in range(samples_per_night):
            ts = base + k * step_min * 60
            rows.append({
                "ts": ts,
                "outdoor": out,
                "indoor": (TARGET + error(out) + bias) if indoor else None,
                "supply": 35.0,
                "degree_minutes": dm,
                "compressor": compressor,
            })
    return rows


def days(outdoors, error, **kw):
    """Round-the-clock samples, for the filtering tests."""
    rows = []
    for i, out in enumerate(outdoors):
        for hour in range(24):
            rows.append({
                "ts": _ts(10 + i, hour),
                "outdoor": out,
                "indoor": TARGET + error(out),
                "supply": 35.0,
                "degree_minutes": kw.get("dm", -60.0),
                "compressor": kw.get("compressor", "Heating"),
            })
    return rows


WIDE = [-8, -6, -4, -2, 0, 2, 4, 6, 8, 10]          # span 18 C, ten nights
COLD_TO_MILD = [-12, -9, -6, -3, 0, 3, 6, 9, 12]    # span 24 C, nine nights


class TooColdEverywhere(unittest.TestCase):
    """Uniformly 1.2 C too cold: a level error, which is what offset is for."""

    def setUp(self):
        self.r = autotune.analyse(nights(WIDE, lambda t: -1.2, noise=0.1),
                                  TARGET, settings(offset=0))

    def test_it_is_confident_and_says_why(self):
        self.assertEqual(self.r["state"], "confident")
        self.assertTrue(self.r["ok"])
        self.assertEqual(self.r["nights"], len(WIDE))
        self.assertIn("för kallt", self.r["reason_sv"])

    def test_the_level_error_is_recovered(self):
        self.assertAlmostEqual(self.r["offset_error_c"], -1.2, delta=0.25)

    def test_no_slope_is_claimed_where_there_is_none(self):
        # The slope is reported (the weather varied enough to determine it) but
        # must come out near zero rather than fitting the noise.
        self.assertIsNotNone(self.r["slope_error_c_per_c"])
        self.assertLess(abs(self.r["slope_error_c_per_c"]), autotune.SLOPE_MIN_C_PER_C)

    def test_it_proposes_one_offset_step_up(self):
        p = self.r["proposal"]
        self.assertEqual(p["register"], R_OFFSET)
        self.assertEqual((p["from"], p["to"]), (0, 1))
        self.assertGreaterEqual(p["wait_hours"], 24)

    def test_a_big_error_still_only_buys_one_step(self):
        r = autotune.analyse(nights(WIDE, lambda t: -3.0, noise=0.1),
                             TARGET, settings(offset=0))
        self.assertEqual(r["proposal"]["to"], 1)
        # ...and says out loud that more will be needed, instead of hiding it.
        self.assertIn("steg", r["proposal"]["why_sv"])

    def test_too_warm_goes_the_other_way(self):
        r = autotune.analyse(nights(WIDE, lambda t: 1.3, noise=0.1),
                             TARGET, settings(offset=2))
        self.assertEqual((r["proposal"]["from"], r["proposal"]["to"]), (2, 1))

    def test_offset_at_its_limit_is_refused_with_a_reason(self):
        r = autotune.analyse(nights(WIDE, lambda t: -1.2, noise=0.1),
                             TARGET, settings(offset=10))
        self.assertIsNone(r["proposal"])
        self.assertTrue(any("gränsen" in n for n in r["notes_sv"]))

    def test_without_pump_settings_there_is_a_finding_but_no_proposal(self):
        r = autotune.analyse(nights(WIDE, lambda t: -1.2, noise=0.1), TARGET)
        self.assertIsNone(r["proposal"])
        self.assertLess(r["offset_error_c"], -0.9)
        self.assertTrue(any("40031" in n for n in r["notes_sv"]))


class WrongOnlyWhenCold(unittest.TestCase):
    """Right in mild weather, 1 C too cold at -12: a slope error.

    This is the case an offset makes worse rather than better, so the module
    must name the curve and must not touch 40031.
    """

    def setUp(self):
        self.r = autotune.analyse(
            nights(COLD_TO_MILD, lambda t: 0.05 * t - 0.4, noise=0.1),
            TARGET, settings(offset=0))

    def test_the_slope_is_reported_and_roughly_right(self):
        self.assertEqual(self.r["state"], "confident")
        self.assertAlmostEqual(self.r["slope_error_c_per_c"], 0.05, delta=0.02)

    def test_it_does_not_propose_an_offset(self):
        self.assertIsNotNone(self.r["proposal"])
        self.assertNotEqual(self.r["proposal"]["register"], R_OFFSET)
        self.assertIn("lutning", self.r["reason_sv"])
        self.assertIn("Offset", self.r["reason_sv"])

    def test_it_moves_a_curve_point_the_weather_actually_reaches(self):
        p = self.r["proposal"]
        self.assertIn(p["register"], R_OWN_CURVE)
        # -30 and -20 C are P1 and P2; this house was never colder than -12.
        i = R_OWN_CURVE.index(p["register"])
        self.assertIn(autotune.OWN_CURVE_OUTDOOR[i], (-20, -10, 0))
        self.assertGreater(p["to"], p["from"], "too cold means more supply, not less")

    def test_the_point_move_is_small_and_bounded(self):
        p = self.r["proposal"]
        self.assertLessEqual(abs(p["to"] - p["from"]), autotune.POINT_STEP_MAX_C)

    def test_a_point_against_max_supply_is_refused_and_names_the_register(self):
        r = autotune.analyse(
            nights(COLD_TO_MILD, lambda t: 0.05 * t - 0.4, noise=0.1),
            TARGET, settings(points=(58, 58, 58, 58, 58, 58, 58), max_supply=58.0))
        self.assertIsNone(r["proposal"])
        self.assertTrue(any("40039" in n for n in r["notes_sv"]), r["notes_sv"])

    def test_too_warm_when_cold_lowers_the_point(self):
        r = autotune.analyse(
            nights(COLD_TO_MILD, lambda t: -0.05 * t + 0.4, noise=0.1),
            TARGET, settings())
        self.assertLess(r["proposal"]["to"], r["proposal"]["from"])

    def test_a_numbered_curve_gets_a_curve_step_instead(self):
        r = autotune.analyse(
            nights(COLD_TO_MILD, lambda t: 0.05 * t - 0.4, noise=0.1),
            TARGET, settings(curve=5))
        self.assertEqual(r["proposal"]["register"], 40027)
        self.assertEqual((r["proposal"]["from"], r["proposal"]["to"]), (5, 6))


class NotEnoughEvidence(unittest.TestCase):
    def test_a_narrow_outdoor_range_refuses_even_a_clear_error(self):
        # Eight nights, all between 2 and 5 C, house plainly 1.2 C too cold. A
        # level error and a slope error look identical here, and guessing wrong
        # makes the untested weather worse.
        r = autotune.analyse(nights([2, 3, 4, 5, 2, 3, 4, 5], lambda t: -1.2),
                             TARGET, settings())
        self.assertEqual(r["state"], "insufficient")
        self.assertFalse(r["ok"])
        self.assertIsNone(r["proposal"])
        self.assertIsNone(r["slope_error_c_per_c"])
        self.assertTrue(any("varierat" in m for m in r["missing_sv"]))

    def test_two_nights_is_not_enough_and_it_says_when_it_will_be(self):
        r = autotune.analyse(nights([-5, 5], lambda t: -1.2), TARGET, settings(),
                             now=_ts(12, 12))
        self.assertEqual(r["state"], "insufficient")
        self.assertTrue(any("nätter" in m for m in r["missing_sv"]))
        self.assertTrue(any("2026-01-" in m for m in r["missing_sv"]),
                        "should say roughly when the answer will exist")

    def test_the_slope_is_never_reported_from_too_little_weather(self):
        r = autotune.analyse(nights([0, 1, 2, 3, 4, 5, 0, 1], lambda t: -0.1 * t),
                             TARGET, settings())
        self.assertIsNone(r["slope_error_c_per_c"])
        self.assertTrue(any("lutning" in m for m in r["missing_sv"]))

    def test_a_house_with_no_indoor_source_degrades_instead_of_failing(self):
        r = autotune.analyse(nights(WIDE, lambda t: -1.2, indoor=False),
                             TARGET, settings())
        self.assertEqual(r["state"], "insufficient")
        self.assertEqual(r["samples"], 0)
        self.assertTrue(any("Inomhustemperatur" in m for m in r["missing_sv"]))

    def test_no_target_temperature_means_no_error_to_measure(self):
        r = autotune.analyse(nights(WIDE, lambda t: -1.2), None, settings())
        self.assertEqual(r["state"], "insufficient")
        self.assertIsNone(r["offset_error_c"])

    def test_a_noisy_house_is_uncertain_rather_than_confident(self):
        # Two degrees of night-to-night scatter that the outdoor temperature
        # does not explain: an open window, a stove, or a bad indoor feed.
        r = autotune.analyse(nights(WIDE, lambda t: -1.2, noise=2.0, seed=3),
                             TARGET, settings())
        self.assertEqual(r["state"], "uncertain")
        self.assertIsNone(r["proposal"])
        self.assertLess(r["confidence"], 0.5)


class Filtering(unittest.TestCase):
    def test_summer_is_thrown_away_entirely(self):
        r = autotune.analyse(days([22, 24, 26, 28, 25, 23], lambda t: 2.0),
                             TARGET, settings())
        self.assertEqual(r["samples"], 0)
        self.assertEqual(r["nights"], 0)
        self.assertEqual(r["state"], "insufficient")
        self.assertIsNone(r["proposal"])
        self.assertEqual(r["rejected"].get("no_heat_demand_outdoor"), 6 * 24)

    def test_daylight_hours_are_not_evidence(self):
        rows = [row for row in days(WIDE, lambda t: -1.2)
                if 8 <= time.localtime(row["ts"]).tm_hour < 18]
        r = autotune.analyse(rows, TARGET, settings())
        self.assertEqual(r["samples"], 0)
        self.assertTrue(r["rejected"].get("daytime"))

    def test_hot_water_and_no_demand_are_dropped(self):
        hot = autotune.analyse(nights(WIDE, lambda t: -1.2, compressor="Hot Water"),
                               TARGET, settings())
        self.assertEqual(hot["samples"], 0)
        self.assertTrue(hot["rejected"].get("compressor_not_heating"))

        idle = autotune.analyse(nights(WIDE, lambda t: -1.2, dm=0.0),
                                TARGET, settings())
        self.assertEqual(idle["samples"], 0)
        self.assertTrue(idle["rejected"].get("no_demand_degree_minutes"))

    def test_broken_sensor_values_do_not_reach_the_fit(self):
        rows = nights(WIDE, lambda t: -1.2, noise=0.1)
        rows.append(dict(rows[0], indoor=-40.0))
        r = autotune.analyse(rows, TARGET, settings())
        self.assertEqual(r["rejected"].get("implausible"), 1)
        self.assertAlmostEqual(r["offset_error_c"], -1.2, delta=0.25)

    def test_a_night_with_two_stray_readings_is_not_a_night(self):
        rows = nights(WIDE, lambda t: -1.2, noise=0.1)
        rows += nights([-30], lambda t: 5.0, first_day=40, samples_per_night=2)
        r = autotune.analyse(rows, TARGET, settings())
        self.assertEqual(r["nights"], len(WIDE))


class AlreadyRight(unittest.TestCase):
    def test_a_correct_curve_gets_no_proposal_and_says_so(self):
        r = autotune.analyse(nights(WIDE, lambda t: 0.0, noise=0.1),
                             TARGET, settings())
        self.assertEqual(r["state"], "confident")
        self.assertTrue(r["ok"])
        self.assertIsNone(r["proposal"])
        self.assertIn("behöver inget", r["reason_sv"])

    def test_an_error_inside_the_deadband_is_left_alone(self):
        # Half a degree is less than one offset step is worth; correcting it
        # would overshoot.
        r = autotune.analyse(nights(WIDE, lambda t: -0.3, noise=0.05),
                             TARGET, settings())
        self.assertIsNone(r["proposal"])


class OneStepOnly(unittest.TestCase):
    """The rule the whole module hangs on: never more than one step at a time."""

    CASES = {
        "too cold": (WIDE, lambda t: -1.2),
        "much too cold": (WIDE, lambda t: -4.0),
        "too warm": (WIDE, lambda t: 2.5),
        "cold only": (COLD_TO_MILD, lambda t: 0.05 * t - 0.4),
        "warm only when cold": (COLD_TO_MILD, lambda t: -0.08 * t + 0.6),
        "right": (WIDE, lambda t: 0.0),
        "narrow": ([2, 3, 4, 5], lambda t: -1.2),
    }

    def test_at_most_one_register_and_one_step(self):
        for name, (outdoors, error) in self.CASES.items():
            for offset in (-3, 0, 3):
                r = autotune.analyse(nights(outdoors, error, noise=0.1),
                                     TARGET, settings(offset=offset))
                p = r["proposal"]
                if p is None:
                    continue
                self.assertIn("register", p, name)
                if p["register"] == R_OFFSET:
                    self.assertEqual(abs(p["to"] - p["from"]), 1,
                                     "%s proposed more than one offset step" % name)
                elif p["register"] == 40027:
                    self.assertEqual(abs(p["to"] - p["from"]), 1, name)
                else:
                    self.assertIn(p["register"], R_OWN_CURVE, name)
                    self.assertLessEqual(abs(p["to"] - p["from"]),
                                         autotune.POINT_STEP_MAX_C, name)

    def test_a_proposal_always_carries_its_reasoning_and_a_wait(self):
        r = autotune.analyse(nights(WIDE, lambda t: -1.2, noise=0.1),
                             TARGET, settings())
        p = r["proposal"]
        for key in ("register", "from", "to", "why_sv", "expected_sv", "wait_hours"):
            self.assertIn(key, p)
        self.assertTrue(p["why_sv"] and p["expected_sv"])
        self.assertIn("fem minuter", p["expected_sv"],
                      "the calculated supply ramp is the thing people misread")

    def test_floor_heating_waits_longer_than_radiators(self):
        h = nights(WIDE, lambda t: -1.2, noise=0.1)
        floor = autotune.analyse(h, TARGET, settings(), emitter_kind="floor")
        rads = autotune.analyse(h, TARGET, settings(), emitter_kind="radiators")
        self.assertGreater(floor["proposal"]["wait_hours"],
                           rads["proposal"]["wait_hours"])


class NeverRaises(unittest.TestCase):
    def test_garbage_histories_produce_a_result_not_a_traceback(self):
        for junk in (None, [], "nonsense", 17, [None, "x", (1, 2)], [{}],
                     [{"ts": "yesterday"}], object()):
            r = autotune.analyse(junk, TARGET, settings())
            self.assertIn(r["state"], ("insufficient", "uncertain", "confident"))
            self.assertIsNone(r["proposal"])
            self.assertTrue(r["reason_sv"])

    def test_broken_settings_do_not_break_the_analysis(self):
        h = nights(WIDE, lambda t: -1.2, noise=0.1)
        for bad in ({}, {"offset": None}, {"offset": "hej"},
                    {"curve": 0, "own_curve": "not a list"},
                    {"curve": 0, "own_curve": [None] * 7}):
            r = autotune.analyse(h, TARGET, bad)
            self.assertIsNotNone(r["offset_error_c"])

    def test_the_result_shape_is_stable(self):
        r = autotune.analyse(nights(WIDE, lambda t: -1.2, noise=0.1),
                             TARGET, settings())
        for key in ("ok", "state", "reason_sv", "samples", "outdoor_span", "days",
                    "offset_error_c", "slope_error_c_per_c", "proposal",
                    "confidence", "missing_sv"):
            self.assertIn(key, r)
        self.assertEqual(len(r["outdoor_span"]), 2)
        self.assertTrue(0.0 <= r["confidence"] <= 1.0)


class Statistics(unittest.TestCase):
    def test_two_points_never_look_perfectly_determined(self):
        # A line through two points has zero residuals, which would otherwise
        # read as certainty.
        f = autotune.fit([(0.0, 1.0), (10.0, 2.0)])
        self.assertIsNone(f.slope)
        self.assertFalse(f.slope_is_real())

    def test_identical_outdoor_temperatures_give_no_slope(self):
        f = autotune.fit([(5.0, 1.0)] * 4)
        self.assertIsNone(f.slope)
        self.assertIsNotNone(f.level)

    def test_the_same_slope_is_significant_on_a_wide_range_and_not_a_narrow_one(self):
        wide = autotune.fit([(t, 0.05 * t) for t in range(-12, 13, 3)])
        narrow = autotune.fit([(t, 0.05 * t) for t in (0, 1, 2, 3, 4, 5, 6, 7)])
        self.assertTrue(wide.slope_is_real())
        # The narrow fit recovers the same slope exactly -- noise-free data --
        # but the module still refuses to report it, because the gate is the
        # outdoor span, not the fit's own opinion of itself.
        r = autotune.analyse(nights([0, 1, 2, 3, 4, 5, 6, 7], lambda t: 0.05 * t),
                             TARGET, settings())
        self.assertIsNone(r["slope_error_c_per_c"])
        self.assertGreater(narrow.span, 0)

    def test_confidence_grows_with_evidence(self):
        few = autotune.analyse(nights([-6, 0, 6], lambda t: -1.2, noise=0.1),
                               TARGET, settings())
        many = autotune.analyse(nights(WIDE * 2, lambda t: -1.2, noise=0.1),
                                TARGET, settings())
        self.assertLess(few["confidence"], many["confidence"])
        self.assertLessEqual(many["confidence"], autotune.CONFIDENCE_CAP)


class NoRetractedMeasurements(unittest.TestCase):
    """What the user-facing text is allowed to claim.

    docs/registers.md retracted the 1.5 C-per-step figure: the run that
    produced it was stopped while the value was still ramping, which makes it a
    lower bound and not a measurement. It was quoted in the proposal a
    household reads before changing its heating -- and quoted as "just den här
    pumpen", one specific house, in a project strangers run against theirs.
    """

    def _proposal(self):
        result = autotune.analyse(nights(WIDE, lambda out: -1.2), TARGET,
                                  settings(offset=0))
        self.assertIsNotNone(result["proposal"])
        return result["proposal"]

    def test_the_proposal_does_not_quote_the_retracted_number(self):
        text = " ".join(str(v) for v in self._proposal().values())
        self.assertNotIn("1,5", text)
        self.assertNotIn("1.5", text)

    def test_and_does_not_speak_of_one_particular_pump(self):
        text = " ".join(str(v) for v in self._proposal().values())
        self.assertNotIn("just den här pumpen", text)

    def test_it_still_says_what_to_expect(self):
        expected = self._proposal()["expected_sv"]
        self.assertIn("2,5", expected)
        self.assertIn("Framledningen", expected)

    def test_the_constants_are_the_documented_ones(self):
        self.assertEqual(autotune.SUPPLY_C_PER_INDOOR_C, 2.5)
        self.assertEqual(autotune.INDOOR_C_PER_OFFSET_STEP, 1.0)


class TValues(unittest.TestCase):
    """The table stops at 30 degrees of freedom. What happens after it."""

    def test_past_the_table_it_does_not_jump_to_the_asymptote(self):
        # t(31) is 2.04, not 1.96. Falling straight to the dof = infinity value
        # overstated the evidence by 4 % the moment a run passed a month.
        self.assertGreaterEqual(autotune._t95(31), 2.03)
        self.assertGreaterEqual(autotune._t95(35), 2.02)

    def test_it_never_goes_up_as_evidence_grows(self):
        values = [autotune._t95(d) for d in range(1, 400)]
        self.assertEqual(values, sorted(values, reverse=True))

    def test_and_never_below_the_asymptote(self):
        for dof in (31, 60, 121, 1000, 10 ** 6):
            self.assertGreaterEqual(autotune._t95(dof), 1.96)

    def test_no_degrees_of_freedom_is_no_evidence_at_all(self):
        for dof in (0, -1, None, "kalle"):
            self.assertEqual(autotune._t95(dof), float("inf"))

    def test_a_long_run_is_still_allowed_to_be_significant(self):
        # Forty nights of a clear error must not become "uncertain" merely
        # because the t value stopped shrinking.
        outdoors = [-12 + i * 0.6 for i in range(40)]
        result = autotune.analyse(nights(outdoors, lambda out: -1.2), TARGET,
                                  settings())
        self.assertEqual(result["state"], "confident")


class HoursThatAreNotEvidence(unittest.TestCase):
    """An hour that made hot water is not an hour about the heating curve."""

    def _hourly(self, states_for_hour):
        """One night of hourly buckets, as store.autotune_history hands them over."""
        rows = []
        for hour in range(22, 30):
            states = states_for_hour(hour % 24)
            rows.append({
                "ts": _ts(10 if hour < 24 else 11, hour % 24),
                "outdoor": -5.0,
                "indoor": TARGET - 1.2,
                "supply": 35.0,
                "degree_minutes": -60.0,
                "compressor": states[-1],
                "compressor_states": states,
            })
        return rows

    def test_a_bucket_that_only_ended_in_heating_is_still_dropped(self):
        # Hot water 10:00-10:40 and heating 10:40-11:00 keeps the hour if only
        # the last state is looked at, which is what the comment in store.py
        # always said it did not do.
        rows = self._hourly(lambda hour: ["Hot Water", "Heat"] if hour == 23
                            else ["Heat"])
        result = autotune.analyse(rows, TARGET, settings())
        self.assertEqual(result["rejected"].get("compressor_not_heating"), 1)

    def test_a_night_of_heating_only_is_kept(self):
        rows = self._hourly(lambda hour: ["Heat"])
        result = autotune.analyse(rows, TARGET, settings())
        self.assertFalse(result["rejected"].get("compressor_not_heating"))
        self.assertEqual(result["samples"], 8)

    def test_the_english_the_package_map_actually_returns(self):
        for state in ("Hot Water", "Pool", "Cooling"):
            self.assertTrue(autotune._is_not_heating(state), state)
        for state in ("Heat", "Off", "Värme"):
            self.assertFalse(autotune._is_not_heating(state), state)

    def test_a_plain_string_still_works_for_a_source_without_the_list(self):
        self.assertTrue(autotune._is_not_heating("Varmvatten"))
        self.assertFalse(autotune._is_not_heating(None))


class HowLongToWaitComesFromTheTable(unittest.TestCase):
    """It used to come from matching a Swedish sentence.

    `wait_hours = 48 if em["wait"] == "två dygn" else 24` made the advice
    depend on the wording of prose that exists to be read by a person. Reword
    "två dygn" to "ett par dygn" -- an entirely reasonable edit to a Swedish
    string -- and every floor-heating house is silently told to judge its
    change after one day instead of two, which is how a slow system gets
    adjusted twice for one error.
    """

    def test_floor_heating_waits_two_days(self):
        self.assertEqual(emitters("floor")["wait_hours"], 48)

    def test_radiators_wait_one(self):
        self.assertEqual(emitters("radiators")["wait_hours"], 24)

    def test_every_emitter_kind_carries_a_number(self):
        for kind, em in EMITTERS.items():
            self.assertIsInstance(em.get("wait_hours"), int, kind)
            self.assertGreaterEqual(em["wait_hours"], 24, kind)

    def test_the_number_and_the_sentence_still_agree(self):
        # Both are shown to the same person on the same panel.
        for kind, em in EMITTERS.items():
            expected = 48 if "två" in em["wait"] else 24
            self.assertEqual(em["wait_hours"], expected, kind)

    def test_rewording_the_sentence_no_longer_changes_the_advice(self):
        original = EMITTERS["floor"]["wait"]
        EMITTERS["floor"]["wait"] = "ett par dygn"
        try:
            r = autotune.analyse(nights(WIDE, lambda t: -1.2, noise=0.1),
                                 TARGET, settings(offset=0), emitter_kind="floor")
            self.assertEqual(r["proposal"]["wait_hours"], 48)
        finally:
            EMITTERS["floor"]["wait"] = original


class WhatTheSlopeNumberMeans(unittest.TestCase):
    """The docstring said "identifiable and real"; the code says identifiable.

    The code was right and the comment was wrong, which is the worse way round:
    a reader trusting the comment reads a reported 0,02 °C/°C as "the fit
    cleared its own standard error" when it means "the weather varied enough
    to measure, and the answer is about zero". Those are opposite conclusions
    about whether to touch the curve. The comment now says what the code does,
    and this pins the behaviour so the next reader can trust it.
    """

    def test_a_measured_near_zero_slope_is_reported_as_a_number(self):
        r = autotune.analyse(nights(WIDE, lambda t: -1.2, noise=0.1),
                             TARGET, settings(offset=0))
        self.assertIsNotNone(r["slope_error_c_per_c"],
                             "a measured slope came back as 'cannot say'")
        self.assertLess(abs(r["slope_error_c_per_c"]), autotune.SLOPE_MIN_C_PER_C)

    def test_and_it_is_not_acted_on(self):
        # Reported, but the proposal is the offset one: a slope inside the
        # deadband is not evidence about the curve.
        r = autotune.analyse(nights(WIDE, lambda t: -1.2, noise=0.1),
                             TARGET, settings(offset=0))
        self.assertEqual(r["proposal"]["register"], R_OFFSET)

    def test_weather_that_never_varied_says_it_cannot_say(self):
        narrow = [t for t in WIDE if -6 <= t <= -4] or [-5.0] * len(WIDE)
        r = autotune.analyse(nights(narrow, lambda t: -1.2, noise=0.1),
                             TARGET, settings(offset=0))
        self.assertIsNone(r["slope_error_c_per_c"])


class TheNumbersInTheProseAreSwedish(unittest.TestCase):
    """The last four %g / %+d in this module's user-facing strings."""

    def test_a_curve_at_its_limit(self):
        r = autotune.analyse(nights(WIDE, lambda t: -1.2 - 0.25 * t, noise=0.05),
                             TARGET, settings(offset=0, curve=15))
        text = " ".join(r["notes_sv"]) + str(r.get("reason_sv") or "")
        self.assertNotIn("15.0", text)

    def test_an_offset_at_its_limit(self):
        r = autotune.analyse(nights(WIDE, lambda t: -1.2, noise=0.1),
                             TARGET, settings(offset=autotune.OFFSET_MAX))
        text = " ".join(r["notes_sv"])
        self.assertIn("gränsen", text)
        # The range in that sentence used to read "(-10..10)": a hyphen where
        # the page has a minus sign, and two dots where Swedish says "till".
        self.assertNotIn("-10", text)
        self.assertNotIn("..", text)
        self.assertIn("\u221210 till 10", text)

    def test_no_hyphen_minus_survives_into_any_swedish_string(self):
        r = autotune.analyse(nights(WIDE, lambda t: -1.2 - 0.25 * t, noise=0.05),
                             TARGET, settings(offset=0, curve=0))
        strings = list(r["notes_sv"]) + [r.get("reason_sv") or ""]
        proposal = r.get("proposal") or {}
        strings += [str(proposal.get("why_sv") or ""),
                    str(proposal.get("expected_sv") or "")]
        for text in strings:
            for bad in ("-1 °C", "-5 °C", "-10 °C", "-15 °C", "-20 °C"):
                self.assertNotIn(bad, text, text)


if __name__ == "__main__":
    unittest.main()
