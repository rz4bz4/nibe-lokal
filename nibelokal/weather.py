"""Weather forecast from SMHI, because a heat pump answers slowly.

The pump's own outdoor sensor tells you what happened. It does not tell you
what is about to happen, and for a machine that heats a building through a
concrete floor that is the wrong end of the problem: a house has hours of
thermal inertia, so a change made now lands this evening. Knowing that tonight
drops ten degrees is worth more than knowing it is currently four.

Nothing here regulates anything. It supplies the numbers that later features
(pre-heating ahead of a cold snap, backing off before an afternoon that turns
mild) will regulate on, and it supplies them in the shapes those decisions
need: the next hours, the next days, and a short summary of where it is going.

The API
-------
SMHI's open point forecast. The category matters and is easy to get wrong:
**pmp3g version 2 is gone** -- it stopped being served on 2026-03-31 and every
old URL now 404s. The current point forecast is category `snow1g` **version 1**
on the same host:

    https://opendata-download-metfcst.smhi.se/api/category/snow1g/version/1
        /geotype/point/lon/<lon>/lat/<lat>/data.json

Verified live 2026-09-06 (HTTP 200, 81 forecast hours). Docs:
https://opendata.smhi.se/metfcst/snow1gv1/get_point_forecast
The symbol table below comes from SMHI's own parameter page, which still
documents the code list unchanged: https://opendata.smhi.se/apidocs/metfcst/parameters.html

The payload changed shape with the category, not just the name. snow1g returns

    {"createdTime": ..., "referenceTime": ..., "geometry": {...},
     "timeSeries": [{"time": "2026-09-06T20:00:00Z",
                     "data": {"air_temperature": 14.4, "wind_speed": 3.8, ...}}]}

-- `time`, not `validTime`, and a flat `data` object of long parameter names
instead of a `parameters` list of `{"name": "t", "values": [...]}`. Anything
written against the pmp3g documentation parses to nothing here, silently, which
is why the names are pinned in NAMES below rather than guessed at call sites.

Two things the live API does that are worth knowing:

* More than six decimals in a coordinate is a 404, not a rounding. The URL is
  built with "%.6f" so that cannot happen by accident.
* A point outside the model domain is also a 404, body "Requested point is out
  of bounds" -- a plausible-looking lat/lon swap fails this way.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from . import __version__

log = logging.getLogger("nibelokal.weather")

BASE = ("https://opendata-download-metfcst.smhi.se/api/category/%s/version/%d"
        "/geotype/point/lon/%s/lat/%s/data.json")
CATEGORY = "snow1g"
VERSION = 1

# SMHI asks that you identify yourself. Contrary to a widely repeated claim,
# the default urllib agent was *not* refused on 2026-09-06 (it answered 200) --
# but an anonymous agent is the first thing an operator blocks when a client
# misbehaves, and being nameless in that log is a bad place to be.
USER_AGENT = "nibe-lokal/%s (+https://github.com/rz4bz4/nibe-lokal)" % __version__

# snow1g's parameter names -> the short keys this module hands out. The comments
# are the units SMHI documents; the octas one in particular surprises people who
# expect a percentage.
NAMES = {
    "t": "air_temperature",                   # C
    "ws": "wind_speed",                       # m/s at 10 m
    "r": "relative_humidity",                 # %
    "cloud": "cloud_area_fraction",           # octas, 0-8
    "precip": "precipitation_amount_mean",    # mm/h
    "symbol": "symbol_code",                  # 1-27, table below
}

# Wsymb2 / symbol_code, 1..27, from SMHI's parameter table (see module docstring).
# Swedish because it is shown to the person in the house, and phrased the way a
# forecast is read out loud rather than transliterated from the English table.
SYMBOLS = {
    1: "klart",
    2: "nästan klart",
    3: "växlande molnighet",
    4: "halvklart",
    5: "molnigt",
    6: "mulet",
    7: "dimma",
    8: "lätta regnskurar",
    9: "regnskurar",
    10: "kraftiga regnskurar",
    11: "åskskurar",
    12: "lätta byar av snöblandat regn",
    13: "byar av snöblandat regn",
    14: "kraftiga byar av snöblandat regn",
    15: "lätta snöbyar",
    16: "snöbyar",
    17: "kraftiga snöbyar",
    18: "lätt regn",
    19: "regn",
    20: "kraftigt regn",
    21: "åska",
    22: "lätt snöblandat regn",
    23: "snöblandat regn",
    24: "kraftigt snöblandat regn",
    25: "lätt snöfall",
    26: "snöfall",
    27: "kraftigt snöfall",
}

# How far the trend has to move over twelve hours before it is called a trend.
# 1.0 C, for two reasons that happen to agree: SMHI's own short-range 2 m
# temperature error is around a degree, so anything smaller is inside the
# forecast's own noise; and one degree outdoors moves a heat curve's supply
# temperature by roughly two, which is the smallest change worth acting on.
# Below this the honest answer is "stabilt" -- reporting a 0.2 C drift as
# "faller" would have the house pre-heating for nothing.
TREND_DEADBAND = 1.0

# The forecast is hourly for about the first day and then thins to six- and
# twelve-hour steps. A daily min/max built from twelve-hour samples is not a
# day's min/max, so every daily row carries the number of samples behind it
# rather than pretending the last days are as solid as tomorrow.
DAILY_MIN_SAMPLES = 2

# Serving an hour-old forecast beats serving nothing: the house does not change
# its mind that fast, and a dropped Wi-Fi packet should not blank the panel.
# Past this the numbers stop describing today's weather, and silence is honester.
STALE_SECONDS = 3 * 3600


def wind_chill(t: float | None, ws: float | None) -> float | None:
    """Perceived temperature, JAG/TI wind chill (Environment Canada / US NWS, 2001).

        T_wc = 13.12 + 0.6215*T - 11.37*V^0.16 + 0.3965*T*V^0.16     [T in C, V in km/h]

    Wind matters to a house the way it matters to a person: a windy 0 C pulls
    more heat through the envelope than a still 0 C, so this is the temperature
    the pump will effectively have to answer, not the one on the thermometer.

    The formula is defined only for T <= 10 C and V >= 4.8 km/h (1.34 m/s) -- it
    was fitted to walking people in cold wind and outside that range it produces
    nonsense (it "warms" a calm winter day). Outside the range this returns the
    plain air temperature unchanged rather than extrapolating, which is also the
    right answer physically: with no wind there is no wind chill.
    """
    if t is None or ws is None:
        return None if t is None else round(float(t), 1)
    v = float(ws) * 3.6
    if float(t) > 10.0 or v < 4.8:
        return round(float(t), 1)
    f = v ** 0.16
    return round(13.12 + 0.6215 * float(t) - 11.37 * f + 0.3965 * float(t) * f, 1)


class Weather:
    """SMHI point forecast for one house, cached, and never fatal.

    Every failure -- unconfigured, offline, SMHI down, SMHI answering something
    unexpected -- comes back as {"ok": False, "error": "<Swedish sentence>"}.
    A forecast is a nice-to-have next to a working Modbus connection, and it
    must not be able to take the page down with it.
    """

    def __init__(self, config: dict | None = None):
        cfg = config or {}
        self.lat = _coord(cfg.get("weather_lat"), 90.0)
        self.lon = _coord(cfg.get("weather_lon"), 180.0)
        self.ttl = max(60.0, _number(cfg.get("weather_ttl_minutes"), 60.0) * 60.0)
        self.timeout = max(1.0, _number(cfg.get("weather_timeout"), 8.0))
        # The web server runs a thread per request on top of the polling
        # thread, so two callers can arrive at an expired cache at once. The
        # lock costs nothing and stops that from becoming two SMHI requests.
        self._lock = threading.Lock()
        self._cached: dict | None = None
        self._fetched: float = 0.0
        self._expires: float = 0.0

    @property
    def configured(self) -> bool:
        """False means "no coordinates set" -- the caller hides the feature.

        Deliberately not an error. Someone who has not told the app where the
        house is has not asked for a forecast, and a red box explaining that is
        noise on every page load.
        """
        return self.lat is not None and self.lon is not None

    def url(self) -> str:
        # %.6f, never repr(): the API 404s on a seventh decimal, and a float
        # that prints as 58.58120000000001 is exactly how that happens.
        return BASE % (CATEGORY, VERSION, "%.6f" % self.lon, "%.6f" % self.lat)

    def snapshot(self, now: float | None = None) -> dict:
        """The whole forecast in one dict. Never raises; see the class docstring."""
        try:
            with self._lock:
                return self._snapshot(time.time() if now is None else now)
        except Exception as exc:                           # noqa: BLE001
            # Any path that reaches here is a bug, not weather. Log it with a
            # traceback and still answer, because the caller renders a page.
            log.exception("weather snapshot failed: %s", exc)
            return {"ok": False, "error": "Väderprognosen kunde inte tas fram just nu."}

    # -- internals -------------------------------------------------------

    def _snapshot(self, now: float) -> dict:
        if not self.configured:
            return {"ok": False, "error":
                    "Ingen plats är inställd, så ingen prognos hämtas. "
                    "Sätt weather_lat och weather_lon i config.yaml."}

        if self._cached is not None and now < self._expires:
            return self._cached

        try:
            payload, max_age = self._fetch()
        except Exception as exc:                           # noqa: BLE001
            error = _error_text(exc, self.lat, self.lon)
            log.warning("SMHI fetch failed: %s", exc)
            # A forecast from an hour ago is still a forecast.
            if self._cached is not None and now - self._fetched < STALE_SECONDS:
                stale = dict(self._cached)
                stale["stale"] = True
                stale["note"] = ("Prognosen är från %s och kunde inte uppdateras: %s"
                                 % (_local_iso(self._fetched), error))
                return stale
            return {"ok": False, "error": error}

        try:
            snap = _build(payload, now)
        except Exception as exc:                           # noqa: BLE001
            log.warning("SMHI answered something unparseable: %s", exc)
            return {"ok": False, "error":
                    "SMHI svarade med data som inte gick att tolka som en prognos."}

        if not snap["hourly"]:
            return {"ok": False, "error":
                    "SMHI svarade utan några prognostimmar för den platsen."}

        self._cached = snap
        self._fetched = now
        # Honour SMHI's own Cache-Control over a shorter configured TTL. Their
        # edge answers max-age=3600 because the model runs about hourly; asking
        # again inside that window returns the identical bytes and costs them
        # bandwidth for nothing.
        self._expires = now + max(self.ttl, max_age)
        return snap

    def _fetch(self) -> tuple[dict, float]:
        req = urllib.request.Request(self.url(), headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read()
            max_age = _max_age(resp.headers.get("Cache-Control"))
        return json.loads(raw.decode("utf-8")), max_age


# -- parsing -------------------------------------------------------------

def _build(payload: dict, now: float) -> dict:
    hours = []
    for entry in (payload.get("timeSeries") or []):
        row = _hour(entry)
        if row is not None:
            hours.append(row)
    hours.sort(key=lambda h: h["ts"])

    snap = {
        "ok": True,
        "at": int(now),
        # SMHI's own reference time -- when the model run this forecast comes
        # from was valid. createdTime is when they published it; referenceTime
        # is the one that says how old the physics is.
        "issued": payload.get("referenceTime") or payload.get("createdTime") or "",
        "now": {},
        "hourly": [_public(h) for h in hours],
        "daily": _daily(hours),
        "summary": _summary(hours),
    }
    if hours:
        first = hours[0]
        snap["now"] = {
            "t": first["t"],
            "ws": first["ws"],
            "effective": first["effective"],
            "symbol": first["symbol"],
            "symbol_sv": symbol_text(first["symbol"]),
        }
    return snap


def _hour(entry: dict) -> dict | None:
    ts = _parse_time(entry.get("time"))
    if ts is None:
        return None
    data = entry.get("data")
    if not isinstance(data, dict):
        return None
    values = {short: _number(data.get(name), None) for short, name in NAMES.items()}
    if values["t"] is None:
        # Temperature is the only parameter everything downstream needs; an
        # entry without one would poison every min/max it landed in.
        return None
    symbol = values["symbol"]
    return {
        "ts": ts,
        "t": round(values["t"], 1),
        "ws": values["ws"],
        "cloud": values["cloud"],
        "precip": values["precip"],
        "r": values["r"],
        "symbol": int(symbol) if symbol is not None else None,
        "effective": wind_chill(values["t"], values["ws"]),
    }


def _public(hour: dict) -> dict:
    return {
        "time": _local_iso(hour["ts"]),
        "t": hour["t"],
        "ws": hour["ws"],
        "effective": hour["effective"],
        "cloud": hour["cloud"],
        "precip": hour["precip"],
        "symbol": hour["symbol"],
        "symbol_sv": symbol_text(hour["symbol"]),
    }


def _daily(hours: list) -> list:
    """Per-day min/max/mean, grouped by *local* date.

    Local, not UTC: a day for a house is the day its inhabitants live in, and
    grouping by UTC would move Swedish midnight into the previous row.
    """
    days: dict[str, list] = {}
    for h in hours:
        days.setdefault(_local_date(h["ts"]), []).append(h["t"])
    out = []
    for date in sorted(days):
        temps = days[date]
        if len(temps) < DAILY_MIN_SAMPLES:
            continue
        out.append({
            "date": date,
            "min": round(min(temps), 1),
            "max": round(max(temps), 1),
            "mean": round(sum(temps) / len(temps), 1),
            # See DAILY_MIN_SAMPLES: two samples is a day's shape, not its min.
            "samples": len(temps),
        })
    return out


def _summary(hours: list) -> dict:
    out = {"min_24h": None, "mean_24h": None, "min_48h": None,
           "coldest_hour": "", "trend": "stabilt", "trend_c": 0.0}
    if not hours:
        return out
    start = hours[0]["ts"]
    day = [h for h in hours if h["ts"] <= start + 24 * 3600]
    two = [h for h in hours if h["ts"] <= start + 48 * 3600]
    if day:
        coldest = min(day, key=lambda h: h["t"])
        out["min_24h"] = round(min(h["t"] for h in day), 1)
        out["mean_24h"] = round(sum(h["t"] for h in day) / len(day), 1)
        out["coldest_hour"] = _local_iso(coldest["ts"])
    if two:
        out["min_48h"] = round(min(h["t"] for h in two), 1)

    window = [h for h in hours if h["ts"] <= start + 12 * 3600]
    if len(window) >= 2:
        # Means of the ends rather than the two endpoints: one hour that happens
        # to sit in a shower flips an endpoint comparison, and the question here
        # is where the next twelve hours are going, not what one hour does.
        edge = max(1, min(3, len(window) // 2))
        head = sum(h["t"] for h in window[:edge]) / edge
        tail = sum(h["t"] for h in window[-edge:]) / edge
        delta = tail - head
        out["trend_c"] = round(delta, 1)
        if delta <= -TREND_DEADBAND:
            out["trend"] = "faller"
        elif delta >= TREND_DEADBAND:
            out["trend"] = "stiger"
    return out


def symbol_text(symbol) -> str:
    """Swedish text for a Wsymb2 code, or an honest blank for anything else."""
    return SYMBOLS.get(symbol, "")


# -- small helpers -------------------------------------------------------

def _coord(value, limit: float) -> float | None:
    v = _number(value, None)
    if v is None or not -limit <= v <= limit:
        return None
    return v


def _number(value, default):
    """Floats out of YAML, JSON or the environment, without exploding on junk.

    Booleans are rejected on purpose: `weather_lat: true` is a typo, and True
    would otherwise sail through as latitude 1.0.
    """
    if value is None or isinstance(value, bool):
        return default
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v or v in (float("inf"), float("-inf")):      # NaN and infinities
        return default
    return v


def _parse_time(text) -> int | None:
    """SMHI timestamps are UTC with a literal Z, which fromisoformat cannot read
    before Python 3.11. Parsed explicitly so this works on the Pi it runs on."""
    if not isinstance(text, str):
        return None
    try:
        dt = datetime.strptime(text.strip(), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


def _local(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone()


def _local_iso(ts: float) -> str:
    return _local(ts).strftime("%Y-%m-%dT%H:%M")


def _local_date(ts: float) -> str:
    return _local(ts).strftime("%Y-%m-%d")


def _max_age(header) -> float:
    if not header:
        return 0.0
    m = re.search(r"max-age\s*=\s*(\d+)", header)
    return float(m.group(1)) if m else 0.0


def _error_text(exc: Exception, lat, lon) -> str:
    """One plain Swedish sentence, saying which of the likely causes it was."""
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 404:
            return ("SMHI har ingen prognos för %.6f, %.6f — punkten ligger utanför "
                    "modellens område. Kontrollera att latitud och longitud inte "
                    "hamnat i fel ordning." % (lat, lon))
        if exc.code in (429, 503):
            return "SMHI svarar inte just nu (för många anrop). Prognosen kommer tillbaka."
        return "SMHI svarade med fel %d." % exc.code
    if isinstance(exc, TimeoutError):
        # A read timeout escapes urllib unwrapped, so it needs its own branch.
        return "SMHI svarade inte i tid. Prognosen försöker igen nästa gång sidan laddas."
    if isinstance(exc, urllib.error.URLError):
        return "Kunde inte nå SMHI. Kontrollera att den här maskinen har internet."
    if isinstance(exc, (json.JSONDecodeError, ValueError)):
        return "SMHI svarade med data som inte gick att tolka som en prognos."
    return "Väderprognosen kunde inte hämtas: %s" % exc
