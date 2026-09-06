"""Tests for the wiring between the app and the five optional integrations.

The modules themselves are tested elsewhere. What is tested here is everything
between them and the running app, which is where the failures are boring and
expensive:

  * a config key that is in the example file but not in DEFAULTS -- config.load
    prints "ignoring unknown key" and the setting silently does nothing;
  * a new endpoint that answers 502 when the service is merely unconfigured,
    which puts a red banner over a page whose actual job is working;
  * a new endpoint that forgets the auth token or the same-origin check;
  * an integration that throws inside the poll loop and takes the pump history
    down with it;
  * a history join that produces something autotune cannot read.

No network. Nothing here is configured, so no provider has anywhere to call,
and the one server that is started binds to 127.0.0.1 on a port the kernel
picks.
"""
import contextlib
import io
import json
import logging
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nibelokal import alarms, autotune, config, server, spot     # noqa: E402
from nibelokal.pump import DASHBOARD                             # noqa: E402
from nibelokal.store import (R_DEGREE_MINUTES, R_OUTDOOR,        # noqa: E402
                             R_PRIORITY, R_SUPPLY, Poller, Store)

# Every key this wiring added, with the type it must come out of config.load as
# and the default it must have when nobody has said anything.
NEW_KEYS = {
    "homey_host": (str, ""),
    "homey_token": (str, ""),
    "homey_devices": (str, ""),
    "homey_max_age_minutes": (float, 60.0),
    "homey_timeout": (float, 5.0),
    "homey_cache_seconds": (float, 120.0),
    "pushover_token": (str, ""),
    "pushover_user": (str, ""),
    "alarm_notify": (bool, True),
    "alarm_min_severity": (str, "warning"),
    "alarm_emergency_priority": (bool, True),
    "alarm_retry_seconds": (int, 300),
    "alarm_expire_seconds": (int, 10800),
    "alarm_debounce_seconds": (int, 900),
    "weather_lat": (str, ""),
    "weather_lon": (str, ""),
    "weather_ttl_minutes": (float, 60.0),
    "weather_timeout": (float, 8.0),
    "tibber_token": (str, ""),
    "tibber_home_id": (str, ""),
    "spot_timeout": (float, 10.0),
    "spot_cache_seconds": (float, 900.0),
    "spot_min_spread": (float, 0.15),
    "spot_cheap_rank": (float, 0.25),
    "spot_expensive_rank": (float, 0.75),
    "spot_max_offset": (int, 1),
    "spot_max_pairs": (int, 4),
    "autotune_target_indoor": (str, ""),
    "autotune_days": (int, 30),
}

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def setUpModule():
    # Several tests here break something on purpose and the code under test is
    # supposed to log a warning about it. The warning is the correct behaviour,
    # not the result, and letting it through drowns the test output.
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


class FakeRegister:
    def __init__(self, address):
        self.address = address
        self.title = "register %d" % address
        self.unit = "°C"


class FakeRegistry:
    source = "fake"

    def get(self, address):
        return FakeRegister(address)

    def search(self, needle, writable=False):
        return []


class FakePump:
    """A pump that answers plausibly and never touches a socket."""

    host = "192.0.2.10"
    port = 502

    def __init__(self, values=None, fail=False):
        self.registry = FakeRegistry()
        self.fail = fail
        self.values = values or {30002: 3.4, 30006: 32.0, 31976: 0,
                                 40012: -120, 40027: 5, 40031: 0}
        self.reads = 0

    def read_many(self, addresses):
        self.reads += 1
        if self.fail:
            raise OSError("pumpen svarar inte")
        return {a: {"value": self.values[a]} for a in addresses if a in self.values}


def _cfg(**overrides):
    cfg = dict(config.DEFAULTS)
    cfg.update({"host": "192.0.2.10", "model": "S735"})
    cfg.update(overrides)
    return cfg


