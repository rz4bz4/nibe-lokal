"""Indoor temperature from a local Homey Pro, over its LAN API.

The pump only knows the room temperature if a room sensor is wired to it. Most
installations have none, so the heating curve runs blind on outdoor temperature
alone. Homey usually already has a dozen sensors in the house; this reads them.

The trap this module exists to avoid: the obvious implementation is "average
every device that reports measure_temperature". That is wrong here in two ways.

  1. Half those devices are door and window sensors. They sit in the frame, in
     the draught, on an outside wall -- measured in this house, the entrance
     sensor reads several degrees away from the room it nominally lives in.
     Averaging it in biases the whole house reading and, once this number is
     allowed to move the heating curve, that bias becomes real fuel. Hence the
     whitelist: `homey_devices` names the sensors you actually trust, and an
     empty whitelist means "every temperature device", which is convenient for
     a first look and almost never what you want to regulate on.

  2. A sensor with a flat battery does not disappear. Homey keeps serving its
     last value forever, with only `lastUpdated` to give it away. A sensor
     frozen at 24 C in June will happily hold the heating down all winter. So
     anything older than `homey_max_age_minutes` is marked stale and left out
     of every aggregate, and if that is all of them there is no average at all
     rather than a confident wrong one.

Verified against a real Homey Pro (firmware serving /api/manager/devices/device)
on 2026-09-06. Two things there differ from Athom's documented shape:

  * `lastUpdated` is an integer of epoch MILLISECONDS (1788719999314), not the
    ISO-8601 string with a trailing Z that the docs show. Both are parsed.
  * `zoneName` is absent on every device; `zone` is a bare UUID. Zone names
    live in /api/manager/zones/zone and are fetched separately, so `by_zone`
    can say "Vardagsrum" rather than a UUID.
"""
from __future__ import annotations

import copy
import datetime as dt
import http.client
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request

from . import sv_number
from .config import header_safe, scrub

log = logging.getLogger("nibelokal.homey")

DEVICES_PATH = "/api/manager/devices/device"
ZONES_PATH = "/api/manager/zones/zone"
CAPABILITY = "measure_temperature"

# Readings outside this band are not rooms, they are a broken sensor or a
# decoding accident (0 K, a masked -3276.8). Dropping them costs nothing and
# keeps one dead device from dragging an average that later moves the curve.
PLAUSIBLE_C = (-50.0, 80.0)


