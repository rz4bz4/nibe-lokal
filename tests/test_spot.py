"""Tests for spot prices and the offset plan, against canned Tibber responses.

Nothing here touches the network. The cases that matter are the ones where a
plausible-looking plan would do the wrong thing to a real house: a flat day
where relative banding finds a "cheapest quartile" that is worth four öre, an
offset that grows past one step, and a day whose offsets do not cancel out --
which is no longer load shifting but load shedding, and ends with a cold house.
"""
import datetime as dt
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nibelokal import spot                                     # noqa: E402
from nibelokal.spot import Plan, Tibber                        # noqa: E402

DAY = "2026-01-15"
NEXT = "2026-01-16"
# Middle of the night on the 15th, so "now" lands inside the fixture.
AT = dt.datetime.fromisoformat("2026-01-15T03:30:00+01:00").timestamp()

# A real winter shape: cheap at night, a morning peak, a worse evening peak.
SPIKY = [0.42, 0.38, 0.35, 0.33, 0.34, 0.51, 0.94, 1.62,
         1.71, 1.20, 0.88, 0.79, 0.75, 0.74, 0.78, 0.96,
         1.44, 2.05, 1.88, 1.31, 0.97, 0.72, 0.58, 0.47]
# A windy Sunday: nine öre between the best and worst hour of the day.
FLAT = [0.31 + (i % 4) * 0.03 for i in range(24)]


def prices(totals, day=DAY, offset="+01:00"):
    """Tibber Price entries: local wall time with an explicit offset."""
    return [{"total": total,
             "startsAt": "%sT%02d:00:00.000%s" % (day, hour, offset),
             "level": "NORMAL"}
            for hour, total in enumerate(totals)]


def payload(today, tomorrow=None, currency="SEK", current_hour=3):
    """A Tibber response. `current` mirrors one of today's hours, as the API does."""
    usable = [row for row in today if isinstance(row, dict)]
    current = None
    if len(usable) > current_hour:
        current = dict(usable[current_hour], currency=currency)
    return {"data": {"viewer": {"homes": [{
        "id": "home-1",
        "currentSubscription": {"priceInfo": {
            "current": current,
            "today": today,
            "tomorrow": tomorrow if tomorrow is not None else [],
        }},
    }]}}}


class FakeTibber(Tibber):
    """A Tibber that answers from a canned payload instead of the network."""

    def __init__(self, response, config=None):
        base = {"tibber_token": "test-token", "spot_cache_seconds": 0}
        base.update(config or {})
        super().__init__(base)
        self.response = response
        self.calls = 0
        self.queries = []

    def _post(self, query):
        self.calls += 1
        self.queries.append(query)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def snap(totals, tomorrow=None, config=None, at=AT):
    return FakeTibber(payload(prices(totals), tomorrow), config).snapshot(now=at)


class Fetching(unittest.TestCase):
    def test_no_token_is_reported_as_unconfigured_not_as_an_error(self):
        result = Tibber({}).snapshot(now=AT)
        self.assertFalse(result["ok"])
        self.assertFalse(result["configured"])
        self.assertIn("tibber_token", result["error"])

    def test_a_normal_response_is_banded_and_summarised(self):
        result = snap(SPIKY)
        self.assertTrue(result["ok"])
        self.assertEqual(result["horizon_hours"], 24)
        self.assertEqual(result["currency"], "SEK")
        self.assertAlmostEqual(result["stats"]["min"], min(SPIKY))
        self.assertAlmostEqual(result["stats"]["max"], max(SPIKY))
        self.assertAlmostEqual(result["stats"]["median"], 0.785)
        self.assertEqual(result["now"]["starts_at"], "2026-01-15T03:00:00+01:00")
        self.assertEqual(result["now"]["band"], spot.BAND_CHEAP)

    def test_the_query_asks_for_whole_hours(self):
        # A quarter-hourly schedule for a register that shifts the whole house
        # by 2.5 C is not something anyone asked for.
        client = FakeTibber(payload(prices(SPIKY)))
        client.snapshot(now=AT)
        self.assertEqual(client.calls, 1)
        self.assertIn("resolution: HOURLY", client.queries[0])
        self.assertIn("priceInfo", client.queries[0])
        for field in ("today", "tomorrow", "total", "startsAt", "level", "currency"):
            self.assertIn(field, client.queries[0])

    def test_an_older_schema_without_the_resolution_argument_is_retried(self):
        class Fussy(FakeTibber):
            def _post(self, query):
                super()._post(query)
                if "resolution" in query:
                    return {"errors": [{"message": 'Unknown argument "resolution".'}]}
                return payload(prices(SPIKY))

        client = Fussy(None)
        result = client.snapshot(now=AT)
        self.assertTrue(result["ok"])
        self.assertEqual(client.calls, 2)
        self.assertNotIn("resolution", client.queries[1])

    def test_prices_are_cached_rather_than_refetched_every_page_load(self):
        client = FakeTibber(payload(prices(SPIKY)), {"spot_cache_seconds": 900})
        client.snapshot(now=AT)
        second = client.snapshot(now=AT + 60)
        self.assertEqual(client.calls, 1)
        self.assertTrue(second["cached"])