class ConfigDefaults(unittest.TestCase):
    def test_every_new_key_is_known_to_config(self):
        for key in NEW_KEYS:
            self.assertIn(key, config.DEFAULTS,
                          "%s is not in DEFAULTS, so config.load() would drop "
                          "it with a warning" % key)

    def test_defaults_have_the_documented_values(self):
        for key, (_, default) in NEW_KEYS.items():
            self.assertEqual(config.DEFAULTS[key], default, key)

    def test_types_after_a_round_trip_through_a_config_file(self):
        lines = ["host: 192.0.2.10", "model: S735",
                 "homey_host: 192.168.1.20", "homey_token: abc123",
                 "homey_devices: Sovrum, Kontor",
                 "homey_max_age_minutes: 45", "homey_timeout: 3",
                 "homey_cache_seconds: 90",
                 "pushover_token: ptok", "pushover_user: puser",
                 "alarm_notify: false", "alarm_min_severity: alarm",
                 "alarm_emergency_priority: false",
                 "alarm_retry_seconds: 60", "alarm_expire_seconds: 600",
                 "alarm_debounce_seconds: 120",
                 "weather_lat: 59.3293", "weather_lon: 18.0686",
                 "weather_ttl_minutes: 30", "weather_timeout: 4",
                 "tibber_token: ttok", "tibber_home_id: home-1",
                 "spot_timeout: 7", "spot_cache_seconds: 600",
                 "spot_min_spread: 0.2", "spot_cheap_rank: 0.2",
                 "spot_expensive_rank: 0.8", "spot_max_offset: 1",
                 "spot_max_pairs: 3",
                 "autotune_target_indoor: 21.5", "autotune_days: 14"]
        cfg = self._load("\n".join(lines))
        for key, (kind, _) in NEW_KEYS.items():
            if kind is str:
                continue
            self.assertIsInstance(cfg[key], kind, key)
        self.assertEqual(cfg["homey_max_age_minutes"], 45.0)
        self.assertEqual(cfg["alarm_retry_seconds"], 60)
        self.assertIs(cfg["alarm_notify"], False)
        self.assertIs(cfg["alarm_emergency_priority"], False)
        self.assertEqual(cfg["weather_lat"], 59.3293)
        self.assertEqual(cfg["autotune_target_indoor"], 21.5)
        self.assertEqual(cfg["spot_max_pairs"], 3)

    def test_the_int_float_bool_sets_agree_with_the_defaults(self):
        for key in config.INT_KEYS:
            self.assertNotIsInstance(config.DEFAULTS[key], bool, key)
        for key in config.BOOL_KEYS:
            self.assertIsInstance(config.DEFAULTS[key], bool, key)
        # Nothing may sit in two of the sets at once: _coerce checks bool
        # first, so a key in BOOL_KEYS and INT_KEYS would silently be a bool.
        self.assertFalse(config.INT_KEYS & config.FLOAT_KEYS)
        self.assertFalse(config.INT_KEYS & config.BOOL_KEYS)
        self.assertFalse(config.FLOAT_KEYS & config.BOOL_KEYS)

    def test_environment_overrides_reach_the_new_keys(self):
        # The token keys in particular: they are the ones people set from the
        # environment rather than writing into a file that gets committed.
        env = {
            "NIBE_HOMEY_TOKEN": "from-env",
            "NIBE_TIBBER_TOKEN": "tibber-from-env",
            "NIBE_PUSHOVER_TOKEN": "push-from-env",
            "NIBE_PUSHOVER_USER": "user-from-env",
            "NIBE_WEATHER_LAT": "58.5",
            "NIBE_AUTOTUNE_DAYS": "7",
            "NIBE_ALARM_NOTIFY": "false",
        }
        cfg = self._load("host: 192.0.2.10\nmodel: S735\n", env=env)
        self.assertEqual(cfg["homey_token"], "from-env")
        self.assertEqual(cfg["tibber_token"], "tibber-from-env")
        self.assertEqual(cfg["pushover_token"], "push-from-env")
        self.assertEqual(cfg["pushover_user"], "user-from-env")
        self.assertEqual(cfg["weather_lat"], 58.5)
        self.assertEqual(cfg["autotune_days"], 7)
        self.assertIs(cfg["alarm_notify"], False)

    def test_a_typo_in_an_optional_key_does_not_stop_the_app(self):
        # An unparseable latitude is a broken forecast, not a broken heat pump.
        cfg = self._load("host: 192.0.2.10\nmodel: S735\nweather_lat: norrut\n")
        self.assertEqual(cfg["weather_lat"], "")
        self.assertIn("weather_lat", self.printed)
        self.assertEqual(cfg["host"], "192.0.2.10")

    def test_the_modules_own_defaults_have_not_drifted(self):
        # spot.py and alarms.py each declare what they read. If one of them
        # changes a default and DEFAULTS is not updated, the app quietly runs
        # on a different number than the module documents.
        for key, value in spot.CONFIG_KEYS.items():
            self.assertIn(key, config.DEFAULTS, key)
            self.assertEqual(config.DEFAULTS[key], value, key)
        for key, value in alarms.CONFIG_DEFAULTS.items():
            self.assertIn(key, config.DEFAULTS, key)
            self.assertEqual(config.DEFAULTS[key], value, key)

    def test_the_example_file_only_documents_keys_that_exist(self):
        path = os.path.join(ROOT, "config.example.yaml")
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
        for key in NEW_KEYS:
            self.assertIn(key, body, "%s is undocumented in config.example.yaml" % key)
        # And a fresh clone still starts: every optional key stays commented
        # out, so loading the example as a config file changes nothing.
        cfg = self._load(body + "\nhost: 192.0.2.10\nmodel: S735\n")
        # The failure this guards against: a key documented in the example file
        # but missing from DEFAULTS is dropped with a warning, and the setting
        # the owner wrote does nothing at all.
        self.assertNotIn("ignoring unknown key", self.printed)
        for key, (_, default) in NEW_KEYS.items():
            self.assertEqual(cfg[key], default,
                             "%s is set in config.example.yaml; it should be "
                             "commented out so a fresh clone starts" % key)

    def _load(self, body, env=None):
        """Write a config file, load it, and keep whatever it printed."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.yaml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
            saved = {}
            for key, value in (env or {}).items():
                saved[key] = os.environ.get(key)
                os.environ[key] = value
            printed = io.StringIO()
            try:
                with contextlib.redirect_stdout(printed):
                    cfg = config.load(path)
            finally:
                self.printed = printed.getvalue()
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
            return cfg


class Registers(unittest.TestCase):
    def test_the_poller_reads_what_the_watchers_need(self):
        # alarms.py cannot see an alarm the poller never asks for.
        self.assertIn(alarms.R_ALARM, DASHBOARD)
        self.assertIn(alarms.R_CLASS1, DASHBOARD)
        # ... and autotune cannot fit a curve without these three.
        for address in (R_OUTDOOR, R_SUPPLY, R_DEGREE_MINUTES, R_PRIORITY):
            self.assertIn(address, DASHBOARD)

    def test_store_agrees_with_advisor_about_the_addresses(self):
        from nibelokal import advisor
        self.assertEqual(R_OUTDOOR, advisor.R_OUTDOOR)
        self.assertEqual(R_SUPPLY, advisor.R_SUPPLY)
        self.assertEqual(R_DEGREE_MINUTES, advisor.R_DEGREE_MINUTES)
        self.assertEqual(R_PRIORITY, advisor.R_PRIORITY)


class Endpoints(unittest.TestCase):
    """The new endpoints, unconfigured, over a real socket.

    Unconfigured is the case that matters: it is what a fresh clone runs, and
    it is the one where the temptation to answer 500 is strongest.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store = Store(os.path.join(cls.tmp.name, "nibe.db"), history_days=400)
        cls.pump = FakePump()
        cfg = _cfg(auth_token="hemligt")
        providers = server.build_providers(cls.pump, cfg, cls.store)
        cls.providers = providers
        poller = Poller(cls.pump, cls.store, DASHBOARD, 60,
                        watcher=providers["watcher"], indoor=providers["homey"])
        server.Handler.ctx = server.build_context(
            cls.pump, cfg, cls.tmp.name, cls.store, poller, providers,
            allowed_hosts={"localhost", "127.0.0.1"})
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        cls.store.close()
        cls.tmp.cleanup()
        server.Handler.ctx = {}

    def get(self, path, token="hemligt", headers=None):
        request = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path))
        if token:
            request.add_header("X-Auth-Token", token)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8") or "{}")

    # -- the contract ----------------------------------------------------

    def test_indoor(self):
        status, body = self.get("/api/indoor")
        self.assertEqual(status, 200)
        self.assertIs(body["ok"], False)
        self.assertIn("Homey", body["error"])
        self.assertEqual(body["source"], "homey")

    def test_weather(self):
        status, body = self.get("/api/weather")
        self.assertEqual(status, 200)
        self.assertIs(body["ok"], False)
        self.assertTrue(body["error"])

    def test_spot(self):
        status, body = self.get("/api/spot")
        self.assertEqual(status, 200)
        self.assertIs(body["ok"], False)
        self.assertIn("prices", body)
        self.assertIn("plan", body)
        self.assertIs(body["prices"]["ok"], False)
        self.assertIs(body["prices"]["configured"], False)
        # The plan still answers in its own shape rather than being absent,
        # so a caller renders one empty panel instead of crashing on None.
        self.assertIs(body["plan"]["ok"], False)
        self.assertEqual(body["plan"]["hours"], [])
        self.assertTrue(body["plan"]["summary"])
        self.assertTrue(body["error"])

    def test_alarms(self):
        status, body = self.get("/api/alarms")
        self.assertEqual(status, 200)
        self.assertIs(body["ok"], True)
        self.assertEqual(body["active"], [])
        self.assertEqual(body["history"], [])
        # Watching works without credentials; only the push is off.
        self.assertIs(body["notify"], False)
        self.assertIsNone(body["last_error"])

    def test_autotune(self):
        status, body = self.get("/api/autotune")
        self.assertEqual(status, 200)
        self.assertIn(body["state"], ("insufficient", "uncertain", "confident"))
        self.assertTrue(body["reason_sv"])
        for key in ("ok", "samples", "outdoor_span", "days", "offset_error_c",
                    "slope_error_c_per_c", "proposal", "confidence",
                    "missing_sv", "target_indoor", "history_days"):
            self.assertIn(key, body)
        self.assertIs(body["ok"], False)          # no history at all yet
        self.assertEqual(body["history_days"], config.DEFAULTS["autotune_days"])

    def test_status_keeps_its_old_shape_and_gains_summaries(self):
        status, body = self.get("/api/status")
        self.assertEqual(status, 200)
        # The keys web/index.html reads by name.
        for key in ("pump", "register_map", "polled_at", "poll_error", "registers"):
            self.assertIn(key, body)
        self.assertTrue(body["registers"])
        first = body["registers"][0]
        for key in ("address", "title", "unit", "value", "error"):
            self.assertIn(key, first)

        self.assertIs(body["indoor"]["configured"], False)
        self.assertIs(body["weather"]["configured"], False)
        self.assertEqual(body["alarm"]["active"], 0)
        self.assertIs(body["alarm"]["ok"], True)
        self.assertEqual(
            set(body["features"]),
            {"indoor", "weather", "spot", "alarms", "notify", "autotune"})
        self.assertIs(body["features"]["indoor"], False)
        self.assertIs(body["features"]["weather"], False)
        self.assertIs(body["features"]["spot"], False)
        self.assertIs(body["features"]["notify"], False)
        self.assertIs(body["features"]["autotune"], False)
        # Alarm watching is the one that is on without being configured.
        self.assertIs(body["features"]["alarms"], True)

    def test_a_dead_pump_does_not_turn_the_new_endpoints_into_errors(self):
        self.pump.fail = True
        try:
            for path in ("/api/indoor", "/api/weather", "/api/spot",
                         "/api/alarms", "/api/autotune"):
                status, body = self.get(path)
                self.assertEqual(status, 200, path)
                self.assertIn("ok", body, path)
        finally:
            self.pump.fail = False

    # -- the checks the existing endpoints make --------------------------

    def test_every_new_endpoint_requires_the_token(self):
        for path in ("/api/indoor", "/api/weather", "/api/spot",
                     "/api/alarms", "/api/autotune", "/api/status"):
            status, body = self.get(path, token=None)
            self.assertEqual(status, 401, path)
            self.assertEqual(body["error"], "unauthorised")

    def test_every_new_endpoint_refuses_a_foreign_origin(self):
        for path in ("/api/indoor", "/api/weather", "/api/spot",
                     "/api/alarms", "/api/autotune"):
            status, _ = self.get(path, headers={"Origin": "http://evil.example"})
            self.assertEqual(status, 403, path)

    def test_the_token_also_works_as_a_query_parameter(self):
        # Same as the existing endpoints: the phone gets the link once.
        status, body = self.get("/api/alarms?token=hemligt", token=None)
        self.assertEqual(status, 200)
        self.assertIs(body["ok"], True)


