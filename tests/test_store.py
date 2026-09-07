"""The history table: what it costs to ask it a question, and what a poll does
when it cannot be written to.

Two reproduced defects live here.

* Every /api/heating, /api/advice and /api/autotune request asked the store
  "how long have you been recording", and the answer came from a statement
  that counted every row -- a full table scan, measured at about 1.4 s at a
  year of data, taken while holding the store's lock, which is the same lock
  the poller needs to write the next reading. The page polls; the poller waits.
* A poll that read the pump perfectly and then failed to *store* the reading
  reported the pump as broken: the sqlite message went into `last_error`, the
  poll interval doubled towards ten minutes, the alarm watcher was handed an
  empty dict -- so alarm detection stopped -- and the daily backup was skipped.

The measurements here are step counts from SQLite's own progress handler
rather than wall-clock times, so they mean the same thing on a Pi as on a
laptop and do not go flaky under load.
"""
import logging
import os
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nibelokal import advisor                                   # noqa: E402
from nibelokal.store import Poller, Store                       # noqa: E402


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


def steps(db, fn):
    """How many virtual-machine instructions SQLite ran for `fn`.

    A query answered off an index costs a number that does not move when the
    table grows; a query that scans the table costs one that does.
    """
    counted = [0]

    def tick():
        counted[0] += 1
        return 0

    db.set_progress_handler(tick, 1)
    try:
        fn()
    finally:
        db.set_progress_handler(None, 1)
    return counted[0]


class HowMuchAQuestionCosts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "nibe.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _fill(self, polls, registers=25, start=0):
        now = int(time.time())
        rows = [(now - (start + p) * 60, 30000 + r, float(r), None)
                for p in range(polls) for r in range(registers)]
        with self.store._lock, self.store._db as c:
            c.executemany("INSERT INTO readings (ts,address,value,text) "
                          "VALUES (?,?,?,?)", rows)
        return len(rows)

    def test_span_does_not_get_slower_as_the_history_grows(self):
        self._fill(200)
        small = steps(self.store._db, self.store.span)
        self._fill(1000, start=200)
        large = steps(self.store._db, self.store.span)
        # MIN(ts) and MAX(ts) come off readings_ts; twenty-five thousand more
        # rows must not show up in the cost at all.
        self.assertLess(large, small * 2,
                        "span() is scanning the table (%d -> %d steps)"
                        % (small, large))

    def test_whereas_counting_every_row_does(self):
        """The statement that used to answer it, kept here as the contrast.

        This is not a test of our code -- it is the evidence that the fix is a
        fix. If SQLite ever learns to answer this off an index, this test fails
        loudly rather than leaving the comparison above meaningless.
        """
        self._fill(200)

        def old():
            with self.store._lock, self.store._db as c:
                c.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM readings").fetchone()

        small = steps(self.store._db, old)
        self._fill(1000, start=200)
        large = steps(self.store._db, old)
        self.assertGreater(large, small * 3,
                           "the old statement no longer scans; rewrite the comparison")

    def test_span_answers_the_same_thing_stats_does(self):
        self._fill(50)
        stats = self.store.stats()
        self.assertEqual(self.store.span(), (stats["first"], stats["last"]))

    def test_an_empty_history_is_two_nones_and_not_a_crash(self):
        self.assertEqual(self.store.span(), (None, None))
        self.assertEqual(self.store.stats(),
                         {"rows": 0, "first": None, "last": None})

    def test_stats_still_counts_the_rows_for_the_backup_page(self):
        n = self._fill(10)
        self.assertEqual(self.store.stats()["rows"], n)