class MissingTomorrow(unittest.TestCase):
    def test_an_empty_tomorrow_is_the_normal_state_not_a_failure(self):
        # Tomorrow's prices do not exist until the day-ahead auction is published
        # and Tibber picks it up, around 13:00. Half of every day looks like this.
        result = snap(SPIKY, tomorrow=[])
        self.assertTrue(result["ok"])
        self.assertFalse(result["has_tomorrow"])
        self.assertEqual(result["horizon_hours"], 24)

    def test_tomorrow_extends_the_horizon_and_is_planned_separately(self):
        result = snap(SPIKY, tomorrow=prices(SPIKY, day=NEXT))
        self.assertTrue(result["has_tomorrow"])
        self.assertEqual(result["horizon_hours"], 48)
        plan = Plan().build(result, now=AT)
        self.assertEqual([day["day"] for day in plan["days"]], [DAY, NEXT])

    def test_a_missing_tomorrow_key_is_treated_as_no_tomorrow(self):
        raw = payload(prices(SPIKY))
        home = raw["data"]["viewer"]["homes"][0]
        del home["currentSubscription"]["priceInfo"]["tomorrow"]
        result = FakeTibber(raw).snapshot(now=AT)
        self.assertTrue(result["ok"])
        self.assertEqual(result["horizon_hours"], 24)


class Banding(unittest.TestCase):
    def test_a_flat_day_is_normal_all_the_way_through(self):
        # Relative banding on its own always finds a cheapest quartile. On a day
        # whose whole spread is nine öre there is nothing to optimise, and saying
        # so is the honest answer.
        result = snap(FLAT)
        self.assertTrue(result["ok"])
        self.assertTrue(result["stats"]["flat"])
        self.assertEqual({row["band"] for row in result["hours"]}, {spot.BAND_NORMAL})

    def test_a_spiky_day_bands_the_night_cheap_and_the_evening_dear(self):
        result = snap(SPIKY)
        bands = {row["starts_at"][11:13]: row["band"] for row in result["hours"]}
        self.assertEqual(bands["03"], spot.BAND_CHEAP)
        self.assertEqual(bands["17"], spot.BAND_EXPENSIVE)
        self.assertEqual(bands["12"], spot.BAND_NORMAL)
        self.assertEqual(sum(1 for b in bands.values() if b == spot.BAND_CHEAP), 6)
        self.assertEqual(sum(1 for b in bands.values() if b == spot.BAND_EXPENSIVE), 6)

    def test_banding_is_relative_so_an_expensive_winter_day_still_has_cheap_hours(self):
        # Every hour here costs more than the worst hour of the flat day above.
        # A fixed öre threshold would call all 24 expensive and say nothing useful.
        result = snap([price + 2.0 for price in SPIKY])
        bands = [row["band"] for row in result["hours"]]
        self.assertIn(spot.BAND_CHEAP, bands)
        self.assertIn(spot.BAND_EXPENSIVE, bands)

    def test_the_spread_floor_is_a_config_key(self):
        # The same day is flat or not depending only on spot_min_spread.
        loose = snap(FLAT, config={"spot_min_spread": 0.01})
        self.assertFalse(loose["stats"]["flat"])
        self.assertIn(spot.BAND_CHEAP, [row["band"] for row in loose["hours"]])
        strict = snap(SPIKY, config={"spot_min_spread": 5.0})
        self.assertEqual({row["band"] for row in strict["hours"]}, {spot.BAND_NORMAL})

    def test_identical_prices_do_not_all_land_in_the_cheapest_quartile(self):
        result = snap([1.0] * 12 + [3.0] * 12)
        cheap = [row for row in result["hours"] if row["band"] == spot.BAND_CHEAP]
        dear = [row for row in result["hours"] if row["band"] == spot.BAND_EXPENSIVE]
        self.assertEqual(len(cheap), 12)
        self.assertEqual(len(dear), 12)


