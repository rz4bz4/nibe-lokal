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
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nibelokal import alarms                                   # noqa: E402
from nibelokal.alarms import R_ALARM, R_CLASS1, Watcher         # noqa: E402
from nibelokal.profile import Profile                           # noqa: E402
from nibelokal.store import Store                               # noqa: E402


#: The two profiles every case in this file is run against. `S` is the one the
#: asserted texts belong to; `F` appears only where the difference is the
#: point.
S = Profile("S")
F = Profile("F")


class FakePump:
    """A pump stands in for a Pump, and a Pump always has a profile.

    Which generation it is decides which alarm table the watcher reads, and
    the two tables are different sets of alarms at the same numbers -- see
    TestTheTableIsChosenByGeneration below. Every case in this file that is
    not about the generation runs on an S-series pump, which is the one the
    texts it asserts belong to.
    """

    host = "192.0.2.10"

    def __init__(self, generation="S"):
        self.profile = Profile(generation)


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

    def silent_watcher(self, config=None, store=None):
        """A watcher with no notifier at all, as an unconfigured install has.

        Built the way the app builds it -- no credentials in the config, so
        make_notifier() returns None -- rather than by passing one in.
        """
        w = Watcher(store or self.store, FakePump(), config or {})
        self.assertIsNone(w.notifier)
        return w

    def fresh_watcher(self, name: str):
        """A watcher on its own empty database, for one case of a loop."""
        store = Store(os.path.join(self.dir.name, "%s.db" % name))
        self.addCleanup(store.close)
        return Watcher(store, FakePump(), {}, notifier=FakeNotifier())

    def reopen(self):
        """Close and reopen everything, as a restart would."""
        self.store.close()
        self.store = Store(self.path)
        return self.store


class TestTable(AlarmTestCase):
    def test_shipped_table_loads(self):
        table = alarms.load_table("S")
        self.assertGreater(len(table["codes"]), 100)
        self.assertIn("source", table["_meta"])

    def test_known_code(self):
        # 163 is a phase fault: the pump cannot run the compressor at all.
        info = alarms.describe(163, S)
        self.assertTrue(info["known"])
        self.assertTrue(info["text"])
        self.assertIn(info["severity"], alarms.SEVERITIES)

    def test_unknown_code_is_not_guessed(self):
        info = alarms.describe(65123, S)
        self.assertFalse(info["known"])
        self.assertEqual(info["code"], 65123)
        # The number has to be in the text: it is the only thing the owner can
        # actually look up.
        self.assertIn("65123", info["text"])
        self.assertEqual(info["severity"], "warning")

    def test_garbage_code_does_not_raise(self):
        info = alarms.describe("not a number", S)
        self.assertFalse(info["known"])
        self.assertIsNone(info["code"])

    def test_missing_table_file(self):
        # An explicit path is never cached, so the shipped table is untouched
        # and still there afterwards.
        table = alarms.load_table("S", os.path.join(self.dir.name, "nope.json"))
        self.assertEqual(table["codes"], {})
        self.assertTrue(alarms.describe(163, S)["known"])

    def test_a_table_for_a_generation_that_does_not_exist(self):
        # Not a fallback to the other one: there are exactly two numberings,
        # and a third is a bug in the caller rather than a reason to guess.
        with self.assertRaises(ValueError):
            alarms.load_table("X")


