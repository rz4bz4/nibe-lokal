"""The web app: a small JSON API plus the static PWA, on stdlib http.server.

No framework on purpose. This runs on a Raspberry Pi, a NAS or a Mac mini for
years without anyone updating its dependencies, which is the whole point of not
renting the same feature from a cloud.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import mimetypes
import os
import posixpath
import re
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import advisor, autotune, settings
from .alarms import Watcher
from .homey import Homey
from .pump import DASHBOARD
from .safety import Refused, tier
from .spot import Plan, Tibber
from .store import Poller, Store
from .weather import Weather

log = logging.getLogger("nibelokal.server")


#: ?token=... in a URL. Written into the DEBUG request log under -v, and log
#: files are what people paste into bug reports.
_TOKEN_IN_URL = re.compile(r"([?&]token=)[^&\s]*")


def _redact(text: str) -> str:
    return _TOKEN_IN_URL.sub(r"\1<dold>", text)


def _as_int(value, name: str, default=None, low=None, high=None) -> int:
    """An integer out of a request, or a ValueError the router turns into 400.

    int(None) is a TypeError and int("2e9") a ValueError, and both reached the
    catch-all as a 502 with a stack trace -- `{"minutes": null}` is a bad
    request, not a broken server. The bounds are here too, because
    `?limit=100000000000000000000` is an integer Python is perfectly happy with
    and SQLite is not (OverflowError, once, deep inside the query).
    """
    if value is None or value == "":
        if default is None:
            raise ValueError("%s is required" % name)
        return int(default)
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError("%s must be a whole number (got %r)" % (name, value))
    if low is not None and number < low:
        raise ValueError("%s must be at least %d" % (name, low))
    if high is not None and number > high:
        raise ValueError("%s may be at most %d" % (name, high))
    return number


def _field_int(body: dict, name: str, default=None, low=None, high=None) -> int:
    """An integer field out of a JSON body. Absent is not the same as null.

    A key that is not there means "I did not say", and the default is the right
    answer. A key that is there holding null means the client thinks it *is*
    saying something -- and quietly reading `{"minutes": null}` as three hours
    of extra hot water is not it. It used to be neither: body.get() flattened
    the two together and int(None) then left as a 502 with a stack trace.
    """
    if name not in body:
        if default is None:
            raise ValueError("%s is required" % name)
        return int(default)
    if body[name] is None:
        raise ValueError("%s must be a whole number, not null" % name)
    return _as_int(body[name], name, default, low, high)


def _flag(value) -> bool:
    """Booleans off the wire. `"false"` is a string, and a truthy one -- taking
    it at face value once turned "off: false" into switching hot water off."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _log_writes(store, writes) -> bool:
    """Record what was written. Never let this decide the response.

    By the time we get here the pump has already changed. A locked database or
    a full disk must not turn a completed write into a 502 the caller will
    retry -- the retry then trips the `expect` guard and reads as a bug.
    """
    try:
        for w in writes:
            store.record_write(w)
        return True
    except Exception as exc:                              # noqa: BLE001
        log.warning("write succeeded but could not be logged: %s", exc)
        return False


def _not_started(feature: str, why: str | None) -> dict:
    """The body an endpoint answers with when its provider would not build.

    Still HTTP 200 and still the same shape as every other failure here: the
    web app renders one Swedish sentence in a panel instead of showing the
    household a stack trace, and the pump page keeps working around it.
    """
    return {
        "ok": False,
        "error": "%s kunde inte startas: %s. Kontrollera inställningarna i "
                 "config.yaml." % (feature, why or "okänt fel"),
    }


