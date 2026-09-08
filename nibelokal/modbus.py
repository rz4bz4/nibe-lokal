"""Minimal Modbus client for NIBE heat pumps, over a TCP socket.

Why not pymodbus/async_modbus: they set socket.TCP_KEEPIDLE, which does not
exist on macOS, and they make NIBE's documented limits (max 20 registers per
query, max 100 registers per second) awkward to respect. This is stdlib only
and does exactly what the pump allows.

Addressing follows NIBE's own convention, as used by the `nibe` package:
    input register   (read only)  : coil 3xxxx -> wire address coil - 30001, FC04
    holding register (read/write) : coil 4xxxx -> wire address coil - 40001, FC03/FC16

32-bit values occupy two registers. Which of the two comes first is not decided
here -- this file moves 16-bit words and nothing else. On the S series it is
the low word, NIBE TIF EN 2608 p.6: "when reading multiple registers, the
registers are shown in reverse order". On the F series it is a setting, in the
pump's menu 5.3.11 and in register 48852, and NIBE's manual and NIBE's register
database disagree about which way it ships. See nibelokal/profile.py, which
picks a side and then asks the pump anyway, and nibelokal/registry.py, where the
two words become a number.

Two framings, one socket
------------------------

An S-series pump has Ethernet and speaks Modbus TCP: menu 7.5.9, an MBAP
header, done. That is `framing: tcp`, and it is the default.

An F-series pump has no Ethernet at all. Reaching one needs NIBE's MODBUS 40
accessory, which is RS485 running Modbus RTU at 9600 8N1, plus something that
carries RS485 over the network. Those gateways come in two kinds, and which
one you have decides what arrives on the socket:

* **Protocol converters** turn Modbus TCP into Modbus RTU themselves. Nothing
  to do here: `framing: tcp` is right, and this file cannot tell the
  difference between one of these and a real Modbus TCP pump.
* **Transparent serial bridges** -- most Waveshare, USR-TCP232 and PUSR boxes
  in their default mode -- forward the serial bytes untouched. What comes out
  of the TCP socket is then a raw RTU frame: unit id, PDU, CRC16, with no MBAP
  header anywhere. `framing: rtu` speaks that.

The differences RTU brings, all handled below: the CRC has to be computed and
checked, and there is no transaction id to match an answer to a request by.

That second one is the dangerous one, and it is worth being precise about what
is and is not done about it. Request X times out; this client drops the socket;
the bridge, which has been waiting on a 9600-baud serial line the whole time,
delivers X's answer onto the *new* socket -- and request Y, to the same unit
with the same function code, has nothing in the frame that says it is not Y's.
Three things narrow that here, and none of them closes it:

* `read` checks the answer's byte count against the number of registers it
  asked for, and `write` checks that function 16's echo names the address and
  quantity it sent. A straggler for a different-sized request is caught.
* After a failed RTU exchange, `_drain` reads and discards whatever is already
  waiting before the next request goes out, and logs what it threw away.
* Every failure still drops the socket, as before.

A straggler of the same length, to the same unit, with the same function code,
arriving after the drain, is still accepted as the answer to the wrong request.
There is no way to tell it apart in the protocol. What that costs is every
following reading shifted by one register, which is why the three partial
defences are there and why the log line exists.

**A serial port is out of scope.** Talking to /dev/ttyUSB0 needs pyserial,
which is not in the standard library, and this project has no runtime
dependencies. If your pump is on a local USB adapter rather than on the
network, put a transparent TCP bridge in front of it (`socat`, `ser2net`) and
point `host`/`port` at that.
"""
from __future__ import annotations

import logging
import socket
import struct
import threading
import time

log = logging.getLogger("nibelokal.modbus")

#: The two framings `framing` in config.yaml accepts.
FRAMINGS = ("tcp", "rtu")

