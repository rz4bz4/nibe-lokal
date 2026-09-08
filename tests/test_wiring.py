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
import builtins
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
from nibelokal.profile import Profile                            # noqa: E402
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

    def __init__(self, values=None, fail=False, profile=None):
        self.registry = FakeRegistry()
        # An S-series profile is the identity, so this fake behaves exactly as
        # it did before the profile existed. See nibelokal/profile.py.
        self.profile = profile if profile is not None else Profile("S", "S735")
        self.fail = fail
        self.values = values or {30002: 3.4, 30006: 32.0, 31976: 0,
                                 40012: -120, 40027: 5, 40031: 0}
        self.reads = 0
        # Registers this pump is currently refusing, as pump.missing() reports
        # them to /api/status.
        self.missing_list: list[dict] = []

    def register(self, address):
        """Pump.register: the register a canonical address resolves to here."""
        if not self.profile.available(address):
            return None
        return self.registry.get(self.profile.physical(address))

    def read_many(self, addresses):
        self.reads += 1
        if self.fail:
            raise OSError("pumpen svarar inte")
        # A register this pump cannot address drops out, exactly as it does in
        # Pump.read_many -- which is how the alarm watcher finds out that an
        # F-series pump has no class 1 flag.
        return {a: {"value": self.values[a]} for a in addresses
                if a in self.values and self.register(a) is not None}

    def missing(self):
        return list(self.missing_list)


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

    def post(self, path, body, token="hemligt"):
        request = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json"})
        if token:
            request.add_header("X-Auth-Token", token)
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
        # Nothing has been rejected, so nothing to show the household.
        self.assertIsNone(body["notify_error"])

    def test_a_test_notification_without_credentials_says_what_to_set(self):
        status, body = self.post("/api/alarms/test", {})
        self.assertEqual(status, 200)
        self.assertIs(body["ok"], False)
        self.assertIs(body["notify"], False)
        self.assertIn("pushover_token", body["error"])

    def test_the_test_notification_endpoint_needs_the_token_too(self):
        status, _ = self.post("/api/alarms/test", {}, token=None)
        self.assertEqual(status, 401)

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


class AutotuneIsShownWhenItWorks(unittest.TestCase):
    """features.autotune and /api/autotune had different opinions.

    The flag required autotune_target_indoor; the endpoint falls back to the
    pump's own room setpoint and works without it. The UI hides the feature on
    the flag, so a working analysis was invisible to anybody who had not set a
    key config.example.yaml documents as optional. The flag now means what the
    endpoint means: there is an indoor source.
    """

    class Indoor:
        configured = True

        def snapshot(self):
            return {"ok": True, "at": 1788700000, "average": 21.3,
                    "sensors": [], "source": "homey"}

        def cached_snapshot(self):
            return self.snapshot()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "nibe.db"))
        self.pump = FakePump()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _ctx(self, **cfg):
        # 40207 is the room setpoint on the pump's own display -- the fallback
        # target the endpoint uses when nobody has configured one.
        self.pump.values[40207] = 21.0
        poller = Poller(self.pump, self.store, DASHBOARD, 60)
        return server.build_context(self.pump, _cfg(**cfg), self.tmp.name,
                                    self.store, poller,
                                    {"homey": self.Indoor()})

    def test_the_flag_is_on_without_a_target_in_the_config(self):
        body, _ = _handler(self._ctx(autotune_target_indoor="")).call("/api/status")
        self.assertIs(body["features"]["autotune"], True)

    def test_and_the_endpoint_agrees_by_using_the_pumps_own_setpoint(self):
        body, status = _handler(
            self._ctx(autotune_target_indoor="")).call("/api/autotune")
        self.assertEqual(status, 200)
        self.assertEqual(body["target_source"], "pump")
        self.assertIsNotNone(body["target_indoor"])

    def test_a_configured_target_wins_and_says_so(self):
        body, _ = _handler(
            self._ctx(autotune_target_indoor=21.5)).call("/api/autotune")
        self.assertEqual(body["target_indoor"], 21.5)
        self.assertEqual(body["target_source"], "config")

    def test_without_an_indoor_source_there_is_nothing_to_tune_against(self):
        poller = Poller(self.pump, self.store, DASHBOARD, 60)
        ctx = server.build_context(self.pump, _cfg(autotune_target_indoor=21.0),
                                   self.tmp.name, self.store, poller, {})
        body, _ = _handler(ctx).call("/api/status")
        self.assertIs(body["features"]["autotune"], False)

    def test_the_example_config_calls_the_target_optional(self):
        # The three have to agree, and this is the third.
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base, "config.example.yaml"), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        where = next(i for i, line in enumerate(lines)
                     if line.lstrip("# ").startswith("autotune_target_indoor"))
        above = " ".join(lines[max(0, where - 12):where])
        self.assertIn("Optional", above)
        self.assertIn("room setpoint", above)