def build_providers(pump, cfg: dict, store: Store) -> dict:
    """The optional integrations, built once at startup.

    Once, not per request: each of these holds a cache and a lock, and building
    a fresh one for every GET would throw the cache away and turn a page
    refresh into a round trip to Homey, SMHI and Tibber.

    Each is built behind its own try. A constructor that trips over a bad
    config value costs that one feature -- its endpoint then answers ok: false
    with the reason -- rather than stopping the app that heats the house.
    """
    providers: dict = {"homey": None, "weather": None, "tibber": None,
                       "plan": None, "watcher": None, "errors": {}}
    builders = (
        ("homey", lambda: Homey.from_config(cfg)),
        ("weather", lambda: Weather(cfg)),
        ("tibber", lambda: Tibber(cfg)),
        ("plan", lambda: Plan(cfg)),
        # The watcher writes its own tables into the same database, which is
        # the one step here that can fail on a read-only disk.
        ("watcher", lambda: Watcher(store, pump, cfg)),
    )
    for name, build in builders:
        try:
            providers[name] = build()
        except Exception as exc:                          # noqa: BLE001
            providers["errors"][name] = str(exc)
            log.warning("optional feature %s not available: %s", name, exc)
    return providers


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


#: Seconds a request may spend not sending. BaseHTTPRequestHandler sets no
#: timeout at all, so a phone that drops WiFi in the middle of a POST leaves
#: its thread parked in rfile.read() with its socket open -- for ever. This
#: server runs a thread per request behind no proxy, so that is a thread and
#: two file descriptors per dropped connection, which is the same
#: descriptor-exhaustion failure an earlier release shipped with the sqlite
#: connections. Measured before the fix: the thread count grew and never fell.
REQUEST_TIMEOUT = 30.0

#: The largest JSON body any endpoint here has a use for. The biggest real one
#: is a write_all of a handful of registers; a kilobyte would do. This is the
#: cap that makes Content-Length something we can trust before reading it.
MAX_BODY_BYTES = 64 * 1024


