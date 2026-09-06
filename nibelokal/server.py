"""The web app: a small JSON API plus the static PWA, on stdlib http.server.

No framework on purpose. This runs on a Raspberry Pi, a NAS or a Mac mini for
years without anyone updating its dependencies, which is the whole point of not
renting the same feature from a cloud.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import posixpath
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .pump import DASHBOARD
from .safety import Refused, tier
from .store import Poller, Store

log = logging.getLogger("nibelokal.server")


class Handler(BaseHTTPRequestHandler):
    server_version = "nibe-lokal"
    ctx: dict = {}

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

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
        return secrets.compare_digest(given, token)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return {}

    # -- routing ---------------------------------------------------------

    def do_GET(self):                                     # noqa: N802
        route = urlparse(self.path)
        if route.path.startswith("/api/"):
            if not self._authorised():
                return self._json({"error": "unauthorised"}, 401)
            return self._api_get(route)
        return self._static(route.path)

    def do_POST(self):                                    # noqa: N802
        route = urlparse(self.path)
        if not route.path.startswith("/api/"):
            return self._json({"error": "not found"}, 404)
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
            return self._json({
                "pump": {"host": pump.host, "port": pump.port},
                "register_map": pump.registry.source,
                "polled_at": poller.last_ok,
                "poll_error": poller.last_error,
                "registers": out,
            })

        if path == "/api/fan":
            return self._json({"speeds": pump.fan_speeds()})

        if path == "/api/search":
            needle = q.get("q", [""])[0]
            writable = q.get("writable", ["0"])[0] == "1"
            hits = pump.registry.search(needle, writable)[:200]
            return self._json([dict(r.as_dict(), tier=tier(r.address)) for r in hits])

        if path.startswith("/api/register/"):
            address = int(path.rsplit("/", 1)[1])
            reg = pump.registry.get(address)
            if reg is None:
                return self._json({"error": "no such register"}, 404)
            return self._json(dict(reg.as_dict(), value=pump.read(address), tier=tier(address)))

        if path == "/api/history":
            address = int(q.get("address", ["30009"])[0])
            hours = max(1, min(24 * 400, int(q.get("hours", ["24"])[0])))
            return self._json({"address": address, "hours": hours,
                               "points": store.series(address, hours)})

        if path == "/api/log":
            return self._json(store.last_writes(int(q.get("limit", ["50"])[0])))

        if path == "/api/backups":
            directory = self.ctx["backup_dir"]
            files = []
            if os.path.isdir(directory):
                for name in sorted(os.listdir(directory), reverse=True):
                    if name.endswith(".json") and name != "latest.json":
                        full = os.path.join(directory, name)
                        files.append({"name": name, "bytes": os.path.getsize(full),
                                      "taken_at": os.path.getmtime(full)})
            return self._json({"directory": directory, "backups": files,
                               "history": store.stats()})

        return self._json({"error": "not found"}, 404)

    # -- POST endpoints ---------------------------------------------------

    def _api_post(self, route, body):
        pump = self.ctx["pump"]
        store: Store = self.ctx["store"]
        path = route.path

        if path == "/api/hotwater":
            result = pump.extra_hot_water(int(body.get("minutes", 180)),
                                          bool(body.get("off", False)))
            for w in result["writes"]:
                store.record_write(w)
            return self._json(result)

        if path == "/api/ventilation":
            result = pump.ventilate(str(body.get("direction", "up")),
                                    int(body.get("hours", 3)),
                                    body.get("mode"))
            for w in result["writes"]:
                store.record_write(w)
            return self._json(result)

        if path == "/api/write":
            if "address" not in body:
                raise ValueError("address is required")
            result = pump.write(int(body["address"]), body.get("value"),
                                bool(body.get("confirm", False)))
            store.record_write(result)
            return self._json(result)

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


def serve(pump, cfg: dict, base: str, listen: str, port: int) -> int:
    from .config import resolve

    store = Store(resolve(cfg, "database", base) or os.path.join(base, "nibe.db"),
                  cfg["history_days"])
    poller = Poller(pump, store, DASHBOARD, cfg["poll_seconds"])
    poller.start()

    Handler.ctx = {
        "pump": pump,
        "store": store,
        "poller": poller,
        "token": os.environ.get("NIBE_TOKEN", cfg.get("auth_token") or ""),
        "backup_dir": resolve(cfg, "backup_dir", base) or os.path.join(base, "backup"),
        "web_root": os.path.join(base, "web"),
    }

    httpd = ThreadingHTTPServer((listen, port), Handler)
    where = "http://%s:%d/" % ("localhost" if listen in ("0.0.0.0", "") else listen, port)
    print("nibe-lokal serving %s" % where)
    print("  pump         : %s:%d" % (pump.host, pump.port))
    print("  register map : %s" % pump.registry.source)
    print("  polling      : every %d s" % cfg["poll_seconds"])
    if not Handler.ctx["token"]:
        print("  auth         : none - anyone on your LAN can change settings.")
        print("                 Set auth_token in config.yaml to require a token.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        poller.stop()
        pump.mb.close()
    return 0
