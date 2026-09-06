"""Stubbad UI-rok: kor hela appen mot pahittad men realistisk JSON.

Ingen pump, ingen Homey, ingen Tibber och inget SMHI behovs. Servern harinne
svarar pa allt appen fragar efter och kan stallas i tre lagen:

    full   -- alla integrationer konfigurerade, larm aktivt, plan for dygnet
    quiet  -- allt konfigurerat men urlakat: inga larm, platt elpris, for lite
              underlag for kalibrering, en givare som slutat svara
    off    -- ingen integration konfigurerad (features=false + ok:false).
              Da ska ingenting av det nya synas alls -- varken fel eller
              tomma rutor -- utom tipset under Installningar.

Kor allt (startar servern sjalv, tar skarmbilder i _shots/):

    python3 tests/ui_smoke_new.py

Bara servern, sa att gamla tests/ui_smoke.py kan koras mot den:

    python3 tests/ui_smoke_new.py --serve --mode full --port 8391
    NIBE_UI=http://127.0.0.1:8391/ python3 tests/ui_smoke.py
"""
from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import socket
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
sys.path.insert(0, str(ROOT))

from nibelokal import settings as nsettings          # noqa: E402
from nibelokal import safety                          # noqa: E402

NOW = time.time()


# --------------------------------------------------------------------------
# pahittad pumpdata
# --------------------------------------------------------------------------
REGISTERS = [
    (30002, "Outdoor temperature BT1", "°C", -4.2),
    (30006, "Supply line BT2", "°C", 38.4),
    (30008, "Return line BT3", "°C", 33.1),
    (30009, "Hot water top BT7", "°C", 52.6),
    (30010, "Hot water charging BT6", "°C", 47.9),
    (30020, "Exhaust air BT20", "°C", 21.4),
    (30117, "Room temperature BT50", "°C", None),
    (31018, "Calculated supply", "°C", 39.0),
    (31026, "Runtime additional heat", "h", 112),
    (31028, "Internal electrical addition", "kW", 0.0),
    (31047, "Compressor frequency", "Hz", 43),
    (31079, "Temporary lux status", "", 0),
    (31088, "Compressor runtime", "h", 18422),
    (31092, "Hot water runtime", "h", 2611),
    (31535, "Compressor starts", "", 9741),
    (31975, "Fan speed", "%", 30),
    (31976, "Alarm number", "", None),   # satts av lage
    (32134, "Exhaust air fan", "%", 30),
    (40012, "Degree minutes", "GM", -142),
    (40031, "Heat offset", "", 2),
    (40057, "Hot water comfort", "", 1),
    (40105, "Ventilation mode", "", 0),
    (40110, "Fan speed normal", "%", 30),
    (40226, "Temporary lux minutes", "min", 0),
]

SETTING_VALUES = {
    40027: 0, 40031: 2, 40046: 46, 40045: 42, 40044: 37, 40043: 32, 40042: 26,
    40041: 21, 40040: 20, 40047: 0, 40048: 0,
    40035: 20, 40039: 55, 40185: 17, 40167: 1, 40094: 60,
    40057: 1, 40064: 47, 40063: 52, 40065: 42, 40062: 55, 40067: 14, 40077: 5,
    40103: 6.0, 40181: 1, 40186: 15, 40189: 4, 40188: 6,
    40203: 0, 40207: 21, 40211: 2,
    40020: -1, 40228: 0, 40229: 24, 40012: -142, 40182: 1,
}
SETTING_UNITS = {40031: "", 40035: "°C", 40039: "°C", 40185: "°C", 40103: "kW",
                 40207: "°C", 40229: "°C", 40012: "GM", 40094: "min", 40067: "dygn"}
SETTING_OPTIONS = {
    40057: [["0", "Small"], ["1", "Medium"], ["2", "Large"], ["3", "Smart control"]],
    40181: [["0", "Nej"], ["1", "Ja"]],
    40182: [["0", "Nej"], ["1", "Ja"]],
    40203: [["0", "Av"], ["1", "På"]],
    40228: [["0", "Av"], ["1", "På"]],
}


def hour_iso(dt: datetime) -> str:
    return dt.replace(minute=0, second=0, microsecond=0).astimezone().isoformat()