# NIBE documents: max 20 registers per query, max 100 registers per second.
#
# 20 is the protocol ceiling this client will never exceed. It is not the limit
# that applies to a given pump: through a MODBUS 40 the limit is *one* register
# per request for anything outside the accessory's 20-entry LOG.SET file. That
# is a property of the generation rather than of this file, so it lives in
# nibelokal/profile.py and is applied where the batching happens, in pump.py.
# See docs/f-series.md, "What MODBUS 40 can and cannot do".
MAX_REGS_PER_QUERY = 20
MAX_REGS_PER_SECOND = 100

#: Function 0x2B/0x0E, Read Device Identification, and the three basic objects
#: every device that implements it must answer: vendor, product code, revision.
#: MODBUS 40 answers "NIBE", a product code such as "F1245" and a software
#: version such as 5539 (docs/f-series.md); S-series pumps answer it too.
FC_DEVICE_ID = 0x2B
MEI_DEVICE_ID = 0x0E
DEVICE_ID_FIELDS = {0x00: "vendor", 0x01: "product", 0x02: "revision"}

#: How long to wait for an answer to 0x2B, and how many attempts to make.
#:
#: One second and one attempt, both deliberately smaller than everything else
#: in this file. 0x2B is optional in the Modbus specification, so "no answer"
#: is one of its normal outcomes, and the only caller is a best-effort line.
#: A device that answers an exception costs nothing; a device that *ignores*
#: the function costs a timeout, and with the ordinary 5 s timeout and the
#: ordinary retry that is two timeouts, ten seconds, and a dropped and
#: reopened socket -- paid by whoever ran `status`, before the registers they
#: actually asked for. NIBE's own table gives 0x2B a 0.5 s maximum on a
#: MODBUS 40, so one second is already generous.
DEVICE_ID_TIMEOUT = 1.0
DEVICE_ID_ATTEMPTS = 1

#: How long to spend discarding stragglers after an RTU request timed out.
#:
#: 0.3 s, and only on the RTU path and only after a failure. The case is a
#: transparent RS485-to-Ethernet bridge: request X times out, this client drops
#: the socket, and the bridge -- which has been waiting on a 9600-baud serial
#: line all along -- delivers X's answer onto the *next* socket. Modbus RTU has
#: no transaction id, so that frame is matched to the next request Y by unit id
#: and function code, and both of those are the same. Reading whatever is
#: already waiting before sending Y consumes the straggler instead of matching
#: it. At 9600 8N1 a maximum-length RTU frame is about a quarter of a second on
#: the wire, so 0.3 s is one frame's worth of patience and no more.
RTU_DRAIN_SECONDS = 0.3

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
            elif "identification" in context:
                # Function 0x2B is optional in the specification, so this is a
                # device saying "I do not do that", not one saying anything is
                # wrong. It reads as an error otherwise, and the only caller is
                # a best-effort line in `status`.
                msg += " - this pump does not implement that function."
            else:
                msg += " - this pump does not implement that register."
        super().__init__(msg + ((" (" + context + ")") if context else ""))


class ModbusOffline(Exception):
    """Could not reach the pump at all."""


class ModbusCorrupt(ModbusOffline):
    """The frame arrived and was damaged: a bad CRC on an RTU answer.

    A subclass of ModbusOffline so that every existing caller goes on treating
    it as "the pump did not answer usefully", which is what it is. It is its
    own class because one case can be told apart from a dead link and needs to
    be: users report that a MODBUS 40 answers 32-bit registers outside its
    LOG.SET file with CRC errors, register after register, on a pump that is
    otherwise perfectly reachable (docs/f-series.md, "What users report"). If
    that arrives as a plain ModbusOffline it takes the whole poll down every
    minute for one unreadable register; as this, pump._read_block can drop that
    one register into the hourly retry and read the rest.
    """


