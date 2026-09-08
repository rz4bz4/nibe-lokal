"""Which generation of pump this is, and what its registers are called.

Everything in this app above `pump.py` -- the dashboard list, the settings
editor, the advisor, the alarm watcher, and every register number written into
`web/index.html` -- speaks **S-series register numbers**. That was not a
decision so much as an accident of history: the app was written against one
S735, and its numbers became the app's vocabulary.

The F generation numbers the same concepts differently. Not partially: the
heating curve is 40027 on an S735 and 47007 on an F750, the offset 40031 and
47011, the outdoor sensor 30002 and 40004. Measured against the F750 map,
`settings.py` found 13 of its 36 rows and `pump.DASHBOARD` 2 of its 25 -- and
the heating advice regulated on registers the pump does not have. It looked
like it worked and did nothing, which is worse than not supporting the F series
at all.

Worse still, some of those numbers are *not absent* on an F pump, they are
somebody else. 40031 on an F750 is `EP22-BT50 Room Temp S3`. 40020, which is
`Holiday function status` on an S735, is `EB100-BT16 Evaporator temp`. A
number that means one thing on one pump and another thing on another is the
exact failure this module exists to prevent.

So: **canonical addresses in, physical addresses out, translated in one place.**
Everything above `pump.py` goes on speaking S-series numbers, `pump.py`
translates at the Modbus boundary, and on an S-series pump the translation is
the identity -- byte for byte the behaviour the author's own pump has today.

The table below is hand-built. Title matching does not work: NIBE renamed
nearly everything between the generations ("Heating offset climate system 1"
became "Heat Offset S1"), and matching the 58 addresses this app uses by title
against the F750 map found exactly one. Every row was read out of
`Model.F750.get_coil_data()` and checked against `Model.S735`'s entry for the
canonical address: same meaning, same unit, same division factor, same
writability. `tests/test_profile.py` re-checks all of that, on every F map the
`nibe` package ships, and fails the build if a row drifts.

The profile also carries the four things that are true of the *transport* on
each generation rather than of the register numbering: how many registers one
request may ask for, how far it may read through a gap, how long a request may
take, and which of the two 16-bit words of a 32-bit value comes first. Those
were module constants until an F-series pump turned out to allow one register
per request rather than twenty (docs/f-series.md), and a constant that is right
for one generation and silently wrong for the other is the same class of bug
as a register number that means something else.

**Nothing here has been run against a real F-series pump.** That is what
`verified` is False for on an F profile, and why the web app puts a line at the
top of the page saying so. Whoever has one: an issue with a `nibelokal backup`
of your own pump would settle every question this file guesses at, and the
questions it refuses to guess at are listed in NO_F_EQUIVALENT below.
"""
from __future__ import annotations

#: F-series models the `nibe` package ships maps for, by its own enum names.
#:
#: Decided by reading the maps rather than by the model name, because the name
#: does not decide it. "SMO 20" and "SMO 40" are F-generation numbering while
#: "SMO S40" is S-generation, and the VVMs split the same way: VVM 225, 310,
#: 320, 325 and 500 are F, VVM S320, S325 and S500 are S. The test that
#: proves it is one line: an F map has 47007 `Heat Curve S1` and no 40027, an
#: S map has 40027 `Heating curve climate system 1` and no 47007. Those two
#: facts partition all 32 models the package ships with no overlap and no
#: leftovers -- see tests/test_profile.py, which asserts exactly that, so a new
#: model added to the `nibe` package fails the build rather than silently
#: landing in whichever generation the fallback prefers.
F_MODELS = frozenset({
    # Exhaust-air, the F-generation siblings of the S735 this app was written
    # against. F750 and F730 are the closest relatives: same shape, same
    # ventilation-as-heat-source, same exhaust fan.
    "F750", "F730", "F370", "F470",
    # Ground source. No exhaust fan, so the fan rows below simply do not exist
    # in their maps and drop out of every list by themselves.
    "F1155", "F1255", "F1145", "F1245", "F1345", "F1355",
    # Controllers and indoor units on the F-generation numbering.
    "SMO20", "SMO40",
    "VVM225", "VVM310", "VVM320", "VVM325", "VVM500",
})

#: S-series models, listed rather than inferred for the same reason.
S_MODELS = frozenset({
    "S735", "S735C", "S1155", "S1255", "S1156", "S1256",
    "S320", "S325", "S330", "S332", "S2125",
    "SMOS40",
    "VVMS320", "VVMS325", "VVMS500",
})

#: The two registers that tell the generations apart, for the error message
#: and for the test. Kept next to the lists they were derived from.
S_MARKER, F_MARKER = 40027, 47007


