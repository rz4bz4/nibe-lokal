"""Configuration. Deliberately tiny: a YAML file if PyYAML is around, plain
key: value parsing if it is not, and environment variables on top of both.

Nothing here has a default that points at anyone's actual house.
"""
from __future__ import annotations

import os

DEFAULTS = {
    "host": "",                 # the pump's IP address on your LAN
    "port": 502,
    "unit": 1,                  # Modbus slave id; try 1, then 0
    "model": "",                # required unless register_csv is set
    "register_csv": "",         # CSV exported from the pump, menu 7.5.9
    "listen": "127.0.0.1",      # bind address for the web app
    "listen_port": 8377,
    "auth_token": "",           # empty = no token required
    "allowed_hosts": "",        # extra Host names to accept, space separated
    "allow_any_host": False,    # only if something else already checks Host
    "poll_seconds": 60,         # NIBE's own guidance is not to poll harder
    "history_days": 400,
    # Hours between automatic full-settings snapshots. 0 turns them off.
    "auto_backup_hours": 24,
    "allow_guarded_writes": True,
    # floor | radiators | mixed -- shapes the heating advice, nothing else
    "emitters": "radiators",
    "database": "nibe.db",
    "backup_dir": "backup",
    "timeout": 5.0,

    # --- optional integrations ------------------------------------------
    # Every one of these is off until it is configured, and every one of them
    # fails into a message on the page rather than into a broken app. The
    # modules that read them (homey.py, alarms.py, weather.py, spot.py,
    # autotune.py) each carry the reasoning behind the numbers; the defaults
    # are repeated here, and not imported from them, so that a feature module
    # that will not import cannot stop the pump app from starting.

    # Indoor temperature from a Homey Pro on the LAN -- nibelokal/homey.py.
    # The pump has no room sensor, so this is the only indoor number there is.
    "homey_host": "",              # e.g. 192.168.1.20
    "homey_token": "",             # Homey local API key
    "homey_devices": "",           # trusted sensors, comma separated; empty = all
    "homey_max_age_minutes": 60.0,  # older readings count as stale, not as cold
    "homey_timeout": 5.0,
    "homey_cache_seconds": 120.0,

    # Alarm watching -- nibelokal/alarms.py. Watching itself is always on: the
    # alarm register is polled anyway and the events are written to the same
    # database. Only the push needs credentials.
    "pushover_token": "",          # application token from pushover.net
    "pushover_user": "",           # your user key from the same page
    "alarm_notify": True,          # mute switch that keeps the credentials
    "alarm_min_severity": "warning",   # info | warning | alarm
    "alarm_emergency_priority": True,  # priority 2: repeats until acknowledged
    "alarm_retry_seconds": 300,
    "alarm_expire_seconds": 10800,
    # Flap guard: at most a few notifications per alarm code inside this many
    # seconds, the rest held and collapsed. See alarms.py.
    "alarm_debounce_seconds": 900,

    # SMHI forecast -- nibelokal/weather.py. No coordinates, no forecast.
    "weather_lat": "",
    "weather_lon": "",
    "weather_ttl_minutes": 60.0,
    "weather_timeout": 8.0,

    # Spot prices from Tibber and the load-shifting plan -- nibelokal/spot.py.
    # The plan is advisory: nothing here writes to the pump.
    "tibber_token": "",            # personal token from developer.tibber.com
    "tibber_home_id": "",          # only needed with more than one home
    "spot_timeout": 10.0,
    "spot_cache_seconds": 900.0,
    "spot_min_spread": 0.15,       # kr/kWh; below this the plan does nothing
    "spot_cheap_rank": 0.25,
    "spot_expensive_rank": 0.75,
    "spot_max_offset": 1,          # clamped to 1 by spot.Plan regardless
    "spot_max_pairs": 4,

    # Curve autotuning -- nibelokal/autotune.py. It fits indoor error against
    # outdoor temperature, so it needs an indoor source: in practice Homey.
    "autotune_target_indoor": "",  # the temperature the house should hold
    "autotune_days": 30,           # how much history the analysis looks at
}

# Keys the app can run without. A typo in one of them must not stop the pump
# app from starting -- an optional integration that is misconfigured takes
# itself out of service, it does not take the heating app with it. The keys
# above this line keep failing loudly, because a mistyped port is not a
# feature that can be skipped.
OPTIONAL_KEYS = {
    "homey_host", "homey_token", "homey_devices", "homey_max_age_minutes",
    "homey_timeout", "homey_cache_seconds",
    "pushover_token", "pushover_user", "alarm_notify", "alarm_min_severity",
    "alarm_emergency_priority", "alarm_retry_seconds", "alarm_expire_seconds",
    "alarm_debounce_seconds",
    "weather_lat", "weather_lon", "weather_ttl_minutes", "weather_timeout",
    "tibber_token", "tibber_home_id", "spot_timeout", "spot_cache_seconds",
    "spot_min_spread", "spot_cheap_rank", "spot_expensive_rank",
    "spot_max_offset", "spot_max_pairs",
    "autotune_target_indoor", "autotune_days",
}

INT_KEYS = {"port", "unit", "listen_port", "poll_seconds", "history_days",
            "alarm_retry_seconds", "alarm_expire_seconds",
            "alarm_debounce_seconds",
            "spot_max_offset", "spot_max_pairs", "autotune_days"}
