"""Klickar sig genom hela UI:t i en riktig webblasare.

Laser och oppnar bara - ingenting skrivs till varmepumpen. Korr mot en igang
varande `serve`:

    pip install playwright && playwright install chromium
    python3 -m nibelokal serve &
    python3 tests/ui_smoke.py
"""
import os
import sys

from playwright.sync_api import sync_playwright

URL = os.environ.get("NIBE_UI", "http://localhost:8377/")
VIEWPORTS = [("telefon", 390, 844), ("dator", 1280, 900)]
fel = []


def check(ok, msg):
    print(("  ok    " if ok else "  FEL   ") + msg)
    if not ok:
        fel.append(msg)


def run(page, label, width):
    print("\n=== %s (%dpx) ===" % (label, width))
    page.goto(URL, wait_until="networkidle")
    page.wait_for_timeout(2500)

    # -- nav och vyer -------------------------------------------------
    # Elpriset har en egen flik nar det ar konfigurerat och ar borta nar det
    # inte ar det, sa antalet synliga flikar ar 4 eller 5. Ventilationen delar
    # flik med varmvattnet. Fliknamnet och vyns rubrik ar samma ord.
    tabs = [b for b in page.locator("nav button").all() if b.is_visible()]
    check(len(tabs) in (4, 5), "fyra eller fem flikar i navigeringen (%d)" % len(tabs))
    for name, heading in [("now", "Just nu"), ("heat", "Värme"), ("price", "Elpris"),
                          ("water", "Vatten & luft"), ("hist", "Historik")]:
        tab = page.locator('nav button[data-view="%s"]' % name)
        if not tab.is_visible():
            check(name == "price", "fliken %s ar dold" % name)
            continue
        tab.click()
        page.wait_for_timeout(900)
        vis = page.locator("#v-" + name).is_visible()
        title = page.locator("#viewTitle").inner_text()
        check(vis and title == heading, "fliken %s visar %r" % (name, title))

    # bara en vy at gangen
    synliga = [v for v in ["now", "heat", "price", "water", "hist", "set"]
               if page.locator("#v-" + v).is_visible()]
    check(len(synliga) == 1, "exakt en vy synlig (%s)" % synliga)

    # -- just nu ------------------------------------------------------
    page.locator('nav button[data-view="now"]').click()
    page.wait_for_timeout(700)
    hero = page.locator("#heroVal").inner_text()
    check(hero not in ("", "-", "–"), "husets huvudsiffra i hero: %r" % hero)
    n = page.locator("#tiles .tile").count()
    check(n >= 6, "minst sex kakel (%d)" % n)
    rows = page.locator("#all tr").count()
    check(rows >= 15, "alla varden listade (%d rader)" % rows)

    # -- varme --------------------------------------------------------
    page.locator('nav button[data-view="heat"]').click()
    page.wait_for_timeout(1800)
    off = page.locator("#offsetNow").inner_text()
    check(off not in ("", "–"), "offset last: %r" % off)
    note = page.locator("#offsetNote").inner_text()
    check("varmare" in note and "svalare" in note,
          "offsettexten forklarar riktningen")
    line = page.locator("#heatLine").inner_text()
    check(len(line) > 15, "varmelaget sammanfattat: %r" % line[:70])
    flags = page.locator("#heatFlags").inner_text()
    check(len(flags) > 20, "varningar visas (%d tecken)" % len(flags))
    # the long background notes must be folded away, not stacked above the button.
    # Count only what is actually on screen -- what sits inside <details> is in
    # the DOM but not in the way.
    open_warns = page.locator("#heatFlags > .warn").count()
    folded = page.locator("#heatFlags details .warn").count()
    check(open_warns <= 2, "hogst tva varningar utfallda (%d utfallda, %d bortvikta)"
          % (open_warns, folded))
    if folded:
        check(not page.locator("#heatFlags details").first.get_attribute("open"),
              "resten ar hopfalld fran start")
    # Egen kurva ligger bakom "Avancerat", hopfallt fran start.
    if page.locator("#advCard").is_visible():
        check(not page.locator("#advBox").evaluate("e => e.open"),
              "avancerat ar hopfallt fran start")
        page.locator("#advSum").click()
        page.wait_for_timeout(600)
        pts = page.locator("#advBody .ptrow[data-pt]").count()
        check(pts >= 5, "egen kurva ritad med %d punkter" % pts)
        check(page.locator("#curveChart").count() == 1, "och som en bild")
        page.locator("#advSum").click()
        page.wait_for_timeout(300)

    page.locator('#whenTabs [data-when="cold_outside"]').click()
    page.locator("#adviceGo").click()
    page.wait_for_timeout(4500)
    sugg = page.locator("#adviceOut [data-group]").count()
    blocked = page.locator("#adviceOut .warn").count()
    check(sugg > 0 or blocked > 0, "radgivaren svarar (forslag %d, varning %d)" % (sugg, blocked))

    # -- vatten och luft ----------------------------------------------
    page.locator('nav button[data-view="water"]').click()
    page.wait_for_timeout(2500)
    opts = page.locator("#hwMin option").count()
    check(opts >= 4, "varmvattentider i listan (%d)" % opts)
    vrows = page.locator("#setWater .setrow").count()
    check(vrows >= 3, "varmvatteninstallningar pa vattenfliken (%d)" % vrows)
    modes = page.locator("#ventMode option").all_inner_texts()
    check(any("%" in m for m in modes), "ventilationslagen visar procent: %s" % modes)

    # -- historik -----------------------------------------------------
    page.locator('nav button[data-view="hist"]').click()
    page.wait_for_timeout(2500)
    chips = page.locator("#histTabs button").count()
    check(chips >= 3, "historikflikar (%d)" % chips)
    page.locator('[data-h="6"]').click()
    page.wait_for_timeout(2000)
    has_path = page.locator("#chart path.ln").count() > 0
    has_text = "Ingen historik" in (page.locator("#chart").text_content() or "")
    check(has_path or has_text, "grafen ritar kurva eller tomt lage")

    # -- installningar ------------------------------------------------
    page.locator("#goSettings").click()
    page.wait_for_timeout(3500)
    srows = page.locator("#settings .setrow").count()
    groups = page.locator("#settings .setgroup h3").all_inner_texts()
    # Varmekurvans punkter och granser bor under Varme -> Avancerat och raknas
    # inte har langre.
    check(srows >= 12, "installningsrader (%d i %s)" % (srows, groups))
    check(page.locator('#settings [data-edit="40044"]').count() == 0,
          "kurvpunkterna ligger inte kvar under Installningar")

    page.locator('#settings [data-edit="40167"]').click()
    page.wait_for_timeout(500)
    val = page.locator("#sv-40167").input_value()
    why = page.locator('[data-panel="40167"] .setwhy').inner_text()
    check(val != "" and len(why) > 20, "redigeraren oppnas med varde %r och forklaring" % val)
    page.locator('#settings [data-edit="40167"]').click()   # stang
    page.wait_for_timeout(300)
    check(not page.locator("#sv-40167").is_visible(), "redigeraren gar att stanga")

    bstate = page.locator("#backupState").inner_text()
    check("backup" in bstate.lower(), "backupstatus visas: %r" % bstate[:60])

    log_rows = page.locator("#wlog tr").count()
    check(log_rows >= 1, "andringsloggen visas (%d rader)" % log_rows)

    # -- inget vagrat vid horisontell scroll --------------------------
    over = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
    check(over <= 1, "ingen horisontell scroll (%dpx over)" % over)


with sync_playwright() as pw:
    b = pw.chromium.launch()
    for label, w, h in VIEWPORTS:
        page = b.new_page(viewport={"width": w, "height": h})
        errs = []
        page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errs.append("pageerror: %s" % e))
        run(page, label, w)
        real = [e for e in errs if "favicon" not in e.lower()]
        check(not real, "inga konsolfel" + (": " + "; ".join(real[:3]) if real else ""))
        page.close()

    # morkt lage renderar
    page = b.new_page(color_scheme="dark", viewport={"width": 390, "height": 844})
    page.goto(URL, wait_until="networkidle")
    page.wait_for_timeout(1800)
    bg = page.evaluate("() => getComputedStyle(document.body).backgroundColor")
    print("\n=== morkt lage ===")
    check(bg not in ("rgba(0, 0, 0, 0)", "rgb(255, 255, 255)"), "mork bakgrund: %s" % bg)
    page.close()
    b.close()

print("\n" + ("ALLT OK" if not fel else "%d FEL:\n  - %s" % (len(fel), "\n  - ".join(fel))))
sys.exit(1 if fel else 0)