def generation_from_addresses(addresses, source: str = "") -> str:
    """"S" or "F", decided by which marker register the map contains.

    The same one-line test the model lists above were partitioned by, applied
    to a map instead of to a name: an S map has 40027 "Heating curve climate
    system 1" and no 47007, an F map has 47007 "Heat Curve S1" and no 40027.
    That holds for all 32 models the `nibe` package ships (tests/test_profile.py
    asserts it), and it is the only thing a CSV exported from a pump carries
    that says which generation numbered it -- the export has no model name in
    it at all.

    Refused loudly when the map has both markers or neither: this decides how
    every register number in the app is translated, and the failure mode of
    guessing is not an error, it is plausible nonsense.
    """
    addresses = set(addresses)
    has_s, has_f = S_MARKER in addresses, F_MARKER in addresses
    if has_s and not has_f:
        return "S"
    if has_f and not has_s:
        return "F"
    where = (" (%s)" % source) if source else ""
    found = ("both %d and %d" % (S_MARKER, F_MARKER) if has_s
             else "neither %d nor %d" % (S_MARKER, F_MARKER))
    raise ValueError(
        "Cannot tell which generation this register map%s is for: it contains "
        "%s.\n\n"
        "The heating curve is register %d on the S generation (S735, S1155, "
        "S320, SMO S40, VVM S320) and %d on the F generation (F750, F1155, "
        "SMO 20/40, VVM 225/320/500), and every map the `nibe` package ships "
        "has exactly one of the two. Set `generation` to S or F in config.yaml "
        "to say which this one is."
        % (where, found, S_MARKER, F_MARKER))


# ---------------------------------------------------------------------------
# What the transport allows, per generation
# ---------------------------------------------------------------------------
#
# These were module constants in modbus.py and pump.py, which is where they
# belonged while there was one generation. They are per-generation limits of
# the *accessory*, not of the pump, and the two generations do not reach the
# bus the same way at all: an S-series pump is Modbus TCP straight into the
# pump, an F-series one is a MODBUS 40 board speaking RTU at 9600 baud.
#
# The F numbers are NIBE's own table, quoted in docs/f-series.md ("What MODBUS
# 40 can and cannot do"): a read of registers *not* in the module's 20-entry
# LOG.SET file is limited to 1-2 registers per request -- two only because a
# 32-bit parameter occupies two -- with a 2.1 s maximum timeout. Users on the
# LogicMachine forum report the 2.1 s as real spacing between requests rather
# than as a timeout that is rarely reached.
#
# So on an F pump this app asks for exactly one register at a time. It does not
# build a LOG.SET file and does not require one: LOG.SET is a cache, not a
# whitelist, and every register is readable without it -- slowly. What that
# costs is written down in config.example.yaml next to poll_seconds, because a
# poll that takes forty seconds is a thing to know before it surprises you.

#: Registers one read request may ask for. 20 is NIBE's documented S-series
#: ceiling; 1 is MODBUS 40's limit for anything outside LOG.SET, and a 32-bit
#: register still goes out as its two words in one request (that is the "1-2"
#: in NIBE's table, and the only reason a request here is ever two words long).
MAX_REGS_PER_QUERY = {"S": 20, "F": 1}

#: How many unwanted registers may be read through to keep two wanted ones in
#: the same request. Worth it when a request costs milliseconds; on an F pump
#: a request costs seconds and there is no batching to serve, so: none.
READ_GAP = {"S": 3, "F": 0}

#: The smallest per-request timeout this generation's transport needs, in
#: seconds. The configured `timeout` is raised to it and never lowered, so an
#: S-series install keeps exactly the timeout it has today. NIBE documents
#: 2.1 s as MODBUS 40's maximum for a register outside LOG.SET; 2.5 leaves the
#: margin that a documented maximum is not a measured round trip.
MIN_REQUEST_TIMEOUT = {"S": 0.0, "F": 2.5}

#: Which of the two 16-bit registers of a 32-bit value arrives first.
#:
#: True means the LOW word is at the lower address, which is what NIBE's
#: S-series TIF (EN 2608 p.6) says -- "when reading multiple registers, the
#: registers are shown in reverse order" -- and what this app has always done.
#:
#: On the F generation it is a *setting*, in the pump's menu 5.3.11, and it
#: only exists from MODBUS 40 software v.11. **What its factory value is, the
#: sources disagree about**, and this default used to take the wrong side.
#:
#: The MODBUS 40 installer manual says "Factory setting: Big Endian", which
#: reads as the high word first. Three other sources say the opposite -- two
#: independent readings, since the third shares the second's lineage -- and
#: between them they are what an F owner's pump has actually been talking to:
#:
#: * The register maps themselves. All seventeen F maps the `nibe` package
#:   ships, and none of its S maps, carry **48852 "Modbus40 Word Swap", u8,
#:   writable, default 1**, whose info text is "If set; swapping the words in
#:   32-bit variables when value requested via 'read holding register'
#:   commando". Swapped, from a factory default of 1, is the low word first.
#: * The `nibe` package decodes `word_swap=True` as the low word first, and
#:   tells users to set it False only if they turned the swap off in 5.3.11.
#: * Home Assistant's nibe_heatpump integration defaults `word_swap` to True --
#:   which is the same reading, since that integration is built on `nibe`.
#:
#: A register with a documented factory value of 1 beats a sentence in a manual
#: about what "Big Endian" means, so the default here is the same as the S
#: series: low word first. It is still only a default. Two things override it,
#: in this order: `word_swap` in config.yaml, and then -- because 48852 is a
#: register and can simply be asked -- the pump's own answer, read once at
#: startup by `Pump.resolve_word_order`. `as_dict` reports which of the three
#: decided, because a backup taken with the wrong one is full of plausible
#: nonsense in exactly the 32-bit registers and nowhere else.
LOW_WORD_FIRST = {"S": True, "F": True}

