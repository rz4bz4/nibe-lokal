"""Klickar sig genom UI:t i en riktig webblasare. Andrar INGENTING pa pumpen."""
import sys
from playwright.sync_api import sync_playwright

URL = "http://localhost:8377/"
fel = []

with sync_playwright() as pw:
    b = pw.chromium.launch()
    page = b.new_page()
    errs = []
    page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errs.append("pageerror: %s" % e))
    page.goto(URL, wait_until="networkidle")
    page.wait_for_timeout(2500)

    # 1. kakel
    n = page.locator(".tile").count()
    print("kakel: %d" % n)
    if n < 5:
        fel.append("for fa kakel")

    # 2. varmekortet
    off = page.locator("#offsetNow").inner_text()
    print("offset visas: %r" % off)
    if off == "-" or off == "":
        fel.append("offset lastes inte")

    # 3. ventilationslagen
    opts = page.locator("#ventMode option").all_inner_texts()
    print("ventilationslagen: %s" % opts)
    if not any("%" in o for o in opts):
        fel.append("ventilationslagen saknar procent")

    # 4. alla installningar
    page.locator("#settingsBox summary").click()
    page.wait_for_timeout(3000)
    rows = page.locator(".setrow").count()
    groups = page.locator(".setgroup h3").all_inner_texts()
    print("installningar: %d rader i %s" % (rows, groups))
    if rows < 20:
        fel.append("for fa installningsrader (%d)" % rows)

    # 5. oppna en redigerare (utan att spara)
    page.locator('[data-edit="40185"]').click()
    page.wait_for_timeout(400)
    vis = page.locator('[data-panel="40185"]').is_visible()
    val = page.locator("#sv-40185").input_value() if vis else None
    print("redigerare for varmestopp oppnas: %s, varde %r" % (vis, val))
    if not vis or not val:
        fel.append("redigeraren oppnades inte")

    # 6. radgivaren
    page.locator("#adviceOpen").click()
    page.wait_for_timeout(300)
    page.locator('[data-when="cold_outside"]').click()
    page.locator("#adviceGo").click()
    page.wait_for_timeout(4000)
    sugg = page.locator(".sugg .h").all_inner_texts()
    print("radgivaren: %s" % sugg)
    if not sugg:
        fel.append("radgivaren gav inget forslag")

    # 7. historikflikar
    tabs = page.locator("#histTabs button").all_inner_texts()
    print("historikflikar: %s" % tabs)

    # 8. service worker
    sw = page.evaluate("() => 'serviceWorker' in navigator")
    print("service worker stods: %s" % sw)

    b.close()

real = [e for e in errs if "favicon" not in e.lower()]
if real:
    print("\nKONSOLFEL:")
    for e in real[:10]:
        print("  " + e[:160])
    fel.append("%d konsolfel" % len(real))

print("\n" + ("ALLT OK" if not fel else "FEL: " + "; ".join(fel)))
sys.exit(1 if fel else 0)
