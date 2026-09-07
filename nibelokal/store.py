"""History: a background poller and a SQLite table of readings.

myUplink's paid tier sells you history. This is that history, in a file you own,
at whatever resolution you configure.

**What it costs.** One row per register per poll, and nothing is aggregated on
the way in. The shipped DASHBOARD is 25 registers and `poll_seconds` is 60, so:

    rows = registers x 86400 / poll_seconds x days

which is 36 000 rows a day and 13.1 million a year -- measured by building the
table, not estimated. On disk that came out between 57 and 83 bytes a row
depending on how SQLite's pages fill, so roughly 0.75-1.1 GB a year, and about
the same again in the steady state at the shipped `history_days: 400`. That is
a real amount of an SD card on a Pi, and the three levers are the three terms
of the formula: fewer registers in DASHBOARD, a longer `poll_seconds`, or a
shorter `history_days`. Nothing here thins old rows behind the owner's back:
somebody working out why the immersion heater ran at three in the morning last
February wants the minute the pump has, not an hourly average of it, and that
is not a trade this file gets to make on their behalf.

**What it must not cost.** Asking the table a question must not scan it. Every
/api/heating, /api/advice and /api/autotune request asks how far back the
history reaches, and it used to ask with COUNT(*) alongside MIN(ts) and
MAX(ts) -- one statement, and that statement is a full scan: 1.4 s at 13
million rows, taken while holding `_lock`, which is the lock the poller needs
to write the next reading. span() answers the same question off readings_ts in
microseconds; see it and stats() below.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time

log = logging.getLogger("nibelokal.store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    ts      INTEGER NOT NULL,
    address INTEGER NOT NULL,
    value   REAL,
    text    TEXT
);
CREATE INDEX IF NOT EXISTS readings_addr_ts ON readings (address, ts);
-- prune() deletes by time alone; without this it full-scans the table once a
-- day while holding the write lock, which on a Pi with millions of rows is
-- long enough to make concurrent writes time out.
CREATE INDEX IF NOT EXISTS readings_ts ON readings (ts);

CREATE TABLE IF NOT EXISTS writes (
    ts        INTEGER NOT NULL,
    address   INTEGER NOT NULL,
    title     TEXT,
    before    TEXT,
    requested TEXT,
    after     TEXT,
    tier      TEXT,
    source    TEXT
);
CREATE INDEX IF NOT EXISTS writes_ts ON writes (ts);

-- The indoor temperature does not come from the pump: an S-series with no room
-- sensor answers 40203 = 0, and the number comes from Homey instead. It is
-- kept out of `readings` on purpose -- that table is keyed by Modbus address,
-- and inventing an address for something the pump has never heard of would put
-- a fictional register in every backup and every /api/history query.
CREATE TABLE IF NOT EXISTS indoor (
    ts     INTEGER NOT NULL,
    value  REAL NOT NULL,
    source TEXT
);
CREATE INDEX IF NOT EXISTS indoor_ts ON indoor (ts);
"""

# Registers the autotune history is built from. Named here rather than imported
# from advisor to keep store.py free of the analysis modules; they are asserted
# to agree in tests/test_wiring.py.
R_OUTDOOR = 30002
R_SUPPLY = 30006
R_DEGREE_MINUTES = 40012
# Text, not a number: the register carries mappings, so registry.Register.decode
# hands back the mapped English string from the package map -- "Hot Water",
# "Heat", "Pool", "Cooling", "Off" -- and not 20/30/40. A CSV exported from a
# Swedish pump can spell the same states in Swedish, which is why autotune
# matches these case-insensitively as substrings in both languages.
R_PRIORITY = 31029        # what the compressor is working on


