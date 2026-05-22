"""
Visual scraper — Zeus-first, Playwright fallback.

Strategy:
  1. Zeus mode  (preferred): if a Zeus cache file exists for this URL's page_id,
     download CDN images directly and skip Playwright entirely. This gives Claude
     Vision full-resolution, properly labelled images in display order.
  2. Playwright fallback: when no Zeus cache exists, Playwright loads the page,
     detects carousel/banner structure, and screenshots each section.
"""

import re
from pathlib import Path
from typing import List, Optional, Tuple
from playwright.sync_api import sync_playwright, Page
from scraper.models import PDPTextData, CarouselSlide, Banner, ZeusImage
from scraper.zeus_connector import get_zeus_images, download_zeus_images, get_zeus_reviews
from utils.config_loader import load_config
from utils.logger import get_logger

log = get_logger("visual_scraper")
config = load_config()
SCREENSHOTS_DIR = Path(config["report"]["screenshots_dir"])
W = config["scraper"]["screenshot_width"]
H = config["scraper"]["screenshot_height"]


# ── Carousel detection strategies ─────────────────────────────────────────────
# Each entry: (carousel_container_selector, slide_selector_within_container)
# Tried in order — first one that returns slides wins.

CAROUSEL_STRATEGIES: List[Tuple[str, str]] = [
    # Swiper.js (very common on modern sites)
    (".swiper-wrapper",         ".swiper-slide"),
    (".swiper",                 ".swiper-slide"),
    # Slick
    (".slick-list",             ".slick-slide:not(.slick-cloned)"),
    (".slick-track",            ".slick-slide:not(.slick-cloned)"),
    # Generic slider / carousel
    (".slider",                 "div, li"),
    ("[class*='carousel__track']", "[class*='carousel__slide']"),
    ("[class*='carousel-inner']",  "[class*='carousel-item']"),
    ("[class*='carousel']",    "[class*='slide']"),
    # Gallery / product images
    ("[class*='gallery-track']",  "[class*='gallery-item']"),
    ("[class*='product-gallery']","[class*='gallery-item'], [class*='slide']"),
    # React/custom
    ("[class*='Slider']",       "[class*='Slide']"),
    ("[class*='ImageGallery']", "[class*='image-gallery-slide']"),
    # Broad fallback
    ("[class*='slider']",       "div, li"),
]

# Banner selectors — tried in order
BANNER_SELECTORS = [
    "[class*='hero-banner']",
    "[class*='heroBanner']",
    "[class*='hero']:not(section)",
    "[class*='banner']:not(nav)",
    "[class*='Banner']:not(nav)",
    "[class*='promo-strip']",
    "[class*='announcement-bar']",
    "[class*='top-bar']",
]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _slug(url: str) -> str:
    return re.sub(r"[^\w]", "_", url)[:60]


def _save_screenshot(page: Page, element, filename: str) -> Optional[str]:
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    path = SCREENSHOTS_DIR / filename
    try:
        element.screenshot(path=str(path))
        return str(path)
    except Exception:
        try:
            page.screenshot(path=str(path), full_page=False)
            return str(path)
        except Exception as e:
            log.debug(f"Screenshot failed for {filename}: {e}")
            return None


def _get_slide_copy(slide) -> str:
    """
    Extract clean copy from a carousel slide.
    Targets headings and text elements — skips prices, buttons, numeric-only text.
    """
    copy_parts = []
    for sel in ["h1", "h2", "h3", "h4", "p",
                "[class*='title']", "[class*='heading']",
                "[class*='copy']", "[class*='desc']", "[class*='text']"]:
        try:
            for el in slide.query_selector_all(sel):
                t = el.inner_text().strip()
                if (t and len(t) > 4 and
                        not t.replace(".", "").replace("%", "").replace(
                            ",", "").replace("₹", "").replace(" ", "").isdigit()):
                    copy_parts.append(t)
        except Exception:
            pass

    if copy_parts:
        seen, deduped = set(), []
        for p in copy_parts:
            if p not in seen:
                seen.add(p)
                deduped.append(p)
        return " | ".join(deduped)[:400]

    # Fallback
    try:
        return slide.inner_text().strip()[:300]
    except Exception:
        return ""


def _is_visible_and_sized(el) -> bool:
    """Return True if element is visible and has meaningful dimensions."""
    try:
        box = el.bounding_box()
        return box is not None and box["width"] > 50 and box["height"] > 50
    except Exception:
        return False


# ── Carousel scraping ──────────────────────────────────────────────────────────