class TheStatusSummaryNeverGoesToTheLan(unittest.TestCase):
    """/api/status is polled every 30 s and must not wait for anything.

    The comment used to say the indoor summary was "a dict lookup rather than a
    request to the LAN". That was true only while the cache was warm, and the
    poller refreshes it after a *successful* pump poll -- so when the pump was
    down and Homey was slow, the endpoint the page lives on stalled for two
    timeouts.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "nibe.db"))
        self.pump = FakePump()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _status(self, homey):
        poller = Poller(self.pump, self.store, DASHBOARD, 60)
        ctx = server.build_context(self.pump, _cfg(), self.tmp.name, self.store,
                                   poller, {"homey": homey})
        return _handler(ctx).call("/api/status")[0]

    def test_a_cold_cache_is_reported_rather_than_fetched(self):
        class NeverAnswers:
            configured = True
            asked = 0

            def snapshot(self):
                NeverAnswers.asked += 1
                raise AssertionError("/api/status went to the LAN")

            def cached_snapshot(self):
                return None

        body = self._status(NeverAnswers())
        self.assertEqual(NeverAnswers.asked, 0)
        self.assertIs(body["indoor"]["configured"], True)
        self.assertIs(body["indoor"]["ok"], False)
        self.assertTrue(body["indoor"]["error"])
        self.assertIs(body["features"]["indoor"], True)

    def test_a_warm_cache_is_summarised_without_asking_homey(self):
        class Cached:
            configured = True
            asked = 0

            def snapshot(self):
                Cached.asked += 1
                raise AssertionError("/api/status went to the LAN")

            def cached_snapshot(self):
                return {"ok": True, "at": 1788700000, "average": 21.3,
                        "sensors": [{"name": "Sovrum", "value": 20.9,
                                     "stale": False},
                                    {"name": "Garage", "value": 12.0,
                                     "stale": True}],
                        "source": "homey"}

        body = self._status(Cached())
        self.assertEqual(Cached.asked, 0)
        self.assertEqual(body["indoor"]["average"], 21.3)
        self.assertEqual(body["indoor"]["sensors"], 2)
        self.assertEqual(body["indoor"]["stale"], 1)

    def test_the_real_homey_class_offers_that_method(self):
        from nibelokal.homey import Homey

        self.assertTrue(hasattr(Homey(host="192.0.2.20", token="x"),
                                "cached_snapshot"))


class AlarmNotificationsThatWereRejected(unittest.TestCase):
    """A configuration the notification service refused, where a person sees it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "nibe.db"))
        self.pump = FakePump()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _ctx(self, watcher):
        poller = Poller(self.pump, self.store, DASHBOARD, 60)
        return server.build_context(self.pump, _cfg(), self.tmp.name, self.store,
                                    poller, {"watcher": watcher})

    def _watcher(self):
        class Notifier:
            name = "fake"
            sent = []

            def send(self, title, message, priority=0):
                Notifier.sent.append((title, priority))

        Notifier.sent = []
        return alarms.Watcher(self.store, self.pump, _cfg(), notifier=Notifier())

    def test_the_endpoint_carries_the_rejection_next_to_the_notify_flag(self):
        watcher = self._watcher()
        watcher.notify_error = "Pushover avvisade inloggningen (HTTP 401)."
        body, status = _handler(self._ctx(watcher)).call("/api/alarms")
        self.assertEqual(status, 200)
        self.assertIs(body["notify"], True)
        self.assertEqual(body["notify_error"], watcher.notify_error)

    def test_and_so_does_the_status_summary(self):
        watcher = self._watcher()
        watcher.notify_error = "Fel token."
        body, _ = _handler(self._ctx(watcher)).call("/api/status")
        self.assertEqual(body["alarm"]["notify_error"], "Fel token.")
        self.assertIs(body["alarm"]["notify"], True)

    def test_a_healthy_watcher_reports_no_rejection(self):
        body, _ = _handler(self._ctx(self._watcher())).call("/api/status")
        self.assertIsNone(body["alarm"]["notify_error"])

    def test_no_watcher_at_all_still_has_the_key(self):
        poller = Poller(self.pump, self.store, DASHBOARD, 60)
        ctx = server.build_context(self.pump, _cfg(), self.tmp.name, self.store,
                                   poller, {})
        body, _ = _handler(ctx).call("/api/status")
        self.assertIn("notify_error", body["alarm"])

    def test_the_test_notification_endpoint_reaches_the_watcher(self):
        watcher = self._watcher()
        handler = server.Handler.__new__(server.Handler)
        handler.ctx = self._ctx(watcher)
        captured = {}
        handler._json = lambda obj, status=200: captured.update(
            body=obj, status=status)                            # noqa: SLF001
        from urllib.parse import urlparse

        handler._api_post(urlparse("/api/alarms/test"), {})     # noqa: SLF001
        self.assertIs(captured["body"]["ok"], True)
        self.assertEqual(len(watcher.notifier.sent), 1)
        self.assertEqual(watcher.notifier.sent[0][1], 0)


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

    def test_pruning_covers_the_alarm_log(self):
        # alarms.py creates its tables in this same file, and nothing was ever
        # going to delete their rows. A working pump writes none for months; a
        # flapping sensor writes two a minute.
        store = Store(os.path.join(self.tmp.name, "alarmprune.db"), history_days=1)
        watcher = alarms.Watcher(store, FakePump(), _cfg())
        old = int(time.time()) - 10 * 86400
        with store._lock, store._db as c:                       # noqa: SLF001
            c.execute("INSERT INTO alarm_events (ts, code, kind, severity, text, "
                      "sent) VALUES (?,?,?,?,?,2)", (old, 163, "alarm", "alarm", "x"))
            c.execute("INSERT INTO alarm_events (ts, code, kind, severity, text, "
                      "sent) VALUES (?,?,?,?,?,2)",
                      (int(time.time()), 163, "clear", "info", "x"))
            c.execute("INSERT OR REPLACE INTO alarm_state (code, first_seen, "
                      "last_seen) VALUES (?,?,?)", (42, old, old))
        store.prune()
        with store._lock, store._db as c:                       # noqa: SLF001
            events = c.execute("SELECT COUNT(*) FROM alarm_events").fetchone()[0]
            state = c.execute("SELECT COUNT(*) FROM alarm_state").fetchone()[0]
        self.assertEqual(events, 1, "only the old event goes")
        self.assertEqual(state, 0)
        self.assertIsNotNone(watcher)
        store.close()

    def test_pruning_a_database_that_has_never_seen_an_alarm_still_works(self):
        # The alarm tables only exist once a Watcher has been built against the
        # file. prune() must not raise on a store that has no watcher.
        store = Store(os.path.join(self.tmp.name, "noalarms.db"), history_days=1)
        store.record({30002: {"value": 1.0}}, ts=int(time.time()) - 10 * 86400)
        self.assertEqual(store.prune(), 1)
        store.close()

    def test_a_standing_alarm_is_not_pruned_out_from_under_the_watcher(self):
        store = Store(os.path.join(self.tmp.name, "standing.db"), history_days=1)
        watcher = alarms.Watcher(store, FakePump(), _cfg())
        watcher.poll({alarms.R_ALARM: {"value": 163}})
        store.prune()
        self.assertEqual([a["code"] for a in watcher.active()], [163])
        store.close()

    def test_the_indoor_feed_keeps_running_when_the_pump_is_unreachable(self):
        # It is an independent source, and the poll loop is the only thing
        # keeping its cache warm for /api/status, which never fetches.
        seen = []

        class Source:
            configured = True

            def snapshot(self):
                seen.append(1)
                return {"ok": True, "at": int(time.time()) + len(seen),
                        "average": 20.5, "sensors": [], "source": "homey"}

        pump = FakePump(fail=True)
        poller = Poller(pump, self.store, [30002], seconds=15, indoor=Source())
        poller.start()
        deadline = time.time() + 10
        while time.time() < deadline and not seen:
            time.sleep(0.01)
        poller.stop()
        poller.join(timeout=5)
        self.assertTrue(seen, "the indoor feed stopped with the pump")
        self.assertEqual(self._indoor_rows(), len(seen))

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
        """Fourteen days of hourly readings, cold and getting colder.

        Anchored to the top of an hour, and then a minute in, so that the
        indoor sample written 30 s after each pump reading always lands in the
        same hour bucket. Anchoring on time.time() itself made this fixture
        fail for the last half-minute of every hour -- ts + 30 crossed into the
        next bucket, which has no pump reading, so the newest hour came back
        with no indoor value and autotune counted a "no_indoor".
        """
        now = int(time.time()) // 3600 * 3600 + 60
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
                                        "degree_minutes", "compressor",
                                        "compressor_states"})
        self.assertEqual(rows, sorted(rows, key=lambda r: r["ts"]))
        # Bucketed to the hour: the indoor sample was written half a minute
        # after the pump reading and still lands on the same row.
        joined = [r for r in rows if r["indoor"] is not None]
        self.assertTrue(joined)
        self.assertEqual(joined[0]["indoor"], 20.4)
        self.assertEqual(joined[0]["compressor"], "Värme")
        self.assertEqual(joined[0]["compressor_states"], ["Värme"])
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