class ProvidersThatWillNotBuild(unittest.TestCase):
    def test_an_endpoint_whose_provider_is_missing_still_answers_200(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "nibe.db"))
            pump = FakePump()
            providers = {"homey": None, "weather": None, "tibber": None,
                         "plan": None, "watcher": None,
                         "errors": {"homey": "diskfel"}}
            ctx = server.build_context(pump, _cfg(), tmp, store, None, providers)
            handler = _handler(ctx)
            for path in ("/api/indoor", "/api/weather", "/api/spot", "/api/alarms"):
                body, status = handler.call(path)
                self.assertEqual(status, 200, path)
                self.assertIs(body["ok"], False, path)
                self.assertIn("config.yaml", body["error"], path)
            store.close()


class SummariesWhenConfigured(unittest.TestCase):
    """The other half of /api/status: what it says once things are set up.

    Handlers are called directly here rather than over a socket, because the
    point is the summary the page reads and every provider is a fake with a
    canned answer -- there is nothing for a socket to add.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "nibe.db"))
        self.pump = FakePump()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _ctx(self, **providers):
        base = {"homey": None, "weather": None, "tibber": None, "plan": None,
                "watcher": None, "errors": {}}
        base.update(providers)
        poller = Poller(self.pump, self.store, DASHBOARD, 60)
        return server.build_context(self.pump, _cfg(autotune_target_indoor=21.0),
                                    self.tmp.name, self.store, poller, base)

    def test_status_summarises_a_working_indoor_feed(self):
        class Homey:
            configured = True

            def snapshot(self):
                return {"ok": True, "at": 1788700000, "average": 21.3,
                        "by_zone": {"Sovrum": 20.9, "Vardagsrum": 21.7},
                        "sensors": [{"name": "Sovrum", "value": 20.9, "stale": False},
                                    {"name": "Garage", "value": 12.0, "stale": True}],
                        "source": "homey"}

        body, status = _handler(self._ctx(homey=Homey())).call("/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(body["indoor"], {"ok": True, "configured": True,
                                          "average": 21.3, "at": 1788700000,
                                          "sensors": 2, "stale": 1, "error": None})
        self.assertIs(body["features"]["indoor"], True)
        # Both halves are needed before the curve can be tuned: somewhere for
        # the indoor temperature to come from, and a target to compare it with.
        self.assertIs(body["features"]["autotune"], True)

    def test_status_summarises_the_forecast_already_fetched(self):
        class Weather:
            configured = True
            # What /api/weather left behind. Status never fetches: SMHI is on
            # the internet and a cold cache would make the polled endpoint wait
            # out the timeout.
            _cached = {"ok": True, "at": 1788700000,
                       "now": {"t": -4.2, "effective": -9.1, "symbol_sv": "snöfall"},
                       "summary": {"min_24h": -11.0, "trend": "faller"}}

        body, _ = _handler(self._ctx(weather=Weather())).call("/api/status")
        self.assertEqual(body["weather"]["t"], -4.2)
        self.assertEqual(body["weather"]["effective"], -9.1)
        self.assertEqual(body["weather"]["symbol_sv"], "snöfall")
        self.assertEqual(body["weather"]["min_24h"], -11.0)
        self.assertEqual(body["weather"]["trend"], "faller")
        self.assertIs(body["features"]["weather"], True)

    def test_status_says_so_before_the_forecast_has_been_fetched(self):
        class Weather:
            configured = True
            _cached = None

        body, _ = _handler(self._ctx(weather=Weather())).call("/api/status")
        self.assertIs(body["weather"]["configured"], True)
        self.assertIs(body["weather"]["ok"], False)
        self.assertTrue(body["weather"]["error"])

    def test_status_summarises_a_standing_alarm(self):
        class Watcher:
            notifier = object()
            last_error = None

            def active(self):
                return [{"code": 163, "text": "Kompressorfel", "severity": "alarm",
                         "first_seen": 1788600000, "last_seen": 1788700000},
                        {"code": 42, "text": "Givarfel", "severity": "warning"}]

        body, _ = _handler(self._ctx(watcher=Watcher())).call("/api/status")
        self.assertEqual(body["alarm"]["active"], 2)
        self.assertEqual(body["alarm"]["code"], 163)
        self.assertEqual(body["alarm"]["severity"], "alarm")
        self.assertIs(body["alarm"]["notify"], True)
        self.assertIs(body["features"]["notify"], True)

    def test_spot_hands_the_prices_it_fetched_to_the_plan(self):
        # One fetch, one plan: two calls a second apart can straddle the moment
        # tomorrow's prices land, and a plan that disagrees with the table
        # printed beside it is worse than no plan at all.
        day = "2026-01-15"
        prices = []
        for hour in range(8):
            total = [0.30, 0.28, 0.31, 0.35, 1.60, 1.80, 1.75, 0.90][hour]
            prices.append({"starts_at": "%sT%02d:00:00+01:00" % (day, hour),
                           "day": day, "level": "NORMAL", "total": total,
                           "rank": hour / 7.0,
                           "band": ("billig" if total < 0.4 else
                                    "dyr" if total > 1.5 else "normal")})
        snapshot = {"ok": True, "configured": True, "at": time.time(),
                    "currency": "SEK", "now": None, "hours": prices,
                    "stats": {"min": 0.28, "max": 1.80, "spread": 1.52,
                              "flat": False, "mean": 0.9, "median": 0.6},
                    "horizon_hours": 8, "has_tomorrow": False}

        class Tibber:
            configured = True
            calls = 0

            def snapshot(self):
                Tibber.calls += 1
                return snapshot

        ctx = self._ctx(tibber=Tibber(), plan=spot.Plan(_cfg()))
        body, status = _handler(ctx).call("/api/spot")
        self.assertEqual(status, 200)
        self.assertEqual(Tibber.calls, 1)
        self.assertIs(body["ok"], True)
        self.assertNotIn("error", body)
        self.assertEqual(body["prices"]["hours"], prices)
        self.assertIs(body["plan"]["ok"], True)
        self.assertEqual(body["plan"]["register"], spot.R_OFFSET)
        moved = [h for h in body["plan"]["hours"] if h["offset_delta"]]
        self.assertTrue(moved)
        # The property the whole module rests on: the day nets to zero.
        self.assertEqual(sum(h["offset_delta"] for h in body["plan"]["hours"]), 0)


class PollLoop(unittest.TestCase):
    """The poll loop is the one thread that must not die."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "nibe.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _run_once(self, **kwargs):
        pump = FakePump()
        poller = Poller(pump, self.store, [30002, 30006], seconds=15, **kwargs)
        poller.start()
        deadline = time.time() + 10
        while time.time() < deadline and poller.last_ok is None:
            time.sleep(0.01)
        # The first pass is done; the thread is now waiting out its interval.
        alive = poller.is_alive()
        poller.stop()
        poller.join(timeout=5)
        return pump, poller, alive

    def test_a_watcher_that_raises_does_not_stop_the_polling(self):
        class Boom:
            calls = 0

            def poll(self, values):
                Boom.calls += 1
                raise RuntimeError("larmvakten sprack")

        pump, poller, alive = self._run_once(watcher=Boom())
        self.assertTrue(alive, "the poller died with the watcher")
        self.assertGreaterEqual(Boom.calls, 1)
        self.assertIn("larmvakten sprack", poller.last_watch_error)
        # The pump was still read and the reading was still stored.
        self.assertIsNotNone(poller.last_ok)
        self.assertIsNone(poller.last_error)
        self.assertGreater(self.store.stats()["rows"], 0)

    def test_an_indoor_source_that_raises_does_not_stop_the_polling(self):
        class Boom:
            configured = True

            def snapshot(self):
                raise RuntimeError("homey sprack")

        pump, poller, alive = self._run_once(indoor=Boom())
        self.assertTrue(alive)
        self.assertIn("homey sprack", poller.last_indoor_error)
        self.assertGreater(self.store.stats()["rows"], 0)

    def test_a_fresh_indoor_reading_is_stored_and_a_stale_one_is_not(self):
        class Source:
            configured = True

            def __init__(self, average):
                self.average = average

            def snapshot(self):
                return {"ok": True, "at": int(time.time()), "average": self.average,
                        "sensors": [], "source": "homey"}

        self._run_once(indoor=Source(20.7))
        rows = self.store.autotune_history(days=1)
        self.assertTrue(any(r["indoor"] == 20.7 for r in rows))

        # An average of None means every sensor was stale. Repeating the last
        # good number would read as hours of a very steady house.
        before = self._indoor_rows()
        self._run_once(indoor=Source(None))
        self.assertEqual(self._indoor_rows(), before)

    def test_the_watcher_still_gets_a_turn_when_the_pump_is_unreachable(self):
        # That is when a pending notification most needs retrying.
        seen = []

        class Recorder:
            def poll(self, values):
                seen.append(values)

        pump = FakePump(fail=True)
        poller = Poller(pump, self.store, [30002], seconds=15, watcher=Recorder())
        poller.start()
        deadline = time.time() + 10
        while time.time() < deadline and not seen:
            time.sleep(0.01)
        poller.stop()
        poller.join(timeout=5)
        self.assertEqual(seen[0], {},
                         "a failed poll must not be replayed as a reading")
        self.assertIsNotNone(poller.last_error)

    def test_an_alarm_reaches_the_endpoint_through_the_real_loop(self):
        # The whole chain in one test: the register is in DASHBOARD, the poller
        # hands the reading to the watcher, the watcher writes it, and
        # /api/alarms reads it back. Each link is fine on its own and the app
        # is still silent about a broken pump if any one of them is missing.
        pump = FakePump(values={alarms.R_ALARM: 163, 30002: -5.0})
        watcher = alarms.Watcher(self.store, pump, _cfg())
        poller = Poller(pump, self.store, DASHBOARD, seconds=15, watcher=watcher)
        poller.start()
        deadline = time.time() + 10
        while time.time() < deadline and not watcher.active():
            time.sleep(0.01)
        poller.stop()
        poller.join(timeout=5)

        ctx = server.build_context(pump, _cfg(), self.tmp.name, self.store, poller,
                                   {"watcher": watcher})
        body, status = _handler(ctx).call("/api/alarms")
        self.assertEqual(status, 200)
        self.assertEqual([a["code"] for a in body["active"]], [163])
        self.assertEqual(body["history"][0]["kind"], "alarm")
        # Nothing was sent anywhere: no credentials are configured.
        self.assertIs(body["notify"], False)

        summary, _ = _handler(ctx).call("/api/status")
        self.assertEqual(summary["alarm"]["code"], 163)
        self.assertEqual(summary["alarm"]["active"], 1)

    def test_pruning_covers_the_indoor_table(self):
        store = Store(os.path.join(self.tmp.name, "prune.db"), history_days=1)
        old = int(time.time()) - 10 * 86400
        store.record({30002: {"value": 1.0}}, ts=old)
        store.record_indoor(21.0, ts=old)
        store.record_indoor(21.0)
        store.prune()
        with store._lock, store._db as c:                       # noqa: SLF001
            left = c.execute("SELECT COUNT(*) FROM indoor").fetchone()[0]
        self.assertEqual(left, 1)
        store.close()

    def _indoor_rows(self):
        with self.store._lock, self.store._db as c:             # noqa: SLF001
            return c.execute("SELECT COUNT(*) FROM indoor").fetchone()[0]