class Handler(BaseHTTPRequestHandler):
    server_version = "nibe-lokal"
    ctx: dict = {}
    #: socketserver sets this on the connection before the first line is read.
    timeout = REQUEST_TIMEOUT

    def log_message(self, fmt, *args):
        # The query string can carry ?token=..., which is how the auth token
        # ends up in a log file under -v -- and log files get pasted into bug
        # reports. Everything else about the request is kept.
        log.debug("%s %s", self.address_string(), _redact(fmt % args))

    def handle_one_request(self):
        """As the base class, but a timed-out connection is closed, not logged.

        Without an override, a socket that goes quiet raises a timeout out of
        the base class's readline and BaseHTTPRequestHandler turns it into a
        traceback in the log; the thread is then finished either way. This just
        says so quietly and lets the thread end.
        """
        try:
            super().handle_one_request()
        except (TimeoutError, socket.timeout):
            self.close_connection = True
            log.debug("connection from %s timed out", self.address_string())

    # -- helpers ---------------------------------------------------------

    def _json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self) -> bool:
        token = self.ctx.get("token")
        if not token:
            return True
        given = self.headers.get("X-Auth-Token") or ""
        if not given:
            given = parse_qs(urlparse(self.path).query).get("token", [""])[0]
        # Bytes, not str: compare_digest raises TypeError on a str with a
        # character above U+00FF, and this is called outside the try that turns
        # exceptions into responses -- so a token with an emoji in it dropped
        # the connection with a traceback instead of answering 401. Comparing
        # the UTF-8 encodings is the same comparison, in constant time, for
        # every string a client can send.
        return secrets.compare_digest(given.encode("utf-8"), token.encode("utf-8"))

    def _same_origin(self) -> bool:
        """Refuse requests a foreign web page made on the browser's behalf.

        Modbus has no authentication and this app may run without a token, so
        without this any page the household happens to open could POST
        "operating mode = additional heat only" to the pump. Requiring a header
        the browser will not send cross-origin without a preflight, plus a Host
        check, closes both CSRF and DNS rebinding without a session model.
        """
        origin = self.headers.get("Origin")
        if origin:
            host = self.headers.get("Host") or ""
            if origin.split("://")[-1] != host:
                return False
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        allowed = self.ctx.get("allowed_hosts") or set()
        if not allowed or host in allowed:
            return True
        # A bare IP address cannot be DNS-rebound -- rebinding needs a *name*
        # whose resolution can be changed. Reaching the app at 192.168.1.5 or a
        # Tailscale address is the normal case, so allow any literal address and
        # keep the check for names.
        return _is_ip_literal(host)

    def _body(self) -> dict:
        """The JSON body, as a dict. Anything else is a bad request.

        Three things this has to survive, all of them reproduced:

        * `Content-Length: -1` -- int() accepts it and rfile.read(-1) then
          reads until the client closes the connection, which a client that
          never closes never does.
        * A length that lies: 500 announced, two bytes sent. read(500) blocks
          on the rest until the socket timeout (see REQUEST_TIMEOUT) rather
          than for ever, and a short read is a bad request, not an empty body.
        * A body that is valid JSON but not an object -- `[1,2]`, `"hi"`,
          `null`. Every caller below does body.get(...), so a list arrived as
          an AttributeError and left as a 502 with a stack trace.
        """
        raw = self.headers.get("Content-Length")
        try:
            length = int(raw or 0)
        except (TypeError, ValueError):
            raise ValueError("Content-Length is not a number")
        if length < 0:
            raise ValueError("Content-Length may not be negative")
        if length > MAX_BODY_BYTES:
            raise ValueError("the body is larger than %d bytes" % MAX_BODY_BYTES)
        if not length:
            return {}
        try:
            data = self.rfile.read(length)
        except (TimeoutError, socket.timeout, OSError) as exc:
            # The socket timeout from REQUEST_TIMEOUT, nearly always: the
            # client announced a body and stopped sending. Said as the bad
            # request it is rather than as a 502 blaming the pump.
            raise ValueError("the request body did not arrive (%s)" % exc)
        if len(data) < length:
            raise ValueError("the request body was shorter than Content-Length said")
        try:
            body = json.loads(data or b"{}")
        except ValueError:
            return {}
        if not isinstance(body, dict):
            raise ValueError("the request body must be a JSON object")
        return body

    # -- routing ---------------------------------------------------------

    def do_GET(self):                                     # noqa: N802
        route = urlparse(self.path)
        if not route.path.startswith("/api/"):
            return self._static(route.path)
        if not self._same_origin():
            return self._json({"error": "cross-origin request refused"}, 403)
        if not self._authorised():
            return self._json({"error": "unauthorised"}, 401)
        try:
            return self._api_get(route)
        except Refused as exc:
            return self._json({"error": str(exc), "refused": True}, 403)
        except (KeyError, ValueError) as exc:
            return self._json({"error": str(exc)}, 400)
        except Exception as exc:                          # noqa: BLE001
            # Without this the pump-is-unreachable message never reaches the
            # browser: the connection just closes and the app says nothing useful.
            log.warning("GET %s failed: %s", route.path, exc)
            return self._json({"error": str(exc)}, 502)

    def do_POST(self):                                    # noqa: N802
        route = urlparse(self.path)
        if not route.path.startswith("/api/"):
            return self._json({"error": "not found"}, 404)
        if not self._same_origin():
            return self._json({"error": "cross-origin request refused"}, 403)
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            return self._json({"error": "POST requires Content-Type: application/json"}, 415)
        if not self._authorised():
            return self._json({"error": "unauthorised"}, 401)
        try:
            return self._api_post(route, self._body())
        except Refused as exc:
            return self._json({"error": str(exc), "refused": True}, 403)
        except (KeyError, ValueError) as exc:
            return self._json({"error": str(exc)}, 400)
        except Exception as exc:                          # noqa: BLE001
            log.exception("write failed")
            return self._json({"error": str(exc)}, 502)

    # -- GET endpoints ----------------------------------------------------

    def _api_get(self, route):
        pump = self.ctx["pump"]
        store: Store = self.ctx["store"]
        poller: Poller = self.ctx["poller"]
        path = route.path
        q = parse_qs(route.query)

        if path == "/api/status":
            live = q.get("live", ["0"])[0] == "1"
            data = pump.read_many(DASHBOARD) if live else (poller.latest or pump.read_many(DASHBOARD))
            out = []
            for address, row in sorted(data.items()):
                reg = pump.registry.get(address)
                out.append({
                    "address": address,
                    "title": reg.title if reg else str(address),
                    "unit": reg.unit if reg else "",
                    "value": row.get("value"),
                    "error": row.get("error"),
                })
            # Additive on purpose: registers, polled_at, poll_error and
            # register_map keep their names and their meanings, because the web
            # app reads them by name and an older cached index.html has to keep
            # working against a newer server.
            body = {
                "pump": {"host": pump.host, "port": pump.port},
                "register_map": pump.registry.source,
                "polled_at": poller.last_ok,
                "poll_error": poller.last_error,
                # A database that will not take the readings is its own
                # failure, and no longer reported as the pump having failed.
                "store_error": getattr(poller, "last_store_error", None),
                # Registers this pump refused, and is therefore not being
                # polled for -- until they are retried, see pump.missing().
                # Without this a register that quietly dropped out of every
                # poll was invisible, and 31976 dropping out means the alarm
                # watching has stopped.
                "missing_registers": pump.missing() if hasattr(pump, "missing") else [],
                "registers": out,
            }
            body.update(self._summaries())
            return self._json(body)

        if path == "/api/heating":
            return self._json(advisor.diagnose(pump, store, self.ctx["emitters"]))

        if path == "/api/advice":
            return self._json(advisor.advise(
                pump,
                q.get("feeling", ["warmer"])[0],
                q.get("when", ["always"])[0],
                store,
                self.ctx["emitters"],
            ).as_dict())

        if path == "/api/settings":
            values = pump.read_many(settings.ADDRESSES)
            return self._json({"groups": settings.build(pump, values)})

        if path == "/api/fan":
            return self._json({"speeds": pump.fan_speeds()})

        if path == "/api/search":
            needle = q.get("q", [""])[0]
            writable = q.get("writable", ["0"])[0] == "1"
            hits = pump.registry.search(needle, writable)[:200]
            return self._json([dict(r.as_dict(), tier=tier(r.address)) for r in hits])

        if path.startswith("/api/register/"):
            address = _as_int(path.rsplit("/", 1)[1], "address")
            reg = pump.registry.get(address)
            if reg is None:
                return self._json({"error": "no such register"}, 404)
            return self._json(dict(reg.as_dict(), value=pump.read(address), tier=tier(address)))

        if path == "/api/history":
            address = _as_int(q.get("address", ["30009"])[0], "address")
            hours = _as_int(q.get("hours", ["24"])[0], "hours", 24, 1, 24 * 400)
            return self._json({"address": address, "hours": hours,
                               "points": store.series(address, hours)})

        if path == "/api/log":
            limit = _as_int(q.get("limit", ["50"])[0], "limit", 50, 1, 500)
            return self._json(store.last_writes(limit))

        if path == "/api/backups":
            directory = self.ctx["backup_dir"]
            files = []
            if os.path.isdir(directory):
                for name in sorted(os.listdir(directory), reverse=True):
                    if name.endswith(".json") and name != "latest.json":
                        full = os.path.join(directory, name)
                        files.append({"name": name, "bytes": os.path.getsize(full),
                                      "taken_at": os.path.getmtime(full)})
            newest = max((f["taken_at"] for f in files), default=None)
            return self._json({
                "directory": directory, "backups": files, "history": store.stats(),
                "newest": newest,
                "age_hours": None if newest is None else (time.time() - newest) / 3600,
                "auto": bool(poller.backup_dir),
                "auto_every_hours": poller.backup_hours,
            })

        # -- optional integrations ---------------------------------------
        # All five answer 200 with {"ok": false, "error": "<svensk mening>"}
        # when the service is unconfigured, down or slow. A missing forecast is
        # not a server error, and a 5xx here would put a red banner over a page
        # whose actual job -- the pump -- is working fine.

        if path == "/api/indoor":
            homey = self.ctx.get("homey")
            if homey is None:
                return self._json(_not_started("Inomhusgivarna (Homey)",
                                               self._provider_error("homey")))
            return self._json(homey.snapshot())

        if path == "/api/weather":
            weather = self.ctx.get("weather")
            if weather is None:
                return self._json(_not_started("Väderprognosen (SMHI)",
                                               self._provider_error("weather")))
            return self._json(weather.snapshot())

        if path == "/api/spot":
            tibber, plan = self.ctx.get("tibber"), self.ctx.get("plan")
            if tibber is None or plan is None:
                return self._json(_not_started("Elpriserna (Tibber)",
                                               self._provider_error("tibber")))
            prices = tibber.snapshot()
            # The plan is built from these exact prices rather than from a
            # second fetch: two calls a second apart can straddle the moment
            # tomorrow's prices land, and a plan that disagrees with the table
            # printed next to it is worse than no plan.
            result = {"ok": bool(prices.get("ok")), "prices": prices,
                      "plan": plan.build(prices)}
            if not result["ok"]:
                result["error"] = prices.get("error") or "Inga elpriser att visa."
            return self._json(result)

        if path == "/api/alarms":
            watcher = self.ctx.get("watcher")
            if watcher is None:
                return self._json(_not_started("Larmbevakningen",
                                               self._provider_error("watcher")))
            limit = _as_int(q.get("limit", ["50"])[0], "limit", 50, 1, 500)
            return self._json({
                "ok": True,
                "active": watcher.active(),
                "history": watcher.history(limit),
                # False means alarms are still recorded and shown here, but
                # nothing leaves the machine. That is the default.
                "notify": watcher.notifier is not None,
                # notify is "credentials are configured"; notify_error is
                # "and the notification service rejected them". Both, because
                # a wrong Pushover token used to be a log line on a machine in
                # a cupboard while this endpoint went on saying notify: true.
                # null when nothing has been rejected.
                "notify_error": watcher.notify_error,
                "last_error": watcher.last_error,
            })

        if path == "/api/autotune":
            return self._json(self._autotune(q))

        return self._json({"error": "not found"}, 404)

    # -- optional integrations --------------------------------------------

    def _provider_error(self, name: str):
        return (self.ctx.get("provider_errors") or {}).get(name)

    def _autotune(self, q) -> dict:
        """The curve analysis, over stored history. Never raises past here."""
        pump = self.ctx["pump"]
        store: Store = self.ctx["store"]
        days = _as_int(q.get("days", [str(self.ctx["autotune_days"])])[0],
                       "days", self.ctx["autotune_days"], 1, 400)

        # The pump's current settings are what lets a proposal name a register
        # and a from-value. A pump that does not answer is not a reason to
        # refuse the analysis -- autotune degrades to the diagnosis without the
        # proposal, and says so itself.
        current, settings_error = None, None
        try:
            current = advisor.diagnose(pump, store, self.ctx["emitters"])
        except Exception as exc:                          # noqa: BLE001
            settings_error = str(exc)
            log.warning("autotune: could not read the pump's settings: %s", exc)

        target = self.ctx["autotune_target"]
        source = "config" if target is not None else None
        if target is None and current is not None:
            # The pump's own room setpoint, when nobody has said otherwise. It
            # is what the household set on the display, which is the closest
            # thing to a stated wish that exists without a config key.
            target = current.get("room_setpoint")
            source = "pump" if target is not None else None

        result = autotune.analyse(store.autotune_history(days), target, current,
                                  self.ctx["emitters"])
        # Additive keys, so a caller can say what the verdict was measured
        # against without going and reading the config itself.
        result["target_indoor"] = target
        result["target_source"] = source
        result["history_days"] = days
        result["settings_error"] = settings_error
        return result

    def _summaries(self) -> dict:
        """Header-line summaries for /api/status, plus what is configured.

        Deliberately small: a couple of numbers each, not the whole snapshot.
        The full data has its own endpoint, and /api/status is the one the page
        polls on a timer.
        """
        homey = self.ctx.get("homey")
        weather = self.ctx.get("weather")
        watcher = self.ctx.get("watcher")
        tibber = self.ctx.get("tibber")
        configured = {
            "indoor": bool(homey is not None and homey.configured),
            "weather": bool(weather is not None and weather.configured),
            "spot": bool(tibber is not None and tibber.configured),
            # Alarm watching needs no credentials: the register is polled with
            # everything else. Only the push off the machine is optional.
            "alarms": watcher is not None,
            "notify": bool(watcher is not None and watcher.notifier is not None),
            # An indoor source is the hard requirement: without a room
            # temperature there is nothing to compare a curve against. A target
            # is *not* -- /api/autotune falls back to the pump's own room
            # setpoint, which is what the household set on the display, and
            # says which of the two it used in `target_source`. This flag used
            # to require autotune_target_indoor as well, which hid a feature
            # that worked.
            "autotune": bool(homey is not None and homey.configured),
        }
        return {
            "indoor": self._indoor_summary(homey),
            "weather": self._weather_summary(weather),
            "alarm": self._alarm_summary(watcher),
            "features": configured,
        }

    def _indoor_summary(self, homey) -> dict:
        # Never fetched from here, the same rule the forecast follows below.
        # snapshot() goes to the LAN whenever the cache is cold, and a Homey
        # that accepts the connection and never answers then made this
        # endpoint -- which the page polls every 30 s -- wait out two 5 s
        # timeouts. The poller keeps the cache warm (it now does so even when
        # the pump poll failed, which is when it used to stop); this only
        # reads it, and says so plainly when there is nothing there yet.
        if homey is None or not homey.configured:
            return {"ok": False, "configured": False, "average": None,
                    "sensors": 0, "stale": 0, "at": None, "error": None}
        try:
            snap = (homey.cached_snapshot()
                    if hasattr(homey, "cached_snapshot") else homey.snapshot())
            if snap is None:
                return {"ok": False, "configured": True, "average": None,
                        "sensors": 0, "stale": 0, "at": None,
                        "error": "Inomhustemperaturen är inte hämtad ännu."}
            sensors = snap.get("sensors") or []
            return {
                "ok": bool(snap.get("ok")),
                "configured": True,
                "average": snap.get("average"),
                "at": snap.get("at"),
                "sensors": len(sensors),
                "stale": sum(1 for s in sensors if s.get("stale")),
                "error": snap.get("error") or snap.get("warning"),
            }
        except Exception as exc:                          # noqa: BLE001
            return {"ok": False, "configured": True, "average": None,
                    "sensors": 0, "stale": 0, "at": None, "error": str(exc)}

    def _weather_summary(self, weather) -> dict:
        empty = {"ok": False, "configured": False, "t": None, "effective": None,
                 "symbol_sv": "", "min_24h": None, "trend": "", "at": None,
                 "stale": False, "error": None}
        if weather is None or not weather.configured:
            return empty
        # Unlike Homey, this one is never fetched from here. Homey is on the
        # LAN and kept warm by the poller; SMHI is on the internet and warmed
        # by nobody, so a cold cache would make one status call an hour wait
        # out the timeout of the endpoint everything else polls. Only what has
        # already been fetched by /api/weather is summarised.
        #
        # cached_snapshot(), not `_cached`: `_cached` holds the last SUCCESSFUL
        # fetch and nothing else, so with SMHI down for a day this header went
        # on reporting yesterday's temperature as today's while /api/weather
        # said ok: false. cached_snapshot() answers with what /api/weather
        # would answer -- the failure, or the old forecast marked stale.
        snap = (weather.cached_snapshot()
                if hasattr(weather, "cached_snapshot")
                else getattr(weather, "_cached", None))   # noqa: SLF001
        if not isinstance(snap, dict):
            return dict(empty, configured=True,
                        error="Prognosen är inte hämtad ännu.")
        now = snap.get("now") or {}
        summary = snap.get("summary") or {}
        return {
            "ok": bool(snap.get("ok")),
            "configured": True,
            "t": now.get("t"),
            "effective": now.get("effective"),
            "symbol_sv": now.get("symbol_sv") or "",
            "min_24h": summary.get("min_24h"),
            "trend": summary.get("trend") or "",
            "at": snap.get("at"),
            # Additive: true when these numbers are the last forecast rather
            # than the current one. `error` carries the Swedish sentence saying
            # so, as it always has, whether it came from a failure or from the
            # stale marking.
            "stale": bool(snap.get("stale")),
            "error": snap.get("note") or snap.get("error"),
        }

    def _alarm_summary(self, watcher) -> dict:
        if watcher is None:
            return {"ok": False, "active": 0, "code": None, "text": "",
                    "severity": None, "notify": False, "notify_error": None,
                    "error": self._provider_error("watcher")}
        notify_error = getattr(watcher, "notify_error", None)
        try:
            active = watcher.active()
        except Exception as exc:                          # noqa: BLE001
            return {"ok": False, "active": 0, "code": None, "text": "",
                    "severity": None, "notify": watcher.notifier is not None,
                    "notify_error": notify_error, "error": str(exc)}
        first = active[0] if active else {}
        return {
            "ok": True,
            "active": len(active),
            # The oldest standing alarm, which is the one that started the
            # trouble; the rest are at /api/alarms.
            "code": first.get("code"),
            "text": first.get("text") or "",
            "severity": first.get("severity"),
            "notify": watcher.notifier is not None,
            # A configuration the notification service rejected, in Swedish, or
            # null. The page shows it next to the notify flag: "notiser på" and
            # "Pushover avvisade inloggningen" are both true at once, and only
            # saying the first is how somebody finds out at the wrong moment.
            "notify_error": notify_error,
            "error": watcher.last_error,
        }

    # -- POST endpoints ---------------------------------------------------

    def _api_post(self, route, body):
        pump = self.ctx["pump"]
        store: Store = self.ctx["store"]
        path = route.path

        if path == "/api/hotwater":
            result = pump.extra_hot_water(
                _field_int(body, "minutes", 180),
                _flag(body.get("off")))
            result["logged"] = _log_writes(store, result["writes"])
            return self._json(result)

        if path == "/api/ventilation":
            result = pump.ventilate(str(body.get("direction") or "up"),
                                    _field_int(body, "hours", 3),
                                    body.get("mode"))
            result["logged"] = _log_writes(store, result["writes"])
            return self._json(result)

        if path == "/api/write":
            if isinstance(body.get("changes"), list) and body["changes"]:
                # write_all already answers {"changes": [...], "partial": bool}.
                # Wrapping that whole dict in another "changes" key -- which an
                # earlier version did -- buried the list one level down and hid
                # "partial" entirely: the browser got an object where it expected
                # an array, the write log was handed dict keys instead of write
                # records and silently stored nothing, and a half-applied pair
                # reported as a clean success. Answer the dict as it stands.
                result = pump.write_all(body["changes"], _flag(body.get("confirm")))
                result["logged"] = _log_writes(store, result["changes"])
                return self._json(result)
            if "address" not in body:
                raise ValueError("address is required")
            result = pump.write(_field_int(body, "address"), body.get("value"),
                                _flag(body.get("confirm")),
                                body.get("expect"))
            result["logged"] = _log_writes(store, [result])
            return self._json(result)

        if path == "/api/alarms/test":
            # A notification somebody asked for. The alternative way to find
            # out that a token was pasted with a character missing is to wait
            # for a real alarm and then not hear about it.
            watcher = self.ctx.get("watcher")
            if watcher is None:
                return self._json(_not_started("Larmbevakningen",
                                               self._provider_error("watcher")))
            return self._json(watcher.send_test_notification())

        if path == "/api/backup":
            path_out = pump.backup(self.ctx["backup_dir"], str(body.get("note", "")))
            return self._json({"path": path_out, "name": os.path.basename(path_out)})

        return self._json({"error": "not found"}, 404)

    # -- static ------------------------------------------------------------

    def _static(self, path: str):
        root = self.ctx["web_root"]
        rel = posixpath.normpath(path).lstrip("/") or "index.html"
        if rel.startswith(".."):
            return self._json({"error": "not found"}, 404)
        full = os.path.join(root, rel)
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        if not os.path.isfile(full):
            full = os.path.join(root, "index.html")
        if not os.path.isfile(full):
            return self._json({"error": "the web/ directory is missing"}, 500)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)