class Store:
    def __init__(self, path: str, history_days: int = 400):
        self.path = path
        self.history_days = history_days
        # One connection guarded by a lock, not one per thread.
        # ThreadingHTTPServer starts a fresh thread per request, so a
        # thread-local connection is really a connection per request that is
        # never closed -- two file descriptors each. Measured: the server hit
        # macOS's 256-descriptor limit after about 160 requests and started
        # answering "unable to open database file". The writes here take
        # milliseconds, so serialising them costs nothing worth having.
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=10000")
        with self._lock, self._db as c:
            c.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- writing ---------------------------------------------------------

    def record(self, readings: dict[int, dict], ts: int | None = None) -> None:
        ts = ts or int(time.time())
        rows = []
        for address, row in readings.items():
            if "error" in row:
                continue
            v = row.get("value")
            if isinstance(v, bool):
                rows.append((ts, address, float(v), None))
            elif isinstance(v, (int, float)):
                rows.append((ts, address, float(v), None))
            elif isinstance(v, str):
                rows.append((ts, address, None, v))
        if not rows:
            return
        with self._lock, self._db as c:
            c.executemany("INSERT INTO readings (ts, address, value, text) VALUES (?,?,?,?)", rows)

    def record_write(self, result: dict, source: str = "web") -> None:
        with self._lock, self._db as c:
            c.execute(
                "INSERT INTO writes (ts, address, title, before, requested, after, tier, source) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (int(time.time()), result.get("address"), result.get("title"),
                 str(result.get("before")), str(result.get("requested")),
                 str(result.get("after")), result.get("tier"), source),
            )

    def record_indoor(self, value: float, ts: int | None = None,
                      source: str = "homey") -> None:
        """Store one indoor temperature. Only ever called with a fresh reading.

        Staleness is the caller's judgement, not this table's: a sensor with a
        flat battery keeps serving its last value forever, and six hours of a
        repeated number would read here as six hours of a very steady house.
        """
        with self._lock, self._db as c:
            c.execute("INSERT INTO indoor (ts, value, source) VALUES (?,?,?)",
                      (int(ts or time.time()), float(value), source))

    def prune(self) -> int:
        cutoff = int(time.time()) - self.history_days * 86400
        with self._lock, self._db as c:
            cur = c.execute("DELETE FROM readings WHERE ts < ?", (cutoff,))
            pruned = cur.rowcount
            # Same retention for the indoor feed: it is history in the same
            # sense, and a table nobody prunes is a disk that fills up in a
            # year nobody is watching.
            c.execute("DELETE FROM indoor WHERE ts < ?", (cutoff,))
            # And the alarm log, which alarms.py creates in this same file. It
            # is small -- a working pump writes nothing to it for months -- but
            # a flapping sensor writes two rows a minute, and nothing else was
            # ever going to delete them. Guarded because these tables only
            # exist once alarms.Watcher has been built at least once against
            # this database; a store with no watcher must still prune.
            for table in ("alarm_events", "alarm_state"):
                try:
                    c.execute("DELETE FROM %s WHERE %s < ?"
                              % (table, "ts" if table == "alarm_events"
                                 else "last_seen"), (cutoff,))
                except sqlite3.OperationalError:
                    # No such table: alarms have never run here.
                    pass
        return pruned

    # -- reading ---------------------------------------------------------

    def series(self, address: int, hours: int = 24, buckets: int = 240) -> list[list]:
        """Downsampled series: [[unix_seconds, value], ...]."""
        since = int(time.time()) - hours * 3600
        width = max(60, hours * 3600 // max(1, buckets))
        with self._lock, self._db as c:
            rows = c.execute(
                "SELECT (ts / ?) * ? AS bucket, AVG(value) FROM readings "
                "WHERE address = ? AND ts >= ? AND value IS NOT NULL "
                "GROUP BY bucket ORDER BY bucket",
                (width, width, address, since),
            ).fetchall()
        return [[int(b), round(v, 2)] for b, v in rows if v is not None]

    def last_writes(self, limit: int = 50) -> list[dict]:
        with self._lock, self._db as c:
            rows = c.execute(
                "SELECT ts, address, title, before, requested, after, tier, source "
                "FROM writes ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        keys = ["ts", "address", "title", "before", "requested", "after", "tier", "source"]
        return [dict(zip(keys, r)) for r in rows]

    def autotune_history(self, days: int = 30) -> list[dict]:
        """History in the shape autotune.analyse() eats: one dict per hour.

        Keys are autotune.Sample's field names -- ts, outdoor, indoor, supply,
        degree_minutes, compressor -- so the result goes straight into
        analyse() without a translation step in the caller.

        Bucketed to the hour, because the two sources do not share a clock.
        Pump readings land on the poll interval; the indoor feed is written
        when Homey last answered, behind a cache of its own, so an exact join
        on `ts` would match almost nothing. An hour is small enough for
        autotune (its night window is eight hours and it wants at least three
        samples in one) and far shorter than a house's thermal time constant,
        so nothing is lost by averaging inside it.

        Hours with no indoor reading are still returned. autotune counts them
        as "no_indoor" and can then say the pump data is fine and the room data
        is missing, which is a different problem from having no history at all.

        `compressor` is the state the hour ended in and `compressor_states` is
        every distinct state seen inside it, oldest first. Both, because they
        answer different questions: the first is "what was it doing", and the
        second is "is this hour evidence about the heating curve at all". An
        hour that made hot water from 10:00 to 10:40 and then heated is not,
        and the last state alone cannot say so.
        """
        since = int(time.time()) - max(3600, int(days) * 86400)
        rows: dict[int, dict] = {}

        def bucket(ts: int) -> dict:
            return rows.setdefault(ts, {"ts": float(ts), "outdoor": None, "indoor": None,
                                        "supply": None, "degree_minutes": None,
                                        "compressor": None, "compressor_states": []})

        with self._lock, self._db as c:
            for key, address in (("outdoor", R_OUTDOOR), ("supply", R_SUPPLY),
                                 ("degree_minutes", R_DEGREE_MINUTES)):
                for ts, value in c.execute(
                        "SELECT (ts / 3600) * 3600 AS bucket, AVG(value) FROM readings "
                        "WHERE address = ? AND ts >= ? AND value IS NOT NULL "
                        "GROUP BY bucket", (address, since)):
                    if value is not None:
                        bucket(int(ts))[key] = float(value)

            # Grouped by (hour, text) rather than by hour alone, so an hour
            # that changed its mind reports every state it was in. Ordered by
            # each state's last appearance, so the final element is the state
            # the hour ended in -- which is what `compressor` keeps, and it is
            # SQLite's documented behaviour for a bare column beside MAX().
            for ts, text, _ in c.execute(
                    "SELECT (ts / 3600) * 3600 AS bucket, text, MAX(ts) AS last "
                    "FROM readings WHERE address = ? AND ts >= ? AND text IS NOT NULL "
                    "GROUP BY bucket, text ORDER BY bucket, last", (R_PRIORITY, since)):
                row = bucket(int(ts))
                row["compressor_states"].append(text)
                row["compressor"] = text

            for ts, value in c.execute(
                    "SELECT (ts / 3600) * 3600 AS bucket, AVG(value) FROM indoor "
                    "WHERE ts >= ? GROUP BY bucket", (since,)):
                if value is not None:
                    # Only on hours the pump was also seen: an indoor reading
                    # with no pump data behind it cannot say anything about the
                    # curve, and autotune would drop it for want of an outdoor
                    # temperature anyway.
                    row = rows.get(int(ts))
                    if row is not None:
                        row["indoor"] = float(value)

        return [rows[ts] for ts in sorted(rows)]

    def span(self) -> tuple:
        """(first ts, last ts) of the history, or (None, None). Index only.

        Bare MIN(ts) and MAX(ts) are answered from readings_ts in microseconds;
        asking for COUNT(*) in the same statement turns both into a full scan
        of the table -- 1.3 s at a year of data, taken with `_lock` held, which
        means the poller waits for it. This is what the callers that only want
        "how long have we been recording" ask for, and it is on every request
        the page makes: /api/heating, /api/advice and /api/autotune all reach
        it through advisor._history_hours.
        """
        with self._lock, self._db as c:
            first = c.execute("SELECT MIN(ts) FROM readings").fetchone()[0]
            last = c.execute("SELECT MAX(ts) FROM readings").fetchone()[0]
        return first, last

    def stats(self) -> dict:
        """Row count and span. Same keys as before; three statements, not one.

        COUNT(*) alone is a covering-index count SQLite does in a tenth of the
        time the combined statement took, and the two extremes come off the
        index for nothing. Only /api/backups asks for the count, and nobody
        polls that.
        """
        with self._lock, self._db as c:
            n = c.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
            first = c.execute("SELECT MIN(ts) FROM readings").fetchone()[0]
            last = c.execute("SELECT MAX(ts) FROM readings").fetchone()[0]
        return {"rows": n or 0, "first": first, "last": last}


class Poller(threading.Thread):
    """Reads the dashboard registers on a timer and stores them."""

    daemon = True

    def __init__(self, pump, store: Store, addresses: list[int], seconds: int = 60,
                 backup_dir: str | None = None, backup_hours: float = 24.0,
                 watcher=None, indoor=None):
        super().__init__(name="nibe-poller")
        self.pump = pump
        self.store = store
        self.addresses = addresses
        self.seconds = max(15, seconds)
        self.latest: dict[int, dict] = {}
        self.last_ok: float | None = None
        self.last_error: str | None = None
        self.backup_dir = backup_dir
        self.backup_hours = backup_hours
        self.last_backup: str | None = None
        # Optional passengers. Both default to None, so a Poller built the way
        # it was before this existed behaves exactly as it did.
        self.watcher = watcher            # alarms.Watcher, or None
        self.indoor = indoor              # homey.Homey, or None
        # Kept apart from last_error on purpose: one means "the pump did not
        # answer", the other "the pump answered and the database would not take
        # it". They call for different fixes and only one of them stops the
        # heating being watched.
        self.last_store_error: str | None = None
        self.last_watch_error: str | None = None
        self.last_indoor_error: str | None = None
        self._last_indoor_at: int | None = None
        # Not _stop: threading.Thread has a private _stop() of its own, and
        # shadowing it with an Event makes join(timeout=...) raise TypeError
        # the moment the thread has actually finished.
        self._halt = threading.Event()
        self._pruned = 0.0

    def stop(self) -> None:
        self._halt.set()

    def run(self) -> None:
        backoff = self.seconds
        while not self._halt.is_set():
            polled = False
            try:
                data = self.pump.read_many(self.addresses)
            except Exception as exc:                       # noqa: BLE001
                self.last_error = str(exc)
                log.warning("poll failed: %s", exc)
                # The pump drops sessions now and then; back off rather than hammer.
                backoff = min(backoff * 2, 600)
            else:
                self.latest = data
                self.last_ok = time.time()
                self.last_error = None
                backoff = self.seconds
                polled = True
                # Storing is a separate failure from reading, and used to be
                # the same one. A locked or full database put the sqlite
                # message in `last_error`, doubled the poll interval to ten
                # minutes though the pump was answering perfectly, handed the
                # alarm watcher an empty dict -- so alarms went unnoticed --
                # and suppressed the daily backup. The reading is in hand;
                # losing a row of history is the smallest of those.
                try:
                    self.store.record(data)
                    self.last_store_error = None
                except Exception as exc:                   # noqa: BLE001
                    self.last_store_error = str(exc)
                    log.warning("poll stored nothing: %s", exc)
            # The optional passengers run after the pump and never before it,
            # each behind its own guard. This loop is what keeps the history
            # and the web app's freshness alive; an integration that throws
            # must cost its own feature and nothing else.
            self._watch(self.latest if polled else {})
            # The indoor feed runs whether or not the pump answered. It is an
            # independent source -- a Homey on the LAN knows nothing about a
            # Modbus session that dropped -- and this loop is the only thing
            # keeping its cache warm for /api/status, which must never fetch.
            # Skipping it on a failed poll meant the indoor reading disappeared
            # from the page exactly when the pump was already down.
            self._record_indoor()
            if time.time() - self._pruned > 86400:
                try:
                    self.store.prune()
                    self._pruned = time.time()
                except Exception:                          # noqa: BLE001
                    pass
            self._maybe_backup()
            self._halt.wait(backoff)

    # -- optional passengers ---------------------------------------------

    def _watch(self, data: dict) -> None:
        """Show this poll's readings to the alarm watcher.

        Caught here as well as inside Watcher.poll(): poll() promises not to
        raise, and this loop is too important to take that promise on trust.

        The empty dict on a failed poll is deliberate. The watcher then finds
        no alarm numbers and reports nothing -- "the pump did not answer" is
        not "the alarm cleared" -- but it still gets its turn to retry
        notifications that could not be sent earlier, which is exactly the
        situation where the pump is unreachable and somebody should know.
        """
        if self.watcher is None:
            return
        try:
            self.watcher.poll(data)
            self.last_watch_error = None
        except Exception as exc:                           # noqa: BLE001
            self.last_watch_error = str(exc)
            log.warning("alarm watcher failed: %s", exc)

    def _record_indoor(self) -> None:
        """Store the house temperature, when there is a fresh one to store.

        Only the aggregate is kept, and only when the source produced one:
        `average` is computed from the sensors that answered recently, so a
        None here means every sensor was stale or missing. Writing the last
        good number again would turn a flat battery into hours of a very steady
        house, which is precisely the evidence autotune must never be given.
        """
        if self.indoor is None or not getattr(self.indoor, "configured", False):
            return
        try:
            snap = self.indoor.snapshot()
            if not isinstance(snap, dict) or not snap.get("ok"):
                return
            value = snap.get("average")
            if value is None:
                return
            at = int(snap.get("at") or time.time())
            if at == self._last_indoor_at:
                # The source caches its answers, and its cache outlives one
                # poll interval. Storing the same reading again would weight it
                # twice in the hourly average that autotune fits on.
                return
            self.store.record_indoor(float(value), at)
            self._last_indoor_at = at
            self.last_indoor_error = None
        except Exception as exc:                           # noqa: BLE001
            self.last_indoor_error = str(exc)
            log.warning("indoor reading not stored: %s", exc)

    # -- automatic snapshots ---------------------------------------------

    def _maybe_backup(self) -> None:
        """Snapshot every setting once a day, without being asked.

        A backup you have to remember to take is a backup you do not have when
        you need it -- and the moment you need it is right after changing
        something you should not have. Settings move rarely, so a daily JSON
        costs a few hundred kilobytes a year and makes every change reversible.
        """
        if not self.backup_dir or self.last_error:
            return
        try:
            newest = self._newest_backup()
            if newest is not None and time.time() - newest < self.backup_hours * 3600:
                return
            path = self.pump.backup(self.backup_dir, "automatisk daglig backup")
            self.last_backup = os.path.basename(path)
            log.info("automatic backup written: %s", path)
        except Exception as exc:                           # noqa: BLE001
            # Never let a failed backup stop the polling.
            log.warning("automatic backup failed: %s", exc)

    def _newest_backup(self) -> float | None:
        try:
            times = [os.path.getmtime(os.path.join(self.backup_dir, n))
                     for n in os.listdir(self.backup_dir)
                     if n.startswith("nibe-") and n.endswith(".json")]
        except OSError:
            return None
        return max(times) if times else None
