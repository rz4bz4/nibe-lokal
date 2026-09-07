"""Stubbad UI-rok: kor hela appen mot en pahittad men *aktig* S735.

Den har filen ar inte bara en skarmbildstagare. Den ar den enda platsen dar
appens JavaScript motter samma data som en riktig varmepump skickar, och den
har tidigare ljugit om precis de detaljer som gjorde appen trasig i verkliga
livet:

  * Registervarden hittades pa for hand. Pa en riktig S735 kommer 32196 som
    stangen "No alarm" och inte som 0, 31029 som "Heat" och 40057 som "Medium"
    -- for att registerkartan mappar dem, och kartan ar engelsk. Stubben
    svarade 0, "Heating" och 1, och lat darfor bade det falska larmet (varje
    kall start) och de fyra "-" i installningslistan passera.
  * Enheterna var redan oversatta. Gradminuterna heter "DM" i kartan, inte
    "GM", och intervallet for periodisk varmvattenhojning "days", inte "dygn".
  * De svenska meningarna var handskrivna och snyggare an de riktiga.

Darfor: registerkartan kommer nu ur `nibe`-paketet (samma Model.S735.
get_coil_data() som appen kor mot i skarpt lage), varje varde gar genom
registrets egen encode/decode, och /api/heating, /api/advice, /api/settings
och /api/spot byggs av `nibelokal.advisor`, `nibelokal.settings` och
`nibelokal.spot` -- alltsa av samma kod som svarar pa riktigt. Det som star pa
skarmen ar det servern faktiskt skriver, med sina egna minustecken och
decimalkomman.

Lagen:

    full     -- alla integrationer konfigurerade, larm aktivt (31976 = 175,
                32196 = "Alarm"), extra varmvatten igang, plan for dygnet
    quiet    -- allt konfigurerat men urlakat: inga larm, platt elpris, for
                lite underlag for kalibrering, givare som slutat svara
    off      -- inget *valfritt* konfigurerat. Larmbevakningen ar PASLAGEN:
                den behover inga nycklar, server.py bygger alltid Watcher och
                features.alarms ar sann sa fort den gick att bygga. Det ar
                notiserna som saknas. Ingenting annat nytt ska synas.
    fallback -- larmbevakningen kunde INTE byggas. Det ar det enda satt
                features.alarms blir falsk pa en riktig server: Watcher
                skriver sina tabeller i historikdatabasen, och pa en skrivskyd-
                dad disk kastar konstruktorn. /api/alarms svarar da med
                serverns _not_started-kropp, och larmnumret i register 31976
                ar allt appen har.
    alarmdown-- pumpen svarar, larmbevakningen gor det inte: /api/alarms drojer
                och svarar sedan 502, med 31976 = 0 och 32196 = "No alarm".
                Det ar bade den kalla starten (svaret har inte kommit an) och
                det permanenta felet (svaret kommer aldrig) -- de tva
                tillstand dar appen tidigare malade ett rott larm ur ingenting.
    curve    -- agarens riktiga kurva: P1-P3 alla 45, P7 (15) under min
                framledning (26), punktforskjutning -2/+3, offset -1.
    unreach  -- allt konfigurerat men ingen integration svarar.
    slow     -- servern svarar, men langsamt.
    heat502  -- /api/heating svarar 502.

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

from nibelokal import advisor                             # noqa: E402
from nibelokal import settings as nsettings               # noqa: E402
from nibelokal import spot as nspot                       # noqa: E402
from nibelokal.homey import Homey                         # noqa: E402
from nibelokal.pump import DASHBOARD, FAN_SPEED_REGISTER  # noqa: E402
from nibelokal.registry import Registry                   # noqa: E402
from nibelokal.weather import Weather                     # noqa: E402

NOW = time.time()

#: Samma karta appen kor mot i skarpt lage. Inte en handskriven kopia av den:
#: titlar, enheter, storlekar, skalfaktorer och mappningar kommer harifran, och
#: det ar mappningarna som gor att ett halvt dussin register svarar med ord.
REGISTRY = Registry.load("S735")


# --------------------------------------------------------------------------
# pumpen
# --------------------------------------------------------------------------
#: Vad pumpen *haller*, i samma form som en skrivning tar emot: tal, och for
#: ett mappat register nyckeln. Vad appen far se avgor registret sjalv, genom
#: att koda och avkoda vardet precis som en riktig lasning gor -- sa 31029: 30
#: kommer fram som "Heat" och 40057: 1 som "Medium", vare sig vi vill det eller
#: inte. Det ar hela poangen med den har filen.
PUMP: dict[int, float] = {
    30002: -4.2,    # utetemperatur BT1
    30006: 38.4,    # framledning BT2
    30008: 33.1,    # retur BT3
    30009: 52.6,    # varmvatten topp BT7
    30010: 47.9,    # varmvatten laddning BT6
    30020: 21.4,    # franluft BT20
    # 30117 (rumsgivare BT50) svarar inte: huset har ingen. Registret utelamnas
    # helt, vilket ar vad pumpen gor -- inte "0 grader inne".
    31018: 39.0,    # berknad framledning
    31026: 112,     # drifttid tillskott
    31028: 0.0,     # tillskott just nu, kW
    31029: 30,      # prioritet -> "Heat"
    31047: 43,      # kompressorfrekvens
    31079: 0,       # extra varmvatten, status
    31088: 18422,   # drifttid kompressor
    31092: 2611,    # drifttid varmvatten
    31535: 9741,    # kompressorstarter
    31975: 30,      # flaktvarvtal
    31976: 0,       # larmnummer
    32134: 30,      # franluftsflakt
    32196: 0,       # larm av klass 1 -> "No alarm"
    40012: -142,    # gradminuter, enhet DM i kartan
    40020: -1,
    40027: 0,       # kurva 0 = egen kurva
    40031: 2,       # varmeoffset
    40035: 20, 40039: 55, 40185: 17, 40167: 1, 40094: 60,
    40040: 20, 40041: 21, 40042: 26, 40043: 32,
    40044: 37, 40045: 42, 40046: 46,
    40047: 0, 40048: 0,
    40057: 1,       # varmvattenkomfort -> "Medium"
    40062: 55, 40063: 52, 40064: 47, 40065: 42,
    40067: 14,      # periodisk hojning, enhet "days" i kartan
    40077: 5,
    40103: 6.0, 40181: 1, 40186: 15, 40188: 6, 40189: 4,
    40105: 0,       # ventilationslage: normal
    # Flaktprocent per lage. Lagena ar INTE ordnade lagt till hogt: lage 1 ar
    # 0 %, vilket ar precis den vag appen medvetet lagger en varning framfor.
    40110: 30, 40109: 0, 40108: 40, 40107: 55, 40106: 70,
    40203: 0, 40207: 21, 40211: 2,
    40226: 0,       # extra varmvatten, minuter kvar
    40228: 0, 40229: 24, 40182: 1,
}

#: Uppmatt pa agarens egen S735. P1-P3 ar alla 45: kurvan ar platt fran -30 till
#: -10 fast taket ligger pa 58, och P7 (15) ligger under min framledning (26).
OWNER = {
    40027: 0, 40031: -1,
    40046: 45, 40045: 45, 40044: 45, 40043: 37, 40042: 33, 40041: 23, 40040: 15,
    40047: -2, 40048: 3,
    40035: 26, 40039: 58, 40185: 23,
}

#: Vilket klimatsystem huset har. Styr vantetiden advisor raknar med, och den
#: ska inte vara samma i alla lagen -- 36 timmars kalibreringsforslag mot ett
#: dygns kurvandring ar hela poangen med att appen valjer den langsta.
EMITTERS = {"quiet": "mixed"}


def pump_values(mode: str) -> dict:
    v = dict(PUMP)
    if mode == "curve":
        v.update(OWNER)
    if mode in ("full", "fallback"):
        v[31976] = 175          # larmnummer
        v[32196] = 1            # -> "Alarm"
    if mode == "full":
        v[40226] = 27           # extra varmvatten pagar: ett kakel till
    if mode == "quiet":
        v[40057] = 4            # -> "Smart Control"
    return v


class FakePump:
    """Samma yta mot advisor/settings som nibelokal.pump.Pump har.

    read_many kodar och avkodar varje varde genom registret, sa mappade
    register kommer ut som de strangar kartan sager och skalfaktorerna
    tillampas -- det ar skillnaden mellan att testa appen och att testa sin
    egen fantasi om pumpen.
    """

    host = "192.168.1.40"
    port = 502

    def __init__(self, mode: str):
        self.registry = REGISTRY
        self.values = pump_values(mode)

    def _read(self, address: int):
        reg = self.registry.get(address)
        return reg.decode(reg.encode(self.values[address]))

    def read(self, address: int):
        return self._read(address)

    def read_many(self, addresses) -> dict:
        out = {}
        for a in addresses:
            if self.registry.get(a) is None or a not in self.values:
                continue
            out[a] = {"value": self._read(a)}
        return out

    def missing(self) -> list:
        return []

    def fan_speeds(self) -> dict:
        data = self.read_many(list(FAN_SPEED_REGISTER.values()))
        return {mode: data.get(addr, {}).get("value")
                for mode, addr in FAN_SPEED_REGISTER.items()}


class FakeStore:
    """Bara det advisor fragar efter: hur langt tillbaka historiken racker."""

    def span(self):
        return (NOW - 30 * 86400, NOW)


def status_registers(pump: FakePump) -> list:
    """Samma rader som server.py bygger i /api/status: titel och enhet ur
    kartan, alltsa engelska bada tva, och vardet som pumpen svarade."""
    data = pump.read_many(DASHBOARD)
    out = []
    for address, row in sorted(data.items()):
        reg = pump.registry.get(address)
        out.append({"address": address,
                    "title": reg.title if reg else str(address),
                    "unit": reg.unit if reg else "",
                    "value": row.get("value"),
                    "error": row.get("error")})
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


def hour_iso(dt: datetime) -> str:
    return dt.replace(minute=0, second=0, microsecond=0).astimezone().isoformat()


# --------------------------------------------------------------------------
# integrationer
# --------------------------------------------------------------------------
UNREACH = {
    "indoor": "Homey svarade inte inom 5 sekunder (192.168.1.20).",
    "weather": "SMHI svarade 503. Ingen prognos hamtad an.",
    "spot": "Tibber svarade inte: namnuppslagningen misslyckades.",
    "alarms": "Larmbevakningen kunde inte lasa larmregistret.",
    "autotune": "Kalibreringen kunde inte lasa historiken.",
}

#: Vad servern svarar nar en provider inte gick att bygga. Ordagrant serverns
#: egen _not_started -- det ar den meningen appen ska rendera.
WATCHER_WHY = ("attempt to write a readonly database: historiken ligger pa en "
               "disk som inte gar att skriva till")


def not_started(feature: str, why: str) -> dict:
    return {"ok": False,
            "error": "%s kunde inte startas: %s. Kontrollera inställningarna i "
                     "config.yaml." % (feature, why)}


def indoor(mode: str) -> dict:
    if mode == "unreach":
        return {"ok": False, "error": UNREACH["indoor"]}
    if mode in ("off", "fallback"):
        # Homey-objektet byggs alltid; det ar det som svarar nar inget star i
        # config.yaml, och meningen kommer darfor fran homey.py sjalv.
        return Homey.from_config({}).snapshot()
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
    if mode == "unreach":
        return {"ok": False, "error": UNREACH["weather"]}
    if mode in ("off", "fallback"):
        return Weather({}).snapshot()
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


#: Tibber-formade prisrader, som de kommer ur GraphQL-svaret. De gar sedan
#: genom spot.Tibber._build och spot.Plan, sa banden, statistiken och planens
#: svenska mening ar serverns egna och inte handskrivna har.
def _tibber_rows(flat: bool) -> tuple[list, list]:
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today, tomorrow = [], []
    span = 24 if flat else 48
    for i in range(span):
        t = start + timedelta(hours=i)
        if flat:
            total = 0.62 + 0.02 * math.sin(i / 3.0)
        else:
            total = (0.35
                     + 0.95 * max(0.0, math.sin((t.hour - 5) / 24.0 * 2 * math.pi))
                     + (1.15 if t.hour in (7, 8, 17, 18, 19) else 0.0)
                     + 0.05 * (i // 24))
        row = {"startsAt": hour_iso(t), "total": round(total, 3), "level": "NORMAL"}
        (today if i < 24 else tomorrow).append(row)
    return today, tomorrow


def spot(mode: str) -> dict:
    tibber = nspot.Tibber({"tibber_token": "stub-token"})
    plan = nspot.Plan({})
    if mode == "unreach":
        prices = {"ok": False, "configured": True, "at": NOW, "error": UNREACH["spot"]}
        return {"ok": False, "prices": prices, "plan": plan.build(prices),
                "error": UNREACH["spot"]}
    if mode in ("off", "fallback"):
        prices = nspot.Tibber({}).snapshot()
        return {"ok": False, "prices": prices, "plan": plan.build(prices),
                "error": prices.get("error")}
    today, tomorrow = _tibber_rows(mode == "quiet")
    now_key = hour_iso(datetime.now())
    current = next((r for r in today if r["startsAt"] == now_key), today[0])
    info = {"today": today, "tomorrow": tomorrow,
            "current": dict(current, currency="SEK")}
    prices = tibber._build(info, NOW)
    return {"ok": True, "prices": prices, "plan": plan.build(prices, NOW)}


def alarms(mode: str) -> dict:
    if mode == "unreach":
        return {"ok": False, "error": UNREACH["alarms"]}
    if mode == "fallback":
        # Det enda satt features.alarms blir falsk pa en riktig server.
        return not_started("Larmbevakningen", WATCHER_WHY)
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
    if mode == "off":
        # Larmbevakningen behover inga nycklar och ar darfor pa: registret
        # pollas anda. En nyinstallation har bara ingen historik an, och inga
        # notiser eftersom Pushover inte ar ifyllt.
        return {"ok": True, "active": [], "history": [], "notify": False,
                "notify_error": None, "last_error": None}
    if mode == "quiet":
        # Nycklarna finns, men Pushover avvisade dem: "notiser på" och "notiserna
        # når inte fram" är båda sanna samtidigt, och appen ska säga båda.
        return {"ok": True, "active": [], "history": hist, "notify": True,
                "notify_error": "Pushover avvisade token (401). Kontrollera "
                                "pushover_token och pushover_user.",
                "last_error": None}
    if mode != "full":
        return {"ok": True, "active": [], "history": hist, "notify": False,
                "notify_error": None, "last_error": None}
    return {
        "ok": True,
        "active": [
            {"code": 175, "text": "Kompressorn blockerad av högt kondensortryck", "severity": "alarm",
             "action": "Kontrollera att framledningen kommer fram: stängda radiatorventiler eller "
                       "en luftad krets är vanligaste orsaken. Pumpen värmer med elpatron så länge.",
             "known": True, "since": int(NOW - 5400)},
            {"code": 163, "text": "Kommunikationsfel med rumsenhet", "severity": "warning",
             "action": "Kontrollera kabeln till rumsenheten. Värmen påverkas inte.",
             "known": True, "since": int(NOW - 3600 * 30)},
        ],
        "history": hist, "notify": True, "notify_error": None, "last_error": None,
    }


def autotune(mode: str) -> dict:
    if mode == "unreach":
        return {"ok": False, "error": UNREACH["autotune"]}
    if mode in ("off", "fallback"):
        return {"ok": False, "error": "Kalibreringen behöver en inomhustemperatur att "
                                      "jämföra kurvan mot. Sätt homey_host i config.yaml."}
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
    #: Sekunder att sova innan varje svar. "slow" satter den; da ska appen visa
    #: skelett och inte krascha, i stallet for att blinka fram tomma kort.
    delay = 0.0
    #: Extra fordrojning bara pa /api/alarms, och sedan ett fel. Det ar den
    #: kalla starten: pumpen svarar, larmbevakningen har inte svarat an.
    alarm_delay = 0.0
    alarm_status = 200

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(WEB), **kw)

    def log_message(self, *a):
        pass

    def _json(self, obj, status=200):
        if self.delay:
            time.sleep(self.delay)
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:                            # fliken stangdes
            pass

    def do_POST(self):                                     # noqa: N802
        route = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        pump = FakePump(self.mode)
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
            speeds = pump.fan_speeds()
            mode = int(body.get("mode", 0) or 0)
            return self._json({"mode": mode, "percent": speeds.get(mode),
                               "normal_percent": speeds.get(0),
                               "hours": body.get("hours", 3)})
        if route.path == "/api/alarms/test":
            if self.mode == "quiet":
                return self._json({"ok": False, "sent": False, "notify": True,
                                   "error": "Pushover avvisade token (401)."})
            return self._json({"ok": True, "sent": True, "notify": True})
        if route.path == "/api/backup":
            return self._json({"name": "backup-stub.json"})
        return self._json({"error": "not found"}, 404)

    def do_GET(self):                                      # noqa: N802
        route = urlparse(self.path)
        path, q = route.path, parse_qs(route.query)
        if not path.startswith("/api/"):
            return super().do_GET()
        m = self.mode
        pump = FakePump(m)
        store = FakeStore()
        emitters = EMITTERS.get(m, "radiators")
        # "off" och "fallback" ar bada okonfigurerade -- men larmbevakningen
        # behover inget att konfigurera, sa den star pa i alla lagen utom det
        # dar dess konstruktor kastade.
        on = m not in ("off", "fallback")
        alarms_on = m != "fallback"

        if path == "/api/status":
            return self._json({
                "pump": {"host": pump.host, "port": pump.port},
                "register_map": REGISTRY.source,
                # Pollern gar; svaret ska darfor vara farskt varje gang och
                # inte aldras genom testkorningen -- "4 min sedan" i rubriken
                # ar en egenskap hos stubben, inte hos appen.
                "polled_at": int(time.time() - 20),
                "poll_error": None,
                "store_error": None,
                "missing_registers": [],
                "features": {"indoor": on, "weather": on, "spot": on,
                             "alarms": alarms_on,
                             "notify": m == "full" or m == "quiet",
                             "autotune": on},
                "indoor": ({"ok": True, "configured": True, "average": 21.1,
                            "at": int(NOW), "sensors": 5, "stale": 1, "error": None}
                           if on else {"ok": False, "configured": False, "average": None,
                                       "sensors": 0, "stale": 0, "at": None, "error": None}),
                "alarm": ({"ok": True, "active": len(alarms(m).get("active", [])),
                           "code": 175 if m == "full" else None,
                           "text": "", "severity": "alarm" if m == "full" else None,
                           "notify": m == "full", "error": None}
                          if alarms_on else {"ok": False, "active": 0, "code": None, "text": "",
                                             "severity": None, "notify": False,
                                             "error": WATCHER_WHY}),
                "registers": status_registers(pump),
            })
        if path == "/api/heating":
            if m == "heat502":
                return self._json({"error": "Pumpen svarar inte pa 192.168.1.40:502 "
                                            "(anslutningen nekades)."}, 502)
            return self._json(advisor.diagnose(pump, store, emitters))
        if path == "/api/advice":
            return self._json(advisor.advise(
                pump,
                q.get("feeling", ["warmer"])[0],
                q.get("when", ["always"])[0],
                store,
                emitters,
            ).as_dict())
        if path == "/api/settings":
            values = pump.read_many(nsettings.ADDRESSES)
            return self._json({"groups": nsettings.build(pump, values)})
        if path == "/api/fan":
            return self._json({"speeds": pump.fan_speeds()})
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
            # HTTP 200 aven nar Homey inte svarar. Servern gor likadant: en
            # integration som inte gar att na ar inte ett serverfel, och en
            # 502 har lade dessutom ett rott "Failed to load resource" i
            # webblasarens konsol -- ett fel appen inte hade.
            return self._json(indoor(m))
        if path == "/api/weather":
            return self._json(weather(m))
        if path == "/api/spot":
            return self._json(spot(m))
        if path == "/api/alarms":
            if self.alarm_delay:
                time.sleep(self.alarm_delay)
            if self.alarm_status != 200:
                return self._json({"error": "Larmbevakningen svarar inte."},
                                  self.alarm_status)
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


#: mode -> (delay pa allt, extra delay pa /api/alarms, status pa /api/alarms)
TIMING = {
    "slow": (1.1, 0.0, 200),
    "alarmdown": (0.0, 2.5, 502),
}


def start(mode: str, port: int) -> ThreadingHTTPServer:
    delay, alarm_delay, alarm_status = TIMING.get(mode, (0.0, 0.0, 200))
    handler = type("StubMode", (Stub,), {"mode": mode, "delay": delay,
                                         "alarm_delay": alarm_delay,
                                         "alarm_status": alarm_status})
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


TABS = [("now", "Just nu"), ("heat", "Värme"), ("price", "Elpris"),
        ("water", "Vatten & luft"), ("hist", "Historik")]


def shoot(page, out: pathlib.Path, name: str):
    out.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(out / (name + ".png")), full_page=True)


def visible_tabs(page):
    return [b for b in page.locator("nav button").all() if b.is_visible()]


def all_values(page) -> dict:
    """Vardekolumnen i "Alla avlasta varden", per registernummer.

    Bara vardet: radens undertext ar registrets egen titel ur kartan, och den
    ar engelsk av samma skal som registernumret ar ett nummer -- det ar
    registrets identitet, inte en mening till hushallet.
    """
    return page.evaluate("""() => Object.fromEntries(
      [...document.querySelectorAll('#all tr')].map(tr => [
        (tr.children[0].querySelector('.addr') || {textContent: ''})
          .textContent.split('·')[0].trim(),
        tr.children[1].textContent.trim()]))""")


def tile_map(page) -> dict:
    # Mjuka bindestreck bort: kakelrubriken bar ett i sammansattningsfogen sa
    # att "Varmvattenkomfort" bryts som en svensk lasare skulle bryta det, och
    # det tecknet ar inte en del av ordet.
    return page.evaluate("""() => Object.fromEntries(
      [...document.querySelectorAll('#tiles .tile')].map(t => [
        t.querySelector('.k').textContent.replace(/\u00AD/g, '').trim(),
        t.querySelector('.v').textContent.trim()]))""")


def setrow_value(page, address) -> str | None:
    return page.evaluate("""a => {
      const b = document.querySelector('[data-edit="' + a + '"]');
      return b ? b.closest('.setrow').querySelector('.val').textContent.trim() : null;
    }""", str(address))


#: Ord som bara kan komma fran registerkartan, alltsa fran pumpen. Star nagot
#: av dem i en vardekolumn har appen slutat oversatta.
ENGLISH_VALUES = ["No alarm", "Alarm", "Heat", "Hot Water", "Cooling",
                  "Small", "Medium", "Large", "DM", "days"]


def pump_words(page, mode: str):
    """Defekt 1 och 7: pumpens egna ord, som de nar skarmen."""
    vals = all_values(page)
    leaks = {a: v for a, v in vals.items()
             if any(w in v for w in ENGLISH_VALUES)}
    check(not leaks, "inga engelska registervarden i tabellen (%s)" % leaks)

    check(vals.get("31029") == "värme",
          "prioriteringen star pa svenska: %r" % vals.get("31029"))
    check(vals.get("32196") == ("larm" if mode == "full" else "inget larm"),
          "klass 1-flaggan star pa svenska: %r" % vals.get("32196"))
    check("»" not in (vals.get("31029") or "") and "»" not in (vals.get("32196") or ""),
          "ett ord appen kan oversatta citeras inte")
    hw = vals.get("40057") or ""
    check(hw in ("Medel", "Smart Control"),
          "varmvattenkomforten visas som ord, inte som '–' eller nyckel: %r" % hw)
    gm = vals.get("40012") or ""
    check("GM" in gm and "DM" not in gm, "gradminuterna far svensk enhet: %r" % gm)
    check((vals.get("40067") or "").endswith("dygn"),
          "intervallet raknas i dygn, inte 'days': %r" % vals.get("40067"))

    tiles = tile_map(page)
    check(tiles.get("Varmvattenkomfort") in ("Medel", "Smart Control"),
          "kaklet sager samma sak: %r" % tiles.get("Varmvattenkomfort"))

    # Regeln sjalv, provad direkt: ett mappat ord ar aldrig sant i sig.
    rules = page.evaluate("""() => ({
      noAlarm: flagOn('No alarm'), alarm: flagOn('Alarm'), zero: flagOn(0),
      one: flagOn(1), rubbish: flagOn('Vilostäge'),
      num: regNum('No alarm'), unknown: mappedHtml(31029, 'Silent mode'),
      dm: unitSv('DM'), days: unitSv('days'), c: unitSv('°C')})""")
    check(rules["noAlarm"] is False and rules["alarm"] is True
          and rules["zero"] is False and rules["one"] is True,
          "flagOn tolkar bade talet och det mappade ordet (%s)" % rules)
    check(rules["rubbish"] is False, "ett okant ord ar inte ett larm")
    check(rules["num"] is None, "regNum gor inte tal av ett ord")
    check("asis" in rules["unknown"], "ett ord utan svensk oversattning markeras som citat")
    check(rules["dm"] == "GM" and rules["days"] == "dygn" and rules["c"] == "°C",
          "enhetstabellen oversatter bara det den kanner igen (%s)" % rules)


def tiles_twelve(page, mode: str):
    """Defekt 6: tolv rutor, och urvalet ar bestamt -- inte avhugget."""
    tiles = tile_map(page)
    check(len(tiles) == 12, "tolv kakel pa Just nu (%d)" % len(tiles))
    if mode == "full":
        check("Extra varmvatten" in tiles,
              "en pagaende boost ligger forst (%s)" % list(tiles)[:2])
        check("Drifttid" not in tiles,
              "och tar da platsen fran den sista rutan, avsiktligt")
    else:
        check("Drifttid" in tiles, "Drifttid far plats nar ingen boost pagar")


def settings_headings(page):
    """Defekt 3: varje grupp har sin rubrik."""
    heads = page.locator("#settings .setgroup h3").all_inner_texts()
    groups = page.locator("#settings .setgroup").count()
    check(groups == len(heads) and groups >= 5,
          "varje installningsgrupp har en rubrik (%d grupper, %d rubriker)"
          % (groups, len(heads)))
    for want in ("Gränser och säsong", "Tillskott", "Rumsgivare", "Övrigt"):
        check(any(want in h for h in heads), "rubriken %r finns (%s)" % (want, heads))
    check(page.locator("#setWater .setgroup h3").count() == 0,
          "varmvattnet star pa sin egen flik och behover ingen rubrik dar")


def drive(page, mode: str, label: str, out: pathlib.Path, url: str):
    print("\n=== %s / %s ===" % (mode, label))
    page.goto(url, wait_until="networkidle")
    page.wait_for_timeout(2400)

    want = 4 if mode in ("off", "fallback") else 5
    check(len(visible_tabs(page)) == want,
          "%d flikar i navigeringen (%d)" % (want, len(visible_tabs(page))))

    for name, heading in TABS:
        tab = page.locator('nav button[data-view="%s"]' % name)
        if not tab.is_visible():
            check(name == "price" and mode in ("off", "fallback"),
                  "fliken %s ar dold" % name)
            continue
        tab.click()
        page.wait_for_timeout(900)
        check(page.locator("#v-" + name).is_visible()
              and page.locator("#viewTitle").inner_text() == heading,
              "fliken %s heter samma sak som vyn" % name)
        shoot(page, out, "%s-%s-%s" % (mode, label, name))

    page.locator('nav button[data-view="now"]').click()
    page.wait_for_timeout(700)
    pump_words(page, mode)
    tiles_twelve(page, mode)

    page.locator("#goSettings").click()
    page.wait_for_timeout(2500)
    check(page.locator("#viewTitle").inner_text() == "Inställningar", "instaellningsvyn")
    settings_headings(page)
    body = page.locator("body").inner_text()
    check("timmer" not in body, "ingen 'timmer' pa sidan")
    check("var 24:e timme" in body, "backupintervallet star pa svenska")
    shoot(page, out, "%s-%s-set" % (mode, label))

    # -- avancerat: kurvan som bild, bakom en hopfalld lucka ---------------
    page.locator('nav button[data-view="heat"]').click()
    page.wait_for_timeout(1800)
    check(page.locator("#advCard").is_visible(), "avancerat-kortet finns pa Varme")
    check(not page.locator("#advBox").evaluate("e => e.open"),
          "avancerat ar hopfallt fran start")
    sumtxt = page.locator("#advSum").inner_text().strip()
    check(sumtxt.startswith("Öppna"), "luckan sager vad ett klick gor: %r" % sumtxt)
    check(not page.locator("#advBody .ptrow").first.is_visible(),
          "punkterna syns inte forran man oppnar")
    page.locator("#advSum").click()
    page.wait_for_timeout(700)
    check(page.locator("#advSum").inner_text().strip() == "Avancerade värmeinställningar",
          "och bara rubriken nar den redan ar oppen: %r" % page.locator("#advSum").inner_text())
    pts = page.locator('#advBody .ptrow[data-pt]').count()
    check(pts == 7, "sju punkter i listan (%d)" % pts)
    check(page.locator("#curveChart").count() == 1, "kurvan ritas som bild")
    check(page.locator("#curveChart .lim").count() == 2,
          "min och max framledning ritas som golv och tak")
    adv = page.locator("#advBody").inner_text()
    check("40044" in adv, "registernumret star kvar, men nedtonat")
    check("minusgrader" in adv, "punkterna forklaras i vader, inte i register")
    shoot(page, out, "%s-%s-avancerat" % (mode, label))
    over = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
    check(over <= 1, "ingen horisontell scroll med avancerat oppet (%d px)" % over)
    page.locator("#advSum").click()
    page.wait_for_timeout(300)

    page.locator('nav button[data-view="now"]').click()
    page.wait_for_timeout(700)

    vis = lambda sel: page.locator(sel).is_visible()          # noqa: E731
    if mode == "off":
        for sel in ["#indoorCard", "#weatherCard", "#spotCard", "#tuneCard",
                    "#alarmBanner"]:
            check(not vis(sel), "%s renderas inte alls" % sel)
        check(not vis("#alarmHistWrap"),
              "larmbevakningen ar pa, men en nyinstallation har ingen historik an")
        page.locator("#goSettings").click()
        page.wait_for_timeout(1200)
        check(vis("#featHint"), "tipset om config.yaml finns under Installningar")
        txt = page.locator("#featHintList").inner_text()
        check("homey_host" in txt and "tibber_token" in txt and "weather_lat" in txt,
              "tipset namnger nycklarna")
        check("pushover_token" in txt,
              "och sager att det ar notiserna som saknas, inte larmbevakningen")
        check('homey_devices: "Sovrum, Vardagsrum"' in txt,
              "homey_devices star som strang, precis som config.example.yaml vill ha den")
        check("autotune_target_indoor" in txt,
              "kalibreringstipset sager vad som faktiskt behover fyllas i")
        check("Inget att fylla i" not in txt,
              "tipset lovar inte att kalibreringen slar pa sig sjalv")
        check(page.locator("#featHint .cfg").count() >= 3, "nycklarna visas som kod")
        page.locator('nav button[data-view="heat"]').click()
        page.wait_for_timeout(1000)
        lead = page.locator("#adviceLead").inner_text()
        check("Två källor" not in lead,
              "utan kalibrering lovar ingressen inte tva kallor: %r" % lead[:70])
        over = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
        check(over <= 1, "ingen horisontell scroll (%d px over)" % over)
        return

    check(not vis("#featHint"), "inget config-tips nar allt som gar att sla pa ar paslaget")
    check(vis("#indoorCard"), "inomhuskortet syns")
    zones = page.locator("#indoorBody .zone").count()
    check(zones >= 1, "inomhustemperatur per vaning (%d)" % zones)
    hero = page.locator("#heroKey").inner_text().strip().lower()
    check(hero == "inne", "hjalten pa Just nu ar husets temperatur, inte varmvattnet (%r)" % hero)
    check("Larm" not in page.locator("#heroSide").inner_text(),
          "ingen 'Larm 0'-rad i hjalten")
    page.locator("#indoorBody details summary").click()
    page.wait_for_timeout(350)
    dim = page.locator("#indoorBody tr.dim").count()
    check(page.locator("#indoorBody tr").count() >= 4 and dim >= 1,
          "givarlistan visar aven tysta givare (%d graa)" % dim)
    check("inaktuell" not in page.locator("#indoorBody").inner_text(),
          "en givare ar inte 'inaktuell', den har inte svarat")
    shoot(page, out, "%s-%s-now-sensorer" % (mode, label))

    check(vis("#weatherCard"), "vaderkortet syns")
    check(page.locator("#wxChart path.ln").count() == 1, "timprognosen ritas som kurva")
    check(page.locator("#weatherBody .day").count() >= 3, "dygnsprognos i rutor")
    wx = page.locator("#weatherBody").inner_text()
    check("48 h" not in wx and "-" not in wx.replace("−", ""),
          "inga engelska minustecken eller '48 h' i vaderkortet")
    check("i dag" in wx or "i morgon" in wx, "tidsaxeln sager vilken dag")
    check("om två dygn" not in wx,
          "48-timmarslagsta ar inte 'kallast om tva dygn'")
    low = page.locator("#weatherBody .metric .n").first.inner_text()
    check(low not in ("", "–"), "kommande lagsta temperatur visas: %r" % low)

    # -- larmet ska synas pa varje flik, inte bara pa Just nu --------------
    if mode == "full":
        check(vis("#alarmBanner"), "aktivt larm pa Just nu")
        n = page.locator("#alarmBanner .alarmcard").count()
        sev = page.locator("#alarmBanner .alarmcard.sev-warning").count()
        check(n == 2 and sev == 1, "larmen fargade efter allvar (%d st, %d varning)" % (n, sev))
        check("Kontrollera" in page.locator("#alarmBanner").inner_text(),
              "foreslagen atgard star i larmet")
        check(page.locator("#alarmBanner .hint").count() == 1,
              "aterstallningsforklaringen star en gang, inte en gang per larm")
        for tab in ("heat", "water", "hist"):
            page.locator('nav button[data-view="%s"]' % tab).click()
            page.wait_for_timeout(500)
            check(vis("#alarmBanner"), "larmet syns aven pa fliken %s" % tab)
            check("larm" in page.locator("#freshness").inner_text().lower(),
                  "rubrikraden sager larm pa %s, inte 'live'" % tab)
        page.locator('nav button[data-view="now"]').click()
        page.wait_for_timeout(600)
    else:
        check(not vis("#alarmBanner"), "ingen larmruta nar inget larmar")
        check("larm" not in page.locator("#freshness").inner_text().lower(),
              "och rubrikraden sager inte larm heller")
    check(vis("#alarmHistWrap"), "larmhistoriken finns bakom en knapp")
    page.locator("#alarmHistSum").click()
    page.wait_for_timeout(400)
    hist_txt = page.locator("#alarmHistWrap").inner_text()
    # "information" innehaller "info", sa leta efter de engelska orden som de star.
    check("warning" not in hist_txt and "severity" not in hist_txt,
          "inga engelska allvarsgrader i larmhistoriken")
    check(hist_txt.count("varning") == 2 and hist_txt.count("information") == 1,
          "allvarsgraden star pa svenska, en gang per rad (%d/%d)"
          % (hist_txt.count("varning"), hist_txt.count("information")))
    check(page.locator("#alarmTest").count() == 1, "det gar att prova larmnotisen")
    if mode == "quiet":
        check("når inte fram" in hist_txt or "avvisade" in hist_txt,
              "en avvisad pushover-nyckel syns i appen, inte bara i loggen")
    page.locator("#alarmHistSum").click()
    page.wait_for_timeout(300)
    check(page.locator("#alarmBanner button").count() == 0,
          "ingen aterstall-larm-knapp finns")

    # -- elpriset: en genvag med tydlig knapp, och en egen flik ------------
    check(page.locator("#priceStrip button").count() == 1, "elprisgenvag pa Just nu")
    strip = page.locator("#priceStrip").inner_text()
    check("kr/kWh" in strip, "genvagen visar samma enhet som kortet: %r" % strip.replace("\n", " "))
    page.locator("#priceStrip button").click()
    page.wait_for_timeout(1200)
    check(page.locator("#v-price").is_visible(), "genvagen leder till elprisfliken")

    check(vis("#spotCard"), "elpriskortet pa sin egen flik")
    bars = page.locator("#spotBody .bars rect").count()
    check(bars >= 24, "timpriserna ritas som staplar (%d rektanglar)" % bars)
    check(page.locator("#spotBody .barwrap .yax span").count() == 3,
          "staplarna har en y-axel att lasa av")
    txt = page.locator("#spotBody").inner_text()
    if mode != "quiet":
        check(txt.count("gör ingenting av det här själv") == 1,
              "planen sager en gang att appen inte agerar sjalv")
        rows = page.locator("#spotBody .planrow").count()
        check(rows >= 2, "planen listar timmar (%d rader)" % rows)
        check("summerar till noll" in txt, "planen forklarar att dygnet summerar till noll")
        check("Summan per dygn" not in txt, "och sager det bara en gang")
        two = page.evaluate("""() => {
          renderSpot({prices: {ok: true, currency: 'SEK', now: {total: 1, band: 'dyr'},
            hours: [], stats: {}},
            plan: {ok: true, advisory: true, hours: [
              {starts_at: '2026-01-01T02:00:00+01:00', offset_delta: 2, why: 'billigt'},
              {starts_at: '2026-01-01T18:00:00+01:00', offset_delta: -2, why: 'dyrt'}],
              days: [{day: '2026-01-01', net: 0}]}});
          return [...document.querySelectorAll('#spotBody .delta')].map(e => e.textContent);
        }""")
        check(two == ["+2", "−2"], "planen visar hela steget, inte alltid ±1 (%s)" % two)
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(2000)
        page.locator('nav button[data-view="price"]').click()
        page.wait_for_timeout(1200)
        check(page.locator("#spotBody .bars rect.nowcol").count() == 1, "nu-timmen markerad")
        check(page.locator("#spotBody .bars rect.split").count() == 1, "imorgon avdelad")
    else:
        check(rows_flat(page), "platt dygn forklaras i stallet for att visa en plan")
        check("gör ingenting av det här själv" not in txt,
              "ingen varning om att appen inte agerar nar det inte finns nagon plan")

    page.locator('nav button[data-view="heat"]').click()
    page.wait_for_timeout(1600)
    check(vis("#tuneCard"), "kalibreringskortet pa Varme")
    ttxt = page.locator("#tuneBody").inner_text()
    if mode == "quiet":
        check(page.locator("#tuneGo").count() == 0, "inget forslag nar underlaget inte racker")
        check(page.locator("#tuneBody ul.miss li").count() >= 2, "det som saknas listas")
    else:
        check(page.locator("#tuneGo").count() == 1, "forslaget har en knapp")
        check("40031" in ttxt and "Värmeoffset" in ttxt,
              "forslaget namnger reglaget och registret")
        check("Register 40031:" not in ttxt,
              "forslaget kallar det Varmeoffset, inte 'Register 40031'")
    check("-0.6" not in ttxt and "+0.02" not in ttxt,
          "inga engelska decimalpunkter i kalibreringskortet")
    lead = page.locator("#adviceLead").inner_text()
    check("Två källor" in lead,
          "med kalibreringen igang finns det tva kallor: %r" % lead[:60])
    shoot(page, out, "%s-%s-heat-nedre" % (mode, label))

    over = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
    check(over <= 1, "ingen horisontell scroll (%d px over)" % over)


def rows_flat(page):
    t = page.locator("#spotBody").inner_text()
    return "för jämnt" in t or "för lite" in t or "för liten" in t


def register_fallback(page, url, out):
    """Larmbevakningen kunde inte startas. Da finns bara larmnumret i register
    31976, och den samre rutan ska visas anda -- ett larm ar viktigare an
    snyggheten."""
    print("\n=== larm utan larmbevakning ===")
    page.goto(url, wait_until="networkidle")
    page.wait_for_timeout(2400)
    check(page.locator("#alarmBanner").is_visible(), "registerlarmet visas anda")
    txt = page.locator("#alarmBanner").inner_text()
    check("175" in txt, "larmnumret star i rutan: %r" % txt[:60])
    check(not page.locator("#alarmHistWrap").is_visible(), "ingen larmhistorik utan bevakning")
    check(not page.locator("#indoorCard").is_visible(), "inget annat nytt renderas")
    shoot(page, out, "fallback-registerlarm")
    page.locator('nav button[data-view="heat"]').click()
    page.wait_for_timeout(800)
    check(page.locator("#alarmBanner").is_visible(), "och det foljer med till Varme")


def alarm_never_invented(page, url, out):
    """Det falska larmet: 32196 svarar "No alarm", vilket ar sant i JavaScript.

    Tva tillstand i ett lage. Forst den kalla starten -- /api/status har svarat,
    /api/alarms har inte -- och sedan det permanenta: /api/alarms svarar 502.
    I bada agde registerfallbacken larmrutan, och i bada malade den rott.
    """
    print("\n=== larm ur ingenting ===")
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(1300)                 # /api/alarms droejer 2,5 s
    check(page.locator("#tiles .tile").count() >= 6,
          "pumpens varden har hunnit fram (%d kakel)" % page.locator("#tiles .tile").count())
    for when in ("kall start", "larmbevakningen svarar inte"):
        check(page.locator("#alarmBanner").is_hidden(),
              "ingen larmruta: %s" % when)
        fresh = page.locator("#freshness").inner_text().lower()
        check("larm" not in fresh, "rubrikraden sager inte larm (%s): %r" % (when, fresh))
        cls = page.locator("#statusWrap").get_attribute("class") or ""
        check("alarm" not in cls, "rubrikraden ar inte rod (%s): %r" % (when, cls))
        vals = all_values(page)
        check(vals.get("32196") == "inget larm",
              "och registret sager just det: %r" % vals.get("32196"))
        shoot(page, out, "alarmdown-%s" % when.split()[0])
        page.wait_for_timeout(3200)             # nu har 502 kommit


def mapped_setting(page, url, out):
    """Ett mappat register, hela vagen: raden, valjaren, rutan och kvittensen."""
    print("\n=== mappad installning ===")
    page.goto(url + "#water", wait_until="networkidle")
    page.wait_for_timeout(2600)
    check(setrow_value(page, 40057) == "Medel",
          "raden visar ordet, inte '–': %r" % setrow_value(page, 40057))
    check((setrow_value(page, 40067) or "").endswith("dygn"),
          "och grannen raknar i dygn: %r" % setrow_value(page, 40067))
    rng = page.evaluate("""() => document.querySelector('[data-edit="40067"]')
      .closest('.setrow').querySelector('.addr').textContent""")
    check("dygn" in rng and "days" not in rng, "aven i intervallet: %r" % rng)

    page.locator('[data-edit="40057"]').click()
    page.wait_for_timeout(400)
    opts = page.locator("#sv-40057 option").all_inner_texts()
    check(opts[:3] == ["Litet", "Medel", "Stort"],
          "valjaren star pa svenska: %s" % opts)
    sel = page.locator("#sv-40057").input_value()
    check(sel == "1", "och forvaljer det pumpen star pa (%r)" % sel)
    shoot(page, out, "mappad-valjare")
    page.locator("#sv-40057").select_option("2")
    page.locator('[data-save="40057"]').click()
    page.wait_for_timeout(600)
    check(page.locator("#modal").is_visible(), "bekraftelserutan oppnas")
    mtxt = page.locator("#modalBody").inner_text()
    # Sjalva andringsraden, inte hela rutan: registrets `why` kommer ur
    # nibelokal/settings.py och rakar rakna upp kartans engelska nycklar
    # ("Small, Medium, Large eller Smart Control") -- det ar en mening i
    # backend, inte appens oversattning, och hor inte hemma i det har testet.
    arrow = page.locator("#modalBody .arrow").first.inner_text()
    arrow = " ".join(arrow.split())
    check(arrow == "Medel → Stort", "rutan sager 'Medel → Stort': %r" % arrow)
    check("Medium" not in arrow and "→ 2" not in arrow,
          "inte 'Medium → 2': %r" % arrow)
    check("nästa varmvattenladdning" in mtxt,
          "och vantetiden galler varmvatten, inte huset: %r"
          % mtxt.replace("\n", " ")[-120:])
    check("ett dygn" not in mtxt and "två dygn" not in mtxt,
          "ingen dygnsvantan for en varmvatteninstallning")
    shoot(page, out, "mappad-bekraftelse")
    page.locator("#modalYes").click()
    page.wait_for_timeout(1500)
    t = page.locator("#toast .toast").first.inner_text()
    check("Medel" in t and "Stort" in t and "Medium" not in t,
          "kvittensen sager samma sak: %r" % t)
    check("Känn efter" not in t, "och ber ingen kanna efter i huset: %r" % t)


def ventilation_down(page, url, out):
    """Vagen appen medvetet lagger en varning framfor -- och som var dod."""
    print("\n=== mindre luft an normalt ===")
    posts = []
    page.on("request", lambda r: posts.append(r.url) if r.method == "POST" else None)
    errs = []
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.goto(url + "#water", wait_until="networkidle")
    page.wait_for_timeout(2600)
    opts = page.locator("#ventMode option").all_inner_texts()
    check(any("MINDRE luft" in o for o in opts),
          "ett lage under normalt finns i listan: %s" % opts)
    page.locator("#ventMode").select_option("1")
    page.locator("#ventSet").click()
    page.wait_for_timeout(600)
    check(page.locator("#modal").is_visible(), "varningen visas")
    check("Mindre luft" in page.locator("#modalTitle").inner_text(), "och sager vad den galler")
    shoot(page, out, "ventilation-varning")
    page.locator("#modalYes").click()
    page.wait_for_timeout(1800)
    check(any("/api/ventilation" in u for u in posts),
          "skrivningen gick i vag (%s)" % posts)
    check(not errs, "ingen TypeError pa vagen (%s)" % errs[:2])
    t = page.locator("#toast .toast").first.inner_text()
    check("Läge 1" in t and "0" in t, "och kvitteras: %r" % t)


def owner_curve(page, url, out):
    """Agarens egen kurva: P1-P3 alla 45, P7 under min framledning."""
    print("\n=== agarens kurva ===")
    page.goto(url + "#heat", wait_until="networkidle")
    page.wait_for_timeout(2600)
    page.locator("#advSum").click()
    page.wait_for_timeout(700)
    body = page.locator("#advBody").inner_text()
    check("P1–P3 ligger alla på 45" in body,
          "den flata kalla anden pekas ut: %r" % body[body.find("P1–P3"):][:80])
    check("58" in body, "och taket namns, sa att man ser att det inte ar taket som haller nere den")
    check("under min framledning" in body, "P7 under golvet pekas ut")
    check(page.locator("#curveChart path.raw").count() == 1,
          "det pumpen faktiskt skickar ut ritas streckat nar en punkt ligger utanfor")
    check(page.locator("#curveChart line.shift, #curveChart .shiftpt").count() >= 1,
          "punktforskjutningen ritas i bilden, inte bara beskrivs under den")
    check("−1" in page.locator("#offsetNow").inner_text(), "offset -1 med riktigt minustecken")
    rules = page.locator(".rule").inner_text()
    check(len(rules) > 60 and "inte P5 och P6" in rules,
          "regeltabellen sager vilket par som faktiskt galler i milt vader: %r" % rules[-220:])
    # Etiketterna for golv och tak far inte ligga ovanpa punkterna.
    hit = page.evaluate("""() => {
      const svg = document.querySelector('#curveChart');
      const labs = [...svg.querySelectorAll('text')].filter(t => /Min |Max /.test(t.textContent));
      const dots = [...svg.querySelectorAll('circle')];
      let worst = 0;
      for (const l of labs) { const a = l.getBoundingClientRect();
        for (const d of dots) { const b = d.getBoundingClientRect();
          const ox = Math.min(a.right, b.right) - Math.max(a.left, b.left);
          const oy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
          if (ox > 0 && oy > 0) worst = Math.max(worst, Math.min(ox, oy)); } }
      return worst; }""")
    check(hit < 1.5, "gransetiketterna krockar inte med kurvans punkter (%.1f px)" % hit)
    shoot(page, out, "curve-avancerat-oppet")

    # radgivaren pekar ut punkten, och punkten ritas i kurvan
    page.locator('#whenTabs [data-when="cold_outside"]').click()
    page.locator("#adviceGo").click()
    page.wait_for_timeout(2500)
    check(page.locator("#adviceOut .prop").count() == 1,
          "de tva punkterna ligger i ett forslag med en knapp")
    check(page.locator("#adviceOut [data-group]").count() == 1, "en knapp, inte tva")
    check(page.locator("#curveChart circle.ghost").count() == 2,
          "forslaget ritas som spoken i kurvan")
    advtxt = page.locator("#advBody").inner_text()
    check("Rådet just nu gäller den här kurvan" in advtxt,
          "avancerat speglar radet, utan en andra knapp")
    check(page.locator("#advBody button.primary").count() == 0,
          "ingen andra genomfor-knapp i avancerat")
    shoot(page, out, "curve-rad-i-kurvan")

    # och skrivningen gar genom samma ruta som allt annat
    page.locator("#adviceOut [data-group]").click()
    page.wait_for_timeout(500)
    check(page.locator("#modal").is_visible(), "bekraftelserutan oppnas")
    mtxt = page.locator("#modalBody").inner_text()
    check("40044" in mtxt and "45" in mtxt and "47" in mtxt,
          "rutan visar register och fore -> efter")
    check("ett dygn" in mtxt, "rutan sager hur lange man ska vanta")
    check("Var för sig gör de fel sak" not in mtxt,
          "tva egna kurvpunkter gor inte fel sak var for sig")
    shoot(page, out, "curve-bekraftelse")
    page.locator("#modalNo").click()
    page.wait_for_timeout(300)
    check(not page.locator("#modal").is_visible(), "och gar att avbryta")

    # den milda regeln, som backend faktiskt foljer
    page.locator('#whenTabs [data-when="mild_outside"]').click()
    page.locator("#adviceGo").click()
    page.wait_for_timeout(2500)
    got = page.locator("#adviceOut").inner_text()
    check("P4" in got and "P5" in got,
          "och radet landar dar backend brackar: %r" % got[:140].replace("\n", " "))


def heat_down(page, url, out):
    """/api/heating svarar 502. Varmefliken ska saga det."""
    print("\n=== pumpen svarar inte pa /api/heating ===")
    page.goto(url + "#heat", wait_until="networkidle")
    page.wait_for_timeout(2600)
    txt = page.locator("#heatState").inner_text()
    check(len(txt) > 30, "varmefliken sager vad som ar fel: %r" % txt[:70])
    check("502" in txt or "svarar inte" in txt or "nekades" in txt, "och sager varfor")
    check(page.locator("#heatUp").is_disabled() and page.locator("#heatDown").is_disabled(),
          "stegaren gar inte att anvanda utan avlast varde")
    shoot(page, out, "heat502-varme")


def unreachable(page, url, out):
    """Allt konfigurerat, ingenting svarar."""
    print("\n=== integrationerna svarar inte ===")
    page.goto(url, wait_until="networkidle")
    page.wait_for_timeout(2600)
    for sel, ord_ in [("#indoorCard", "Homey"), ("#weatherCard", "SMHI")]:
        check(page.locator(sel).is_visible(), "%s visas med felet i stallet for att forsvinna" % sel)
        check(ord_ in page.locator(sel).inner_text(), "%s namnger tjansten" % sel)
    # Larmbevakningen tiger, men pumpen sager "No alarm": ingen ruta ska malas.
    check(not page.locator("#alarmBanner").is_visible(),
          "ingen larmruta ur en tyst larmbevakning och en tyst pump")
    shoot(page, out, "unreach-now")
    page.locator('nav button[data-view="heat"]').click()
    page.wait_for_timeout(1500)
    check(page.locator("#advCard").is_visible(),
          "avancerat funkar anda -- det hanger inte pa nagon integration")
    shoot(page, out, "unreach-heat")


def slow_server(page, url, out):
    """Servern svarar, men langsamt: skelett ska sta kvar, inget far krascha."""
    print("\n=== langsam server ===")
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(600)
    check(page.locator(".skel").count() >= 1, "skelett medan svaren droejer")
    check(not page.locator("#alarmBanner").is_visible(),
          "och ingen larmruta medan ingenting ar last")
    shoot(page, out, "slow-tidigt")
    page.wait_for_timeout(9000)
    check(page.locator("#tiles .tile").count() >= 6, "och sedan kommer varden")
    check(page.locator(".skel").count() == 0, "skeletten forsvinner nar svaren kommit")
    shoot(page, out, "slow-klart")


def guarded_write(page, url):
    """Autotune-forslaget ska ga genom appens vanliga bekraftelse -- samma ruta,
    samma ordval och samma vantetid som stegaren och radgivaren."""
    print("\n=== skyddad skrivning ===")
    page.goto(url + "#heat", wait_until="networkidle")
    page.wait_for_timeout(2800)
    page.locator("#tuneGo").click()
    page.wait_for_timeout(600)
    check(page.locator("#modal").is_visible(), "bekraftelserutan oppnas")
    txt = page.locator("#modalBody").inner_text()
    check("40031" in txt and "2" in txt and "3" in txt,
          "bekraftelserutan visar register och fore -> efter")
    check("36 timmar" in txt, "bekraftelserutan sager hur lange man ska vanta")
    check(txt.count("timmar") + txt.count("dygn") >= 1 and "ett dygn" not in txt,
          "en vantetid, inte tva olika i samma ruta")
    check("Värmeoffset" in txt, "rutan kallar reglaget vid namn, inte bara vid nummer")
    page.locator("#modalYes").click()
    page.wait_for_timeout(1800)
    check(page.locator("#toast .toast").count() >= 1, "skrivningen kvitteras med en toast")
    t = page.locator("#toast .toast").first.inner_text()
    check("Värmeoffset" in t and "→" in t, "kvittensen sager samma sak som rutan: %r" % t)
    check("Register 40031" not in t, "och inte 'Register 40031: 2 -> 3'")


def stepper_write(page, url):
    """Stegaren och radgivaren ska ge exakt samma ruta for samma skrivning."""
    print("\n=== samma skrivning, samma ruta ===")
    page.goto(url + "#heat", wait_until="networkidle")
    page.wait_for_timeout(2600)
    page.locator("#heatUp").click()
    page.wait_for_timeout(1200)
    check(page.locator("#modal").is_visible(), "stegaren oppnar samma ruta")
    a = page.locator("#modalBody").inner_text()
    check("40031" in a and "Värmeoffset" in a, "med samma rubrik och samma register")
    page.locator("#modalNo").click()
    page.wait_for_timeout(400)
    page.locator("#adviceGo").click()
    page.wait_for_timeout(2500)
    page.locator("#adviceOut [data-group]").click()
    page.wait_for_timeout(600)
    b = page.locator("#modalBody").inner_text()
    check(page.locator("#modal").is_visible(), "radgivaren oppnar samma ruta")
    check("Värmeoffset" in b and "40031" in b, "med samma rubrik och samma register")
    check(("ett dygn" in a) == ("ett dygn" in b), "och samma vantetid: %r / %r"
          % (a[-90:].replace("\n", " "), b[-90:].replace("\n", " ")))
    page.locator("#modalNo").click()


MODES = ["full", "quiet", "off", "fallback", "curve", "unreach",
         "slow", "heat502", "alarmdown"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve", action="store_true", help="bara servern, ingen webblasare")
    ap.add_argument("--mode", default="full", choices=MODES)
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
    for mode in MODES:
        ports[mode] = free_port()
        start(mode, ports[mode])
    time.sleep(0.4)
    url = lambda m: "http://127.0.0.1:%d/" % ports[m]        # noqa: E731

    with sync_playwright() as pw:
        b = pw.chromium.launch()
        for mode in ("full", "quiet", "off", "curve"):
            for label, w, h in [("telefon", 390, 844), ("dator", 1280, 900)]:
                page = b.new_page(viewport={"width": w, "height": h})
                errs = []
                page.on("console", lambda msg: errs.append(msg.text) if msg.type == "error" else None)
                page.on("pageerror", lambda e: errs.append("pageerror: %s" % e))
                drive(page, mode, label, out, url(mode))
                real = [e for e in errs if "favicon" not in e.lower()]
                check(not real, "inga konsolfel i %s/%s%s"
                      % (mode, label, (": " + "; ".join(real[:3])) if real else ""))
                page.close()

        for fn, mode in [(register_fallback, "fallback"), (owner_curve, "curve"),
                         (heat_down, "heat502"), (unreachable, "unreach"),
                         (slow_server, "slow"), (alarm_never_invented, "alarmdown"),
                         (mapped_setting, "full"), (ventilation_down, "full")]:
            page = b.new_page(viewport={"width": 390, "height": 844})
            errs = []
            page.on("pageerror", lambda e: errs.append("pageerror: %s" % e))
            fn(page, url(mode), out)
            check(not errs, "inga JS-krascher i %s/%s: %s"
                  % (mode, fn.__name__, errs[:2]))
            page.close()

        for fn in (guarded_write, stepper_write):
            page = b.new_page(viewport={"width": 390, "height": 844})
            fn(page, url("full"))
            page.close()

        # morkt lage, i bada bredder, over alla flikar
        print("\n=== morkt lage ===")
        for label, w, h in [("telefon", 390, 844), ("dator", 1280, 900)]:
            page = b.new_page(color_scheme="dark", viewport={"width": w, "height": h})
            page.goto(url("full"), wait_until="networkidle")
            page.wait_for_timeout(2400)
            for name, _ in TABS:
                tab = page.locator('nav button[data-view="%s"]' % name)
                if not tab.is_visible():
                    continue
                tab.click()
                page.wait_for_timeout(800)
                shoot(page, out, "full-mork-%s-%s" % (label, name))
            page.locator('nav button[data-view="heat"]').click()
            page.wait_for_timeout(900)
            page.locator("#advSum").click()
            page.wait_for_timeout(700)
            shoot(page, out, "full-mork-%s-avancerat" % label)
            page.locator("#goSettings").click()
            page.wait_for_timeout(1600)
            shoot(page, out, "full-mork-%s-set" % label)
            bg = page.evaluate("() => getComputedStyle(document.body).backgroundColor")
            check(bg not in ("rgba(0, 0, 0, 0)", "rgb(255, 255, 255)"),
                  "mork bakgrund i %s: %s" % (label, bg))
            page.close()

        page = b.new_page(color_scheme="dark", viewport={"width": 390, "height": 844})
        page.goto(url("curve") + "#heat", wait_until="networkidle")
        page.wait_for_timeout(2400)
        page.locator("#advSum").click()
        page.wait_for_timeout(700)
        shoot(page, out, "curve-mork-avancerat")
        page.close()

        # Varje lage i bada bredder och bada temana. De fyra kombinationerna ar
        # billiga att kora och dyra att missa: en ruta som spricker vid 1280 px
        # i morkt lage ar precis den sorts fel ingen tittar efter.
        print("\n=== alla lagen, bada bredder, bada temana ===")
        for mode in MODES:
            for scheme in ("light", "dark"):
                for label, w, h in [("telefon", 390, 844), ("dator", 1280, 900)]:
                    page = b.new_page(color_scheme=scheme,
                                      viewport={"width": w, "height": h})
                    errs = []
                    page.on("pageerror", lambda e: errs.append(str(e)))
                    page.on("console",
                            lambda m: errs.append(m.text) if m.type == "error" else None)
                    page.goto(url(mode), wait_until="domcontentloaded")
                    page.wait_for_timeout(9000 if mode in ("slow", "alarmdown") else 3000)
                    shoot(page, out, "sweep-%s-%s-%s" % (mode, scheme, label))
                    over = page.evaluate(
                        "() => document.documentElement.scrollWidth - window.innerWidth")
                    bg = page.evaluate("() => getComputedStyle(document.body).backgroundColor")
                    dark = bg not in ("rgba(0, 0, 0, 0)", "rgb(255, 255, 255)")
                    check(over <= 1, "%s/%s/%s: ingen horisontell scroll (%d px)"
                          % (mode, scheme, label, over))
                    check(dark if scheme == "dark" else True,
                          "%s/%s/%s: temat slar igenom (%s)" % (mode, scheme, label, bg))
                    # "Failed to load resource" ar webblasarens egen rad om en
                    # 502, och en 502 fran /api/alarms eller /api/heating ar
                    # ett riktigt tillstand som servern producerar (server.py
                    # svarar 502 nar en endpoint kastar). Det appen inte far
                    # gora ar att kasta sjalv -- det fangas av pageerror.
                    real = [e for e in errs if "favicon" not in e.lower()
                            and "failed to load resource" not in e.lower()]
                    check(not real, "%s/%s/%s: inga konsolfel%s"
                          % (mode, scheme, label,
                             (": " + "; ".join(real[:2])) if real else ""))
                    # Ingen larmruta far uppsta ur ett register som sager "No alarm".
                    if mode not in ("full", "fallback"):
                        check(page.locator("#alarmBanner").is_hidden(),
                              "%s/%s/%s: ingen larmruta ur ingenting" % (mode, scheme, label))
                    page.close()
        b.close()

    print("\nskarmbilder i %s" % out)
    print("\n" + ("ALLT OK" if not fel else "%d FEL:\n  - %s" % (len(fel), "\n  - ".join(fel))))
    return 1 if fel else 0


if __name__ == "__main__":
    sys.exit(main())