class Homey:
    """Read-only view of the temperature sensors on one Homey Pro.

    No method here raises. This is called from the polling loop in store.py,
    which must survive a Homey that is rebooting, unplugged, or has just had
    its API key rotated -- none of which are reasons to stop reading the pump.
    """

    def __init__(self, host: str = "", token: str = "", devices: str = "",
                 max_age_minutes: float = 60.0, timeout: float = 5.0,
                 cache_seconds: float = 120.0):
        self.base = _base_url(host)
        self.token = (token or "").strip()
        self.whitelist = _split_names(devices)
        self.max_age_minutes = float(max_age_minutes or 0) or 60.0
        self.timeout = float(timeout or 5.0)
        # The token is checked once, here, rather than every time a header is
        # built from it: a value http.client refuses makes it raise a
        # ValueError whose message quotes the whole header -- API key included
        # -- and _get turns that into the error string the page prints. Found
        # on a config.yaml with a stray carriage return inside a quoted string.
        self.token_error: str | None = None
        if self.token and not header_safe(self.token):
            self.token_error = (
                "homey_token innehåller tecken som inte får finnas i en "
                "HTTP-header (radbrytning, tabb eller liknande). Kontrollera "
                "att nyckeln står på en rad i config.yaml. Inget anrop görs "
                "förrän den är rättad."
            )
        # The pump poll runs every 60 s and the web page polls on top of it.
        # Homey is a small box doing real work; a cached answer that is up to
        # two minutes old is indistinguishable from a live one for a house that
        # takes hours to change temperature.
        self.cache_seconds = float(cache_seconds or 0)
        # _lock guards the two cached attributes and nothing else -- never a
        # network call. _fetching is the single-flight guard; see snapshot().
        self._lock = threading.Lock()
        self._fetching = threading.Lock()
        self._cached: dict | None = None
        self._cached_at = 0.0

    @classmethod
    def from_config(cls, cfg: dict) -> "Homey":
        """Build from the app config.

        Read with .get and explicit defaults rather than indexing: the keys are
        in config.DEFAULTS now, but this class is also built straight from a
        plain dict in tests and from the environment in __main__, and a missing
        key there must fall back rather than raise.
        """
        return cls(
            host=cfg.get("homey_host", "") or "",
            token=cfg.get("homey_token", "") or "",
            devices=cfg.get("homey_devices", "") or "",
            max_age_minutes=_number(cfg.get("homey_max_age_minutes"), 60.0),
            timeout=_number(cfg.get("homey_timeout"), 5.0),
            cache_seconds=_number(cfg.get("homey_cache_seconds"), 120.0),
        )

    @property
    def configured(self) -> bool:
        """False when there is nothing to talk to.

        Callers use this to hide the indoor-temperature panel entirely. An
        unconfigured optional feature is not an error and should not look like
        one on the dashboard.
        """
        return bool(self.base and self.token)

    # -- the one public method -------------------------------------------

    def snapshot(self, force: bool = False) -> dict:
        """Current indoor temperatures, or an error dict. Never raises.

        The two LAN requests in _build() happen *outside* `self._lock`, and
        that is the whole point of the dance below. Holding the lock across
        them meant a Homey that accepts a connection and then never answers
        blocked every other caller for the length of two timeouts -- measured
        at 4.8 s on a concurrent /api/status while the pump was also down. The
        lock now only ever guards two attribute assignments.

        `_fetching` keeps that from becoming a stampede: one caller refreshes
        and the others carry on with the copy they already have rather than
        queueing up behind it on the network. Only a caller with nothing at all
        to return waits, and it waits for the fetch, not for the lock.
        """
        try:
            at = time.time()
            with self._lock:
                cached, cached_at = self._cached, self._cached_at
            # A backward clock step (ntp, a Pi with no RTC catching up after a
            # power cut) makes `at - cached_at` negative, which read as "very
            # fresh" and froze the cache until the clock caught up again.
            if (cached is not None and not force
                    and 0 <= at - cached_at < self.cache_seconds):
                return copy.deepcopy(cached)

            if not self._fetching.acquire(blocking=force or cached is None):
                # Somebody else is already talking to Homey. An answer two
                # minutes old beats waiting out their timeout -- unless the
                # caller asked for a fresh one, and then we do wait.
                return copy.deepcopy(cached)
            try:
                with self._lock:
                    cached, cached_at = self._cached, self._cached_at
                if (cached is not None and not force
                        and 0 <= time.time() - cached_at < self.cache_seconds):
                    # Somebody refreshed it while we waited for our turn.
                    return copy.deepcopy(cached)
                # Failures are cached for the same TTL as successes on purpose:
                # a Homey that is down should be asked once every two minutes,
                # not once every poll for as long as it stays down.
                result = self._build()
                with self._lock:
                    self._cached = result
                    self._cached_at = time.time()
            finally:
                self._fetching.release()
            # A deep copy, always: the cache is shared with every other caller
            # and a page handler that edits what it was handed -- even one
            # sensor row inside it -- would corrupt it for everyone. Copying a
            # dozen sensors costs microseconds; a poisoned cache lasts until
            # the process restarts.
            return copy.deepcopy(result)
        except Exception as exc:                           # noqa: BLE001
            # Nothing below is expected to throw, but this runs inside a loop
            # that must not die, so an unforeseen shape gets an error dict too.
            log.warning("homey snapshot failed unexpectedly: %s", exc)
            return _fail("Kunde inte läsa inomhustemperaturen från Homey: %s"
                         % scrub(exc, self.token))

    def cached_snapshot(self) -> dict | None:
        """The last answer, or None if there has not been one yet.

        Never touches the network, so a caller that must not block -- the
        /api/status summary the page polls every 30 s -- can have the number
        without becoming a request to the LAN on a cold cache.
        """
        with self._lock:
            return None if self._cached is None else copy.deepcopy(self._cached)

    # -- internals --------------------------------------------------------

    def _build(self) -> dict:
        if not self.configured:
            missing = "adress" if not self.base else "API-nyckel"
            return _fail("Homey är inte konfigurerad: %s saknas i config.yaml "
                         "(homey_host, homey_token)." % missing)
        if self.token_error:
            return _fail(self.token_error)

        devices, error = self._get(DEVICES_PATH)
        if error:
            return _fail(error)
        if isinstance(devices, list):
            devices = {str(d.get("id") or i): d for i, d in enumerate(devices)
                       if isinstance(d, dict)}
        if not isinstance(devices, dict):
            return _fail("Homey svarade med ett oväntat format på enhetslistan. "
                         "Kontrollera att adressen pekar på en Homey Pro.")

        # A failed zone lookup must not fail the snapshot; the temperatures are
        # the point, the zone names are decoration on top of them.
        zones, zone_error = self._get(ZONES_PATH)
        zone_names = _zone_names(zones) if not zone_error else {}
        if zone_error:
            log.info("homey zone names unavailable: %s", zone_error)

        now = time.time()
        sensors = []
        matched = set()
        undated = []
        for device_id, device in devices.items():
            if not isinstance(device, dict):
                continue
            name = _text(device.get("name")) or str(device_id)
            hit = self._whitelisted(device_id, name)
            if hit is None:
                continue
            matched.add(hit)

            cap = (device.get("capabilitiesObj") or {}).get(CAPABILITY)
            if not isinstance(cap, dict):
                continue
            value = _temperature(cap.get("value"))
            if value is None:
                continue

            updated = _parse_time(cap.get("lastUpdated"))
            if updated is None:
                # No timestamp is not evidence of a stale reading, so this is
                # kept and counted -- but it is said out loud, because it is
                # also the one case where the dead-battery guard cannot work.
                age = None
                stale = False
                undated.append(name)
            else:
                age = max(0.0, (now - updated) / 60.0)
                stale = age > self.max_age_minutes

            zone_id = _text(device.get("zone"))
            zone = (zone_names.get(zone_id)
                    or _text(device.get("zoneName"))   # some firmwares do send it
                    or None)
            sensors.append({
                "id": str(device_id),
                "name": name,
                "zone": zone,
                "value": round(value, 1),
                "age_minutes": None if age is None else round(age, 1),
                "stale": stale,
            })

        sensors.sort(key=lambda s: ((s["zone"] or "￿").lower(), s["name"].lower()))
        out = {
            "ok": True,
            "at": int(now),
            "sensors": sensors,
            "source": "homey",
        }
        out.update(self._aggregate(sensors))

        warnings = []
        if not sensors:
            warnings.append(
                "Ingen av enheterna i Homey rapporterade någon temperatur."
                if not self.whitelist else
                "Ingen av de valda enheterna rapporterade någon temperatur."
            )
        elif out["average"] is None:
            warnings.append(
                "Alla avläsningar är äldre än %s minuter, så inget medelvärde "
                "beräknas. Kontrollera batterierna i givarna."
                % sv_number(self.max_age_minutes, None)
            )
        unknown = [n for n in self.whitelist if n not in matched]
        if unknown:
            warnings.append("Hittade ingen Homey-enhet som heter: %s."
                            % ", ".join(sorted(unknown)))
        if undated:
            warnings.append("Saknar tidsstämpel (kan inte bedömas som gammal): %s."
                            % ", ".join(sorted(undated)))
        if warnings:
            out["warning"] = " ".join(warnings)
        return out

    def _aggregate(self, sensors: list) -> dict:
        """Zone averages, and the mean of those as the house average.

        Deliberately not the mean of all sensors: the living room may have four
        devices that report a temperature and the bedroom one, and a plain mean
        would let the living room cast four votes. One room, one vote is what a
        person means by "the indoor temperature", and it is what the heating
        decision should eventually run on.

        Grouped by zone NAME, not zone id: this house has two distinct zones
        both called "Vardagsrum" (one per floor), and showing the same name
        twice with two different numbers is a bug report waiting to happen.
        Merging them is the lesser evil, and the sensor list still shows both.
        """
        groups: dict = {}
        for s in sensors:
            if s["stale"]:
                continue
            groups.setdefault(s["zone"] or "Utan zon", []).append(s["value"])
        by_zone = {zone: round(sum(v) / len(v), 1) for zone, v in groups.items()}
        average = round(sum(by_zone.values()) / len(by_zone), 1) if by_zone else None
        return {"average": average, "by_zone": by_zone}

    def _whitelisted(self, device_id, name: str):
        """Return the whitelist entry this device matched, or None.

        Returns the entry rather than a bool so unmatched entries can be
        reported: a whitelist that silently matches nothing looks exactly like
        a Homey with no sensors, and the fix is entirely different.
        """
        if not self.whitelist:
            return ""    # empty string: matched by the "no whitelist" rule
        for entry in self.whitelist:
            if entry.lower() in (name.lower(), str(device_id).lower()):
                return entry
        return None

    def _safe(self, value) -> str:
        """A message with the API key taken out of it, whatever produced it."""
        return scrub(value, self.token)

    def _get(self, path: str):
        """GET one endpoint. Returns (parsed, None) or (None, Swedish error)."""
        if self.token_error:
            return None, self.token_error
        req = urllib.request.Request(
            self.base + path,
            headers={"Authorization": "Bearer " + self.token,
                     "Accept": "application/json"},
        )
        try:
            # No proxy, ever. Homey is on the LAN, and an http_proxy in the
            # environment -- normal on a machine that also talks to the outside
            # world -- would otherwise send this to a proxy that cannot route
            # to 192.168.x and time out for reasons nobody can see from here.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return None, ("Homey nekade anropet (%d). API-nyckeln i "
                              "homey_token är fel eller återkallad." % exc.code)
            if exc.code == 404:
                return None, ("Homey svarade 404 på %s. Är det en Homey Pro med "
                              "lokalt API påslaget?" % path)
            return None, "Homey svarade med fel %d." % exc.code
        except urllib.error.URLError as exc:
            return None, ("Kunde inte nå Homey på %s (%s). Kontrollera adressen "
                          "och att den svarar på nätverket."
                          % (self.base, self._safe(exc.reason)))
        except (OSError, http.client.HTTPException, ValueError) as exc:
            # http.client.HTTPException -- BadStatusLine, IncompleteRead -- is
            # not an OSError and is not a URLError, so it used to escape this
            # method entirely. snapshot() then caught it in its own catch-all,
            # which does not write the cache: a Homey answering half a response
            # was re-asked on every single call instead of once per cache
            # window. scrub() as well as the header_safe check in __init__:
            # this string is printed on the page, and a message that quotes the
            # request is exactly how an API key gets published to whoever is
            # looking.
            return None, ("Kunde inte nå Homey på %s (%s)."
                          % (self.base, self._safe(exc)))
        try:
            return json.loads(raw.decode("utf-8", "replace")), None
        except ValueError:
            return None, ("Homey svarade med något som inte är JSON. Pekar "
                          "homey_host på rätt enhet?")


