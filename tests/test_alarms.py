"""Tests for the alarm watcher, against a real SQLite file and a fake notifier.

The failure that matters here is not a crash, it is silence in either
direction: an alarm that is never announced, or a standing alarm announced
every minute until the person turns notifications off and then misses the next
real one. Both look fine in a smoke test, so they are what these cover --
together with the restart, because this runs on a machine that reboots after
every power cut, which is exactly when a pump has also just alarmed.

No network: the notifier is a fake, and PushoverNotifier is only ever asked to
build its request, never to send it.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nibelokal import alarms                                   # noqa: E402
from nibelokal.alarms import R_ALARM, R_CLASS1, Watcher         # noqa: E402
from nibelokal.store import Store                               # noqa: E402


class FakePump:
    host = "192.0.2.10"


class FakeNotifier:
    """Records what would have been sent, and fails on demand."""

    name = "fake"

    def __init__(self):
        self.sent = []
        self.fail_with = None

    def send(self, title, message, priority=0):
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append({"title": title, "message": message, "priority": priority})


def reading(code, class1=None):
    """The shape Pump.read_many() returns."""
    values = {R_ALARM: {"value": code}}
    if class1 is not None:
        values[R_CLASS1] = {"value": class1}
    return values


class AlarmTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "nibe.db")
        self.store = Store(self.path)
        self.notifier = FakeNotifier()

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def watcher(self, config=None, notifier=None, store=None):
        return Watcher(store or self.store, FakePump(), config or {},
                       notifier=self.notifier if notifier is None else notifier)

    def reopen(self):
        """Close and reopen everything, as a restart would."""
        self.store.close()
        self.store = Store(self.path)
        return self.store


class TestTable(AlarmTestCase):
    def test_shipped_table_loads(self):
        table = alarms.load_table()
        self.assertGreater(len(table["codes"]), 100)
        self.assertIn("source", table["_meta"])

    def test_known_code(self):
        # 163 is a phase fault: the pump cannot run the compressor at all.
        info = alarms.describe(163)
        self.assertTrue(info["known"])
        self.assertTrue(info["text"])
        self.assertIn(info["severity"], alarms.SEVERITIES)

    def test_unknown_code_is_not_guessed(self):
        info = alarms.describe(65123)
        self.assertFalse(info["known"])
        self.assertEqual(info["code"], 65123)
        # The number has to be in the text: it is the only thing the owner can
        # actually look up.
        self.assertIn("65123", info["text"])
        self.assertEqual(info["severity"], "warning")

    def test_garbage_code_does_not_raise(self):
        info = alarms.describe("not a number")
        self.assertFalse(info["known"])
        self.assertIsNone(info["code"])

    def test_missing_table_file(self):
        alarms._table = None
        try:
            table = alarms.load_table(os.path.join(self.dir.name, "nope.json"))
            self.assertEqual(table["codes"], {})
        finally:
            alarms._table = None
        # And the shipped table is picked up again afterwards.
        self.assertTrue(alarms.describe(163)["known"])


class TestEdgeTriggering(AlarmTestCase):
    def test_standing_alarm_notifies_once(self):
        w = self.watcher()
        first = w.poll(reading(163))
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["kind"], "alarm")
        self.assertEqual(first[0]["code"], 163)

        # Twenty more polls of the same standing alarm: not one more event.
        for _ in range(20):
            self.assertEqual(w.poll(reading(163)), [])
        self.assertEqual(len(self.notifier.sent), 1)

    def test_no_alarm_is_quiet(self):
        w = self.watcher()
        for _ in range(5):
            self.assertEqual(w.poll(reading(0)), [])
        self.assertEqual(self.notifier.sent, [])

    def test_new_code_replacing_old_reports_both_transitions(self):
        w = self.watcher()
        w.poll(reading(163))
        events = w.poll(reading(101))
        kinds = {(e["kind"], e["code"]) for e in events}
        self.assertEqual(kinds, {("clear", 163), ("alarm", 101)})

    def test_unreadable_register_is_not_an_all_clear(self):
        """A Modbus error is not good news, and must not be reported as any."""
        w = self.watcher()
        w.poll(reading(163))
        self.notifier.sent.clear()
        self.assertEqual(w.poll({R_ALARM: {"error": "timeout"}}), [])
        self.assertEqual(w.poll({}), [])
        self.assertEqual(self.notifier.sent, [])
        # The alarm is still considered standing, so it is not re-announced
        # when the register comes back.
        self.assertEqual(w.poll(reading(163)), [])

    def test_class_one_flag_without_a_number(self):
        w = self.watcher()
        events = w.poll(reading(0, class1=1))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["code"], 0)
        self.assertFalse(events[0]["known"])
        self.assertIn("32196", events[0]["text"])
        # And it clears like any other.
        self.assertEqual([e["kind"] for e in w.poll(reading(0, class1=0))], ["clear"])


class TestClearTransition(AlarmTestCase):
    def test_clear_is_its_own_event(self):
        w = self.watcher()
        w.poll(reading(163))
        events = w.poll(reading(0))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "clear")
        self.assertEqual(events[0]["code"], 163)
        self.assertEqual(len(self.notifier.sent), 2)
        self.assertEqual(self.notifier.sent[1]["priority"], 0)

    def test_clear_then_same_alarm_again_notifies_again(self):
        w = self.watcher()
        w.poll(reading(163))
        w.poll(reading(0))
        events = w.poll(reading(163))
        self.assertEqual([e["kind"] for e in events], ["alarm"])
        self.assertEqual(len(self.notifier.sent), 3)

    def test_clear_is_sent_even_below_the_threshold(self):
        w = self.watcher({"alarm_min_severity": "alarm"})
        w.poll(reading(163))
        w.poll(reading(0))
        kinds = [s["title"].split()[0] for s in self.notifier.sent]
        self.assertEqual(len(kinds), 2)


class TestRestart(AlarmTestCase):
    def test_standing_alarm_is_not_reannounced_after_restart(self):
        w = self.watcher()
        self.assertEqual(len(w.poll(reading(163))), 1)

        store = self.reopen()
        notifier = FakeNotifier()
        w2 = Watcher(store, FakePump(), {}, notifier=notifier)
        self.assertEqual(w2.poll(reading(163)), [])
        self.assertEqual(notifier.sent, [])

    def test_alarm_that_cleared_while_down_is_reported_on_the_next_poll(self):
        w = self.watcher()
        w.poll(reading(163))

        store = self.reopen()
        notifier = FakeNotifier()
        w2 = Watcher(store, FakePump(), {}, notifier=notifier)
        events = w2.poll(reading(0))
        self.assertEqual([e["kind"] for e in events], ["clear"])

    def test_state_survives_as_active(self):
        w = self.watcher()
        w.poll(reading(163))
        self.assertEqual([a["code"] for a in w.active()], [163])
        store = self.reopen()
        w2 = Watcher(store, FakePump(), {}, notifier=FakeNotifier())
        self.assertEqual([a["code"] for a in w2.active()], [163])


class TestNotifier(AlarmTestCase):
    def test_disabled_by_default(self):
        """No credentials in the config means nothing goes anywhere."""
        w = Watcher(self.store, FakePump(), {})
        self.assertIsNone(w.notifier)
        events = w.poll(reading(163))
        # The alarm is still detected and logged; only the push is missing.
        self.assertEqual(len(events), 1)
        self.assertEqual([h["kind"] for h in w.history()], ["alarm"])

    def test_disabled_when_only_half_configured(self):
        self.assertIsNone(alarms.make_notifier({"pushover_token": "t" * 30}))
        self.assertIsNone(alarms.make_notifier({"pushover_user": "u" * 30}))

    def test_mute_switch(self):
        cfg = {"pushover_token": "t" * 30, "pushover_user": "u" * 30,
               "alarm_notify": False}
        self.assertIsNone(alarms.make_notifier(cfg))
        cfg["alarm_notify"] = "false"       # as a plain-parser config file gives it
        self.assertIsNone(alarms.make_notifier(cfg))
        cfg["alarm_notify"] = True
        self.assertIsInstance(alarms.make_notifier(cfg), alarms.PushoverNotifier)

    def test_severity_becomes_priority(self):
        w = self.watcher()
        w.poll(reading(163))                # severity "alarm" in the table
        self.assertEqual(self.notifier.sent[0]["priority"], 2)

        w2 = self.watcher({"alarm_emergency_priority": False})
        w2.poll(reading(0))
        self.notifier.sent.clear()
        w2.poll(reading(163))
        self.assertEqual(self.notifier.sent[0]["priority"], 1)

    def test_min_severity_filters(self):
        info_code = next(c for c, e in alarms.load_table()["codes"].items()
                         if e["severity"] == "info")
        w = self.watcher({"alarm_min_severity": "alarm"})
        w.poll(reading(int(info_code)))
        self.assertEqual(self.notifier.sent, [])

    def test_failed_send_is_retried_next_poll(self):
        w = self.watcher()
        self.notifier.fail_with = alarms.SendFailed("network down")
        w.poll(reading(163))
        self.assertEqual(self.notifier.sent, [])

        self.notifier.fail_with = None
        # No new alarm, but the pending notification goes out.
        self.assertEqual(w.poll(reading(163)), [])
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(self.notifier.sent[0]["priority"], 2)

    def test_pending_send_survives_a_restart(self):
        w = self.watcher()
        self.notifier.fail_with = alarms.SendFailed("network down")
        w.poll(reading(163))

        store = self.reopen()
        notifier = FakeNotifier()
        w2 = Watcher(store, FakePump(), {}, notifier=notifier)
        w2.poll(reading(163))
        self.assertEqual(len(notifier.sent), 1)

    def test_rejected_send_is_not_retried_forever(self):
        w = self.watcher()
        self.notifier.fail_with = alarms.SendFailed("bad token", permanent=True)
        w.poll(reading(163))
        self.notifier.fail_with = None
        self.assertEqual(w.poll(reading(163)), [])
        self.assertEqual(self.notifier.sent, [])

    def test_queue_is_bounded(self):
        w = self.watcher()
        self.notifier.fail_with = alarms.SendFailed("network down")
        # Flap the alarm far more often than the queue is allowed to hold.
        for i in range(alarms.MAX_PENDING * 2):
            w.poll(reading(163 if i % 2 == 0 else 0))
        with self.store._lock, self.store._db as c:
            pending = c.execute(
                "SELECT COUNT(*) FROM alarm_events WHERE sent = 0").fetchone()[0]
        self.assertLessEqual(pending, alarms.MAX_PENDING)

        self.notifier.fail_with = None
        w.poll(reading(163))
        self.assertLessEqual(len(self.notifier.sent), alarms.MAX_PENDING)

    def test_broken_notifier_does_not_break_the_poller(self):
        class Exploding:
            name = "boom"

            def send(self, *_args, **_kwargs):
                raise RuntimeError("kaboom")

        w = self.watcher(notifier=Exploding())
        self.assertEqual(len(w.poll(reading(163))), 1)
        self.assertEqual(len(w.poll(reading(0))), 1)


class TestPushoverRequest(AlarmTestCase):
    """The request is built, never sent. Priority 2 without retry/expire is
    rejected by Pushover, and it is the priority that matters most."""

    def fields(self, priority, **kwargs):
        sent = {}
        notifier = alarms.PushoverNotifier("t" * 30, "u" * 30, **kwargs)

        def fake_urlopen(request, timeout=None):
            import urllib.parse
            sent.update(dict(urllib.parse.parse_qsl(request.data.decode("utf-8"))))
            raise AssertionError("no network in tests")

        original = alarms.urllib.request.urlopen
        alarms.urllib.request.urlopen = fake_urlopen
        try:
            with self.assertRaises(alarms.SendFailed):
                notifier.send("titel", "meddelande", priority)
        finally:
            alarms.urllib.request.urlopen = original
        return sent

    def test_emergency_carries_retry_and_expire(self):
        fields = self.fields(2)
        self.assertEqual(fields["priority"], "2")
        self.assertIn("retry", fields)
        self.assertIn("expire", fields)
        self.assertGreaterEqual(int(fields["retry"]), 30)
        self.assertLessEqual(int(fields["expire"]), 10800)

    def test_retry_and_expire_are_clamped_to_the_api_limits(self):
        fields = self.fields(2, retry_seconds=5, expire_seconds=99999)
        self.assertEqual(fields["retry"], "30")
        self.assertEqual(fields["expire"], "10800")

    def test_normal_priority_omits_them(self):
        fields = self.fields(0)
        self.assertNotIn("retry", fields)
        self.assertNotIn("expire", fields)
        self.assertEqual(fields["token"], "t" * 30)
        self.assertEqual(fields["user"], "u" * 30)
        self.assertEqual(fields["message"], "meddelande")


if __name__ == "__main__":
    unittest.main()
