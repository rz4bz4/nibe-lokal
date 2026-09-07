"""Tests for the indoor feed, against a fake Homey instead of the LAN.

Two of these are about a lock rather than about temperatures, and they are the
ones that matter on a bad day: the pump is unreachable, the poller is backing
off up to ten minutes, Homey accepts the connection and never answers -- and
/api/status, which the page polls every thirty seconds, must still return at
once. The rest cover what a credential must never do, which is appear in a
message on that page.
"""
import copy
import http.client
import os
import sys
import threading
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nibelokal.homey import Homey                              # noqa: E402

DEVICES = {
    "dev-1": {"id": "dev-1", "name": "Sovrum", "zone": "zone-1",
              "capabilitiesObj": {"measure_temperature":
                                  {"value": 20.5, "lastUpdated": None}}},
    "dev-2": {"id": "dev-2", "name": "Vardagsrum", "zone": "zone-2",
              "capabilitiesObj": {"measure_temperature":
                                  {"value": 21.5, "lastUpdated": None}}},
}
ZONES = {"zone-1": {"id": "zone-1", "name": "Sovrum"},
         "zone-2": {"id": "zone-2", "name": "Vardagsrum"}}


def fresh_devices():
    """The device list with fresh timestamps, in Homey's epoch milliseconds."""
    devices = copy.deepcopy(DEVICES)
    for device in devices.values():
        device["capabilitiesObj"]["measure_temperature"]["lastUpdated"] = (
            time.time() * 1000.0)
    return devices


class FakeHomey(Homey):
    """A Homey that answers from canned data. Counts and can block."""

    def __init__(self, config=None, gate=None, error=None):
        base = {"homey_host": "192.0.2.20", "homey_token": "abc123",
                "homey_cache_seconds": 120.0}
        base.update(config or {})
        super().__init__(host=base["homey_host"], token=base["homey_token"],
                         devices=base.get("homey_devices", ""),
                         cache_seconds=base["homey_cache_seconds"])
        self.calls = 0
        self.gate = gate
        self.error = error

    def _get(self, path):
        self.calls += 1
        if self.gate is not None:
            self.gate.wait(5)
        if self.error is not None:
            return None, self.error
        return (fresh_devices() if path.endswith("/device") else ZONES), None


class TheLockIsNeverHeldAcrossTheNetwork(unittest.TestCase):
    def test_a_second_caller_is_not_stuck_behind_a_slow_homey(self):
        # The repro: a Homey that accepts the connection and never answers. The
        # concurrent /api/status call used to wait out both 5 s timeouts.
        gate = threading.Event()
        gate.set()                           # open, so the first fetch is quick
        homey = FakeHomey(gate=gate)
        homey.snapshot()                     # fill the cache, then let it rot
        homey._cached_at = 0.0               # noqa: SLF001
        gate.clear()                         # and now Homey stops answering

        started = threading.Event()

        def slow():
            started.set()
            homey.snapshot()

        worker = threading.Thread(target=slow, daemon=True)
        worker.start()
        started.wait(5)
        time.sleep(0.05)                     # the worker is inside _get by now

        began = time.time()
        second = homey.snapshot()
        waited = time.time() - began
        gate.set()
        worker.join(timeout=5)

        self.assertLess(waited, 1.0, "the status call waited out the LAN request")
        self.assertTrue(second["ok"])

    def test_only_one_of_them_talks_to_homey(self):
        gate = threading.Event()
        gate.set()
        homey = FakeHomey(gate=gate)
        homey.snapshot()
        calls_after_first = homey.calls
        homey._cached_at = 0.0               # noqa: SLF001
        gate.clear()

        worker = threading.Thread(target=homey.snapshot, daemon=True)
        worker.start()
        time.sleep(0.05)
        homey.snapshot()
        gate.set()
        worker.join(timeout=5)
        self.assertEqual(homey.calls, calls_after_first * 2,
                         "the second caller started its own request")

    def test_a_caller_with_nothing_cached_does_wait_for_an_answer(self):
        homey = FakeHomey()
        self.assertIsNone(homey.cached_snapshot())
        self.assertTrue(homey.snapshot()["ok"])