# -- helpers ---------------------------------------------------------------


def _fail(message: str) -> dict:
    return {"ok": False, "error": message, "at": int(time.time()), "source": "homey"}


def _base_url(host: str) -> str:
    host = (host or "").strip().rstrip("/")
    if not host:
        return ""
    if "://" not in host:
        host = "http://" + host
    return host


def _split_names(value) -> list:
    """Split the whitelist.

    Commas take precedence over whitespace, because real Homey device names
    contain spaces ("AC uppe", "Sovrum master") and a whitespace-only split
    would turn one sensor into two entries that match nothing.
    """
    if isinstance(value, (list, tuple)):
        parts = [str(v) for v in value]
    else:
        text = str(value or "")
        parts = text.split(",") if "," in text else text.split()
    return [p.strip() for p in parts if p.strip()]


def _number(value, default: float) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


def _text(value):
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _temperature(value):
    """A number in a believable range, or None. Booleans are not temperatures."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    v = float(value)
    if v != v or not PLAUSIBLE_C[0] <= v <= PLAUSIBLE_C[1]:   # NaN or nonsense
        return None
    return v


_FRACTION = re.compile(r"\.(\d+)")


def _parse_time(value):
    """Homey's lastUpdated as a unix timestamp, or None.

    Two formats in the wild: epoch milliseconds (what the firmware measured
    here actually sends) and the ISO-8601 string with a trailing Z from the
    documentation. dateutil is not a dependency, and datetime.fromisoformat
    before 3.11 rejects both the Z and a fractional part that is not exactly
    3 or 6 digits, so both are normalised by hand first.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = float(value)
        if n <= 0:
            return None
        # Milliseconds since 1970 passed 1e12 in 2001 and seconds will not
        # reach 1e11 until the year 5138, so the boundary is unambiguous.
        return n / 1000.0 if n > 1e11 else n
    text = _text(value)
    if text is None:
        return None
    if text[-1] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    match = _FRACTION.search(text)
    if match:
        micros = match.group(1)[:6].ljust(6, "0")
        text = text[:match.start()] + "." + micros + text[match.end():]
    try:
        stamp = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        # Homey reports UTC; assuming local time here would make a fresh
        # reading look two hours stale every summer.
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return stamp.timestamp()


