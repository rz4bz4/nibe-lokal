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
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nibelokal.modbus import ModbusError, ModbusOffline, ModbusTCP    # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
