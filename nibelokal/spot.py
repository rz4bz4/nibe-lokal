"""Hourly spot prices from Tibber, and a deliberately timid plan for using them.

The house has an hourly variable contract, so the price of a kilowatt-hour
swings by a factor of five over a normal winter day and by a factor of fifty
over a year. That is worth *something*. It is not worth letting the house get
cold, and the owner said so in as many words: "inte så aggressivt, vintern
brukar vara dyr". So this module is built around one rule:

    **Shift the load, never shed it.**

Every hour this plan heats extra is paid back by an hour it coasts, inside the
same day. The house ends the day as warm as it started; what changes is *when*
the compressor did the work. That is the honest version of price optimisation
on a heat pump, and it is the only version that survives a February cold snap
without someone turning the whole thing off in irritation.

Two design decisions follow from that, and both are the interesting part:

**Banding is relative, with an absolute floor.** Swedish spot prices span two
orders of magnitude across a year -- 3 öre/kWh on a windy Sunday in May, 4
kr/kWh on a still January evening. Any fixed öre threshold is wrong most of the
time: in May it calls every hour cheap, in January it calls every hour
expensive, and in both cases it has said nothing. So an hour is cheap or
expensive *relative to other hours* (`spot_cheap_rank` / `spot_expensive_rank`).
But a pure rank always finds a cheapest quartile, even on a flat day where the
cheapest and dearest hour are four öre apart -- and then it recommends heating
the house differently for no money at all. Hence `spot_min_spread`: below that
spread the honest answer is "normal" for every hour, and the plan does nothing.

**Relative to what, exactly, is two different questions**, and each row answers
both:

  ``band`` / ``rank``          across the whole horizon (today and, after about
                               13:00, tomorrow too). This is the comparison a
                               person looking at the price chart is making:
                               "is this hour dear *for the hours I can see*".
  ``day_band`` / ``day_rank``  within that row's own calendar day.

The plan reads ``day_band`` and nothing else, because it pairs hours up *inside
one day* -- one hour warmer paid back by one hour cooler, same day, sum zero.
Banding across the horizon and pairing per day is the bug this split exists to
prevent: on a Friday at 1.00-2.15 kr followed by a Saturday at 0.10-0.56 kr,
every horizon-cheap hour lands on Saturday and every horizon-dear hour on
Friday, so neither day has both and the plan proposes nothing at all -- while
telling the owner that no hour stands out, on a day with 1.15 kr of spread.
Weekday/weekend and a weather front moving through both shift the whole level
like that, which in Sweden makes it the common case rather than the corner one.

**The knob is the heating offset (register 40031), one step either way.** NIBE
documents one step as roughly 2.5 °C of supply temperature at every outdoor
temperature ("a curve offset of +2 steps increases the supply temperature by
5 °C"), which is already a real change to a house -- see the appendix in
docs/registers.md. One step is therefore the whole budget, not the unit of a
scale, and `spot_max_offset` is clamped to 1 no matter what the config says.

**SG Ready is deliberately not used for this.** It is a
tempting shortcut -- it exists precisely to be driven by an electricity price
signal -- and it is the wrong instrument here. What SG Ready's "low price" mode
actually *does* depends on settings this app does not control and cannot read
back meaningfully: the low-price parallel displacement, the hot-water start
temperature offset, and on some models a max-additional-heat limit
(register 41053), all configured on the pump's own display. Two identical S735s
with different installers respond to the same SG Ready input by different
amounts, and neither reports what it did. A ±1 heating offset is the opposite
of that: bounded, symmetric, instantly reversible, visible in the pump's own
menu 1.1.1, and expressed in the same unit the owner already uses when the house
feels cold. When it does something wrong, it is obvious what and by how much.

Nothing here writes to the pump. `Plan.build()` returns recommendations and a
register number; deciding whether to apply them, and passing them through
`safety.check()`, happens elsewhere.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
import urllib.error
import urllib.request

from . import sv_number
from .config import header_safe, scrub

log = logging.getLogger("nibelokal.spot")

API_URL = "https://api.tibber.com/v1-beta/gql"

#: The register this module reasons about. Guarded in safety.py, and rightly so.
R_OFFSET = 40031

#: Named only to say they are not used. 43033 arms SG Ready over Modbus and
#: 46009 requests one of its four states. 40761 is neither -- it is the pump's
#: own menu setting for whether SG Ready may affect the heating at all, which
#: is why safety.py guards it alongside 40762 and 40763 rather than treating it
#: as an input. And on an S735 you can arm SG Ready and then have no
#: addressable way to request a state, because 46009 is not on that map.
#: See "Appendix: SG Ready" in docs/registers.md.
R_SG_READY_ACTIVATE = 43033
R_SG_READY_REQUEST = 46009

BAND_CHEAP = "billig"
BAND_NORMAL = "normal"
BAND_EXPENSIVE = "dyr"

#: Every setting this module has, with its default, in one place.
#:
#: All of these are in config.DEFAULTS, so they can be set in config.yaml (or
#: through the environment as NIBE_TIBBER_TOKEN and friends) -- tests/test_wiring.py
#: asserts the two agree, so changing a default here without changing it there
#: fails the suite rather than quietly running on a different number. They can
#: also be passed to these classes directly, which is what the tests do.
CONFIG_KEYS = {
    # Personal API token from developer.tibber.com. Empty = no price features.
    "tibber_token": "",
    # Only needed when the Tibber account has more than one home.
    "tibber_home_id": "",
    "spot_timeout": 10.0,
    # Prices change twice a day and Tibber rate-limits. Fifteen minutes is
    # frequent enough to pick up tomorrow's prices soon after they land.
    "spot_cache_seconds": 900.0,
    # Currency per kWh, on the price the owner actually pays (Tibber's `total`,
    # i.e. energy + tax + VAT). Below this max-to-min spread over the horizon,
    # nothing is banded cheap or expensive and the plan does nothing.
    #
    # 0.15 kr/kWh is chosen from what a step is worth: an S735 in a Swedish
    # winter moves on the order of 3-5 kWh in an hour, so shifting one hour's
    # heat across a 15-öre spread is worth well under a krona a day -- while
    # one offset step is about 2.5 C of supply temperature, which is a change
    # the household can feel. Below that ratio the honest answer is "leave the
    # house alone". Raise it to be even more conservative.
    "spot_min_spread": 0.15,
    # An hour is cheap in the bottom quartile of the horizon and expensive in
    # the top quartile. Quartiles rather than halves because "cheap" should mean
    # unusually cheap, not merely below average.
    "spot_cheap_rank": 0.25,
    "spot_expensive_rank": 0.75,
    # Clamped to 1 regardless. See Plan.__init__.
    "spot_max_offset": 1,
    # At most this many hours up and the same number down, per day.
    "spot_max_pairs": 4,
}


def _setting(config: dict | None, key: str):
    value = (config or {}).get(key)
    return CONFIG_KEYS[key] if value in (None, "") else value


# Verified against Tibber's own client library (pyTibber, tibber/gql_queries.py)
# rather than from memory: `priceInfo` hangs off viewer.homes[].currentSubscription,
# `today` and `tomorrow` are plain lists of Price, and each Price has total,
# startsAt and level. `current` is the one place the currency is exposed on this
# query, which is why it is asked for there and nowhere else.
#
# The resolution argument is the part that moved: when the Nordic day-ahead
# market went to 15-minute settlement, Tibber added QUARTER_HOURLY and clients
# started passing it explicitly. We want whole hours -- the offset register is
# not something to touch four times an hour -- so HOURLY is passed explicitly
# rather than relying on a default that has already changed once. If the
# argument is rejected (an older schema), _fetch retries without it.
_QUERY = """{
  viewer {
    homes {
      id
      currentSubscription {
        priceInfo%s {
          current { total startsAt level currency }
          today { total startsAt level }
          tomorrow { total startsAt level }
        }
      }
    }
  }
}"""

_RESOLUTIONS = ["(resolution: HOURLY)", ""]


class Tibber:
    """Today's and tomorrow's hourly prices, banded relative to the horizon."""

    def __init__(self, config: dict | None = None):
        self.token = str(_setting(config, "tibber_token")).strip()
        self.home_id = str(_setting(config, "tibber_home_id")).strip()
        self.timeout = float(_setting(config, "spot_timeout"))
        self.min_spread = float(_setting(config, "spot_min_spread"))
        self.cheap_rank = float(_setting(config, "spot_cheap_rank"))
        self.expensive_rank = float(_setting(config, "spot_expensive_rank"))
        self.cache_seconds = float(_setting(config, "spot_cache_seconds"))
        # A token is checked once, here, rather than every time a header is
        # built from it: a value http.client refuses makes it raise a
        # ValueError whose message quotes the whole header -- token included --
        # and the error paths below turn that into an HTTP response body.
        self.token_error: str | None = None
        if self.token and not header_safe(self.token):
            self.token_error = (
                "tibber_token innehåller tecken som inte får finnas i en "
                "HTTP-header (radbrytning, tabb eller liknande). Kontrollera "
                "att den är klistrad in i en rad i config.yaml. Ingen "
                "förfrågan skickas förrän den är rättad."
            )
        # One request at a time. The web server runs a thread per request on
        # top of the polling thread, so two tabs arriving at an expired cache
        # made two Tibber requests -- against an API that rate-limits, for
        # prices that change twice a day. weather.py has had this from the
        # start; this had the caches and not the lock.
        self._lock = threading.Lock()
        self._cached: dict | None = None
        self._cached_at = 0.0
        # Failures are remembered too. Without this a dead token or a 429 is
        # retried on every single page load, which is precisely how a rate
        # limit turns into a ban. Shorter than the success TTL, because a
        # failure is more likely to be temporary than prices are to change.
        self._failure: dict | None = None
        self._failure_at = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.token)

    @property
    def failure_seconds(self) -> float:
        """How long a failed fetch is remembered. See _failure above."""
        return min(self.cache_seconds, 300.0)

    # -- fetching ------------------------------------------------------------

    def _post(self, query: str) -> dict:
        """One GraphQL POST. Raises; only _fetch is allowed to see that."""
        body = json.dumps({"query": query}).encode("utf-8")
        request = urllib.request.Request(
            API_URL,
            data=body,
            method="POST",
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "nibe-lokal",
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            raw = response.read()
        return json.loads(raw.decode("utf-8", "replace"))

    def _fetch(self) -> dict:
        """The priceInfo object for the chosen home. Raises _SpotError."""
        last: Exception | None = None
        for resolution in _RESOLUTIONS:
            try:
                payload = self._post(_QUERY % resolution)
            except urllib.error.HTTPError as exc:
                # Kept for a proxy or a future API that answers this way; Tibber
                # itself does not (see _is_token_error).
                if exc.code in (401, 403):
                    raise _SpotError(_BAD_TOKEN % ("HTTP %d" % exc.code)) from exc
                raise _SpotError(
                    "Tibber svarade HTTP %d." % exc.code
                ) from exc
            except urllib.error.URLError as exc:
                raise _SpotError(
                    "Kunde inte nå Tibber: %s." % (self._safe(exc.reason),)
                ) from exc
            except (OSError, ValueError) as exc:
                # ValueError covers a body that is not JSON at all, which is what
                # a captive portal or a proxy error page looks like from here.
                raise _SpotError("Kunde inte läsa svaret från Tibber: %s."
                                 % self._safe(exc)) from exc

            errors = payload.get("errors") if isinstance(payload, dict) else None
            if errors:
                message = _first_error(errors)
                # An older schema rejects the resolution argument by name. That is
                # the one GraphQL error worth retrying differently instead of
                # reporting; everything else is a real problem.
                if resolution and "resolution" in message.lower():
                    last = _SpotError(message)
                    continue
                # A rejected token is *not* an HTTP 401 here. Verified against
                # the live API: a bad Bearer token answers HTTP 200 with a
                # GraphQL error, so the tailored advice has to hang off the
                # message rather than off the status code.
                if _is_token_error(message):
                    raise _SpotError(_BAD_TOKEN % message)
                raise _SpotError("Tibber svarade med ett fel: %s" % message)

            return _price_info(payload, self.home_id)

        raise _SpotError("Tibber svarade med ett fel: %s" % last)

    # -- public --------------------------------------------------------------

    def _safe(self, value) -> str:
        """A message with the token taken out of it, whatever produced it."""
        return scrub(value, self.token)

    def snapshot(self, now: float | None = None) -> dict:
        """Prices for the horizon we can see, banded. Never raises."""
        at = time.time() if now is None else float(now)
        with self._lock:
            return self._snapshot(at)

    def _snapshot(self, at: float) -> dict:
        if not self.configured:
            return {
                "ok": False,
                "configured": False,
                "at": at,
                "error": "Ingen Tibber-token är konfigurerad. Sätt tibber_token i "
                         "config.yaml (hämtas på developer.tibber.com) om du vill "
                         "att appen ska se elpriset.",
            }
        if self.token_error:
            return {"ok": False, "configured": True, "at": at,
                    "error": self.token_error}

        if (self._cached is not None
                and at - self._cached_at < self.cache_seconds
                and at >= self._cached_at):
            # Tibber rate-limits, and the prices only change twice a day. Re-asking
            # every page load would be rude and would buy nothing.
            return dict(self._cached, cached=True)
        if (self._failure is not None
                and at - self._failure_at < self.failure_seconds
                and at >= self._failure_at):
            # And a failure is worth remembering for the same reason: a revoked
            # token or an HTTP 429 answers the same way every time, and asking
            # again on every page load is how a rate limit becomes a ban.
            return dict(self._failure, cached=True)

        try:
            info = self._fetch()
            result = self._build(info, at)
        except _SpotError as exc:
            return self._remember_failure(at, self._safe(exc))
        except Exception as exc:                                # noqa: BLE001
            log.exception("unexpected failure reading Tibber")
            return self._remember_failure(
                at, "Oväntat fel vid hämtning av elpris: %s" % self._safe(exc))

        self._cached, self._cached_at = result, at
        self._failure, self._failure_at = None, 0.0
        return dict(result, cached=False)

    def _remember_failure(self, at: float, error: str) -> dict:
        self._failure = {"ok": False, "configured": True, "at": at, "error": error}
        self._failure_at = at
        return dict(self._failure, cached=False)

    def _build(self, info: dict, at: float) -> dict:
        today = _hours(info.get("today"))
        tomorrow = _hours(info.get("tomorrow"))
        if not today and not tomorrow:
            raise _SpotError(
                "Tibber lämnade inga priser. Kontot behöver ett aktivt "
                "timprisavtal för att priserna ska finnas."
            )
        # Tomorrow does not exist until the day-ahead auction is published and
        # Tibber has picked it up, around 13:00 svensk tid. Before that the field
        # is an empty list, not an error, and a horizon of 24 hours (or fewer, late
        # in the day) is the normal state of affairs for half of every day.
        rows = today + tomorrow

        totals = [row["total"] for row in rows]
        spread = max(totals) - min(totals)
        flat = spread < self.min_spread

        count = len(rows)
        for row in rows:
            row["rank"] = _rank(totals, row["total"])
            row["band"] = self._band(row["rank"], flat, count)

        # The same question asked again, inside each calendar day. See the
        # module docstring: the plan pairs hours within one day, so it needs a
        # band that means "cheap for this day" -- a horizon-wide band on a day
        # that is uniformly dearer than tomorrow contains no cheap hours at all,
        # and the plan then has nothing to pair and says the day is featureless.
        days: dict = {}
        for row in rows:
            days.setdefault(row["day"], []).append(row)
        day_stats = []
        for key in sorted(days):
            day_rows = days[key]
            day_totals = [row["total"] for row in day_rows]
            day_spread = max(day_totals) - min(day_totals)
            day_flat = day_spread < self.min_spread
            for row in day_rows:
                row["day_rank"] = _rank(day_totals, row["total"])
                row["day_band"] = self._band(row["day_rank"], day_flat,
                                             len(day_rows))
            day_stats.append({
                "day": key,
                "hours": len(day_rows),
                "min": min(day_totals),
                "max": max(day_totals),
                "spread": day_spread,
                "flat": day_flat,
            })

        current = info.get("current") or {}
        # "now" comes from our own banded rows, not from Tibber's `current`. The
        # two normally agree, but `current` carries no band, and if the horizon
        # does not actually cover this moment -- stale cache, a pump left off for
        # a week -- then there is no honest "now" to report and it stays None.
        now_row = _match_now(rows, at)
        now = None
        if now_row is not None:
            # Tibber's `current` is authoritative for price and level, but only
            # for the hour it actually belongs to -- a cached response makes it
            # older than the row we matched.
            same_hour = _hour_key(current.get("startsAt")) == now_row["starts_at"]
            total = current.get("total") if same_hour else None
            if not isinstance(total, (int, float)) or isinstance(total, bool):
                total = now_row["total"]
            level = current.get("level") if same_hour else None
            now = {
                "total": float(total),
                "level": level or now_row.get("level"),
                "starts_at": now_row["starts_at"],
                "band": now_row["band"],
                "day_band": now_row["day_band"],
            }

        currency = current.get("currency")
        if not isinstance(currency, str) or not currency:
            # The house is in Sweden and the contract is in SEK; saying so is more
            # useful than a blank unit next to a number.
            currency = "SEK"

        return {
            "ok": True,
            "configured": True,
            "at": at,
            "currency": currency,
            "now": now,
            "hours": rows,
            "stats": {
                "min": min(totals),
                "max": max(totals),
                "mean": sum(totals) / count,
                "median": _median(totals),
                "spread": spread,
                "flat": flat,
            },
            # Which comparison the plain `band` and `rank` on each row were
            # made against, said out loud so a reader of the JSON does not have
            # to infer it. `day_band` and `day_rank` are always the calendar day.
            "band_scope": "horizon",
            "days": day_stats,
            "horizon_hours": count,
            "has_tomorrow": bool(tomorrow),
        }

    def _band(self, rank: float, flat: bool, count: int) -> str:
        # Two hours are not a distribution, and a day whose whole spread is a few
        # öre has nothing to optimise: calling its cheapest quartile "billig" would
        # invite a change to the house worth less than a krona.
        if flat or count < 4:
            return BAND_NORMAL
        if rank <= self.cheap_rank:
            return BAND_CHEAP
        if rank >= self.expensive_rank:
            return BAND_EXPENSIVE
        return BAND_NORMAL


class Plan:
    """A symmetric ±1 offset schedule: load shifting, not load shedding.

    Every cheap hour that gets +1 is paid for by an expensive hour that gets -1,
    within the same calendar day, so the day's offsets sum to exactly zero. The
    house is warmed a little earlier than it otherwise would have been, and cools
    back through the expensive hours on the heat already in the slab and the
    radiators. Nothing is given up; the timing moves.

    The pairing is capped at `spot_max_pairs` hours in each direction, which is
    also what bounds a *sliding* 24 h window. Such a window is the tail of one
    day plus the head of the next; since each day sums to zero, its total is
    head(B) - head(A), so at worst 2 x spot_max_pairs hour-steps of one offset
    step -- eight, at the default. Measured on a normal winter shape it stays at
    five, because the cheap hours cluster at night and the expensive ones in the
    evening rather than at the seam. Either way it is a temporary lean of one
    step, not a standing setting, and the day it belongs to still closes at zero.
    The cap is also the difference between "gentle" and a schedule that spends a
    third of the day at a different supply temperature.
    """

    def __init__(self, config: dict | None = None):
        # Hard ceiling, not a default. One step is already ~2.5 C of supply
        # temperature; a config typo must not be able to turn this into a system
        # that swings the house by five degrees to save money.
        self.max_offset = max(0, min(1, int(_setting(config, "spot_max_offset"))))
        self.max_pairs = max(0, int(_setting(config, "spot_max_pairs")))
        self.min_spread = float(_setting(config, "spot_min_spread"))

    def build(self, snapshot: dict, now: float | None = None) -> dict:
        """Per-hour offset deltas. Advisory only, and never raises."""
        at = time.time() if now is None else float(now)
        try:
            return self._build(snapshot or {}, at)
        except Exception as exc:                                # noqa: BLE001
            log.exception("unexpected failure building the price plan")
            return _empty_plan(at, "Kunde inte räkna fram något förslag: %s" % exc)

    def _build(self, snapshot: dict, at: float) -> dict:
        if not snapshot.get("ok"):
            return _empty_plan(at, snapshot.get("error")
                               or "Inga elpriser att planera efter.")
        rows = snapshot.get("hours") or []
        if not rows:
            return _empty_plan(at, "Inga timpriser i underlaget.")

        days: dict[str, list] = {}
        for row in rows:
            days.setdefault(row.get("day") or "", []).append(row)

        hours: list[dict] = []
        summaries: list[dict] = []
        for key in sorted(days):
            day_rows = days[key]
            hours.extend(self._plan_day(key, day_rows, summaries))

        hours.sort(key=_by_instant)
        now_row = _match_now(hours, at)
        moved = sum(1 for row in hours if row["offset_delta"])
        return {
            "ok": True,
            "at": at,
            "advisory": True,
            "register": R_OFFSET,
            "max_offset": self.max_offset,
            # Which banding the pairing was made from. The prices snapshot says
            # "horizon" for its own plain `band`; these two are not the same
            # question and the JSON should not leave a reader guessing which.
            "band_scope": "day",
            "currency": snapshot.get("currency", "SEK"),
            "hours": hours,
            "days": summaries,
            "now": now_row,
            "summary": self._summary(summaries, moved),
        }

    def _plan_day(self, key: str, day_rows: list, summaries: list) -> list:
        rows = sorted(day_rows, key=_by_instant)
        planned = [dict(row, offset_delta=0, why="") for row in rows]
        totals = [row["total"] for row in rows]
        spread = max(totals) - min(totals) if totals else 0.0

        def finish(pairs: int, reason: str) -> list:
            summaries.append({
                "day": key,
                "hours": len(planned),
                "spread": spread,
                "pairs": pairs,
                # The property the whole module rests on, reported rather than
                # merely asserted -- a caller can check it without trusting us.
                "net": sum(row["offset_delta"] for row in planned),
                "reason": reason,
            })
            return planned

        if self.max_offset == 0 or self.max_pairs == 0:
            return finish(0, "Prisstyrningen är avstängd i konfigurationen.")
        if len(planned) < 4:
            return finish(0, "För få kända timmar det här dygnet för att flytta något.")
        if spread < self.min_spread:
            return finish(0, "Prisskillnaden över dygnet är bara %s, för liten för att "
                             "vara värd en ändring i huset." % _price(spread))

        # `day_band`, never `band`: the pairing happens inside this one day, so
        # the band it reads has to be about this one day too. See the module
        # docstring. `band` is the fallback only for a snapshot built by
        # something older that does not carry the per-day banding at all.
        cheap = [row for row in planned if _day_band(row) == BAND_CHEAP]
        dear = [row for row in planned if _day_band(row) == BAND_EXPENSIVE]
        # Symmetry is enforced here and nowhere else: as many hours up as down.
        # min() of the two sets is what makes the day sum to zero even when the
        # banding hands us six cheap hours and two expensive ones.
        pairs = min(len(cheap), len(dear), self.max_pairs)
        if pairs == 0:
            return finish(0, "Inga timmar sticker ut tillräckligt åt båda hållen "
                             "det här dygnet.")

        by_price = sorted(planned, key=lambda row: row["total"])
        # The cheapest and dearest hours of the day are by construction a subset
        # of the cheap and expensive bands, so these two slices never overlap.
        for row in by_price[:pairs]:
            row["offset_delta"] = self.max_offset
            row["why"] = ("Billig timme (%s) — värm lite extra nu, så slipper "
                          "pumpen jobba när det är dyrt." % _price(row["total"]))
        for row in by_price[-pairs:]:
            row["offset_delta"] = -self.max_offset
            row["why"] = ("Dyr timme (%s) — låt huset gå på värmen det redan har."
                          % _price(row["total"]))
        return finish(pairs, "")

    def _summary(self, summaries: list, moved: int) -> str:
        active = [row for row in summaries if row["pairs"]]
        if not active:
            reasons = [row["reason"] for row in summaries if row["reason"]]
            return reasons[0] if reasons else "Inget att flytta just nu."
        tail = ("Summan över varje dygn är noll — huset ska vara lika varmt i kväll "
                "som i morse, värmen är bara flyttad till billigare timmar.")
        if len(active) == 1:
            return ("Förslag: %d timmar med ett steg varmare och lika många med ett "
                    "steg svalare. %s" % (moved // 2, tail))
        # Not "jämnt fördelat": each day is planned on its own prices, so four
        # pairs one day and one the next is a perfectly normal outcome, and
        # claiming an even split would be a claim the code does not make good on.
        share = ", ".join("%s: %d par" % (row["day"], row["pairs"])
                          for row in active)
        return ("Förslag: %d timmar med ett steg varmare och lika många med ett steg "
                "svalare, fördelat över %d dygn (%s). %s"
                % (moved // 2, len(active), share, tail))


# --- helpers ----------------------------------------------------------------


class _SpotError(Exception):
    """A failure with a message already written in Swedish, for the user."""


#: One message for a rejected token, whichever way Tibber phrased the rejection.
_BAD_TOKEN = ("Tibber avvisade token (%s). Skapa en ny på developer.tibber.com "
              "och uppdatera tibber_token.")

#: What a rejected Bearer token looks like in a GraphQL error message. Tibber
#: answers HTTP 200 with one of these rather than 401, so this -- not the status
#: code -- is what tells a wrong token apart from a broken query.
_TOKEN_ERRORS = ("invalid token", "invalid access token", "unauthenticated",
                 "unauthorized", "not authorized", "no valid token",
                 "token is invalid", "forbidden")


def _is_token_error(message: str) -> bool:
    low = str(message or "").lower()
    return any(needle in low for needle in _TOKEN_ERRORS)


def _day_band(row: dict) -> str:
    """The band for this row's own day, falling back to the horizon band."""
    band = row.get("day_band")
    return band if band else row.get("band")