class TheCacheIsHandedOutAsACopy(unittest.TestCase):
    def test_editing_the_answer_does_not_corrupt_the_cache(self):
        homey = FakeHomey()
        first = homey.snapshot()
        first["average"] = -999
        first["sensors"][0]["value"] = -999
        second = homey.snapshot()
        self.assertNotEqual(second["average"], -999)
        self.assertNotEqual(second["sensors"][0]["value"], -999)
        self.assertEqual(homey.calls, 2, "and it was not refetched to recover")

    def test_cached_snapshot_is_a_copy_too(self):
        homey = FakeHomey()
        homey.snapshot()
        cached = homey.cached_snapshot()
        cached["sensors"] = []
        self.assertTrue(homey.cached_snapshot()["sensors"])


class TheStatusSummaryNeverFetches(unittest.TestCase):
    def test_cached_snapshot_returns_none_before_anything_was_fetched(self):
        homey = FakeHomey()
        self.assertIsNone(homey.cached_snapshot())
        self.assertEqual(homey.calls, 0)

    def test_and_never_goes_to_the_lan_afterwards(self):
        homey = FakeHomey()
        homey.snapshot()
        calls = homey.calls
        homey._cached_at = 0.0               # noqa: SLF001 -- expired
        self.assertIsNotNone(homey.cached_snapshot())
        self.assertEqual(homey.calls, calls)


class Caching(unittest.TestCase):
    def test_a_second_call_inside_the_ttl_does_not_ask_again(self):
        homey = FakeHomey()
        homey.snapshot()
        homey.snapshot()
        self.assertEqual(homey.calls, 2)     # one devices + one zones request

    def test_a_failure_is_cached_for_the_same_ttl_as_a_success(self):
        homey = FakeHomey(error="Homey svarade med fel 500.")
        first = homey.snapshot()
        homey.snapshot()
        self.assertFalse(first["ok"])
        self.assertEqual(homey.calls, 1,
                         "a Homey that is down must not be asked every poll")

    def test_a_clock_that_steps_backwards_does_not_freeze_the_cache(self):
        # ntp after a power cut, or a Pi with no clock at all. A negative age
        # read as "very fresh" and the cache stopped updating until the clock
        # caught up.
        homey = FakeHomey()
        homey.snapshot()
        calls = homey.calls
        homey._cached_at = time.time() + 3600.0            # noqa: SLF001
        homey.snapshot()
        self.assertGreater(homey.calls, calls)


class Credentials(unittest.TestCase):
    BAD = "abc\rSECRETVALUE"

    def test_a_token_with_a_control_character_never_reaches_the_request(self):
        # http.client raises ValueError("Invalid header value b'Bearer ...'"),
        # and _get turned that message -- token included -- into the error
        # string printed on the page.
        homey = FakeHomey({"homey_token": self.BAD})
        result = homey.snapshot()
        self.assertFalse(result["ok"])
        self.assertEqual(homey.calls, 0)
        self.assertIn("homey_token", result["error"])
        self.assertNotIn("SECRETVALUE", result["error"])

    def test_the_check_happens_once_at_construction(self):
        self.assertIsNone(Homey(host="192.0.2.20", token="fine").token_error)
        self.assertTrue(Homey(host="192.0.2.20", token=self.BAD).token_error)

    def test_an_error_that_quotes_the_token_is_scrubbed(self):
        token = "goodlookingtoken"

        class Leaky(Homey):
            def _get(self, path):
                raise ValueError("Invalid header value b'Bearer %s'" % token)

        result = Leaky(host="192.0.2.20", token=token).snapshot()
        self.assertFalse(result["ok"])
        self.assertNotIn(token, result["error"])

    def test_a_url_error_reason_is_scrubbed_too(self):
        import urllib.error
        token = "anothergoodtoken"

        class Leaky(Homey):
            def _get(self, path):
                return Homey._get(self, path)

        homey = Leaky(host="192.0.2.20", token=token)

        def boom(*_a, **_k):
            raise urllib.error.URLError("Bearer %s refused" % token)

        import nibelokal.homey as module
        original = module.urllib.request.build_opener
        module.urllib.request.build_opener = lambda *_a: type(
            "O", (), {"open": staticmethod(boom)})()
        try:
            result = homey.snapshot()
        finally:
            module.urllib.request.build_opener = original
        self.assertFalse(result["ok"])
        self.assertNotIn(token, result["error"])