class PlanShape(unittest.TestCase):
    def test_the_offset_never_leaves_minus_one_to_plus_one(self):
        plan = Plan().build(snap(SPIKY), now=AT)
        deltas = {row["offset_delta"] for row in plan["hours"]}
        self.assertTrue(deltas <= {-1, 0, 1}, deltas)
        self.assertEqual(plan["max_offset"], 1)

    def test_a_config_asking_for_more_than_one_step_is_clamped(self):
        # One step is already about 2.5 C of supply temperature. A typo here must
        # not become a system that swings the house by five degrees.
        plan = Plan({"spot_max_offset": 4}).build(snap(SPIKY), now=AT)
        self.assertEqual(plan["max_offset"], 1)
        self.assertTrue(all(abs(row["offset_delta"]) <= 1 for row in plan["hours"]))

    def test_the_plan_is_advisory_and_names_the_offset_register(self):
        plan = Plan().build(snap(SPIKY), now=AT)
        self.assertTrue(plan["advisory"])
        self.assertEqual(plan["register"], 40031)

    def test_sg_ready_is_not_part_of_the_plan(self):
        # Deliberate: SG Ready's effect depends on pump settings this app does
        # not control. See the module docstring.
        plan = Plan().build(snap(SPIKY), now=AT)
        self.assertNotIn(str(spot.R_SG_READY), json.dumps(plan))

    def test_cheap_hours_go_up_and_expensive_hours_go_down(self):
        plan = Plan().build(snap(SPIKY), now=AT)
        for row in plan["hours"]:
            if row["offset_delta"] > 0:
                self.assertEqual(row["band"], spot.BAND_CHEAP)
            elif row["offset_delta"] < 0:
                self.assertEqual(row["band"], spot.BAND_EXPENSIVE)

    def test_the_plan_is_data_and_nothing_else(self):
        # Wiring the plan to the pump happens elsewhere, behind safety.check().
        # A plan that handed back a callable would be a way around that.
        plan = Plan().build(snap(SPIKY), now=AT)
        self.assertTrue(plan["ok"])
        self.assertEqual(json.loads(json.dumps(plan)), plan)


class SumsToZero(unittest.TestCase):
    """The property the whole module rests on: shifting, not shedding."""

    def test_every_day_of_offsets_cancels_out_exactly(self):
        for totals in (SPIKY, FLAT, [p + 2.0 for p in SPIKY], sorted(SPIKY)):
            result = snap(totals, tomorrow=prices(list(reversed(SPIKY)), day=NEXT))
            plan = Plan().build(result, now=AT)
            for day in plan["days"]:
                rows = [row for row in plan["hours"] if row["day"] == day["day"]]
                self.assertEqual(sum(row["offset_delta"] for row in rows), 0,
                                 "%s does not cancel out" % day["day"])
                self.assertEqual(day["net"], 0)

    def test_any_24_hour_window_is_approximately_zero(self):
        # Days cancel exactly. A window straddling midnight is the tail of one
        # day plus the head of the next, so it is bounded by twice the pairs a day
        # may use -- that is the "approximately", and it is why the cap exists.
        result = snap(SPIKY, tomorrow=prices(list(reversed(SPIKY)), day=NEXT))
        plan = Plan().build(result, now=AT)
        deltas = [row["offset_delta"] for row in plan["hours"]]
        self.assertEqual(len(deltas), 48)
        for start in range(len(deltas) - 24 + 1):
            window = sum(deltas[start:start + 24])
            self.assertLessEqual(abs(window), 2 * Plan().max_pairs,
                                 "window at %d sums to %d" % (start, window))
        self.assertEqual(sum(deltas[0:24]), 0)
        self.assertEqual(sum(deltas[24:48]), 0)

    def test_a_flat_day_moves_nothing_at_all(self):
        plan = Plan().build(snap(FLAT), now=AT)
        self.assertEqual({row["offset_delta"] for row in plan["hours"]}, {0})
        self.assertIn("liten", plan["days"][0]["reason"])

    def test_lopsided_banding_still_pairs_up(self):
        # One very expensive hour and eleven cheap ones: the plan may only move as
        # many hours up as it can move down, or the day stops cancelling out.
        totals = [0.20] * 23 + [4.00]
        plan = Plan().build(snap(totals), now=AT)
        up = sum(1 for row in plan["hours"] if row["offset_delta"] > 0)
        down = sum(1 for row in plan["hours"] if row["offset_delta"] < 0)
        self.assertEqual(up, down)
        self.assertEqual(sum(row["offset_delta"] for row in plan["hours"]), 0)

    def test_the_number_of_moved_hours_is_capped(self):
        plan = Plan({"spot_max_pairs": 2}).build(snap(SPIKY), now=AT)
        moved = sum(1 for row in plan["hours"] if row["offset_delta"])
        self.assertEqual(moved, 4)

    def test_pairs_zero_disables_the_whole_thing(self):
        plan = Plan({"spot_max_pairs": 0}).build(snap(SPIKY), now=AT)
        self.assertEqual({row["offset_delta"] for row in plan["hours"]}, {0})


