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
    # The pump's own register list. On the S series that is the export in menu
    # 7.5.9; on the F series the pump has none and it comes from NIBE's
    # ModbusManager instead. See nibelokal/registry.py, which reads both.
    "register_csv": "",
    # S or F. Empty means "work it out", which is what almost everyone wants
    # and what happens by default: from `model` when there is one, and
    # otherwise from the register map itself -- 40027 is the heating curve on
    # an S and 47007 on an F, which partitions every map there is. It has to
    # stay settable for the map that answers neither or both, because the
    # generation decides how every register number in this app is translated
    # and guessing it reads plausible nonsense. See nibelokal/profile.py.
    "generation": "",
    # tcp (Modbus TCP with an MBAP header, what an S-series pump speaks over
    # Ethernet) or rtu (a raw Modbus RTU frame with a CRC16, what a
    # transparent RS485-to-Ethernet gateway in front of an F-series pump's
    # MODBUS 40 accessory forwards). See nibelokal/modbus.py.
    "framing": "tcp",
    # Which of the two 16-bit registers of a 32-bit value comes first. Empty
    # means: ask the pump. On the S series there is nothing to ask -- the order
    # is NIBE's own TIF and not a setting -- so it is the low word first. On the
    # F series it IS a setting, the pump's menu 5.3.11, and it is also register
    # 48852, so the app reads that at startup and follows it; the default when
    # it does not answer is the low word first too, because 48852's documented
    # factory value is 1 ("swapping the words") while the MODBUS 40 manual's
    # prose says the opposite. It has to stay settable because get it wrong and
    # every 32-bit register reads a large, stable-looking nonsense number while
    # every 16-bit one is perfect. See nibelokal/profile.py.
    "word_swap": "",
    "listen": "127.0.0.1",      # bind address for the web app
    "listen_port": 8377,
    "auth_token": "",           # empty = no token required
    "allowed_hosts": "",        # extra Host names to accept, space separated
    "allow_any_host": False,    # only if something else already checks Host
    "poll_seconds": 60,         # NIBE's own guidance is not to poll harder
    "history_days": 400,
    # Hours between automatic full-settings snapshots. 0 turns them off.
    #
    # None, and not 24, because the right answer depends on the generation and
    # nothing here knows which one this is: a snapshot of an S-series pump is a
    # few seconds of Modbus TCP, and the same snapshot through a MODBUS 40 is
    # 23-34 minutes of one-register-per-request that the polling thread holds
    # the bus for. `auto_backup_hours()` below turns the None into 24 on an S
    # pump and 0 on an F one; a number written in config.yaml is used on either.
    "auto_backup_hours": None,
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
            # The key is checked before the value, and that order is the point.
            # The other way round -- which is how this read until 2026-09-08 --
            # an unknown key with an empty value was skipped as "not set" and
            # never reported, so `genration: ""` in a config file was silent in
            # both directions: no warning about the typo, and no generation.
            # A key nobody recognises is worth saying so about whatever it
            # holds.
            if k not in cfg:
                # Better a loud warning than a setting that silently does nothing
                # -- a mistyped allowed_hosts is a locked-out phone.
                print("config: ignoring unknown key %r" % k)
                continue
            if v in (None, ""):
                # A known key left empty means "use the default", and every
                # empty-means-unset key in DEFAULTS depends on that.
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
    generation = str(cfg.get("generation") or "").strip().upper()
    if generation and generation not in ("S", "F"):
        raise SystemExit(
            "`generation` must be S, F or left empty (got %r).\n\n"
            "S is the 2021 platform: S735, S1155, S1255, S320, SMO S40, VVM S320.\n"
            "F is everything before it: F750, F1155, SMO 20/40, VVM 225/320/500.\n\n"
            "Leave it empty to work it out from `model`." % cfg["generation"]
        )
    # A register_csv on its own is a complete configuration, and
    # config.example.yaml has said so since before the generations split:
    # "required unless register_csv is set". It carries no model name, so the
    # generation is derived from the map itself -- profile.generation_from_
    # addresses, the same 40027-or-47007 test the model lists are partitioned
    # by -- in __main__.build(), where the map has actually been loaded. A map
    # that answers neither or both fails there, loudly, naming both markers.
    # Refusing here instead turned a working configuration into a startup
    # error, which is a regression against the released behaviour.
    try:
        from .profile import parse_word_swap
        parse_word_swap(cfg.get("word_swap"))
    except ValueError as exc:
        # Refused here rather than at the first 32-bit read, which is a value
        # on a page that looks like a number and is not.
        raise SystemExit(
            "%s\n\n"
            "Leave it empty unless you have looked at the pump's menu 5.3.11 "
            "and know what it says." % exc
        )
    if str(cfg.get("framing") or "tcp").strip().lower() not in ("tcp", "rtu"):
        raise SystemExit(
            "`framing` must be tcp or rtu (got %r).\n\n"
            "tcp is Modbus TCP, which is what an S-series pump speaks over Ethernet "
            "and what a\nprotocol-converting RS485 gateway presents. rtu is a raw "
            "Modbus RTU frame over the\nsame socket, which is what a *transparent* "
            "serial bridge in front of an F-series\npump's MODBUS 40 forwards."
            % cfg["framing"]
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


#: What an unset `auto_backup_hours` means on an S-series pump. Its own
#: constant only so that the number in config.example.yaml, the number in the
#: README and the number here are one number.
DEFAULT_AUTO_BACKUP_HOURS = 24.0

#: NIBE's documented maximum timeout for one register outside a MODBUS 40's
#: LOG.SET file, in seconds, and -- users on the LogicMachine forum say -- the
#: real spacing between requests rather than a ceiling that is rarely reached.
#: One request per register, so this times the size of the map is what a backup
#: costs on an F pump. See docs/f-series.md.
F_SECONDS_PER_REGISTER = 2.1


def auto_backup_hours(cfg: dict, generation: str) -> float:
    """Hours between automatic snapshots, resolved for this generation.

    A number in config.yaml is used as it stands, on either generation: an
    owner who has asked for daily snapshots on an F pump knows what they are
    asking for, and this is not the place to overrule them.

    Unset is where the generations part. On an S-series pump a full snapshot is
    a few seconds of Modbus TCP and a daily one is free, which is what this app
    has always done. On an F-series pump the same snapshot is every register in
    the map at one register per request and 2.1 s per request -- 23 to 34
    minutes for the 646-register F750 map -- taken by the polling thread, which
    holds the pump's only bus for all of it. Every poll in that window is
    skipped, the dashboard goes stale for half an hour, and an alarm raised
    during it is noticed when the backup finishes. A daily backup is worth
    having; it is not worth having by default at that price, on a transport
    nobody has measured. So on an F pump it is off until it is asked for, and
    config.example.yaml says so where somebody will read it.
    """
    value = cfg.get("auto_backup_hours")
    if value is None or value == "":
        return 0.0 if generation == "F" else DEFAULT_AUTO_BACKUP_HOURS
    return float(value)


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
