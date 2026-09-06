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
R_ROOM_SETPOINT = 40207
R_ROOM_TEMP = 30117
R_OUTDOOR = 30002
R_SUPPLY = 30006
R_RETURN = 30008
R_DEGREE_MINUTES = 40012
R_ADD_HEAT_POWER = 31028
R_ROOM_SENSOR_SYSTEM = 40052

READ = [R_CURVE, R_OFFSET, R_MIN_SUPPLY, R_MAX_SUPPLY, R_ROOM_SETPOINT, R_ROOM_TEMP,
        R_OUTDOOR, R_SUPPLY, R_RETURN, R_DEGREE_MINUTES, R_ADD_HEAT_POWER,
        R_ROOM_SENSOR_SYSTEM] + R_OWN_CURVE

# Roughly how much one step of offset moves the indoor temperature. NIBE's own
# documentation and the installer rule of thumb both land near 1 degree; it is
# a starting point for one iteration, not a calibrated model.
DEGREES_PER_OFFSET_STEP = 1.0


@dataclass
class Suggestion:
    address: int
    title: str
    current: float
    proposed: float
    unit: str = ""
    why: str = ""
    confirm_required: bool = True

    def as_dict(self) -> dict:
        return {
            "address": self.address,
            "title": self.title,
            "current": self.current,
            "proposed": self.proposed,
            "unit": self.unit,
            "why": self.why,
            "confirm_required": self.confirm_required,
        }


@dataclass
class Advice:
    observations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    suggestions: list[Suggestion] = field(default_factory=list)
    blocked: str = ""

    def as_dict(self) -> dict:
        return {
            "observations": self.observations,
            "warnings": self.warnings,
            "suggestions": [s.as_dict() for s in self.suggestions],
            "blocked": self.blocked,
        }


def _value(data: dict, address: int):
    row = data.get(address) or {}
    v = row.get("value")
    return v if isinstance(v, (int, float)) else None


