# F-series support has shipped unverified — F750 owners, does any of this work?

> **På svenska:** Den här utgåvan innehåller stöd för F-serien (F750, F1155,
> F1255 med flera) som **ingen har provat mot en riktig värmepump**. Ingen av
> oss äger en F-serie. Allt är läst ur NIBE:s egna manualer och registerkartor,
> inget är mätt. Om du har en F750 och en MODBUS 40 vore det till stor hjälp om
> du ville testa — eller bara gå till pumpens display och svara på frågorna
> längst ned, det kräver varken app eller tillbehör. **Svara gärna på svenska**,
> det går alldeles utmärkt. Tråden är på engelska bara för att resten av repot
> är det.

## What shipped

Support for the F generation — F370, F470, F730, F750, F1145, F1155, F1245,
F1255, F1345, F1355 — as a translation layer over the existing app. Nothing
above the Modbus layer changed. Concretely:

- A `generation` setting (`""` derives it from `model`, or `S` / `F`), and a
  register translation table so the app's existing concepts find their
  F-generation addresses.
- A `framing` setting (`tcp` / `rtu`), because an F pump is reached through an
  RS485-to-Ethernet gateway and those come in two incompatible kinds.
- `nibelokal/data/alarms_f.json`: NIBE's 461 F-series alarm texts in Swedish,
  fetched verbatim from NIBE's own alarm-code search.
- [`docs/f-series.md`](f-series.md), which is where the reasoning and
  every source lives.

## What is unverified

