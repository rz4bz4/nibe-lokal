"""Tests for the SMHI forecast, against a recorded response. No network.

The failure this module exists to prevent is the quiet one. SMHI retired the
pmp3g category on 2026-03-31 and replaced it with snow1g, which not only
renamed the endpoint but changed the payload from `validTime` plus a list of
`{"name": "t", "values": [...]}` to `time` plus a flat `data` object of long
names. A parser written against the old documentation does not crash on the
new payload -- it finds nothing, and reports a house with no weather.

So the fixture below is a real snow1g response (fetched 2026-09-06, trimmed to
six hours), and the tests assert on values read out of it rather than on the
shape of the code.
"""
import json
import os
import sys
import time
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nibelokal import weather                                   # noqa: E402

# The daily rows are grouped by local date, so the tests need a known one.
# Europe/Stockholm is UTC+2 in September: 22:00Z is already the next day here,
# which is exactly the boundary worth testing.
os.environ["TZ"] = "Europe/Stockholm"
if hasattr(time, "tzset"):
    time.tzset()

needs_tz = unittest.skipUnless(hasattr(time, "tzset"),
                               "local-date grouping needs a settable timezone")

# Recorded from
# https://opendata-download-metfcst.smhi.se/api/category/snow1g/version/1
#   /geotype/point/lon/16.158000/lat/58.581200/data.json  on 2026-09-06.
# Trimmed to six hours and to the six parameters this module reads; the real
# response carries 81 hours and 24 parameters per hour.
FIXTURE = json.loads("""
{
  "createdTime": "2026-09-06T19:18:26Z",
  "referenceTime": "2026-09-06T19:00:00Z",
  "geometry": {"type": "Point", "coordinates": [16.158549, 58.577821]},
  "timeSeries": [
    {"time": "2026-09-06T20:00:00Z", "data": {"air_temperature": 14.4,
      "wind_speed": 3.8, "relative_humidity": 67, "cloud_area_fraction": 7,
      "precipitation_amount_mean": 0.0, "symbol_code": 3}},
    {"time": "2026-09-06T21:00:00Z", "data": {"air_temperature": 13.7,
      "wind_speed": 4.0, "relative_humidity": 71, "cloud_area_fraction": 7,
      "precipitation_amount_mean": 0.0, "symbol_code": 3}},
    {"time": "2026-09-06T22:00:00Z", "data": {"air_temperature": 12.8,
      "wind_speed": 3.7, "relative_humidity": 78, "cloud_area_fraction": 5,
      "precipitation_amount_mean": 0.0, "symbol_code": 2}},
    {"time": "2026-09-06T23:00:00Z", "data": {"air_temperature": 11.7,
      "wind_speed": 3.4, "relative_humidity": 85, "cloud_area_fraction": 0,
      "precipitation_amount_mean": 0.0, "symbol_code": 1}},
    {"time": "2026-09-07T00:00:00Z", "data": {"air_temperature": 11.4,
      "wind_speed": 3.4, "relative_humidity": 88, "cloud_area_fraction": 8,
      "precipitation_amount_mean": 0.0, "symbol_code": 4}},
    {"time": "2026-09-07T01:00:00Z", "data": {"air_temperature": 11.4,
      "wind_speed": 3.0, "relative_humidity": 88, "cloud_area_fraction": 8,
      "precipitation_amount_mean": 0.2, "symbol_code": 4}}
  ]
}
""")

CONFIG = {"weather_lat": 58.5812, "weather_lon": 16.158}


def fake(payload, config=None, max_age=0.0):
    """A Weather that answers from `payload` instead of the network."""
    w = weather.Weather(config if config is not None else CONFIG)
    w._fetch = lambda: (payload, max_age)
    return w


def broken(exc, config=None):
    w = weather.Weather(config if config is not None else CONFIG)

    def boom():
        raise exc

    w._fetch = boom
    return w