def _first_error(errors) -> str:
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, str) and message:
                return message
    return str(errors)


def _price_info(payload: dict, home_id: str) -> dict:
    """Dig priceInfo out of the response, saying which step was missing."""
    if not isinstance(payload, dict):
        raise _SpotError("Tibber svarade med något som inte är JSON-data.")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise _SpotError("Tibber-svaret saknar data.")
    viewer = data.get("viewer")
    if not isinstance(viewer, dict):
        raise _SpotError("Tibber-svaret saknar viewer.")
    homes = viewer.get("homes")
    if not isinstance(homes, list) or not homes:
        raise _SpotError("Tibber-kontot har inga hem kopplade till sig.")

    chosen = None
    for home in homes:
        if not isinstance(home, dict):
            continue
        if home_id and home.get("id") != home_id:
            continue
        subscription = home.get("currentSubscription")
        if not isinstance(subscription, dict):
            continue
        if isinstance(subscription.get("priceInfo"), dict):
            chosen = subscription["priceInfo"]
            break
    if chosen is None:
        if home_id:
            raise _SpotError(
                "Hittade inget hem med id %s som har ett aktivt elavtal hos Tibber. "
                "Lämna tibber_home_id tomt om du bara har ett hem." % home_id
            )
        raise _SpotError(
            "Inget av hemmen hos Tibber har ett aktivt elavtal med timpris."
        )
    return chosen


