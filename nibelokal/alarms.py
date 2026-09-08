"""Alarm watching: notice that the pump is complaining, and say so once.

This is the one thing myUplink gives away for nothing that this app has not
had, and it is the one that matters. A heat pump that has dropped to the
immersion heater keeps the house warm and says nothing; the first sign is the
electricity bill six weeks later. The pump has known since minute one -- the
alarm number sits in register 31976 -- so all that is missing is somebody
watching it.

Registers, verified against the S735 map in yozik04/nibe:

  31976  alarm number, s16, read-only. 0 = nothing wrong. No mappings, so it
         arrives as a plain integer.
  32196  "Class 1 alarm", u8, read-only, 0/1. Despite its address sitting next
         to the reset registers, this is a *flag*, not a reset: it says an
         alarm of class 1 exists, not which one. Used only to catch the case
         where the pump flags an alarm it will not name. It *does* carry
         mappings ({"0": "No alarm", "1": "Alarm"}), so registry.Register.decode
         hands it over as one of those strings and not as 0/1 -- confirmed on
         the real pump, where /api/register/32196 answers "No alarm". Both
         forms are accepted here; see _class1_raised.
  40023  "Reset alarm", writable. 45171 "Alarm Reset" on the F generation.

**This module never writes anything.** There is no write in it, no import of
anything that writes, and nothing in the watcher, the notifier or the poll
loop that could reach a register. An alarm this app silently cleared is an
alarm nobody ever learns about, and the pump would go on failing in exactly
the way that costs money.

That is a promise about this module and not about the app, and an earlier
version of this paragraph said "there is no code path that could", which was
not true: `/api/write` will write any writable register an owner asks it to,
and 40023 and 45171 are writable. What is actually enforced there is the tier.
Both are classified GUARDED in nibelokal/safety.py with the reason spelled out,
so a reset needs an explicit `confirm: true` from somebody who typed the
register number, and is refused outright when `allow_guarded_writes: false`.
Nothing resets an alarm by itself, and no button in the web app offers to.
Resetting is done deliberately, by a person who has read what the alarm says --
on the pump's own display, or through `/api/write` with the confirm that says
they meant it.

Edge triggering is the whole design. The poller runs every 60 seconds and a
standing alarm is present in every one of those readings; notifying on presence
rather than on the transition into presence is 1440 identical pushes a day,
which is indistinguishable from no alarm system at all. The transition is
therefore recorded in the same SQLite file as the history, so a restart -- and
this thing is meant to run for years on a Pi that reboots after a power cut --
does not re-announce an alarm the owner acknowledged last week.

Sending is off until Pushover credentials are configured, and an event recorded
while nothing was configured is recorded as *not sendable* rather than as
pending. That distinction is the difference between a quiet log and twenty
pushes -- half of them at priority 2, which repeats until acknowledged -- the
evening somebody finally pastes their Pushover keys into config.yaml.

Notification is rate limited per alarm code, because a pump can flap: a sensor
that is right on the edge raises and clears the same alarm on alternate polls,
which at a 60 s poll is two pushes a minute for as long as it lasts. At most
NOTIFY_BURST notifications per code go out inside one `alarm_debounce_seconds`
window -- enough for the honest life of an alarm: it appeared, it went away, it
came back. Further transitions are *held*, not dropped: they stay queued, and
when the window rolls over the newest one is sent with a line saying how many
transitions it stands for. The first news is never delayed, the current state
is never lost, and the pushes are bounded.

Config keys and defaults are in CONFIG_DEFAULTS below. They are all in
config.py's DEFAULTS as well, so they can be set in config.yaml (or through the
environment as NIBE_PUSHOVER_TOKEN and friends); tests/test_wiring.py asserts
the two agree, so changing a default here without changing it there fails the
suite rather than quietly running on a different number.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("nibelokal.alarms")

#: Canonical (S-series) addresses, like every other register number in this
#: app. The readings this module is handed come out of `pump.read_many`, which
#: is canonical in and canonical out, so nothing here has to translate to look
#: a value up -- see nibelokal/profile.py.
#:
#: What does have to translate is the *text*. Two of the sentences below name a
#: register number, and they are read by somebody standing in front of a pump:
#: on an F-series pump the alarm number is register 45001, not 31976, and
#: telling them to look at 31976 sends them looking for a register that pump
#: does not have. `_register_name` does that translation.
#:
#: The class 1 flag has no F-series equivalent at all -- no F map the `nibe`
#: package ships publishes one. That degrades by itself and needs no branch:
#: with no reading for 32196, `_current_codes` never consults the flag, and an
#: F-series alarm is caught by its number alone. What is lost is the ability to
#: notice an alarm the pump raises *without* naming it, which on the S series
#: is the only thing this flag is for.
R_ALARM = 31976       # alarm number
R_CLASS1 = 32196      # "Class 1 alarm" flag, 0/1

#: The alarm code table, per generation. **The numbers are not the same
#: alarms.** 301 code numbers appear in both files and exactly one of them
#: carries the same text in both; code 123 is "Ingen rumsgivare i kyla" on the
#: S series and "Givarfel: AZ2-BT23 uteluftsgivare" on the F. See
#: `_meta.numbering_warning` in alarms_f.json and docs/f-series.md.
#:
#: So the table is chosen by the pump's generation, and by nothing else. The
#: failure this prevents is not a missing text: it is a fluent, confident
#: Swedish sentence about a fault the pump does not have, arriving on
#: somebody's phone at three in the morning at Pushover priority 2. Where the
#: generation cannot be established the lookup is refused outright and the code
#: is reported as a number -- see describe().
_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
TABLE_PATHS = {"S": os.path.join(_DATA, "alarms_s.json"),
               "F": os.path.join(_DATA, "alarms_f.json")}

#: The S-series table's path, kept under its old name because it is what this
#: module has always called it. Nothing here reads it any more; the tables are
#: chosen by generation.
TABLE_PATH = TABLE_PATHS["S"]

#: Config keys this module reads, with the defaults it assumes when they are
#: absent. Repeated here rather than imported from config.py so that this
#: module works standalone; all of them are in config.DEFAULTS too (the ints in
#: INT_KEYS, the bools in BOOL_KEYS), and tests/test_wiring.py fails if the two
#: lists ever drift apart.
CONFIG_DEFAULTS = {
    # Both empty means no notifications are sent at all. That is the default.
    "pushover_token": "",
    "pushover_user": "",
    # A mute switch that does not require deleting the credentials.
    "alarm_notify": True,
    # info | warning | alarm -- the lowest severity worth a push.
    "alarm_min_severity": "warning",
    # Pushover priority 2 for severity "alarm": repeats until acknowledged.
    # Turn it off if being woken at 03:00 by a sensor fault is worse than
    # finding out at breakfast.
    "alarm_emergency_priority": True,
    # Priority 2 only: seconds between repeats, and how long to keep repeating.
    # Pushover's own limits are retry >= 30 and expire <= 10800.
    "alarm_retry_seconds": 300,
    "alarm_expire_seconds": 10800,
    # Flap guard. Inside one window of this length an alarm code may produce at
    # most NOTIFY_BURST notifications; the rest are held and collapsed into one
    # message when the window rolls over. Fifteen minutes with a burst of three
    # is at worst twelve pushes an hour, instead of the hundred and twenty a
    # sensor toggling on alternate 60 s polls produces -- and short enough that
    # a person who is watching the pump still sees the state change while they
    # are standing next to it. Measured: 200 transitions over 3 h 20 gave 39.
    "alarm_debounce_seconds": 900,
}

SEVERITIES = ("info", "warning", "alarm")

#: Unsent notifications waiting for the next poll. A pump that alarms while the
#: internet is down must not build an unbounded backlog that arrives all at
#: once three days later; past this many, the oldest are given up on and logged.
MAX_PENDING = 20

#: Notifications one alarm code may produce inside one debounce window. Three
#: is the honest life of an alarm -- it appeared, it went away, it came back --
#: and anything past that is a pump flapping, which is one story and not thirty.
NOTIFY_BURST = 3

#: Seconds to wait after the Nth consecutive failed send before trying again.
#: The first retry is immediate because the common failure is one dropped
#: packet on a home router. After that it backs off, because the second common
#: failure is the WAN being down -- and on macOS a DNS lookup with no route out
#: is not bounded by urlopen's timeout at all and can stall the polling thread
#: for 10-30 seconds *per attempt*. The pump poll is what must not be delayed.
SEND_BACKOFF = (0, 60, 300, 900)

#: Pushover's own limits. Anything longer is rejected, so truncate rather than
#: lose the message.
PUSHOVER_TITLE_MAX = 250
PUSHOVER_MESSAGE_MAX = 1024
PUSHOVER_RETRY_MIN = 30
PUSHOVER_EXPIRE_MAX = 10800

# Its own tables, created the same way store.py creates its own: idempotent
# CREATE ... IF NOT EXISTS run at startup, no migration machinery.
#
# `alarm_state` is what makes the trigger an edge rather than a level, and
# survives a restart. `alarm_events` is both the log and the retry queue: an
# event is written before anything is sent, so a crash mid-send loses a
# notification but never loses the fact that the alarm happened.
SCHEMA = """
CREATE TABLE IF NOT EXISTS alarm_state (
    code       INTEGER PRIMARY KEY,
    first_seen INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS alarm_events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       INTEGER NOT NULL,
    code     INTEGER NOT NULL,
    kind     TEXT NOT NULL,     -- 'alarm' (raised) or 'clear' (gone)
    severity TEXT,
    text     TEXT,
    -- 0 = still to send, 1 = sent, 2 = not sent and never will be. A 2 covers
    -- every way an event can be recorded without being pushed: no notifier was
    -- configured when it happened, it was below the severity threshold, it was
    -- superseded by a newer transition for the same code, Pushover rejected it,
    -- or it aged out of the queue. All of them mean the same thing to a reader:
    -- it is in the history, nobody was told.
    sent     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS alarm_events_ts ON alarm_events (ts);
CREATE INDEX IF NOT EXISTS alarm_events_unsent ON alarm_events (sent, id);

-- The flap guard's budget, one row per alarm code. On disk rather than in
-- memory because a pump that flaps is also a pump that gets power-cycled by
-- somebody trying to fix it, and a restart must not hand a flapping alarm a
-- fresh allowance of pushes every time.
CREATE TABLE IF NOT EXISTS alarm_notify_window (
    code         INTEGER PRIMARY KEY,
    window_start INTEGER NOT NULL,
    sent_count   INTEGER NOT NULL,
    -- Transitions folded into the next message: the number the notification
    -- reports as "it has switched N times since you last heard". Cleared when
    -- a message actually carries it, and so not reset with the window.
    held         INTEGER NOT NULL DEFAULT 0
);
"""

#: The loaded tables, by generation. Read once each and kept.
_tables: dict[str, dict] = {}


def generation_of(profile) -> str | None:
    """"S", "F", or None when it cannot be established.

    Takes a Profile, a bare "S"/"F", or None -- a Watcher may be built against
    a test double or an older Pump that has no profile at all, and that is the
    case this returns None for rather than assuming a generation.
    """
    if profile is None:
        return None
    gen = getattr(profile, "generation", profile)
    gen = str(gen or "").strip().upper()
    return gen if gen in TABLE_PATHS else None


def load_table(generation: str, path: str | None = None) -> dict:
    """The alarm code table for one generation, read once and kept.

    A missing or broken file is not fatal: without it every code is unknown,
    which still tells the owner the number and that something is wrong. Losing
    the alarm because the lookup table would not parse would be the wrong
    trade entirely.

    `path` is for the tests, which need to see what a broken file does without
    breaking the shipped one.
    """
    generation = str(generation or "").strip().upper()
    if generation not in TABLE_PATHS:
        raise ValueError("no alarm table for generation %r" % (generation,))
    if path is None and generation in _tables:
        return _tables[generation]
    try:
        with open(path or TABLE_PATHS[generation], encoding="utf-8") as fh:
            loaded = json.load(fh)
        codes = loaded.get("codes")
        if not isinstance(codes, dict):
            raise ValueError("no 'codes' object")
        table = loaded
    except Exception as exc:                              # noqa: BLE001
        log.warning("%s-series alarm code table unavailable (%s): every code "
                    "will be reported by number only", generation, exc)
        table = {"_meta": {"error": str(exc)}, "codes": {}}
    if path is None:
        _tables[generation] = table
    return table


def describe(code, profile=None) -> dict:
    """Look up one alarm number, in this pump's own generation. Never guesses.

    The result of this ends up in front of a person deciding whether to call an
    engineer at their own expense, so an unknown code says it is unknown and
    shows the number. A plausible-sounding invented text would be worse than
    nothing: it is the one failure mode where the app is confidently wrong
    about a thing the owner cannot check without the number.

    `profile` decides which table is read, and there is no default. An S-series
    text served for an F-series code is exactly the plausible-sounding invented
    text this function refuses to produce -- the two numberings share 301 code
    numbers and one text -- so with no profile the answer is the number and an
    honest sentence saying why there is nothing else. Falling back to the S
    table would be the same mistake in a more confident voice.
    """
    try:
        number = int(code)
    except (TypeError, ValueError):
        number = None

    generation = generation_of(profile)
    if generation is None:
        shown = number if number is not None else code
        return {
            "code": number,
            "severity": "warning",
            "text": "Larm %s. Appen vet inte vilken generation pumpen är (S "
                    "eller F), och larmnumren betyder olika saker på de två – "
                    "samma nummer är olika fel. Därför slås koden inte upp "
                    "alls." % shown,
            "action": "Sätt `generation` (eller `model`) i config.yaml, och "
                      "läs larmet på pumpens display så länge.",
            "known": False,
        }

    entry = None
    if number is not None:
        entry = load_table(generation)["codes"].get(str(number))
    # Eleven codes in NIBE's S-series table, and twelve in the F one, carry a
    # severity and an action but no text at all -- and four of the F ones have
    # no long text either, so for them there is nothing but the number.
    # Handing those over as known with text "" put an empty line
    # in the notification and an empty line on the page; what the owner can
    # actually use is the number and the fact that the table has nothing to
    # say about it. Severity and action are still NIBE's, so they are kept.
    if isinstance(entry, dict) and (entry.get("sv") or "").strip():
        return {
            "code": number,
            "text": entry.get("sv") or "",
            "severity": entry.get("severity") if entry.get("severity") in SEVERITIES
                        else "warning",
            "action": entry.get("action"),
            "known": True,
        }

    shown = number if number is not None else code
    fallback_action = ("Slå upp koden på pumpens display eller på "
                       "nibe.eu/sv-se/support/larmkoder.")
    if isinstance(entry, dict):
        # In the table, but without a text. Not the same as unknown, and said
        # as the different thing it is.
        return {
            "code": number,
            "severity": entry.get("severity") if entry.get("severity") in SEVERITIES
                        else "warning",
            "text": "Larm %s. NIBE publicerar ingen larmtext för den koden, så "
                    "appen kan inte säga vad den betyder." % shown,
            "action": entry.get("action") or fallback_action,
            "known": False,
        }
    return {
        "code": number,
        # Severity "warning" and not "alarm": we do not know which it is, and
        # saying "alarm" would be as much of a guess as the text would.
        "severity": "warning",
        "text": "Larm %s. Koden finns inte i appens tabell, så appen kan inte "
                "säga vad den betyder." % shown,
        "action": fallback_action,
        "known": False,
    }


# -- notifiers ---------------------------------------------------------------


class SendFailed(Exception):
    """A notification did not go out.

    `permanent` distinguishes "the network was down, try again in a minute"
    from "Pushover rejected this message and will reject it every time" --
    retrying the latter forever is how a queue stops draining.

    `user_sv` is the Swedish sentence to put in front of a person when the
    rejection is something they can fix, which is nearly always the case for a
    permanent one: a token pasted wrong is not a transient network event, and
    the only way anyone finds out is if the app says so on the page. None means
    "nothing worth showing"; the log line stands on its own.
    """

    def __init__(self, message: str, permanent: bool = False,
                 user_sv: str | None = None):
        super().__init__(message)
        self.permanent = permanent
        self.user_sv = user_sv


#: HTTP codes in the 4xx range that mean "later", not "never". 429 is
#: Pushover's monthly message quota and their per-second burst limit; both lift
#: by themselves.
RETRY_LATER_CODES = frozenset({408, 429})


def _pushover_hint(code: int) -> str | None:
    """What to tell the household about an HTTP code from Pushover."""
    if code in (401, 403):
        return ("Pushover avvisade inloggningen (HTTP %d). Kontrollera "
                "pushover_token och pushover_user i config.yaml." % code)
    if code == 429:
        return ("Pushover har tillfälligt stoppat notiserna (HTTP 429, för "
                "många meddelanden). Appen försöker igen av sig själv.")
    if 400 <= code < 500:
        return ("Pushover avvisade notisen (HTTP %d). Oftast är det fel "
                "pushover_token eller pushover_user i config.yaml." % code)
    return None


class PushoverNotifier:
    """Pushover, over urllib. No SDK, nothing to keep updated.

    A notifier is anything with a `name` and a `send(title, message, priority)`
    that raises SendFailed. That is the whole contract -- a second backend is
    another small class and one more branch in make_notifier(), not a plugin
    system.
    """

    name = "pushover"
    url = "https://api.pushover.net/1/messages.json"

    def __init__(self, token: str, user: str, retry_seconds: int = 300,
                 expire_seconds: int = 10800, timeout: float = 10.0):
        self.token = token
        self.user = user
        # Priority 2 without both of these is rejected by the API, so they are
        # clamped into Pushover's documented range here rather than trusted
        # from config -- retry: 30 s minimum, expire: 10800 s maximum.
        self.retry_seconds = max(PUSHOVER_RETRY_MIN, int(retry_seconds))
        self.expire_seconds = min(PUSHOVER_EXPIRE_MAX, max(PUSHOVER_RETRY_MIN,
                                                           int(expire_seconds)))
        self.timeout = timeout

    def send(self, title: str, message: str, priority: int = 0) -> None:
        priority = max(-2, min(2, int(priority)))
        fields = {
            "token": self.token,
            "user": self.user,
            "title": title[:PUSHOVER_TITLE_MAX],
            "message": message[:PUSHOVER_MESSAGE_MAX] or "(tom)",
            "priority": str(priority),
        }
        if priority == 2:
            fields["retry"] = str(self.retry_seconds)
            fields["expire"] = str(self.expire_seconds)

        data = urllib.parse.urlencode(fields).encode("utf-8")
        request = urllib.request.Request(
            self.url, data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")[:200]
            except Exception:                             # noqa: BLE001
                pass
            # Pushover documents 4xx as "do not retry this request" -- a bad
            # token or a malformed message will be bad next time too. With one
            # exception: 429 is their rate limit, which says "not now", not
            # "not ever". Treating it as permanent gives up on notifications
            # for good over a quota that resets at the start of every month.
            permanent = 400 <= exc.code < 500 and exc.code not in RETRY_LATER_CODES
            raise SendFailed("pushover HTTP %s: %s" % (exc.code, body),
                             permanent=permanent,
                             user_sv=_pushover_hint(exc.code))
        except Exception as exc:                          # noqa: BLE001
            raise SendFailed("pushover: %s" % exc)


def make_notifier(config: dict):
    """The configured notifier, or None when nothing is configured.

    None is the default and means the watcher still records every alarm; it
    just does not push. Nothing here reaches the network until somebody has put
    real credentials in the config file.
    """
    config = config or {}
    if not _flag(config.get("alarm_notify", CONFIG_DEFAULTS["alarm_notify"])):
        return None
    token = str(config.get("pushover_token") or "").strip()
    user = str(config.get("pushover_user") or "").strip()
    if not token or not user:
        return None
    return PushoverNotifier(
        token, user,
        retry_seconds=config.get("alarm_retry_seconds",
                                 CONFIG_DEFAULTS["alarm_retry_seconds"]),
        expire_seconds=config.get("alarm_expire_seconds",
                                  CONFIG_DEFAULTS["alarm_expire_seconds"]),
    )


# -- the watcher -------------------------------------------------------------


class Watcher:
    """Turns a stream of register readings into "this just started" events."""

    def __init__(self, store, pump, config: dict, notifier=None, profile=None):
        self.store = store
        self.pump = pump
        self.config = config or {}
        # Which generation's alarm table to read, and which register numbers to
        # quote in a notification. Taken from the pump, which always has one;
        # `profile` is for a caller that has one and no pump. None means the
        # codes are reported by number only -- see describe().
        self.profile = profile if profile is not None else getattr(pump, "profile", None)
        self.notifier = make_notifier(self.config) if notifier is None else notifier
        self.min_severity = str(self.config.get(
            "alarm_min_severity", CONFIG_DEFAULTS["alarm_min_severity"])).lower()
        if self.min_severity not in SEVERITIES:
            self.min_severity = CONFIG_DEFAULTS["alarm_min_severity"]
        self.emergency = _flag(self.config.get(
            "alarm_emergency_priority", CONFIG_DEFAULTS["alarm_emergency_priority"]))
        self.debounce_seconds = max(0, _int(
            self.config.get("alarm_debounce_seconds"),
            CONFIG_DEFAULTS["alarm_debounce_seconds"]))
        self.last_error: str | None = None
        # A configuration the notification service rejected, in Swedish, for
        # /api/alarms to show. A wrong token used to be a log line on a machine
        # nobody reads while the page went on claiming notifications were on.
        self.notify_error: str | None = None
        # Consecutive failed sends, and the instant the next attempt is allowed.
        # See SEND_BACKOFF: this runs inside the polling thread.
        self._send_failures = 0
        self._blocked_until = 0.0
        # Share the store's single connection and lock rather than opening a
        # second one: store.py opens exactly one for the reason written there,
        # and two writers on one WAL file only buys lock contention.
        with self.store._lock, self.store._db as c:       # noqa: SLF001
            c.executescript(SCHEMA)

    # -- polling ---------------------------------------------------------

    def poll(self, values: dict, now: float | None = None) -> list[dict]:
        """Given the latest readings, return the alarms that are news.

        Called from the polling loop, so it swallows everything. A watcher that
        can crash the poller takes the history and the web app down with it,
        and the alarm it was watching for goes unnoticed anyway.

        `now` exists so a test can walk a clock across a debounce window
        without sleeping through it; the poller never passes it.
        """
        at = time.time() if now is None else float(now)
        try:
            events = self._detect(values or {}, at)
        except Exception as exc:                          # noqa: BLE001
            self.last_error = str(exc)
            log.exception("alarm poll failed: %s", exc)
            return []
        self.last_error = None
        try:
            self._flush(at)
        except Exception as exc:                          # noqa: BLE001
            # Detection already succeeded and is on disk; a failed send round
            # is retried next poll rather than losing the events found above.
            log.warning("alarm notification round failed: %s", exc)
        return events

    def _detect(self, values: dict, now: float | None = None) -> list[dict]:
        now = int(time.time() if now is None else now)
        current = self._current_codes(values)
        if current is None:
            # No usable reading. "The pump did not answer" is not "the alarm
            # cleared", and reporting it as one would send an all-clear every
            # time the network hiccups.
            return []

        with self.store._lock, self.store._db as c:       # noqa: SLF001
            known = {row[0] for row in c.execute("SELECT code FROM alarm_state")}

            events = []
            for code in sorted(current - known):
                events.append(self._event(c, now, code, "alarm"))
                c.execute("INSERT OR REPLACE INTO alarm_state (code, first_seen, "
                          "last_seen) VALUES (?,?,?)", (code, now, now))
            for code in sorted(known - current):
                events.append(self._event(c, now, code, "clear"))
                c.execute("DELETE FROM alarm_state WHERE code = ?", (code,))
            if current & known:
                c.execute("UPDATE alarm_state SET last_seen = ? WHERE code IN (%s)"
                          % ",".join("?" * len(current & known)),
                          [now] + sorted(current & known))

            if events:
                self._trim(c)
        return events

    def _current_codes(self, values: dict) -> set[int] | None:
        """The alarm numbers standing right now, or None if we cannot tell."""
        row = values.get(R_ALARM)
        if not isinstance(row, dict) or "value" not in row:
            return None
        try:
            code = int(row["value"])
        except (TypeError, ValueError):
            return None

        codes = {code} if code > 0 else set()

        # 32196 is a flag, so it cannot name an alarm -- but it can catch one
        # that 31976 does not report, which would otherwise look like a healthy
        # pump. Code 0 is not a real NIBE code, so it cannot collide.
        flag = values.get(R_CLASS1)
        if not codes and isinstance(flag, dict) and "value" in flag:
            if _class1_raised(flag["value"]):
                codes = {0}
        return codes

    def _event(self, c, now: int, code: int, kind: str) -> dict:
        info = self._describe(code)
        severity = info["severity"] if kind == "alarm" else "info"
        # An event recorded while nothing was configured to send it was never
        # going to be sent, and saying otherwise is not free: it stays pending
        # for ever, _trim keeps the twenty newest of them, and the evening
        # somebody finally pastes their Pushover keys into config.yaml the
        # first poll delivers twenty months-old pushes at once -- half of them
        # at priority 2, which repeats every five minutes until acknowledged.
        # It is history either way; only the queue is affected.
        sent = 0 if self.notifier is not None else 2
        cur = c.execute(
            "INSERT INTO alarm_events (ts, code, kind, severity, text, sent) "
            "VALUES (?,?,?,?,?,?)", (now, code, kind, severity, info["text"], sent))
        log.warning("alarm %s: %d %s", kind, code, info["text"])
        return {
            "id": cur.lastrowid, "ts": now, "code": code, "kind": kind,
            "severity": severity, "text": info["text"], "action": info["action"],
            "known": info["known"],
        }

    def _register_name(self, address: int) -> int:
        """The number this pump's own display and paperwork use for `address`.

        A notification is read by somebody who is about to go and look at the
        pump, so it has to name the register they will find there. Falls back
        to the canonical number for a pump object that has no profile -- a test
        double, or a Watcher built against an older Pump.
        """
        return (self.profile.physical(address) if self.profile is not None
                else address)

    def _describe(self, code: int) -> dict:
        if code == 0:
            # The 32196 case. Truthful about what is and is not known.
            return {
                "code": 0, "severity": "alarm", "known": False,
                "text": "Pumpen rapporterar ett larm (register %d) men lämnar "
                        "inget larmnummer i register %d."
                        % (self._register_name(R_CLASS1),
                           self._register_name(R_ALARM)),
                "action": "Läs larmet på pumpens display – appen kan inte se "
                          "vilket det är.",
            }
        return describe(code, self.profile)

    def _trim(self, c) -> None:
        """Keep the unsent queue bounded.

        Everything is kept as history; only the *pending* ones are capped. An
        alarm from three days ago that nobody could be told about is not worth
        pushing now, and a queue that only grows is a queue that eventually
        arrives all at once.

        Nothing is pending unless a notifier existed when it was recorded (see
        _event), so this no longer fires -- and no longer fills the log with
        "gave up on N unsent alarm notifications" -- on an installation that
        never had any notifications to give up on.
        """
        stale = [r[0] for r in c.execute(
            "SELECT id FROM alarm_events WHERE sent = 0 ORDER BY id DESC "
            "LIMIT -1 OFFSET ?", (MAX_PENDING,))]
        if stale:
            c.execute("UPDATE alarm_events SET sent = 2 WHERE id IN (%s)"
                      % ",".join("?" * len(stale)), stale)
            log.warning("gave up on %d unsent alarm notifications (queue full)",
                        len(stale))

    # -- sending ---------------------------------------------------------

    def _flush(self, now: float | None = None) -> None:
        """Send whatever is still unsent, oldest first, at a bounded rate.

        Three things happen here beyond "send the queue", and each of them is a
        way a notification system stops being usable:

        * **Backoff.** This runs in the polling thread. A send that fails is
          not retried on the very next poll for ever; see SEND_BACKOFF.
        * **Coalescing.** Several pending events for one code are one story.
          Only the newest is sent -- it is the current state -- and the older
          ones are marked as superseded, with the count carried into the
          message so nothing is silently swallowed.
        * **The flap budget.** NOTIFY_BURST notifications per code per
          `alarm_debounce_seconds`. Past that the code's events stay pending
          and go out when the window rolls over, which is what turns a sensor
          toggling on alternate polls from a hundred pushes into four.
        """
        at = time.time() if now is None else float(now)
        if self.notifier is None:
            return
        if at < self._blocked_until:
            # Still inside the backoff after a failed send. The events stay
            # pending; this poll costs nothing at all.
            return
        with self.store._lock, self.store._db as c:       # noqa: SLF001
            pending = c.execute(
                "SELECT id, ts, code, kind, severity, text FROM alarm_events "
                "WHERE sent = 0 ORDER BY id LIMIT ?", (MAX_PENDING,)).fetchall()
            windows = {row[0]: (row[1], row[2], row[3]) for row in c.execute(
                "SELECT code, window_start, sent_count, held "
                "FROM alarm_notify_window")}
        if not pending:
            return

        groups: dict = {}
        for row in pending:
            groups.setdefault(row[2], []).append(row)

        for code, rows in groups.items():
            start, count, held = windows.get(code, (0, 0, 0))
            if at - start >= self.debounce_seconds:
                # The window has rolled over. `held` is deliberately not reset
                # with it: it counts transitions nobody has been told about
                # yet, and it is cleared when a message finally carries it.
                start, count = 0, 0

            row_id, ts, _code, kind, severity, text = rows[-1]
            superseded = rows[:-1]
            for older in superseded:
                # Several pending events for one code are one story, and only
                # the newest is the current state. The older ones stay in the
                # history as events; they are just not each their own push.
                self._mark(older[0], 2)
            held += len(superseded)

            if count >= NOTIFY_BURST:
                # Over the flap budget. Exactly one event per code stays
                # pending -- the newest -- and goes out when the window rolls
                # over, carrying the count of what it stands for. Collapsing
                # here rather than leaving the whole run pending is what keeps
                # a flap from filling the queue and being trimmed away.
                self._remember(code, start, count, held)
                continue

            if not self._worth_sending(row_id, code, kind, severity):
                # Below the configured threshold, or an all-clear for an alarm
                # nobody was ever told about. Still in the log, never sent.
                self._mark(row_id, 2)
                self._remember(code, start, count, held)
                continue
            title, message, priority = self._compose(
                ts, code, kind, severity, text, held=held)
            try:
                self.notifier.send(title, message, priority)
            except SendFailed as exc:
                log.warning("alarm notification not sent (%s): %s",
                            "given up" if exc.permanent else "will retry", exc)
                if exc.permanent:
                    # A rejected configuration, nearly always. Remember why in
                    # Swedish so /api/alarms can put it in front of a person;
                    # a log line on a machine in a cupboard is not telling
                    # anybody that their notifications are off.
                    self.notify_error = exc.user_sv or str(exc)
                    self._mark(row_id, 2)
                    continue
                self._failed_send(at)
                # Left at sent = 0 for the next poll. Stop the round here: if
                # this one failed on the network, the rest will fail too.
                return
            except Exception as exc:                      # noqa: BLE001
                # A notifier that raises something else is a bug in the
                # notifier, not a reason to lose the event.
                log.warning("alarm notification failed unexpectedly: %s", exc)
                self._failed_send(at)
                return
            self._mark(row_id, 1)
            # The message carried `held`, so the count starts again at zero.
            self._remember(code, start or at, count + 1, 0)
            self._sent_ok()

    def _failed_send(self, now: float | None = None) -> None:
        """Note a failed send and set the earliest time to try again.

        `now` is the same clock _flush was called with, not time.time(): the
        two must agree or the backoff is measured against a different clock
        from the one that checks it.
        """
        at = time.time() if now is None else float(now)
        self._send_failures += 1
        wait = SEND_BACKOFF[min(self._send_failures - 1, len(SEND_BACKOFF) - 1)]
        self._blocked_until = at + wait

    def _sent_ok(self) -> None:
        self._send_failures = 0
        self._blocked_until = 0.0
        self.notify_error = None

    def _remember(self, code: int, start: float, count: int, held: int) -> None:
        """Write back this code's flap budget: window, pushes spent, held."""
        with self.store._lock, self.store._db as c:       # noqa: SLF001
            c.execute("INSERT OR REPLACE INTO alarm_notify_window "
                      "(code, window_start, sent_count, held) VALUES (?,?,?,?)",
                      (code, int(start), int(count), int(held)))

    def _worth_sending(self, row_id: int, code: int, kind: str,
                       severity: str) -> bool:
        if kind == "clear":
            # An all-clear goes out only if the alarm itself did. Being told
            # the pump alarmed and never told it recovered leaves a person
            # waiting, which is why this used to be unconditional -- but the
            # other half of that bargain is that "Larmet borta (105)" about an
            # alarm nobody was ever told about is a message with no referent,
            # and it arrives precisely when the severity filter was doing its
            # job.
            return self._raise_was_sent(row_id, code)
        try:
            return SEVERITIES.index(severity) >= SEVERITIES.index(self.min_severity)
        except ValueError:
            return True

    def _raise_was_sent(self, row_id: int, code: int) -> bool:
        """Does somebody still believe this code is alarming?

        The question an all-clear has to answer is not "was the raise it
        belongs to sent" but "what does the phone in the owner's pocket
        currently say". Those come apart on a flapping alarm: the flap guard
        marks the raise it collapsed as sent = 2 (superseded), and reading that
        as "nobody was told" dropped the final all-clear of a flap that ended
        clear. The pushes then read *larmar / borta / larmar* and stopped, with
        the pump no longer alarming and the owner's phone saying it was -- the
        one outcome the module docstring promises cannot happen.

        So: what actually reached anyone last decides. A raise that reached
        them means the all-clear is news, whatever became of the raise's own
        row afterwards.
        """
        try:
            with self.store._lock, self.store._db as c:   # noqa: SLF001
                raised = c.execute(
                    "SELECT id FROM alarm_events WHERE code = ? AND "
                    "kind = 'alarm' AND id < ? ORDER BY id DESC LIMIT 1",
                    (code, row_id)).fetchone()
                told = c.execute(
                    "SELECT kind FROM alarm_events WHERE code = ? AND id < ? "
                    "AND sent = 1 ORDER BY id DESC LIMIT 1",
                    (code, row_id)).fetchone()
        except Exception as exc:                          # noqa: BLE001
            log.warning("could not tell whether alarm %d was announced: %s",
                        code, exc)
            return True
        # No raise on record at all (a database from an older version, a row
        # pruned away) is not evidence either way, and silence is the worse
        # error of the two.
        if raised is None:
            return True
        # Nothing about this code has ever been sent: the severity filter did
        # its job on the raise, and "Larmet borta (105)" would be a message
        # with no referent.
        if told is None:
            return False
        return told[0] == "alarm"

    # -- a notification somebody asked for --------------------------------

    def send_test_notification(self) -> dict:
        """Send one push on demand, and say plainly what happened.

        The only way to find out that a token was pasted with a character
        missing used to be to wait for a real alarm and then not hear about it.
        Priority 0 whatever the config says: a test that repeats every five
        minutes until acknowledged is a test nobody runs twice.
        """
        if self.notifier is None:
            return {"ok": False, "sent": False, "notify": False,
                    "error": "Inga notiser är konfigurerade. Sätt "
                             "pushover_token och pushover_user i config.yaml "
                             "(och alarm_notify: true)."}
        where = getattr(self.pump, "host", "") or ""
        try:
            self.notifier.send(
                "Testnotis från nibe-lokal",
                "Notiserna fungerar. Det här är ett test, pumpen%s larmar inte."
                % (" på %s" % where if where else ""), 0)
        except SendFailed as exc:
            self.notify_error = exc.user_sv or str(exc)
            log.warning("test notification failed: %s", exc)
            return {"ok": False, "sent": False, "notify": True,
                    "error": self.notify_error}
        except Exception as exc:                          # noqa: BLE001
            log.warning("test notification failed unexpectedly: %s", exc)
            return {"ok": False, "sent": False, "notify": True,
                    "error": "Notisen kunde inte skickas: %s" % exc}
        # A working send also clears a backoff and a stale rejection: the
        # person just proved both wrong.
        self._sent_ok()
        return {"ok": True, "sent": True, "notify": True, "error": None}

    def _compose(self, ts: int, code: int, kind: str, severity: str,
                 text: str, held: int = 0) -> tuple[str, str, int]:
        where = getattr(self.pump, "host", "") or ""
        named = code > 0
        # `held` is how many transitions for this code were folded into this
        # one message. Said out loud rather than hidden: "it has come and gone
        # nine times" is the most useful sentence in the whole notification,
        # because a flapping alarm and a standing one need different actions.
        flapping = ("" if not held else
                    "Larmet har växlat %d gånger till sedan förra notisen – "
                    "det här är det senaste läget." % held)
        if kind == "clear":
            title = "Larmet borta (%d)" % code if named else "Larmet borta"
            body = ["Pumpen%s larmar inte längre." % (" på %s" % where if where else "")]
            if text:
                body.append("Larmet var: %s" % text)
            if flapping:
                body.append(flapping)
            body.append("Kontrollera ändå att den värmer som den ska – ett larm "
                        "som försvinner av sig självt brukar komma tillbaka.")
            return title, "\n\n".join(body), 0

        info = self._describe(code)
        title = ("Värmepumpen larmar: %d" % code if named
                 else "Värmepumpen larmar (utan larmnummer)")
        body = [text or ""]
        if info.get("action"):
            body.append(info["action"])
        if flapping:
            body.append(flapping)
        if where:
            body.append("Pump: %s" % where)
        priority = 2 if (severity == "alarm" and self.emergency) else (
            1 if severity in ("alarm", "warning") else 0)
        return title, "\n\n".join(p for p in body if p), priority

    def _mark(self, row_id: int, sent: int) -> None:
        with self.store._lock, self.store._db as c:       # noqa: SLF001
            c.execute("UPDATE alarm_events SET sent = ? WHERE id = ?", (sent, row_id))

    # -- reading ---------------------------------------------------------

    def active(self) -> list[dict]:
        """The alarms standing right now, as far as the database knows."""
        try:
            with self.store._lock, self.store._db as c:   # noqa: SLF001
                rows = c.execute("SELECT code, first_seen, last_seen FROM "
                                 "alarm_state ORDER BY code").fetchall()
        except Exception as exc:                          # noqa: BLE001
            log.warning("could not read alarm state: %s", exc)
            return []
        out = []
        for code, first_seen, last_seen in rows:
            entry = dict(self._describe(code))
            entry.update({"code": code, "first_seen": first_seen,
                          "last_seen": last_seen})
            out.append(entry)
        return out

    def history(self, limit: int = 50) -> list[dict]:
        """Recent alarm and all-clear events, newest first."""
        try:
            with self.store._lock, self.store._db as c:   # noqa: SLF001
                rows = c.execute(
                    "SELECT ts, code, kind, severity, text, sent FROM alarm_events "
                    "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        except Exception as exc:                          # noqa: BLE001
            log.warning("could not read alarm history: %s", exc)
            return []
        keys = ["ts", "code", "kind", "severity", "text", "sent"]
        return [dict(zip(keys, r)) for r in rows]


#: What register 32196 reads as when an alarm is standing, in both the forms it
#: arrives in. The pump's register map carries mappings for this register, so
#: registry.Register.decode returns the *mapped string* -- "Alarm" or "No
#: alarm" on the S735 map, verified against the real pump -- and int() on that
#: raises. A register map without mappings (a CSV exported from the pump, an
#: older map) still yields the raw 0/1, so both are handled.
_CLASS1_RAISED = ("alarm", "larm", "1", "true", "on", "yes")
_CLASS1_CLEAR = ("no alarm", "inget larm", "0", "false", "off", "no", "")


def _class1_raised(value) -> bool:
    """True when the class 1 flag says an alarm is standing.

    Anything unrecognised is False and logged rather than guessed: this flag's
    whole job is to catch an alarm with no number, and inventing one from a
    word we do not know would put a notification with no code behind it in
    front of somebody at three in the morning.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return int(value) == 1
    text = str(value or "").strip().lower()
    if text in _CLASS1_RAISED:
        return True
    if text not in _CLASS1_CLEAR:
        log.warning("register %d answered %r, which is neither an alarm nor an "
                    "all-clear in any wording this app knows; treated as no "
                    "alarm", R_CLASS1, value)
    return False


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _flag(value) -> bool:
    """Booleans out of a config file, where `"false"` is a truthy string."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)