class Configuration(unittest.TestCase):
    def test_from_config_reads_the_keys_config_defaults_carries(self):
        from nibelokal.config import DEFAULTS

        homey = Homey.from_config(dict(DEFAULTS, homey_host="192.0.2.20",
                                       homey_token="abc", homey_devices="Sovrum",
                                       homey_max_age_minutes=45.0,
                                       homey_cache_seconds=90.0))
        self.assertTrue(homey.configured)
        self.assertEqual(homey.whitelist, ["Sovrum"])
        self.assertEqual(homey.max_age_minutes, 45.0)
        self.assertEqual(homey.cache_seconds, 90.0)

    def test_a_missing_key_still_falls_back(self):
        homey = Homey.from_config({})
        self.assertFalse(homey.configured)
        self.assertEqual(homey.max_age_minutes, 60.0)


class AHomeyThatAnswersHalfARespone(unittest.TestCase):
    """BadStatusLine and IncompleteRead are neither OSError nor URLError.

    http.client raises its own exception hierarchy, and _get() caught the two
    urllib ones plus OSError -- so a Homey that accepts the connection and then
    sends a truncated response threw straight past _get(). snapshot()'s own
    catch-all then caught it, and that path does not write the cache: the
    result was a device on the LAN being re-asked on every single call, with
    every /api/indoor and every poll paying a fresh timeout, instead of once
    per cache window like every other failure here.
    """

    def _client(self, exc, cache_seconds=120.0):
        homey = Homey(host="192.0.2.20", token="abc123",
                      cache_seconds=cache_seconds)
        calls = []

        class Opener:
            def open(self, req, timeout=None):
                calls.append(1)
                raise exc

        def build_opener(*handlers):
            return Opener()

        patched = unittest.mock.patch(
            "nibelokal.homey.urllib.request.build_opener", build_opener)
        patched.start()
        self.addCleanup(patched.stop)
        return homey, calls

    def test_a_truncated_status_line_is_a_swedish_sentence(self):
        homey, _ = self._client(http.client.BadStatusLine("\x16\x03\x01"))
        snap = homey.snapshot()
        self.assertFalse(snap["ok"])
        self.assertIn("Homey", snap["error"])

    def test_and_it_is_remembered_like_every_other_failure(self):
        homey, calls = self._client(http.client.BadStatusLine("garbage"))
        homey.snapshot()
        homey.snapshot()
        homey.snapshot()
        self.assertEqual(len(calls), 1,
                         "a half-answering Homey was re-asked on every call")

    def test_an_incomplete_read_too(self):
        homey, calls = self._client(http.client.IncompleteRead(b"{", 400))
        self.assertFalse(homey.snapshot()["ok"])
        homey.snapshot()
        self.assertEqual(len(calls), 1)

    def test_the_token_is_not_in_the_message(self):
        # The exception text can quote the bytes that were being sent.
        homey, _ = self._client(http.client.BadStatusLine("Bearer abc123"))
        self.assertNotIn("abc123", homey.snapshot()["error"])

    def test_nothing_escapes_snapshot_whatever_http_client_raises(self):
        for exc in (http.client.BadStatusLine("x"),
                    http.client.IncompleteRead(b"", 1),
                    http.client.LineTooLong("header line"),
                    http.client.RemoteDisconnected("closed")):
            homey, _ = self._client(exc)
            snap = homey.snapshot()
            self.assertFalse(snap["ok"], repr(exc))
            self.assertTrue(snap["error"].strip(), repr(exc))


if __name__ == "__main__":
    unittest.main()
