"""Malformed answers from the pump, against a socket that really sends them.

`_read_block` in pump.py catches ModbusError and ModbusOffline, which is the
contract this module is supposed to keep: everything that can go wrong on the
wire arrives as one of those two. Two frames broke it, and both are things a
pump mid-reboot or a half-closed TCP connection actually produces:

  * a one-byte exception frame -- the function code with the error bit set and
    no exception code after it. `body[1]` raised IndexError, which is neither
    of the two, so it escaped every caller that handles Modbus failures and
    came out of the poll loop as an unhandled exception.
  * a read answer whose byte count does not match the bytes that followed.
    struct.unpack then raised struct.error, and the poller logged it as though
    the pump had said something about a register.

A real listening socket rather than a mocked one: the framing, the transaction
id and the retry are all part of what is being tested, and a mock of _transact
would test none of them.
"""
import logging
import os
import socket
import struct
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nibelokal.modbus import (DEVICE_ID_TIMEOUT, FC_DEVICE_ID,        # noqa: E402
                              RTU_DRAIN_SECONDS, ModbusCorrupt,
                              ModbusError, ModbusOffline, ModbusTCP, crc16)


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


class FakePump:
    """Answers every request with the same scripted PDU body.

    The MBAP header is built correctly (right transaction id, right unit), so
    the only thing under test is what the client does with the body.
    """

    def __init__(self, body_for):
        self.body_for = body_for
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.requests = 0
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(conn,), daemon=True).start()

    def _one(self, conn):
        with conn:
            while True:
                try:
                    head = conn.recv(7)
                    if len(head) < 7:
                        return
                    tid, _proto, length, _unit = struct.unpack(">HHHB", head)
                    pdu = conn.recv(length - 1)
                    if not pdu:
                        return
                    self.requests += 1
                    body = self.body_for(pdu)
                    if body is None:                  # hang up mid-conversation
                        return
                    conn.sendall(struct.pack(">HHHB", tid, 0, len(body) + 1, 1) + body)
                except OSError:
                    return

    def close(self):
        self.sock.close()


class MalformedAnswers(unittest.TestCase):
    def client(self, body_for):
        pump = FakePump(body_for)
        self.addCleanup(pump.close)
        mb = ModbusTCP("127.0.0.1", pump.port, unit=1, timeout=2.0)
        self.addCleanup(mb.close)
        return mb, pump

    def test_an_exception_frame_with_no_exception_code(self):
        # 0x84 = FC04 with the error bit set, and then nothing. body[1].
        mb, _ = self.client(lambda pdu: bytes([pdu[0] | 0x80]))
        with self.assertRaises(ModbusOffline) as caught:
            mb.read(3, 5, 2)
        self.assertIn("truncated", str(caught.exception))

    def test_a_complete_exception_frame_is_still_a_modbus_error(self):
        mb, _ = self.client(lambda pdu: bytes([pdu[0] | 0x80, 2]))
        with self.assertRaises(ModbusError) as caught:
            mb.read(3, 5, 2)
        self.assertEqual(caught.exception.code, 2)

    def test_a_read_answer_with_no_byte_count(self):
        mb, _ = self.client(lambda pdu: bytes([pdu[0]]))
        with self.assertRaises(ModbusOffline):
            mb.read(3, 5, 2)

    def test_a_byte_count_that_promises_more_than_arrives(self):
        # "here are eight bytes", followed by two.
        mb, _ = self.client(lambda pdu: bytes([pdu[0], 8]) + b"\x00\x01")
        with self.assertRaises(ModbusOffline) as caught:
            mb.read(3, 5, 4)
        self.assertIn("8", str(caught.exception))

    def test_an_odd_byte_count(self):
        # Registers are two bytes each; an odd count cannot be unpacked, and
        # struct.error is not something any caller catches.
        mb, _ = self.client(lambda pdu: bytes([pdu[0], 3]) + b"\x00\x01\x02")
        with self.assertRaises(ModbusOffline):
            mb.read(3, 5, 2)

    def test_a_well_formed_answer_still_reads(self):
        mb, _ = self.client(
            lambda pdu: bytes([pdu[0], 4]) + struct.pack(">HH", 0x1234, 0x5678))
        self.assertEqual(mb.read(3, 5, 2), [0x1234, 0x5678])

    def test_every_wire_failure_is_one_of_the_two_pump_py_catches(self):
        """The contract, asserted as a contract.

        pump._read_block catches (ModbusError, ModbusOffline) and nothing else.
        Anything this module can raise that is not one of those two reaches the
        poll loop as an unhandled exception.
        """
        frames = [
            lambda pdu: bytes([pdu[0] | 0x80]),               # short exception
            lambda pdu: bytes([pdu[0]]),                      # no byte count
            lambda pdu: bytes([pdu[0], 8]) + b"\x00\x01",     # short data
            lambda pdu: bytes([pdu[0], 3]) + b"\x00\x01\x02",  # odd byte count
            lambda pdu: None,                                 # hangs up
        ]
        for frame in frames:
            mb, _ = self.client(frame)
            with self.assertRaises((ModbusError, ModbusOffline), msg=repr(frame)):
                mb.read(3, 5, 2)