class WhatTheHeatingPageAsksFor(unittest.TestCase):
    """advisor._history_hours is on the path of three polled endpoints.

    It wants two timestamps. It used to get them from stats(), which fetches a
    row count nobody on that path reads.
    """

    class Spy:
        def __init__(self, first, last):
            self.first, self.last = first, last
            self.stats_calls = 0

        def span(self):
            return self.first, self.last

        def stats(self):
            self.stats_calls += 1
            return {"rows": 13_000_000, "first": self.first, "last": self.last}

    def test_it_does_not_count_the_rows(self):
        spy = self.Spy(1_700_000_000, 1_700_000_000 + 36 * 3600)
        self.assertAlmostEqual(advisor._history_hours(spy), 36.0)
        self.assertEqual(spy.stats_calls, 0,
                         "the heating page still triggers a full table scan")

    def test_a_store_from_an_older_build_still_works(self):
        # span() is new. A store without it must still answer, because this
        # module is also handed stand-ins by other code and by tests.
        class Old:
            stats_calls = 0

            def stats(self):
                Old.stats_calls += 1
                return {"rows": 1, "first": 1_700_000_000,
                        "last": 1_700_000_000 + 7200}

        self.assertAlmostEqual(advisor._history_hours(Old()), 2.0)
        self.assertEqual(Old.stats_calls, 1)

    def test_no_history_at_all_is_zero_hours(self):
        self.assertEqual(advisor._history_hours(self.Spy(None, None)), 0.0)


class ADatabaseThatWillNotTakeTheReading(unittest.TestCase):
    """A store failure is not a pump failure, and must not be reported as one.

    Reproduced by making record() raise, which is what a database on a full SD
    card or one locked by a stray sqlite3 shell does.
    """

    class Pump:
        host, port = "192.0.2.10", 502

        def __init__(self):
            self.registry = None
            self.reads = 0

        def read_many(self, addresses):
            self.reads += 1
            return {a: {"value": 1.0} for a in addresses}

    class DeadStore:
        def __init__(self):
            self.attempts = 0

        def record(self, data, ts=None):
            self.attempts += 1
            raise sqlite3.OperationalError("database or disk is full")

        def record_indoor(self, *a, **kw):
            pass

        def prune(self):
            return 0

    class Watcher:
        def __init__(self):
            self.seen = []

        def poll(self, values):
            self.seen.append(values)

    def _run(self, **kwargs):
        pump = self.Pump()
        store = self.DeadStore()
        poller = Poller(pump, store, [30002, 30006], seconds=15, **kwargs)
        poller.start()
        deadline = time.time() + 10
        while time.time() < deadline and store.attempts < 1:
            time.sleep(0.01)
        time.sleep(0.05)
        alive = poller.is_alive()
        poller.stop()
        poller.join(timeout=5)
        return pump, store, poller, alive

    def test_the_poll_loop_survives_it(self):
        _, store, poller, alive = self._run()
        self.assertTrue(alive)
        self.assertGreaterEqual(store.attempts, 1)

    def test_the_pump_is_not_blamed_for_it(self):
        _, _, poller, _ = self._run()
        self.assertIsNone(poller.last_error,
                          "a database failure was reported as the pump failing")
        self.assertIsNotNone(poller.last_ok, "the poll did succeed")

    def test_it_is_reported_as_itself_so_somebody_can_fix_the_right_thing(self):
        _, _, poller, _ = self._run()
        self.assertIn("full", poller.last_store_error)

    def test_the_reading_is_still_in_hand_for_the_page(self):
        _, _, poller, _ = self._run()
        self.assertEqual(poller.latest, {30002: {"value": 1.0},
                                         30006: {"value": 1.0}})

    def test_and_the_alarm_watcher_still_sees_the_real_values(self):
        """The one that would have cost somebody a compressor.

        With the store failure counted as a poll failure, _watch() was handed
        {} -- the "the pump did not answer" case -- so register 31976 was never
        looked at and an alarm standing right there went unnoticed for as long
        as the disk stayed full.
        """
        watcher = self.Watcher()
        self._run(watcher=watcher)
        self.assertTrue(watcher.seen)
        self.assertTrue(all(seen for seen in watcher.seen),
                        "the watcher was handed an empty poll")

    def test_a_pump_failure_is_still_a_pump_failure(self):
        class Broken(self.Pump):
            def read_many(self, addresses):
                self.reads += 1
                raise OSError("pumpen svarar inte")

        pump = Broken()
        store = self.DeadStore()
        poller = Poller(pump, store, [30002], seconds=15)
        poller.start()
        deadline = time.time() + 10
        while time.time() < deadline and pump.reads < 1:
            time.sleep(0.01)
        time.sleep(0.05)
        poller.stop()
        poller.join(timeout=5)
        self.assertIn("svarar inte", poller.last_error)
        self.assertIsNone(poller.last_store_error)
        self.assertEqual(store.attempts, 0)


if __name__ == "__main__":
    unittest.main()
