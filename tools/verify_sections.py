import warnings; warnings.filterwarnings("ignore")
from scraper.text_scraper import _extract_next_data_sections
from analyser.visual_scorer import _get_zeus_section_order
from playwright.sync_api import sync_playwright
import yaml

cfg = yaml.safe_load(open("config.yaml"))
urls = [(p["name"].split()[0]+"/"+u["url"].rstrip("/").split("/")[-1], u["url"]) for p in cfg["products"] for u in p["urls"]]

def live_sections(url):
    with sync_playwright() as pw:
        b=pw.chromium.launch(headless=True)
        pg=b.new_context(viewport={'width':1440,'height':900}).new_page()
        try:
            pg.goto(url, wait_until="domcontentloaded", timeout=35000)
            pg.wait_for_timeout(3000)
            return _extract_next_data_sections(pg)
        except Exception as e:
            print("  err", e); return []
        finally:
            b.close()

for name,url in urls:
    live = live_sections(url)
    zeus = _get_zeus_section_order(url)
    # which source would section-flow use?
    if len(live) >= 5: src = f"LIVE ({len(live)})"
    elif len(zeus) >= 5: src = f"Zeus ({len(zeus)})"
    else: src = "H2 fallback"
    print(f"{name:<20} live={len(live):>2} zeus={len(zeus):>2}  -> uses {src}")
    chosen = live if len(live)>=5 else zeus
    print("   order:", " > ".join(chosen[:8]) + (" ..." if len(chosen)>8 else ""))
