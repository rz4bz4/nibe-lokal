"""History: a background poller and a SQLite table of readings.

myUplink's paid tier sells you history. This is that history, in a file you own,
at whatever resolution you configure. One row per register per poll; a year of
20 registers at 60 s is roughly 10 million rows, which SQLite handles fine but
which is why old rows are pruned to `history_days`.
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
"""


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

    def prune(self) -> int:
        cutoff = int(time.time()) - self.history_days * 86400
        with self._lock, self._db as c:
            cur = c.execute("DELETE FROM readings WHERE ts < ?", (cutoff,))
            return cur.rowcount

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

    def stats(self) -> dict:
        with self._lock, self._db as c:
            n, first, last = c.execute(
                "SELECT COUNT(*), MIN(ts), MAX(ts) FROM readings"
            ).fetchone()
        return {"rows": n or 0, "first": first, "last": last}


class Poller(threading.Thread):
    """Reads the dashboard registers on a timer and stores them."""

    daemon = True

    def __init__(self, pump, store: Store, addresses: list[int], seconds: int = 60,
                 backup_dir: str | None = None, backup_hours: float = 24.0):
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
        self._stop = threading.Event()
        self._pruned = 0.0

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        backoff = self.seconds
        while not self._stop.is_set():
            try:
                data = self.pump.read_many(self.addresses)
                self.latest = data
                self.last_ok = time.time()
                self.last_error = None
                self.store.record(data)
                backoff = self.seconds
            except Exception as exc:                       # noqa: BLE001
                self.last_error = str(exc)
                log.warning("poll failed: %s", exc)
                # The pump drops sessions now and then; back off rather than hammer.
                backoff = min(backoff * 2, 600)
            if time.time() - self._pruned > 86400:
                try:
                    self.store.prune()
                    self._pruned = time.time()
                except Exception:                          # noqa: BLE001
                    pass
            self._maybe_backup()
            self._stop.wait(backoff)

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
