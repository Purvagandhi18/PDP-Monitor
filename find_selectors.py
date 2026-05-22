"""
Quick selector discovery for ManMatters PDPs.
Run: python find_selectors.py
Prints what it finds so you can update config.yaml
"""
from playwright.sync_api import sync_playwright

URL = "https://manmatters.com/dp/shilajit-gummies/2024397"

CANDIDATES = {
    "headline":        ["h1", ".product-title", ".pdp-title", "[class*='title']"],
    "cta_button":      ["button[class*='cart']", "button[class*='add']", ".add-to-cart",
                        "[class*='atc']", "button[class*='buy']"],
    "review_item":     ["[class*='review']", "[class*='Review']", "[data-testid*='review']"],
    "review_text":     ["[class*='review-body']", "[class*='review-text']", "[class*='reviewBody']"],
    "review_rating":   ["[class*='rating']", "[class*='star']", "[class*='Rating']"],
    "review_date":     ["[class*='review-date']", "[class*='reviewDate']", "time"],
    "carousel":        ["[class*='swiper']", "[class*='slick']", "[class*='carousel']",
                        "[class*='slider']", "[class*='Slider']"],
    "carousel_slide":  ["[class*='swiper-slide']", "[class*='slick-slide']",
                        "[class*='carousel-item']", "[class*='slide']"],
    "banner":          ["[class*='banner']", "[class*='Banner']", "[class*='hero']",
                        "[class*='Hero']", "[class*='promo']"],
}

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    print(f"\nLoading {URL}...")
    page.goto(URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(3000)

    # Scroll to trigger lazy loads
    page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
    page.wait_for_timeout(1500)
    page.evaluate("window.scrollTo(0, 0)")

    print("\n" + "="*60)
    print("SELECTOR DISCOVERY RESULTS")
    print("="*60)

    for element_type, selectors in CANDIDATES.items():
        print(f"\n── {element_type.upper()} ──")
        found_any = False
        for sel in selectors:
            try:
                els = page.query_selector_all(sel)
                if els:
                    sample = els[0].inner_text()[:80].replace("\n", " ").strip()
                    print(f"  ✓ FOUND [{len(els)}x] {sel}")
                    print(f"    Sample: \"{sample}\"")
                    found_any = True
            except Exception:
                pass
        if not found_any:
            print(f"  ✗ Nothing found — try inspecting manually")

    # Also dump all class names that contain key words
    print("\n" + "="*60)
    print("ALL CLASSES CONTAINING: review, carousel, swiper, banner, slider")
    print("="*60)
    classes = page.evaluate("""() => {
        const all = document.querySelectorAll('*');
        const found = new Set();
        all.forEach(el => {
            el.classList.forEach(c => {
                if (/review|carousel|swiper|banner|slider|slick|hero|rating|star/i.test(c)) {
                    found.add(c);
                }
            });
        });
        return Array.from(found).sort();
    }""")
    for c in classes:
        print(f"  .{c}")

    browser.close()
    print("\nDone. Share this output and I'll update config.yaml.")