#: The register that settles the question above, on an F-series pump.
#:
#: "Modbus40 Word Swap": 1 means the words are swapped, which is the low word
#: first; 0 means they are not, which is the high word first. Present in every
#: F map the `nibe` package ships and in none of the S ones -- there the order
#: is NIBE's TIF and not a setting, so there is nothing to ask.
F_WORD_SWAP_REGISTER = 48852

#: The outdoor temperature each own-curve point governs, P1 (coldest) first.
#:
#: On the S series this is NIBE's own menu 1.30.7: seven points, -30 to +30 in
#: ten-degree steps. On the F series the register map also has seven points
#: with identical defaults, but NIBE's F750 and F1155 user manuals both show
#: menu 1.9.7 with exactly **six** rows, -30 to +20. P7 exists as a register;
#: what outdoor temperature it governs is not established (docs/f-series.md,
#: "The seventh own-curve point"). None says so, and everything that
#: interpolates skips a point whose temperature is None rather than guessing
#: +30 -- an unverified point has no business in a fit that decides how warm a
#: house is.
OWN_CURVE_OUTDOOR = {
    "S": (-30, -20, -10, 0, 10, 20, 30),
    "F": (-30, -20, -10, 0, 10, 20, None),
}


def parse_word_swap(value):
    """`word_swap` out of config.yaml: None, True or False.

    None is "" and means "use the generation's factory default". Anything that
    is neither a boolean nor empty is a mistake worth stopping for: a
    misspelled true here is every 32-bit register silently reading nonsense,
    which is the exact failure the setting exists to fix.
    """
    if value is None or isinstance(value, bool):
        return value
    text = str(value).strip().strip('"').strip("'").lower()
    if text == "":
        return None
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise ValueError(
        "word_swap must be true, false or empty (got %r). Empty means: ask the "
        "pump. On an F-series pump this app reads register %d 'Modbus40 Word "
        "Swap' at startup and uses what it says; if that register does not "
        "answer, and on the S series where the order is NIBE's own TIF and not "
        "a setting, it assumes the low word first."
        % (value, F_WORD_SWAP_REGISTER))


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------
#
# canonical (S-series) -> physical (F-series). Every row was read out of
# Model.F750.get_coil_data() and compared with Model.S735's entry for the
# canonical address. The comment on each row is the F title; where the S title
# says something different, both are given. Unit and factor agree on every row
# in this table -- that is asserted, not asserted-in-a-comment, by
# tests/test_profile.py, across F750, F730, F370, F470, F1155, F1255, F1145,
# F1245, F1345 and F1355.
#
# A row whose physical address is missing from a particular F model's map is
# not a problem and needs no per-model table: `registry.get()` returns None,
# and the app already drops a register the pump does not implement. That is how
# an F1155 (ground source, no exhaust fan) ends up with no fan rows without
# anything here knowing that it is ground source.