class OneVersionString(unittest.TestCase):
    """Three copies of the version disagreed, and the oldest one left the house.

    The package said 0.2.1, the wheel 0.3.0 and the git tag v0.3.1 -- and
    weather.py builds the User-Agent SMHI sees out of the package one, so the
    only copy anybody outside could observe was the wrong one. There is one
    now, and pyproject reads it from the package.
    """

    def test_the_package_is_the_source_of_truth(self):
        import nibelokal
        self.assertEqual(nibelokal.__version__, "0.4.0")

    def test_the_wheel_does_not_carry_a_second_copy(self):
        with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as fh:
            text = fh.read()
        project = text.split("[build-system]")[0]
        for line in project.splitlines():
            self.assertFalse(line.strip().startswith("version = "),
                             "pyproject hardcodes a version again: %s" % line)
        self.assertIn('dynamic = ["version"]', text)
        self.assertIn('version = {attr = "nibelokal.__version__"}', text)

    def test_and_smhi_is_told_that_one(self):
        import nibelokal
        from nibelokal import weather as weather_module
        self.assertIn(nibelokal.__version__, weather_module.USER_AGENT)


class WhatTheStatusEndpointNowSays(unittest.TestCase):
    """Three additive fields the page may start reading, and one that changed.

    `store_error`, `missing_registers` and `weather.stale` are new keys.
    Nothing that was there before changed name or meaning; `weather.error` now
    also carries the note from a stale forecast, which it did not before --
    that is the fix for the header reporting yesterday's temperature as today's.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "nibe.db"))
        self.pump = FakePump()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _status(self, poller=None, **providers):
        base = {"homey": None, "weather": None, "tibber": None, "plan": None,
                "watcher": None, "errors": {}}
        base.update(providers)
        poller = poller or Poller(self.pump, self.store, DASHBOARD, 60)
        ctx = server.build_context(self.pump, _cfg(), self.tmp.name, self.store,
                                   poller, base)
        return _handler(ctx).call("/api/status")[0]

    def test_the_keys_the_page_already_reads_are_untouched(self):
        body = self._status()
        for key in ("registers", "polled_at", "poll_error", "register_map",
                    "indoor", "weather", "alarm", "features"):
            self.assertIn(key, body, key)

    def test_a_database_failure_is_its_own_field(self):
        poller = Poller(self.pump, self.store, DASHBOARD, 60)
        poller.last_store_error = "database or disk is full"
        body = self._status(poller=poller)
        self.assertEqual(body["store_error"], "database or disk is full")
        self.assertIsNone(body["poll_error"], "the pump was blamed for the disk")

    def test_registers_the_pump_refused_are_visible(self):
        # Invisible before: a register that dropped out of every poll left no
        # trace anywhere, and 31976 dropping out means alarm watching stopped.
        self.pump.missing_list = [{"address": 31976, "title": "Larmnummer",
                                   "since": 1.0, "retry_in": 3599.0}]
        body = self._status()
        self.assertEqual(body["missing_registers"][0]["address"], 31976)

    def test_and_it_is_an_empty_list_when_all_is_well(self):
        self.assertEqual(self._status()["missing_registers"], [])

    def test_a_stale_forecast_is_marked_stale_and_not_served_as_now(self):
        class StaleWeather:
            configured = True

            def cached_snapshot(self):
                return {"ok": True, "stale": True, "at": 1788700000,
                        "note": "Prognosen är från igår och har inte uppdaterats.",
                        "now": {"t": 3.4, "effective": 1.0, "symbol_sv": "Klart"},
                        "summary": {"min_24h": -1.0, "trend": "kallare"}}

        body = self._status(weather=StaleWeather())
        self.assertTrue(body["weather"]["stale"])
        self.assertIn("uppdaterats", body["weather"]["error"])

    def test_a_fresh_one_is_not(self):
        class FreshWeather:
            configured = True

            def cached_snapshot(self):
                return {"ok": True, "at": 1788700000, "note": None,
                        "now": {"t": 3.4, "effective": 1.0, "symbol_sv": "Klart"},
                        "summary": {"min_24h": -1.0, "trend": "kallare"}}

        body = self._status(weather=FreshWeather())
        self.assertFalse(body["weather"]["stale"])
        self.assertIsNone(body["weather"]["error"])

    def test_the_unconfigured_shape_carries_the_new_key_too(self):
        # The page reads the same shape whether or not a feature is set up.
        self.assertIn("stale", self._status()["weather"])


class TheStatusSummariesCostNothing(unittest.TestCase):
    """The indoor and alarm summaries the page does not read yet.

    /api/status carries them and the page polls /api/indoor and /api/alarms
    itself, so they are dead weight -- but only if they cost something. They
    were kept rather than removed, because removing a field the UI may start
    reading is the more expensive mistake of the two, and made cheap enough
    that carrying them does not matter: no network (asserted elsewhere), and
    no query that scans a table.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "nibe.db"))
        self.pump = FakePump()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_status_never_counts_the_rows_of_the_history(self):
        class NoScans(Store):
            def stats(self):
                raise AssertionError("/api/status triggered a full table scan")

        store = NoScans(os.path.join(self.tmp.name, "scan.db"))
        self.addCleanup(store.close)
        watcher = alarms.Watcher(store, self.pump, _cfg())
        poller = Poller(self.pump, store, DASHBOARD, 60)
        ctx = server.build_context(self.pump, _cfg(), self.tmp.name, store,
                                   poller, {"watcher": watcher})
        body = _handler(ctx).call("/api/status")[0]
        self.assertIn("alarm", body)
        self.assertIn("indoor", body)

    def test_the_alarm_summary_reads_only_the_standing_alarms(self):
        # alarm_state holds one row per standing alarm -- nothing on a healthy
        # pump -- and not the event log, which grows.
        store = Store(os.path.join(self.tmp.name, "alarm.db"))
        self.addCleanup(store.close)
        watcher = alarms.Watcher(store, self.pump, _cfg())
        for _ in range(50):
            watcher.poll({alarms.R_ALARM: {"value": 163}})
            watcher.poll({alarms.R_ALARM: {"value": 0}})
        poller = Poller(self.pump, store, DASHBOARD, 60)
        ctx = server.build_context(self.pump, _cfg(), self.tmp.name, store,
                                   poller, {"watcher": watcher})
        body = _handler(ctx).call("/api/status")[0]
        self.assertEqual(body["alarm"]["active"], 0)
        self.assertTrue(body["alarm"]["ok"])



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