def _hours(entries) -> list:
    """Tibber Price entries as our own rows, one per whole hour.

    Quarter-hourly data is folded into whole hours by averaging, so a response
    at the finer resolution still produces a schedule for the offset register --
    which is not something to touch four times an hour.
    """
    if not isinstance(entries, list):
        return []
    buckets: dict = {}
    order: list = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        total = entry.get("total")
        if not isinstance(total, (int, float)) or isinstance(total, bool):
            continue
        when = _parse_iso(entry.get("startsAt"))
        if when is None:
            continue
        hour = when.replace(minute=0, second=0, microsecond=0)
        key = hour.isoformat()
        if key not in buckets:
            buckets[key] = {"starts_at": key, "day": hour.date().isoformat(),
                            "level": entry.get("level"), "_totals": []}
            order.append(key)
        buckets[key]["_totals"].append(float(total))
    rows = []
    for key in order:
        row = buckets[key]
        totals = row.pop("_totals")
        row["total"] = sum(totals) / len(totals)
        rows.append(row)
    rows.sort(key=_by_instant)
    return rows


def _instant(value) -> float | None:
    """An ISO timestamp as a unix instant, or None if it will not parse."""
    when = _parse_iso(value)
    if when is None:
        return None
    if when.tzinfo is None:
        # A naive fixture: read it as UTC rather than guess a zone. Every row
        # from Tibber carries an offset, so this is test data only.
        when = when.replace(tzinfo=dt.timezone.utc)
    return when.timestamp()


