"""Termostat: håller inomhustemperaturen genom att stega värmeoffset (40031).

Pumpen har ingen rumsgivare. Den här modulen läser inomhus -- snittet av
Homey-givarna, annars frånluften (BT20) som är husets blandade luft -- jämför med
ett mål och flyttar offset ett steg i taget.

Försiktigheten är hela poängen, för en termostat som stegar fel pendlar:

* Ett steg åt gången, och minst `hold_hours` mellan stegen. Golvvärmen nere
  svarar på timmar; den som stegar varje kvart hinner gå i botten innan
  första steget märkts.
* Dödband: inom ±deadband gör den ingenting.
* Trendspärr: rör sig huset redan mot målet med minst 0,1 °C/h väntar den.
* Värmestopp: över 17 °C ute värmer pumpen inte alls, och offset betyder
  ingenting. Då rör den inget -- annars vandrar offset till ändläget en
  solig septemberdag och huset är kallt den kväll det behövs.
* Gränser: offset hålls inom [offset_min, offset_max].
* Någon annan ändrar offset (displayen, myUplink, appens knappar): det
  respekteras. Ändringen loggas och hålltiden startar om därifrån.

Allt den gör hamnar i writes (source = termostat) och i thermostat_log.
"""
from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("nibelokal.thermostat")