class TestTheTableIsChosenByGeneration(AlarmTestCase):
    """The S and F alarm numbers are different alarms at the same numbers.

    301 code numbers appear in both files and exactly one of them carries the
    same text. Code 123 is the clearest case: "Ingen rumsgivare i kyla" on the
    S series and a sensor fault on the outdoor air sensor on the F. Serving the
    wrong one is not a missing text -- it is a fluent Swedish sentence about a
    fault the pump does not have, pushed at priority 2 at three in the morning.
    """

    def test_the_f_table_answers_for_an_f_pump(self):
        info = alarms.describe(123, F)
        self.assertTrue(info["known"])
        self.assertIn("BT23", info["text"])

    def test_the_s_table_answers_for_an_s_pump(self):
        info = alarms.describe(123, S)
        self.assertTrue(info["known"])
        self.assertIn("rumsgivare", info["text"])

    def test_the_two_texts_are_not_the_same(self):
        self.assertNotEqual(alarms.describe(123, S)["text"],
                            alarms.describe(123, F)["text"])

    def test_no_profile_refuses_rather_than_falling_back(self):
        info = alarms.describe(123, None)
        self.assertFalse(info["known"])
        self.assertEqual(info["code"], 123)
        # The number is all it will say, and it says why.
        self.assertIn("123", info["text"])
        self.assertIn("generation", info["text"])

    def test_a_bare_generation_letter_works_too(self):
        # The watcher hands over a Profile; a caller with only the letter
        # should not have to build one.
        self.assertEqual(alarms.describe(123, "F")["text"],
                         alarms.describe(123, F)["text"])

    def test_the_watcher_takes_it_from_the_pump(self):
        w = Watcher(self.store, FakePump("F"), {}, notifier=self.notifier)
        events = w.poll(reading(123))
        self.assertEqual(len(events), 1)
        self.assertIn("BT23", events[0]["text"])
        self.assertIn("BT23", self.notifier.sent[0]["message"])

    def test_and_an_s_pump_still_gets_the_s_text(self):
        w = Watcher(self.store, FakePump("S"), {}, notifier=self.notifier)
        events = w.poll(reading(123))
        self.assertIn("rumsgivare", events[0]["text"])

    def test_a_pump_with_no_profile_at_all_reports_the_number(self):
        # A test double, or a Watcher built against an older Pump. It still
        # notices the alarm and still notifies; what it will not do is invent
        # a text for it.
        pump = FakePump()
        del pump.profile
        w = Watcher(self.store, pump, {}, notifier=self.notifier)
        events = w.poll(reading(123))
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0]["known"])
        self.assertIn("123", events[0]["text"])


class TestTheFCodesReadByHand(AlarmTestCase):
    """Ten F-series codes the machine rule got wrong, in both directions.

    The rule that classified `alarms_f.json` reads NIBE's long text. Ten codes
    have no long text at all, or only NIBE's generic "this alarm came from the
    heat pump" wording, so all ten fell through to `warning`. Seven of them are
    the pump narrating normal operation -- a defrost, a start-up, a preheat --
    and at the default `alarm_min_severity: warning` an exhaust-air F750 would
    have pushed a notification for every defrost, several times a day, all
    winter. Three are the faults that stop the compressor, and at
    `alarm_min_severity: alarm` nobody would have been woken by them.

    Reclassified by hand, by the S file's own rule, and recorded in
    `_meta.hand_reclassified` -- these are the only codes in that file a person
    has read.
    """

    IN_PROGRESS = (175, 183, 233, 234, 235, 270, 998)
    REAL_FAULTS = (220, 221, 222)

    def test_a_defrost_is_information_and_not_a_warning(self):
        self.assertEqual("info", alarms.describe(183, F)["severity"])
        self.assertIn("Avfrostning", alarms.describe(183, F)["text"])

    def test_every_in_progress_code_is_information(self):
        for code in self.IN_PROGRESS:
            self.assertEqual("info", alarms.describe(code, F)["severity"], code)

    def test_a_defrost_does_not_notify_at_the_default_setting(self):
        w = Watcher(self.store, FakePump("F"), {}, notifier=self.notifier)
        w.poll(reading(183))
        self.assertEqual([], self.notifier.sent)

    def test_high_pressure_is_an_alarm(self):
        self.assertEqual("alarm", alarms.describe(220, F)["severity"])

    def test_every_named_fault_is_an_alarm(self):
        for code in self.REAL_FAULTS:
            self.assertEqual("alarm", alarms.describe(code, F)["severity"], code)

    def test_high_pressure_wakes_somebody(self):
        w = Watcher(self.store, FakePump("F"), {}, notifier=self.notifier)
        w.poll(reading(220))
        self.assertEqual(2, self.notifier.sent[0]["priority"])

    def test_the_s_table_is_untouched(self):
        # 183 is a different alarm on the S series and was already info there
        # for its own reason; 220 is not in the S file's hand-reviewed set and
        # must not have been moved by this.
        self.assertIn("klimatsystem", alarms.describe(183, S)["text"])

    def test_the_meta_records_the_rule_and_every_code_it_moved(self):
        with open(alarms.TABLE_PATHS["F"], encoding="utf-8") as fh:
            meta = json.load(fh)["_meta"]
        moved = meta["hand_reclassified"]
        self.assertIn("rule", moved)
        self.assertEqual(set(str(c) for c in self.IN_PROGRESS), set(moved["to_info"]))
        self.assertEqual(set(str(c) for c in self.REAL_FAULTS), set(moved["to_alarm"]))
        for reason in list(moved["to_info"].values()) + list(moved["to_alarm"].values()):
            self.assertGreater(len(reason), 20, reason)


