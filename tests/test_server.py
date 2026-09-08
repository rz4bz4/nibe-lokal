"""The HTTP front door, driven over a real socket by a hostile client.

Every test here was a reproduced defect, and they share one shape: input a
browser would never send, answered with a stack trace, a 502, or nothing at
all. This app listens on a home LAN with an auth token in front of it, but the
things that reach it are not only browsers -- a phone that walks out of WiFi
mid-POST is the realistic case for half of this file, and a port scanner is
the realistic case for the other half.

Raw sockets rather than urllib, because urllib will not send `Content-Length:
-1` or announce five hundred bytes and then send two. That is the point.
"""
import json
import logging
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nibelokal import config, server                            # noqa: E402
from nibelokal.profile import Profile                           # noqa: E402
from nibelokal.registry import Register                         # noqa: E402
from nibelokal.store import Poller, Store                       # noqa: E402

TOKEN = "hemligt"

#: Short, because two of these tests wait one out. The shipped value is
#: server.REQUEST_TIMEOUT; that it exists at all is what test_the_timeout_is_set
#: asserts.
TEST_TIMEOUT = 1.5


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


class FakeRegister:
    def __init__(self, address):
        self.address = address
        self.title = "register %d" % address
        self.unit = "°C"


class FakeRegistry:
    source = "fake"

    def get(self, address):
        return FakeRegister(address)

    def search(self, needle, writable=False):
        return []


class FakePump:
    """Answers plausibly, never touches a socket, and encodes for real.

    `write` runs the value through a real registry.Register so that a value no
    register can hold travels the whole way a real one would.
    """

    host = "192.0.2.10"
    port = 502

    def __init__(self):
        self.registry = FakeRegistry()
        # An S-series profile is the identity, so this fake behaves exactly as
        # it did before the profile existed. See nibelokal/profile.py.
        self.profile = Profile("S", "S735")
        self.values = {30002: 3.4, 30006: 32.0, 31976: 0, 40012: -120,
                       40027: 5, 40031: 0}
        self.written = []

    def register(self, address):
        if not self.profile.available(address):
            return None
        return self.registry.get(self.profile.physical(address))

    def read_many(self, addresses):
        return {a: {"value": self.values[a]} for a in addresses if a in self.values}

    def read(self, address):
        return self.values.get(address)

    def missing(self):
        return []

    def extra_hot_water(self, minutes, off=False):
        return {"minutes": minutes, "off": off, "writes": []}

    def ventilate(self, direction="up", hours=3, mode=None):
        return {"direction": direction, "hours": hours, "mode": mode,
                "writes": []}

    def write(self, address, value, confirmed=False, expect=None):
        reg = Register(address=address, title="offset", size="s16", factor=1,
                       mode="RW", unit="")
        coerced = reg.coerce(value)
        self.written.append((address, coerced))
        return {"address": address, "title": reg.title, "before": 0,
                "requested": coerced, "after": coerced, "tier": "safe",
                "writes": []}


