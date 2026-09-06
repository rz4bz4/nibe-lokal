"""Command line: backup, status, serve."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from .config import load, resolve
from .pump import DASHBOARD, Pump
from .registry import Registry


def build(args) -> tuple[Pump, dict, str]:
    base = os.path.dirname(os.path.abspath(args.config)) or "."
    cfg = load(args.config)
    registry = Registry.load(cfg["model"], resolve(cfg, "register_csv", base))
    pump = Pump(cfg["host"], cfg["port"], cfg["unit"], registry,
                cfg["allow_guarded_writes"], cfg["timeout"])
    return pump, cfg, base


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="nibelokal", description=__doc__)
    ap.add_argument("-c", "--config", default="config.yaml",
                    help="path to config.yaml (default: ./config.yaml)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="log every Modbus exchange")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("backup", help="read every register and save a JSON snapshot")
    p.add_argument("--note", default="", help="a line describing why this snapshot was taken")

    sub.add_parser("status", help="print the dashboard registers")

    p = sub.add_parser("read", help="read specific registers")
    p.add_argument("address", type=int, nargs="+",
                   help="NIBE register addresses, e.g. 30009 40105")

    p = sub.add_parser("search", help="search the register map")
    p.add_argument("needle", help="text to look for in register titles")
    p.add_argument("--writable", action="store_true",
                   help="only registers that can be changed")

    p = sub.add_parser("serve", help="run the web app")
    p.add_argument("--listen", help="bind address, overrides config.yaml")
    p.add_argument("--port", type=int, help="port, overrides config.yaml")

    p = sub.add_parser("diff", help="compare two backup snapshots")
    p.add_argument("old", help="the earlier snapshot")
    p.add_argument("new", help="the later snapshot")

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.cmd == "diff":
        from .diff import print_diff
        return print_diff(args.old, args.new)

    pump, cfg, base = build(args)

    if args.cmd == "backup":
        path = pump.backup(resolve(cfg, "backup_dir", base), args.note)
        with open(path, encoding="utf-8") as fh:
            snap = json.load(fh)
        c = snap["counts"]
        print("Snapshot: %s" % path)
        print("  register map      : %s" % snap["register_map"])
        print("  registers read    : %d of %d (%d failed)" % (c["read"], c["in_map"], c["failed"]))
        print("  settings captured : %d" % c["settings"])
        print("  differ from default: %d" % c["differ_from_default"])
        return 0

    if args.cmd == "status":
        data = pump.read_many(DASHBOARD)
        for addr in DASHBOARD:
            row = data.get(addr)
            if not row:
                continue
            reg = pump.registry.get(addr)
            if "error" in row:
                print("  %-6d %-42s -- %s" % (addr, reg.title[:42], row["error"][:50]))
            else:
                print("  %-6d %-42s %s %s" % (addr, reg.title[:42], row["value"], reg.unit))
        return 0

    if args.cmd == "read":
        data = pump.read_many(args.address)
        for addr in args.address:
            reg = pump.registry.get(addr)
            row = data.get(addr, {})
            name = reg.title if reg else "(not in map)"
            print("  %-6d %-42s %s %s" % (addr, name[:42],
                                          row.get("value", row.get("error", "-")),
                                          reg.unit if reg else ""))
        return 0

    if args.cmd == "search":
        for reg in pump.registry.search(args.needle, args.writable):
            rng = ""
            if reg.min is not None and reg.max is not None:
                rng = " [%g..%g]" % (reg.min, reg.max)
            print("  %-6d %-46s %-8s %s%s" % (reg.address, reg.title[:46],
                                              "write" if reg.writable else "read",
                                              reg.unit, rng))
        return 0

    if args.cmd == "serve":
        from .server import serve
        return serve(pump, cfg, base, args.listen or cfg["listen"],
                     args.port or cfg["listen_port"])
    return 1


def cli() -> int:
    """Entry point that turns the expected failures into one readable line."""
    from .modbus import ModbusError, ModbusOffline
    try:
        return main()
    except (RuntimeError, ModbusOffline, ModbusError) as exc:
        print("nibelokal: %s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(cli())