F_TABLE: dict[int, int] = {
    # -- what the house feels: the heating curve ---------------------------
    # S: "Heating curve climate system 1"   F: "Heat Curve S1"      s8, 0-15
    40027: 47007,
    # S: "Heating offset climate system 1"  F: "Heat Offset S1"     s8, -10..10
    #
    # This is the row that makes the whole module necessary: 40031 is a real
    # register on an F750, and it is EP22-BT50 Room Temp S3. Untranslated, the
    # advisor's "one step warmer" would have written a heating offset into a
    # read-only room temperature.
    40031: 47011,
    # The own curve, P1 (coldest) .. P7. Both generations count the points the
    # same way and both run them *downwards* in address order, so the pairing
    # is P-number to P-number and not address to address.
    # S: "Own curve, heating P1".."P7"      F: "Own Heating Curve P1".."P7"
    # s8, unit C, min 5 max 80 on both.
    #
    # The outdoor temperature each point governs is NOT the same claim on the
    # two generations, and this comment used to make it for both: P7 at +30 C
    # is NIBE's own menu 1.30.7 on the S series and an inference from register
    # order and identical defaults on the F, where both the F750 and the F1155
    # user manual show menu 1.9.7 with six rows ending at +20. That is what
    # OWN_CURVE_OUTDOOR above says with a None, and docs/f-series.md ("The
    # seventh own-curve point") is the reading it comes from. The mapping
    # itself is unaffected -- P7 is 47020 whatever weather it governs.
    40046: 47026,   # P1  -30 C
    40045: 47025,   # P2  -20 C
    40044: 47024,   # P3  -10 C
    40043: 47023,   # P4    0 C
    40042: 47022,   # P5  +10 C
    40041: 47021,   # P6  +20 C
    40040: 47020,   # P7  +30 C on the S series; unestablished on the F
    # S: "Point offset outdoor temperature" F: "Point offset outdoor temp."
    # s8, C, -40..30 on both.
    40047: 47027,
    # S: "Point offset"                     F: "Point offset"       s8, C, -10..10
    40048: 47028,
    # S: "Min supply climate system 1"      F: "Min Supply System 1"
    # s16, factor 10, C. The S735's floor is 20.0-80.0 C and the F750's
    # 5.0-70.0 C; both are the pump's own range, read from whichever map is
    # loaded, so `safety.clamp` still checks against the right one.
    40035: 47015,
    # S: "Max supply climate system 1"      F: "Max Supply System 1"  s16, f10, C
    40039: 47019,
    # S: "Auto mode, stop temperature for heating"  F: "Stop Temperature Heating"
    # s16, f10, C, -20.0..40.0 on both. The F info text names menu 4.9.2.
    40185: 47375,
    # S: "Heating start at under temp." has no F equivalent -- see NO_F_EQUIVALENT.
    # S: "Period time heating"              F: "Period Heat"        u8, min, 0..180
    40094: 47135,

    # -- what it costs: additional heat ------------------------------------
    # S: "Max. internal additional heat"    F: "Max int add. power"
    # s16, factor 100, kW, 0..45.00 on both.
    40103: 47212,
    # S: "Permit additional heat, heating"  F: "Allow Additive Heating"  u8 0/1
    # The F map's info adds "only valid for operational mode Manual or Add.
    # heat only", which is the same menu 4.1 permit the S register is; the S
    # map simply carries no info text.
    40181: 47370,
    # S: "Permit heating"                   F: "Allow Heating"      u8 0/1
    40182: 47371,
    # S: "Auto mode, additional heat stop temperature"
    # F: "Stop Temperature Additive"        s16, f10, C, -25.0..40.0 on both
    40186: 47376,
    # S: "Max difference supply, compressor"  F: "Max diff. comp."
    # s16, f10, C, 1.0..25.0 on both.
    40188: 47378,
    # S: "Max difference supply, additional heat"  F: "Max diff. add."
    # s16, f10, C, 1.0..24.0 on both.
    40189: 47379,
    # S: "Operating mode"                   F: "Operational mode"   u8 0..2
    # Not read by this app; it is in safety.GUARDED, and safety works on
    # physical addresses, so this row exists so that the *reason* quoted for a
    # refusal is the right one. Both maps enumerate 2 as additional heat only.
    40238: 47137,

    # -- hot water ---------------------------------------------------------
    # S: "Hot water mode"                   F: "Hot water comfort mode"
    # s8, 0..4, default 1, and the keys are identical: 0, 1, 2, 4.
    #
    # The *labels* differ -- Small/Medium/Large on an S735, Economy/Normal/
    # Luxury on an F750 -- and this is the one row in the table where that
    # matters, because the value comes back as a word. It is still the same
    # setting at the same keys in the same order, and the four stop
    # temperatures below corroborate it exactly: the S register the app calls
    # "low" is the F register called "Economy", "normal" is "Normal", and
    # "high" is "Luxury". The web app is told about both wordings; anything it
    # is not told about it shows as the pump wrote it, marked as a quotation.
    40057: 47041,
    # S: "Stop temperature HW periodic increase"  F: "Stop temperature Periodic HW"
    # s16, f10, C, 55.0..70.0 on both.
    40062: 47046,
    # S: "Stop temperature HW high temperature"   F: "Stop temperature HW Luxury"
    40063: 47047,
    # S: "Stop temperature HW normal temperature" F: "Stop temperature HW Normal"
    40064: 47048,
    # S: "Stop temperature HW low temperature"    F: "Stop temperature HW Economy"
    40065: 47049,
    # S: "Periodic hot water interval"      F: "Periodic HW Interval"
    # s8, unit "days", 1..90 on both.
    40067: 47051,
    # S: "Hot water charging offset"        F: "HW charge offset"   s8, f10, C
    # Absent from the ground-source F maps; drops out by itself there.
    40077: 47062,

    # -- ventilation -------------------------------------------------------
    # S: "Ventilation mode"                 F: "Fan Mode"           u8 0..4
    #
    # One difference worth knowing: the F map gives this register mappings
    # (0 Normal, 1..4 Fan mode 1..4) and the S map does not, so on an F pump
    # the value reads back as a word rather than a number. Writing is
    # unaffected -- every key 0..4 exists -- and `pump.ventilate()` still
    # chooses the mode by reading the percentages, which is the only safe way:
    # the modes are not ordered low to high on either generation.
    40105: 47260,
    # S: "Exhaust air fan speed 4".."normal"  F: "Exhaust Fan speed 4".."normal"
    # u8, %, 0..100 on both. Note the F map also has a *supply* fan set at
    # 47266-47270; these are the exhaust ones, which is what the S registers
    # are and what an exhaust-air pump's heat comes through.
    40106: 47261,
    40107: 47262,
    40108: 47263,
    40109: 47264,
    40110: 47265,
    # S: "Return time fan 4".."fan 1"       F: "Fan return time 4".."1"
    # u8, h. The S range is 1..24 and the F range 1..99; the app caps its own
    # requests at 24 regardless, so the wider F range is never reached.
    40116: 47271,
    40117: 47272,
    40118: 47273,
    40119: 47274,

    # -- room sensor, climate system 1 -------------------------------------
    # S: "Use room sensor climate system 1"  F: "Use room sensor S1"   u8 0/1
    #
    # The F map gives this one mappings (0 Off, 1 On) where the S map leaves it
    # a bare 0/1, so on an F pump it reads back as a word and the settings row
    # becomes a two-option list instead of a number field. Same for night
    # cooling at 47537 below. That is the register map talking, not this app:
    # the row still writes the same value to the same setting.
    40203: 47394,
    # S: "Room sensor set point value climate system 1"
    # F: "Room sensor setpoint S1"          s16, f10, C, 5.0..30.0 on both
    40207: 47398,
    # S: "Room sensor factor climate system 1"  F: "Room sensor factor S1"
    # u8, factor 10, 0.0..6.0 on both
    40211: 47402,

    # -- night cooling -----------------------------------------------------
    # S: "Night cooling 1"                  F: "Night cooling"      u8 0/1
    # The F750 map has four registers titled "Night cooling" -- 47537 and then
    # 49358-49360 -- but only 47537 sits with its own start temperature
    # (47538) and minimum difference (47539); 49358-49360 are the per-climate-
    # system copies. 47537 is the one that pairs with 40228/40229.
    40228: 47537,
    # S: "Start temperature night cooling 1"  F: "Start room temp. night cooling"
    # u8, C, 20..30 on both.
    40229: 47538,

    # -- degree minutes ----------------------------------------------------
    # S: "Degree minutes" (s32, factor 10, unit DM, -3000.0..3000.0)
    # F: "Degree Minutes (16 bit)" (s16, factor 10, unit DM, same range)
    #
    # The F generation publishes this twice: 43005 in 16 bits and 40940 in 32,
    # same unit, same factor, same range, both writable. 40940 is the exact
    # type twin of the S register and 43005 is the one every other F-series
    # tool reads; 43005 is chosen because it is the one an F owner will
    # recognise from their own documentation, and because the real range
    # (+/- 3000 DM) fits a 16-bit register with room to spare -- there is no
    # value this app can read or write that the 16-bit form cannot hold.
    # Absent from the F370 and F470 maps, where it drops out by itself.
    40012: 43005,

    # -- alarms ------------------------------------------------------------
    # S: "Alarm number"                     F: "Alarm"              s16, read-only
    # The F info text says "Indicates the alarm number of the most severe
    # current alarm", which is what the S register is.
    #
    # 45001 is *also* the address of "Activate forced control" on the S
    # generation, and safety.BLOCKED says so. That block is harmless here: on
    # every F map 45001 is read-only, so `pump.write` refuses it before safety
    # is consulted at all, and nothing in this app writes an alarm register.
    # It is left alone because widening a block costs nothing and narrowing
    # one costs the model that needed it.
    31976: 45001,
    # S: "Reset alarm"                      F: "Alarm Reset"        u8, writable
    # Never written by this app on either generation -- see alarms.py -- and
    # mapped only so that a register the app names is not a register the app
    # would reach by accident.
    40023: 45171,

    # -- the dashboard's measurements --------------------------------------
    # S: "Current outdoor temperature (BT1)"  F: "BT1 Outdoor Temperature"
    # s16, f10, C
    30002: 40004,
    # S: "Supply line (BT2)"                F: "BT2 Supply temp S1"   s16, f10, C
    30006: 40008,
    # S: "Return line (BT3)"      F: "EB100-EP14-BT3 Return temp"   s16, f10, C
    30008: 40012,
    # S: "Hot water top (BT7)"              F: "BT7 HW Top"           s16, f10, C
    30009: 40013,
    # S: "Hot water charging (BT6)"         F: "BT6 HW Load"          s16, f10, C
    30010: 40014,
    # S: "Exh. air (BT20)"                  F: "BT20 Exhaust air temp. 1"
    # s16, f10, C
    30020: 40025,
    # S: "Room average temp. clim. system 1 (BT50)"
    # F: "BT50 Room Temp S1 Average"        s16, f10, C
    # The F map also has 40033 "BT50 Room Temp S1", the instantaneous reading;
    # the S register says "average", so 40195 is the one that means the same.
    30117: 40195,
    # S: "Calculated supply climate system 1"  F: "Calc. Supply S1"   s16, f10, C
    # This is the number the advisor judges a curve change by.
    31018: 43009,
    # S: "Total run time additional heat"   F: "Tot. op.time add."   s32, f10, h
    31026: 43081,
    # S: "Power internal additional heat"   F: "Int. el.add. Power"  s16, f100, kW
    31028: 43084,
    # S: "Priority"                         F: "Prio"                u8, mapped
    # The F enumeration is a superset: both give 10 Off, 20 Hot Water, 30 Heat,
    # 40 Pool, 60 Cooling, and the F map adds 41 Pool 2 and 50 Transfer. The
    # advisor only asks whether the word contains "water", and the web app
    # shows a word it has no Swedish for as the pump's own, quoted.
    31029: 43086,
    # S: "Compressor frequency, current"    F: "Compressor Frequency, Actual"
    # u16, f10, Hz. Absent from the older F1145/F1245/F370/F470 maps.
    31047: 43136,
    # S: "Total run time compressor"        F: "Tot. op.time compr. EB100-EP14"
    # s32, f1, h
    31088: 43420,
    # S: "Total run time compressor hot water"
    # F: "Tot. HW op.time compr. EB100-EP14"  s32, f1, h
    31092: 43424,
    # S: "Compressor, number of starts (EB100-EP14)"
    # F: "Compressor starts EB100-EP14"
    # Both count compressor starts with factor 1 and no unit. The only
    # difference is signedness -- u32 on the S map, s32 on the F -- which
    # changes nothing for a counter that starts at zero and is capped at
    # 9 999 999 in the F map.
    31535: 43416,
}


