"""Command line: backup, status, serve."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from .config import load, resolve
from .profile import Profile
from .pump import DASHBOARD, Pump
from .registry import Registry


def build(args) -> tuple[Pump, dict, str]:
    base = os.path.dirname(os.path.abspath(args.config)) or "."
    cfg = load(args.config)
    registry = Registry.load(cfg["model"], resolve(cfg, "register_csv", base))
    # The profile decides how every register number in this app is translated
    # before it reaches that map. Three sources, in order: an explicit
    # `generation`, then the model name, and -- for a `register_csv` with
    # neither, which is a configuration config.example.yaml has always said is
    # complete -- the map itself. A CSV exported from a pump carries no model
    # name, but it does carry the one register that tells the generations
    # apart. Anything that cannot be decided raises, and raises naming both
    # markers: guessing S on an F pump does not fail, it reads plausible
    # nonsense.
    generation = str(cfg.get("generation") or "").strip()
    if not generation and not cfg["model"]:
        generation = _generation_from_map(registry)
    try:
        profile = Profile.for_model(cfg["model"], generation,
                                    cfg.get("word_swap", ""))
    except ValueError as exc:
        raise SystemExit(str(exc))
    pump = Pump(cfg["host"], cfg["port"], cfg["unit"], registry,
                cfg["allow_guarded_writes"], cfg["timeout"],
                profile, cfg.get("framing", "tcp"))
    # Asked here rather than in Pump.__init__: it is one Modbus request, and a
    # constructor that talks to the network is a constructor a test cannot
    # build. It does nothing at all on an S-series pump. See
    # Pump.resolve_word_order.
    pump.resolve_word_order()
    return pump, cfg, base


def _generation_from_map(registry) -> str:
    """S or F, read out of the loaded register map. See profile.py."""
    from .profile import generation_from_addresses
    try:
        return generation_from_addresses(registry.registers, registry.source)
    except ValueError as exc:
        raise SystemExit(str(exc))


def print_identity(pump) -> None:
    """One line saying what the pump says it is. Best effort, never fatal.

    Function 0x2B (Read Device Identification) answers with a vendor, a product
    code and a software version -- "NIBE", "F1245", "5539". It is the cheapest
    cross-check there is that the `model:` in config.yaml is the pump on the
    other end of the wire, and on the F series it is worth more than on the S,
    because there the register map, the pump firmware and the MODBUS 40's own
    firmware are three things that can disagree. It is also the single most
    useful thing an F750 owner can paste into an issue.

    **It is printed last, and it asks last.** It is optional in the Modbus
    specification, and a device may implement "no" by staying silent rather
    than by answering an exception. Asking first then costs `status` a timeout
    -- ten seconds and a dropped socket, with the ordinary timeout and the
    ordinary retry -- before a single register the command was actually run for
    is read. `modbus.device_id` asks once with a one-second timeout for the same
    reason. The registers are the point of the command; this is a bonus, and a
    bonus goes at the end where it cannot cost anything but its own second.
    """
    from .modbus import ModbusError, ModbusOffline
    try:
        info = pump.mb.device_id()
    except (ModbusError, ModbusOffline, OSError, ValueError) as exc:
        print("  device id: not answered (%s)" % str(exc).split(" (")[0])
        return
    except Exception as exc:                                  # noqa: BLE001
        # A malformed answer is not a reason to fail a command whose job is to
        # read registers. Say what happened and get on with it.
        print("  device id: unreadable (%s)" % exc)
        return
    named = [info.get(k) for k in ("vendor", "product", "revision")]
    line = " ".join(str(v) for v in named if v)
    extra = {n: t for n, t in info.get("objects", {}).items() if n > 2}
    if extra:
        line += " " + " ".join("[%d] %s" % (n, t) for n, t in sorted(extra.items()))
    print("  device id: %s" % (line or "answered, but named nothing"))
    model = (getattr(pump.profile, "model", "") or "").upper()
    product = str(info.get("product") or "").upper().replace(" ", "").replace("-", "")
    if model and product and model not in product and product not in model:
        # Not fatal, and not a guess about which of the two is right: a
        # mismatch between the configured map and the pump's own answer is
        # exactly the thing that reads plausible nonsense, so it is said out
        # loud and left to a person.
        print("             note: config.yaml says model %s and the pump says "
              "%s. One of the two is wrong, and a mismatched register map does "
              "not fail -- it reads plausible nonsense."
              % (pump.profile.model, info.get("product")))


def print_backup_estimate(pump) -> None:
    """On an F-series pump, say how long this is going to take. Before it does.

    A backup is one request per register, and on an F pump NIBE gives a request
    outside the LOG.SET file 2.1 s. A 646-register F750 map is therefore twenty
    minutes or more of a command that prints nothing until it finishes, on a bus
    that answers nothing else meanwhile. A person who knows that waits; a person
    who does not presses Ctrl-C at four minutes and files a bug.

    Nothing on the S series, where the same snapshot is a few seconds and a line
    about it would be noise.
    """
    if pump.profile.generation != "F":
        return
    from .config import F_SECONDS_PER_REGISTER
    count = len(pump.registry)
    minutes = count * F_SECONDS_PER_REGISTER / 60.0
    print("Reading %d registers, one request each. On an F-series pump NIBE "
          "allows one" % count)
    print("register per request with a %.1f s timeout, so expect roughly %d "
          "minutes." % (F_SECONDS_PER_REGISTER, round(minutes)))
    print("The pump answers nothing else while this runs.")


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
        print_backup_estimate(pump)
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
            reg = pump.register(addr)
            if reg is None:
                continue
            if "error" in row:
                print("  %-6d %-42s -- %s" % (addr, reg.title[:42], row["error"][:50]))
            else:
                print("  %-6d %-42s %s %s" % (addr, reg.title[:42], row["value"], reg.unit))
        # Last, and optional. See print_identity.
        print_identity(pump)
        return 0

    if args.cmd == "read":
        import textwrap
        data = pump.read_many(args.address)
        for addr in args.address:
            reg = pump.register(addr)
            row = data.get(addr, {})
            # "(not in map)" is right for a register this pump does not have and
            # wrong for one this app refuses to translate. The second kind has a
            # reason written down in profile.NO_F_EQUIVALENT -- often that the
            # number IS a register here, holding something else entirely -- and
            # printing "(not in map)" for it sends somebody looking through
            # their own documentation for a register that is right there.
            why = pump.profile.why_unavailable(addr) if reg is None else ""
            name = reg.title if reg else ("(no %s-series equivalent)"
                                          % pump.profile.generation
                                          if why else "(not in map)")
            print("  %-6d %-42s %s %s" % (addr, name[:42],
                                          row.get("value", row.get("error", "-")),
                                          reg.unit if reg else ""))
            if why:
                print(textwrap.fill(why, width=78, initial_indent=" " * 9,
                                    subsequent_indent=" " * 9))
        return 0

    if args.cmd == "search":
        # The map is keyed by physical addresses, so that is what a hit is
        # numbered by -- and on an F-series pump that is the number in the
        # owner's own documentation. Where this app calls it something else,
        # the canonical number is printed after it, because that is the one
        # `nibelokal read` and the web app take.
        for reg in pump.registry.search(args.needle, args.writable):
            rng = ""
            if reg.min is not None and reg.max is not None:
                rng = " [%g..%g]" % (reg.min, reg.max)
            canonical = pump.profile.canonical(reg.address)
            also = "" if canonical == reg.address else "  (app: %d)" % canonical
            print("  %-6d %-46s %-8s %s%s%s" % (reg.address, reg.title[:46],
                                                "write" if reg.writable else "read",
                                                reg.unit, rng, also))
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