**All of it.** No F-series pump has ever been read by this app. Nobody involved
owns one. The whole thing is manuals, NIBE's register database as republished by
[yozik04/nibe](https://github.com/yozik04/nibe), and other people's forum posts.

The named gaps, in rough order of how much they would bite:

1. **End to end.** Whether `python3 -m nibelokal status` returns anything at all
   through a MODBUS 40 and a gateway.
2. **Timing.** NIBE documents a 2.1 s *maximum timeout* and a limit of one
   register per request for anything not in a LOG.SET file, and users on one
   forum report the 2.1 s as real spacing rather than a timeout. The app takes
   both at face value on an F pump: one register per request, no batching, and
   a per-request timeout of at least 2.5 s. If the reports are right, a poll of
   the twenty dashboard registers an F750 answers is around 40 seconds of bus
   time and a full `backup` of the map is 15–20 minutes, so `poll_seconds: 60`
   is the floor rather than a default. Nobody has measured any of it.
3. **Own-curve point P7.** The register map has seven points; NIBE's F750 and
   F1155 manuals both show menu 1.9.7 with six rows, −30 to +20 °C. Either the
   menu scrolls or the seventh point is spare.
4. **Extra hot water.** On an F pump this is register 48132 with fixed options
   (off / 3 h / 6 h / 12 h), not a number of minutes as on the S series. The map
   also lists a value 4, "one time increase", which the F750 manual's menu 2.1
   does not offer.
5. **Degree minutes.** 43005 (16-bit) and 40940 (32-bit) both exist and are both
   marked writable. Which one the regulation follows is not established, and
   this app does not write either.
6. **Alarm severity.** The three-way severity in `alarms_f.json` is derived by
   this project from NIBE's Swedish wording, not published by NIBE. Ten codes
   have since been reclassified by hand and are the only ones in that file a
   person has read: 175, 183, 233, 234, 235, 270 and 998 — all of them the pump
   saying an operation is in progress, a defrost or a start-up — moved from
   `warning` to `info`, so that an exhaust-air F750 does not push a
   notification for every defrost all winter; and 220 Högtryckslarm, 221
   Lågtryckslarm and 222 Motorskyddslarm moved from `warning` to `alarm`,
   because NIBE's generic long text left the rule nothing to read and those
   three stop the compressor. The rule and the list are in
   `_meta.hand_reclassified`. For the other 451, expect roughly one code in
   forty to be classified differently from how you would classify it, and 45 of
   them carry only NIBE's generic "this alarm came from the heat pump" text.
7. **Word swap, and which way round it ships.** MODBUS 40 v.11 and later has a
   word-swap setting in menu 5.3.11 that decides the order of the two registers
   in a 32-bit value. Get it wrong and every 32-bit reading is nonsense while
   every 16-bit one is fine. **NIBE's own two documents disagree about the
   factory value**: the MODBUS 40 installer manual says "Big Endian" (high word
   first) and NIBE's register database gives 48852 "Modbus40 Word Swap" a
   default of 1, "swapping the words" (low word first). The `nibe` package and
   Home Assistant both follow the register; so does this app, which reads 48852
   at startup and uses what the pump answers, falling back to the low word first
   and saying so in the backup header. Nobody has stood next to a real F750 and
   compared menu 5.3.11 with what 48852 says. `word_swap: true` / `false` in
   `config.yaml` overrules both.

## If you have an F-series pump

### What you need

- The pump, with software version **above 3000** (menu 3.1 "serviceinfo").
- A **MODBUS 40** accessory, NIBE part **067 144**, fitted and activated in menu
  **5.2** (**5.2.4** on an F1345, F1355, SMO 40, VVM 225/310/320/325/500). Its
  own software version, also in menu 3.1, should be **7 or higher**.
- An **RS485-to-Ethernet gateway** — a Waveshare RS485 TO ETH, a USR/PUSR
  USR-TCP232-410s, an Elfin EW11, or anything equivalent. Wire it A=+, B=−,
  GND=ground to AA9-X2 on the MODBUS 40 board. Serial settings are fixed and
  not negotiable: **9600 baud, 8 data bits, no parity, 1 stop bit**.
- The machine you already run this app on.

### Config

```yaml
host: "192.168.1.50"     # the gateway's address, not the pump's
port: 502                # whatever port your gateway listens on
unit: 1                  # MODBUS 40 is slave 1 unless you changed it in menu 5.3.11
model: "F750"
generation: "F"          # or leave empty and let it derive from model (or from register_csv)
framing: "rtu"           # "rtu" for a transparent gateway, "tcp" for one converting to Modbus TCP
poll_seconds: 300        # start slow; see the timing note above
word_swap: ""            # empty = read register 48852 and believe the pump
```

Two things the app does differently on an F pump, so that they are not
surprises: the **daily automatic backup is off** (a full snapshot is 20–35
minutes of held bus, taken inside the poll loop — set `auto_backup_hours`
yourself if you want one overnight), and a **wrong `unit` looks exactly like a
gateway that is not there**, because a MODBUS 40 that is not being addressed
answers nothing rather than "wrong slave".

If you get a connection that opens and then times out with nothing in it, the
first thing to try is **the other value of `framing`**. Those two failures look
identical from outside.

### What to run, and what to paste back

**1. The register export for your own pump.** This is the single most useful
thing, and it does not need the app working. It does not come from the pump: an
F-series pump has no register export in its USB menu (that is an S-series
thing). It comes from NIBE's Windows tool — **ModbusManager → File → Export to
file**, with your model selected. Attach the file here — or just say which model
and firmware it came from if you would rather not. Then:

```yaml
register_csv: "/path/to/that/export.csv"
```

That is a complete configuration on its own: the file has no model name in it,
and the app works the generation out from the map (40027 is the heating curve on
an S, 47007 on an F). Add `generation: "F"` only if it tells you it cannot
decide.

**2. Status.**

```bash
python3 -m nibelokal status
```

Paste the whole output, including any error. If it fails, `--verbose` output or
the traceback is more useful than a summary.

Its **last** line is the most useful one of all: `status` asks the pump what it
is with Modbus function 0x2B, and prints what came back — something like
`device id: NIBE F750 5539`. Whether your pump answers that at all, and what it
says, settles which map and which firmware everything else here is about. A pump
that does not answer it prints `not answered` and everything above that line is
unaffected. (It is asked last, and with a one-second timeout, precisely because
0x2B is optional in the Modbus specification: a pump that implements "no" by
staying silent must not cost you a timeout before the registers you actually ran
the command for.)

**3. If the numbers look wrong.** Say which ones. Specifically: are the *16-bit*
values plausible (outdoor temperature, supply temperature, hot water) while the
*32-bit* ones are absurd (degree minutes, compressor hours, energy)? That is the
word-swap setting in menu 5.3.11, not a bad register map, and knowing which way
round yours is set is itself a useful data point.

**4. Timing, if you have the patience.** Roughly how long does one read take?
Even "about two seconds each, so `status` took most of a minute" settles the
`poll_seconds` question.

Please do not test writes. Nothing above needs them, and this app has never
written to an F pump.

## Questions you can answer from the pump's display alone

No app, no gateway, no MODBUS 40 — just walk to the pump. Any one of these is a
useful reply.

- [ ] **Menu 1.9.7 "egen kurva":** how many rows are there? Does the list scroll
      past *framledningstemp. vid 20 °C* to a seventh row at **30 °C**? (The
      manuals show six. The register map has seven.)
- [ ] **Menu 1.9.1 "värmekurva":** does setting it to 0 say anywhere on screen
      that own curve is in use?
- [ ] **Menu 2.1 "tillfällig lyx":** what are the options, exactly? Is there
      anything besides *från / 3 / 6 / 12 timmar* — an *engångshöjning* or
      similar?
- [ ] **Menu 2.2 "komfortläge":** are the three options *ekonomi*, *normal*,
      *lyx*? Anything else, e.g. a smart-control option?
- [ ] **Menu 5.1.5 "fläkthast. frånluft":** what are the five percentages —
      normal, and speeds 1 through 4? (The map's defaults are 65, 0, 30, 80,
      100, which would mean speeds 1 and 2 *reduce* ventilation. If yours differ,
      that is exactly why this app reads them rather than assuming an order.)
- [ ] **Menu 1.9.6 "fläktåtergångstid":** what is the highest value it will
      accept — 24 hours, or 99?
- [ ] **Menu 4.9.3 "gradminutinställning":** what does "aktuellt värde" show,
      and what range does it let you set?
- [ ] **Menu 3.1 "serviceinfo":** the pump's software version, and the MODBUS 40
      version if one is fitted.
- [ ] **Menu 5.3.11:** does it exist on your pump? What does it show — a slave
      address, a word-swap setting, both? **What is the word swap set to, in the
      menu's own words?** This is the question with the most riding on it: NIBE's
      MODBUS 40 manual and NIBE's own register database say opposite things
      about the factory value, and the app has picked a side.
- [ ] **Register 48852 "Modbus40 Word Swap":** what does it read? If you have
      the app running, `python3 -m nibelokal read 48852`; otherwise
      ModbusManager will show it. 1 means the words are swapped (low word
      first), 0 means they are not. Whether it agrees with what menu 5.3.11
      shows is itself worth knowing — the app reads this register at startup and
      believes it.

## Not a promise

This is a hobby project for one S735 that has grown a second generation it has
never met. If it does not work for you, that is the expected outcome and saying
so here is the contribution. If you already run Home Assistant, its
[Nibe integration](https://www.home-assistant.io/integrations/nibe_heatpump/)
has far more F-series mileage than this does, and the README has said to use it
instead since before any of this existed.