def diagnose(pump, store=None) -> dict:
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
        "own_curve": [_value(data, a) for a in R_OWN_CURVE],
    }
    out["has_room_sensor"] = out["room_temp"] is not None
    out["uses_own_curve"] = out["curve"] == 0

    notes: list[str] = []
    warnings: list[str] = []

    if out["uses_own_curve"]:
        points = [p for p in out["own_curve"] if p is not None]
        notes.append(
            "Kurvan står på 0, vilket betyder egen kurva: pumpen följer dina egna "
            "punkter (%s °C) i stället för en av de numrerade kurvorna."
            % ", ".join("%g" % p for p in points)
        )
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

    supply, max_supply = out["supply"], out["max_supply"]
    if supply is not None and max_supply is not None and supply >= max_supply - 1:
        warnings.append(
            "Framledningen (%g °C) ligger i taket för max framledning (%g °C). "
            "Att höja värmen mer får ingen effekt förrän taket höjs."
            % (supply, max_supply)
        )

    if out["additional_heat_kw"]:
        warnings.append(
            "Elpatronen går just nu (%g kW). Höj inte värmen förrän du vet varför — "
            "annars betalar du för direktverkande el." % out["additional_heat_kw"]
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


def advise(pump, feeling: str, when: str, store=None) -> Advice:
    """Turn "too cold, all the time" into one concrete register change.

    feeling: "colder" | "warmer"  -- what the person wants
    when:    "always" | "cold_outside" | "mild_outside"
    """
    state = diagnose(pump, store)
    advice = Advice(observations=list(state["observations"]),
                    warnings=list(state["warnings"]))

    if feeling not in ("warmer", "colder"):
        raise ValueError("feeling must be 'warmer' or 'colder'")
    if when not in ("always", "cold_outside", "mild_outside"):
        raise ValueError("when must be 'always', 'cold_outside' or 'mild_outside'")

    direction = 1 if feeling == "warmer" else -1

    # Raising heat while the immersion heater already runs is the one case where
    # the honest answer is "not until you know why".
    if direction > 0 and state.get("additional_heat_kw"):
        advice.blocked = (
            "Elpatronen går just nu. Att höja värmen då gör tillskottet större, "
            "inte värmepumpen effektivare. Ta reda på varför den går först — "
            "vanligast är varmvattenladdning eller att kurvan redan ligger för högt "
            "för utetemperaturen."
        )
        return advice

    if when == "always":
        offset = state["offset"]
        if offset is None:
            advice.blocked = "Kunde inte läsa värmeoffset (register 40031)."
            return advice
        advice.suggestions.append(Suggestion(
            address=R_OFFSET,
            title="Värmeoffset",
            current=offset,
            proposed=offset + direction,
            why="Huset känns %s i alla väder, och då är det hela kurvan som ligger "
                "fel — inte dess lutning. Offset flyttar hela kurvan. Ett steg "
                "motsvarar ungefär %g grad inomhus."
                % ("för kallt" if direction > 0 else "för varmt", DEGREES_PER_OFFSET_STEP),
        ))
        return advice

    # Wrong only in some weather = the curve's shape, not its height.
    cold_end = when == "cold_outside"
    if state["uses_own_curve"]:
        # P1 is the coldest point, P7 the mildest.
        idx = [0, 1] if cold_end else [5, 6]
        ceiling = state["max_supply"]
        floor = state["min_supply"]
        capped = False
        for i in idx:
            current = state["own_curve"][i]
            if current is None:
                continue
            proposed = current + direction * 2
            # A curve point above max supply is a setting the pump will not act
            # on: the supply temperature is capped elsewhere. Suggesting it would
            # look like it worked and change nothing.
            if ceiling is not None and proposed > ceiling:
                capped = True
                continue
            if floor is not None and proposed < floor:
                capped = True
                continue
            advice.suggestions.append(Suggestion(
                address=R_OWN_CURVE[i],
                title="Egen kurva, punkt P%d" % (i + 1),
                current=current,
                proposed=proposed,
                unit="°C",
                why="Du kör egen kurva, så det är punkterna som formar den. P%d är "
                    "%s änden. Två grader framledning där ändrar hur mycket värme "
                    "huset får %s, utan att röra resten av kurvan."
                    % (i + 1, "den kalla" if cold_end else "den milda",
                       "när det är kallt ute" if cold_end else "i milt väder"),
            ))
        if capped:
            advice.warnings.append(
                "Minst en punkt ligger redan mot gränsen för framledning "
                "(%s–%s °C). Att höja den längre gör ingenting förrän max "
                "framledning (register 40039) höjs — och det är en inställning "
                "som beror på vad ditt värmesystem tål, inte något appen bör "
                "gissa åt dig." % (_fmt(floor), _fmt(ceiling))
            )
        if not advice.suggestions and not advice.blocked:
            advice.blocked = (
                "Ingen av punkterna i den änden går att flytta utan att passera "
                "gränserna för framledning."
            )
        return advice

    curve = state["curve"]
    if curve is None:
        advice.blocked = "Kunde inte läsa värmekurvan (register 40027)."
        return advice

    if cold_end:
        advice.suggestions.append(Suggestion(
            address=R_CURVE,
            title="Värmekurva",
            current=curve,
            proposed=curve + direction,
            why="Det stämmer bara när det är kallt ute, alltså är det kurvans "
                "lutning som är fel, inte dess höjd. En brantare kurva ger mer "
                "värme ju kallare det blir och lämnar det milda vädret ifred.",
        ))
    else:
        # Mild-weather-only error: flatten the curve, and hold the cold end where
        # it was by moving the offset the other way. Two changes that cancel out
        # in cold weather -- which is exactly the point.
        offset = state["offset"]
        advice.suggestions.append(Suggestion(
            address=R_CURVE,
            title="Värmekurva",
            current=curve,
            proposed=curve - direction,
            why="Det stämmer bara i milt väder. En flackare kurva ger mindre "
                "skillnad mellan milt och kallt.",
        ))
        if offset is not None:
            advice.suggestions.append(Suggestion(
                address=R_OFFSET,
                title="Värmeoffset",
                current=offset,
                proposed=offset + direction,
                why="Och offset åt andra hållet, så att kalla dagar hamnar där de "
                    "var. De två ändringarna tar ut varandra när det är kallt och "
                    "ger effekt bara i milt väder.",
            ))

    return advice


def _fmt(v) -> str:
    return "–" if v is None else ("%g" % v)


def _history_hours(store) -> float:
    stats = store.stats()
    if not stats.get("first") or not stats.get("last"):
        return 0.0
    return max(0.0, (stats["last"] - stats["first"]) / 3600.0)


def _duration(hours: float) -> str:
    if hours < 1:
        return "%d minuter" % int(hours * 60)
    if hours < 48:
        return "%d timmar" % int(hours)
    return "%d dygn" % int(hours / 24)
