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
        self.assertIs(first, second)
        self.assertEqual(len(calls), 1)

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


if __name__ == "__main__":
    unittest.main()