def series(temps, ws=0.0, start="2026-01-10T00:00:00Z"):
    """A synthetic hourly series from a list of temperatures."""
    hour = int(start[11:13])
    day = int(start[8:10])
    out = []
    for i, t in enumerate(temps):
        h = (hour + i) % 24
        d = day + (hour + i) // 24
        out.append({"time": "2026-01-%02dT%02d:00:00Z" % (d, h),
                    "data": {"air_temperature": t, "wind_speed": ws,
                             "relative_humidity": 80, "cloud_area_fraction": 4,
                             "precipitation_amount_mean": 0.0, "symbol_code": 5}})
    return {"referenceTime": start, "timeSeries": out}


class Endpoint(unittest.TestCase):
    def test_the_url_is_snow1g_v1_and_not_the_retired_pmp3g(self):
        # Regression against the whole reason this module was rewritten: pmp3g
        # version 2 stopped being served on 2026-03-31 and now 404s.
        url = weather.Weather(CONFIG).url()
        self.assertIn("/category/snow1g/version/1/", url)
        self.assertNotIn("pmp3g", url)

    def test_coordinates_never_exceed_six_decimals(self):
        # More than six decimals is a 404 from SMHI, and a float that prints as
        # 58.58120000000001 is exactly how a naive formatter gets there.
        url = weather.Weather({"weather_lat": 58.58120000000001,
                               "weather_lon": 16.1 / 3}).url()
        for part in url.split("/"):
            if "." in part and part.replace(".", "").replace("-", "").isdigit():
                self.assertLessEqual(len(part.split(".")[1]), 6, url)


class Parsing(unittest.TestCase):
    def test_the_recorded_response_parses(self):
        snap = fake(FIXTURE).snapshot()
        self.assertTrue(snap["ok"], snap)
        self.assertEqual(snap["issued"], "2026-09-06T19:00:00Z")
        self.assertEqual(len(snap["hourly"]), 6)
        self.assertEqual(snap["now"]["t"], 14.4)
        self.assertEqual(snap["now"]["ws"], 3.8)
        self.assertEqual(snap["now"]["symbol"], 3)
        self.assertEqual(snap["now"]["symbol_sv"], "växlande molnighet")

    def test_the_hourly_rows_carry_cloud_and_precipitation(self):
        last = fake(FIXTURE).snapshot()["hourly"][-1]
        self.assertEqual(last["cloud"], 8.0)       # octas, not percent
        self.assertEqual(last["precip"], 0.2)      # mm/h
        self.assertEqual(last["symbol_sv"], "halvklart")

    def test_every_symbol_code_one_to_twentyseven_has_swedish_text(self):
        self.assertEqual(sorted(weather.SYMBOLS), list(range(1, 28)))
        for code in range(1, 28):
            self.assertTrue(weather.symbol_text(code).strip(), code)
        self.assertEqual(weather.symbol_text(99), "")
        self.assertEqual(weather.symbol_text(None), "")


@needs_tz
class DailyAggregation(unittest.TestCase):
    def test_days_are_split_on_local_midnight_not_utc(self):
        daily = fake(FIXTURE).snapshot()["daily"]
        self.assertEqual([d["date"] for d in daily], ["2026-09-06", "2026-09-07"])
        # 22:00Z is 00:00 local, so only the first two hours belong to the 6th.
        self.assertEqual(daily[0]["samples"], 2)
        self.assertEqual(daily[1]["samples"], 4)

    def test_min_max_and_mean_per_day(self):
        daily = fake(FIXTURE).snapshot()["daily"]
        self.assertEqual((daily[0]["min"], daily[0]["max"], daily[0]["mean"]),
                         (13.7, 14.4, 14.1))
        self.assertEqual((daily[1]["min"], daily[1]["max"], daily[1]["mean"]),
                         (11.4, 12.8, 11.8))

    def test_a_day_with_a_single_sample_is_not_reported_as_a_min_and_max(self):
        # The forecast thins to twelve-hour steps after a couple of days. One
        # sample is not a day's range, and printing it as one is a lie.
        payload = series([5.0, 4.0, 3.0], start="2026-01-10T22:00:00Z")
        daily = fake(payload).snapshot()["daily"]
        self.assertEqual([d["date"] for d in daily], ["2026-01-11"])


