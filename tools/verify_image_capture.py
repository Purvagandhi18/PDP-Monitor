import re
from playwright.sync_api import sync_playwright
from scraper.visual_scraper import _playwright_cdn_sweep, _is_chrome_image

import yaml
_cfg = yaml.safe_load(open("config.yaml"))
URLS = []
for _p in _cfg["products"]:
    for _u in _p["urls"]:
        _short = _p["name"].split()[0] + "/" + _u["url"].rstrip("/").split("/")[-1]
        URLS.append((_short, _u["url"]))

def live_unique(url):
    """Independently count unique content images on the fully-interacted live page."""
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        pg = b.new_context(viewport={'width':1440,'height':900}).new_page()
        seen=set()
        pg.on('request', lambda r: seen.add(r.url) if 'i.mscwlns.co/media' in r.url or 'i.mscwlns.co/mosaic' in r.url else None)
        try:
            pg.goto(url, wait_until='domcontentloaded', timeout=35000)
            pg.wait_for_timeout(2500)
            # scroll
            pg.evaluate("async()=>{const d=ms=>new Promise(r=>setTimeout(r,ms));let y=0;for(let i=0;i<40;i++){window.scrollTo(0,y);await d(280);const t=document.body.scrollHeight;y+=700;if(y>=t)break;}}")
            pg.wait_for_timeout(800)
            # top + click all next buttons + bullets
            pg.evaluate("""async()=>{const d=ms=>new Promise(r=>setTimeout(r,ms));window.scrollTo(0,0);await d(400);
                const sel=['.swiper-button-next','.slick-next','.carousel-mobile-nav.right','[class*=carousel-mobile-nav][class*=right]','button[aria-label*=next i]','[class*=next]','[class*=arrow][class*=right]'];
                for(const btn of document.querySelectorAll(sel.join(','))){try{btn.scrollIntoView({block:'center'});await d(250);}catch(e){}for(let i=0;i<15;i++){try{if(btn.offsetParent===null)break;btn.click();await d(300);}catch(e){break;}}}
                for(const dot of document.querySelectorAll('.swiper-pagination-bullet,[class*=bullet],[class*=dot]')){try{if(dot.offsetParent)dot.click();await d(250);}catch(e){}}
            }""")
            pg.wait_for_timeout(1500)
            pg.evaluate("async()=>{const d=ms=>new Promise(r=>setTimeout(r,ms));const t=document.body.scrollHeight;for(let y=0;y<=t;y+=600){window.scrollTo(0,y);await d(150);}}")
            pg.wait_for_timeout(1000)
        except Exception as e:
            print(f'   live_unique error: {e}')
        finally:
            b.close()
    # filter to content (drop chrome + tiny)
    bases=set()
    for u in seen:
        if _is_chrome_image(u): continue
        bases.add(u.split('?')[0])
    return bases

print(f"{'PRODUCT':<20} {'SWEEP':>6} {'LIVE':>6} {'MISSED':>7}  STATUS")
for name, url in URLS:
    try:
        sweep = _playwright_cdn_sweep(url, 'verify_'+name.replace('/','_'))
        sweep_bases = {i.url.split('?')[0] for i in sweep}
        live = live_unique(url)
        missed = live - sweep_bases
        status = 'OK' if len(missed)<=2 else f'GAP! {len(missed)} missing'
        print(f"{name:<20} {len(sweep_bases):>6} {len(live):>6} {len(missed):>7}  {status}")
        for m in list(missed)[:5]:
            print(f"      MISSED: {m.split('/')[-1][:55]}")
    except Exception as e:
        print(f"{name:<20}  ERROR: {e}")
