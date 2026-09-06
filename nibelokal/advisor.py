"""Advice on heating settings: which knob to turn, and by how much.

Getting a heat curve right by hand is genuinely awful. Not because the physics
is hard, but because the feedback loop is a day long, there are four knobs that
all look like they do the same thing, and the pump gives you no way to tell
which one is wrong.

This is not a model and not a prediction. It is two things:

1. **What the pump can tell us on its own** -- whether a room sensor exists,
   whether the supply temperature is pinned against its own ceiling, whether
   the immersion heater is running, whether degree minutes ever reach zero.
   Those are facts, and several of them silently invalidate the obvious advice.
2. **The standard rule for which knob to turn**, which depends entirely on
   *when* the house feels wrong:

       wrong in all weather      -> offset  (moves the whole curve up or down)
       wrong only when it's cold -> the curve's slope (or the cold points of a
                                    custom curve)
       wrong only when it's mild -> the mild end of the curve

   That single distinction is what people get wrong, and it is why turning the
   offset up in November makes the house too warm in March.

Every suggestion is one step. One step, then wait a day: the building's thermal
mass means a change made at breakfast is not visible until the evening, and
stacking three changes before the first has landed is how people end up lost.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Registers this module reasons about.
R_CURVE = 40027            # heating curve, climate system 1 (0 = own curve)
R_OFFSET = 40031           # heating offset, climate system 1
R_MIN_SUPPLY = 40035
R_MAX_SUPPLY = 40039
R_OWN_CURVE = [40046, 40045, 40044, 40043, 40042, 40041, 40040]  # P1 (coldest) .. P7

# Each own-curve point sits at a fixed outdoor temperature, ten degrees apart,
# P1 at the cold end. Verify against your own pump's display (menu 1.30.7) --
# this is the S-series layout and the point spacing is what makes the advice
# land on the right point instead of one nobody's weather ever reaches.
OWN_CURVE_OUTDOOR = [-30, -20, -10, 0, 10, 20, 30]
R_ROOM_SETPOINT = 40207
R_ROOM_TEMP = 30117
R_OUTDOOR = 30002
R_SUPPLY = 30006
R_RETURN = 30008
R_DEGREE_MINUTES = 40012
R_ADD_HEAT_POWER = 31028
R_CALC_SUPPLY = 31018       # calculated supply -- what the curve is actually asking for
R_PRIORITY = 31029          # what the pump is currently prioritising

READ = [R_CURVE, R_OFFSET, R_MIN_SUPPLY, R_MAX_SUPPLY, R_ROOM_SETPOINT, R_ROOM_TEMP,
        R_OUTDOOR, R_SUPPLY, R_RETURN, R_DEGREE_MINUTES, R_ADD_HEAT_POWER,
        R_CALC_SUPPLY, R_PRIORITY] + R_OWN_CURVE

# Roughly how much one step of offset moves the indoor temperature. NIBE's own
# documentation and the installer rule of thumb both land near 1 degree; it is
# a starting point for one iteration, not a calibrated model.
DEGREES_PER_OFFSET_STEP = 1.0

# 40027 accepts 0..15, but 0 is not "flatter than 1" -- it switches the pump to
# the own-curve points entirely. Advice never crosses that line by accident.
CURVE_MIN, CURVE_MAX = 1, 15
OFFSET_MIN, OFFSET_MAX = -10, 10

# What the water actually runs through changes how long you must wait before a
# change means anything, and how big a step is sensible. Underfloor heating is
# a slab of concrete: it wants low supply temperatures and answers in a day or
# two. Radiators answer in hours and want it hotter. A house with both on one
# curve is always a compromise, and the advice should say so rather than
# pretend one number suits both.
EMITTERS = {
    "floor": {
        "name": "golvvärme",
        "wait": "två dygn",
        "point_step": 1,
        "note": "Golvvärme är trög: en ändring på morgonen syns i rummet först "
                "nästa kväll, ibland dagen därpå. Vänta ut den innan du rör "
                "något igen — annars är det omöjligt att veta vad som gjorde vad.",
    },
    "radiators": {
        "name": "radiatorer",
        "wait": "ett dygn",
        "point_step": 2,
        "note": "",
    },
    "mixed": {
        "name": "golvvärme och radiatorer",
        "wait": "två dygn",
        "point_step": 1,
        "note": "Du har golvvärme och radiatorer på samma kurva, vilket alltid är "
                "en kompromiss: golvet vill ha låg framledning, elementen högre. "
                "Min framledning är golvets golv och max är elementens tak — en "
                "ändring flyttar båda. Känns det olika på olika plan är det inte "
                "kurvan som är fel utan injusteringen mellan systemen, och den "
                "sitter i ventilerna, inte här.",
    },
}
DEFAULT_EMITTERS = "radiators"


@dataclass
class Suggestion:
    address: int
    title: str
    current: float
    proposed: float
    unit: str = ""
    why: str = ""
    confirm_required: bool = True
    #: Suggestions sharing a group must be applied together or not at all.
    group: str = ""

    def as_dict(self) -> dict:
        return {
            "address": self.address,
            "title": self.title,
            "current": self.current,
            "proposed": self.proposed,
            "unit": self.unit,
            "why": self.why,
            "confirm_required": self.confirm_required,
            "group": self.group,
        }


@dataclass
class Advice:
    observations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    suggestions: list[Suggestion] = field(default_factory=list)
    blocked: str = ""
    #: How long to wait before judging the change, in words.
    wait: str = "ett dygn"

    def as_dict(self) -> dict:
        return {
            "observations": self.observations,
            "warnings": self.warnings,
            "suggestions": [s.as_dict() for s in self.suggestions],
            "blocked": self.blocked,
            "wait": self.wait,
        }


def _value(data: dict, address: int):
    row = data.get(address) or {}
    v = row.get("value")
    return v if isinstance(v, (int, float)) else None


def emitters(kind: str) -> dict:
    return EMITTERS.get((kind or "").lower(), EMITTERS[DEFAULT_EMITTERS])


def diagnose(pump, store=None, emitter_kind: str = DEFAULT_EMITTERS) -> dict:
    """What the pump can tell us about its own heating, without being asked."""
    data = pump.read_many(READ)
    out = {
        "curve": _value(data, R_CURVE),
        "offset": _value(data, R_OFFSET),
        "min_supply": _value(data, R_MIN_SUPPLY),
        "max_supply": _value(data, R_MAX_SUPPLY),
        "room_setpoint": _value(data, R_ROOM_SETPOINT),
        "room_temp": _value(data, R_ROOM_TEMP),
        "outdoor": _value(data, R_OUTDOOR),
        "supply": _value(data, R_SUPPLY),
        "return": _value(data, R_RETURN),
        "degree_minutes": _value(data, R_DEGREE_MINUTES),
        "additional_heat_kw": _value(data, R_ADD_HEAT_POWER),
        # What the curve is asking for right now. The measured supply lags this
        # and swings with the compressor, so this is the number to judge a
        # curve change by -- and the honest way to tell whether a write landed.
        "calculated_supply": _value(data, R_CALC_SUPPLY),
        "priority": (data.get(R_PRIORITY) or {}).get("value"),
        "own_curve": [_value(data, a) for a in R_OWN_CURVE],
    }
    out["has_room_sensor"] = out["room_temp"] is not None
    out["uses_own_curve"] = out["curve"] == 0
    em = emitters(emitter_kind)
    out["emitters"] = emitter_kind
    out["emitters_name"] = em["name"]
    out["wait"] = em["wait"]

    notes: list[str] = []
    warnings: list[str] = []

    if em["note"]:
        notes.append(em["note"])

    if out["uses_own_curve"]:
        pairs = [(OWN_CURVE_OUTDOOR[i], p) for i, p in enumerate(out["own_curve"])
                 if p is not None]
        if pairs:
            notes.append(
                "Kurvan står på 0, vilket betyder egen kurva: pumpen följer dina egna "
                "punkter (%s) i stället för en av de numrerade kurvorna."
                % ", ".join("%+d °C ute → %g °C fram" % (t, p) for t, p in pairs)
            )
        else:
            notes.append("Kurvan står på 0 (egen kurva), men punkterna gick inte att läsa.")
        warnings.append(
            "Sätter du kurvan till 1–15 slutar pumpen använda dina egna punkter. "
            "Justera hellre offset, eller punkterna själva."
        )

    if not out["has_room_sensor"]:
        warnings.append(
            "Ingen rumsgivare svarar (BT50). Rumsbörvärdet går att skriva men "
            "pumpen har inget att reglera mot, så det påverkar troligen ingenting. "
            "Det som faktiskt styr värmen är kurvan och offset."
        )

    supply = out["calculated_supply"] if out["calculated_supply"] is not None else out["supply"]
    max_supply = out["max_supply"]
    if supply is not None and max_supply is not None and supply >= max_supply - 1:
        warnings.append(
            "Framledningen (%g °C) ligger i taket för max framledning (%g °C). "
            "Att höja värmen mer får ingen effekt förrän taket höjs."
            % (supply, max_supply)
        )

    if out["additional_heat_kw"]:
        why = ""
        if isinstance(out["priority"], str):
            why = " Pumpen prioriterar just nu: %s." % out["priority"]
        warnings.append(
            "Elpatronen går just nu (%g kW).%s Höj inte värmen förrän du vet varför — "
            "annars betalar du för direktverkande el."
            % (out["additional_heat_kw"], why)
        )

    if out["outdoor"] is not None:
        notes.append("Ute %g °C, framledning %s °C, retur %s °C."
                     % (out["outdoor"], _fmt(out["supply"]), _fmt(out["return"])))

    if store is not None:
        hours = _history_hours(store)
        out["history_hours"] = hours
        if hours < 24:
            notes.append(
                "Historiken är bara %s gammal. Råden nedan bygger på hur pumpen "
                "står just nu och på hur en värmekurva fungerar, inte på mätning "
                "över tid — den blir användbar efter ett par veckor."
                % _duration(hours)
            )

    out["observations"] = notes
    out["warnings"] = warnings
    return out


def advise(pump, feeling: str, when: str, store=None,
           emitter_kind: str = DEFAULT_EMITTERS) -> Advice:
    """Turn "too cold, all the time" into one concrete register change.

    feeling: "colder" | "warmer"  -- what the person wants
    when:    "always" | "cold_outside" | "mild_outside"
    """
    # Validate before reading nineteen registers off the pump.
    if feeling not in ("warmer", "colder"):
        raise ValueError("feeling must be 'warmer' or 'colder'")
    if when not in ("always", "cold_outside", "mild_outside"):
        raise ValueError("when must be 'always', 'cold_outside' or 'mild_outside'")

    state = diagnose(pump, store, emitter_kind)
    em = emitters(emitter_kind)
    advice = Advice(observations=list(state["observations"]),
                    warnings=list(state["warnings"]),
                    wait=em["wait"])

    direction = 1 if feeling == "warmer" else -1

    # Raising heat while the immersion heater already runs is the one case where
    # the honest answer is "not until you know why".
    if direction > 0 and state.get("additional_heat_kw"):
        priority = state.get("priority")
        if isinstance(priority, str) and "water" in priority.lower():
            advice.blocked = (
                "Elpatronen går, men för varmvatten (pumpen prioriterar %s just nu), "
                "inte för värmen. Kom tillbaka om en halvtimme när laddningen är "
                "klar, så blir svaret rättvisande." % priority
            )
        else:
            advice.blocked = (
                "Elpatronen går just nu%s. Att höja värmen då gör tillskottet större, "
                "inte värmepumpen effektivare. Ta reda på varför den går först — "
                "vanligast är varmvattenladdning eller att kurvan redan ligger för "
                "högt för utetemperaturen."
                % (" (prioritet: %s)" % priority if isinstance(priority, str) else "")
            )
        return advice

    if when == "always":
        offset = state["offset"]
        if offset is None:
            advice.blocked = "Kunde inte läsa värmeoffset (register 40031)."
            return advice
        if not OFFSET_MIN <= offset + direction <= OFFSET_MAX:
            advice.blocked = (
                "Offset står redan på %g, som är gränsen. Behöver du mer värme än så "
                "är det kurvan som ligger fel, inte offset." % offset
            )
            return advice
        advice.suggestions.append(Suggestion(
            address=R_OFFSET,
            title="Värmeoffset",
            current=offset,
            proposed=offset + direction,
            why="Huset känns %s i alla väder, och då är det hela kurvan som ligger "
                "fel — inte dess lutning. Offset flyttar hela kurvan. Tumregeln är "
                "att ett steg motsvarar ungefär en grad inomhus, men utslaget syns "
                "tydligare i kyla än i milt väder."
                % ("för kallt" if direction > 0 else "för varmt"),
        ))
        return advice

    # Wrong only in some weather = the curve's shape, not its height.
    cold_end = when == "cold_outside"
    if state["uses_own_curve"]:
        idx = _points_for(when, state.get("outdoor"))
        ceiling = state["max_supply"]
        floor = state["min_supply"]
        hit_ceiling = hit_floor = False
        # What the curve asks for in the weather being complained about. A point
        # is worth moving when moving it changes THIS number -- the point's own
        # value against min/max says nothing, because the pump interpolates.
        weather = state.get("outdoor")
        if weather is None:
            weather = -10.0 if cold_end else 8.0
        elif cold_end:
            weather = min(weather - 1.0, -5.0)
        for i in idx:
            current = state["own_curve"][i]
            if current is None:
                continue
            proposed = current + direction * em["point_step"]
            after = curve_at([proposed if j == i else p
                              for j, p in enumerate(state["own_curve"])], weather)
            before = curve_at(state["own_curve"], weather)
            if after is not None and before is not None:
                if ceiling is not None and min(after, before) >= ceiling:
                    hit_ceiling = True
                    continue
                if floor is not None and max(after, before) <= floor:
                    hit_floor = True
                    continue
            advice.suggestions.append(Suggestion(
                address=R_OWN_CURVE[i],
                title="Egen kurva, punkt P%d (%+d °C ute)" % (i + 1, OWN_CURVE_OUTDOOR[i]),
                current=current,
                proposed=proposed,
                unit="°C",
                group="own_curve",
                why="Du kör egen kurva, så det är punkterna som formar den. P%d är "
                    "punkten för %+d °C ute, alltså den som gäller %s. %d grad%s "
                    "framledning där ändrar värmen i just det vädret, utan att röra "
                    "resten av kurvan."
                    % (i + 1, OWN_CURVE_OUTDOOR[i],
                       "när det är kallt" if cold_end else "i milt väder",
                       em["point_step"], "er" if em["point_step"] != 1 else ""),
            ))
        if hit_ceiling:
            advice.warnings.append(
                "Kurvan ligger redan mot max framledning (%s °C, register 40039) i "
                "det vädret. Att höja den längre gör ingenting förrän taket höjs — "
                "och hur högt det får ligga beror på vad ditt värmesystem tål, inte "
                "på något appen bör gissa." % _fmt(ceiling)
            )
        if hit_floor:
            extra = ""
            if emitter_kind in ("floor", "mixed"):
                extra = (" Med golvvärme är min framledning satt för att golvet inte "
                         "ska kännas kallt — sänk den försiktigt, och inte under vad "
                         "golvbeläggningen tål.")
            advice.warnings.append(
                "Kurvan ligger redan på min framledning (%s °C, register 40035) i det "
                "vädret, så pumpen går på golvet och punkten styr ingenting. Vill du "
                "ha svalare där är det 40035 som ska ner.%s" % (_fmt(floor), extra)
            )
        if not advice.suggestions and not advice.blocked:
            if all(state["own_curve"][i] is None for i in idx):
                advice.blocked = "Kunde inte läsa punkterna för din egna kurva."
            else:
                advice.blocked = (
                    "Punkterna för det vädret ligger redan mot gränserna för "
                    "framledning (%s–%s °C), så de går inte att flytta åt det hållet."
                    % (_fmt(floor), _fmt(ceiling))
                )
        return advice

    curve = state["curve"]
    if curve is None:
        advice.blocked = "Kunde inte läsa värmekurvan (register 40027)."
        return advice

    if cold_end:
        proposed = curve + direction
        if not CURVE_MIN <= proposed <= CURVE_MAX:
            advice.blocked = _curve_limit(curve, proposed)
            return advice
        advice.suggestions.append(Suggestion(
            address=R_CURVE,
            title="Värmekurva",
            current=curve,
            proposed=proposed,
            why="Det stämmer bara när det är kallt ute, alltså är det kurvans "
                "lutning som är fel, inte dess höjd. En brantare kurva ger mer "
                "värme ju kallare det blir och lämnar det milda vädret ifred.",
        ))
    else:
        # Mild-weather-only error: flatten the curve, and hold the cold end where
        # it was by moving the offset the other way. Two changes that cancel out
        # in cold weather -- which is exactly the point.
        offset = state["offset"]
        proposed_curve = curve - direction
        if not CURVE_MIN <= proposed_curve <= CURVE_MAX:
            advice.blocked = _curve_limit(curve, proposed_curve)
            return advice
        if offset is None or not OFFSET_MIN <= offset + direction <= OFFSET_MAX:
            advice.blocked = (
                "Den här justeringen kräver att både kurvan och offset flyttas, och "
                "offset (%s) kan inte gå åt det hållet. Att bara ändra kurvan skulle "
                "göra fel sak i kallt väder." % _fmt(offset)
            )
            return advice
        # These two only make sense together: the curve alone would change cold
        # weather too, which is the opposite of what was asked. One group, one
        # button -- see the UI.
        advice.suggestions.append(Suggestion(
            address=R_CURVE,
            title="Värmekurva",
            current=curve,
            proposed=proposed_curve,
            group="mild_pair",
            why="Det stämmer bara i milt väder. En flackare kurva ger mindre "
                "skillnad mellan milt och kallt.",
        ))
        advice.suggestions.append(Suggestion(
            address=R_OFFSET,
            title="Värmeoffset",
            current=offset,
            proposed=offset + direction,
            group="mild_pair",
            why="Och offset åt andra hållet, så att kalla dagar hamnar där de var. "
                "De två ändringarna tar ut varandra när det är kallt och ger effekt "
                "bara i milt väder — därför genomförs de tillsammans.",
        ))

    return advice


def curve_at(points: list, outdoor: float) -> float | None:
    """Supply temperature the own curve asks for at a given outdoor temperature.

    Linear between the two bracketing points, flat outside the ends. This is
    what actually governs the house -- judging a point against min/max supply
    on its own says "this point does nothing" about a point that is half of the
    interpolation currently in force.
    """
    known = [(t, p) for t, p in zip(OWN_CURVE_OUTDOOR, points) if p is not None]
    if not known:
        return None
    if outdoor <= known[0][0]:
        return float(known[0][1])
    if outdoor >= known[-1][0]:
        return float(known[-1][1])
    for (t0, p0), (t1, p1) in zip(known, known[1:]):
        if t0 <= outdoor <= t1:
            if t1 == t0:
                return float(p0)
            k = (outdoor - t0) / (t1 - t0)
            return float(p0) + k * (float(p1) - float(p0))
    return float(known[-1][1])


def _points_for(when: str, outdoor) -> list[int]:
    """Which own-curve points actually govern the weather being complained about.

    Reaching for P1 and P2 because they are "the cold end" is the mistake worth
    avoiding: they sit at -30 and -20 C, which most of Sweden never sees. The
    point that governs a normal cold day is the one bracketing it.
    """
    if when == "cold_outside":
        # Aim just below the current temperature, not far below it: at -10 out,
        # subtracting twelve lands on the -20 and -30 points, which govern
        # weather this house may never see. The mild branch below uses the
        # current temperature directly, and these should be symmetric.
        target = -10.0 if outdoor is None else min(outdoor - 1.0, -5.0)
    else:
        target = 8.0 if outdoor is None else max(outdoor, 5.0)
    # The two points bracketing `target`, clamped to the ends of the curve.
    below = [i for i, t in enumerate(OWN_CURVE_OUTDOOR) if t <= target]
    lo = below[-1] if below else 0
    hi = min(lo + 1, len(OWN_CURVE_OUTDOOR) - 1)
    if lo == hi:
        lo = max(0, hi - 1)
    return [lo, hi]


def _curve_limit(curve: float, proposed: float) -> str:
    if proposed < CURVE_MIN:
        return (
            "Kurvan står redan på %g, och nästa steg nedåt är 0 — vilket inte är en "
            "flackare kurva utan byter pumpen till egen kurva med helt andra "
            "punkter. Sänk hellre offset, eller justera egen kurva medvetet från "
            "pumpens display." % curve
        )
    return "Kurvan står redan på %g, som är den brantaste pumpen har." % curve


def _fmt(v) -> str:
    return "–" if v is None else ("%g" % v)


def _history_hours(store) -> float:
    stats = store.stats()
    if not stats.get("first") or not stats.get("last"):
        return 0.0
    return max(0.0, (stats["last"] - stats["first"]) / 3600.0)


def _duration(hours: float) -> str:
    if hours < 1:
        minutes = max(1, int(hours * 60))
        return "%d minut%s" % (minutes, "er" if minutes != 1 else "")
    if hours < 48:
        whole = int(hours)
        return "%d timm%s" % (whole, "ar" if whole != 1 else "e")
    days = int(hours / 24)
    return "%d dyg%s" % (days, "n" if days != 1 else "n")