def build_settings() -> list:
    out = []
    for g in nsettings.GROUPS:
        rows = []
        for address, label, why in g["registers"]:
            if address not in SETTING_VALUES:
                continue
            rows.append({
                "address": address,
                "label": label,
                "why": why,
                "value": SETTING_VALUES[address],
                "unit": SETTING_UNITS.get(address, "°C" if 40040 <= address <= 40048 else ""),
                "min": 0 if address not in (40031, 40020, 40012) else -10,
                "max": 100 if address != 40012 else 3000,
                "default": None,
                "options": SETTING_OPTIONS.get(address),
                "tier": safety.tier(address),
            })
        if rows:
            out.append({"key": g["key"], "title": g["title"], "intro": g["intro"], "rows": rows})
    return out


def history(address: int, hours: int) -> list:
    pts, n = [], min(240, max(12, hours * 2))
    for i in range(n):
        t = NOW - (n - 1 - i) * (hours * 3600 / n)
        base = {30009: 50, 30002: -3, 30020: 21, 30006: 38,
                40012: -140, 32134: 30, 31047: 42}.get(address, 30)
        amp = {30009: 4.5, 30002: 3.5, 40012: 60, 31047: 8}.get(address, 1.5)
        pts.append([round(t), round(base + amp * math.sin(i / 6.0) + 0.4 * math.sin(i / 1.7), 1)])
    return pts


# --------------------------------------------------------------------------
# pahittad integrationsdata
# --------------------------------------------------------------------------
def indoor(mode: str) -> dict:
    if mode == "off":
        return {"ok": False, "error": "Homey är inte konfigurerad (homey_host saknas i config.yaml)."}
    sensors = [
        {"id": "a1", "name": "Vardagsrum", "zone": "Nere", "value": 20.9, "age_minutes": 3, "stale": False},
        {"id": "a2", "name": "Kök", "zone": "Nere", "value": 20.7, "age_minutes": 6, "stale": False},
        {"id": "a3", "name": "Sovrum", "zone": "Uppe", "value": 21.6, "age_minutes": 2, "stale": False},
        {"id": "a4", "name": "Arbetsrum", "zone": "Uppe", "value": 21.4, "age_minutes": 11, "stale": False},
        {"id": "a5", "name": "Gästrum", "zone": "Uppe", "value": 24.1,
         "age_minutes": 41 * 1440, "stale": True},
    ]
    if mode == "quiet":
        for s in sensors[:2]:
            s["stale"] = True
            s["age_minutes"] = 5200
        return {"ok": True, "at": int(NOW), "sensors": sensors,
                "average": 21.5, "by_zone": {"Uppe": 21.5},
                "warning": "Två givare nere har inte hört av sig på fyra dygn. "
                           "Nedervåningen saknas därför helt i snittet.",
                "source": "homey"}
    return {"ok": True, "at": int(NOW), "sensors": sensors, "average": 21.1,
            "by_zone": {"Nere": 20.8, "Uppe": 21.5},
            "warning": "Gästrummets givare har inte rapporterat sedan 27 juli. "
                       "Den räknas inte in i snittet.",
            "source": "homey"}


def weather(mode: str) -> dict:
    if mode == "off":
        return {"ok": False, "error": "Ingen plats angiven — sätt weather_lat och weather_lon i config.yaml."}
    start = datetime.now().replace(minute=0, second=0, microsecond=0)
    hourly = []
    for i in range(48):
        t = start + timedelta(hours=i)
        temp = -2.0 + 5.0 * math.sin((t.hour - 9) / 24.0 * 2 * math.pi) - i * 0.06
        hourly.append({
            "time": hour_iso(t),
            "t": round(temp, 1),
            "ws": round(2.5 + 1.5 * math.sin(i / 5.0), 1),
            "cloud": 4 + (i % 5),
            "precip": 0.0 if i % 7 else 0.3,
            "symbol": 3 if i % 7 else 8,
            "symbol_sv": "Växlande molnighet" if i % 7 else "Lätt snöfall",
            "effective": round(temp - 2.4, 1),
        })
    coldest = min(hourly[:24], key=lambda h: h["t"])
    daily = []
    for dnum in range(5):
        day = (start + timedelta(days=dnum)).date()
        vals = [h["t"] for h in hourly if h["time"][:10] == day.isoformat()] or [-3.0]
        daily.append({"date": day.isoformat(), "min": round(min(vals), 1),
                      "max": round(max(vals), 1),
                      "mean": round(sum(vals) / len(vals), 1), "samples": len(vals)})
    out = {
        "ok": True, "issued": int(NOW - 1800),
        "now": {"t": hourly[0]["t"], "ws": hourly[0]["ws"], "effective": hourly[0]["effective"],
                "symbol": hourly[0]["symbol"], "symbol_sv": hourly[0]["symbol_sv"]},
        "hourly": hourly, "daily": daily,
        "summary": {
            "min_24h": min(h["t"] for h in hourly[:24]),
            "mean_24h": round(sum(h["t"] for h in hourly[:24]) / 24, 1),
            "min_48h": min(h["t"] for h in hourly),
            "coldest_hour": coldest["time"],
            "trend": "fallande", "trend_c": -2.8,
        },
    }
    if mode == "quiet":
        out["stale"] = True
        out["note"] = "SMHI svarade inte vid senaste försöket; visar förra hämtningen."
    return out