class BadResponses(unittest.TestCase):
    def _error(self, response):
        result = FakeTibber(response).snapshot(now=AT)
        self.assertFalse(result["ok"])
        self.assertTrue(result["configured"])
        self.assertTrue(result["error"], "a failure must say why, in Swedish")
        return result["error"]

    def test_garbage_json_does_not_raise(self):
        for response in ({}, [], "nope", None, {"data": None},
                         {"data": {"viewer": {}}},
                         {"data": {"viewer": {"homes": []}}},
                         {"data": {"viewer": {"homes": [{"currentSubscription": None}]}}},
                         {"errors": [{"message": "invalid token"}]},
                         {"errors": "something went wrong"}):
            self._error(response)

    def test_prices_that_are_not_numbers_are_dropped_not_believed(self):
        rows = prices(SPIKY)
        rows[0]["total"] = "1,42"
        rows[1]["total"] = None
        rows[2]["startsAt"] = "not a timestamp"
        rows[3] = "not even a dict"
        result = FakeTibber(payload(rows)).snapshot(now=AT)
        self.assertTrue(result["ok"])
        self.assertEqual(result["horizon_hours"], 20)
        self.assertTrue(all(isinstance(row["total"], float) for row in result["hours"]))

    def test_a_response_with_no_usable_prices_is_an_error(self):
        message = self._error(payload([{"total": None, "startsAt": None}]))
        self.assertIn("priser", message)

    def test_network_failures_are_reported_in_swedish(self):
        import urllib.error
        message = self._error(urllib.error.URLError("connection refused"))
        self.assertIn("Kunde inte nå Tibber", message)

    def test_a_rejected_token_says_so(self):
        import urllib.error
        message = self._error(
            urllib.error.HTTPError(spot.API_URL, 401, "Unauthorized", {}, None))
        self.assertIn("token", message.lower())

    def test_a_garbage_snapshot_produces_a_plan_that_explains_itself(self):
        for bad in ({}, {"ok": False, "error": "trasigt"}, {"ok": True, "hours": []},
                    {"ok": True, "hours": [{"nonsense": 1}]}):
            plan = Plan().build(bad)
            self.assertFalse(plan["ok"])
            self.assertEqual(plan["hours"], [])
            self.assertTrue(plan["error"])

    def test_nothing_public_raises_whatever_it_is_handed(self):
        for value in (None, 0, "", [], {"ok": True, "hours": "not a list"}):
            self.assertIsInstance(Plan().build(value), dict)


class Timestamps(unittest.TestCase):
    def test_quarter_hourly_data_is_folded_into_whole_hours(self):
        # Tibber added QUARTER_HOURLY when the Nordic market went 15-minute. If
        # the hourly resolution ever stops being offered, the schedule must still
        # come out as 24 rows and not 96.
        quarters = []
        for hour, total in enumerate(SPIKY):
            for quarter in range(4):
                quarters.append({
                    "total": total + quarter * 0.01,
                    "startsAt": "%sT%02d:%02d:00.000+01:00" % (DAY, hour, quarter * 15),
                    "level": "NORMAL",
                })
        result = FakeTibber(payload(quarters)).snapshot(now=AT)
        self.assertEqual(result["horizon_hours"], 24)
        self.assertAlmostEqual(result["hours"][0]["total"], SPIKY[0] + 0.015)

    def test_a_dst_day_of_23_hours_still_cancels_out(self):
        # Sweden loses an hour on the last Sunday in March. A plan that assumed
        # 24 hours in a day would unbalance itself twice a year.
        rows = ([{"total": SPIKY[h], "startsAt": "2026-03-29T%02d:00:00.000+01:00" % h,
                  "level": "NORMAL"} for h in range(3)]
                + [{"total": SPIKY[h], "startsAt": "2026-03-29T%02d:00:00.000+02:00" % h,
                    "level": "NORMAL"} for h in range(3, 24)])
        result = FakeTibber(payload(rows)).snapshot(now=AT)
        plan = Plan().build(result, now=AT)
        self.assertEqual(result["horizon_hours"], 24)
        self.assertEqual(len(plan["days"]), 1)
        self.assertEqual(plan["days"][0]["net"], 0)

    def test_stale_prices_are_not_reported_as_now(self):
        old = dt.datetime.fromisoformat("2026-01-20T12:00:00+01:00").timestamp()
        result = snap(SPIKY, at=old)
        self.assertTrue(result["ok"])
        self.assertIsNone(result["now"])


if __name__ == "__main__":
    unittest.main()