class Trend(unittest.TestCase):
    def test_a_small_drift_is_stable_not_falling(self):
        # The whole point of the deadband: 0.2 C over twelve hours is inside
        # SMHI's own forecast error, and pre-heating for it would be wasted.
        snap = fake(series([5.0] * 6 + [4.8] * 7)).snapshot()
        self.assertEqual(snap["summary"]["trend"], "stabilt")

    def test_a_real_drop_falls(self):
        snap = fake(series([5.0, 5.0, 5.0, 3.0, 1.0, 0.0, -1.0,
                            -2.0, -3.0, -4.0, -5.0, -5.0, -5.0])).snapshot()
        self.assertEqual(snap["summary"]["trend"], "faller")
        self.assertLess(snap["summary"]["trend_c"], -weather.TREND_DEADBAND)

    def test_a_real_rise_rises(self):
        snap = fake(series([-5.0] * 3 + [-3.0, -1.0, 0.0, 1.0,
                            2.0, 3.0, 4.0, 5.0, 5.0, 5.0])).snapshot()
        self.assertEqual(snap["summary"]["trend"], "stiger")

    def test_the_deadband_boundary_counts_as_a_trend(self):
        # Exactly TREND_DEADBAND must not fall through to "stabilt"; an
        # exclusive comparison here leaves a silent gap at the threshold.
        flat = [10.0] * 3
        drop = [10.0 - weather.TREND_DEADBAND] * 3
        payload = series(flat + [10.0] * 7 + drop)
        snap = fake(payload).snapshot()
        self.assertEqual(snap["summary"]["trend_c"], -weather.TREND_DEADBAND)
        self.assertEqual(snap["summary"]["trend"], "faller")

    def test_the_trend_only_looks_twelve_hours_ahead(self):
        # Flat for twelve hours, then a collapse. The pump does not need to know
        # yet, and calling it "faller" now would pre-heat half a day too early.
        payload = series([2.0] * 13 + [-20.0] * 10)
        self.assertEqual(fake(payload).snapshot()["summary"]["trend"], "stabilt")

    def test_the_summary_windows_and_coldest_hour(self):
        snap = fake(series([5.0] * 10 + [-2.0] + [5.0] * 40)).snapshot()
        summary = snap["summary"]
        self.assertEqual(summary["min_24h"], -2.0)
        self.assertEqual(summary["min_48h"], -2.0)
        self.assertTrue(summary["coldest_hour"])
        self.assertIn(summary["coldest_hour"], [h["time"] for h in snap["hourly"]])


class WindChill(unittest.TestCase):
    """The JAG/TI formula is only defined for T <= 10 C and V >= 4.8 km/h."""

    def test_inside_the_range_the_formula_is_applied(self):
        self.assertEqual(weather.wind_chill(0.0, 5.0), -4.9)
        self.assertEqual(weather.wind_chill(-5.0, 10.0), -13.7)

    def test_the_temperature_boundary(self):
        # 10.0 C is inside the range, 10.1 is outside and must be left alone.
        self.assertLess(weather.wind_chill(10.0, 5.0), 10.0)
        self.assertEqual(weather.wind_chill(10.1, 5.0), 10.1)

    def test_the_wind_boundary(self):
        # 4.8 km/h is 1.333 m/s. Below it there is no wind chill to compute,
        # and the formula would "warm" a calm winter day if applied anyway.
        self.assertEqual(weather.wind_chill(0.0, 1.3), 0.0)
        self.assertLess(weather.wind_chill(0.0, 1.4), 0.0)
        self.assertEqual(weather.wind_chill(-10.0, 0.0), -10.0)

    def test_a_missing_wind_speed_falls_back_to_the_air_temperature(self):
        self.assertEqual(weather.wind_chill(-3.0, None), -3.0)
        self.assertIsNone(weather.wind_chill(None, 5.0))

    def test_the_hourly_rows_carry_it(self):
        # A windy zero loses a house more heat than a still zero.
        snap = fake(series([0.0] * 4, ws=8.0)).snapshot()
        self.assertLess(snap["hourly"][0]["effective"], 0.0)
        self.assertEqual(snap["now"]["effective"], snap["hourly"][0]["effective"])