class HistoryForAutotune(unittest.TestCase):
    """The join, and the only test that matters for it: autotune accepts it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "nibe.db"))
        self._fill(days=14)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _fill(self, days):
        """Fourteen days of hourly readings, cold and getting colder."""
        now = int(time.time())
        for hour in range(days * 24):
            ts = now - hour * 3600
            day = hour // 24
            outdoor = 2.0 - day * 1.2          # spans about 16 degrees
            self.store.record({
                R_OUTDOOR: {"value": outdoor},
                R_SUPPLY: {"value": 34.0 - outdoor},
                R_DEGREE_MINUTES: {"value": -180.0},
                R_PRIORITY: {"value": "Värme"},
            }, ts=ts)
            self.store.record_indoor(20.4, ts=ts + 30)

    def test_the_rows_have_exactly_the_field_names_autotune_reads(self):
        rows = self.store.autotune_history(days=20)
        self.assertTrue(rows)
        for row in rows[:5]:
            self.assertEqual(set(row), {"ts", "outdoor", "indoor", "supply",
                                        "degree_minutes", "compressor"})
        self.assertEqual(rows, sorted(rows, key=lambda r: r["ts"]))
        # Bucketed to the hour: the indoor sample was written half a minute
        # after the pump reading and still lands on the same row.
        joined = [r for r in rows if r["indoor"] is not None]
        self.assertTrue(joined)
        self.assertEqual(joined[0]["indoor"], 20.4)
        self.assertEqual(joined[0]["compressor"], "Värme")
        self.assertEqual(joined[0]["degree_minutes"], -180.0)

    def test_analyse_accepts_the_join_without_complaining(self):
        rows = self.store.autotune_history(days=20)
        result = autotune.analyse(rows, 21.0, {"offset": 0, "curve": 5,
                                               "own_curve": None,
                                               "min_supply": 20, "max_supply": 60})
        # The catch-all in analyse() says this; if it appears, the shape is wrong.
        self.assertNotIn("Kunde inte analysera historiken", result["reason_sv"])
        self.assertNotIn("unreadable", result["rejected"])
        self.assertNotIn("no_indoor", result["rejected"])
        self.assertGreater(result["samples"], 0)
        self.assertGreaterEqual(result["nights"], 3)
        self.assertIn(result["state"], ("insufficient", "uncertain", "confident"))

    def test_hours_without_an_indoor_reading_are_still_reported(self):
        # Homey unconfigured: autotune should be able to say that the pump data
        # is fine and the room data is missing, which is a different problem
        # from having no history at all.
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "bare.db"))
            now = int(time.time())
            for hour in range(48):
                store.record({R_OUTDOOR: {"value": -3.0},
                              R_DEGREE_MINUTES: {"value": -200.0}},
                             ts=now - hour * 3600)
            rows = store.autotune_history(days=5)
            self.assertTrue(rows)
            self.assertTrue(all(r["indoor"] is None for r in rows))
            result = autotune.analyse(rows, 21.0)
            self.assertEqual(result["state"], "insufficient")
            self.assertTrue(result["rejected"].get("no_indoor"))
            self.assertTrue(any("Inomhustemperatur" in m
                                for m in result["missing_sv"]))
            store.close()

    def test_an_empty_database_is_an_empty_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "empty.db"))
            self.assertEqual(store.autotune_history(days=30), [])
            store.close()


class _handler:
    """Call a GET route without a socket, for the cases a server cannot reach."""

    def __init__(self, ctx):
        self.ctx = ctx

    def call(self, path):
        from urllib.parse import urlparse

        handler = server.Handler.__new__(server.Handler)
        handler.ctx = self.ctx
        captured = {}

        def capture(obj, status=200):
            captured["body"], captured["status"] = obj, status

        handler._json = capture                                 # noqa: SLF001
        handler._api_get(urlparse(path))
        return captured["body"], captured["status"]


if __name__ == "__main__":
    unittest.main()


class WriteAllResponseShape(unittest.TestCase):
    """A multi-register write must answer the shape the browser reads.

    Regression: /api/write wrapped write_all's whole return value in another
    "changes" key. The pump was written, so the house changed, but the page
    threw "(r.changes || []).map is not a function" and told the owner it had
    failed; nothing reached the write log, because _log_writes iterated the
    dict's keys; and "partial" was one level too deep to ever be seen, so a
    rolled-back half of a pair would have been reported as success.
    """

    def _reply(self, write_all_result):
        import nibelokal.server as server

        logged = []

        class FakeStore:
            def record_write(self, w, source="web"):
                logged.append(w)

        result = dict(write_all_result)
        result["logged"] = server._log_writes(FakeStore(), result["changes"])
        return result, logged

    def test_changes_is_the_list_itself(self):
        done = [{"address": 40031, "title": "Heating offset", "before": 0,
                 "after": -1, "requested": -1, "unit": "", "tier": "guarded",
                 "verified": True}]
        reply, logged = self._reply({"changes": done, "partial": False})
        self.assertIsInstance(reply["changes"], list)
        self.assertEqual(reply["changes"][0]["after"], -1)
        self.assertFalse(reply["partial"])
        self.assertTrue(reply["logged"])
        self.assertEqual(logged, done, "the write records, not the dict's keys")

    def test_partial_survives_to_the_top_level(self):
        reply, _ = self._reply({"changes": [], "partial": True,
                                "rolled_back": ["Värmekurva"],
                                "rollback_failed": [],
                                "error": "Värmeoffset: timeout"})
        self.assertTrue(reply["partial"])
        self.assertIn("rolled_back", reply)