class TheCRC(unittest.TestCase):
    """CRC16-Modbus, against the vector everyone checks against.

    `01 03 00 00 00 0A` -- read ten holding registers from address 0 on unit 1
    -- has the check bytes C5 CD, low byte first, which is the integer 0xCDC5.
    It is in every Modbus tutorial and in the specification's own appendix, and
    it is the one thing in the RTU framing that cannot be got right by reading
    the code twice.
    """

    def test_the_standard_vector(self):
        self.assertEqual(crc16(bytes([0x01, 0x03, 0x00, 0x00, 0x00, 0x0A])),
                         0xCDC5)
        self.assertEqual(struct.pack("<H", 0xCDC5), b"\xc5\xcd")

    def test_an_empty_frame_is_the_seed(self):
        self.assertEqual(crc16(b""), 0xFFFF)

    def test_one_flipped_bit_changes_it(self):
        a = crc16(bytes([0x01, 0x03, 0x00, 0x00, 0x00, 0x0A]))
        b = crc16(bytes([0x01, 0x03, 0x00, 0x00, 0x00, 0x0B]))
        self.assertNotEqual(a, b)


class RtuPump:
    """A transparent RS485 gateway with an F-series pump behind it.

    No MBAP header anywhere: what arrives on the socket is a raw RTU frame,
    and what goes back is one too. The CRC is computed for real in both
    directions, because a client that computes it correctly and checks it
    against nothing is a client that has not been tested.
    """

    def __init__(self, registers=None, corrupt_crc=False, unit=1):
        self.registers = dict(registers or {})
        self.corrupt_crc = corrupt_crc
        self.unit = unit
        self.requests = []
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(conn,), daemon=True).start()

    def _read_request(self, conn):
        """One RTU request frame, read the way its function code says."""
        head = self._exactly(conn, 2)
        if head is None:
            return None
        function = head[1]
        if function in (3, 4):
            rest = self._exactly(conn, 6)               # addr, count, CRC
        elif function == 0x2B:
            rest = self._exactly(conn, 4)               # MEI type, read code, CRC
        elif function == 16:
            fixed = self._exactly(conn, 5)              # addr, count, bytecount
            if fixed is None:
                return None
            rest = fixed + self._exactly(conn, fixed[4] + 2)
        else:
            return None
        return None if rest is None else head + rest

    @staticmethod
    def _exactly(conn, n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def _one(self, conn):
        with conn:
            while True:
                try:
                    frame = self._read_request(conn)
                except OSError:
                    return
                if frame is None:
                    return
                self.requests.append(frame)
                # The client's own CRC, checked here so a client that sends a
                # wrong one fails loudly rather than being humoured.
                assert struct.unpack("<H", frame[-2:])[0] == crc16(frame[:-2]), \
                    "the client sent a bad CRC"
                body = self._answer(frame)
                if body is None:
                    return
                payload = bytes([self.unit]) + body
                crc = crc16(payload) ^ (0xFFFF if self.corrupt_crc else 0)
                try:
                    conn.sendall(payload + struct.pack("<H", crc))
                except OSError:
                    return

    #: What this pump says it is when asked with function 0x2B. NIBE's own
    #: example: label "NIBE", product code "F1245", software version 5539.
    device_id = ("NIBE", "F1245", "5539")

    def _answer(self, frame):
        function = frame[1]
        if function == 0x2B:
            if self.device_id is None:
                # A device that does not implement it. Exception 1, which is
                # what this pump answers for anything it does not do.
                return bytes([function | 0x80, 1])
            objects = b""
            for number, text in enumerate(self.device_id):
                raw = text.encode("latin-1")
                objects += bytes([number, len(raw)]) + raw
            # fc, MEI type, read code, conformity, more follows, next id, count
            return (bytes([function, 0x0E, 0x01, 0x81, 0x00, 0x00,
                           len(self.device_id)]) + objects)
        if function in (3, 4):
            start, count = struct.unpack(">HH", frame[2:6])
            words = [self.registers.get(start + i, 0) for i in range(count)]
            return (bytes([function, count * 2])
                    + b"".join(struct.pack(">H", w & 0xFFFF) for w in words))
        if function == 16:
            start, count = struct.unpack(">HH", frame[2:6])
            for i in range(count):
                self.registers[start + i] = struct.unpack(
                    ">H", frame[7 + i * 2:9 + i * 2])[0]
            return struct.pack(">BHH", 16, start, count)
        return None

    def close(self):
        self.sock.close()


class RtuFraming(unittest.TestCase):
    """An F-series pump behind a transparent gateway, end to end.

    Nothing above modbus.py knows which framing is in use, so what is tested
    here is that the same read() and write() calls produce the same values over
    a socket that carries no MBAP header at all.
    """

    def client(self, pump, **kw):
        mb = ModbusTCP("127.0.0.1", pump.port, unit=1, timeout=2.0,
                       framing="rtu", **kw)
        self.addCleanup(mb.close)
        return mb

    def test_a_read_round_trips(self):
        pump = RtuPump({3006: 0x0180, 3007: 0x0000})     # 47007 -> wire 3006
        self.addCleanup(pump.close)
        mb = self.client(pump)
        self.assertEqual(mb.read(4, 3006, 2), [0x0180, 0x0000])
        # And it really did go out as an RTU frame: unit, function, then the
        # request, then a CRC -- eight bytes, no six-byte MBAP header.
        self.assertEqual(len(pump.requests[0]), 8)
        self.assertEqual(pump.requests[0][:2], b"\x01\x03")

    def test_a_write_round_trips(self):
        pump = RtuPump({3010: 0})                        # 47011 -> wire 3010
        self.addCleanup(pump.close)
        mb = self.client(pump)
        mb.write(3010, [3])
        self.assertEqual(pump.registers[3010], 3)
        self.assertEqual(mb.read(4, 3010, 1), [3])

    def test_a_32_bit_write_round_trips(self):
        pump = RtuPump()
        self.addCleanup(pump.close)
        mb = self.client(pump)
        mb.write(939, [0x1234, 0x5678])                  # 40940, low word first
        self.assertEqual([pump.registers[939], pump.registers[940]],
                         [0x1234, 0x5678])

    def test_a_corrupted_crc_is_refused(self):
        """The whole reason the CRC is checked rather than merely appended.

        A gateway that is quietly in Modbus-TCP mode, or RS485 wiring that
        picks up noise from the compressor, both arrive as this. Handing the
        bytes over anyway would put an invented temperature on the page.
        """
        pump = RtuPump({3006: 42}, corrupt_crc=True)
        self.addCleanup(pump.close)
        mb = self.client(pump)
        # ModbusCorrupt, not a plain ModbusOffline: a frame that arrived and
        # was damaged can be told from a link that is not there, and pump.py
        # needs that distinction to drop one unreadable register instead of
        # failing the whole poll. It is a subclass, so every caller that
        # handles ModbusOffline still handles this.
        with self.assertRaises(ModbusCorrupt) as caught:
            mb.read(4, 3006, 1)
        self.assertIsInstance(caught.exception, ModbusOffline)
        self.assertIn("CRC", str(caught.exception))
        # And the advice names the likely cause rather than only the symptom,
        # which means it has to survive the retry that follows it.
        self.assertIn("framing: tcp", str(caught.exception))

    def test_an_exception_frame_is_still_a_modbus_error(self):
        pump = RtuPump()
        self.addCleanup(pump.close)
        pump._answer = lambda frame: bytes([frame[1] | 0x80, 2])
        mb = self.client(pump)
        with self.assertRaises(ModbusError) as caught:
            mb.read(4, 3006, 1)
        self.assertEqual(caught.exception.code, 2)

    def test_an_answer_from_another_unit_is_not_handed_back(self):
        """RTU has no transaction id, so this is all the resynchronising there is."""
        pump = RtuPump({3006: 42}, unit=9)
        self.addCleanup(pump.close)
        mb = self.client(pump)
        with self.assertRaises(ModbusOffline) as caught:
            mb.read(4, 3006, 1)
        self.assertIn("Out-of-step", str(caught.exception))

    def test_every_wire_failure_is_still_one_of_the_two_pump_py_catches(self):
        pump = RtuPump({3006: 42}, corrupt_crc=True)
        self.addCleanup(pump.close)
        mb = self.client(pump)
        with self.assertRaises((ModbusError, ModbusOffline)):
            mb.read(4, 3006, 1)

    def test_the_rate_limiter_is_still_there(self):
        pump = RtuPump()
        self.addCleanup(pump.close)
        mb = self.client(pump)
        before = len(mb._budget._events)
        mb.read(4, 0, 5)
        self.assertEqual(len(mb._budget._events), before + 1)
        self.assertEqual(mb._budget._events[-1][1], 5)

    def test_the_pump_is_remembered_once_it_has_answered(self):
        pump = RtuPump({0: 1})
        self.addCleanup(pump.close)
        mb = self.client(pump)
        self.assertFalse(mb._seen_pump)
        mb.read(4, 0, 1)
        self.assertTrue(mb._seen_pump)

    def test_an_unreachable_gateway_says_gateway_rather_than_menu_759(self):
        # There is no menu 7.5.9 on an F-series pump, and sending somebody to
        # look for one is worse than saying nothing.
        mb = ModbusTCP("127.0.0.1", 1, unit=1, timeout=0.5, framing="rtu")
        self.addCleanup(mb.close)
        with self.assertRaises(ModbusOffline) as caught:
            mb.read(4, 0, 1)
        self.assertIn("gateway", str(caught.exception))
        self.assertNotIn("7.5.9", str(caught.exception))


class ReadDeviceIdentification(unittest.TestCase):
    """Function 0x2B, over both framings, against a pump that answers it.

    It is the cheapest cross-check there is that the `model:` in config.yaml is
    the pump on the other end of the wire, and the one thing an F750 owner can
    paste into an issue that settles what they have. It is also optional in the
    specification, so the case that matters as much as the happy one is a pump
    that does not implement it: that must arrive as a ModbusError for a caller
    to shrug at, not as something that takes a command down.
    """

    def test_rtu_returns_vendor_product_and_revision(self):
        pump = RtuPump()
        self.addCleanup(pump.close)
        mb = ModbusTCP("127.0.0.1", pump.port, unit=1, timeout=2.0, framing="rtu")
        self.addCleanup(mb.close)
        info = mb.device_id()
        self.assertEqual(info["vendor"], "NIBE")
        self.assertEqual(info["product"], "F1245")
        self.assertEqual(info["revision"], "5539")

    def test_rtu_asks_with_the_documented_pdu(self):
        pump = RtuPump()
        self.addCleanup(pump.close)
        mb = ModbusTCP("127.0.0.1", pump.port, unit=1, timeout=2.0, framing="rtu")
        self.addCleanup(mb.close)
        mb.device_id()
        # unit, 0x2B, MEI type 0x0E, read code 0x01 (basic), CRC.
        self.assertEqual(pump.requests[0][:4], b"\x01\x2b\x0e\x01")
        self.assertEqual(len(pump.requests[0]), 6)

    def test_a_device_that_does_not_answer_it_raises_a_modbus_error(self):
        pump = RtuPump()
        pump.device_id = None
        self.addCleanup(pump.close)
        mb = ModbusTCP("127.0.0.1", pump.port, unit=1, timeout=2.0, framing="rtu")
        self.addCleanup(mb.close)
        with self.assertRaises(ModbusError) as caught:
            mb.device_id()
        self.assertEqual(caught.exception.code, 1)
        # And it says function, not register: exception 1 to a read means "I do
        # not have that register", and to 0x2B it means "I do not do that", and
        # the second one is not a fault.
        self.assertIn("function", str(caught.exception))
        self.assertNotIn("does not implement that register", str(caught.exception))

    def test_tcp_framing_reads_the_same_answer(self):
        # The MBAP header carries the length, so nothing about 0x2B needs
        # special handling there -- which is worth asserting rather than
        # assuming, because it is the framing every S-series pump speaks.
        objects = b""
        for number, text in enumerate(("NIBE", "S735", "9615")):
            raw = text.encode("latin-1")
            objects += bytes([number, len(raw)]) + raw
        body = bytes([0x2B, 0x0E, 0x01, 0x81, 0x00, 0x00, 3]) + objects
        pump = FakePump(lambda pdu: body)
        self.addCleanup(pump.close)
        mb = ModbusTCP("127.0.0.1", pump.port, unit=1, timeout=2.0)
        self.addCleanup(mb.close)
        self.assertEqual(mb.device_id()["product"], "S735")

    def test_an_answer_that_is_not_a_device_id_is_not_handed_back(self):
        pump = FakePump(lambda pdu: bytes([0x2B, 0x07, 0x01]))
        self.addCleanup(pump.close)
        mb = ModbusTCP("127.0.0.1", pump.port, unit=1, timeout=2.0)
        self.addCleanup(mb.close)
        with self.assertRaises(ModbusOffline):
            mb.device_id()

    def test_a_truncated_object_is_dropped_rather_than_half_read(self):
        # Half a product code is worse than none in the one place whose job is
        # telling you which pump you have.
        body = bytes([0x2B, 0x0E, 0x01, 0x81, 0x00, 0x00, 2]) \
            + bytes([0, 4]) + b"NIBE" + bytes([1, 5]) + b"F12"
        pump = FakePump(lambda pdu: body)
        self.addCleanup(pump.close)
        mb = ModbusTCP("127.0.0.1", pump.port, unit=1, timeout=2.0)
        self.addCleanup(mb.close)
        info = mb.device_id()
        self.assertEqual(info["vendor"], "NIBE")
        self.assertNotIn("product", info)


class ASilentDeviceIdCostsOneSecondAndNoReconnect(unittest.TestCase):
    """0x2B is optional, and "no" can be said by saying nothing.

    A pump that answers an exception costs nothing. A pump that ignores the
    function costs a timeout -- and at the ordinary 5 s timeout with the
    ordinary one retry that is ten seconds and a dropped, reopened socket, paid
    by whoever ran `status`. `nibelokal status` therefore asks last, and this
    asks once, with a second's patience. See modbus.DEVICE_ID_TIMEOUT.
    """

    def test_one_attempt_and_about_a_second(self):
        # A pump that accepts the request and never answers anything.
        pump = FakePump(lambda pdu: None if pdu[0] == FC_DEVICE_ID else b"\x03\x02\x00\x00")
        self.addCleanup(pump.close)
        mb = ModbusTCP("127.0.0.1", pump.port, unit=1, timeout=30.0)
        self.addCleanup(mb.close)
        started = time.monotonic()
        with self.assertRaises(ModbusOffline):
            mb.device_id()
        elapsed = time.monotonic() - started
        # One attempt at DEVICE_ID_TIMEOUT, not two at the configured 30 s.
        self.assertLess(elapsed, DEVICE_ID_TIMEOUT * 4,
                        "device_id waited %.1f s" % elapsed)
        self.assertEqual(1, pump.requests)

    def test_the_configured_timeout_is_put_back_afterwards(self):
        # The short timeout is for this one exchange. Left behind on the shared
        # socket it would be every register's timeout.
        answers = {FC_DEVICE_ID: None}
        pump = FakePump(lambda pdu: answers.get(pdu[0], b"\x03\x02\x00\x2a"))
        self.addCleanup(pump.close)
        mb = ModbusTCP("127.0.0.1", pump.port, unit=1, timeout=3.0)
        self.addCleanup(mb.close)
        with self.assertRaises(ModbusOffline):
            mb.device_id()
        self.assertEqual([42], mb.read(4, 0, 1))
        self.assertEqual(3.0, mb._sock.gettimeout())


class AnAnswerOfTheWrongShapeIsNotThisRequestsAnswer(unittest.TestCase):
    """RTU has no transaction id, so the shape is what is left to check.

    The case: request X times out, the socket is dropped, and the transparent
    bridge delivers X's answer onto the socket opened for request Y. Unit id
    and function code match, because they are the same for every register this
    app reads. What can still disagree is the size of the answer, and for a
    write the address and quantity function 16 echoes back.
    """

    def test_a_read_answer_of_the_wrong_length_is_refused(self):
        pump = FakePump(lambda pdu: b"\x03\x02\x00\x2a")     # one register
        self.addCleanup(pump.close)
        mb = ModbusTCP("127.0.0.1", pump.port, unit=1, timeout=2.0)
        self.addCleanup(mb.close)
        self.assertEqual([42], mb.read(4, 0, 1))
        with self.assertRaises(ModbusOffline) as caught:
            mb.read(4, 0, 2)
        self.assertIn("somebody else", str(caught.exception))

    def test_a_write_echo_naming_another_register_is_refused(self):
        pump = FakePump(lambda pdu: struct.pack(">BHH", 16, 9, 1))
        self.addCleanup(pump.close)
        mb = ModbusTCP("127.0.0.1", pump.port, unit=1, timeout=2.0)
        self.addCleanup(mb.close)
        mb.write(9, [7])                                     # the echo is right
        with self.assertRaises(ModbusOffline) as caught:
            mb.write(5, [7])                                 # and now it is not
        self.assertIn("somebody else", str(caught.exception))

    def test_a_write_echo_of_the_wrong_quantity_is_refused(self):
        pump = FakePump(lambda pdu: struct.pack(">BHH", 16, 5, 1))
        self.addCleanup(pump.close)
        mb = ModbusTCP("127.0.0.1", pump.port, unit=1, timeout=2.0)
        self.addCleanup(mb.close)
        with self.assertRaises(ModbusOffline):
            mb.write(5, [7, 8])


class LateFramesAreDrainedAfterATimeout(unittest.TestCase):
    """The straggler, in full, against a bridge that behaves like one.

    Request X is swallowed on the serial side; the client times out on both
    attempts and gives up. MODBUS 40 answers X anyway, onto a bridge with no
    client attached, so the bridge holds the frame and pushes it at the next
    client that connects -- which is this app opening a socket for request Y.
    Y asks for the same register count from the same unit with the same
    function code, so nothing in X's answer says it is not Y's.

    What closes it here is reading and discarding whatever is already waiting
    before Y goes out. That is partial and the code says so: a straggler still
    in flight when Y is sent is accepted.
    """

    LATE = b"\x01\x03\x02\x01\x2c"        # 300
    GOOD = b"\x01\x03\x02\x02\x58"        # 600

    def _bridge(self):
        late = self.LATE + struct.pack("<H", crc16(self.LATE))
        good = self.GOOD + struct.pack("<H", crc16(self.GOOD))
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        self.addCleanup(srv.close)
        state = {"requests": 0, "pending": False}

        def serve():
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                threading.Thread(target=one, args=(conn,), daemon=True).start()

        def one(conn):
            with conn:
                if state["pending"]:
                    state["pending"] = False
                    try:
                        conn.sendall(late)     # X's answer, before Y is asked
                    except OSError:
                        return
                while True:
                    try:
                        if not conn.recv(4096):
                            return
                    except OSError:
                        return
                    state["requests"] += 1
                    if state["requests"] <= 2:
                        # Request X, both of its attempts: nothing in time.
                        state["pending"] = state["requests"] == 2
                        continue
                    try:
                        conn.sendall(good)
                    except OSError:
                        return

        threading.Thread(target=serve, daemon=True).start()
        return srv.getsockname()[1]

    def _client(self):
        mb = ModbusTCP("127.0.0.1", self._bridge(), unit=1, timeout=0.4,
                       framing="rtu")
        self.addCleanup(mb.close)
        with self.assertRaises(ModbusOffline):
            mb.read(4, 3, 1)                   # request X: swallowed
        return mb

    def test_the_straggler_is_discarded_and_the_right_answer_read(self):
        mb = self._client()
        self.assertEqual([600], mb.read(4, 7, 1))

    def test_without_the_drain_it_would_be_read_as_this_requests_answer(self):
        # Not a test of the app: a test that the case is real. With the flag
        # cleared by hand, the same bridge hands over 300.
        mb = self._client()
        mb._resync = False
        self.assertEqual([300], mb.read(4, 7, 1))

    def test_what_was_discarded_is_logged(self):
        mb = self._client()
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, logging.CRITICAL)
        with self.assertLogs("nibelokal.modbus", level="WARNING") as caught:
            mb.read(4, 7, 1)
        self.assertTrue(any("discarded" in line for line in caught.output),
                        caught.output)

    def test_nothing_is_drained_when_nothing_failed(self):
        # The drain costs up to RTU_DRAIN_SECONDS, so it happens after a
        # failure and at no other time.
        pump = RtuPump({0: 42})
        self.addCleanup(pump.close)
        mb = ModbusTCP("127.0.0.1", pump.port, unit=1, timeout=2.0, framing="rtu")
        self.addCleanup(mb.close)
        started = time.monotonic()
        for _ in range(5):
            mb.read(4, 0, 1)
        self.assertLess(time.monotonic() - started, 5 * RTU_DRAIN_SECONDS)


