"""The settings worth putting in front of a person, in plain Swedish.

The register map's own titles are terse English abbreviations written for an
installer with the manual open -- "Auto mode, stop temperature for heating"
does not tell you that this is the outdoor temperature at which the pump stops
heating for the season, which is the single setting people most want to move.

So this file is the editorial layer: which of the 562 writable registers are
worth showing, what to call them, what they actually do, and in what order.
Everything here still goes through the same safety gate as any other write --
this decides what is *offered*, never what is *allowed*.

Registers a given pump does not implement drop out automatically: the group is
built from what answered, not from this list.
"""
from __future__ import annotations

# (address, label, explanation). Order within a group is the order shown.
GROUPS: list[dict] = [
    {
        "key": "curve",
        "title": "Värmekurvan",
        "intro": "Kurvan bestämmer hur varmt vatten pumpen skickar ut vid en given "
                 "utetemperatur. Det är den som avgör husets temperatur — inte "
                 "rumsbörvärdet, om du saknar rumsgivare.",
        "registers": [
            (40027, "Värmekurva",
             "1–15, brantare siffra ger mer värme ju kallare det blir. "
             "0 betyder egen kurva: då är det punkterna nedan som gäller och "
             "kurvnumret ignoreras."),
            (40031, "Värmeoffset",
             "Plus ger varmare hus, minus svalare. NIBE kallar det en "
             "parallellförskjutning: framledningen ändras lika mycket vid alla "
             "utetemperaturer. Ungefär en grad inomhus per steg, men hur många steg "
             "som behövs beror på ditt värmesystem. Rätt reglage när huset känns fel "
             "i alla väder — och nedåt tar det stopp vid min framledning."),
            (40046, "Egen kurva P1 (−30 °C ute)", "Framledning när det är −30 ute."),
            (40045, "Egen kurva P2 (−20 °C ute)", "Framledning när det är −20 ute."),
            (40044, "Egen kurva P3 (−10 °C ute)",
             "Framledning vid −10. Den här och P4 är de som styr en normal svensk vinterdag."),
            (40043, "Egen kurva P4 (0 °C ute)", "Framledning vid noll grader."),
            (40042, "Egen kurva P5 (+10 °C ute)", "Framledning i milt väder."),
            (40041, "Egen kurva P6 (+20 °C ute)", "Framledning när det är varmt."),
            (40040, "Egen kurva P7 (+30 °C ute)", "Framledning i sommarvärme."),
            (40047, "Punktförskjutning, utetemperatur",
             "Vid vilken utetemperatur den extra knycken på kurvan sitter."),
            (40048, "Punktförskjutning, grader",
             "Hur mycket kurvan knycks till vid den temperaturen. Används för att "
             "rätta till ett enskilt temperaturintervall utan att flytta resten."),
        ],
    },
    {
        "key": "limits",
        "title": "Gränser och säsong",
        "intro": "Taket och golvet för framledningen, och när pumpen slutar värma "
                 "för säsongen.",
        "registers": [
            (40035, "Min framledning",
             "Lägsta temperatur pumpen skickar ut när den värmer. Med golvvärme är "
             "den satt så att golvet inte känns kallt — sänk försiktigt, och inte "
             "under vad golvbeläggningen tål."),
            (40039, "Max framledning",
             "Taket. Radiatorer vill ha högre, golvvärme lägre. Ligger kurvan över "
             "taket händer ingenting förrän taket höjs."),
            (40185, "Värmestopp, utetemperatur",
             "Över den här utetemperaturen slutar pumpen värma huset. Sänk den om "
             "du tycker att värmen går i onödan på våren, höj om huset känns kallt "
             "i svalt höstväder."),
            (40167, "Starta värmen vid undertemperatur",
             "Hur många grader under börvärdet det ska bli innan värmen startar "
             "igen. Större värde ger färre kompressorstarter men mer svängning."),
            (40094, "Periodtid värme",
             "Hur länge pumpen kör värme innan den växlar till varmvatten."),
        ],
    },
    {
        "key": "hotwater",
        "title": "Varmvatten",
        "intro": "Temperaturerna avgör hur mycket varmvatten du får och hur ofta "
                 "elpatronen behöver hjälpa till. Över ungefär 50–55 °C når "
                 "kompressorn inte hela vägen själv.",
        "registers": [
            (40057, "Varmvattenkomfort", "Small, Medium, Large eller Smart Control."),
            (40064, "Stopptemperatur, normal", "När laddningen slutar i normalläge."),
            (40063, "Stopptemperatur, hög", "Samma, för läget hög."),
            (40065, "Stopptemperatur, låg", "Samma, för läget låg."),
            (40062, "Stopptemperatur, periodisk höjning",
             "Temperaturen vid den periodiska höjningen mot legionella."),
            (40067, "Periodisk höjning, intervall",
             "Antal dygn mellan höjningarna."),
            (40077, "Laddningsoffset",
             "Hur mycket varmare vattnet laddas än stopptemperaturen."),
        ],
    },
    {
        "key": "additional",
        "title": "Tillskott (elpatron)",
        "intro": "Det här är den dyra värmen. Varje kilowattimme här är direktverkande el.",
        "registers": [
            (40103, "Max effekt, intern elpatron",
             "Hur många kilowatt elpatronen får ta. Lägre gör det svårare för "
             "pumpen att klara riktig kyla, men dyrare misstag mindre dyra."),
            (40181, "Tillåt tillskott för värme",
             "0 stänger av elpatronen för husvärmen helt. Varmvattnet påverkas inte."),
            (40186, "Tillskottsstopp, utetemperatur",
             "Över den här utetemperaturen får elpatronen inte hjälpa till med värmen."),
            (40189, "Max skillnad framledning, tillskott",
             "Hur långt under börvärdet framledningen får ligga innan tillskottet går in."),
            (40188, "Max skillnad framledning, kompressor",
             "Samma, men för kompressorn."),
        ],
    },
    {
        "key": "room",
        "title": "Rumsgivare",
        "intro": "Fungerar bara om det finns en rumsgivare kopplad. Saknas den är "
                 "börvärdet ett tal utan verkan.",
        "registers": [
            (40203, "Använd rumsgivare", "0 = av, 1 = på."),
            (40207, "Rumsbörvärde", "Önskad inomhustemperatur."),
            (40211, "Rumsgivarfaktor",
             "Hur hårt givaren får påverka framledningen. Högre = snabbare "
             "korrigering men mer svängning."),
        ],
    },
    {
        "key": "other",
        "title": "Övrigt",
        "intro": "",
        "registers": [
            (40020, "Semesterläge", "−1 betyder avstängt."),
            (40228, "Nattsvalka", "0 = av, 1 = på."),
            (40229, "Starttemperatur nattsvalka",
             "Inomhustemperatur då nattsvalkan startar."),
            (40012, "Gradminuter",
             "Pumpens mått på hur mycket värme som saknas. Skriv bara här om du vet "
             "varför — det tvingar fram kompressor- eller tillskottsstart direkt."),
            (40182, "Tillåt värme", "0 stänger av husvärmen helt. Varmvatten fortsätter."),
        ],
    },
]

#: Every address this module offers, in the order the groups list them.
ADDRESSES = [addr for g in GROUPS for addr, _, _ in g["registers"]]


def build(pump, values: dict) -> list[dict]:
    """Turn the readings into the groups the app renders.

    A register the pump did not answer is left out entirely rather than shown
    as a dash: an editable field for a setting this model does not have is a
    trap, not information.
    """
    from . import safety

    out = []
    for group in GROUPS:
        rows = []
        for address, label, why in group["registers"]:
            row = values.get(address)
            if not row or "error" in row or row.get("value") is None:
                continue
            reg = pump.registry.get(address)
            if reg is None or not reg.writable:
                continue
            rows.append({
                "address": address,
                "label": label,
                "why": why,
                "value": row["value"],
                "unit": reg.unit,
                "min": reg.min,
                "max": reg.max,
                "default": reg.default,
                "options": (sorted(reg.mappings.items(), key=lambda kv: int(kv[0]))
                            if reg.mappings else None),
                "tier": safety.tier(address),
            })
        if rows:
            out.append({"key": group["key"], "title": group["title"],
                        "intro": group["intro"], "rows": rows})
    return out
