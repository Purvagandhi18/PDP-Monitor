from scraper.text_scraper import _scrape_reviews
from scraper.zeus_connector import get_zeus_reviews
from analyser.reviews_scorer import _parse_date
from playwright.sync_api import sync_playwright
import yaml

cfg = yaml.safe_load(open("config.yaml"))
urls = [(p["name"].split()[0]+"/"+u["url"].rstrip("/").split("/")[-1], u["url"]) for p in cfg["products"] for u in p["urls"]]

def live_reviews(url):
    with sync_playwright() as pw:
        b=pw.chromium.launch(headless=True)
        pg=b.new_context(viewport={'width':1440,'height':900}).new_page()
        try:
            pg.goto(url, wait_until="domcontentloaded", timeout=35000)
            pg.wait_for_timeout(3000)
            pg.evaluate("async()=>{const d=ms=>new Promise(r=>setTimeout(r,ms));let y=0;for(let i=0;i<30;i++){window.scrollTo(0,y);await d(220);const t=document.body.scrollHeight;y+=700;if(y>=t)break;}}")
            pg.wait_for_timeout(1000)
            return _scrape_reviews(pg)
        except Exception as e:
            print("  live err", e); return []
        finally:
            b.close()

print(f"{'URL':<20} {'ZEUS':>5} {'LIVE':>5}  FINAL(dated)")
for name,url in urls:
    z = get_zeus_reviews(url); zd=sum(1 for r in z if r.date)
    l = live_reviews(url);     ld=sum(1 for r in l if _parse_date(r.date))
    # mirror enrich choice
    if zd: final=f"{len(z)} Zeus ({zd} dated)"
    elif ld: final=f"{len(l)} live ({ld} dated)"
    elif z: final=f"{len(z)} Zeus (0 dated)"
    elif l: final=f"{len(l)} live (0 dated)"
    else: final="EMPTY!"
    print(f"{name:<20} {len(z):>5} {len(l):>5}  {final}")
