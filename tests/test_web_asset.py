"""The web app is a file we ship, so check it is a whole one.

v0.3.0 shipped `web/index.html` truncated at line 680, in the middle of an
empty `<script>` tag: no JavaScript, no closing tag, no `</body>`. It was
pushed to GitHub and deployed, and every one of the 241 tests then in the
suite passed, because not one of them looked at the file. The app rendered a
dead screen -- correct colours, correct header, no data and no working tabs --
for about a day, and it took a person opening it to notice.

These tests are cheap and they are structural: they do not know whether the
page is any good, only that it is complete enough to be worth loading. The
browser-level checks live in tests/ui_smoke_new.py, which needs Playwright;
these run everywhere, including on a machine with no browser at all.
"""
from __future__ import annotations

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")
INDEX = os.path.join(WEB, "index.html")

#: The page was 130 kB when this test was written. A floor well under that
#: catches a truncation without failing every time someone deletes a card.
MIN_BYTES = 60_000


class TheShippedPage(unittest.TestCase):
    def setUp(self):
        with open(INDEX, encoding="utf-8") as fh:
            self.html = fh.read()

    def test_is_not_suspiciously_small(self):
        self.assertGreater(
            len(self.html.encode("utf-8")), MIN_BYTES,
            "web/index.html is smaller than any complete version has ever been. "
            "A truncated write is the likeliest explanation.")

    def test_every_structural_tag_is_closed(self):
        for tag in ("html", "head", "body", "script", "style"):
            opens = len(re.findall(r"<%s[\s>]" % tag, self.html, re.I))
            closes = len(re.findall(r"</%s>" % tag, self.html, re.I))
            self.assertEqual(
                opens, closes,
                "web/index.html has %d <%s> and %d </%s>. An unclosed tag at the "
                "end of the file is what truncation looks like." % (opens, tag, closes, tag))

    def test_the_document_ends_where_a_document_ends(self):
        self.assertTrue(
            self.html.rstrip().endswith("</html>"),
            "web/index.html does not end with </html>; it ends with %r"
            % self.html.rstrip()[-60:])

    def test_the_behaviour_layer_is_present(self):
        script = re.search(r"<script>(.*?)</script>", self.html, re.S)
        self.assertIsNotNone(script, "no <script> block with content in web/index.html")
        body = script.group(1)
        self.assertGreater(
            len(body), 20_000,
            "the <script> block is %d characters. The page has never worked with "
            "less; an empty or stub script renders a dead screen that looks fine "
            "in a screenshot." % len(body))
        # Named because losing any one of them is a silent loss of a whole
        # feature rather than a visible error: the poll loop, the write path,
        # navigation, and the number formatting the whole UI depends on.
        for symbol in ("async function", "fetch(", "data-view", "function num"):
            self.assertIn(symbol, body,
                          "web/index.html's script no longer contains %r" % symbol)

    def test_it_is_self_contained(self):
        # No build step, no CDN, no network at load: the app has to work on a
        # LAN with no route to the internet, which is the normal case for a
        # heat pump in a cellar.
        for pattern in (r'<script[^>]+src=', r'<link[^>]+rel=["\']?stylesheet'):
            self.assertIsNone(
                re.search(pattern, self.html, re.I),
                "web/index.html pulls in an external asset (%s). The page must "
                "load with no network." % pattern)


class TheProgressiveWebAppFiles(unittest.TestCase):
    def test_the_service_worker_and_manifest_are_whole(self):
        for name, must_contain in (("sw.js", "addEventListener"),
                                   ("manifest.webmanifest", "start_url")):
            path = os.path.join(WEB, name)
            if not os.path.exists(path):
                self.skipTest("web/%s is not part of this checkout" % name)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn(must_contain, text, "web/%s looks truncated" % name)

    def test_the_service_worker_never_caches_the_api(self):
        path = os.path.join(WEB, "sw.js")
        if not os.path.exists(path):
            self.skipTest("web/sw.js is not part of this checkout")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("/api/", text,
                      "web/sw.js says nothing about /api/. A cached reading of a "
                      "heat pump is a lie with a timestamp on it.")


if __name__ == "__main__":
    unittest.main()