class Failures(unittest.TestCase):
    def test_without_coordinates_the_feature_hides_itself(self):
        w = weather.Weather({})
        self.assertFalse(w.configured)
        snap = w.snapshot()
        self.assertFalse(snap["ok"])
        self.assertIn("weather_lat", snap["error"])

    def test_nonsense_coordinates_count_as_unconfigured(self):
        for cfg in ({"weather_lat": "norrköping", "weather_lon": 16.1},
                    {"weather_lat": 58.5, "weather_lon": 999.0},
                    {"weather_lat": True, "weather_lon": 16.1},
                    {"weather_lat": None, "weather_lon": None}):
            self.assertFalse(weather.Weather(cfg).configured, cfg)

    def test_an_empty_forecast_is_an_error_not_an_empty_page(self):
        snap = fake({"referenceTime": "x", "timeSeries": []}).snapshot()
        self.assertFalse(snap["ok"])
        self.assertIn("prognostimmar", snap["error"])

    def test_garbage_where_the_forecast_should_be(self):
        for payload in ({}, {"timeSeries": "nej"}, {"timeSeries": [1, 2, 3]},
                        {"timeSeries": [{"time": "not a time", "data": {}}]},
                        {"timeSeries": [{"time": "2026-09-06T20:00:00Z",
                                         "data": {"air_temperature": None}}]}):
            snap = fake(payload).snapshot()
            self.assertFalse(snap["ok"], payload)
            self.assertTrue(snap["error"].strip())

    def test_hours_missing_a_temperature_are_dropped_not_zeroed(self):
        # A None temperature read as 0 would poison every min and mean it
        # landed in, and 0 C in a forecast looks perfectly plausible.
        payload = series([5.0, 4.0, 3.0])
        payload["timeSeries"][1]["data"]["air_temperature"] = None
        snap = fake(payload).snapshot()
        self.assertEqual([h["t"] for h in snap["hourly"]], [5.0, 3.0])

    def test_a_point_outside_the_model_area_says_so(self):
        exc = urllib.error.HTTPError(url="x", code=404, msg="Not Found",
                                     hdrs=None, fp=None)
        snap = broken(exc).snapshot()
        self.assertFalse(snap["ok"])
        self.assertIn("utanför", snap["error"])

    def test_no_network_is_a_sentence_not_a_traceback(self):
        snap = broken(urllib.error.URLError("no route to host")).snapshot()
        self.assertFalse(snap["ok"])
        self.assertIn("internet", snap["error"])

    def test_nothing_escapes_snapshot(self):
        # Public methods must never raise: the forecast is a nice-to-have next
        # to a working Modbus connection and must not take the page down. Only
        # Exception is swallowed, so Ctrl-C still stops the server.
        for exc in (TimeoutError("timed out"), MemoryError(),
                    AttributeError("payload changed shape again")):
            w = weather.Weather(CONFIG)

            def boom(exc=exc):
                raise exc

            w._fetch = boom
            snap = w.snapshot()
            self.assertFalse(snap["ok"])
            self.assertTrue(snap["error"].strip())


class Caching(unittest.TestCase):
    def test_a_second_call_inside_the_ttl_does_not_refetch(self):
        w = fake(FIXTURE)
        calls = []
        original = w._fetch
        w._fetch = lambda: (calls.append(1), original())[1]
        first = w.snapshot(now=1000.0)
        second = w.snapshot(now=1000.0 + 59 * 60)
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)
        # Equal, but not the same object: the cache is handed out as a copy so
        # that a caller who edits what it was given cannot corrupt it for
        # everybody else.
        self.assertIsNot(first, second)
        first["now"]["t"] = -999
        self.assertNotEqual(w.snapshot(now=1000.0 + 59 * 60)["now"]["t"], -999)

    def test_smhis_own_cache_control_wins_over_a_shorter_ttl(self):
        # SMHI answers max-age=3600 because the model runs about hourly.
        # Asking again inside that window returns the identical bytes.
        w = fake(FIXTURE, config=dict(CONFIG, weather_ttl_minutes=1), max_age=3600.0)
        w.snapshot(now=0.0)
        self.assertEqual(w._expires, 3600.0)

    def test_an_hour_old_forecast_beats_no_forecast(self):
        w = fake(FIXTURE)
        w.snapshot(now=1000.0)
        w._fetch = lambda: (_ for _ in ()).throw(urllib.error.URLError("down"))
        stale = w.snapshot(now=1000.0 + 2 * 3600)
        self.assertTrue(stale["ok"])
        self.assertTrue(stale["stale"])
        self.assertIn("kunde inte uppdateras", stale["note"])

    def test_but_a_day_old_forecast_does_not(self):
        w = fake(FIXTURE)
        w.snapshot(now=1000.0)
        w._fetch = lambda: (_ for _ in ()).throw(urllib.error.URLError("down"))
        snap = w.snapshot(now=1000.0 + 24 * 3600)
        self.assertFalse(snap["ok"])