def spot(mode: str) -> dict:
    if mode == "off":
        return {"prices": {"ok": False,
                           "error": "Ingen Tibber-token i config.yaml, så priserna kan inte hämtas."},
                "plan": {"ok": False, "error": "Utan priser finns ingen plan."}}
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    flat = mode == "quiet"
    hours = []
    for i in range(48 if not flat else 24):
        t = start + timedelta(hours=i)
        if flat:
            total = 0.62 + 0.02 * math.sin(i / 3.0)
        else:
            total = 0.35 + 0.95 * max(0.0, math.sin((t.hour - 5) / 24.0 * 2 * math.pi)) \
                    + (1.15 if t.hour in (7, 8, 17, 18, 19) else 0.0) + 0.05 * (i // 24)
        hours.append({"starts_at": hour_iso(t), "day": t.date().isoformat(),
                      "total": round(total, 3), "level": "NORMAL", "rank": 0, "band": "normal"})
    ordered = sorted(hours, key=lambda h: h["total"])
    for rank, h in enumerate(ordered):
        h["rank"] = rank
        if not flat:
            share = rank / max(1, len(ordered) - 1)
            h["band"] = "billig" if share < 0.3 else ("dyr" if share > 0.75 else "normal")
            h["level"] = {"billig": "CHEAP", "normal": "NORMAL", "dyr": "EXPENSIVE"}[h["band"]]
    now_key = hour_iso(datetime.now())
    now_row = next((h for h in hours if h["starts_at"] == now_key), hours[0])
    totals = [h["total"] for h in hours]
    prices = {
        "ok": True, "currency": "SEK",
        "now": {"total": now_row["total"], "level": now_row["level"],
                "starts_at": now_row["starts_at"], "band": now_row["band"]},
        "hours": hours,
        "stats": {"min": round(min(totals), 3), "max": round(max(totals), 3),
                  "mean": round(sum(totals) / len(totals), 3),
                  "median": round(sorted(totals)[len(totals) // 2], 3),
                  "spread": round(max(totals) - min(totals), 3), "flat": flat},
        "has_tomorrow": not flat,
    }
    if flat:
        return {"prices": prices,
                "plan": {"ok": True, "advisory": True, "register": 40031, "hours": [], "days": [],
                         "summary": "Prisskillnaden i dag är 4 öre. Det är för lite för att "
                                    "vara värt att flytta värmen — planen gör ingenting."}}
    plan_hours = []
    for h in hours[:24]:
        hh = datetime.fromisoformat(h["starts_at"]).hour
        if hh in (2, 3, 4):
            plan_hours.append({"starts_at": h["starts_at"], "offset_delta": 1,
                               "why": "Nattens billigaste timmar — ta värmen här i stället."})
        elif hh in (17, 18, 19):
            plan_hours.append({"starts_at": h["starts_at"], "offset_delta": -1,
                               "why": "Dygnets dyraste timmar. Huset har redan värmen i sig."})
        else:
            plan_hours.append({"starts_at": h["starts_at"], "offset_delta": 0, "why": ""})
    return {"prices": prices,
            "plan": {"ok": True, "advisory": True, "register": 40031, "hours": plan_hours,
                     "days": [{"day": start.date().isoformat(), "net": 0, "spread": 1.94,
                               "pairs": 3, "reason": "Tre par: natten mot kvällen."}],
                     "summary": "Tre timmar upp i natt, tre timmar ner i kväll. Summan är noll."}}


def alarms(mode: str) -> dict:
    if mode == "off":
        return {"ok": False, "error": "Larmbevakningen är inte påslagen i config.yaml."}
    hist = [
        {"code": 163, "text": "Kommunikationsfel med rumsenhet", "severity": "warning",
         "action": "Kontrollera kabeln till rumsenheten.", "known": True,
         "since": int(NOW - 86400 * 6)},
        {"code": 44, "text": "Låg framledningstemperatur", "severity": "info",
         "action": "Ingen åtgärd om det gick över av sig självt.", "known": True,
         "since": int(NOW - 86400 * 19)},
        {"code": 999, "text": "Okänd larmkod 999", "severity": "warning",
         "action": "Slå upp koden på pumpens display.", "known": False,
         "since": int(NOW - 86400 * 40)},
    ]
    if mode == "quiet":
        return {"active": [], "history": hist, "notify": False,
                "last_error": "Pushover svarade 401 vid senaste försöket."}
    return {
        "active": [
            {"code": 175, "text": "Kompressorn blockerad av högt kondensortryck", "severity": "alarm",
             "action": "Kontrollera att framledningen kommer fram: stängda radiatorventiler eller "
                       "en luftad krets är vanligaste orsaken. Pumpen värmer med elpatron så länge.",
             "known": True, "since": int(NOW - 5400)},
            {"code": 163, "text": "Kommunikationsfel med rumsenhet", "severity": "warning",
             "action": "Kontrollera kabeln till rumsenheten. Värmen påverkas inte.",
             "known": True, "since": int(NOW - 3600 * 30)},
        ],
        "history": hist, "notify": True, "last_error": None,
    }


def autotune(mode: str) -> dict:
    if mode == "off":
        return {"ok": False, "error": "Kalibreringen är avstängd i config.yaml."}
    if mode == "quiet":
        return {
            "ok": True, "state": "insufficient",
            "reason_sv": "Nio nätter med underlag, men utetemperaturen har bara rört sig mellan "
                         "−1 och +4 grader. Med så lite spridning går lutningen inte att skilja "
                         "från noll.",
            "samples": 9, "outdoor_span": [-1.2, 4.1], "days": 6,
            "offset_error_c": None, "slope_error_c_per_c": None, "proposal": None,
            "confidence": 0.18,
            "missing_sv": ["Minst 20 nätter med mätvärden (har 9).",
                           "Minst 12 graders spridning i utetemperatur (har 5,3).",
                           "Minst tre nätter under noll grader (har 1)."],
            "notes_sv": ["Kalibreringen tittar bara på nätter: dagtid värms huset också av solen, "
                         "ugnen och folket i det, och inget av det finns i historiken."],
        }
    return {
        "ok": True, "state": "confident",
        "reason_sv": "31 nätter mellan −11 och +7 grader ute. Huset ligger jämnt drygt en halv "
                     "grad för svalt i alla väder, och lutningen ser rätt ut — det är alltså "
                     "nivån som är fel, inte kurvans branthet.",
        "samples": 31, "outdoor_span": [-11.4, 7.2], "days": 34,
        "offset_error_c": -0.6, "slope_error_c_per_c": 0.02,
        "proposal": {"register": 40031, "from": 2, "to": 3,
                     "why_sv": "Ett offsetsteg lyfter hela kurvan lika mycket i alla väder, "
                               "vilket är precis den sortens fel mätningen visar.",
                     "expected_sv": "Ungefär +0,6 °C inne, jämnt över dygnet. Framledningen "
                                    "höjs cirka 2 grader.",
                     "wait_hours": 36},
        "confidence": 0.74,
        "missing_sv": [],
        "notes_sv": ["Gör bara en ändring i taget. Två samtidiga går inte att utvärdera."],
    }


# --------------------------------------------------------------------------
# stubbservern
# --------------------------------------------------------------------------
class Stub(SimpleHTTPRequestHandler):
    mode = "full"

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(WEB), **kw)

    def log_message(self, *a):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):                                     # noqa: N802
        route = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if route.path == "/api/write":
            if "changes" in body:
                return self._json({"changes": [
                    {"address": c["address"], "title": "register %s" % c["address"],
                     "before": c.get("expect"), "after": c["value"]} for c in body["changes"]]})
            return self._json({"address": body.get("address"), "before": body.get("expect"),
                               "after": body.get("value")})
        if route.path == "/api/hotwater":
            return self._json({"minutes": body.get("minutes", 0), "off": body.get("off", False)})
        if route.path == "/api/ventilation":
            return self._json({"mode": body.get("mode", 0), "percent": 55,
                               "normal_percent": 30, "hours": body.get("hours", 3)})
        if route.path == "/api/backup":
            return self._json({"name": "backup-stub.json"})
        return self._json({"error": "not found"}, 404)

    def do_GET(self):                                      # noqa: N802
        route = urlparse(self.path)
        path, q = route.path, parse_qs(route.query)
        if not path.startswith("/api/"):
            return super().do_GET()
        m = self.mode
        on = m not in ("off", "fallback")

        if path == "/api/status":
            return self._json({
                "pump": {"host": "192.168.1.40", "port": 502},
                "register_map": "S735 (stub)",
                "polled_at": int(NOW - 20),
                "poll_error": None,
                "features": {"indoor": on, "weather": on, "spot": on,
                             "alarms": on, "notify": m == "full", "autotune": on},
                # Sammanfattningarna som riktiga /api/status ocksa skickar med.
                # Larmsiffran ar den appen anvander for att slippa hamta hela
                # larmlistan varje varv.
                "indoor": ({"ok": True, "configured": True, "average": 21.1,
                            "at": int(NOW), "sensors": 5, "stale": 1, "error": None}
                           if on else {"ok": False, "configured": False, "average": None,
                                       "sensors": 0, "stale": 0, "at": None, "error": None}),
                "alarm": ({"ok": True, "active": len(alarms(m).get("active", [])),
                           "code": 175 if m == "full" else None,
                           "text": "", "severity": "alarm" if m == "full" else None,
                           "notify": m == "full", "error": None}
                          if on else {"ok": False, "active": 0, "code": None, "text": "",
                                      "severity": None, "notify": False, "error": None}),
                "registers": [
                    {"address": a, "title": t, "unit": u,
                     "value": (175 if m in ("full", "fallback") else 0) if a == 31976 else v,
                     "error": None}
                    for a, t, u, v in REGISTERS],
            })
        if path == "/api/heating":
            return self._json({
                "curve": 0, "offset": 2, "min_supply": 20, "max_supply": 55,
                "room_setpoint": 21, "room_temp": None, "outdoor": -4.2, "supply": 38.4,
                "return": 33.1, "degree_minutes": -142, "additional_heat_kw": 0.0,
                "calculated_supply": 39.0, "priority": "Värme",
                "own_curve": [46, 42, 37, 32, 26, 21, 20],
                "has_room_sensor": False, "uses_own_curve": True,
                "emitters": "mixed", "emitters_name": "golvvärme och radiatorer",
                "wait": "ett dygn",
                "warnings": [
                    "Ingen rumsgivare svarar (BT50). Rumsbörvärdet går att skriva men pumpen har "
                    "inget att reglera mot, så det påverkar troligen ingenting.",
                    "Framledningen ligger nära taket (55 °C). Ytterligare höjningar av kurvan "
                    "gör ingenting förrän taket höjs.",
                ],
                "observations": [
                    "Kurvan står på 0, vilket betyder egen kurva: pumpen följer dina egna punkter "
                    "(−30 °C ute → 46 °C fram, −20 → 42, −10 → 37, 0 → 32, +10 → 26) i stället för "
                    "en av de numrerade kurvorna.",
                    "Huset har både golvvärme och radiatorer på samma krets, vilket alltid är en "
                    "kompromiss: golvet vill ha lågt och jämnt, radiatorerna högt och snabbt.",
                ],
            })
        if path == "/api/advice":
            return self._json({
                "observations": [],
                "warnings": ["Ingen rumsgivare, så förslaget bygger på kurvan och din egen känsla."],
                "suggestions": [{"address": 40031, "title": "Värmeoffset", "current": 2,
                                 "proposed": 3, "unit": "", "group": "warmer",
                                 "why": "Ett steg upp lyfter hela kurvan ungefär en grad inne. "
                                        "Vänta ett dygn innan du bedömer.",
                                 "confirm_required": True}],
                "blocked": "", "wait": "ett dygn",
            })
        if path == "/api/settings":
            return self._json({"groups": build_settings()})
        if path == "/api/fan":
            return self._json({"speeds": {"0": 30, "1": 0, "2": 40, "3": 55, "4": 70}})
        if path == "/api/history":
            addr = int(q.get("address", ["30009"])[0])
            hrs = int(q.get("hours", ["24"])[0])
            return self._json({"address": addr, "hours": hrs, "points": history(addr, hrs)})
        if path == "/api/log":
            return self._json([
                {"ts": int(NOW - 3600 * 5), "address": 40031, "title": "Värmeoffset",
                 "before": 1, "after": 2},
                {"ts": int(NOW - 86400 * 3), "address": 40185, "title": "Värmestopp",
                 "before": 18, "after": 17},
            ])
        if path == "/api/backups":
            return self._json({
                "directory": "backup",
                "backups": [{"name": "2026-09-06T0300.json", "bytes": 41000, "taken_at": NOW - 7200},
                            {"name": "2026-09-05T0300.json", "bytes": 40800, "taken_at": NOW - 93600}],
                "history": {}, "newest": NOW - 7200, "age_hours": 2.0,
                "auto": True, "auto_every_hours": 24,
            })
        if path == "/api/indoor":
            return self._json(indoor(m))
        if path == "/api/weather":
            return self._json(weather(m))
        if path == "/api/spot":
            return self._json(spot(m))
        if path == "/api/alarms":
            return self._json(alarms(m))
        if path == "/api/autotune":
            return self._json(autotune(m))
        return self._json({"error": "not found"}, 404)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start(mode: str, port: int) -> ThreadingHTTPServer:
    handler = type("StubMode", (Stub,), {"mode": mode})
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# --------------------------------------------------------------------------
# drivaren
# --------------------------------------------------------------------------
fel: list[str] = []


def check(ok, msg):
    print(("  ok    " if ok else "  FEL   ") + msg)
    if not ok:
        fel.append(msg)


TABS = [("now", "Just nu"), ("heat", "Värme"), ("water", "Varmvatten"),
        ("air", "Ventilation"), ("hist", "Historik")]


def shoot(page, out: pathlib.Path, name: str):
    out.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(out / (name + ".png")), full_page=True)