class TestCodesWithoutAText(AlarmTestCase):
    """Eleven codes in NIBE's table carry a severity and no text at all.

    Regression: they came back known: True with text "", which put an empty
    line in the notification and an empty line on the page. The number and the
    fact that NIBE publishes nothing for it is what the owner can actually use.
    """

    def _textless(self):
        return sorted(int(c) for c, e in alarms.load_table("S")["codes"].items()
                      if not (e.get("sv") or "").strip())

    def test_the_table_still_has_some(self):
        self.assertTrue(self._textless(), "the table changed shape; check this")

    def test_they_are_not_reported_as_known_with_an_empty_text(self):
        for code in self._textless():
            info = alarms.describe(code, S)
            self.assertTrue(info["text"].strip(), code)
            self.assertIn(str(code), info["text"], code)
            self.assertFalse(info["known"], code)

    def test_nibes_own_action_is_still_kept_where_there_is_one(self):
        with_action = [c for c in self._textless()
                       if alarms.load_table("S")["codes"][str(c)].get("action")]
        self.assertTrue(with_action)
        for code in with_action:
            self.assertEqual(alarms.describe(code, S)["action"],
                             alarms.load_table("S")["codes"][str(code)]["action"])


class TestSelfClearingCodes(AlarmTestCase):
    """A fault that clears itself must not repeat until acknowledged.

    183 is "Tillfälligt kom.fel mot klimatsystem 2", and NIBE's own text says
    the disturbance passes and the message resets itself. It was classed
    `alarm`, so it went out at Pushover priority 2 -- which repeats every five
    minutes until somebody acknowledges it -- for a fault that goes away on
    its own.
    """

    def test_183_is_not_an_alarm(self):
        self.assertEqual(alarms.describe(183, S)["severity"], "info")

    def test_and_so_it_does_not_wake_anybody_at_the_default_setting(self):
        w = self.watcher()          # alarm_min_severity defaults to "warning"
        w.poll(reading(183))
        self.assertEqual(self.notifier.sent, [])

    def test_a_real_alarm_still_does(self):
        w = self.watcher()
        w.poll(reading(163))
        self.assertEqual(self.notifier.sent[0]["priority"], 2)


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
        # 32196 carries mappings in the register map, so registry.Register.decode
        # hands back the mapped string and not 0/1 -- confirmed against the real
        # pump, where /api/register/32196 answers "No alarm". int("Alarm") used
        # to raise inside _current_codes and the whole fallback was dead code;
        # the test passed because it fed the register a 1.
        w = self.watcher()
        events = w.poll(reading(0, class1="Alarm"))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["code"], 0)
        self.assertFalse(events[0]["known"])
        self.assertIn("32196", events[0]["text"])
        # And it clears like any other.
        self.assertEqual([e["kind"] for e in w.poll(reading(0, class1="No alarm"))],
                         ["clear"])

    def test_the_flag_is_read_in_every_form_a_register_map_produces(self):
        # A CSV exported from the pump has no mappings, so the same register
        # arrives as a bare 0/1 there. Both have to work.
        for i, raised in enumerate((1, "1", True, "Alarm", "ALARM", "larm")):
            w = self.fresh_watcher("raised%d" % i)
            self.assertEqual([e["kind"] for e in w.poll(reading(0, class1=raised))],
                             ["alarm"], raised)
        for i, clear in enumerate((0, "0", False, "No alarm", "no alarm",
                                   "Inget larm", "")):
            w = self.fresh_watcher("clear%d" % i)
            self.assertEqual(w.poll(reading(0, class1=clear)), [], clear)

    def test_a_word_nobody_knows_is_not_turned_into_an_alarm(self):
        # Inventing an alarm out of a word we do not recognise would wake
        # somebody at three in the morning with no code behind it.
        w = self.watcher()
        self.assertEqual(w.poll(reading(0, class1="Ukendt tilstand")), [])


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
        info_code = next(c for c, e in alarms.load_table("S")["codes"].items()
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


class TestNothingToSendIsNotAQueue(AlarmTestCase):
    """The evening somebody finally pastes their Pushover keys into config.yaml.

    An event recorded while no notifier existed was never going to be sent. It
    used to be recorded as pending anyway, which meant _trim kept the twenty
    newest of them for ever and the first poll after configuring notifications
    delivered twenty months-old pushes at once -- half at priority 2, which
    repeats every five minutes until acknowledged.
    """

    def _flap(self, watcher, cycles):
        at = 1000.0
        for _ in range(cycles):
            watcher.poll(reading(163), at)
            at += 60
            watcher.poll(reading(0), at)
            at += 60
        return at

    def test_events_recorded_without_a_notifier_are_never_pending(self):
        w = self.silent_watcher()     # explicit None: no notifier at all
        self._flap(w, 15)
        with self.store._lock, self.store._db as c:             # noqa: SLF001
            pending = c.execute(
                "SELECT COUNT(*) FROM alarm_events WHERE sent = 0").fetchone()[0]
            total = c.execute("SELECT COUNT(*) FROM alarm_events").fetchone()[0]
        self.assertEqual(pending, 0)
        self.assertEqual(total, 30, "the history is kept either way")

    def test_configuring_notifications_later_sends_nothing_historical(self):
        w = self.silent_watcher()
        at = self._flap(w, 15)
        later = Watcher(self.store, FakePump(), {}, notifier=self.notifier)
        later.poll(reading(0), at)
        self.assertEqual(self.notifier.sent, [],
                         "twenty historical pushes, ten of them at priority 2")

    def test_and_the_log_is_not_filled_with_giving_up_on_them(self):
        w = self.silent_watcher()
        with self.assertLogs("nibelokal.alarms", level="WARNING") as logged:
            self._flap(w, 15)
        self.assertFalse([line for line in logged.output if "gave up" in line],
                         "nothing was ever meant to be sent")

    def test_the_next_real_alarm_still_goes_out(self):
        w = self.silent_watcher()
        at = self._flap(w, 15)
        later = Watcher(self.store, FakePump(), {}, notifier=self.notifier)
        later.poll(reading(0), at)
        later.poll(reading(163), at + 60)
        self.assertEqual(len(self.notifier.sent), 1)


class TestDebounce(AlarmTestCase):
    """A sensor right on the edge raises and clears on alternate polls."""

    def _flap(self, watcher, cycles, step=60.0, start=1000.0):
        at = start
        for _ in range(cycles):
            watcher.poll(reading(163), at)
            at += step
            watcher.poll(reading(0), at)
            at += step
        return at

    def test_a_flapping_alarm_is_bounded_not_two_pushes_a_minute(self):
        w = self.watcher()
        end = self._flap(w, 100)          # 200 transitions over 3 h 20
        hours = (end - 1000.0) / 3600.0
        self.assertLess(len(self.notifier.sent), 20 * hours,
                        "the flap guard is not holding")
        self.assertGreater(len(self.notifier.sent), 0, "and it is not silence")

    def test_the_first_news_is_never_delayed(self):
        w = self.watcher()
        w.poll(reading(163), 1000.0)
        self.assertEqual(len(self.notifier.sent), 1)

    def test_what_is_held_back_is_counted_in_the_next_message(self):
        w = self.watcher({"alarm_debounce_seconds": 900})
        self._flap(w, 20, start=1000.0)
        # Past the burst the transitions are held, not dropped; the next
        # message after the window rolls over says how many it stands for.
        w.poll(reading(163), 1000.0 + 4000)
        texts = " ".join(s["message"] for s in self.notifier.sent)
        self.assertIn("växlat", texts)

    def test_the_current_state_is_what_finally_goes_out(self):
        w = self.watcher({"alarm_debounce_seconds": 600})
        self._flap(w, 10, start=1000.0)
        w.poll(reading(163), 1000.0 + 5000)
        self.assertIn("larmar", self.notifier.sent[-1]["title"].lower())

    def test_debounce_survives_a_restart(self):
        # A pump that flaps is also a pump somebody power-cycles to fix it.
        w = self.watcher({"alarm_debounce_seconds": 900})
        self._flap(w, 10, start=1000.0)
        spent = len(self.notifier.sent)
        store = self.reopen()
        notifier = FakeNotifier()
        w2 = Watcher(store, FakePump(), {"alarm_debounce_seconds": 900},
                     notifier=notifier)
        self._flap(w2, 5, start=1000.0 + 1200)
        self.assertGreater(spent, 0)
        self.assertLessEqual(len(notifier.sent), alarms.NOTIFY_BURST,
                             "a restart handed the flap a fresh allowance")

    def test_a_flap_that_ends_cleared_still_sends_its_all_clear(self):
        """The mirror of test_the_current_state_is_what_finally_goes_out.

        Regression, and the worst kind: the pushes went *larmar / borta /
        larmar* and then stopped, with the pump no longer alarming and the
        owner's phone still saying it was. The final all-clear was held by the
        flap budget, and when the window rolled over _worth_sending() looked at
        the raise it belonged to, found it marked sent = 2 (superseded by the
        flap guard itself), read that as "nobody was told" and dropped the
        clear -- so `held` was never reported either.
        """
        w = self.watcher({"alarm_debounce_seconds": 900})
        at = self._flap(w, 6, start=1000.0)          # ends on a clear
        for _ in range(60):                          # an hour of nothing wrong
            w.poll(reading(0), at)
            at += 60
        self.assertIn("borta", self.notifier.sent[-1]["title"].lower(),
                      "the last thing the phone was told is that it alarms")
        self.assertIn("växlat", self.notifier.sent[-1]["message"],
                      "and it says how many transitions it stands for")

    def test_the_all_clear_of_a_flap_is_sent_once_not_once_per_poll(self):
        w = self.watcher({"alarm_debounce_seconds": 900})
        at = self._flap(w, 6, start=1000.0)
        for _ in range(120):
            w.poll(reading(0), at)
            at += 60
        clears = [m for m in self.notifier.sent if "borta" in m["title"].lower()]
        self.assertLessEqual(len(clears), alarms.NOTIFY_BURST)

    def test_a_flap_below_the_threshold_still_says_nothing(self):
        # The other half of the bargain: an all-clear for an alarm nobody was
        # ever told about is a message with no referent, whatever the flap
        # guard did with it.
        info_code = int(next(c for c, e in alarms.load_table("S")["codes"].items()
                             if e["severity"] == "info"))
        w = self.watcher({"alarm_min_severity": "alarm",
                          "alarm_debounce_seconds": 900})
        at = 1000.0
        for _ in range(6):
            w.poll(reading(info_code), at)
            at += 60
            w.poll(reading(0), at)
            at += 60
        for _ in range(60):
            w.poll(reading(0), at)
            at += 60
        self.assertEqual(self.notifier.sent, [])

    def test_a_debounce_of_zero_is_the_old_behaviour(self):
        w = self.watcher({"alarm_debounce_seconds": 0})
        self._flap(w, 3)
        self.assertEqual(len(self.notifier.sent), 6)


class TestRejectedConfiguration(AlarmTestCase):
    """A wrong token used to be a log line while the page said notify: true."""

    def test_a_rejected_send_is_remembered_in_swedish(self):
        w = self.watcher()
        self.notifier.fail_with = alarms.SendFailed(
            "pushover HTTP 400", permanent=True,
            user_sv="Pushover avvisade notisen (HTTP 400).")
        w.poll(reading(163))
        self.assertIsNotNone(w.notify_error)
        self.assertIn("Pushover", w.notify_error)

    def test_a_rate_limit_is_not_permanent(self):
        # 429 is Pushover's quota and their burst limit. Both lift by
        # themselves; treating them as permanent gives up on notifications for
        # good over a counter that resets at the start of the month.
        exc = self._http_error(429)
        self.assertFalse(exc.permanent)
        self.assertIn("429", exc.user_sv)

    def test_a_wrong_token_is_permanent_and_says_which_keys_to_check(self):
        for code in (400, 401, 403):
            exc = self._http_error(code)
            self.assertTrue(exc.permanent, code)
            self.assertIn("pushover_token", exc.user_sv, code)

    def test_a_request_timeout_is_retried(self):
        self.assertFalse(self._http_error(408).permanent)

    def test_a_server_error_is_retried(self):
        self.assertFalse(self._http_error(500).permanent)

    def _http_error(self, code):
        import urllib.error
        notifier = alarms.PushoverNotifier("t" * 30, "u" * 30)

        def fake_urlopen(request, timeout=None):
            raise urllib.error.HTTPError(notifier.url, code, "no", {}, None)

        original = alarms.urllib.request.urlopen
        alarms.urllib.request.urlopen = fake_urlopen
        try:
            with self.assertRaises(alarms.SendFailed) as caught:
                notifier.send("t", "m", 0)
        finally:
            alarms.urllib.request.urlopen = original
        return caught.exception

    def test_a_failing_notifier_is_cheap_after_the_first_failure(self):
        # This runs in the polling thread, and on macOS a DNS lookup with no
        # route out is not bounded by urlopen's timeout at all. The pump poll
        # is what must not be delayed.
        attempts = []

        class Slow:
            name = "slow"

            def send(self, *_a, **_k):
                attempts.append(1)
                raise alarms.SendFailed("wan down")

        w = self.watcher(notifier=Slow())
        at = 1000.0
        for _ in range(10):
            w.poll(reading(163 if len(attempts) % 2 == 0 else 0), at)
            at += 60
        self.assertLess(len(attempts), 10, "every poll paid the timeout again")
        self.assertGreater(len(attempts), 0)

    def test_the_backoff_lets_go_once_it_works_again(self):
        w = self.watcher()
        self.notifier.fail_with = alarms.SendFailed("network down")
        w.poll(reading(163), 1000.0)
        self.notifier.fail_with = None
        # Past the longest backoff, the pending notification goes out.
        w.poll(reading(163), 1000.0 + max(alarms.SEND_BACKOFF) + 1)
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(w._send_failures, 0)                   # noqa: SLF001


class TestTestNotification(AlarmTestCase):
    """The only other way to find out a token is wrong is to miss a real alarm."""

    def test_without_credentials_it_says_what_to_configure(self):
        w = self.silent_watcher()
        result = w.send_test_notification()
        self.assertFalse(result["ok"])
        self.assertFalse(result["notify"])
        self.assertIn("pushover_token", result["error"])

    def test_a_working_notifier_sends_one_push_at_priority_zero(self):
        w = self.watcher()
        result = w.send_test_notification()
        self.assertTrue(result["ok"])
        self.assertTrue(result["sent"])
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(self.notifier.sent[0]["priority"], 0,
                         "a test that repeats until acknowledged is a test "
                         "nobody runs twice")
        self.assertIn("test", self.notifier.sent[0]["message"].lower())

    def test_a_rejected_test_reports_the_reason_and_remembers_it(self):
        w = self.watcher()
        self.notifier.fail_with = alarms.SendFailed(
            "pushover HTTP 400", permanent=True, user_sv="Fel token.")
        result = w.send_test_notification()
        self.assertFalse(result["ok"])
        self.assertTrue(result["notify"])
        self.assertEqual(result["error"], "Fel token.")
        self.assertEqual(w.notify_error, "Fel token.")

    def test_a_successful_test_clears_an_earlier_rejection(self):
        w = self.watcher()
        w.notify_error = "Fel token."
        w._blocked_until = 1e12                                 # noqa: SLF001
        self.assertTrue(w.send_test_notification()["ok"])
        self.assertIsNone(w.notify_error)
        self.assertEqual(w._blocked_until, 0.0)                 # noqa: SLF001

    def test_a_notifier_that_explodes_is_still_an_answer(self):
        class Exploding:
            name = "boom"

            def send(self, *_a, **_k):
                raise RuntimeError("kaboom")

        result = self.watcher(notifier=Exploding()).send_test_notification()
        self.assertFalse(result["ok"])
        self.assertIn("kaboom", result["error"])


class TestClearWithoutARaise(AlarmTestCase):
    """"Larmet borta (105)" about an alarm nobody was told about."""

    def test_a_clear_is_not_pushed_when_the_alarm_never_was(self):
        info_code = int(next(c for c, e in alarms.load_table("S")["codes"].items()
                             if e["severity"] == "info"))
        w = self.watcher({"alarm_min_severity": "alarm"})
        w.poll(reading(info_code))
        w.poll(reading(0))
        self.assertEqual(self.notifier.sent, [],
                         "a message with no referent, arriving exactly when "
                         "the severity filter was doing its job")

    def test_but_the_events_are_both_in_the_history(self):
        info_code = int(next(c for c, e in alarms.load_table("S")["codes"].items()
                             if e["severity"] == "info"))
        w = self.watcher({"alarm_min_severity": "alarm"})
        w.poll(reading(info_code))
        w.poll(reading(0))
        self.assertEqual([row["kind"] for row in w.history(10)], ["clear", "alarm"])

    def test_a_clear_still_goes_out_when_the_alarm_did(self):
        w = self.watcher()
        w.poll(reading(163))
        w.poll(reading(0))
        self.assertEqual(len(self.notifier.sent), 2)
        self.assertIn("borta", self.notifier.sent[1]["title"].lower())


if __name__ == "__main__":
    unittest.main()