R_OFFSET = 40031
R_OUTDOOR = 30002
R_EXHAUST = 30020
HEATING_STOP_C = 17.0
TREND_C_PER_H = 0.1
TARGET_MIN, TARGET_MAX = 15.0, 28.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS thermostat_state (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS thermostat_log (
    ts INTEGER NOT NULL, indoor REAL, source TEXT, target REAL,
    offset_before INTEGER, offset_after INTEGER, action TEXT, reason TEXT);
CREATE INDEX IF NOT EXISTS thermostat_log_ts ON thermostat_log (ts);
"""


def _f(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


class Thermostat(threading.Thread):
    daemon = True

    def __init__(self, pump, store, cfg: dict):
        super().__init__(name="nibe-thermostat")
        self.pump = pump
        self.store = store
        self.deadband = _f(cfg.get("thermostat_deadband"), 0.3)
        self.hold_s = _f(cfg.get("thermostat_hold_hours"), 4.0) * 3600
        self.omin = int(_f(cfg.get("thermostat_offset_min"), -2))
        self.omax = int(_f(cfg.get("thermostat_offset_max"), 4))
        self.window_s = int(_f(cfg.get("thermostat_window_minutes"), 60) * 60)
        self.every = 600
        self._halt = threading.Event()
        self._wake = threading.Event()
        self._mu = threading.Lock()
        with store._lock, store._db as c:
            c.executescript(SCHEMA)
        st = self._load()
        self.enabled = st.get("enabled", str(bool(cfg.get("thermostat_enabled")))) == "True"
        self.target = _f(st.get("target"), _f(cfg.get("thermostat_target"), 21.0))
        self.last_change = _f(st.get("last_change"), 0) or None
        self.seen_offset = int(st["seen_offset"]) if st.get("seen_offset") not in (None, "", "None") else None
        self.status: dict = {"state": "startar", "text": "Termostaten har inte gjort sin första koll än."}
        self.last_tick: float | None = None
        self.indoor = None
        self.source = None
        self.slope = None
        self.offset = None

    # -- persistens ---------------------------------------------------------

    def _load(self) -> dict:
        with self.store._lock, self.store._db as c:
            return {k: v for k, v in c.execute("SELECT key, value FROM thermostat_state")}

    def _save(self) -> None:
        vals = {"enabled": str(self.enabled), "target": str(self.target),
                "last_change": str(self.last_change or ""),
                "seen_offset": str(self.seen_offset)}
        with self.store._lock, self.store._db as c:
            c.executemany("INSERT OR REPLACE INTO thermostat_state (key, value) VALUES (?,?)",
                          list(vals.items()))

    def _log(self, action: str, reason: str, before=None, after=None) -> None:
        with self.store._lock, self.store._db as c:
            c.execute("INSERT INTO thermostat_log (ts, indoor, source, target, offset_before, "
                      "offset_after, action, reason) VALUES (?,?,?,?,?,?,?,?)",
                      (int(time.time()), self.indoor, self.source, self.target,
                       before, after, action, reason))

    # -- mätning ------------------------------------------------------------

    def measure(self):
        """(värde, källa, lutning °C/h) -- Homey om färsk, annars frånluften."""
        now = int(time.time())
        with self.store._lock, self.store._db as c:
            rows = c.execute("SELECT ts, value FROM indoor WHERE ts >= ? ORDER BY ts",
                             (now - 7200,)).fetchall()
            src = "givare"
            if not rows or rows[-1][0] < now - 1800:
                rows = c.execute("SELECT ts, value FROM readings WHERE address = ? AND ts >= ? "
                                 "AND value IS NOT NULL ORDER BY ts",
                                 (R_EXHAUST, now - 7200)).fetchall()
                src = "frånluft"
        if not rows:
            return None, None, None
        win = [v for t, v in rows if t >= now - self.window_s] or [rows[-1][1]]
        value = round(sum(win) / len(win), 2)
        slope = None
        if len(rows) >= 6 and rows[-1][0] - rows[0][0] >= 3600:
            mt = sum(t for t, _ in rows) / len(rows)
            mv = sum(v for _, v in rows) / len(rows)
            den = sum((t - mt) ** 2 for t, _ in rows)
            if den:
                slope = round(sum((t - mt) * (v - mv) for t, v in rows) / den * 3600, 3)
        return value, src, slope

    def _outdoor(self):
        with self.store._lock, self.store._db as c:
            r = c.execute("SELECT AVG(value) FROM readings WHERE address = ? AND ts >= ?",
                          (R_OUTDOOR, int(time.time()) - 3600)).fetchone()
        return r[0] if r else None

    # -- loopen -------------------------------------------------------------

    def stop(self) -> None:
        self._halt.set()
        self._wake.set()

    def poke(self) -> None:
        self._wake.set()

    def run(self) -> None:
        self._halt.wait(30)          # låt pollern hinna fylla på först
        while not self._halt.is_set():
            try:
                self.tick()
            except Exception as exc:                       # noqa: BLE001
                self.status = {"state": "fel", "text": "Termostaten stötte på ett fel: %s" % exc}
                log.warning("thermostat tick failed: %s", exc)
            self._wake.wait(self.every)
            self._wake.clear()

    def _set(self, state: str, text: str, **extra) -> None:
        self.status = dict(state=state, text=text, **extra)

    def tick(self) -> None:
        with self._mu:
            now = time.time()
            self.last_tick = now
            off = self.pump.read(R_OFFSET)
            off = off.get("value") if isinstance(off, dict) else off
            if off is None:
                self._set("fel", "Kunde inte läsa värmeoffset från pumpen.")
                return
            off = int(off)
            self.offset = off
            if self.seen_offset is not None and off != self.seen_offset:
                self.last_change = now
                self._log("manuell", "Offset ändrades utanför termostaten (%+d → %+d). "
                          "Hålltiden startar om." % (self.seen_offset, off), self.seen_offset, off)
            self.seen_offset = off
            self._save()

            self.indoor, self.source, self.slope = self.measure()
            if not self.enabled:
                self._set("av", "Termostaten är avstängd. Offset ligger på %+d." % off)
                return
            if self.indoor is None:
                self._set("ingen_matning", "Ingen inomhustemperatur att gå på, så inget ändras.")
                return
            outdoor = self._outdoor()
            if outdoor is not None and outdoor >= HEATING_STOP_C:
                self._set("vila", "%.1f °C ute: pumpen värmer inte, så offset spelar ingen roll nu." % outdoor)
                return
            err = self.target - self.indoor
            if abs(err) <= self.deadband:
                self._set("inom", "%.1f °C inne, inom ±%.1f av målet. Inget att göra."
                          % (self.indoor, self.deadband))
                return
            step = 1 if err > 0 else -1
            word = "kallt" if step > 0 else "varmt"
            if self.slope is not None and self.slope * step >= TREND_C_PER_H:
                self._set("pa_vag", "För %s (%.1f °C), men huset rör sig redan åt rätt håll "
                          "(%+.2f °C/h). Väntar." % (word, self.indoor, self.slope))
                return
            if self.last_change and now - self.last_change < self.hold_s:
                until = self.last_change + self.hold_s
                self._set("vantar", "För %s (%.1f °C). Nästa steg tidigast %s, så förra ändringen "
                          "hinner verka." % (word, self.indoor, time.strftime("%H:%M", time.localtime(until))),
                          until=until)
                return
            new = max(self.omin, min(self.omax, off + step))
            if new == off:
                self._set("grans", "För %s (%.1f °C), men offset står redan vid gränsen %+d."
                          % (word, self.indoor, off))
                return
            res = self.pump.write(R_OFFSET, new, confirmed=True, expect=off)
            try:
                self.store.record_write(res, source="termostat")
            except Exception:                              # noqa: BLE001
                pass
            after = res.get("after")
            after = int(after) if after is not None else new
            reason = "%.1f °C inne mot målet %.1f (%s)." % (self.indoor, self.target, self.source)
            self._log("hojde" if step > 0 else "sankte", reason, off, after)
            log.info("thermostat: offset %+d -> %+d (%s)", off, after, reason)
            self.last_change = now
            self.seen_offset = after
            self.offset = after
            self._save()
            self._set("andrade", "%s offset %+d → %+d. %s" % ("Höjde" if step > 0 else "Sänkte",
                                                               off, after, reason))

    # -- API ------------------------------------------------------------------

    def configure(self, enabled=None, target=None) -> None:
        with self._mu:
            if enabled is not None:
                if bool(enabled) != self.enabled:
                    self.enabled = bool(enabled)
                    self._log("pa" if self.enabled else "av",
                              "Termostaten slogs %s." % ("på" if self.enabled else "av"))
            if target is not None:
                t = round(float(target) * 2) / 2
                if not TARGET_MIN <= t <= TARGET_MAX:
                    raise ValueError("Målet måste ligga mellan %g och %g °C." % (TARGET_MIN, TARGET_MAX))
                if t != self.target:
                    old = self.target
                    self.target = t
                    self._log("mal", "Målet ändrat %.1f → %.1f °C." % (old, t))
            self._save()
        self.poke()

    def as_dict(self) -> dict:
        now = int(time.time())
        with self.store._lock, self.store._db as c:
            rows = c.execute("SELECT ts, indoor, source, target, offset_before, offset_after, action, "
                             "reason FROM thermostat_log ORDER BY ts DESC LIMIT 40").fetchall()
            indoor = c.execute("SELECT (ts/1800)*1800 b, AVG(value) FROM indoor WHERE ts >= ? "
                               "GROUP BY b ORDER BY b", (now - 48 * 3600,)).fetchall()
            exhaust = c.execute("SELECT (ts/1800)*1800 b, AVG(value) FROM readings WHERE address = ? "
                                "AND ts >= ? GROUP BY b ORDER BY b",
                                (R_EXHAUST, now - 48 * 3600)).fetchall()
            offs = c.execute("SELECT ts, after FROM writes WHERE address = ? AND ts >= ? ORDER BY ts",
                             (R_OFFSET, now - 48 * 3600)).fetchall()
        keys = ("ts", "indoor", "source", "target", "before", "after", "action", "reason")
        return {
            "ok": True, "enabled": self.enabled, "target": self.target,
            "deadband": self.deadband, "hold_hours": self.hold_s / 3600,
            "offset_min": self.omin, "offset_max": self.omax,
            "offset": self.offset, "indoor": self.indoor, "source": self.source,
            "slope": self.slope, "last_change": self.last_change, "last_tick": self.last_tick,
            "status": self.status,
            "log": [dict(zip(keys, r)) for r in rows],
            "series": {"indoor": [[b, round(v, 2)] for b, v in indoor if v is not None],
                       "exhaust": [[b, round(v, 2)] for b, v in exhaust if v is not None],
                       "offset_writes": [[t, a] for t, a in offs]},
        }
