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

       wrong in all weather      -> offset  (parallel shift of the whole curve;
                                    NIBE: "the supply temperature changes by the
                                    same amount for all outdoor temperatures",
                                    +2 steps about +5 C -- roughly 1 C indoors
                                    per step, depending on the emitters)
       wrong only when it's cold -> the curve's slope (or the cold points of a
                                    custom curve)
       wrong only when it's mild -> the mild end of the curve

   That single distinction is what people get wrong, and it is why turning the
   offset up in November makes the house too warm in March.

Every suggestion is one step. One step, then wait a day: the building's thermal
mass means a change made at breakfast is not visible until the evening, and
stacking three changes before the first has landed is how people end up lost.


This module takes no weather forecast. It works from the pump's own outdoor
sensor and the recorded history, and nothing here looks further ahead than
the reading in front of it. The README once claimed otherwise; if a forecast
input is ever wired in, that paragraph has to come back with it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import sv_number

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
#
# This is the S-series list, kept as a module constant because it is what
# autotune and the tests import. The list that is actually used comes from the
# pump's profile: on the F generation the seventh point's outdoor temperature
# is not established -- NIBE's F750 and F1155 user manuals both show menu 1.9.7
# with six rows, -30 to +20 -- and the profile says so with a None, which
# `curve_at` and `_points_for` leave out of the fit entirely. See
# profile.OWN_CURVE_OUTDOOR and docs/f-series.md.
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
        # The same wait as a number, for the code that needs to compute with
        # it. autotune used to derive it by matching the Swedish string
        # ("två dygn" -> 48), which turns a wording change into a silently
        # wrong answer about how long to wait before judging a change.
        "wait_hours": 48,
        "point_step": 1,
        "note": "Golvvärme är trög: en ändring på morgonen syns i rummet först "
                "nästa kväll, ibland dagen därpå. Vänta ut den innan du rör "
                "något igen — annars är det omöjligt att veta vad som gjorde vad.",
    },
    "radiators": {
        "name": "radiatorer",
        "wait": "ett dygn",
        "wait_hours": 24,
        "point_step": 2,
        "note": "",
    },
    "mixed": {
        "name": "golvvärme och radiatorer",
        "wait": "två dygn",
        "wait_hours": 48,
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


def outdoor_points(pump=None) -> list:
    """The outdoor temperature of each own-curve point, for this pump.

    One list, from the profile, so that P7's temperature is asserted in exactly
    one place -- and on an F-series pump is not asserted at all: the entry is
    None there, and everything that reads this list skips a point whose
    temperature it does not know. A pump with no profile (a test double) gets
    the S-series list, which is what this module has always assumed.
    """
    profile = getattr(pump, "profile", None)
    if profile is None:
        return list(OWN_CURVE_OUTDOOR)
    return profile.own_curve_outdoor


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
    # The outdoor temperature each of those points governs, from the profile.
    # Sent to the web app as well, so the curve chart draws the same seven --
    # or six and one unlabelled -- points this module reasons about, from one
    # list rather than from a copy of it in JavaScript. A null in it means the
    # temperature is not established; see profile.OWN_CURVE_OUTDOOR.
    temps = outdoor_points(pump)
    out["own_curve_outdoor"] = temps
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
        pairs = [(temps[i], p) for i, p in enumerate(out["own_curve"])
                 if p is not None and temps[i] is not None]
        if pairs:
            notes.append(
                "Kurvan står på 0, vilket betyder egen kurva: pumpen följer dina egna "
                "punkter (%s) i stället för en av de numrerade kurvorna."
                % ", ".join("%s °C ute → %s °C fram"
                            % (sv_number(t, None, sign=True), sv_number(p, None))
                            for t, p in pairs)
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
            "Framledningen (%s °C) ligger i taket för max framledning (%s °C). "
            "Att höja värmen mer får ingen effekt förrän taket höjs."
            % (sv_number(supply, None), sv_number(max_supply, None))
        )

    if out["additional_heat_kw"]:
        why = ""
        if isinstance(out["priority"], str):
            why = " Pumpen prioriterar just nu: %s." % out["priority"]
        warnings.append(
            "Tillskottet går just nu (%s kW).%s Höj inte värmen förrän du vet varför — "
            "annars betalar du för direktverkande el."
            % (sv_number(out["additional_heat_kw"], None), why)
        )

    if out["outdoor"] is not None:
        notes.append("Ute %s °C, framledning %s °C, retur %s °C."
                     % (sv_number(out["outdoor"], None), _fmt(out["supply"]),
                        _fmt(out["return"])))

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
                "Tillskottet går, men för varmvatten (pumpen prioriterar %s just nu), "
                "inte för värmen. Kom tillbaka om en halvtimme när laddningen är "
                "klar, så blir svaret rättvisande." % priority
            )
        else:
            advice.blocked = (
                "Tillskottet går just nu%s. Att höja värmen då gör det större, "
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
                "Offset står redan på %s, som är gränsen. Behöver du mer värme än så "
                "är det kurvan som ligger fel, inte offset." % sv_number(offset, None)
            )
            return advice
        advice.suggestions.append(Suggestion(
            address=R_OFFSET,
            title="Värmeoffset",
            current=offset,
            proposed=offset + direction,
            why="Huset känns %s i alla väder, och då är det hela kurvan som ligger "
                "fel — inte dess lutning. NIBE beskriver offset som en "
                "parallellförskjutning: framledningen ändras lika mycket vid alla "
                "utetemperaturer, och deras exempel är att +2 steg höjer den ungefär "
                "5 °C, vilket brukar bli runt en grad inomhus per steg. Hur många "
                "steg som krävs för en grad beror på ditt klimatsystem."
                % ("för kallt" if direction > 0 else "för varmt"),
        ))
        return advice

    # Wrong only in some weather = the curve's shape, not its height.
    cold_end = when == "cold_outside"
    if state["uses_own_curve"]:
        # The outdoor temperatures diagnose() read out of the profile, so the
        # advice and the diagnosis agree about which point governs what -- and
        # so a point with no known temperature is not proposed on either.
        temps = state.get("own_curve_outdoor") or list(OWN_CURVE_OUTDOOR)
        idx = _points_for(when, state.get("outdoor"), temps)
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
                              for j, p in enumerate(state["own_curve"])],
                             weather, temps)
            before = curve_at(state["own_curve"], weather, temps)
            if after is not None and before is not None:
                if ceiling is not None and min(after, before) >= ceiling:
                    hit_ceiling = True
                    continue
                if floor is not None and max(after, before) <= floor:
                    hit_floor = True
                    continue
            advice.suggestions.append(Suggestion(
                address=R_OWN_CURVE[i],
                title="Egen kurva, punkt P%d (%s °C ute)"
                      % (i + 1, sv_number(temps[i], None, sign=True)),
                current=current,
                proposed=proposed,
                unit="°C",
                group="own_curve",
                why="Du kör egen kurva, så det är punkterna som formar den. P%d är "
                    "punkten för %s °C ute, alltså den som gäller %s. %d grad%s "
                    "framledning där ändrar värmen i just det vädret, utan att röra "
                    "resten av kurvan."
                    % (i + 1, sv_number(temps[i], None, sign=True),
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


def curve_at(points: list, outdoor: float, temps: list | None = None) -> float | None:
    """Supply temperature the own curve asks for at a given outdoor temperature.

    Linear between the two bracketing points, flat outside the ends. This is
    what actually governs the house -- judging a point against min/max supply
    on its own says "this point does nothing" about a point that is half of the
    interpolation currently in force.

    `temps` is the outdoor temperature of each point, from the pump's profile;
    the S-series list is the default because that is the numbering the whole
    app speaks. A point whose temperature is None is left out of the fit
    entirely -- on the F generation that is P7, whose outdoor temperature is
    not documented anywhere. Interpolating it at a guessed +30 would put the
    guess into every number this function returns in mild weather, which is
    where a heat pump spends most of the year.
    """
    temps = OWN_CURVE_OUTDOOR if temps is None else temps
    known = [(t, p) for t, p in zip(temps, points)
             if p is not None and t is not None]
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


def _points_for(when: str, outdoor, temps: list | None = None) -> list[int]:
    """Which own-curve points actually govern the weather being complained about.

    Reaching for P1 and P2 because they are "the cold end" is the mistake worth
    avoiding: they sit at -30 and -20 C, which most of Sweden never sees. The
    point that governs a normal cold day is the one bracketing it.

    A point with no known outdoor temperature is not a candidate: it cannot be
    said to govern any weather. On an F pump that is P7, so the mild end of the
    advice stops at P6 (+20 C) there. See profile.OWN_CURVE_OUTDOOR.
    """
    temps = OWN_CURVE_OUTDOOR if temps is None else temps
    if when == "cold_outside":
        # Aim just below the current temperature, not far below it: at -10 out,
        # subtracting twelve lands on the -20 and -30 points, which govern
        # weather this house may never see. The mild branch below uses the
        # current temperature directly, and these should be symmetric.
        target = -10.0 if outdoor is None else min(outdoor - 1.0, -5.0)
    else:
        target = 8.0 if outdoor is None else max(outdoor, 5.0)
    # The two points bracketing `target`, clamped to the ends of the curve.
    # Indices of the points that have a temperature at all, in order, so that
    # "the point above" is the next known one rather than the next number.
    usable = [i for i, t in enumerate(temps) if t is not None]
    if not usable:
        return []
    below = [i for i in usable if temps[i] <= target]
    lo = below[-1] if below else usable[0]
    after = [i for i in usable if i > lo]
    hi = after[0] if after else lo
    if lo == hi:
        earlier = [i for i in usable if i < hi]
        lo = earlier[-1] if earlier else hi
    return [lo, hi] if lo != hi else [lo]


def _curve_limit(curve: float, proposed: float) -> str:
    if proposed < CURVE_MIN:
        return (
            "Kurvan står redan på %s, och nästa steg nedåt är 0 — vilket inte är en "
            "flackare kurva utan byter pumpen till egen kurva med helt andra "
            "punkter. Sänk hellre offset, eller justera egen kurva medvetet från "
            "pumpens display." % sv_number(curve, None)
        )
    return ("Kurvan står redan på %s, som är den brantaste pumpen har."
            % sv_number(curve, None))


def _fmt(v) -> str:
    """A number in a Swedish sentence. Decimal comma, real minus sign."""
    return sv_number(v, None)


def _history_hours(store) -> float:
    """How long the history reaches back, in hours.

    store.span(), not store.stats(): this is on the path of every /api/heating,
    /api/advice and /api/autotune request, and the count of rows that stats()
    used to fetch alongside the two timestamps cost a full table scan under the
    store's lock -- with the poller waiting behind it.
    """
    if hasattr(store, "span"):
        first, last = store.span()
    else:                                        # a store from an older build
        stats = store.stats()
        first, last = stats.get("first"), stats.get("last")
    if not first or not last:
        return 0.0
    return max(0.0, (last - first) / 3600.0)


def _duration(hours: float) -> str:
    if hours < 1:
        minutes = max(1, int(hours * 60))
        return "%d minut%s" % (minutes, "er" if minutes != 1 else "")
    if hours < 48:
        whole = int(hours)
        return "%d timm%s" % (whole, "ar" if whole != 1 else "e")
    days = int(hours / 24)
    return "%d dyg%s" % (days, "n" if days != 1 else "n")