def crc16(data: bytes) -> int:
    """CRC16-Modbus over `data`. Returns the check value as an integer.

    The classic reflected CRC: seed 0xFFFF, polynomial 0xA001 (0x8005 bit
    reversed), least significant bit first. Written out as the loop rather
    than as a 256-entry table -- it runs a few times a second at most, and a
    table nobody can check by eye is a worse thing to have in a file about
    correctness than eight lines of shifting.

    On the wire the value goes out low byte first, which is the one thing
    about Modbus CRCs everybody gets wrong once; see `_rtu_frame`.

    Checked against the standard vector in tests/test_modbus.py:
    01 03 00 00 00 0A -> C5 CD, i.e. crc16(...) == 0xCDC5.
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def _set_timeout(sock: socket.socket, seconds: float) -> None:
    """Best-effort `settimeout`. A closed socket is not a reason to fail here.

    The only caller is `_transact`, which sets a shorter timeout for one
    exchange and puts the configured one back afterwards. Putting it back
    happens in a `finally`, where the socket may already have been closed by
    something further down -- and an OSError raised there would replace the
    real failure with a bookkeeping one.
    """
    try:
        sock.settimeout(seconds)
    except OSError:
        pass


class _Budget:
    """Sliding-window rate limiter, registers per second.

    Its own lock, and it re-checks after sleeping: a single pass would sleep
    just long enough for the oldest event to age out and then spend regardless,
    which lets a poller and a backup running at once sail past the limit.
    """

    def __init__(self, limit: int = MAX_REGS_PER_SECOND):
        self.limit = limit
        self._events: list[tuple[float, int]] = []
        self._lock = threading.Lock()

    def spend(self, n: int) -> None:
        with self._lock:
            while True:
                now = time.monotonic()
                self._events = [(t, c) for t, c in self._events if now - t < 1.0]
                if sum(c for _, c in self._events) + n <= self.limit or not self._events:
                    self._events.append((now, n))
                    return
                time.sleep(max(0.01, 1.0 - (now - self._events[0][0])))


class ModbusTCP:
    """One long-lived TCP session, serialised requests, auto-reconnect.

    `framing` picks what goes on that socket: "tcp" for Modbus TCP with its
    MBAP header (the default, and what an S-series pump speaks), "rtu" for a
    raw Modbus RTU frame with a CRC16 and no header, which is what a
    transparent RS485-to-Ethernet bridge in front of a MODBUS 40 forwards. The
    rate limiter, the timeouts, the reconnect and the "have we ever heard from
    this pump" advice are the same either way.
    """

    def __init__(self, host: str, port: int = 502, unit: int = 1,
                 timeout: float = 5.0, framing: str = "tcp"):
        self.host = host
        self.port = port
        self.unit = unit
        self.timeout = timeout
        framing = (framing or "tcp").strip().lower()
        if framing not in FRAMINGS:
            raise ValueError("framing must be one of %s, not %r"
                             % (", ".join(FRAMINGS), framing))
        self.framing = framing
        self._sock: socket.socket | None = None
        self._tid = 0
        self._lock = threading.RLock()
        self._budget = _Budget()
        # Once the pump has answered, a failure to connect is a dropped session,
        # not "Modbus is switched off" -- the advice differs, so say the right one.
        self._seen_pump = False
        # Set when an RTU exchange failed, cleared by the drain that the next
        # RTU request does before it sends anything. See RTU_DRAIN_SECONDS.
        self._resync = False

    # -- connection ------------------------------------------------------

    def _connect(self) -> socket.socket:
        if self._sock is not None:
            return self._sock
        try:
            s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            if self._seen_pump:
                raise ModbusOffline(
                    "Lost contact with the heat pump at %s:%d (%s). It answered earlier, "
                    "so this is a dropped session or a network hiccup rather than a "
                    "setting." % (self.host, self.port, exc)
                ) from exc
            if self.framing == "rtu":
                # There is no menu 7.5.9 on an F-series pump, and telling
                # somebody to look for one they do not have is worse than
                # saying nothing. What can be wrong here is the gateway.
                raise ModbusOffline(
                    "Cannot reach the RS485 gateway at %s:%d (%s). With framing: rtu "
                    "this address is the gateway in front of the pump's MODBUS 40, "
                    "not the pump itself. Check that the gateway is powered and on "
                    "the network, and that its serial side is 9600 8N1."
                    % (self.host, self.port, exc)
                ) from exc
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

    def _exchange_tcp(self, sock: socket.socket, pdu: bytes) -> bytes:
        """One Modbus TCP request and its answer, returned as the response PDU."""
        self._tid = (self._tid + 1) % 0x10000
        sent_tid = self._tid
        header = struct.pack(">HHHB", sent_tid, 0, len(pdu) + 1, self.unit)
        sock.sendall(header + pdu)
        head = self._recv_exactly(sock, 7)
        tid, proto, length, _unit = struct.unpack(">HHHB", head)
        if proto != 0 or length < 2 or length > 260:
            raise ModbusOffline("Malformed Modbus header from the pump.")
        body = self._recv_exactly(sock, length - 1)
        # A stale answer must never be handed back as this one's.
        if tid != sent_tid or (body[0] & 0x7F) != pdu[0]:
            raise ModbusOffline("Out-of-step Modbus response; resynchronising.")
        return body

    def _exchange_rtu(self, sock: socket.socket, pdu: bytes) -> bytes:
        """One Modbus RTU frame and its answer, returned as the response PDU.

        RTU has no length field, so the answer is read in the two or three
        steps its own function code prescribes -- which is why only the three
        function codes this client sends are handled: FC03 and FC04 answer with
        a byte count, FC16 with a fixed eight-byte frame, and an exception with
        a fixed five. Anything else is a frame this client did not ask for.

        It has no transaction id either, so "is this the answer to what I just
        asked" is decided by the unit id and the function code alone. That is
        genuinely weaker than a transaction id: two outstanding requests to the
        same unit and function would be indistinguishable. They cannot happen
        here -- every exchange is inside self._lock and strictly one at a time
        -- and anything that does not match drops the socket rather than being
        handed back.
        """
        if self._resync:
            # The previous RTU exchange failed. Anything readable now is from
            # before this request existed. Cleared first: a drain that itself
            # throws must not leave the flag set and drain again forever.
            self._resync = False
            self._drain(sock)
        frame = self._rtu_frame(bytes([self.unit]) + pdu)
        sock.sendall(frame)

        head = self._recv_exactly(sock, 2)
        unit, function = head[0], head[1]
        if function & 0x80:
            rest = self._recv_exactly(sock, 3)            # exception code + CRC
        elif function in (3, 4):
            count = self._recv_exactly(sock, 1)
            rest = count + self._recv_exactly(sock, count[0] + 2)
        elif function == 16:
            rest = self._recv_exactly(sock, 6)            # address, quantity, CRC
        elif function == FC_DEVICE_ID:
            # 0x2B carries neither a byte count nor a fixed length: the header
            # says how many objects follow and each object carries its own
            # length, so it is read object by object. Six header bytes -- MEI
            # type, read code, conformity level, more-follows, next object id,
            # object count -- then that many (id, length, value) triples.
            header = self._recv_exactly(sock, 6)
            objects = b""
            for _ in range(header[5]):
                pair = self._recv_exactly(sock, 2)
                objects += pair + self._recv_exactly(sock, pair[1])
            rest = header + objects + self._recv_exactly(sock, 2)
        else:
            raise ModbusOffline(
                "The pump answered RTU function %d, which this client never sends; "
                "resynchronising." % function)

        full = head + rest
        payload, sent_crc = full[:-2], full[-2:]
        # Low byte first on the wire -- the one detail worth checking twice.
        if struct.unpack("<H", sent_crc)[0] != crc16(payload):
            raise ModbusCorrupt(
                "Bad CRC on the RTU frame from the pump. Either the RS485 wiring is "
                "picking up noise, or the gateway is not in transparent mode and is "
                "speaking Modbus TCP -- in which case set framing: tcp.")
        if unit != self.unit or (function & 0x7F) != pdu[0]:
            raise ModbusOffline("Out-of-step Modbus response; resynchronising.")
        # The response PDU, without the unit id and without the CRC, so the
        # caller sees exactly what the MBAP path hands back.
        return payload[1:]

    def _rtu_advice(self) -> str:
        """What to check when an RTU link opens and then never answers. Or "".

        Only when the pump has never answered on this connection, and only on
        the RTU path: once it has answered, a lost connection is a lost
        connection and this list would be noise. The socket connecting and then
        every request timing out is the single symptom shared by all three of
        the things below, because a MODBUS 40 that is not being addressed says
        nothing rather than saying no -- so all three are named rather than
        guessed between.
        """
        if self.framing != "rtu" or self._seen_pump:
            return ""
        return (
            " The gateway accepted the connection and the pump answered "
            "nothing, which looks the same for all of: the wrong `unit` (a "
            "MODBUS 40 is slave 1, fixed, below its software v.10, and 1-247 "
            "from v.10 where the pump's menu 5.3.11 sets it -- this app is "
            "asking for unit %d); the wrong `framing` (a transparent gateway "
            "needs rtu and a protocol-converting one needs tcp); and a MODBUS "
            "40 that is not activated in the pump's menu 5.2."
            % self.unit)

    def _drain(self, sock: socket.socket) -> None:
        """Read and throw away whatever is already waiting on the socket.

        Called on the RTU path only, and only after the previous exchange
        failed -- see RTU_DRAIN_SECONDS for the case it exists for. Everything
        discarded is logged at WARNING with its bytes: a straggler means a
        request timed out and its answer arrived late, and that is worth
        knowing about a bus somebody thinks is healthy.

        **This does not close the hole.** A straggler is only caught here if it
        arrives before the next request goes out. One that is still in flight,
        or one for a request of the same length to the same unit with the same
        function code, is indistinguishable from the real answer and will be
        accepted as it -- Modbus RTU has no transaction id to tell them apart
        with. The byte-count check in `read` and the echo check in `write` are
        the other two thirds of the same partial defence.
        """
        discarded = b""
        deadline = time.monotonic() + RTU_DRAIN_SECONDS
        _set_timeout(sock, RTU_DRAIN_SECONDS)
        try:
            while time.monotonic() < deadline:
                try:
                    chunk = sock.recv(4096)
                except OSError:
                    # A timeout, which is the ordinary case: nothing was
                    # waiting. Anything else here is a socket the next sendall
                    # will fail on anyway, and this is not the place to report
                    # it.
                    break
                if not chunk:
                    break
                discarded += chunk
        finally:
            _set_timeout(sock, self.timeout)
        if discarded:
            log.warning(
                "discarded %d late byte(s) from %s:%d before sending the next "
                "request: %s. A Modbus RTU frame has no transaction id, so an "
                "answer that arrives after its request timed out would "
                "otherwise be read as the next request's.",
                len(discarded), self.host, self.port, discarded.hex())

    @staticmethod
    def _rtu_frame(payload: bytes) -> bytes:
        """`payload` (unit id + PDU) with its CRC16 appended, low byte first."""
        return payload + struct.pack("<H", crc16(payload))

    def _transact(self, pdu: bytes, cost: int, context: str, *,
                  attempts: int = 2, timeout: float | None = None) -> bytes:
        """Send one PDU, return the response PDU. Retries once on a dropped socket.

        `attempts` and `timeout` exist for one caller: `device_id`, whose
        function is optional in the specification and whose failure is not
        news. Everything else takes the defaults, which are what this method
        has always done. See DEVICE_ID_TIMEOUT.
        """
        with self._lock:
            # Spent inside the lock so the budget tracks what actually goes on
            # the wire, not what got queued.
            self._budget.spend(cost)
            last: Exception | None = None
            for attempt in range(1, attempts + 1):
                try:
                    sock = self._connect()
                except ModbusOffline as exc:
                    last = exc
                    if attempt == attempts or not self._seen_pump:
                        raise
                    time.sleep(0.25)
                    continue
                try:
                    if timeout is not None:
                        # This exchange only, and restored on every path out --
                        # a short timeout left behind on the shared socket would
                        # be a caller's convenience turned into everybody's
                        # timeout.
                        _set_timeout(sock, timeout)
                    try:
                        if self.framing == "rtu":
                            body = self._exchange_rtu(sock, pdu)
                        else:
                            body = self._exchange_tcp(sock, pdu)
                    finally:
                        if timeout is not None:
                            _set_timeout(sock, self.timeout)
                except (OSError, ModbusOffline) as exc:
                    last = exc
                    # An RTU exchange that failed may have an answer still on
                    # its way. The socket is dropped here, but a transparent
                    # bridge will forward that frame onto the next one, where
                    # nothing but the drain can tell it from this request's
                    # answer. See RTU_DRAIN_SECONDS.
                    if self.framing == "rtu":
                        self._resync = True
                    self._drop()
                    if attempt == attempts:
                        if isinstance(exc, ModbusCorrupt):
                            # Twice in a row is not a burst of noise on the
                            # line, it is this register. Re-raised as itself,
                            # with its own advice intact, so pump._read_block
                            # can tell it from a dead link -- and so the
                            # sentence about the gateway's framing survives.
                            raise
                        raise ModbusOffline(
                            "Lost the connection to %s:%d (%s).%s"
                            % (self.host, self.port, exc, self._rtu_advice())
                        ) from exc
                    time.sleep(0.25)
                    continue
                self._seen_pump = True
                # A frame the length header promised but that carries no
                # function code, or an exception frame with no exception code,
                # is a truncated answer -- not something to index into. body[1]
                # on a one-byte exception frame raised IndexError, which is
                # neither ModbusError nor ModbusOffline and so escaped every
                # caller that handles Modbus failures, pump._read_block among
                # them.
                if not body:
                    raise ModbusOffline("The pump sent an empty Modbus response.")
                if body[0] & 0x80:
                    if len(body) < 2:
                        raise ModbusOffline(
                            "The pump sent a truncated Modbus exception response.")
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
        # Everything below indexes into the answer, so the answer is checked
        # first. A short body used to raise IndexError or struct.error, which
        # are not Modbus exceptions and were not caught as such anywhere:
        # pump._read_block catches ModbusError, and the poller then logged a
        # struct.error as if the pump had said something about a register.
        if len(body) < 2:
            raise ModbusOffline("The pump sent a Modbus response with no data.")
        nbytes = body[1]
        raw = body[2:2 + nbytes]
        if nbytes % 2 or len(raw) < nbytes:
            raise ModbusOffline(
                "The pump promised %d bytes of register data and sent %d."
                % (nbytes, len(raw)))
        if nbytes != 2 * count:
            # The answer is for a different request. On Modbus TCP the
            # transaction id already caught that; on RTU there is no
            # transaction id, and a late answer to a request that timed out --
            # delivered by a transparent bridge on the *next* socket -- is
            # matched by unit id and function code alone, both of which it has.
            # The byte count is the third thing it has to agree about, and it
            # is free: this client always knows how many registers it asked
            # for. Without it a straggler for one register was accepted as the
            # answer to a two-register request (or the other way round), which
            # is every following reading shifted by one register -- silently,
            # and with plausible numbers.
            #
            # It does not close the hole. A straggler for a request of the same
            # length, to the same unit, with the same function code, is still
            # indistinguishable from the real answer; that is what `_drain`
            # after a timeout is for, and even that is best effort. See
            # ModbusTCP._drain.
            raise ModbusOffline(
                "The pump answered %d registers to a request for %d; this is "
                "somebody else's answer, and it is being discarded rather than "
                "read as this register's."
                % (nbytes // 2, count))
        return list(struct.unpack(">" + "H" * (nbytes // 2), raw))

    def write(self, address: int, values: list[int]) -> None:
        """Write holding registers with FC16 (works for both 16- and 32-bit values).

        Function 16 and not function 6, on both generations. NIBE, in the
        MODBUS 40 FAQ: *"Modbus40 uses commando type 'Write Multiple
        registers'. 'Write Single registers' does not work in the Modbus40."*
        A client that sends function 6 there gets a pump where reads work and
        writes do nothing at all.
        """
        payload = b"".join(struct.pack(">H", v & 0xFFFF) for v in values)
        pdu = struct.pack(">BHHB", 16, address, len(values), len(payload)) + payload
        body = self._transact(pdu, len(values),
                              "writing %d registers at %d" % (len(values), address))
        # A function 16 answer echoes the address and the quantity it wrote.
        # Checked, for the reason the byte count is checked in read(): on RTU
        # there is no transaction id, so a late echo of an *earlier* write --
        # forwarded by a transparent bridge onto the new socket after a timeout
        # -- matches by unit id and function code alone. Accepting it reports a
        # write to register X as having succeeded when what the pump
        # acknowledged was a write to register Y, which for a setting written
        # once and then read back looks exactly like success.
        if len(body) < 5:
            raise ModbusOffline(
                "The pump acknowledged the write with a truncated response.")
        echoed_address, echoed_count = struct.unpack(">HH", body[1:5])
        if echoed_address != address or echoed_count != len(values):
            raise ModbusOffline(
                "The pump acknowledged a write of %d registers at %d, and this "
                "was a write of %d at %d; this is somebody else's "
                "acknowledgement."
                % (echoed_count, echoed_address, len(values), address))

    def device_id(self) -> dict:
        """Ask the pump what it is: function 0x2B, Read Device Identification.

        Returns {"vendor", "product", "revision"} where the device answered
        them -- "NIBE", a product code such as "F1245", a software version such
        as "5539" -- plus every other object it volunteered, by number.

        This is the cheapest cross-check there is that the `model:` in
        config.yaml is the pump on the other end of the wire, and it is worth
        more on the F series than on the S: there the register map, the pump's
        firmware and the MODBUS 40's own firmware are three things that can
        disagree with each other. NIBE's own table gives it a 0.5 s timeout and
        no LOG.SET involvement, so it costs nothing.

        It is optional in the Modbus specification, and there are two ways for
        a device not to implement it. One answers an exception, which arrives
        as ModbusError. The other simply says nothing -- and that one is not
        free: at the ordinary timeout and the ordinary one retry it costs two
        full timeouts and a dropped socket. So this asks once, with a second's
        patience, and never reconnects: DEVICE_ID_TIMEOUT and
        DEVICE_ID_ATTEMPTS. Every caller must treat any failure here as "no
        answer" rather than as something being wrong, which is why
        `nibelokal status` prints this line best-effort, last, and goes on.
        """
        # Read code 0x01: the basic objects, which is all of NIBE's three.
        pdu = struct.pack(">BBB", FC_DEVICE_ID, MEI_DEVICE_ID, 0x01)
        body = self._transact(pdu, 1, "reading the device identification",
                              attempts=DEVICE_ID_ATTEMPTS,
                              timeout=DEVICE_ID_TIMEOUT)
        return _parse_device_id(body)


def _parse_device_id(body: bytes) -> dict:
    """The objects out of a 0x2B response PDU.

    Kept out of the class because it is pure parsing, and because a malformed
    answer here must produce the same "we do not know" as no answer at all --
    never a half-read product code, which would be worse than nothing in the
    one place whose whole job is telling you which pump you have.
    """
    if len(body) < 7 or body[1] != MEI_DEVICE_ID:
        raise ModbusOffline(
            "The pump answered function 0x2B with something that is not a "
            "device identification response.")
    out: dict = {"objects": {}}
    offset, remaining = 7, body[6]
    while remaining and offset + 2 <= len(body):
        number, length = body[offset], body[offset + 1]
        value = body[offset + 2:offset + 2 + length]
        if len(value) < length:
            # Truncated. Everything read so far is still true; the rest is not
            # invented. NIBE's strings are ASCII, and latin-1 cannot fail.
            break
        offset += 2 + length
        remaining -= 1
        text = value.decode("latin-1", "replace").strip()
        out["objects"][number] = text
        if number in DEVICE_ID_FIELDS:
            out[DEVICE_ID_FIELDS[number]] = text
    return out
