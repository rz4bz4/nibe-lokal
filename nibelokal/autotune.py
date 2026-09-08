"""Long-horizon curve tuning: watch for weeks, then propose one small change.

`advisor.py` answers the immediate question -- "the house feels wrong *now*,
which knob?" -- from one reading plus the owner's own judgement. This module
answers the slow one: given weeks of history, is the curve actually wrong, and
in which of the two possible ways?

There are only two ways, and telling them apart is the entire job:

    level  -- the house is equally wrong in all weather. One offset step
              (register 40031) shifts the whole curve. This is the easy case.
    slope  -- the house is right at +5 C and cold at -5 C. An offset does not
              fix this; it just moves the error from the cold days to the mild
              ones. The curve has to get steeper, which on this pump means
              moving an own-curve P-point.

Reaching for the offset when the slope is wrong is the classic mistake, and the
reason people spend a whole winter chasing their own tail. So this module fits
both terms and reports each with how well it is determined -- and refuses to
name a slope at all when the weather has not varied enough for the number to
mean anything. With eight readings spread over three degrees of outdoor
temperature the slope is not "approximately zero"; it is unknown, and saying
zero would be a lie with a decimal point on it.

Three design choices are worth stating up front, because they are what make the
output trustworthy rather than merely confident:

* **Nights only.** There is no irradiance sensor, no window contact and no
  occupancy signal. During the day the house is heated by the sun, the oven and
  the people in it, and none of that is in the history. At night it is heated by
  the pump. Only night samples are evidence about the curve.
* **One point per night.** The history arrives already averaged into hourly
  buckets (`store.autotune_history`), which is still eight rows a night that
  are all the same measurement: the house has a thermal time constant of hours
  to days, so consecutive samples carry almost no new information. Fitting on
  them as if they were independent produces standard errors that are wrong by
  an order of magnitude and a module that is sure of everything. Each night
  collapses to one point, whatever resolution it arrived at.
* **Advisory only.** This module never writes. It returns a proposal for a human
  to approve, one step at a time, with how long to wait before judging it.

Register numbers, curve limits and the own-curve geometry are imported from
`advisor` rather than restated here, so the two cannot quietly drift apart.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from . import sv_number
from .advisor import (
    CURVE_MAX,
    CURVE_MIN,
    DEFAULT_EMITTERS,
    OFFSET_MAX,
    OFFSET_MIN,
    OWN_CURVE_OUTDOOR,
    R_CURVE,
    R_MAX_SUPPLY,
    R_MIN_SUPPLY,
    R_OFFSET,
    R_OWN_CURVE,
    curve_at,
    emitters,
)

# --- what counts as evidence -------------------------------------------------

# NIBE's default heating stop in auto mode is 17 C, and above it the pump stops
# asking for heat at all. A house sitting at 23 C in July says nothing about the
# curve -- it is the sun's doing, and no curve change would have prevented it.
# Rule 5 in one constant.
HEATING_STOP_C = 17.0

# Night window, local time, [start, end). Sun-up hours are excluded entirely:
# solar gain through a south window is worth several degrees indoors and is not
# in the history, so daytime samples cannot be separated from it. 22-06 also
# skips the evening cooking and TV load, which is the same problem in miniature.
NIGHT_HOURS = (22, 6)

# A night needs this many samples before its mean means anything. At a 60 s poll
# it is nothing; the guard is against a night with two stray readings from a
# poller that was restarting, which would otherwise weigh as much as a full one.
MIN_SAMPLES_PER_NIGHT = 3

# Sensor sanity. Values outside these are a broken sensor or a unit mix-up, and
# one -40 C indoor reading would drag a whole night's mean off the map.
INDOOR_RANGE_C = (5.0, 35.0)
OUTDOOR_RANGE_C = (-45.0, 45.0)

# --- how much evidence is enough ---------------------------------------------

# Nights, not samples. Three nights is the floor for saying anything at all: the
# slab downstairs has a time constant near a day, so two nights are barely two
# independent observations, and one cold snap that has not finished propagating
# through the floor looks exactly like a curve that is too flat.
MIN_NIGHTS_OFFSET = 3

# A slope needs more than a level does, for two separate reasons: it needs the
# weather to have moved (see the span below), and it needs enough points that
# the fit's own standard error is small compared with the effect. Seven nights
# is one turn of the weather -- less than that and every point is the same
# air mass.
MIN_NIGHTS_SLOPE = 7

# Outdoor spread of the night means, in degrees. Below 5 C of spread a level
# error and a slope error are numerically the same thing: a slope of 0.1 C/C --
# large enough to matter over a winter -- shows up as 0.5 C of spread, which is
# inside the noise of an averaged indoor temperature. So under 5 C we do not
# claim a level either; we say we do not know yet.
SPAN_MIN_OFFSET_C = 5.0

# For the slope itself, 12 C. The arithmetic: with n nights spread over a span S
# the standard error of the slope is roughly sigma / (S * sqrt(n/12)). With a
# realistic sigma of 0.3 C and 10 nights, S = 12 gives about 0.03 C/C, so a real
# slope error of 0.05 C/C (which is 1.5 C indoors across a 30 C winter) clears
# two standard errors. At S = 3 the same maths gives 0.12 C/C and nothing is
# ever distinguishable from nothing.
SPAN_MIN_SLOPE_C = 12.0

# Residual scatter above this means the indoor temperature is being driven by
# something that is not the outdoor temperature -- an open window, a wood stove,
# a visitor, a stale indoor feed. One offset step is worth about 1 C indoors, so
# once the unexplained scatter approaches that, a proposal is tuning noise.
MAX_RESIDUAL_SD_C = 0.8

# --- when a change is worth making -------------------------------------------

# Do not propose a step to fix less than this. One offset step is about 1 C
# indoors, so correcting 0.3 C with it overshoots by more than it fixes -- and
# 0.3 C is below what a house average from a handful of room sensors resolves.
DEADBAND_C = 0.4

# A slope smaller than this is not worth a proposal even when it is statistically
# real: 0.03 C/C over the 30 C between a mild autumn day and a cold winter night
# is 0.9 C indoors, about one offset step, which is the finest tool available.
SLOPE_MIN_C_PER_C = 0.03

# When a slope is real, how much of the total error must it explain before it
# wins over the level term? At 0.5 a slope contributing half as much as the
# level already takes priority -- deliberately biased towards the slope, because
# an offset applied to a slope error is the failure this module exists to
# prevent, while a slope tweak on a mostly-level error is merely slower.
SLOPE_DOMINANCE = 0.5

# NIBE documents one offset step as about 2.5 C of supply temperature and about
# 1 C indoors, "depending on your heating system". Both numbers below come from
# there: 2.5 sizes a curve-point move (the conservative direction -- a larger
# degrees-per-step asks for a smaller move), and 1 C indoors per offset step
# decides whether a step is warranted at all.
#
# An earlier six-minute measurement on one pump suggested 1.5 C of supply per
# step and is deliberately not used: the appendix in docs/registers.md records
# that the run was stopped while the value was still ramping, which makes 1.5 a
# lower bound rather than a measurement. Nothing here is calibrated to one
# house -- strangers run this against their own pumps.
SUPPLY_C_PER_INDOOR_C = 2.5
INDOOR_C_PER_OFFSET_STEP = 1.0

# Largest curve-point move proposed in one go, in degrees of supply. Two degrees
# of supply is roughly 0.8 C indoors in that weather -- a change you can feel,
# still small enough that overshooting it costs a couple of days, not a winter.
POINT_STEP_MAX_C = 2.0

# Confidence is capped below 1.0 on principle: the pump has no room sensor
# (40203 = 0), so every indoor number here came from somewhere else, through a
# clock that may not agree with the pump's. Certainty is not available.
CONFIDENCE_CAP = 0.9

# Two-sided 95 % t values. dof is small on purpose -- one point per night means
# a fortnight of data is twelve degrees of freedom, and using 1.96 there would
# overstate the evidence by a third.
_T95 = {1: 12.71, 2: 4.30, 3: 3.18, 4: 2.78, 5: 2.57, 6: 2.45, 7: 2.36, 8: 2.31,
        9: 2.26, 10: 2.23, 11: 2.20, 12: 2.18, 13: 2.16, 14: 2.14, 15: 2.13,
        16: 2.12, 17: 2.11, 18: 2.10, 19: 2.09, 20: 2.09, 21: 2.08, 22: 2.07,
        23: 2.07, 24: 2.06, 25: 2.06, 26: 2.06, 27: 2.05, 28: 2.05, 29: 2.05,
        30: 2.04, 40: 2.02, 60: 2.00, 120: 1.98}

# Strings a pump uses for "the compressor is busy with something that is not
# heating". Matched case-insensitively as substrings, because the wording
# differs between firmware versions and languages.
_NOT_HEATING = ("hot water", "varmvatten", "hetvatten", "pool", "cooling", "kyla")


@dataclass
class Sample:
    """One poll of the pump, joined with an indoor temperature.

    `indoor` is an argument rather than something this module fetches: there is
    no room sensor on the pump (40203 = 0), the indoor feed lives in another
    module that may be absent or stale, and a missing indoor temperature must
    degrade into "not enough data" rather than an import error. A caller that
    has an indoor source joins it in itself -- the house average at the time of
    the poll, and None for any reading it considers stale, since a frozen number
    repeated for six hours would otherwise read as six hours of steady house.
    """

    ts: float
    outdoor: float = None
    indoor: float = None
    supply: float = None
    degree_minutes: float = None
    compressor: object = None


@dataclass
class Fit:
    """Least-squares fit of indoor error against outdoor temperature.

    `level` is the mean error at the mean outdoor temperature, not the intercept
    at 0 C: centring makes the two terms independent, so a badly determined
    slope cannot drag the level around with it.
    """

    n: int = 0
    level: float = None            # C, indoor minus target; negative = too cold
    level_se: float = None
    slope: float = None            # C of indoor error per C outdoors
    slope_se: float = None
    residual_sd: float = None
    mean_outdoor: float = None
    span: float = 0.0

    def slope_is_real(self) -> bool:
        """True when the slope clears both its own noise and the deadband."""
        if self.slope is None or self.slope_se is None:
            return False
        if abs(self.slope) < SLOPE_MIN_C_PER_C:
            return False
        return abs(self.slope) > _t95(self.n - 2) * self.slope_se


@dataclass
class Night:
    key: str
    outdoor: float
    indoor: float
    error: float
    samples: int
    ts: float


@dataclass
class _Result:
    """Mutable working copy of the returned dict; see analyse() for the shape."""

    ok: bool = False
    state: str = "insufficient"
    reason_sv: str = ""
    samples: int = 0
    outdoor_span: list = field(default_factory=lambda: [None, None])
    days: float = 0.0
    offset_error_c: float = None
    slope_error_c_per_c: float = None
    proposal: dict = None
    confidence: float = 0.0
    missing_sv: list = field(default_factory=list)
    nights: int = 0
    rejected: dict = field(default_factory=dict)
    notes_sv: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "state": self.state,
            "reason_sv": self.reason_sv,
            "samples": self.samples,
            "outdoor_span": list(self.outdoor_span),
            "days": self.days,
            "offset_error_c": self.offset_error_c,
            "slope_error_c_per_c": self.slope_error_c_per_c,
            "proposal": self.proposal,
            "confidence": self.confidence,
            "missing_sv": list(self.missing_sv),
            # Beyond the agreed shape, and safe to ignore: nights is the count
            # that the statistics actually rest on, rejected says why samples
            # were dropped, notes_sv carries findings that are not proposals.
            "nights": self.nights,
            "rejected": dict(self.rejected),
            "notes_sv": list(self.notes_sv),
        }


# -- public API ---------------------------------------------------------------


def analyse(history, target_indoor, settings=None, emitter_kind: str = DEFAULT_EMITTERS,
            now: float = None, heating_stop_c: float = HEATING_STOP_C,
            night_hours=NIGHT_HOURS) -> dict:
    """Judge the heating curve from history, and propose at most one step.

    history:       iterable of Sample, of dicts with the Sample field names, or
                   of 6-sequences (ts, outdoor, indoor, supply, degree_minutes,
                   compressor). Anything unparseable is counted and skipped.
    target_indoor: the temperature the house is supposed to hold. Without it
                   there is no error to fit, so the answer is "insufficient".
    settings:      the pump's current heating settings, keyed as
                   advisor.diagnose() returns them ("offset", "curve",
                   "own_curve", "min_supply", "max_supply"). Without it the
                   analysis still runs, but no proposal can name a from-value.

    Returns a plain dict (see _Result.as_dict). Never raises: a caller in a web
    handler or a scheduled job gets a result saying what went wrong instead of a
    traceback, because an autotuner that crashes on a malformed history is worse
    than one that says it does not know.
    """
    try:
        return _analyse(history, target_indoor, settings, emitter_kind, now,
                        heating_stop_c, night_hours)
    except Exception as exc:                                   # noqa: BLE001
        res = _Result()
        res.reason_sv = ("Kunde inte analysera historiken (%s). Inget förslag "
                         "lämnas." % exc)
        res.missing_sv = ["Historiken gick inte att tolka."]
        return res.as_dict()


def usable_samples(history, heating_stop_c: float = HEATING_STOP_C,
                   night_hours=NIGHT_HOURS):
    """Split a history into samples that are evidence about the curve, and why not.

    Returns (samples, rejected) where rejected maps a short English reason to a
    count. The reasons are the whole argument of requirement 5 -- a sample is
    only evidence if the pump was the thing heating the house at the time.
    """
    kept = []
    rejected = {}

    def drop(reason):
        rejected[reason] = rejected.get(reason, 0) + 1

    try:
        rows = list(history or [])
    except TypeError:
        return [], {"unreadable": 1}

    start, end = _night_window(night_hours)
    for row in rows:
        s = _as_sample(row)
        if s is None:
            drop("unreadable")
            continue
        if s.outdoor is None:
            drop("no_outdoor")
            continue
        if s.indoor is None:
            # The common case when the separate indoor source is missing or
            # stale. Counted rather than silently ignored, so the caller can be
            # told that the pump is fine and the room data is not.
            drop("no_indoor")
            continue
        if not _in_range(s.indoor, INDOOR_RANGE_C) \
                or not _in_range(s.outdoor, OUTDOOR_RANGE_C):
            drop("implausible")
            continue
        if s.outdoor >= heating_stop_c:
            drop("no_heat_demand_outdoor")
            continue
        if _is_not_heating(s.compressor):
            drop("compressor_not_heating")
            continue
        if s.degree_minutes is not None and s.degree_minutes >= 0:
            # Degree minutes at or above zero means the pump is not asking for
            # heat: the house is coasting on stored warmth, so its temperature
            # is not a verdict on the curve.
            drop("no_demand_degree_minutes")
            continue
        if not _is_night(s.ts, start, end):
            drop("daytime")
            continue
        kept.append(s)
    return kept, rejected


def nightly_means(samples, night_hours=NIGHT_HOURS, target_indoor=None):
    """Collapse samples to one point per night.

    The honest unit of evidence. Two samples ten minutes apart are one
    measurement of a house whose time constant is hours; counting them as two
    is how a fit ends up claiming 40 degrees of freedom for a single weekend.
    """
    _, end = _night_window(night_hours)
    groups = {}
    for s in samples or []:
        key = _night_key(s.ts, end)
        groups.setdefault(key, []).append(s)
    nights = []
    for key in sorted(groups):
        rows = groups[key]
        if len(rows) < MIN_SAMPLES_PER_NIGHT:
            continue
        outdoor = sum(r.outdoor for r in rows) / len(rows)
        indoor = sum(r.indoor for r in rows) / len(rows)
        error = None if target_indoor is None else indoor - target_indoor
        nights.append(Night(key=key, outdoor=outdoor, indoor=indoor, error=error,
                            samples=len(rows), ts=min(r.ts for r in rows)))
    return nights


def fit(points) -> Fit:
    """Ordinary least squares of error against outdoor temperature, centred.

    Deliberately plain: with a dozen points and one predictor, anything more
    elaborate would add assumptions without adding information.
    """
    pts = [(float(x), float(y)) for x, y in (points or [])
           if x is not None and y is not None]
    n = len(pts)
    out = Fit(n=n)
    if n == 0:
        return out
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    out.span = max(xs) - min(xs)
    out.mean_outdoor = sum(xs) / n
    out.level = sum(ys) / n
    if n < 3:
        # Two points always fit a line exactly; the residuals would be zero and
        # every term perfectly determined, which is the opposite of the truth.
        return out
    mx, my = out.mean_outdoor, out.level
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        # Every night at the same outdoor temperature: the level is known, the
        # slope is not a small number, it is undefined.
        out.level_se = _sd(ys, my) / math.sqrt(n)
        return out
    sxy = sum((x - mx) * (y - my) for x, y in pts)
    slope = sxy / sxx
    sse = sum((y - (my + slope * (x - mx))) ** 2 for x, y in pts)
    dof = n - 2
    s2 = sse / dof
    out.slope = slope
    out.residual_sd = math.sqrt(s2)
    out.slope_se = math.sqrt(s2 / sxx)
    out.level_se = math.sqrt(s2 / n)
    return out


# -- the analysis itself ------------------------------------------------------


def _analyse(history, target_indoor, settings, emitter_kind, now, heating_stop_c,
             night_hours) -> dict:
    res = _Result()
    now = time.time() if now is None else float(now)
    em = emitters(emitter_kind)
    # From the emitter table, not from matching its Swedish prose: reading
    # "två dygn" to mean 48 made the advice depend on the wording of a
    # sentence, and a reworded sentence would have quietly halved the wait.
    wait_hours = int(em.get("wait_hours", 24))

    samples, rejected = usable_samples(history, heating_stop_c, night_hours)
    res.rejected = rejected
    res.samples = len(samples)

    if target_indoor is None:
        res.reason_sv = ("Ingen önskad inomhustemperatur angiven, så det finns "
                         "inget fel att mäta mot.")
        res.missing_sv = ["Önskad inomhustemperatur (målvärde)."]
        return res.as_dict()

    nights = nightly_means(samples, night_hours, float(target_indoor))
    res.nights = len(nights)
    if samples:
        stamps = [s.ts for s in samples]
        res.days = round((max(stamps) - min(stamps)) / 86400.0, 1)
    if nights:
        outs = [n.outdoor for n in nights]
        res.outdoor_span = [round(min(outs), 1), round(max(outs), 1)]

    f = fit([(n.outdoor, n.error) for n in nights])
    if f.level is not None:
        res.offset_error_c = round(f.level, 2)
    # A slope is reported as a number as soon as it is *identifiable*: enough
    # nights, over a wide enough spread of outdoor temperatures, that the fit
    # has something to fit. Whether it is also *real* -- big enough to clear
    # its own standard error and the deadband -- decides whether it is acted
    # on, further down, not whether it is shown. The distinction matters to
    # the reader: a measured slope of 0,02 C/C means "the curve's slope is
    # fine", and null means "the weather has not varied enough to say", which
    # is what the page prints under it. Reporting nothing in the first case
    # would turn a real result into a shrug.
    slope_identifiable = (len(nights) >= MIN_NIGHTS_SLOPE
                          and f.span >= SPAN_MIN_SLOPE_C
                          and f.slope_se is not None)
    if slope_identifiable and f.slope is not None:
        res.slope_error_c_per_c = round(f.slope, 3)

    res.missing_sv = _missing(res, f, nights, now, slope_identifiable, rejected,
                              _night_window(night_hours))

    # 1. Refuse to guess. Everything below this line assumes the gates passed.
    if len(nights) < MIN_NIGHTS_OFFSET or f.span < SPAN_MIN_OFFSET_C or f.level is None:
        res.state = "insufficient"
        res.reason_sv = _insufficient_reason(res, f, nights, rejected)
        res.confidence = 0.0
        return res.as_dict()

    # 2. Something other than the weather is moving the house around.
    if f.residual_sd is not None and f.residual_sd > MAX_RESIDUAL_SD_C:
        res.state = "uncertain"
        res.confidence = 0.2
        res.reason_sv = (
            "Inomhustemperaturen varierar %s °C mer än vad utetemperaturen "
            "förklarar. Något annat än kurvan styr huset — öppet fönster, "
            "braskamin, vädring eller en inomhusgivare som står fel. Ett "
            "kurvbyte skulle justera bort brus, inte ett fel."
            % _sv(f.residual_sd))
        return res.as_dict()

    slope_real = slope_identifiable and f.slope_is_real()
    if slope_real:
        res.slope_error_c_per_c = round(f.slope, 3)
    half_span = f.span / 2.0
    slope_effect = abs(f.slope) * half_span if slope_real else 0.0
    level_effect = abs(f.level)

    # 3. Slope first when it is real and material: an offset applied to a slope
    #    error moves the error to the other end of the winter instead of
    #    removing it, which is the mistake this module exists to catch.
    if slope_real and slope_effect >= SLOPE_DOMINANCE * level_effect \
            and slope_effect >= DEADBAND_C:
        return _slope_answer(res, f, nights, settings, em, wait_hours).as_dict()

    if level_effect >= DEADBAND_C:
        return _offset_answer(res, f, settings, em, wait_hours,
                              slope_identifiable).as_dict()

    # 4. The house is where it should be. This is a result, not a failure, and
    #    it is the one answer people never get from a heating forum.
    res.state = "confident"
    res.ok = True
    # Here the conclusion is "no change", so what makes it credible is that the
    # error is small compared with the deadband -- not that it is far from zero.
    res.confidence = _confidence(f, len(nights), MIN_NIGHTS_OFFSET, SPAN_MIN_OFFSET_C,
                                 signal=_clamp(1.0 - abs(f.level) / DEADBAND_C, 0.0, 1.0))
    res.reason_sv = (
        "Huset ligger i snitt %s °C från börvärdet över %d nätter och från %s "
        "till %s °C ute, vilket är inom mätnoggrannheten. Kurvan behöver inget. "
        "Minsta meningsfulla ändring är ett offsetsteg, och det är ungefär "
        "1 °C — mer än felet."
        % (_sv(f.level, 1, sign=True), len(nights),
           _sv(res.outdoor_span[0], 0), _sv(res.outdoor_span[1], 0)))
    return res.as_dict()


def _offset_answer(res, f, settings, em, wait_hours, slope_identifiable):
    """A level error: one step on 40031, in the direction of the error."""
    res.state = "confident" if slope_identifiable else "uncertain"
    res.ok = res.state == "confident"
    res.confidence = _confidence(f, res.nights, MIN_NIGHTS_OFFSET, SPAN_MIN_OFFSET_C,
                                 signal=_signal(f.level, f.level_se))
    too_cold = f.level < 0
    word = "kallt" if too_cold else "varmt"
    res.reason_sv = (
        "Huset är i snitt %s °C för %s, och lika mycket i hela det uppmätta "
        "vädret (%s till %s °C ute över %d nätter). Ett nivåfel, inte ett "
        "lutningsfel — då är offset rätt reglage."
        % (_sv(abs(f.level)), word, _sv(res.outdoor_span[0], 0),
           _sv(res.outdoor_span[1], 0), res.nights))
    if not slope_identifiable:
        # Honest hedge: without weather variation we cannot rule out that this
        # "level" error is one end of a slope error we have not seen yet.
        res.reason_sv += (" Lutningen går ännu inte att avgöra, så det här är "
                          "det bästa svaret men inte det säkra.")

    offset = _num((settings or {}).get("offset"))
    if offset is None:
        res.notes_sv.append(
            "Kunde inte läsa nuvarande värmeoffset (register 40031), så inget "
            "konkret förslag lämnas. Felet ovan står sig ändå.")
        return res

    step = 1 if too_cold else -1
    target = offset + step
    if not OFFSET_MIN <= target <= OFFSET_MAX:
        res.proposal = None
        res.notes_sv.append(
            # "(-10..10)" was a range written for a programmer: a hyphen where
            # the rest of the page has a minus sign, and two dots where Swedish
            # prose says "till".
            "Offset står redan på %s, som är gränsen (%s till %s). Behöver huset "
            "mer än så är det kurvan som ligger fel, inte offset — och den "
            "ändringen är för stor för att föreslås automatiskt."
            % (_sv(_reg(offset), None), _sv(OFFSET_MIN, None), _sv(OFFSET_MAX, None)))
        return res

    steps_needed = abs(f.level) / INDOOR_C_PER_OFFSET_STEP
    more = ""
    if steps_needed >= 1.8:
        # Say it out loud rather than proposing two steps: the second step is
        # only justified once the first has landed, and how much a step is worth
        # in this particular house is exactly what the first step measures.
        more = (" Felet motsvarar ungefär %s steg, men bara ett föreslås: hur "
                "mycket ett steg är värt i just det här huset vet vi först när "
                "det första har satt sig." % _sv(steps_needed, 0))
    res.proposal = {
        "register": R_OFFSET,
        "from": _reg(offset),
        "to": _reg(target),
        "why_sv": ("Ett steg %s på värmeoffset flyttar hela kurvan parallellt, "
                   "vilket är precis vad ett fel som är lika stort i alla väder "
                   "behöver.%s" % ("upp" if step > 0 else "ner", more)),
        "expected_sv": ("Framledningen ändras ungefär 2,5 °C vid alla "
                        "utetemperaturer enligt NIBE:s dokumentation, och "
                        "inomhus ungefär %s °C. Beräknad framledning "
                        "(31018) rampar under cirka fem minuter efter "
                        "ändringen — läser du av den direkt mäter du rampen, "
                        "inte inställningen. Döm om huset efter %d timmar med "
                        "%s." % ("+1" if step > 0 else "-1", wait_hours, em["name"])),
        "wait_hours": wait_hours,
    }
    return res


def _slope_answer(res, f, nights, settings, em, wait_hours):
    """A slope error: the curve's shape, not its height.

    On this pump the curve is register 40027 = 0, "own curve", so the shape is
    the seven P-points. The proposal has to name the point that actually governs
    the weather the error was measured in -- P1 sits at -30 C, which this house
    may never see, so moving it would be a change with no effect and a false
    sense of progress.
    """
    res.state = "confident"
    res.ok = True
    res.confidence = _confidence(f, len(nights), MIN_NIGHTS_SLOPE, SPAN_MIN_SLOPE_C,
                                 signal=_signal(f.slope, f.slope_se))

    outs = [n.outdoor for n in nights]
    lo, hi = min(outs), max(outs)
    err_lo = f.level + f.slope * (lo - f.mean_outdoor)
    err_hi = f.level + f.slope * (hi - f.mean_outdoor)
    # The end of the observed range where the house is furthest from target.
    if abs(err_lo) >= abs(err_hi):
        worst_x, worst_err, other_x, other_err = lo, err_lo, hi, err_hi
    else:
        worst_x, worst_err, other_x, other_err = hi, err_hi, lo, err_lo

    res.reason_sv = (
        "Felet följer utetemperaturen: %s °C vid %s °C ute och %s °C vid %s °C "
        "ute (%s °C per grad ute, över %d nätter). Det är kurvans lutning som är "
        "fel, inte dess höjd. Offset skulle bara flytta felet till andra änden "
        "av vintern."
        % (_sv(err_lo, 1, sign=True), _sv(lo, 0), _sv(err_hi, 1, sign=True),
           _sv(hi, 0), _sv(f.slope, 3), len(nights)))

    if abs(other_err) >= DEADBAND_C:
        res.notes_sv.append(
            "Huset ligger %s °C fel även vid %s °C ute. Ta lutningen först, "
            "mät om, och rör offset först därefter — annars går det inte att se "
            "vilken ändring som gjorde vad."
            % (_sv(other_err, 1, sign=True), _sv(other_x, 0)))

    settings = settings or {}
    curve = _num(settings.get("curve"))
    points = settings.get("own_curve")
    # The outdoor temperature each point governs, as advisor.diagnose() read it
    # out of the pump's profile. Not a constant: on the F generation the
    # seventh point's temperature is not established and arrives as None, and
    # a point whose weather is unknown cannot be the point that governs the
    # weather where the error is worst. Falls back to the S-series list for a
    # settings dict from an older caller. See profile.OWN_CURVE_OUTDOOR.
    temps = settings.get("own_curve_outdoor") or list(OWN_CURVE_OUTDOOR)
    min_supply = _num(settings.get("min_supply"))
    max_supply = _num(settings.get("max_supply"))

    if curve is None:
        res.notes_sv.append(
            "Kunde inte läsa värmekurvan (register 40027), så inget konkret "
            "förslag lämnas.")
        return res

    if curve != 0:
        # A numbered curve. One step of 40027 changes the cold end and barely
        # touches the mild one, so it only answers a cold-end error. A mild-end
        # error needs curve and offset moved against each other, which is two
        # changes at once and therefore advisor's job, not an autotuner's.
        if worst_x > other_x:
            res.notes_sv.append(
                "Felet är störst i milt väder och pumpen kör en numrerad kurva "
                "(%s). Det kräver att kurvan flackas och offset höjs samtidigt, "
                "alltså två ändringar på en gång — det föreslås inte härifrån."
                % _sv(_reg(curve), None))
            return res
        step = 1 if worst_err < 0 else -1
        target = curve + step
        if not CURVE_MIN <= target <= CURVE_MAX:
            res.notes_sv.append(
                "Kurvan står på %s och nästa steg åt rätt håll ligger utanför "
                "1–15. Ändringen får göras på pumpens display, medvetet."
                % _sv(_reg(curve), None))
            return res
        res.proposal = {
            "register": R_CURVE,
            "from": _reg(curve),
            "to": _reg(target),
            "why_sv": ("En %s kurva ändrar framledningen mest i kallt väder och "
                       "minst i milt, vilket är formen på felet ovan."
                       % ("brantare" if step > 0 else "flackare")),
            "expected_sv": ("Ett kurvsteg är litet i milt väder och växer när det "
                            "blir kallare. Döm om huset efter %d timmar med %s, "
                            "och helst en dag som liknar %s °C ute."
                            % (wait_hours, em["name"], _sv(worst_x, 0))),
            "wait_hours": wait_hours,
        }
        return res

    if not isinstance(points, (list, tuple)) or len(points) != len(temps):
        res.notes_sv.append(
            "Pumpen kör egen kurva (40027 = 0) men punkterna gick inte att läsa, "
            "så det går inte att säga vilken punkt som ska flyttas.")
        return res

    idx = _point_for(worst_x, points, temps)
    if idx is None:
        res.notes_sv.append(
            "Ingen läsbar kurvpunkt ligger nära %s °C ute, där felet är störst."
            % _sv(worst_x, 0))
        return res

    current = _num(points[idx])
    # Indoor degrees to supply degrees. 2.5 is NIBE's documented figure for one
    # offset step; using the larger of the two known values makes the proposed
    # move the smaller one, which is the direction to be wrong in.
    want = abs(worst_err) * SUPPLY_C_PER_INDOOR_C
    delta = max(1.0, min(POINT_STEP_MAX_C, round(want)))
    direction = 1.0 if worst_err < 0 else -1.0
    target = current + direction * delta
    # P-points are supply temperatures, and the pump clamps them to min/max
    # supply anyway; proposing a value it will refuse or silently truncate is
    # worse than proposing nothing.
    if max_supply is not None:
        target = min(target, max_supply)
    if min_supply is not None:
        target = max(target, min_supply)
    if abs(target - current) < 0.5:
        limit = R_MAX_SUPPLY if direction > 0 else R_MIN_SUPPLY
        res.notes_sv.append(
            "Punkten P%d ligger redan mot %s framledning (%s °C, register %d), "
            "så den går inte att flytta åt det hållet. Det är den gränsen som "
            "måste ändras först, och hur högt eller lågt den får ligga beror på "
            "vad värmesystemet tål."
            % (idx + 1, "max" if direction > 0 else "min",
               _fmt(max_supply if direction > 0 else min_supply), limit))
        return res

    before = curve_at(list(points), worst_x, temps)
    after_points = [target if j == idx else p for j, p in enumerate(points)]
    after = curve_at(after_points, worst_x, temps)
    effect = ""
    if before is not None and after is not None:
        effect = (" Beräknad framledning vid %s °C ute går från %s till %s °C."
                  % (_sv(worst_x, 0), _sv(before), _sv(after)))

    res.proposal = {
        "register": R_OWN_CURVE[idx],
        "from": current,
        "to": round(target, 1),
        "why_sv": ("Du kör egen kurva, så lutningen sitter i punkterna. P%d är "
                   "punkten för %s °C ute, den som styr vädret där felet är "
                   "störst (%s °C ute). Att flytta den ändrar kurvan just där "
                   "och lämnar den andra änden i fred — vilket offset inte gör."
                   % (idx + 1, _sv(temps[idx], None, sign=True),
                      _sv(worst_x, 0))),
        "expected_sv": ("%s °C framledning motsvarar ungefär %s °C inomhus i det "
                        "vädret.%s Beräknad framledning (31018) rampar under "
                        "cirka fem minuter efter ändringen. Döm om huset efter "
                        "%d timmar med %s, och först en dag som liknar %s °C "
                        "ute — annars mäter du vädret, inte ändringen."
                        % (_sv(delta, 0), _sv(delta / SUPPLY_C_PER_INDOOR_C), effect,
                           wait_hours, em["name"], _sv(worst_x, 0))),
        "wait_hours": wait_hours,
    }
    return res


# -- what is still missing ----------------------------------------------------


def _missing(res, f, nights, now, slope_identifiable, rejected, window) -> list:
    """What evidence is lacking, and when it will plausibly exist.

    "Not enough data" without a number and a date is an excuse. With them it is
    a plan, and the owner can decide whether to wait or to go and feel the
    radiators themselves.
    """
    out = []
    if not nights:
        if rejected.get("no_indoor"):
            out.append("Inomhustemperatur saknas i %d avläsningar — pumpen har ingen "
                       "rumsgivare, så den måste komma från annat håll."
                       % rejected["no_indoor"])
        if rejected.get("no_heat_demand_outdoor"):
            out.append("Alla avläsningar är från väder över värmestoppet (%s °C). "
                       "Kurvan går inte att bedöma på sommaren; kom tillbaka när "
                       "uppvärmningssäsongen börjat." % _sv(HEATING_STOP_C, 0))
        if rejected.get("daytime"):
            out.append("Bara dagtidsavläsningar. Solinstrålning och matlagning värmer "
                       "huset lika mycket som pumpen, så bara nätter (%02d–%02d) "
                       "räknas som bevis." % window)
        if not out:
            out.append("Inga användbara avläsningar med värmebehov ännu.")
        return out

    if len(nights) < MIN_NIGHTS_OFFSET:
        out.append("Minst %d nätter med värmebehov behövs för att säga något alls; "
                   "det finns %d. Tidigast omkring %s."
                   % (MIN_NIGHTS_OFFSET, len(nights),
                      _eta(now, MIN_NIGHTS_OFFSET - len(nights))))
    if f.span < SPAN_MIN_OFFSET_C:
        out.append("Utetemperaturen har bara varierat %s °C mellan nätterna; minst "
                   "%s °C behövs för att skilja ett nivåfel från ett lutningsfel. "
                   "Det avgörs av vädret och går inte att skynda på."
                   % (_sv(f.span), _sv(SPAN_MIN_OFFSET_C, 0)))
    if not slope_identifiable:
        why = []
        if len(nights) < MIN_NIGHTS_SLOPE:
            why.append("%d nätter av %d" % (len(nights), MIN_NIGHTS_SLOPE))
        if f.span < SPAN_MIN_SLOPE_C:
            why.append("%s °C spridning ute av %s"
                       % (_sv(f.span), _sv(SPAN_MIN_SLOPE_C, 0)))
        out.append("Kurvans lutning går inte att avgöra ännu (%s), så ingen siffra "
                   "redovisas för den. Med så lite variation skulle en lutning vara "
                   "gissad, inte mätt." % "; ".join(why or ["för lite spridning"]))
    return out


def _insufficient_reason(res, f, nights, rejected) -> str:
    if not nights:
        return ("Inga nätter med värmebehov i historiken (%d avläsningar granskade, "
                "%d användbara). Utan mätningar där pumpen faktiskt värmde huset "
                "finns det ingenting att dra slutsatser av."
                % (sum(rejected.values()) + res.samples, res.samples))
    if len(nights) < MIN_NIGHTS_OFFSET:
        return ("Bara %d %s med värmebehov. Golv och stomme svarar över dygn, så "
                "färre än %d nätter är färre än %d oberoende mätningar av huset."
                % (len(nights), "natt" if len(nights) == 1 else "nätter",
                   MIN_NIGHTS_OFFSET, MIN_NIGHTS_OFFSET))
    return ("Utetemperaturen har bara varierat %s °C (%s till %s °C) över %d "
            "nätter. I så smalt väder ser ett nivåfel och ett lutningsfel likadana "
            "ut, och att gissa vilket det är kan göra huset sämre i det väder som "
            "inte mätts."
            % (_sv(f.span), _sv(res.outdoor_span[0], 0), _sv(res.outdoor_span[1], 0),
               len(nights)))


def _signal(term, term_se) -> float:
    """How far an estimate stands out from its own noise, as 0..1.

    Six standard errors counts as full marks. That is a high bar on purpose:
    two standard errors is the threshold for believing a term exists at all,
    not for being sure enough to change a house by it.
    """
    if term is None:
        return 0.0
    if not term_se or term_se <= 0:
        return 0.5
    return _clamp(abs(term) / (6.0 * term_se), 0.0, 1.0)


def _confidence(f, n, min_n, min_span, signal) -> float:
    """0..1, and never 1.

    Three factors, all things that would make a statistician sceptical: how far
    past the minimum the evidence goes, how strong the estimate is against its
    own noise, and how much of the house's behaviour the model failed to
    explain.
    """
    f_n = _clamp(n / float(2 * min_n), 0.0, 1.0)
    f_span = _clamp(f.span / float(2 * min_span), 0.0, 1.0)
    f_signal = _clamp(signal, 0.0, 1.0)
    if f.residual_sd is None:
        f_noise = 0.7
    else:
        f_noise = _clamp(1.0 - f.residual_sd / (2.0 * MAX_RESIDUAL_SD_C), 0.0, 1.0)
    return round(CONFIDENCE_CAP * f_n * f_span * f_signal * f_noise, 2)


# -- small helpers ------------------------------------------------------------


def _as_sample(row):
    if isinstance(row, Sample):
        return row if isinstance(_num(row.ts), float) else None
    if isinstance(row, dict):
        ts = _num(row.get("ts", row.get("timestamp")))
        if ts is None:
            return None
        return Sample(ts=ts,
                      outdoor=_num(row.get("outdoor")),
                      indoor=_num(row.get("indoor")),
                      supply=_num(row.get("supply")),
                      degree_minutes=_num(row.get("degree_minutes", row.get("dm"))),
                      # Every state the bucket contained, when the source knows
                      # them (store.autotune_history does). An hour that made
                      # hot water for forty minutes and then heated is not
                      # evidence about the heating curve, and its last state
                      # alone cannot say so.
                      compressor=(row.get("compressor_states")
                                  or row.get("compressor")))
    if isinstance(row, (list, tuple)) and len(row) >= 2:
        vals = list(row) + [None] * (6 - len(row))
        ts = _num(vals[0])
        if ts is None:
            return None
        return Sample(ts=ts, outdoor=_num(vals[1]), indoor=_num(vals[2]),
                      supply=_num(vals[3]), degree_minutes=_num(vals[4]),
                      compressor=vals[5])
    return None


def _num(v):
    # bool is an int in Python, and True would silently become 1.0 degrees.
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return float(v)


def _in_range(v, bounds) -> bool:
    return bounds[0] <= v <= bounds[1]


def _is_not_heating(state) -> bool:
    """True when the compressor spent any of this sample on something else.

    A sequence -- every state seen inside an hourly bucket -- is true if any
    one of its states was not heating, which is what makes a bucket that
    contains a hot-water run stop counting as evidence about the curve.
    """
    if isinstance(state, str):
        low = state.lower()
        return any(word in low for word in _NOT_HEATING)
    if isinstance(state, (list, tuple, set, frozenset)):
        return any(_is_not_heating(one) for one in state)
    return False


def _night_window(night_hours):
    try:
        start, end = int(night_hours[0]) % 24, int(night_hours[1]) % 24
    except Exception:                                          # noqa: BLE001
        start, end = NIGHT_HOURS
    return start, end


def _is_night(ts, start, end) -> bool:
    hour = time.localtime(ts).tm_hour
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _night_key(ts, end) -> str:
    # Shifting back by the end hour puts an entire night -- the late evening and
    # the small hours after midnight -- on one calendar date.
    lt = time.localtime(ts - end * 3600)
    return "%04d-%02d-%02d" % (lt.tm_year, lt.tm_mon, lt.tm_mday)


def _eta(now, nights_needed) -> str:
    lt = time.localtime(now + max(1, nights_needed) * 86400)
    return "%04d-%02d-%02d" % (lt.tm_year, lt.tm_mon, lt.tm_mday)


def _sd(values, mean) -> float:
    if len(values) < 2:
        return 0.0
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def _t95(dof) -> float:
    """The two-sided 95 % t value for `dof` degrees of freedom.

    Between tabulated rows, and above the last one, the value for the largest
    tabulated dof at or below `dof` is used -- t falls as dof grows, so that is
    always the conservative direction. Falling straight to 1.96 (the dof =
    infinity value) the moment the table ran out was not: at 31 nights the true
    value is 2.04, and using 1.96 there overstates the evidence by 4 %, which
    is exactly the wrong way round for a module whose whole job is to refuse to
    claim more than it knows.
    """
    try:
        dof = int(dof)
    except (TypeError, ValueError):
        return float("inf")
    if dof <= 0:
        return float("inf")
    if dof in _T95:
        return _T95[dof]
    below = [d for d in _T95 if d < dof]
    return _T95[max(below)] if below else float("inf")


def _clamp(v, lo, hi) -> float:
    return max(lo, min(hi, v))


def _point_for(outdoor, points, temps=None):
    """The own-curve point that governs a given outdoor temperature.

    Nearest readable point, not "the cold end": P1 and P2 sit at -30 and -20 C,
    which most of Sweden never reaches, and advice that lands there is advice
    that does nothing.

    A point with no outdoor temperature -- P7 on the F generation, where NIBE's
    manual shows six points and the seventh is inference -- is not a candidate:
    "nearest" is meaningless for a point whose weather nobody has established.
    """
    temps = OWN_CURVE_OUTDOOR if temps is None else temps
    best, best_d = None, None
    for i, t in enumerate(temps):
        if t is None or i >= len(points) or _num(points[i]) is None:
            continue
        d = abs(t - outdoor)
        if best_d is None or d < best_d:
            best, best_d = i, d
    return best


def _fmt(v) -> str:
    return "–" if v is None else _sv(v, 0)


def _sv(v, dec: int = 1, sign: bool = False) -> str:
    """A number as Swedish prose: decimal comma and a real minus sign.

    Kept as a name in this module because every line below uses it, but the
    implementation now lives in the package root, where advisor, spot, homey
    and pump reach it too -- they were all still printing "38.4" and "-10" into
    Swedish sentences the page renders verbatim.
    """
    return sv_number(v, dec, sign)


def _reg(v):
    """Integer registers are shown as integers: offset 1, not offset 1.0."""
    if v is not None and float(v).is_integer():
        return int(v)
    return v