def drive(page, mode: str, label: str, out: pathlib.Path, url: str):
    print("\n=== %s / %s ===" % (mode, label))
    page.goto(url, wait_until="networkidle")
    page.wait_for_timeout(2200)

    check(page.locator("nav button").count() == 5, "fem flikar kvar i navigeringen")

    for name, heading in TABS:
        page.locator('nav button[data-view="%s"]' % name).click()
        page.wait_for_timeout(900)
        check(page.locator("#v-" + name).is_visible()
              and page.locator("#viewTitle").inner_text() == heading,
              "fliken %s" % name)
        shoot(page, out, "%s-%s-%s" % (mode, label, name))

    page.locator("#goSettings").click()
    page.wait_for_timeout(2500)
    shoot(page, out, "%s-%s-set" % (mode, label))

    page.locator('nav button[data-view="now"]').click()
    page.wait_for_timeout(700)

    vis = lambda sel: page.locator(sel).is_visible()          # noqa: E731
    if mode == "off":
        for sel in ["#indoorCard", "#weatherCard", "#spotCard", "#tuneCard",
                    "#alarmHistWrap", "#alarmBanner"]:
            check(not vis(sel), "%s renderas inte alls" % sel)
        page.locator("#goSettings").click()
        page.wait_for_timeout(1200)
        check(vis("#featHint"), "tipset om config.yaml finns under Inställningar")
        txt = page.locator("#featHintList").inner_text()
        check("homey_host" in txt and "tibber_token" in txt and "weather_lat" in txt,
              "tipset namnger nycklarna")
        check(page.locator("#featHint .cfg").count() >= 3, "nycklarna visas som kod")
    else:
        check(not vis("#featHint"), "inget config-tips när allt som går att slå på är påslaget"
              if mode == "full" else "tipset syns bara för det som saknas")
        check(vis("#indoorCard"), "inomhuskortet syns")
        zones = page.locator("#indoorBody .zone").count()
        check(zones >= 1, "inomhustemperatur per våning (%d)" % zones)
        page.locator("#indoorBody details summary").click()
        page.wait_for_timeout(350)
        dim = page.locator("#indoorBody tr.dim").count()
        check(page.locator("#indoorBody tr").count() >= 4 and dim >= 1,
              "givarlistan visar även inaktuella givare (%d gråa)" % dim)
        shoot(page, out, "%s-%s-now-sensorer" % (mode, label))

        check(vis("#weatherCard"), "väderkortet syns")
        check(page.locator("#wxChart path.ln").count() == 1, "timprognosen ritas som kurva")
        check(page.locator("#weatherBody .day").count() >= 3, "dygnsprognos i rutor")
        low = page.locator("#weatherBody .metric .n").first.inner_text()
        check(low not in ("", "–"), "kommande lägsta temperatur visas: %r" % low)

        if mode == "full":
            check(vis("#alarmBanner"), "aktivt larm högst upp på Just nu")
            n = page.locator("#alarmBanner .alarm").count()
            sev = page.locator("#alarmBanner .alarm.sev-warning").count()
            check(n == 2 and sev == 1, "larmen färgade efter allvar (%d st, %d varning)" % (n, sev))
            check("Kontrollera" in page.locator("#alarmBanner").inner_text(),
                  "föreslagen åtgärd står i larmet")
        else:
            check(not vis("#alarmBanner"), "ingen larmruta när inget larmar")
        check(vis("#alarmHistWrap"), "larmhistoriken finns bakom en knapp")
        check(page.locator("#alarmBanner button").count() == 0,
              "ingen återställ-larm-knapp finns")

        check(page.locator("#priceTile").count() == 1, "elpriskakel på Just nu")

        page.locator('nav button[data-view="heat"]').click()
        page.wait_for_timeout(1600)
        check(vis("#spotCard"), "elpriskortet på Värme")
        bars = page.locator("#spotBody .bars rect").count()
        check(bars >= 24, "timpriserna ritas som staplar (%d rektanglar)" % bars)
        txt = page.locator("#spotBody").inner_text()
        if mode == "full":
            check("gör ingenting av det här själv" in txt,
                  "planen säger att appen inte agerar själv")
            rows = page.locator("#spotBody .planrow").count()
            check(rows >= 2, "planen listar timmar (%d rader)" % rows)
            check("±0" in txt and "Summan" in txt,
                  "planen förklarar att dygnet summerar till noll")
            # spot_max_offset far vara 2. Da ska chippet sta +2, inte +1.
            two = page.evaluate("""() => {
              renderSpot({prices: {ok: true, currency: 'SEK', now: {total: 1, band: 'dyr'},
                hours: [], stats: {}},
                plan: {ok: true, advisory: true, hours: [
                  {starts_at: '2026-01-01T02:00:00+01:00', offset_delta: 2, why: 'billigt'},
                  {starts_at: '2026-01-01T18:00:00+01:00', offset_delta: -2, why: 'dyrt'}],
                  days: [{day: '2026-01-01', net: 0}]}});
              return [...document.querySelectorAll('#spotBody .delta')].map(e => e.textContent);
            }""")
            check(two == ["+2", "\u22122"], "planen visar hela steget, inte alltid ±1 (%s)" % two)
            page.reload(wait_until="networkidle")
            page.wait_for_timeout(1800)
            check(page.locator("#spotBody .bars rect.nowcol").count() == 1, "nu-timmen markerad")
            check(page.locator("#spotBody .bars rect.split").count() == 1, "imorgon avdelad")
        else:
            check(rows_flat(page), "platt dygn förklaras i stället för att visa en plan")
            check("gör ingenting av det här själv" not in txt,
                  "ingen varning om att appen inte agerar när det inte finns någon plan")

        check(vis("#tuneCard"), "kalibreringskortet på Värme")
        ttxt = page.locator("#tuneBody").inner_text()
        if mode == "full":
            check(page.locator("#tuneGo").count() == 1, "förslaget har en knapp")
            check("40031" in ttxt and "36" in ttxt, "förslaget visar register och väntetid")
        else:
            check(page.locator("#tuneGo").count() == 0, "inget förslag när underlaget inte räcker")
            check(page.locator("#tuneBody ul.miss li").count() >= 2, "det som saknas listas")
        shoot(page, out, "%s-%s-heat-nedre" % (mode, label))

    over = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
    check(over <= 1, "ingen horisontell scroll (%d px över)" % over)


