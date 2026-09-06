"""Minimal Modbus TCP client for NIBE S-series heat pumps.

Why not pymodbus/async_modbus: they set socket.TCP_KEEPIDLE, which does not
exist on macOS, and they make NIBE's documented limits (max 20 registers per
query, max 100 registers per second) awkward to respect. This is stdlib only
and does exactly what the pump allows.

Addressing follows NIBE's own convention, as used by the `nibe` package:
    input register   (read only)  : coil 3xxxx -> wire address coil - 30001, FC04
    holding register (read/write) : coil 4xxxx -> wire address coil - 40001, FC03/FC16

32-bit values occupy two registers, low word FIRST -- NIBE TIF EN 2608 p.6:
"when reading multiple registers, the registers are shown in reverse order".
"""
from __future__ import annotations

import logging
import socket
import struct
import threading
import time

log = logging.getLogger("nibelokal.modbus")

# NIBE documents: max 20 registers per query, max 100 registers per second.
MAX_REGS_PER_QUERY = 20
MAX_REGS_PER_SECOND = 100

# Exception 1 means two very different things depending on what you were doing,
# so the text is chosen by context rather than baked in here. Measured: this pump
# answers 1, not the textbook 2, for registers it simply does not implement.
EXCEPTIONS = {
    1: "Illegal function",
    2: "Illegal data address - this register does not exist on this pump.",
    3: "Illegal data value - the value is outside the register's allowed range.",
    4: "Slave device failure.",
    5: "Acknowledge - the pump accepted the request but needs more time.",
    6: "Slave device busy - back off and retry.",
}


class ModbusError(Exception):
    """The pump answered, but with an exception response."""

    def __init__(self, code: int, context: str = ""):
        self.code = code
        msg = EXCEPTIONS.get(code, "Modbus exception %d" % code)
        if code == 1:
            if context.startswith("writing"):
                msg += (" - the pump refused the write. Most likely 'Reading Modbus only' "
                        "is switched on in the pump's menu 7.5.9.")
            else:
                msg += " - this pump does not implement that register."
        super().__init__(msg + ((" (" + context + ")") if context else ""))


class ModbusOffline(Exception):
    """Could not reach the pump at all."""


class _Budget:
    """Sliding-window rate limiter, registers per second."""

    def __init__(self, limit: int = MAX_REGS_PER_SECOND):
        self.limit = limit
        self._events: list[tuple[float, int]] = []

    def spend(self, n: int) -> None:
        now = time.monotonic()
        self._events = [(t, c) for t, c in self._events if now - t < 1.0]
        used = sum(c for _, c in self._events)
        if used + n > self.limit:
            oldest = self._events[0][0] if self._events else now
            sleep = max(0.0, 1.0 - (now - oldest))
            if sleep:
                time.sleep(sleep)
            now = time.monotonic()
            self._events = [(t, c) for t, c in self._events if now - t < 1.0]
        self._events.append((now, n))


class ModbusTCP:
    """One long-lived TCP session, serialised requests, auto-reconnect."""

    def __init__(self, host: str, port: int = 502, unit: int = 1, timeout: float = 5.0):
        self.host = host
        self.port = port
        self.unit = unit
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._tid = 0
        self._lock = threading.RLock()
        self._budget = _Budget()

    # -- connection ------------------------------------------------------

    def _connect(self) -> socket.socket:
        if self._sock is not None:
            return self._sock
        try:
            s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            raise ModbusOffline(
                "Cannot reach the heat pump at %s:%d (%s). Most common cause: Modbus "
                "TCP is not enabled. Turn it on from the pump's display, menu 7.5.9 "
                "Modbus TCP/IP." % (self.host, self.port, exc)
            ) from exc
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = s
        return s

    def close(self) -> None:
        with self._lock:
            self._drop()

    def _drop(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    # -- wire ------------------------------------------------------------

    def _recv_exactly(self, sock: socket.socket, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ModbusOffline("The pump closed the connection.")
            buf += chunk
        return buf

    def _transact(self, pdu: bytes, cost: int, context: str) -> bytes:
        """Send one PDU, return the response PDU. Retries once on a dropped socket."""
        self._budget.spend(cost)
        with self._lock:
            last: Exception | None = None
            for attempt in (1, 2):
                sock = self._connect()
                self._tid = (self._tid + 1) % 0x10000
                header = struct.pack(">HHHB", self._tid, 0, len(pdu) + 1, self.unit)
                try:
                    sock.sendall(header + pdu)
                    head = self._recv_exactly(sock, 7)
                    _tid, _proto, length, _unit = struct.unpack(">HHHB", head)
                    body = self._recv_exactly(sock, length - 1)
                except (OSError, ModbusOffline) as exc:
                    last = exc
                    self._drop()
                    if attempt == 2:
                        raise ModbusOffline(
                            "Lost the connection to %s:%d (%s)." % (self.host, self.port, exc)
                        ) from exc
                    time.sleep(0.25)
                    continue
                if body[0] & 0x80:
                    raise ModbusError(body[1], context)
                return body
        raise ModbusOffline(str(last))

    # -- public ----------------------------------------------------------

    def read(self, kind: int, address: int, count: int) -> list[int]:
        """Read raw 16-bit registers. kind: 3 = input (FC04), 4 = holding (FC03)."""
        if count > MAX_REGS_PER_QUERY:
            raise ValueError("the pump allows at most %d registers per query" % MAX_REGS_PER_QUERY)
        fc = 4 if kind == 3 else 3
        pdu = struct.pack(">BHH", fc, address, count)
        body = self._transact(pdu, count, "reading %d registers from %d" % (count, address))
        nbytes = body[1]
        raw = body[2:2 + nbytes]
        return list(struct.unpack(">" + "H" * (nbytes // 2), raw))

    def write(self, address: int, values: list[int]) -> None:
        """Write holding registers with FC16 (works for both 16- and 32-bit values)."""
        payload = b"".join(struct.pack(">H", v & 0xFFFF) for v in values)
        pdu = struct.pack(">BHHB", 16, address, len(values), len(payload)) + payload
        self._transact(pdu, len(values), "writing %d registers at %d" % (len(values), address))