#: Addresses this app names that have **no** F-series equivalent, and why.
#:
#: This set is a promise, not a to-do list: `tests/test_profile.py` walks every
#: address the app actually uses -- pump.DASHBOARD, settings.ADDRESSES, every
#: R_* constant in advisor.py and alarms.py, the fan and hot-water and
#: ventilation registers in pump.py, and every number that appears in
#: web/index.html -- and fails if one of them is neither in F_TABLE nor in
#: here. So a new S-series literal added anywhere in the app breaks the build
#: until somebody has decided what it means on an F pump.
#:
#: Three of these are worse than merely absent, and they are the reason
#: `available()` exists: 40020, 40079 and 40167 are all real registers on an
#: F750, holding something else entirely. Falling back to the identity for
#: those would not be "a register that exists on only one generation, working
#: by its own number" -- it would be this app reading an evaporator temperature
#: and calling it the holiday setting. On an F profile they are refused
#: outright, the same way a register missing from the map is.
NO_F_EQUIVALENT: dict[int, str] = {
    30000: "not a register at all. `setInterval(tick, 30000)` in web/index.html "
           "matches the five-digit address grep the test walks, and is listed "
           "here so that the next person to run that grep does not spend an "
           "afternoon looking for it in the F750 map",
    40079: "not in the S735 map either: it appears only in web/index.html's "
           "REG_WATER list, which classifies a write for the 'how long to wait' "
           "sentence and never reads or writes the register. On the F "
           "generation 40079 IS a register -- EB100-BE3 Current, u32, amperes, "
           "read-only -- so the identity fallback would point a hot-water "
           "classifier at a phase current",
    40020: "S: 'Holiday function status', s8, writable, where -1 means off. The "
           "F generation models a holiday as three registers instead -- 48043 "
           "activated (0/1), 48044 and 48045 the start and end dates as days "
           "since 1 January 2007 -- with no single register that carries the "
           "S-series -1. There is no one-to-one mapping to make. Note that "
           "40020 on an F750 is EB100-BT16 Evaporator temp, read-only",
    40167: "S: 'Heating start at under temp.', s8, factor 10, C, 0.5..10.0 -- "
           "how far below setpoint the heating restarts. The F generation "
           "regulates that start on degree minutes instead (47206 'DM start "
           "heating', a count, not a temperature), and no F map has a register "
           "with this meaning and this unit. Note that 40167 on an F750 is "
           "EP47-BT50 Room Temp S8, read-only",
    40226: "S: 'More hot water (Number of minutes)', u16, and its on/off switch "
           "40698. The F generation has one register for the whole feature, "
           "48132 'Temporary Lux', an enumeration of fixed durations: 0 Off, "
           "1 = 3 h, 2 = 6 h, 3 = 12 h, 4 = one-time increase. It is the same "
           "feature and it takes a different kind of value, so there is nothing "
           "to map an arbitrary number of minutes onto. Mapping the on/off "
           "switch alone would make every request for extra hot water three "
           "hours long whatever was asked for, which is the exact failure this "
           "module exists to avoid. 48132 is classified in safety.EVERYDAY so "
           "an F owner who knows what they want can POST it to /api/write "
           "without ceremony; the app's own button says plainly that it cannot",
    40698: "S: 'More hot water' on/off. The other half of 40226 -- see there",
    31079: "S: 'More hot water status', u8, read-only. The other half of a "
           "feature this app cannot drive on an F pump; no F map publishes a "
           "status register for 48132",
    31975: "S: 'Fan speed (EB100-EP14)', u8, no unit, no enumeration. The F750 "
           "map has four registers titled 'Fan speed current' -- 41256, 41257, "
           "41258 and 43108 -- and all four are the fan *mode* (0 Normal, 1..4 "
           "Fan mode 1..4), not the S register's raw number. Four candidates "
           "and none of them the same shape is not a mapping, it is a guess",
    32134: "S: 'Exhaust air fan speed (GQ2)', u8, %, read-only -- what the "
           "exhaust fan is actually doing right now, and the dashboard's fan "
           "tile. No F map publishes an exhaust-fan percentage read-back at "
           "all; the closest thing is 42093, the GQ3 fan inside the SAM supply-"
           "air accessory, which is a different fan on a different unit",
    32196: "S: 'Class 1 alarm', u8, 0/1 -- the flag alarms.py uses to catch an "
           "alarm the pump raises without putting a number in 31976. No F map "
           "has it. The watcher degrades cleanly: with no reading for this "
           "address the flag is simply never consulted, and an F-series alarm "
           "is caught by its number alone",
}