class TheOfflineAdviceOnRtu(unittest.TestCase):
    """A gateway that connects and then answers nothing has three causes.

    A wrong `unit`, the wrong `framing`, and a MODBUS 40 that was never
    activated in menu 5.2 all look identical from here, because a MODBUS 40
    that is not being addressed says nothing rather than saying no. Naming all
    three beats guessing between them.
    """

    def _silent(self, framing):
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        self.addCleanup(srv.close)
        threading.Thread(target=lambda: [srv.accept() for _ in range(9)],
                         daemon=True).start()
        mb = ModbusTCP("127.0.0.1", srv.getsockname()[1], unit=3, timeout=0.3,
                       framing=framing)
        self.addCleanup(mb.close)
        with self.assertRaises(ModbusOffline) as caught:
            mb.read(4, 0, 1)
        return str(caught.exception)

    def test_rtu_names_the_unit_the_framing_and_the_accessory(self):
        message = self._silent("rtu")
        self.assertIn("unit", message)
        self.assertIn("5.3.11", message)
        self.assertIn("unit 3", message)
        self.assertIn("framing", message)

    def test_tcp_says_none_of_it(self):
        # There is no MODBUS 40 in a Modbus TCP install and no menu 5.3.11 on
        # an S-series pump; this advice there is noise pointing at hardware the
        # owner does not have.
        self.assertNotIn("5.3.11", self._silent("tcp"))


class TheFramingSetting(unittest.TestCase):
    def test_tcp_is_the_default(self):
        self.assertEqual(ModbusTCP("127.0.0.1", 502).framing, "tcp")

    def test_both_spellings_are_accepted(self):
        for value in ("tcp", "TCP", "rtu", " RTU "):
            self.assertIn(ModbusTCP("127.0.0.1", 502, framing=value).framing,
                          ("tcp", "rtu"))

    def test_anything_else_is_refused_at_construction(self):
        # Rather than at the first read, which is minutes later and looks like
        # a network fault.
        with self.assertRaises(ValueError):
            ModbusTCP("127.0.0.1", 502, framing="rtuovertcp")


if __name__ == "__main__":
    unittest.main()