class ServerTestCase(unittest.TestCase):
    """One server per class, on a port the kernel picks, on the loopback."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store = Store(os.path.join(cls.tmp.name, "nibe.db"))
        cls.pump = FakePump()
        cfg = dict(config.DEFAULTS)
        cfg.update({"host": "192.0.2.10", "model": "S735", "auth_token": TOKEN})
        providers = server.build_providers(cls.pump, cfg, cls.store)
        poller = Poller(cls.pump, cls.store, [30002], 60)

        class Handler(server.Handler):
            timeout = TEST_TIMEOUT

        Handler.ctx = server.build_context(
            cls.pump, cfg, cls.tmp.name, cls.store, poller, providers,
            allowed_hosts={"localhost", "127.0.0.1"})
        cls.handler = Handler
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        cls.store.close()
        cls.tmp.cleanup()
        cls.handler.ctx = {}

    # -- a client that does not play nicely --------------------------------

    def raw(self, request: bytes, wait: float = 5.0, keep: bool = False):
        """Send exactly these bytes; return (status, body-text, socket).

        The socket is handed back unclosed when `keep`, so a test can check
        that the server hung up on it by itself.
        """
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=wait)
        sock.sendall(request)
        sock.settimeout(wait)
        data = b""
        try:
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
        except socket.timeout:
            pass
        if not keep:
            sock.close()
        text = data.decode("utf-8", "replace")
        status = int(text.split(" ")[1]) if text.startswith("HTTP/") else None
        body = text.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in text else ""
        return status, body, sock

    def post_raw(self, path, headers, payload=b"", **kwargs):
        head = ["POST %s HTTP/1.1" % path, "Host: 127.0.0.1:%d" % self.port,
                "Content-Type: application/json", "X-Auth-Token: " + TOKEN]
        head += headers
        return self.raw(("\r\n".join(head) + "\r\n\r\n").encode("utf-8") + payload,
                        **kwargs)

    def post(self, path, body, token=TOKEN):
        payload = json.dumps(body).encode("utf-8")
        head = ["POST %s HTTP/1.1" % path, "Host: 127.0.0.1:%d" % self.port,
                "Content-Type: application/json", "X-Auth-Token: " + token,
                "Content-Length: %d" % len(payload), "Connection: close"]
        return self.raw(("\r\n".join(head) + "\r\n\r\n").encode("utf-8") + payload)[:2]

    def get(self, path, token=TOKEN, extra=()):
        head = ["GET %s HTTP/1.1" % path, "Host: 127.0.0.1:%d" % self.port,
                "Connection: close"]
        if token is not None:
            head.append("X-Auth-Token: " + token)
        head += list(extra)
        return self.raw(("\r\n".join(head) + "\r\n\r\n").encode("utf-8"))[:2]


class ABodyThatLies(ServerTestCase):
    """Content-Length is a claim by the client, and it was believed.

    All three of these were reproduced against the shipped server: the first
    read until the client closed the connection, which a client that never
    closes never does; the second parked the thread in rfile.read() with its
    socket open for ever; the third arrived as an AttributeError from
    body.get() and left as a 502 with a stack trace.
    """

    def test_a_negative_content_length_is_refused_not_read(self):
        # int("-1") is perfectly happy and rfile.read(-1) reads to EOF.
        status, body, _ = self.post_raw("/api/hotwater", ["Content-Length: -1"])
        self.assertEqual(status, 400)
        self.assertIn("Content-Length", body)

    def test_a_content_length_that_is_not_a_number(self):
        status, _, _ = self.post_raw("/api/hotwater", ["Content-Length: nio"])
        self.assertEqual(status, 400)

    def test_a_body_larger_than_anything_here_has_a_use_for(self):
        status, _, _ = self.post_raw(
            "/api/write", ["Content-Length: %d" % (server.MAX_BODY_BYTES + 1)])
        self.assertEqual(status, 400)
        # And refused on the header alone: the megabyte was never read.

    def test_a_body_shorter_than_it_promised_is_a_bad_request(self):
        status, _, _ = self.post_raw("/api/hotwater", ["Content-Length: 500"],
                                     b'{"m', wait=TEST_TIMEOUT + 4)
        self.assertEqual(status, 400)

    def test_and_the_connection_does_not_stay_open_for_ever(self):
        """The phone that walked out of WiFi in the middle of a POST.

        Before the timeout this thread sat in rfile.read(500) with two of the
        five hundred bytes in hand, holding a thread and two descriptors, until
        the process was restarted. Measured then: the thread count grew and
        never fell.
        """
        before = threading.active_count()
        status, _, sock = self.post_raw("/api/hotwater", ["Content-Length: 500"],
                                        b'{"m', wait=TEST_TIMEOUT + 4, keep=True)
        try:
            self.assertEqual(status, 400)
        finally:
            sock.close()
        deadline = time.time() + 10
        while time.time() < deadline and threading.active_count() > before:
            time.sleep(0.05)
        self.assertLessEqual(threading.active_count(), before,
                             "the handler thread never ended")

    def test_a_client_that_says_nothing_at_all_is_hung_up_on(self):
        sock = socket.create_connection(("127.0.0.1", self.port),
                                        timeout=TEST_TIMEOUT + 5)
        try:
            sock.settimeout(TEST_TIMEOUT + 5)
            # Not one byte sent. The base class blocks in readline() for ever
            # without a timeout on the handler.
            self.assertEqual(sock.recv(4096), b"",
                             "the server kept the silent connection open")
        finally:
            sock.close()

    def test_the_timeout_is_set_on_the_shipped_handler_too(self):
        self.assertTrue(server.Handler.timeout)
        self.assertLessEqual(server.Handler.timeout, 120)

    def test_a_json_body_that_is_not_an_object(self):
        for payload in ("[1, 2]", '"hej"', "null", "3"):
            status, body = self.post("/api/hotwater", json.loads(payload))
            self.assertEqual(status, 400, payload)
            self.assertIn("JSON", body)


class HostileValues(ServerTestCase):
    """Well-formed JSON carrying values no register can hold.

    All of these answered 502 -- "the pump is broken" -- for what is a bad
    request. The difference matters when somebody is trying to work out why
    their heat pump app is red.
    """

    def test_a_null_where_a_number_belongs(self):
        status, body = self.post("/api/hotwater", {"minutes": None})
        self.assertEqual(status, 400)
        self.assertIn("minutes", body)

    def test_but_leaving_it_out_altogether_still_means_the_default(self):
        # Absent is "I did not say"; null is a client that thinks it did.
        status, body = self.post("/api/hotwater", {})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["minutes"], 180)

    def test_a_null_mode_for_the_fan_too(self):
        status, _ = self.post("/api/ventilation", {"hours": None})
        self.assertEqual(status, 400)

    def test_a_number_that_is_not_one(self):
        status, _ = self.post("/api/hotwater", {"minutes": "snart"})
        self.assertEqual(status, 400)

    def test_infinity_out_of_a_json_body(self):
        # json.loads("1e999") is float("inf"), and int(round(inf)) is an
        # OverflowError -- which is not a ValueError, so it went out as a 502.
        status, body = self.raw(
            b"POST /api/write HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\nX-Auth-Token: " + TOKEN.encode()
            + b"\r\nContent-Length: 32\r\nConnection: close\r\n\r\n"
            b'{"address": 40031, "value": 1e999}')[:2]
        self.assertEqual(status, 400)

    def test_a_limit_larger_than_sqlite_can_count_to(self):
        status, _ = self.get("/api/log?limit=" + "1" + "0" * 20)
        self.assertEqual(status, 400)

    def test_the_same_for_every_endpoint_that_takes_a_limit(self):
        for path in ("/api/log?limit=%s", "/api/alarms?limit=%s",
                     "/api/history?hours=%s", "/api/autotune?days=%s"):
            status, _ = self.get(path % ("1" + "0" * 20))
            self.assertEqual(status, 400, path)

    def test_a_register_address_that_is_not_a_number(self):
        status, _ = self.get("/api/register/inte-ett-nummer")
        self.assertEqual(status, 400)

    def test_a_sensible_request_still_works(self):
        status, body = self.get("/api/log?limit=5")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), [])


class TheToken(ServerTestCase):
    def test_a_token_with_a_character_above_latin1_is_a_401(self):
        """compare_digest raises TypeError on a str with an astral character.

        _authorised() is called outside the try that turns exceptions into
        responses, so this dropped the connection with a traceback instead of
        answering 401 -- a scanner's way of finding out that the app is there.
        """
        status, _ = self.get("/api/status", token="hemligt☃")
        self.assertEqual(status, 401)

    def test_an_emoji_token_too(self):
        status, _ = self.get("/api/status", token="🔑🔑🔑")
        self.assertEqual(status, 401)

    def test_the_right_token_still_gets_in(self):
        status, _ = self.get("/api/status")
        self.assertEqual(status, 200)

    def test_the_wrong_one_still_does_not(self):
        status, _ = self.get("/api/status", token="fel")
        self.assertEqual(status, 401)


class TheRequestLog(unittest.TestCase):
    """`?token=` goes in the DEBUG log under -v, and logs get pasted around.

    The app itself tells people to use `?token=` once, from a phone, because
    typing a header into a browser is not a thing. That is precisely how the
    token ends up in a URL and then in a log file.
    """

    def test_the_token_is_replaced_in_the_query_string(self):
        line = server._redact('"GET /api/status?token=hemligt&live=1 HTTP/1.1" 200 -')
        self.assertNotIn("hemligt", line)
        self.assertIn("<dold>", line)
        # Everything else about the request is still there: the point is a
        # usable log, not a redacted one.
        self.assertIn("/api/status", line)
        self.assertIn("live=1", line)

    def test_it_is_the_first_parameter_or_a_later_one(self):
        for url in ("/x?token=abc", "/x?live=1&token=abc", "/x?token=abc&live=1"):
            self.assertNotIn("abc", server._redact(url), url)

    def test_a_request_with_no_token_in_it_is_untouched(self):
        line = '"GET /api/status HTTP/1.1" 200 -'
        self.assertEqual(server._redact(line), line)

    def test_something_merely_called_token_elsewhere_is_left_alone(self):
        # The header is not in this string, and a path that happens to contain
        # the word is not a secret.
        self.assertIn("tokens", server._redact("/api/tokens"))


if __name__ == "__main__":
    unittest.main()
