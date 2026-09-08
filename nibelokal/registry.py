"""The register map: names, types, scaling, enum labels, min/max.

Two sources, in order of authority:

1. A CSV of your own pump's registers. This is the only map guaranteed to match
   your unit and its firmware. Point `register_csv` at it in config.yaml. Where
   it comes from depends on the generation, and the two files do not look alike:
   an S-series pump exports its own (menu 7.5.9 -> "Export all registers" onto a
   USB stick), while an F-series pump has no register export in its USB menu at
   all and the file comes from NIBE's Windows tool ModbusManager instead (File
   -> Export to file). `from_csv` reads both; see it for how they differ.
2. The `nibe` python package (pip install nibe), which ships the same maps that
   the Home Assistant integration uses. Convenient, but a model/firmware
   mismatch shows up as registers that read plausible nonsense.

Both are normalised into the same Register objects, so the rest of the app does
not care which one you used.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import math
import os
import re
import struct
from dataclasses import dataclass, field

log = logging.getLogger("nibelokal.registry")

# The full span of each type. encode() refuses anything outside it rather than
# masking, because a masked write is a wrong value the pump happily accepts.
LIMITS = {
    "s8": (-0x80, 0x7F),
    "u8": (0, 0xFF),
    "s16": (-0x8000, 0x7FFF),
    "u16": (0, 0xFFFF),
    "s32": (-0x80000000, 0x7FFFFFFF),
    "u32": (0, 0xFFFFFFFF),
}

# NIBE's own CSV export codes the variable size as a digit. Same mapping the
# nibe package's convert_csv.py uses.
CSV_SIZES = {"1": "s8", "2": "s16", "3": "s32", "4": "u8", "5": "u16", "6": "u32"}

#: The column that holds the register number, under every name the two exports
#: give it. NIBE's own USB export calls it "Register" and puts an offset in it;
#: ModbusManager calls it "ID" and puts the whole coil address in it.
ID_COLUMNS = ("register", "id", "register number")

#: Every column name `from_csv` knows, used only to find the header row in a
#: file that has a preamble above it. Not a schema: a column that is not here
#: is ignored, as it always was.
KNOWN_COLUMNS = frozenset(ID_COLUMNS) | {
    "title", "name", "info", "unit", "mode", "r/w", "access",
    "register type", "type", "registertype",
    "size of variable", "size", "division factor", "factor", "divisor",
    "min value", "min", "max value", "max", "default value", "default",
}

#: At or above this, a number in the register column is a whole coil address
#: rather than an offset into one of the two address spaces.
#:
#: It cannot be ambiguous. Coil addresses run to 65534 and the higher of the two
#: bases is 40001, so the largest offset any export can carry is 25533 -- eleven
#: thousand short of the lowest address (30001, the first input register). See
#: `from_csv`.
FULL_ADDRESS_FLOOR = 30001

# Sentinels the pump uses for "no value" (matches the nibe package's convention).
INVALID = {
    "u8": 0xFF,
    "s8": -0x80,
    "u16": 0xFFFF,
    "s16": -0x8000,
    "u32": 0xFFFFFFFF,
    "s32": -0x80000000,
}


@dataclass
class Register:
    address: int          # NIBE coil address, e.g. 30009 or 40226
    title: str
    size: str             # s8 u8 s16 u16 s32 u32
    factor: int = 1       # real value = raw / factor
    unit: str = ""
    writable: bool = False
    min: float | None = None
    max: float | None = None
    default: float | None = None
    mappings: dict[str, str] = field(default_factory=dict)   # {"0": "SMALL", ...}
    info: str = ""
    #: Which 16-bit word of a 32-bit value sits at the lower address. Only
    #: 32-bit registers are affected; every 16-bit one reads the same either
    #: way, which is exactly what makes the wrong setting hard to spot.
    #:
    #: Nothing sets this per register: `Registry.set_word_order` sets it on
    #: every register at once, from the profile, and `Pump.__init__` is the one
    #: caller. The default is True -- low word first, NIBE's S-series TIF, and
    #: what this app has always done -- so a Register built by hand in a test
    #: behaves as it did. See nibelokal/profile.py, LOW_WORD_FIRST.
    low_word_first: bool = True

    def __post_init__(self):
        # min/max/default come out of the register map as RAW values, while
        # every value this app handles is already divided by `factor`. Scale
        # them once here, or the range check compares 51.0 against 530 and
        # rejects perfectly legal settings while letting absurd ones through.
        if self.factor not in (0, 1):
            for attr in ("min", "max", "default"):
                v = getattr(self, attr)
                if v is not None:
                    setattr(self, attr, v / self.factor)

    # -- derived --------------------------------------------------------

    @property
    def kind(self) -> int:
        """3 = input register (FC04), 4 = holding register (FC03/FC16)."""
        return self.address // 10000

    @property
    def wire(self) -> int:
        """The address actually put on the wire."""
        return (self.address % 10000) - 1

    @property
    def count(self) -> int:
        return 2 if self.size in ("s32", "u32") else 1

    @property
    def signed(self) -> bool:
        return self.size.startswith("s")

    # -- codec ----------------------------------------------------------

    def decode(self, regs: list[int]):
        """Raw 16-bit words -> real value (or None when the pump says 'no value')."""
        if self.count == 2:
            # Which half is which is not a constant of Modbus, it is a setting
            # of the pump -- see low_word_first above and profile.py.
            raw = (regs[0] | (regs[1] << 16) if self.low_word_first
                   else (regs[0] << 16) | regs[1])
            if self.signed:
                raw = struct.unpack(">i", struct.pack(">I", raw & 0xFFFFFFFF))[0]
        else:
            raw = regs[0] & 0xFFFF
            if self.signed:
                raw = struct.unpack(">h", struct.pack(">H", raw))[0]
                if self.size == "s8":
                    raw = struct.unpack(">b", struct.pack(">B", raw & 0xFF))[0]
            elif self.size == "u8":
                raw = raw & 0xFF

        limit = INVALID.get(self.size)
        if limit is not None:
            if self.signed and raw <= limit:
                return None
            if not self.signed and raw >= limit:
                return None

        value = raw / self.factor if self.factor not in (0, 1) else raw
        if self.mappings:
            return self.mappings.get(str(int(raw)), value)
        return value

    def coerce(self, value):
        """Normalise an incoming value to the number this register takes.

        A missing value is a bad request, not a server error -- without this it
        surfaced as a TypeError and a 502 with a stack trace in the log.

        For an enum register both the label ("Large") and the key (2) are
        accepted, and anything that is not one of its keys is rejected -- the
        map's min/max cannot be relied on to catch that.
        """
        if value is None:
            raise ValueError("%s: a value is required" % self.title)
        if isinstance(value, (int, float)) and not isinstance(value, bool) \
                and not math.isfinite(value):
            # json.loads("1e999") is float("inf"), and int(round(inf)) is an
            # OverflowError -- which escaped encode() as a 502 with a stack
            # trace. A value the register cannot hold is a bad request.
            raise ValueError("%s: %r is not a number this register can hold"
                             % (self.title, value))
        if self.mappings:
            if isinstance(value, str) and not value.strip().lstrip("-").isdigit():
                for k, v in self.mappings.items():
                    if v.lower() == value.strip().lower():
                        return int(k)
                raise ValueError(
                    "%r is not a valid value for %s. Allowed: %s"
                    % (value, self.title, ", ".join(sorted(self.mappings.values())))
                )
            try:
                key = int(float(value))
            except (OverflowError, ValueError):
                raise ValueError("%r is not a valid value for %s"
                                 % (value, self.title))
            if str(key) not in self.mappings:
                raise ValueError(
                    "%s is not a valid value for %s. Allowed: %s"
                    % (key, self.title,
                       ", ".join("%s (%s)" % (k, v) for k, v in sorted(self.mappings.items())))
                )
            return key
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("%s: %r is not a number this register can hold"
                             % (self.title, value))
        return number

    def encode(self, value) -> list[int]:
        """Real value -> raw 16-bit words, ready for FC16.

        Refuses values the type cannot hold. Masking instead would turn -1
        minutes of extra hot water into 65535 -- a write the pump accepts and
        then runs for six weeks.
        """
        try:
            raw = int(round(float(value) * (self.factor if self.factor else 1)))
        except (OverflowError, TypeError, ValueError):
            # int(round(inf)) is an OverflowError, and OverflowError is not a
            # ValueError -- so it went out of the web app as a 502 blaming the
            # pump for what is a bad request. coerce() catches this first on
            # every path the server takes, but encode() is public and a
            # register is not the place to find out that a caller skipped it.
            raise ValueError("%s: %r is not a number this register can hold"
                             % (self.title, value))
        lo, hi = LIMITS.get(self.size, (-0x8000, 0x7FFF))
        if not lo <= raw <= hi:
            raise ValueError(
                "%s: %s does not fit in a %s register (allowed %s..%s before scaling)"
                % (self.title, value, self.size, lo / (self.factor or 1), hi / (self.factor or 1))
            )
        if self.count == 2:
            raw &= 0xFFFFFFFF
            # The same order the value was read in. A write that used the
            # other one would be the read bug with the consequences reversed:
            # a plausible number on the page and nonsense in the pump.
            if self.low_word_first:
                return [raw & 0xFFFF, (raw >> 16) & 0xFFFF]
            return [(raw >> 16) & 0xFFFF, raw & 0xFFFF]
        return [raw & 0xFFFF]

    def as_dict(self) -> dict:
        return {
            "address": self.address,
            "title": self.title,
            "size": self.size,
            "factor": self.factor,
            "unit": self.unit,
            "writable": self.writable,
            "min": self.min,
            "max": self.max,
            "default": self.default,
            "mappings": self.mappings or None,
        }


class Registry:
    def __init__(self, registers: dict[int, Register], source: str):
        self.registers = registers
        self.source = source
        #: See set_word_order. True is what every map has meant until now.
        self.low_word_first = True

    def __len__(self) -> int:
        return len(self.registers)

    def set_word_order(self, low_first: bool) -> None:
        """Say which 16-bit word of a 32-bit value comes first, once.

        The order is a property of the pump, not of any one register, and the
        pump is what the profile knows about -- so it is decided in
        `Pump.__init__` and applied here to every register at once. Doing it
        per register at each call site would be two places that have to agree
        about the same fact, which is how a read and a write end up using
        opposite orders. See nibelokal/profile.py.
        """
        self.low_word_first = bool(low_first)
        for reg in self.registers.values():
            reg.low_word_first = self.low_word_first

    def get(self, address: int) -> Register | None:
        return self.registers.get(address)

    def search(self, needle: str, writable_only: bool = False) -> list[Register]:
        q = needle.lower()
        out = [
            r for r in self.registers.values()
            if q in r.title.lower() and (r.writable or not writable_only)
        ]
        return sorted(out, key=lambda r: r.address)

    def all(self) -> list[Register]:
        return sorted(self.registers.values(), key=lambda r: r.address)

    # -- loaders --------------------------------------------------------

    @classmethod
    def from_package(cls, model: str) -> "Registry":
        try:
            from nibe.heatpump import Model
        except ImportError as exc:
            raise RuntimeError(
                "The `nibe` package is not installed and no register_csv was given. "
                "Either `pip install nibe`, or export the register list from your pump "
                "(menu 7.5.9 -> Export all registers) and point register_csv at the CSV."
            ) from exc

        try:
            model_enum = Model[model.upper()]
        except KeyError:
            raise RuntimeError(
                "Unknown model %r. The nibe package knows: %s"
                % (model, ", ".join(m.name for m in Model))
            )
        # get_coil_data() merges the model's own map with the shared extensions,
        # which is what the Home Assistant integration reads too.
        raw = model_enum.get_coil_data()

        regs: dict[int, Register] = {}
        for addr, d in raw.items():
            address = int(addr)
            regs[address] = Register(
                address=address,
                title=d.get("title") or d.get("name") or str(address),
                size=d.get("size", "s16"),
                factor=int(d.get("factor", 1) or 1),
                unit=d.get("unit") or "",
                writable=bool(d.get("write", False)),
                min=d.get("min"),
                max=d.get("max"),
                default=d.get("default"),
                mappings=d.get("mappings") or {},
                info=d.get("info") or "",
            )
        return cls(regs, "nibe package, model %s" % model_enum.name)

    @classmethod
    def from_csv(cls, path: str) -> "Registry":
        """Parse a register export: the pump's own, or ModbusManager's.

        Two shapes, and an F-series owner can only produce the second one.

        * **The pump's own USB export** (S series, menu 7.5.9 -> "Export all
          registers"). One column of *offsets* -- 8, 225, 1087 -- and a
          "Register type" column saying which address space each belongs to, so
          the coil address is 30001 or 40001 plus the offset.
        * **ModbusManager -> File -> Export to file** (the F series). NIBE's own
          Windows tool, and the only route to a register list on a pump that has
          no register export in its USB menu. Its ID column holds the *whole*
          coil address -- 40004, 47007 -- and a "Mode" column of R or R/W says
          whether it may be written. It also writes four lines of preamble
          (tool version, date, product, database) before the header row.

        Telling the two apart is one comparison and does not need a flag: an
        offset cannot reach 30001, because the coil addresses it is an offset
        into stop at 65534 and the larger of the two bases is 40001, so the
        largest offset any export can carry is 25533. Anything at or above
        30001 is therefore already a coil address. Getting this wrong is not a
        subtle failure -- it puts every register 30001 or 40001 places from
        where it belongs -- but it is a silent one, which is why it is decided
        by arithmetic rather than by which tool the file looks like it came
        from.
        """
        regs: dict[int, Register] = {}
        # The pump writes these in latin-1 (the degree sign gives it away);
        # ModbusManager and some tools re-save them as UTF-8.
        for encoding in ("utf-8-sig", "latin-1"):
            try:
                with open(path, encoding=encoding, newline="") as probe:
                    text = probe.read()
                break
            except UnicodeDecodeError:
                continue
        # Sniffed and read from the header row down, not from the top of the
        # file: on a ModbusManager export the top of the file is "ModbusManager
        # 1.0.9", which the sniffer reads as a one-column comma-separated
        # header and DictReader then treats as the column names.
        body = _csv_from_header(text)
        try:
            dialect = csv.Sniffer().sniff(body[:4096], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        for row in csv.DictReader(io.StringIO(body), dialect=dialect):
            low = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}

            def pick(*names, default=""):
                for n in names:
                    if low.get(n):
                        return low[n]
                return default

            num = pick(*ID_COLUMNS)
            if not num.lstrip("-").isdigit():
                continue
            # NIBE's own export names this "Register type" with values like
            # MODBUS_HOLDING_REGISTER. ModbusManager instead carries a
            # "Mode" column of R / R/W. Accept both, and treat writability
            # as the tell -- getting this wrong puts every register in the
            # other address space, where it reads garbage.
            rtype = pick("register type", "type", "registertype").lower()
            mode = pick("mode", "r/w", "access").lower()
            holding = "hold" in rtype or "w" in mode.replace("write", "w")
            offset = int(num)
            if offset >= FULL_ADDRESS_FLOOR:
                address = offset
                # The address space is the address's own first digit here,
                # and it outranks the Mode column: a 3xxxx input register is
                # read-only whatever a column says, and there is no FC to
                # write one with.
                holding = holding and address // 10000 == 4
            else:
                address = (40001 if holding else 30001) + offset
            size = pick("size of variable", "size", default="s16").lower()
            # NIBE's export writes the size as a digit 1-6, not as "s16".
            size = CSV_SIZES.get(size, size if size in LIMITS else "s16")
            factor = pick("division factor", "factor", "divisor", default="1")
            lo = _num(pick("min value", "min"))
            hi = _num(pick("max value", "max"))
            # The export uses min == max (usually 0) to mean "no range given".
            # Kept as-is they would refuse every write of anything else.
            if lo is not None and hi is not None and lo == hi:
                lo = hi = None
            regs[address] = Register(
                address=address,
                title=pick("title", "name", default=str(address)),
                size=size,
                factor=int(float(factor)) if factor.replace(".", "").isdigit() else 1,
                unit=pick("unit"),
                writable=holding,
                min=lo,
                max=hi,
                default=_num(pick("default value", "default")),
                # ModbusManager exports an "Info" column, the same sentence the
                # `nibe` package carries. The pump's own export has none, so
                # this is "" there, exactly as it was.
                info=pick("info"),
            )
        if not regs:
            raise RuntimeError("No registers parsed from %s - is it the pump's own export?" % path)
        return cls(regs, "CSV exported from the pump: %s" % os.path.basename(path))

    @classmethod
    def load(cls, model: str, csv_path: str | None = None) -> "Registry":
        if csv_path:
            if not os.path.exists(csv_path):
                # Falling back to the package map here would be the worst kind of
                # quiet: the map decides which wire address a *write* goes to.
                raise RuntimeError(
                    "register_csv points at %r, which does not exist. Fix the path, "
                    "or clear register_csv to use the map for `model` instead."
                    % csv_path
                )
            return cls.from_csv(csv_path)
        return cls.from_package(model)


def _csv_from_header(text: str) -> str:
    """`text` from its column-header row down, dropping any preamble above it.

    ModbusManager's "Export to file" writes four lines before the header --
    the tool version, a date, the product name and the database number -- and
    csv.DictReader has no notion of a preamble: it takes the first line it is
    given as the column names, which on such a file is "ModbusManager 1.0.9".
    The pump's own USB export has no preamble, and there the header is the
    first line, so this returns it unchanged.

    A line counts as the header when it names the register column and at least
    one other column this parser knows. Both conditions, because "id" alone is
    a word that could appear in a title.
    """
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        cells = {c.strip().strip('"').strip("'").lower()
                 for c in re.split(r"[,;\t]", line)}
        if cells & set(ID_COLUMNS) and len(cells & KNOWN_COLUMNS) >= 2:
            return "".join(lines[i:])
    # No recognisable header. Left exactly as it was, so the existing failure
    # -- "No registers parsed from ..." -- is what the caller sees, rather than
    # an empty string and a different, less helpful one.
    return text


def _num(s: str):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None