def _try_carousel_strategies(page: Page) -> Tuple[Optional[object], str, str]:
    """
    Try each carousel strategy in order.
    Returns (carousel_element, slide_selector, strategy_name) or (None, '', '').
    """
    for carousel_sel, slide_sel in CAROUSEL_STRATEGIES:
        try:
            carousels = page.query_selector_all(carousel_sel)
            for carousel in carousels:
                if not _is_visible_and_sized(carousel):
                    continue
                # Test if slides exist within this carousel
                test_slides = carousel.query_selector_all(slide_sel)
                if test_slides:
                    log.info(f"Carousel matched: {carousel_sel} → {len(test_slides)} slides using '{slide_sel}'")
                    return carousel, slide_sel, carousel_sel
        except Exception:
            continue
    return None, "", ""


def _scrape_carousel(page: Page, carousel_el, slide_sel: str,
                     carousel_idx: int, url_slug: str) -> List[CarouselSlide]:
    slides_data = []
    slide_els = carousel_el.query_selector_all(slide_sel)

    if not slide_els:
        # Fallback: screenshot the whole carousel as one slide
        copy = _get_slide_copy(carousel_el)
        path = _save_screenshot(page, carousel_el,
                                f"{url_slug}_carousel{carousel_idx}_full.png")
        return [CarouselSlide(index=1, copy=copy, screenshot_path=path)]

    log.info(f"Carousel {carousel_idx}: {len(slide_els)} slides found")

    for i, slide in enumerate(slide_els):
        try:
            if not _is_visible_and_sized(slide):
                continue

            # Advance carousel if needed
            if i > 0:
                next_btn = carousel_el.query_selector(
                    "button.next, button[aria-label*='next'], button[aria-label*='Next'], "
                    ".slick-next, .swiper-button-next, [data-slide='next'], "
                    "[class*='next-btn'], [class*='nextBtn']"
                )
                if next_btn and next_btn.is_visible():
                    try:
                        next_btn.click()
                        page.wait_for_timeout(500)
                    except Exception:
                        pass

            slide.scroll_into_view_if_needed()
            page.wait_for_timeout(250)

            copy = _get_slide_copy(slide)
            path = _save_screenshot(page, slide,
                                    f"{url_slug}_carousel{carousel_idx}_slide{i + 1}.png")

            if path:
                slides_data.append(CarouselSlide(
                    index=i + 1, copy=copy, screenshot_path=path
                ))
                log.debug(f"  Slide {i + 1}: '{copy[:60]}' → saved")

        except Exception as e:
            log.debug(f"  Slide {i + 1} skipped: {e}")
            continue

    return slides_data


def _fallback_scroll_capture(page: Page, url_slug: str) -> List[CarouselSlide]:
    """
    When no carousel structure is found, capture the page in 3 vertical
    sections (top third, middle third, bottom third) as pseudo-slides.
    Gives the visual scorer something to work with.
    """
    log.info("No carousel found — falling back to scroll-and-capture")
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    slides = []

    scroll_positions = [0, 0.33, 0.66]
    labels = ["top", "middle", "bottom"]

    for pos, label in zip(scroll_positions, labels):
        try:
            page.evaluate(f"window.scrollTo(0, document.body.scrollHeight * {pos})")
            page.wait_for_timeout(600)
            path = SCREENSHOTS_DIR / f"{url_slug}_section_{label}.png"
            page.screenshot(path=str(path), clip={"x": 0, "y": 0, "width": W, "height": H})
            # Get visible text at this scroll position
            visible_text = page.evaluate("""() => {
                const vp = { top: window.scrollY, bottom: window.scrollY + window.innerHeight };
                const els = document.querySelectorAll('h1,h2,h3,p,[class*="title"]');
                const texts = [];
                els.forEach(el => {
                    const r = el.getBoundingClientRect();
                    if (r.top >= 0 && r.bottom <= window.innerHeight) {
                        const t = el.innerText?.trim();
                        if (t && t.length > 5) texts.push(t);
                    }
                });
                return texts.slice(0, 5).join(' | ');
            }""") or ""
            slides.append(CarouselSlide(
                index=len(slides) + 1,
                copy=visible_text[:300],
                screenshot_path=str(path)
            ))
            log.debug(f"  Section {label}: captured")
        except Exception as e:
            log.debug(f"  Section {label} capture failed: {e}")

    return slides


# ── Banner scraping ────────────────────────────────────────────────────────────

