"""Numbers inside Swedish sentences.

The web app formats every number it produces itself with a decimal comma and
U+2212 MINUS SIGN, because that is how Swedish writes numbers and because NIBE's
own manual does ("2,5 °C"). The Python side writes Swedish sentences too, and
the page prints those verbatim -- so "Framledningen (38.4 °C)" and "Egen kurva,
punkt P3 (-10 °C ute)" appeared next to numbers the page had formatted properly,
in the same panel, sometimes in the same row. The second one is a *title*: it
lands in the proposal heading, in the confirm dialog and in the toast.

autotune.py had a _sv() helper for exactly this and the other five modules did
not. The helper now lives in the package root; this file tests it, and then
guards against the next Swedish sentence that formats a number by hand.
"""
import ast
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import nibelokal                                               # noqa: E402
from nibelokal import MINUS, sv_number                         # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Modules whose user-facing strings are Swedish and are printed by the page.
#: __main__.py is the command line and prints English; server.py's only
#: formatted number is in the English start-up banner.
SWEDISH_MODULES = ["advisor.py", "alarms.py", "autotune.py", "homey.py",
                   "pump.py", "spot.py", "weather.py", "safety.py",
                   "settings.py"]

#: A number formatted for a programmer: %g drops the decimal comma, %+d writes
#: a hyphen where a minus sign belongs, and %.2f writes a point.
BY_HAND = re.compile(r"%[-+ #0]*\d*\.?\d*[gGfeE]|%\+\d*d")

#: Anything with one of these in it is Swedish prose rather than a log line, a
#: URL or a GraphQL query.
SWEDISH = re.compile(
    r"[åäöÅÄÖ]|\b(och|inte|som|för|den|det|är|på|att|med|till|av|i|ligger)\b")

#: Strings that format a number by hand and are right to. A coordinate is not
#: a measurement in prose: it is the identifier the owner typed into
#: config.yaml, in the notation SMHI's own API uses, and echoing it back in a
#: different notation than they wrote it in is how "check the order of your
#: latitude and longitude" stops being checkable.
EXEMPT = ("SMHI har ingen prognos för",)


class TheNumberFormatter(unittest.TestCase):
    def test_a_decimal_comma(self):
        self.assertEqual(sv_number(38.4), "38,4")
        self.assertEqual(sv_number(0.42, 2), "0,42")

    def test_a_real_minus_sign_and_not_a_hyphen(self):
        self.assertEqual(sv_number(-10, 0), MINUS + "10")
        self.assertEqual(MINUS, "−")
        self.assertNotIn("-", sv_number(-7.5))

    def test_decimals_none_prints_like_percent_g(self):
        # "kurvan står på 5", not "kurvan står på 5,0".
        self.assertEqual(sv_number(5.0, None), "5")
        self.assertEqual(sv_number(5, None), "5")
        self.assertEqual(sv_number(38.4, None), "38,4")

    def test_a_sign_is_forced_where_a_curve_point_needs_one(self):
        self.assertEqual(sv_number(10, None, sign=True), "+10")
        self.assertEqual(sv_number(-10, None, sign=True), MINUS + "10")
        # Zero gets no sign: "+0 °C ute" is not how anybody writes it.
        self.assertEqual(sv_number(0, None, sign=True), "0")

    def test_nothing_is_an_en_dash_and_not_a_crash(self):
        self.assertEqual(sv_number(None), "–")
        self.assertEqual(sv_number(None, None, sign=True), "–")

    def test_something_that_is_not_a_number_is_handed_back_as_it_came(self):
        # A mapped register decodes to a label, and a label is not a number.
        self.assertEqual(sv_number("Medium"), "Medium")

    def test_the_order_of_the_two_replacements(self):
        # The comma first, then the sign: a "-" already turned into U+2212
        # would not be found by a later replace, and "-38.4" would come out
        # half-converted. One number that exercises both.
        self.assertEqual(sv_number(-38.45, 2), MINUS + "38,45")


class NoSwedishSentenceFormatsItsOwnNumbers(unittest.TestCase):
    """A guard, not a proof: it reads the source for the pattern that bit.

    Every hit is either a string that should go through sv_number, or a string
    that is not Swedish and should be added to the reasoning above rather than
    silently ignored.
    """

    def _offenders(self, filename):
        path = os.path.join(ROOT, "nibelokal", filename)
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        tree = ast.parse(source)
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc is not None:
                    docstrings.add(doc)
        out = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            text = node.value
            if text in docstrings:
                continue
            if any(text.startswith(prefix) for prefix in EXEMPT):
                continue
            if BY_HAND.search(text) and SWEDISH.search(text):
                out.append("%s:%d %r" % (filename, node.lineno, text[:70]))
        return out

    def test_none_of_the_swedish_modules_do(self):
        offenders = []
        for filename in SWEDISH_MODULES:
            offenders += self._offenders(filename)
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_the_guard_would_notice(self):
        # A guard nobody has seen fail is a guard nobody should trust.
        self.assertTrue(BY_HAND.search("Framledningen (%g °C) ligger i taket"))
        self.assertTrue(BY_HAND.search("punkt P3 (%+d °C ute)"))
        self.assertTrue(BY_HAND.search("%.2f kr/kWh för den timmen"))
        self.assertTrue(SWEDISH.search("Framledningen (%g °C) ligger i taket"))
        # And not on the things it must leave alone.
        self.assertFalse(BY_HAND.search("register %d (%s) är skrivskyddat"))


class TheVersion(unittest.TestCase):
    def test_there_is_one_and_it_is_the_released_one(self):
        self.assertEqual(nibelokal.__version__, "0.3.2")


if __name__ == "__main__":
    unittest.main()