class Profile:
    """Which generation a pump is, and how to say its register numbers.

    `canonical` addresses are the S-series numbers the whole app speaks.
    `physical` addresses are what actually goes to the register map and, from
    there, onto the wire. On an S-series profile the two are the same number
    and every method here is the identity -- that is deliberate and is what
    keeps the author's own pump behaving exactly as it did.
    """

    def __init__(self, generation: str, model: str = "",
                 canonical_to_physical: dict[int, int] | None = None,
                 no_equivalent=frozenset(), word_swap: bool | None = None):
        if generation not in ("S", "F"):
            raise ValueError("generation must be 'S' or 'F', not %r" % (generation,))
        self.generation = generation
        self.model = model or ""
        # None means "nobody has said"; True or False is the owner having read
        # menu 5.3.11 and put it in config.yaml. Kept as given so `as_dict` can
        # report which of the three answers below decided the word order.
        self.word_swap = word_swap
        # What the pump's own 48852 answered, when it was asked and answered.
        # See set_word_order_from_register and LOW_WORD_FIRST.
        self.word_swap_read: bool | None = None
        self.canonical_to_physical: dict[int, int] = dict(canonical_to_physical or {})
        # Built here rather than kept by hand: two dicts that have to agree is
        # two dicts that will not.
        self.physical_to_canonical: dict[int, int] = {
            p: c for c, p in self.canonical_to_physical.items()
        }
        if len(self.physical_to_canonical) != len(self.canonical_to_physical):
            # Two canonical addresses pointing at one physical one would make
            # the inverse silently lossy, and the inverse is what names a
            # register in a backup.
            raise ValueError("the register table maps two canonical addresses "
                             "onto one physical address")
        self.no_equivalent = frozenset(no_equivalent)

    #: True when this mapping has been run against a real pump of this
    #: generation. The S table is the identity on the author's own S735, so it
    #: is true by construction. The F table is read out of the `nibe` package's
    #: maps and has never met an F-series pump; /api/status reports this and
    #: the web app says so on the page.
    @property
    def verified(self) -> bool:
        return self.generation == "S"

    # -- what the transport allows -----------------------------------------
    #
    # Four properties, all of them constants of the generation rather than of
    # this app's taste. They live here because the alternative is what was
    # here before: module constants in modbus.py and pump.py that are right for
    # one generation and quietly wrong for the other.

    @property
    def max_regs_per_query(self) -> int:
        """Registers one read request may ask for. See MAX_REGS_PER_QUERY."""
        return MAX_REGS_PER_QUERY[self.generation]

    @property
    def read_gap(self) -> int:
        """Registers worth reading through to join two runs. See READ_GAP."""
        return READ_GAP[self.generation]

    @property
    def request_timeout(self) -> float:
        """The smallest per-request timeout this transport needs, in seconds.

        A floor, not a value: `Pump` raises the configured timeout to it and
        never lowers it, so an S-series install keeps its own.
        """
        return MIN_REQUEST_TIMEOUT[self.generation]

    @property
    def low_word_first(self) -> bool:
        """True when the low 16-bit word of a 32-bit value comes first.

        Three answers, in order of authority: `word_swap` from config.yaml,
        because a person who has read menu 5.3.11 outranks everything; then
        what register 48852 said when it was asked; then the generation's
        default. See LOW_WORD_FIRST and `word_order_source`.
        """
        if self.word_swap is not None:
            return bool(self.word_swap)
        if self.word_swap_read is not None:
            return bool(self.word_swap_read)
        return LOW_WORD_FIRST[self.generation]

    @property
    def word_order_source(self) -> str:
        """Which of the three decided `low_word_first`, in one word.

        "configured", "read" or "assumed". It goes into the backup header
        because a snapshot taken with the wrong word order is not obviously
        wrong: every 16-bit register in it is perfect and every 32-bit one is a
        large, stable-looking number. On the S series "assumed" means NIBE's own
        TIF, which is documented and is not in doubt; on the F it means this
        app's reading of a disagreement between NIBE's manual and NIBE's
        register map, which very much is. See LOW_WORD_FIRST.
        """
        if self.word_swap is not None:
            return "configured"
        if self.word_swap_read is not None:
            return "read"
        return "assumed"

    def set_word_order_from_register(self, value) -> bool | None:
        """Record what register 48852 answered. Returns the order it means.

        1 means the words are swapped -- the low word first. 0 means they are
        not. Anything else (None from an unreadable register, a word the map
        put a mapping on, a value neither 0 nor 1) is not an answer and is
        ignored, leaving the default in place: a word order guessed from a
        value nobody recognises would be worse than the documented default,
        which at least has a paper trail.

        Ignored outright when `word_swap` is configured. The owner has looked at
        the menu; this is the same question asked of the pump, and a person who
        went to the display beats a register read for the same reason a
        measurement beats a default.
        """
        if self.word_swap is not None:
            return None
        if isinstance(value, bool):
            self.word_swap_read = value
        elif isinstance(value, (int, float)) and int(value) in (0, 1):
            self.word_swap_read = bool(int(value))
        else:
            return None
        return self.word_swap_read

    @property
    def own_curve_outdoor(self) -> list:
        """Outdoor temperature per own-curve point, P1 first; None = unknown.

        One list, read by advisor.curve_at, by the settings labels and by the
        chart in web/index.html through /api/heating -- so that the seventh
        point's temperature is asserted in exactly one place, and on the F
        series is not asserted at all. See OWN_CURVE_OUTDOOR.
        """
        return list(OWN_CURVE_OUTDOOR[self.generation])

    def physical(self, address: int) -> int:
        """The address to put in front of the register map.

        An address with no entry in the table comes back unchanged, so a
        register that exists on only one generation still works by its own
        number -- an F owner can read 48132 as 48132.
        """
        return self.canonical_to_physical.get(address, address)

    def canonical(self, address: int) -> int:
        """The inverse: what this app would call the register at `address`."""
        return self.physical_to_canonical.get(address, address)

    def available(self, address: int) -> bool:
        """False when this canonical address means nothing on this pump.

        Only ever False for the addresses in NO_F_EQUIVALENT, on an F profile.
        It exists because for three of them the identity fallback is not
        harmless: 40020, 40079 and 40167 are real F-series registers holding
        something else. Callers treat a False here exactly as they treat a
        register missing from the map, which is a path the app has had since
        the beginning.
        """
        return address not in self.no_equivalent

    def why_unavailable(self, address: int) -> str:
        """The sentence explaining a False from `available`. English: it is a
        note about the register map, and it goes in a log line and in the
        detail of an error, not into a toast.

        Empty for every address `available` says yes to, including on an
        S-series profile -- where `no_equivalent` is empty and the whole set
        means nothing. Reading NO_F_EQUIVALENT directly answered an S-series
        owner asking about a register their map happens not to have with a
        paragraph about what it is on an F750, which is a confident answer to a
        question nobody asked.
        """
        if address not in self.no_equivalent:
            return ""
        return NO_F_EQUIVALENT.get(address, "")

    def as_dict(self) -> dict:
        return {"generation": self.generation, "model": self.model,
                "verified": self.verified,
                "translated": len(self.canonical_to_physical),
                # Which word order 32-bit values were read with, and where that
                # came from: config.yaml, the pump's own register 48852, or
                # this app's default. A backup taken with the wrong one is full
                # of plausible nonsense in exactly the 32-bit registers, and
                # this is what lets a reader tell.
                "low_word_first": self.low_word_first,
                "word_swap_configured": self.word_swap is not None,
                "word_order_source": self.word_order_source}

    def __repr__(self) -> str:                            # pragma: no cover
        return "Profile(%s, %r, %d translated)" % (
            self.generation, self.model, len(self.canonical_to_physical))

    # -- construction ----------------------------------------------------

    @classmethod
    def for_generation(cls, generation: str, model: str = "",
                       word_swap: bool | None = None) -> "Profile":
        if generation == "F":
            return cls("F", model, F_TABLE, frozenset(NO_F_EQUIVALENT),
                       word_swap=word_swap)
        return cls("S", model, word_swap=word_swap)

    @classmethod
    def for_model(cls, model: str, generation: str = "",
                  word_swap=None) -> "Profile":
        """The profile for a configured model, or an explicit generation.

        `generation` wins when it is given: a register map loaded from a CSV
        exported from the pump carries no model name at all, so there is
        nothing to derive from and the config file has to say.

        `word_swap` is config.yaml's, in any of the forms a config file writes
        a boolean in; "" and None both mean "nobody has said", which leaves the
        generation's default in place until `Pump.resolve_word_order` asks the
        pump.
        """
        word_swap = parse_word_swap(word_swap)
        chosen = (generation or "").strip().upper()
        if chosen in ("S", "F"):
            return cls.for_generation(chosen, model, word_swap)
        if chosen:
            raise ValueError(
                "generation must be S, F or empty (got %r). Empty means "
                "'work it out from the model name'." % (generation,))

        name = (model or "").strip().upper().replace(" ", "").replace("-", "")
        if name in F_MODELS:
            return cls.for_generation("F", name, word_swap)
        if name in S_MODELS:
            return cls.for_generation("S", name, word_swap)
        raise ValueError(
            "Cannot tell which generation %r is, so the register numbers this "
            "app uses cannot be translated for it.\n\n"
            "Set `generation` in config.yaml to S or F. S is the 2021 platform "
            "-- S735, S1155, S1255, S320, SMO S40, VVM S320 -- whose heating "
            "curve is register %d. F is everything before it -- F750, F1155, "
            "SMO 20/40, VVM 225/320/500 -- whose heating curve is register %d. "
            "If you are not sure, read one of those two registers: only one of "
            "them answers."
            % (model, S_MARKER, F_MARKER))