def _scrape_banners(page: Page, url_slug: str) -> List[Banner]:
    banners_data = []

    for sel in BANNER_SELECTORS:
        try:
            banner_els = page.query_selector_all(sel)
            if not banner_els:
                continue
            log.info(f"Banner selector matched: {sel} → {len(banner_els)} banners")
            for i, banner in enumerate(banner_els[:5]):
                try:
                    if not _is_visible_and_sized(banner):
                        continue
                    box = banner.bounding_box()
                    if box:
                        if box["y"] < H * 0.3:
                            location = "hero"
                        elif box["y"] < H * 0.7:
                            location = "mid-page"
                        else:
                            location = "footer"
                    else:
                        location = f"banner-{i + 1}"
                    copy = _get_slide_copy(banner)
                    path = _save_screenshot(page, banner,
                                            f"{url_slug}_banner_{location}_{i + 1}.png")
                    if path:
                        banners_data.append(Banner(
                            location=location, copy=copy, screenshot_path=path
                        ))
                except Exception as e:
                    log.debug(f"  Banner {i + 1} skipped: {e}")
            if banners_data:
                break  # Stop after first selector that yields results
        except Exception:
            continue

    if not banners_data:
        log.debug("No banners found with any selector")
    return banners_data


# ── Main ────────────────────────────────────────────────────────────────────────

def enrich_with_visuals(pdp_data: PDPTextData) -> PDPTextData:
    """
    Enriches PDPTextData with visual assets.

    Zeus mode (preferred): fetches CDN images directly from the Zeus cache —
    no Playwright needed, full resolution, properly labelled.

    Playwright fallback: opens the page in headless Chrome and screenshots
    carousel slides + banners when Zeus data is unavailable.
    """
    url = pdp_data.url
    url_slug = _slug(url)

    # ── Zeus-first path ────────────────────────────────────────────────────────
    zeus_images = get_zeus_images(url)
    if zeus_images:
        log.info(f"Zeus mode: {len(zeus_images)} images found — skipping Playwright")
        zeus_images = download_zeus_images(zeus_images, url_slug)

        pdp_data.zeus_images  = zeus_images
        pdp_data.zeus_sourced = True

        # Also populate carousels/banners from Zeus data so downstream
        # scorers that read those fields get something useful
        hero = [z for z in zeus_images if z.position == "hero"]
        other = [z for z in zeus_images if z.position != "hero"]

        pdp_data.carousels = [
            CarouselSlide(
                index=z.index + 1,
                copy=z.label,
                screenshot_path=z.local_path,
            )
            for z in hero
        ]
        pdp_data.banners = [
            Banner(
                location=z.position,
                copy=z.label,
                screenshot_path=z.local_path,
            )
            for z in other
            if z.local_path  # only include successfully downloaded images
        ]

        # Populate reviews from Zeus if not already scraped
        if not pdp_data.reviews:
            zeus_reviews = get_zeus_reviews(url)
            if zeus_reviews:
                pdp_data.reviews = zeus_reviews
                log.info(f"Zeus reviews: {len(zeus_reviews)} loaded")

        log.info(
            f"Zeus visuals done → {len(pdp_data.zeus_images)} total | "
            f"{len(pdp_data.carousels)} hero slides | "
            f"{len(pdp_data.banners)} content images"
        )
        return pdp_data

    # ── Playwright fallback ────────────────────────────────────────────────────
    log.info(f"Playwright fallback → {url}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": W, "height": H},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = ctx.new_page()

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2500)

            # Scroll to trigger lazy loads, then return to top
            page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
            page.wait_for_timeout(800)
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(500)

            # ── Carousel ──────────────────────────────────────────────────────
            carousel_el, slide_sel, strategy = _try_carousel_strategies(page)
            all_slides: List[CarouselSlide] = []

            if carousel_el:
                try:
                    slides = _scrape_carousel(page, carousel_el, slide_sel, 1, url_slug)
                    all_slides.extend(slides)
                except Exception as e:
                    log.warning(f"Carousel scrape failed: {e}")
            else:
                all_slides = _fallback_scroll_capture(page, url_slug)

            # ── Banners ───────────────────────────────────────────────────────
            banners = _scrape_banners(page, url_slug)

        except Exception as e:
            log.error(f"Visual scrape failed for {url}: {e}")
            return pdp_data
        finally:
            browser.close()

    pdp_data.carousels = all_slides
    pdp_data.banners   = banners

    log.info(
        f"Visuals done → {len(all_slides)} carousel slides | "
        f"{len(banners)} banners | "
        f"screenshots: {sum(1 for s in all_slides if s.screenshot_path)}"
    )
    return pdp_data
