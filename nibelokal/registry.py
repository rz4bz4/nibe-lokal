"""The register map: names, types, scaling, enum labels, min/max.

Two sources, in order of authority:

1. A CSV exported from your own pump (menu 7.5.9 -> "Export all registers" onto
   a USB stick). This is the only map guaranteed to match your unit and its
   firmware. Point `register_csv` at it in config.yaml.
2. The `nibe` python package (pip install nibe), which ships the same maps that
   the Home Assistant integration uses. Convenient, but a model/firmware
   mismatch shows up as registers that read plausible nonsense.

Both are normalised into the same Register objects, so the rest of the app does
not care which one you used.
"""
from __future__ import annotations

import csv
import json
import logging
import os
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
            raw = regs[0] | (regs[1] << 16)          # low word first
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

        For an enum register both the label ("Large") and the key (2) are
        accepted, and anything that is not one of its keys is rejected -- the
        map's min/max cannot be relied on to catch that.
        """
        if self.mappings:
            if isinstance(value, str) and not value.strip().lstrip("-").isdigit():
                for k, v in self.mappings.items():
                    if v.lower() == value.strip().lower():
                        return int(k)
                raise ValueError(
                    "%r is not a valid value for %s. Allowed: %s"
                    % (value, self.title, ", ".join(sorted(self.mappings.values())))
                )
            key = int(float(value))
            if str(key) not in self.mappings:
                raise ValueError(
                    "%s is not a valid value for %s. Allowed: %s"
                    % (key, self.title,
                       ", ".join("%s (%s)" % (k, v) for k, v in sorted(self.mappings.items())))
                )
            return key
        return float(value)

    def encode(self, value) -> list[int]:
        """Real value -> raw 16-bit words, ready for FC16.

        Refuses values the type cannot hold. Masking instead would turn -1
        minutes of extra hot water into 65535 -- a write the pump accepts and
        then runs for six weeks.
        """
        raw = int(round(float(value) * (self.factor if self.factor else 1)))
        lo, hi = LIMITS.get(self.size, (-0x8000, 0x7FFF))
        if not lo <= raw <= hi:
            raise ValueError(
                "%s: %s does not fit in a %s register (allowed %s..%s before scaling)"
                % (self.title, value, self.size, lo / (self.factor or 1), hi / (self.factor or 1))
            )
        if self.count == 2:
            raw &= 0xFFFFFFFF
            return [raw & 0xFFFF, (raw >> 16) & 0xFFFF]
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

    def __len__(self) -> int:
        return len(self.registers)

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
        """Parse a CSV exported from the pump itself (menu 7.5.9)."""
        regs: dict[int, Register] = {}
        # The pump writes these in latin-1 (the degree sign gives it away);
        # ModbusManager and some tools re-save them as UTF-8.
        for encoding in ("utf-8-sig", "latin-1"):
            try:
                with open(path, encoding=encoding, newline="") as probe:
                    probe.read()
                break
            except UnicodeDecodeError:
                continue
        with open(path, encoding=encoding, newline="") as fh:
            sample = fh.read(4096)
            fh.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
            except csv.Error:
                dialect = csv.excel
            for row in csv.DictReader(fh, dialect=dialect):
                low = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}

                def pick(*names, default=""):
                    for n in names:
                        if low.get(n):
                            return low[n]
                    return default

                num = pick("register", "id", "register number")
                if not num.lstrip("-").isdigit():
                    continue
                # NIBE's own export names this "Register type" with values like
                # MODBUS_HOLDING_REGISTER. Other exports (and ModbusManager)
                # instead carry a "Mode" column of R / R/W. Accept both, and
                # treat writability as the tell -- getting this wrong puts every
                # register in the other address space, where it reads garbage.
                rtype = pick("register type", "type", "registertype").lower()
                mode = pick("mode", "r/w", "access").lower()
                holding = "hold" in rtype or "w" in mode.replace("write", "w")
                base = 40001 if holding else 30001
                address = base + int(num)
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


def _num(s: str):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None