def _by_instant(row: dict):
    """Sort key: the moment an hour starts, not the text it is written as.

    `starts_at` is local wall time with an offset, and as *text*
    "02:00:00+01:00" sorts before "02:00:00+02:00" -- which is backwards in
    time. On the last Sunday in October, Sweden repeats 02:00 with the offset
    changing from +02:00 to +01:00, so a text sort puts the second 02:00 before
    the first and interleaves 03:00 after it. It is one hour a year and it is
    the hour the plan reads back wrong, so it is sorted on the parsed instant.

    Rows whose timestamp will not parse keep a stable place at the end instead
    of raising: a garbage snapshot must still produce an explained empty plan.
    """
    at = _instant(row.get("starts_at"))
    return (at is None, at if at is not None else 0.0,
            str(row.get("starts_at") or ""))


def _parse_iso(value):
    """Tibber's startsAt as a datetime, or None.

    Tibber sends local wall time with an explicit offset, e.g.
    2026-01-15T03:00:00.000+01:00. The offset is what makes grouping by calendar
    day correct across a DST change, where a "day" is 23 or 25 hours long and a
    plan that assumed 24 would silently unbalance itself twice a year.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text[-1:] in ("Z", "z"):
        # fromisoformat did not accept 'Z' before Python 3.11.
        text = text[:-1] + "+00:00"
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError:
        return None


def _hour_key(value) -> str:
    """The row key an ISO timestamp would land on, or "" if it is unusable."""
    when = _parse_iso(value)
    if when is None:
        return ""
    return when.replace(minute=0, second=0, microsecond=0).isoformat()


def _match_now(rows: list, at: float):
    """The row covering `at`, comparing on the timestamps we were given."""
    when = dt.datetime.fromtimestamp(at, dt.timezone.utc)
    best = None
    for row in rows:
        start = _parse_iso(row.get("starts_at"))
        if start is None:
            continue
        if start.tzinfo is None:
            # A naive fixture: compare naively rather than guess a zone.
            start = start.replace(tzinfo=dt.timezone.utc)
        if start <= when and (best is None or start > best[0]):
            best = (start, row)
    if best is None:
        return None
    if when - best[0] >= dt.timedelta(hours=2):
        # The newest hour we know is already in the past: stale data, and saying
        # "now" about it would be a lie.
        return None
    return best[1]


def _rank(totals: list, value: float) -> float:
    """Midrank of `value` within `totals`, on 0..1. Ties share a rank."""
    if len(totals) < 2:
        return 0.5
    below = sum(1 for other in totals if other < value)
    equal = sum(1 for other in totals if other == value)
    # Average position across the tie, so that a day with many identical hours
    # does not put all of them in the cheapest quartile.
    position = below + (equal - 1) / 2.0
    return position / (len(totals) - 1)


def _median(values: list) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _price(value: float) -> str:
    """A price in a Swedish sentence: "0,42 kr/kWh", not "0.42 kr/kWh"."""
    return "%s kr/kWh" % sv_number(value, 2)


def _empty_plan(at: float, error: str) -> dict:
    return {
        "ok": False,
        "at": at,
        "advisory": True,
        "register": R_OFFSET,
        "max_offset": 0,
        "hours": [],
        "days": [],
        "now": None,
        "summary": error,
        "error": error,
    }