def _optional_number(value):
    """A number out of the config, or None for "not set". Never raises."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_context(pump, cfg: dict, base: str, store, poller, providers: dict,
                  allowed_hosts=None, backup_dir: str | None = None) -> dict:
    """Everything a request needs, assembled once.

    Its own function so that a test can build the exact dict the server runs
    with. A handler reading a key the real context does not have is a 500 in
    the field and a passing test everywhere else.
    """
    from .config import resolve

    return {
        "pump": pump,
        "allowed_hosts": set() if allowed_hosts is None else allowed_hosts,
        "store": store,
        "poller": poller,
        "token": os.environ.get("NIBE_TOKEN", cfg.get("auth_token") or ""),
        "emitters": cfg.get("emitters") or "radiators",
        "backup_dir": backup_dir or resolve(cfg, "backup_dir", base)
                      or os.path.join(base, "backup"),
        "web_root": os.path.join(base, "web"),
        "homey": providers.get("homey"),
        "weather": providers.get("weather"),
        "tibber": providers.get("tibber"),
        "plan": providers.get("plan"),
        "watcher": providers.get("watcher"),
        "provider_errors": providers.get("errors") or {},
        # Coerced here rather than in the handler: "" is how the config says
        # "not set", and float("") is an exception on every request otherwise.
        "autotune_target": _optional_number(cfg.get("autotune_target_indoor")),
        "autotune_days": int(cfg.get("autotune_days") or 30),
    }


def serve(pump, cfg: dict, base: str, listen: str, port: int) -> int:
    from .config import resolve

    store = Store(resolve(cfg, "database", base) or os.path.join(base, "nibe.db"),
                  cfg["history_days"])
    backup_dir = resolve(cfg, "backup_dir", base) or os.path.join(base, "backup")
    providers = build_providers(pump, cfg, store)
    # The watcher and the indoor feed ride along on the poll the pump is
    # already answering: one Modbus read, and the alarm registers are in
    # DASHBOARD, so watching costs nothing extra on the wire.
    poller = Poller(pump, store, DASHBOARD, cfg["poll_seconds"],
                    backup_dir if cfg.get("auto_backup_hours") else None,
                    float(cfg.get("auto_backup_hours") or 24),
                    watcher=providers["watcher"], indoor=providers["homey"])
    poller.start()

    # A Host header that is not one of these means someone resolved a name of
    # their own to this address -- classic DNS rebinding. Add your own name here
    # via `allowed_hosts` in config.yaml if you front this with a proxy.
    allowed = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
    if listen not in ("0.0.0.0", ""):
        allowed.add(listen)
    for extra in str(cfg.get("allowed_hosts") or "").replace(",", " ").split():
        allowed.add(extra)
    if cfg.get("allow_any_host"):
        allowed = set()
    else:
        try:
            import socket as _s
            allowed.add(_s.gethostbyname(_s.gethostname()))
            allowed.add(_s.gethostname())
            allowed.add(_s.gethostname().split(".")[0] + ".local")
        except OSError:
            pass

    Handler.ctx = build_context(pump, cfg, base, store, poller, providers,
                                allowed, backup_dir)

    httpd = ThreadingHTTPServer((listen, port), Handler)
    where = "http://%s:%d/" % ("localhost" if listen in ("0.0.0.0", "") else listen, port)
    print("nibe-lokal serving %s" % where)
    print("  pump         : %s:%d" % (pump.host, pump.port))
    print("  register map : %s" % pump.registry.source)
    print("  polling      : every %d s" % cfg["poll_seconds"])
    if not Handler.ctx["token"]:
        print("  auth         : none - anyone on your LAN can change settings.")
        print("                 Set auth_token in config.yaml to require a token.")
    if poller.backup_dir:
        print("  auto backup  : every %g h into %s" % (poller.backup_hours, backup_dir))
    # Say which of the optional features are on. An integration that is
    # silently off looks exactly like one that is broken, and this is the line
    # that tells them apart without opening the browser.
    extras = []
    if providers["homey"] is not None and providers["homey"].configured:
        extras.append("inomhus (Homey)")
    if providers["weather"] is not None and providers["weather"].configured:
        extras.append("väder (SMHI)")
    if providers["tibber"] is not None and providers["tibber"].configured:
        extras.append("elpris (Tibber)")
    if providers["watcher"] is not None:
        extras.append("larmbevakning%s" % ("" if providers["watcher"].notifier
                                           else " (utan notiser)"))
    print("  extras       : %s" % (", ".join(extras) if extras else "inga"))
    for name, why in sorted(providers["errors"].items()):
        print("  %-13s: kunde inte startas - %s" % (name, why))
    if allowed:
        print("  accepts Host : any IP address, plus %s" % ", ".join(sorted(allowed)))
        print("                 add other names with allowed_hosts in config.yaml")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        poller.stop()
        pump.mb.close()
    return 0
