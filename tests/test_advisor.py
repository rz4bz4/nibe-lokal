"""Tests for the heating advice, against a fake pump.

The advisor writes to nothing, but what it *suggests* ends up as a real change
to a real house, so the cases that matter are the ones where a plausible-looking
suggestion would do nothing (a curve point at a temperature the weather never
reaches) or the wrong thing (curve 1 -> 0 silently switches the whole regime).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nibelokal import advisor                                  # noqa: E402
from nibelokal.advisor import OWN_CURVE_OUTDOOR, R_OWN_CURVE   # noqa: E402
from nibelokal.profile import Profile                          # noqa: E402


class FakePump:
    """Answers read_many() from a dict of address -> value.

    Carries a profile, because a Pump does: it is what says which outdoor
    temperature each own-curve point governs, and on the F generation the
    seventh point's is not established.
    """

    def __init__(self, values: dict, profile=None):
        self.values = values
        self.profile = profile if profile is not None else Profile("S")

    def read_many(self, addresses):
        return {a: {"value": self.values[a]} for a in addresses if a in self.values}


class FakeStore:
    def __init__(self, hours=100.0):
        self.hours = hours

    def stats(self):
        return {"rows": 1, "first": 0, "last": int(self.hours * 3600)}


def own_curve_pump(points=(45, 45, 45, 37, 33, 23, 15), over=None, profile=None):
    values = {
        advisor.R_CURVE: 0,
        advisor.R_OFFSET: 0,
        advisor.R_MIN_SUPPLY: 26.0,
        advisor.R_MAX_SUPPLY: 58.0,
        advisor.R_ROOM_SETPOINT: 20.0,
        advisor.R_OUTDOOR: 5.0,
        advisor.R_SUPPLY: 30.0,
        advisor.R_RETURN: 28.0,
        advisor.R_DEGREE_MINUTES: 0.0,
        advisor.R_ADD_HEAT_POWER: 0.0,
        advisor.R_CALC_SUPPLY: 30.0,
    }
    values.update({a: p for a, p in zip(R_OWN_CURVE, points)})
    values.update(over or {})
    return FakePump(values, profile)


def numbered_pump(curve=5, offset=0, over=None):
    values = {
        advisor.R_CURVE: curve,
        advisor.R_OFFSET: offset,
        advisor.R_MIN_SUPPLY: 20.0,
        advisor.R_MAX_SUPPLY: 60.0,
        advisor.R_ROOM_SETPOINT: 21.0,
        advisor.R_OUTDOOR: 2.0,
        advisor.R_SUPPLY: 35.0,
        advisor.R_RETURN: 32.0,
        advisor.R_DEGREE_MINUTES: -60.0,
        advisor.R_ADD_HEAT_POWER: 0.0,
        advisor.R_CALC_SUPPLY: 36.0,
    }
    values.update(over or {})
    return FakePump(values)


class Validation(unittest.TestCase):
    def test_bad_arguments_are_rejected_before_reading_the_pump(self):
        class Exploding:
            def read_many(self, addresses):
                raise AssertionError("the pump must not be read for a bad request")

        with self.assertRaises(ValueError):
            advisor.advise(Exploding(), "hot", "always")
        with self.assertRaises(ValueError):
            advisor.advise(Exploding(), "warmer", "on tuesdays")


class Offset(unittest.TestCase):
    def test_wrong_in_all_weather_moves_the_offset(self):
        a = advisor.advise(numbered_pump(offset=0), "warmer", "always")
        self.assertEqual([(s.address, s.proposed) for s in a.suggestions],
                         [(advisor.R_OFFSET, 1)])

    def test_colder_moves_it_the_other_way(self):
        a = advisor.advise(numbered_pump(offset=3), "colder", "always")
        self.assertEqual(a.suggestions[0].proposed, 2)

    def test_offset_at_its_limit_is_refused_with_a_reason(self):
        a = advisor.advise(numbered_pump(offset=10), "warmer", "always")
        self.assertFalse(a.suggestions)
        self.assertIn("gränsen", a.blocked)


class NumberedCurve(unittest.TestCase):
    def test_cold_only_steepens_the_curve(self):
        a = advisor.advise(numbered_pump(curve=5), "warmer", "cold_outside")
        self.assertEqual([(s.address, s.proposed) for s in a.suggestions],
                         [(advisor.R_CURVE, 6)])

    def test_never_proposes_curve_zero(self):
        # Regression: curve 1 -> 0 is not "flatter", it switches the pump to the
        # own-curve points entirely. It used to be suggested as a normal step.
        a = advisor.advise(numbered_pump(curve=1), "colder", "cold_outside")
        self.assertFalse(a.suggestions)
        self.assertIn("egen kurva", a.blocked)

        b = advisor.advise(numbered_pump(curve=1, offset=0), "warmer", "mild_outside")
        self.assertFalse(b.suggestions)
        self.assertIn("egen kurva", b.blocked)

    def test_never_proposes_above_the_steepest_curve(self):
        a = advisor.advise(numbered_pump(curve=15), "warmer", "cold_outside")
        self.assertFalse(a.suggestions)
        self.assertIn("brantaste", a.blocked)

    def test_mild_only_pairs_curve_and_offset_in_one_group(self):
        a = advisor.advise(numbered_pump(curve=5, offset=0), "warmer", "mild_outside")
        self.assertEqual(len(a.suggestions), 2)
        # Flatter curve, offset the other way: they cancel out in the cold.
        self.assertEqual(a.suggestions[0].proposed, 4)
        self.assertEqual(a.suggestions[1].proposed, 1)
        groups = {s.group for s in a.suggestions}
        self.assertEqual(groups, {"mild_pair"}, "the pair must be applied together")

    def test_the_pair_is_refused_when_only_half_of_it_is_possible(self):
        # Offset already at the limit: applying the curve alone would make cold
        # weather worse, which is the opposite of what was asked.
        a = advisor.advise(numbered_pump(curve=5, offset=10), "warmer", "mild_outside")
        self.assertFalse(a.suggestions)
        self.assertIn("offset", a.blocked.lower())


class OwnCurvePoints(unittest.TestCase):
    def test_cold_advice_lands_on_points_the_weather_actually_reaches(self):
        # Regression: this used to pick P1 and P2, which sit at -30 and -20 C.
        # A Swedish winter day is -5 to -15, so the advice did nothing.
        a = advisor.advise(own_curve_pump(over={advisor.R_OUTDOOR: 0.0}), "warmer", "cold_outside")
        addresses = [s.address for s in a.suggestions]
        self.assertTrue(addresses, "expected a suggestion")
        temps = [OWN_CURVE_OUTDOOR[R_OWN_CURVE.index(x)] for x in addresses]
        self.assertTrue(all(-25 <= t <= 5 for t in temps),
                        "advice landed on %s C, which normal weather never reaches" % temps)

    def test_mild_advice_lands_near_mild_weather(self):
        a = advisor.advise(own_curve_pump(over={advisor.R_OUTDOOR: 8.0}),
                           "colder", "mild_outside")
        temps = [OWN_CURVE_OUTDOOR[R_OWN_CURVE.index(s.address)] for s in a.suggestions]
        self.assertTrue(all(-5 <= t <= 20 for t in temps), temps)

    def test_the_outdoor_temperature_steers_the_choice(self):
        cold = advisor.advise(own_curve_pump(over={advisor.R_OUTDOOR: -15.0}),
                              "warmer", "cold_outside")
        mild = advisor.advise(own_curve_pump(over={advisor.R_OUTDOOR: 10.0}),
                              "warmer", "cold_outside")
        cold_t = [OWN_CURVE_OUTDOOR[R_OWN_CURVE.index(s.address)] for s in cold.suggestions]
        mild_t = [OWN_CURVE_OUTDOOR[R_OWN_CURVE.index(s.address)] for s in mild.suggestions]
        self.assertLess(min(cold_t), min(mild_t))

    def test_a_point_against_the_ceiling_is_skipped_and_explained(self):
        pump = own_curve_pump(points=(58, 58, 58, 58, 58, 58, 58))
        a = advisor.advise(pump, "warmer", "cold_outside")
        self.assertFalse(a.suggestions)
        self.assertTrue(a.blocked or a.warnings)
        text = " ".join(a.warnings) + a.blocked
        self.assertIn("40039", text, "should name max supply, not min")

    def test_a_point_under_the_floor_names_min_supply_not_max(self):
        # Regression: this used to say "raise max supply (40039)" when it was
        # min supply (40035) doing the limiting, and in the wrong direction.
        pump = own_curve_pump(points=(45, 45, 45, 37, 20, 18, 15))
        a = advisor.advise(pump, "colder", "mild_outside")
        text = " ".join(a.warnings) + a.blocked
        if not a.suggestions:
            self.assertIn("40035", text)

    def test_unreadable_points_say_so(self):
        pump = own_curve_pump(points=(None,) * 7)
        pump.values = {k: v for k, v in pump.values.items() if v is not None}
        a = advisor.advise(pump, "warmer", "cold_outside")
        self.assertIn("läsa", a.blocked)


class Guards(unittest.TestCase):
    def test_will_not_raise_heat_while_the_immersion_heater_runs(self):
        a = advisor.advise(numbered_pump(over={advisor.R_ADD_HEAT_POWER: 3.0}),
                           "warmer", "always")
        self.assertFalse(a.suggestions)
        # "Tillskott" and not "elpatron": it is the word on the pump's own
        # display and the word the page uses everywhere else.
        self.assertIn("Tillskottet", a.blocked)

    def test_lowering_heat_is_still_allowed_then(self):
        a = advisor.advise(numbered_pump(over={advisor.R_ADD_HEAT_POWER: 3.0}),
                           "colder", "always")
        self.assertTrue(a.suggestions)

    def test_hot_water_priority_gives_a_more_useful_answer(self):
        pump = numbered_pump(over={advisor.R_ADD_HEAT_POWER: 3.0,
                                advisor.R_PRIORITY: "Hot Water"})
        a = advisor.advise(pump, "warmer", "always")
        self.assertIn("varmvatten", a.blocked.lower())

    def test_missing_room_sensor_is_called_out(self):
        a = advisor.advise(numbered_pump(), "warmer", "always")
        self.assertTrue(any("rumsgivare" in w.lower() for w in a.warnings))

    def test_own_curve_warns_before_touching_the_curve_number(self):
        a = advisor.advise(own_curve_pump(), "warmer", "always")
        self.assertTrue(any("egna punkter" in w for w in a.warnings))


class MissingRegisters(unittest.TestCase):
    def test_a_pump_that_answers_nothing_does_not_crash(self):
        empty = FakePump({})
        for feeling in ("warmer", "colder"):
            for when in ("always", "cold_outside", "mild_outside"):
                a = advisor.advise(empty, feeling, when)
                self.assertFalse(a.suggestions)
                self.assertTrue(a.blocked, "%s/%s gave neither advice nor a reason"
                                % (feeling, when))

    def test_none_is_never_treated_as_zero(self):
        state = advisor.diagnose(FakePump({}))
        self.assertIsNone(state["curve"])
        self.assertFalse(state["uses_own_curve"])
        self.assertFalse(state["has_room_sensor"])


class Diagnose(unittest.TestCase):
    def test_calculated_supply_is_preferred_over_the_measured_one(self):
        # The measured supply swings with the compressor; the calculated one is
        # what the curve is asking for, and the honest thing to judge against.
        pump = numbered_pump(over={advisor.R_SUPPLY: 30.0, advisor.R_CALC_SUPPLY: 59.5,
                                advisor.R_MAX_SUPPLY: 60.0})
        state = advisor.diagnose(pump)
        self.assertEqual(state["calculated_supply"], 59.5)
        self.assertTrue(any("taket" in w for w in state["warnings"]))

    def test_own_curve_points_are_reported_with_their_outdoor_temperature(self):
        state = advisor.diagnose(own_curve_pump())
        note = " ".join(state["observations"])
        self.assertIn("ute", note)
        self.assertIn("fram", note)


class SwedishSentencesAreWrittenInSwedish(unittest.TestCase):
    """Numbers formatted with %g and %+d, printed verbatim by the page.

    Every number the web app formats itself uses a decimal comma and U+2212,
    and NIBE's own manual writes "2,5 °C". "Framledningen (38.4 °C)" and "punkt
    P3 (-10 °C ute)" sat next to those -- and the second one is a *title*, so
    it landed in the proposal heading, in the confirm dialog and in the toast.
    autotune had a _sv() helper for exactly this; the helper now lives in the
    package root and everything uses it.
    """

    def _text(self, thing):
        return " ".join(thing if isinstance(thing, list) else [thing])

    def test_a_supply_temperature_in_a_warning(self):
        # The warning judges the *calculated* supply, which is the number a
        # curve change actually moves.
        out = advisor.diagnose(numbered_pump(
            over={advisor.R_CALC_SUPPLY: 59.4, advisor.R_MAX_SUPPLY: 60.0}))
        warning = next(w for w in out["warnings"] if "Framledningen" in w)
        self.assertIn("59,4", warning)
        self.assertNotIn("59.4", warning)

    def test_the_own_curve_points_note(self):
        out = advisor.diagnose(own_curve_pump(points=(45.5, 45, 45, 37, 33, 23, 15)))
        note = next(n for n in out["observations"] if "egen kurva" in n)
        # A real minus sign for the cold end, and no hyphen-minus anywhere.
        self.assertIn("\u221230 °C ute", note)
        self.assertNotIn("-30", note)
        self.assertIn("45,5", note)
        self.assertNotIn("45.5", note)

    def test_the_own_curve_suggestion_title_which_the_page_puts_in_a_heading(self):
        a = advisor.advise(own_curve_pump(), "colder", "cold_outside")
        titles = [s.title for s in a.suggestions if s.group == "own_curve"]
        self.assertTrue(titles)
        for title in titles:
            # "(-10 °C ute)" was the shipped form. A hyphen-minus in a title
            # the page puts in a heading, a dialog and a toast.
            self.assertNotIn("(-", title, title)
            self.assertNotIn("-1", title, title)
        self.assertTrue(any("\u2212" in t for t in titles)
                        or any("+" in t for t in titles), titles)

    def test_and_the_why_underneath_it(self):
        a = advisor.advise(own_curve_pump(), "colder", "cold_outside")
        for s in a.suggestions:
            if s.group == "own_curve":
                self.assertNotIn(" -1", s.why)
                self.assertNotIn(" -2", s.why)

    def test_an_outdoor_temperature_below_zero(self):
        out = advisor.diagnose(numbered_pump(over={advisor.R_OUTDOOR: -7.5}))
        note = next(n for n in out["observations"] if n.startswith("Ute"))
        self.assertIn("\u22127,5", note)
        self.assertNotIn("-7.5", note)

    def test_a_whole_number_does_not_grow_a_decimal(self):
        # "kurvan står på 5", not "kurvan står på 5,0": %g's one good habit.
        a = advisor.advise(numbered_pump(curve=15), "warmer", "cold_outside")
        text = (a.blocked or "") + " ".join(s.why for s in a.suggestions)
        self.assertNotIn("15,0", text)


class TheWordForTheImmersionHeater(unittest.TestCase):
    """The pump's own display says "tillskott", and so does the page.

    "Elpatronen" is also wrong on its own terms on a pump that has an external
    additional heat source, which the register (43084) does not distinguish.
    """

    def test_the_blocked_message_says_tillskott(self):
        a = advisor.advise(numbered_pump(over={advisor.R_ADD_HEAT_POWER: 3.0}),
                           "warmer", "always")
        self.assertIn("Tillskottet", a.blocked)
        self.assertNotIn("Elpatron", a.blocked)

    def test_the_hot_water_variant_too(self):
        a = advisor.advise(
            numbered_pump(over={advisor.R_ADD_HEAT_POWER: 3.0,
                                advisor.R_PRIORITY: "Hot Water"}),
            "warmer", "always")
        self.assertIn("Tillskottet", a.blocked)
        self.assertNotIn("Elpatron", a.blocked)

    def test_the_diagnose_warning_too(self):
        out = advisor.diagnose(numbered_pump(over={advisor.R_ADD_HEAT_POWER: 3.0}))
        warning = next(w for w in out["warnings"] if "illskott" in w)
        self.assertNotIn("Elpatron", warning)
        # And the kilowatts in it are Swedish as well.
        self.assertNotIn("3.0", warning)

    def test_the_word_is_gone_from_the_module_altogether(self):
        import inspect
        source = inspect.getsource(advisor)
        self.assertNotIn("Elpatron", source)
        self.assertNotIn("elpatron", source)


class TheSeventhOwnCurvePointOnAnFPump(unittest.TestCase):
    """P7 is +30 C on the S series and an open question on the F.

    NIBE's F750 and F1155 user manuals both show menu 1.9.7 with six rows,
    -30 to +20. The register for a seventh exists with the same default as the
    S series', which is where "+30" came from -- inference from register order,
    not a manual. So on an F pump the point is not labelled with a temperature
    and is not interpolated: an unverified point has no business in the fit
    that decides how warm a house is in mild weather, which is where a heat
    pump spends most of the year.
    """

    def f_pump(self, **kw):
        return own_curve_pump(profile=Profile.for_model("F750"), **kw)

    def test_the_profile_is_where_the_list_lives(self):
        self.assertEqual(Profile("S").own_curve_outdoor,
                         [-30, -20, -10, 0, 10, 20, 30])
        self.assertEqual(Profile("F").own_curve_outdoor,
                         [-30, -20, -10, 0, 10, 20, None])

    def test_diagnose_sends_the_list_on(self):
        # So that the chart in web/index.html draws the same points this
        # module reasons about, from one list rather than a copy of it.
        self.assertEqual(advisor.diagnose(own_curve_pump())["own_curve_outdoor"],
                         [-30, -20, -10, 0, 10, 20, 30])
        self.assertIsNone(advisor.diagnose(self.f_pump())["own_curve_outdoor"][6])

    def test_the_unverified_point_is_left_out_of_the_interpolation(self):
        points = [45, 45, 45, 37, 33, 23, 15]
        f = Profile("F").own_curve_outdoor
        # Above +20 the F curve is flat at P6, because P6 is the warmest point
        # whose weather is known -- not a ramp down towards a P7 at a guessed
        # +30.
        self.assertEqual(advisor.curve_at(points, 25.0, f), 23)
        self.assertEqual(advisor.curve_at(points, 20.0, f), 23)
        # The S series does interpolate towards P7, because NIBE says where it
        # is.
        self.assertEqual(advisor.curve_at(points, 25.0), 19)

    def test_the_s_series_answer_is_untouched(self):
        points = [45, 45, 45, 37, 33, 23, 15]
        for outdoor in (-40, -30, -15, 0, 5, 12, 20, 30, 40):
            self.assertEqual(advisor.curve_at(points, outdoor),
                             advisor.curve_at(points, outdoor, OWN_CURVE_OUTDOOR),
                             outdoor)

    def test_no_advice_ever_proposes_the_unverified_point(self):
        for feeling in ("warmer", "colder"):
            for when in ("cold_outside", "mild_outside"):
                for outdoor in (-15.0, 0.0, 8.0, 18.0, 25.0):
                    a = advisor.advise(
                        self.f_pump(over={advisor.R_OUTDOOR: outdoor}),
                        feeling, when)
                    self.assertNotIn(
                        R_OWN_CURVE[6], [s.address for s in a.suggestions],
                        "P7 proposed at %s C ute (%s, %s), and nobody knows "
                        "what weather it governs" % (outdoor, feeling, when))

    def test_the_mild_end_of_the_advice_still_works_on_an_f_pump(self):
        # Excluding P7 must not mean excluding the advice: P6 is the mild end
        # there, and it is a real point with a documented temperature.
        a = advisor.advise(self.f_pump(over={advisor.R_OUTDOOR: 12.0}),
                           "colder", "mild_outside")
        self.assertTrue([s for s in a.suggestions if s.address in R_OWN_CURVE],
                        "no own-curve suggestion at all in mild weather")

    def test_the_s_series_advice_still_reaches_p7(self):
        # Points kept clear of the 26 C floor, so what is being tested is which
        # point governs +25 C and not whether it is pinned against min supply.
        a = advisor.advise(
            own_curve_pump(points=(45, 45, 45, 37, 33, 31, 29),
                           over={advisor.R_OUTDOOR: 25.0}),
            "colder", "mild_outside")
        self.assertIn(R_OWN_CURVE[6], [s.address for s in a.suggestions])

    def test_and_the_same_curve_on_an_f_pump_stops_at_p6(self):
        a = advisor.advise(
            self.f_pump(points=(45, 45, 45, 37, 33, 31, 29),
                        over={advisor.R_OUTDOOR: 25.0}),
            "colder", "mild_outside")
        addresses = [s.address for s in a.suggestions]
        self.assertIn(R_OWN_CURVE[5], addresses)
        self.assertNotIn(R_OWN_CURVE[6], addresses)


if __name__ == "__main__":
    unittest.main()