# weather_lat, weather_lon and autotune_target_indoor default to "" rather than
# to a number: "" is how "not set" is spelled, and every module that reads them
# treats an unparseable value as absent.
FLOAT_KEYS = {"timeout", "auto_backup_hours",
              "homey_max_age_minutes", "homey_timeout", "homey_cache_seconds",
              "weather_lat", "weather_lon", "weather_ttl_minutes",
              "weather_timeout",
              "spot_timeout", "spot_cache_seconds", "spot_min_spread",
              "spot_cheap_rank", "spot_expensive_rank",
              "autotune_target_indoor"}
BOOL_KEYS = {"allow_guarded_writes", "allow_any_host",
             "alarm_notify", "alarm_emergency_priority"}


def _coerce(key: str, value):
    if isinstance(value, str):
        value = value.strip().strip('"').strip("'")
    if key in BOOL_KEYS:
        if isinstance(value, bool):
            return value
        return str(value).lower() in ("1", "true", "yes", "on")
    if key in INT_KEYS:
        return int(value)
    if key in FLOAT_KEYS:
        return float(value)
    return value


def _coerce_or_default(key: str, value):
    """Coerce, but never let an optional feature's typo be fatal. See OPTIONAL_KEYS."""
    try:
        return _coerce(key, value)
    except (TypeError, ValueError):
        if key not in OPTIONAL_KEYS:
            raise
        print("config: %r is not a valid value for %s; using the default (%r)"
              % (value, key, DEFAULTS[key]))
        return DEFAULTS[key]


def load(path: str = "config.yaml") -> dict:
    cfg = dict(DEFAULTS)

    if os.path.exists(path):
        loaded = None
        try:
            import yaml  # optional
            with open(path, encoding="utf-8") as fh:
                loaded = yaml.safe_load(fh) or {}
        except ImportError:
            loaded = {}
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or ":" not in line:
                        continue
                    k, v = line.split(":", 1)
                    v = v.strip()
                    # Strip a trailing comment, but not a # inside a quoted
                    # value -- a token containing one would be silently cut.
                    if v[:1] in ("'", '"'):
                        end = v.find(v[0], 1)
                        v = v[:end + 1] if end > 0 else v
                    else:
                        v = v.split("#", 1)[0].strip()
                    loaded[k.strip()] = v
        for k, v in (loaded or {}).items():
            if v in (None, ""):
                continue
            if k not in cfg:
                # Better a loud warning than a setting that silently does nothing
                # -- a mistyped allowed_hosts is a locked-out phone.
                print("config: ignoring unknown key %r" % k)
                continue
            cfg[k] = _coerce_or_default(k, v)

    for k in cfg:
        env = os.environ.get("NIBE_" + k.upper())
        if env:
            cfg[k] = _coerce_or_default(k, env)

    if not cfg["model"] and not cfg["register_csv"]:
        raise SystemExit(
            "No pump model configured.\n\n"
            "Set `model` in config.yaml to your pump (S735, S1155, S1255, S320, "
            "SMO40, ...),\nor better, export the register list from the pump itself "
            "(menu 7.5.9 ->\n\"Export all registers\" onto a USB stick) and point "
            "`register_csv` at the CSV.\n\n"
            "There is no safe default: a mismatched register map does not fail, it "
            "reads\nplausible nonsense."
        )
    if not cfg["host"]:
        raise SystemExit(
            "No pump address configured.\n\n"
            "Copy config.example.yaml to config.yaml and set `host` to your heat pump's\n"
            "IP address on your LAN, or set NIBE_HOST in the environment.\n\n"
            "Find it in your router's DHCP lease list -- the pump appears as NIBE-<serial>.\n"
            "Modbus TCP must be enabled on the pump: menu 7.5.9 Modbus TCP/IP."
        )
    return cfg


def resolve(cfg: dict, key: str, base: str) -> str:
    """Turn a possibly-relative path from the config into an absolute one."""
    value = cfg.get(key) or ""
    if not value:
        return ""
    return value if os.path.isabs(value) else os.path.join(base, value)


# -- credentials -------------------------------------------------------------
#
# Two helpers, here rather than in the modules that need them, because homey.py
# and spot.py both put a configured token straight into an Authorization header
# and both got this wrong in the same way.

#: Characters http.client will not put in a header value. A token never
#: contains one; a config file edited by hand does -- a stray carriage return
#: inside a quoted YAML string is the case this was found on.
_CONTROL = frozenset(chr(c) for c in range(0x20)) | {"\x7f"}


def header_safe(value) -> bool:
    """True when `value` can be sent as an HTTP header value at all.

    http.client raises ValueError("Invalid header value b'Bearer <token>'") for
    anything else -- and that message *contains the credential*, which is how a
    token ends up echoed into an HTTP response body by an error path that was
    only trying to be helpful. Checked before the request is built, so that
    exception never happens and there is nothing to leak.
    """
    if not isinstance(value, str):
        return False
    if any(ch in _CONTROL for ch in value):
        return False
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        return False
    return True


def scrub(text, *secrets) -> str:
    """Remove any of `secrets` from `text`. Belt to header_safe's braces.

    Both the plain form and the backslash-escaped form a repr() produces are
    replaced, because that is how a credential appears inside an exception
    message that formatted the raw bytes. Secrets shorter than four characters
    are left alone: they are not credentials, and blanking "1" everywhere would
    make a message unreadable for nothing.
    """
    out = str(text)
    for secret in secrets:
        value = str(secret or "")
        if len(value) < 4:
            continue
        for form in (value, value.encode("unicode_escape").decode("ascii", "replace")):
            if form and form in out:
                out = out.replace(form, "***")
    return out
