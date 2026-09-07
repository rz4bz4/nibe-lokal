"""nibe-lokal - talk to a NIBE S-series heat pump over local Modbus TCP."""

#: The one version string. pyproject.toml reads it from here
#: ([tool.setuptools.dynamic]) and weather.py puts it in the User-Agent SMHI
#: sees, so there is nothing left to keep in step by hand. Three copies used to
#: disagree -- the package said 0.2.1, the wheel 0.3.0 and the git tag v0.3.1 --
#: and SMHI was told the oldest of the three.
__version__ = "0.3.2"

#: A real minus sign (U+2212), not a hyphen. The web app uses it for every
#: number it formats itself, and settings.py already uses it in its titles; a
#: hyphen next to those reads as a different app.
MINUS = "−"


def sv_number(value, decimals: int = 1, sign: bool = False) -> str:
    """A number as Swedish prose: decimal comma and a real minus sign.

    Here, in the package root, because it has no dependencies and every layer
    needs it: the pump refuses writes in Swedish, the advisor and the spot
    planner write Swedish sentences with numbers in them, and autotune has had
    its own copy of this since it was written. "Framledningen (38.4 °C)" in the
    middle of a Swedish sentence reads as a typo -- particularly next to NIBE's
    own "2,5 °C" from the manual, and next to the same number the page formats
    itself two lines further down.

    `decimals=None` prints the number the way "%g" would (no trailing zeros),
    which is what most of the running text wants: "kurvan står på 5", not
    "kurvan står på 5,0".
    """
    if value is None:
        return "–"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if decimals is None:
        text = "%g" % number
        if sign and number > 0:
            text = "+" + text
    else:
        text = ("%+.*f" if sign else "%.*f") % (int(decimals), number)
    # Order matters: the comma first, then the sign, or a "-" already turned
    # into U+2212 would not be found by the second replace.
    return text.replace(".", ",").replace("-", MINUS)