def rows_flat(page):
    return "för jämnt" in page.locator("#spotBody").inner_text() \
        or "för lite" in page.locator("#spotBody").inner_text()


def register_fallback(page, url):
    """Utan larmbevakning finns bara larmnumret i register 31976. Da ska den
    gamla, samre rutan visas anda -- ett larm ar viktigare an snyggheten."""
    print("\n=== larm utan larmbevakning ===")
    page.goto(url, wait_until="networkidle")
    page.wait_for_timeout(2200)
    check(page.locator("#alarmBanner").is_visible(), "registerlarmet visas ändå")
    txt = page.locator("#alarmBanner").inner_text()
    check("175" in txt, "larmnumret står i rutan: %r" % txt[:60])
    check(not page.locator("#alarmHistWrap").is_visible(), "ingen larmhistorik utan bevakning")
    check(not page.locator("#indoorCard").is_visible(), "inget annat nytt renderas")


def guarded_write(page, url):
    """Autotune-forslaget ska ga genom appens vanliga bekraftelse."""
    print("\n=== skyddad skrivning ===")
    page.goto(url + "#heat", wait_until="networkidle")
    page.wait_for_timeout(2500)
    seen = {}
    page.on("dialog", lambda d: (seen.update(text=d.message), d.accept()))
    page.locator("#tuneGo").click()
    page.wait_for_timeout(1500)
    txt = seen.get("text", "")
    check("40031" in txt and "2 → 3" in txt, "bekräftelserutan visar register och före → efter")
    check("36 timmar" in txt, "bekräftelserutan säger hur länge man ska vänta")
    check(page.locator("#toast .toast").count() >= 1, "skrivningen kvitteras med en toast")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve", action="store_true", help="bara servern, ingen webblasare")
    ap.add_argument("--mode", default="full", choices=["full", "quiet", "off", "fallback"])
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--out", default=str(ROOT / "_shots"))
    args = ap.parse_args()

    if args.serve:
        port = args.port or 8377
        start(args.mode, port)
        print("stub pa http://127.0.0.1:%d/ i lage %s" % (port, args.mode))
        while True:
            time.sleep(3600)

    from playwright.sync_api import sync_playwright
    out = pathlib.Path(args.out)
    ports = {}
    for mode in ("full", "quiet", "off", "fallback"):
        ports[mode] = free_port()
        start(mode, ports[mode])
    time.sleep(0.4)

    with sync_playwright() as pw:
        b = pw.chromium.launch()
        for mode in ("full", "quiet", "off"):
            url = "http://127.0.0.1:%d/" % ports[mode]
            for label, w, h in [("telefon", 390, 844), ("dator", 1280, 900)]:
                page = b.new_page(viewport={"width": w, "height": h})
                errs = []
                page.on("console", lambda msg: errs.append(msg.text) if msg.type == "error" else None)
                page.on("pageerror", lambda e: errs.append("pageerror: %s" % e))
                drive(page, mode, label, out, url)
                real = [e for e in errs if "favicon" not in e.lower()]
                check(not real, "inga konsolfel i %s/%s%s"
                      % (mode, label, (": " + "; ".join(real[:3])) if real else ""))
                page.close()
        page = b.new_page(viewport={"width": 390, "height": 844})
        register_fallback(page, "http://127.0.0.1:%d/" % ports["fallback"])
        page.close()

        page = b.new_page(viewport={"width": 390, "height": 844})
        guarded_write(page, "http://127.0.0.1:%d/" % ports["full"])
        page.close()

        # morkt lage, med allt pasl aget
        page = b.new_page(color_scheme="dark", viewport={"width": 390, "height": 844})
        page.goto("http://127.0.0.1:%d/" % ports["full"], wait_until="networkidle")
        page.wait_for_timeout(2200)
        shoot(page, out, "full-mork-now")
        page.locator('nav button[data-view="heat"]').click()
        page.wait_for_timeout(1600)
        shoot(page, out, "full-mork-heat")
        bg = page.evaluate("() => getComputedStyle(document.body).backgroundColor")
        print("\n=== morkt lage ===")
        check(bg not in ("rgba(0, 0, 0, 0)", "rgb(255, 255, 255)"), "mork bakgrund: %s" % bg)
        page.close()
        b.close()

    print("\nskarmbilder i %s" % out)
    print("\n" + ("ALLT OK" if not fel else "%d FEL:\n  - %s" % (len(fel), "\n  - ".join(fel))))
    return 1 if fel else 0


if __name__ == "__main__":
    sys.exit(main())