class TheStatusEndpointSaysWhichGenerationThisIs(unittest.TestCase):
    """Three additive keys the page reads to decide whether to warn.

    `generation`, `model` and `verified` say which numbering the addresses in
    this answer are in and whether the translation between them and the app's
    own has ever been run against real hardware. On an S-series pump the
    translation is the identity, so `verified` is true and nothing is shown.
    On an F-series one it is a table read out of NIBE's own register maps that
    has never met a pump, and the page says so in one line.

    `physical` on a register row is the other half: the address stays the
    canonical one the web app is built around, so a cached index.html goes on
    working, and the number the owner's own documentation uses rides along.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "nibe.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _status(self, pump):
        poller = Poller(pump, self.store, DASHBOARD, 60)
        ctx = server.build_context(
            pump, _cfg(), self.tmp.name, self.store, poller,
            {"homey": None, "weather": None, "tibber": None, "plan": None,
             "watcher": None, "errors": {}})
        return _handler(ctx).call("/api/status")[0]

    def test_an_s_series_pump_reports_itself_verified(self):
        body = self._status(FakePump())
        self.assertEqual(body["generation"], "S")
        self.assertEqual(body["model"], "S735")
        self.assertIs(body["verified"], True)

    def test_no_s_series_row_carries_a_physical_address(self):
        # Because there is nothing to carry: the translation is the identity,
        # and a key that appeared here would change what the page renders on
        # the author's own pump.
        body = self._status(FakePump())
        self.assertTrue(body["registers"])
        for row in body["registers"]:
            self.assertNotIn("physical", row, row)

    def test_an_f_series_pump_reports_itself_unverified(self):
        pump = FakePump(profile=Profile.for_model("F750"))
        body = self._status(pump)
        self.assertEqual(body["generation"], "F")
        self.assertEqual(body["model"], "F750")
        self.assertIs(body["verified"], False,
                      "the F table has never been run against a real pump, and "
                      "the page has a line that only appears when this is false")

    def test_an_f_series_row_carries_both_numbers(self):
        pump = FakePump(profile=Profile.for_model("F750"))
        body = self._status(pump)
        rows = {r["address"]: r for r in body["registers"]}
        # The address stays the one the web app holds...
        self.assertIn(30002, rows)
        # ...and the pump's own number rides along beside it.
        self.assertEqual(rows[30002]["physical"], 40004)
        self.assertEqual(rows[40012]["physical"], 43005)

    def test_a_register_with_no_f_equivalent_is_simply_absent(self):
        # 31976 has one (45001) and stays; 32196, the class 1 alarm flag, has
        # none on any F map, and an absent row is how the alarm watcher is
        # meant to find that out.
        pump = FakePump(values={30002: 3.4, 31976: 0, 32196: 0},
                        profile=Profile.for_model("F750"))
        body = self._status(pump)
        addresses = {r["address"] for r in body["registers"]}
        self.assertIn(31976, addresses)
        self.assertNotIn(32196, addresses)


class TheTwoNewConfigKeys(unittest.TestCase):
    """`generation` and `framing`, and the two ways of getting them wrong."""

    def _load(self, text):
        path = os.path.join(tempfile.mkdtemp(), "config.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return config.load(path)

    def test_both_keys_are_in_defaults(self):
        self.assertEqual(config.DEFAULTS["generation"], "")
        self.assertEqual(config.DEFAULTS["framing"], "tcp")

    def test_the_defaults_are_the_s_series_behaviour(self):
        cfg = self._load("host: 192.0.2.10\nmodel: S735\n")
        self.assertEqual(cfg["generation"], "")
        self.assertEqual(cfg["framing"], "tcp")

    def test_a_csv_with_no_model_and_no_generation_is_accepted(self):
        # config.example.yaml has said "required unless register_csv is set"
        # about `model` since before the generations split, and a released
        # version accepted exactly this. The generation is derived from the map
        # itself in __main__.build(), where the map has been loaded -- see
        # profile.generation_from_addresses -- rather than refused here.
        path = os.path.join(tempfile.mkdtemp(), "registers.csv")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("Register,Title,Mode\n27,Heating curve,R/W\n")
        cfg = self._load("host: 192.0.2.10\nregister_csv: %s\n" % path)
        self.assertEqual(cfg["generation"], "")
        self.assertEqual(cfg["model"], "")

    def test_a_csv_with_a_generation_is_accepted(self):
        path = os.path.join(tempfile.mkdtemp(), "registers.csv")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("Register,Title,Mode\n27,Heating curve,R/W\n")
        cfg = self._load("host: 192.0.2.10\nregister_csv: %s\ngeneration: F\n" % path)
        self.assertEqual(cfg["generation"], "F")

    def test_a_nonsense_generation_is_refused_with_both_letters_named(self):
        with self.assertRaises(SystemExit) as caught:
            self._load("host: 192.0.2.10\nmodel: S735\ngeneration: X\n")
        message = str(caught.exception)
        self.assertIn("S735", message)
        self.assertIn("F750", message)

    def test_a_nonsense_framing_is_refused(self):
        with self.assertRaises(SystemExit) as caught:
            self._load("host: 192.0.2.10\nmodel: S735\nframing: rtuovertcp\n")
        self.assertIn("rtu", str(caught.exception))

    def test_rtu_is_accepted(self):
        cfg = self._load("host: 192.0.2.10\nmodel: F750\nframing: rtu\n")
        self.assertEqual(cfg["framing"], "rtu")


class TheDeviceIdLineInStatus(unittest.TestCase):
    """`status` asks the pump what it is, and never fails because it would not say.

    Function 0x2B is optional in the Modbus specification. It is also the
    cheapest cross-check there is that the `model:` in config.yaml is the pump
    on the other end of the wire -- and the thing an F750 owner should paste
    into an issue -- so it is asked for, printed, and forgiven.
    """

    class FakeModbus:
        def __init__(self, answer):
            self.answer = answer

        def device_id(self):
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

    class FakePump:
        def __init__(self, mb, model="F750"):
            self.mb = mb
            self.profile = Profile.for_model(model)

    def _run(self, answer, model="F750"):
        from nibelokal.__main__ import print_identity
        pump = self.FakePump(self.FakeModbus(answer), model)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            print_identity(pump)
        return out.getvalue()

    def test_it_prints_what_the_pump_said(self):
        line = self._run({"vendor": "NIBE", "product": "F750",
                          "revision": "5539", "objects": {}})
        self.assertIn("NIBE F750 5539", line)

    def test_a_pump_that_does_not_implement_it_is_not_an_error(self):
        from nibelokal.modbus import ModbusError
        line = self._run(ModbusError(1, "reading the device identification"))
        self.assertIn("not answered", line)

    def test_neither_is_a_malformed_answer(self):
        from nibelokal.modbus import ModbusOffline
        self.assertIn("not answered", self._run(ModbusOffline("nonsense")))
        # And not even something nobody anticipated: this line is a bonus on a
        # command whose job is to read registers.
        self.assertIn("unreadable", self._run(RuntimeError("kaboom")))

    def test_a_model_mismatch_is_said_out_loud(self):
        line = self._run({"vendor": "NIBE", "product": "F1245",
                          "revision": "5539", "objects": {}}, model="F750")
        self.assertIn("F1245", line)
        self.assertIn("plausible nonsense", line)

    def test_a_matching_model_says_nothing_extra(self):
        line = self._run({"vendor": "NIBE", "product": "F750",
                          "revision": "5539", "objects": {}}, model="F750")
        self.assertNotIn("note:", line)

    def test_objects_beyond_the_three_basic_ones_are_shown_too(self):
        # Whatever a pump volunteers is worth pasting into an issue, even when
        # this app has no name for it.
        line = self._run({"vendor": "NIBE", "product": "F750",
                          "objects": {0: "NIBE", 1: "F750", 6: "extra"}})
        self.assertIn("[6] extra", line)


class AnUnknownKeyIsReportedWhateverItHolds(unittest.TestCase):
    """The warning has to survive an empty value.

    `config.load` skipped every empty value before it looked at the key, so an
    unknown key with an empty value -- which is exactly how a typo looks in a
    file copied from config.example.yaml, where nearly every line is `key: ""`
    -- was silently dropped. `genration: ""` then produced no warning about the
    typo and no generation, which is the one setting whose absence reads as
    plausible nonsense rather than as a failure.
    """

    def _load_capturing(self, text):
        path = os.path.join(tempfile.mkdtemp(), "config.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        printed = []
        saved = builtins.print
        builtins.print = lambda *a, **kw: printed.append(" ".join(str(x) for x in a))
        try:
            cfg = config.load(path)
        finally:
            builtins.print = saved
        return cfg, "\n".join(printed)

    def test_an_unknown_key_with_an_empty_value_still_warns(self):
        _cfg, printed = self._load_capturing(
            'host: 192.0.2.10\nmodel: S735\ngenration: ""\n')
        self.assertIn("ignoring unknown key", printed)
        self.assertIn("genration", printed)

    def test_an_unknown_key_with_a_value_still_warns(self):
        _cfg, printed = self._load_capturing(
            "host: 192.0.2.10\nmodel: S735\ngenration: F\n")
        self.assertIn("genration", printed)

    def test_a_known_key_left_empty_is_still_the_default_and_silent(self):
        cfg, printed = self._load_capturing(
            'host: 192.0.2.10\nmodel: S735\ngeneration: ""\n')
        self.assertEqual(cfg["generation"], "")
        self.assertNotIn("ignoring unknown key", printed)


class TheDailyBackupIsOffOnAnFPump(unittest.TestCase):
    """23 to 34 minutes of the polling thread holding the only bus there is.

    On an S-series pump a full snapshot is a few seconds of Modbus TCP and a
    daily one is free -- which is what this app has always done and what it goes
    on doing. Through a MODBUS 40 the same snapshot is one register per request
    at NIBE's 2.1 s, taken inside the poll loop: every poll in that window is
    skipped, the page goes stale for half an hour, and an alarm raised during it
    is noticed when the backup finishes. Off until it is asked for, and said out
    loud on the console so that quietly off is not mistaken for broken.
    """

    def test_unset_is_daily_on_an_s_pump(self):
        self.assertEqual(24.0, config.auto_backup_hours({}, "S"))
        self.assertEqual(24.0, config.auto_backup_hours(
            {"auto_backup_hours": None}, "S"))

    def test_unset_is_off_on_an_f_pump(self):
        self.assertEqual(0.0, config.auto_backup_hours({}, "F"))
        self.assertEqual(0.0, config.auto_backup_hours(
            {"auto_backup_hours": None}, "F"))

    def test_a_number_in_the_config_is_used_on_either_generation(self):
        # An owner who asked for it knows what they asked for.
        for generation in ("S", "F"):
            self.assertEqual(6.0, config.auto_backup_hours(
                {"auto_backup_hours": 6}, generation))
            self.assertEqual(0.0, config.auto_backup_hours(
                {"auto_backup_hours": 0}, generation))

    def test_the_default_is_the_unset_sentinel_and_not_a_number(self):
        # Because the right number depends on the generation and config.py
        # does not know it. A 24 here would be an F-series default nobody
        # chose.
        self.assertIsNone(config.DEFAULTS["auto_backup_hours"])

    def test_a_config_file_that_says_nothing_still_reads_as_unset(self):
        path = os.path.join(tempfile.mkdtemp(), "config.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("host: 192.0.2.10\nmodel: F750\n")
        cfg = config.load(path)
        self.assertIsNone(cfg["auto_backup_hours"])
        self.assertEqual(0.0, config.auto_backup_hours(cfg, "F"))

    def test_a_config_file_that_says_a_number_is_honoured(self):
        path = os.path.join(tempfile.mkdtemp(), "config.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("host: 192.0.2.10\nmodel: F750\nauto_backup_hours: 48\n")
        self.assertEqual(48.0, config.auto_backup_hours(config.load(path), "F"))


class TheWordSwapKey(unittest.TestCase):
    """Which half of a 32-bit value comes first, and who decides.

    Empty means the generation's factory setting -- NIBE's own low-word-first
    on the S series, MODBUS 40's Big Endian on the F. It has to be settable
    because on the F series it is a setting in the pump's menu 5.3.11 rather
    than a fact, and the symptom of getting it wrong is a page that looks
    right apart from a few absurd 32-bit numbers.
    """

    def _load(self, text):
        path = os.path.join(tempfile.mkdtemp(), "config.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return config.load(path)

    def test_it_is_in_defaults_and_empty(self):
        self.assertEqual(config.DEFAULTS["word_swap"], "")

    def test_the_default_is_low_word_first_on_both_generations(self):
        # The S series because NIBE's own TIF says so, the F series because
        # register 48852 "Modbus40 Word Swap" has a factory value of 1 --
        # swapped, which is the low word at the lower address. The MODBUS 40
        # manual's "Factory setting: Big Endian" says the opposite and loses to
        # its own register map. See profile.LOW_WORD_FIRST.
        self.assertTrue(Profile.for_model("S735").low_word_first)
        self.assertTrue(Profile.for_model("F750").low_word_first)
        self.assertEqual(Profile.for_model("F750").word_order_source, "assumed")

    def test_an_explicit_value_wins_on_either_generation(self):
        self.assertTrue(Profile.for_model("F750", word_swap="true").low_word_first)
        self.assertFalse(Profile.for_model("S735", word_swap="false").low_word_first)
        self.assertFalse(Profile.for_model("F750", word_swap="false").low_word_first)

    def test_the_spellings_a_config_file_uses(self):
        for text in ("true", "yes", "on", "1", "True"):
            self.assertTrue(Profile.for_model("F750", word_swap=text).low_word_first,
                            text)
        for text in ("false", "no", "off", "0"):
            self.assertFalse(Profile.for_model("S735", word_swap=text).low_word_first,
                             text)

    def test_empty_is_not_false(self):
        # "" is "I have not said", and reading it as false would flip the S
        # series to an order NIBE's own document contradicts.
        self.assertTrue(Profile.for_model("S735", word_swap="").low_word_first)
        self.assertIsNone(Profile("S", word_swap=None).word_swap)

    def test_nonsense_is_refused_at_startup(self):
        # Rather than at the first 32-bit read, which is a plausible-looking
        # number on a page and nothing in a log.
        with self.assertRaises(SystemExit) as caught:
            self._load("host: 192.0.2.10\nmodel: F750\nword_swap: maybe\n")
        self.assertIn("word_swap", str(caught.exception))

    def test_it_reaches_the_pump_through_the_profile(self):
        cfg = self._load("host: 192.0.2.10\nmodel: F750\nword_swap: true\n")
        profile = Profile.for_model(cfg["model"], cfg["generation"],
                                    cfg["word_swap"])
        self.assertTrue(profile.low_word_first)
