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
    "allow_guarded_writes": True,
    # floor | radiators | mixed -- shapes the heating advice, nothing else
    "emitters": "radiators",
    "database": "nibe.db",
    "backup_dir": "backup",
    "timeout": 5.0,
}

INT_KEYS = {"port", "unit", "listen_port", "poll_seconds", "history_days"}
FLOAT_KEYS = {"timeout"}
BOOL_KEYS = {"allow_guarded_writes", "allow_any_host"}


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
                    line = line.split("#", 1)[0].strip()
                    if not line or ":" not in line:
                        continue
                    k, v = line.split(":", 1)
                    loaded[k.strip()] = v.strip()
        for k, v in (loaded or {}).items():
            if v in (None, ""):
                continue
            if k not in cfg:
                # Better a loud warning than a setting that silently does nothing
                # -- a mistyped allowed_hosts is a locked-out phone.
                print("config: ignoring unknown key %r" % k)
                continue
            cfg[k] = _coerce(k, v)

    for k in cfg:
        env = os.environ.get("NIBE_" + k.upper())
        if env:
            cfg[k] = _coerce(k, env)

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