class FailuresAreRemembered(unittest.TestCase):
    """SMHI answers the same way to the same bad question every time.

    homey.py and spot.py both remember a failure for a while. This one did not,
    so a point outside the model area (a permanent 404) or a 429 was asked
    again on every single page load -- which is how a rate limit becomes a
    block.
    """

    def _counting(self, exc):
        w = weather.Weather(CONFIG)
        calls = []

        def boom():
            calls.append(1)
            raise exc

        w._fetch = boom
        return w, calls

    def test_a_failure_is_not_refetched_on_every_page_load(self):
        w, calls = self._counting(urllib.error.URLError("no route to host"))
        first = w.snapshot(now=1000.0)
        second = w.snapshot(now=1000.0 + 30)
        self.assertFalse(first["ok"])
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)

    def test_but_it_is_forgotten_sooner_than_a_forecast_is(self):
        w, calls = self._counting(urllib.error.URLError("down"))
        w.snapshot(now=1000.0)
        self.assertLessEqual(w.failure_seconds, 300.0)
        self.assertLessEqual(w.failure_seconds, w.ttl)
        w.snapshot(now=1000.0 + w.failure_seconds + 1)
        self.assertEqual(len(calls), 2)

    def test_a_success_clears_it(self):
        w = fake(FIXTURE)
        original = w._fetch
        calls = []

        def boom():
            calls.append(1)
            raise urllib.error.URLError("down")

        w._fetch = boom
        self.assertFalse(w.snapshot(now=1000.0)["ok"])
        w._fetch = original
        self.assertTrue(w.snapshot(now=1000.0 + 400)["ok"])
        # And a later failure inside the success TTL is not even reached.
        w._fetch = boom
        self.assertTrue(w.snapshot(now=1000.0 + 500)["ok"])
        self.assertEqual(len(calls), 1)

    def test_a_stale_forecast_is_still_served_while_the_failure_is_remembered(self):
        w = fake(FIXTURE)
        w.snapshot(now=1000.0)
        calls = []

        def boom():
            calls.append(1)
            raise urllib.error.URLError("down")

        w._fetch = boom
        first = w.snapshot(now=1000.0 + 2 * 3600)
        second = w.snapshot(now=1000.0 + 2 * 3600 + 10)
        self.assertTrue(first["stale"])
        self.assertTrue(second["stale"])
        self.assertEqual(len(calls), 1)

    def test_a_clock_that_steps_backwards_does_not_freeze_the_cache(self):
        w = fake(FIXTURE)
        calls = []
        original = w._fetch
        w._fetch = lambda: (calls.append(1), original())[1]
        w.snapshot(now=10000.0)
        w.snapshot(now=10000.0 - 3600)
        self.assertEqual(len(calls), 2,
                         "a negative age read as very fresh and froze the cache")


