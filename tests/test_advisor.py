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


class FakePump:
    """Answers read_many() from a dict of address -> value."""

    def __init__(self, values: dict):
        self.values = values

    def read_many(self, addresses):
        return {a: {"value": self.values[a]} for a in addresses if a in self.values}


class FakeStore:
    def __init__(self, hours=100.0):
        self.hours = hours

    def stats(self):
        return {"rows": 1, "first": 0, "last": int(self.hours * 3600)}


def own_curve_pump(points=(45, 45, 45, 37, 33, 23, 15), over=None):
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
    return FakePump(values)


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
        self.assertIn("Elpatronen", a.blocked)

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


if __name__ == "__main__":
    unittest.main()