def _zone_names(zones) -> dict:
    """id -> human zone name, from /api/manager/zones/zone."""
    out: dict = {}
    if isinstance(zones, dict):
        items = zones.items()
    elif isinstance(zones, list):
        items = [(z.get("id"), z) for z in zones if isinstance(z, dict)]
    else:
        return out
    for zone_id, zone in items:
        if not isinstance(zone, dict):
            continue
        name = _text(zone.get("name"))
        if zone_id and name:
            out[str(zone_id)] = name
    return out


if __name__ == "__main__":
    import os

    homey = Homey(
        host=os.environ.get("NIBE_HOMEY_HOST", ""),
        token=os.environ.get("NIBE_HOMEY_TOKEN", ""),
        devices=os.environ.get("NIBE_HOMEY_DEVICES", ""),
        max_age_minutes=_number(os.environ.get("NIBE_HOMEY_MAX_AGE_MINUTES"), 60.0),
        timeout=_number(os.environ.get("NIBE_HOMEY_TIMEOUT"), 5.0),
    )
    if not homey.configured:
        print("Set NIBE_HOMEY_HOST and NIBE_HOMEY_TOKEN to try this against a "
              "real Homey. Printing the unconfigured error dict instead:")
    print(json.dumps(homey.snapshot(), indent=1, ensure_ascii=False))