class TheCachedSnapshotTheStatusHeaderReads(unittest.TestCase):
    """/api/status must not report a day-old forecast as the weather now.

    The status header used to read `_cached` straight off the object. That
    attribute only ever holds the last *successful* fetch, while the staleness
    -- the `stale` flag and the note saying when the numbers are from -- lives
    on the failure copy that snapshot() builds. So with SMHI down for a day,
    /api/weather said ok: false and the header on the same page cheerfully
    reported yesterday's temperature as today's.

    cached_snapshot() answers with what /api/weather would answer, and never
    fetches: the status endpoint is polled every 30 s by the page and must not
    be able to wait out an SMHI timeout.
    """

    def test_nothing_fetched_yet_is_none_and_not_a_number(self):
        self.assertIsNone(fake(FIXTURE).cached_snapshot(now=1000.0))

    def test_a_fresh_forecast_comes_back_fresh(self):
        w = fake(FIXTURE)
        w.snapshot(now=1000.0)
        snap = w.cached_snapshot(now=1000.0 + 60)
        self.assertTrue(snap["ok"])
        self.assertFalse(snap.get("stale"))

    def test_a_day_old_forecast_is_marked_stale_not_served_as_now(self):
        w = fake(FIXTURE)
        w.snapshot(now=1000.0)
        snap = w.cached_snapshot(now=1000.0 + 24 * 3600)
        self.assertTrue(snap["stale"], "the header would have shown this as now")
        self.assertTrue(snap["note"].strip())
        self.assertIn("uppdaterats", snap["note"])

    def test_a_remembered_failure_is_what_comes_back_while_it_lasts(self):
        w = broken(urllib.error.URLError("no route to host"))
        w.snapshot(now=1000.0)
        snap = w.cached_snapshot(now=1000.0 + 10)
        self.assertFalse(snap["ok"])
        self.assertTrue(snap["error"].strip())

    def test_it_never_fetches(self):
        w = fake(FIXTURE)
        w.snapshot(now=1000.0)
        calls = []
        w._fetch = lambda: (calls.append(1), (FIXTURE, 0.0))[1]
        for at in (1000.0 + 60, 1000.0 + 24 * 3600, 1000.0 + 400 * 3600):
            w.cached_snapshot(now=at)
        self.assertEqual(calls, [], "the status header went to the network")

    def test_it_is_a_copy(self):
        w = fake(FIXTURE)
        w.snapshot(now=1000.0)
        snap = w.cached_snapshot(now=1000.0 + 60)
        snap["now"]["t"] = -999
        self.assertNotEqual(w.cached_snapshot(now=1000.0 + 60)["now"]["t"], -999)


class AnUnparseableAnswerIsRememberedToo(unittest.TestCase):
    """The one failure that used to re-ask SMHI on every page load.

    A payload this app cannot parse is the most repeatable failure of the lot:
    the next fetch gets the same bytes. Both of the _build failures returned
    their error without going through _remember_failure, so they were asked
    again on every /api/weather -- which is how a rate limit becomes a block.
    """

    def _counting(self, payload):
        w = weather.Weather(CONFIG)
        calls = []
        w._fetch = lambda: (calls.append(1), (payload, 0.0))[1]
        return w, calls

    def test_an_unparseable_payload_is_fetched_once(self):
        w, calls = self._counting({"timeSeries": "nej"})
        first = w.snapshot(now=1000.0)
        second = w.snapshot(now=1000.0 + 30)
        self.assertFalse(first["ok"])
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)

    def test_a_forecast_with_no_hours_in_it_is_fetched_once(self):
        w, calls = self._counting({"referenceTime": "x", "timeSeries": []})
        self.assertFalse(w.snapshot(now=1000.0)["ok"])
        w.snapshot(now=1000.0 + 30)
        self.assertEqual(len(calls), 1)

    def test_and_it_is_forgotten_again_like_any_other_failure(self):
        w, calls = self._counting({"timeSeries": "nej"})
        w.snapshot(now=1000.0)
        w.snapshot(now=1000.0 + w.failure_seconds + 1)
        self.assertEqual(len(calls), 2)


class TheUserAgentSaysTheRealVersion(unittest.TestCase):
    """SMHI asks for a contactable User-Agent, and it was three versions old.

    The package said 0.2.1, the wheel 0.3.0 and the git tag v0.3.1. There is
    one string now, in nibelokal/__init__.py, and this is the line that leaves
    the house with it.
    """

    def test_it_carries_the_package_version(self):
        import nibelokal
        self.assertIn(nibelokal.__version__, weather.USER_AGENT)
        self.assertNotIn("0.2.1", weather.USER_AGENT)


if __name__ == "__main__":
    unittest.main()
