"""
Text scraper — Playwright + Claude-based extraction.

Strategy: Playwright loads and scrolls the page, captures a structured element
dump (tag + text). Claude reads the dump and identifies headline, subheads,
body_copy, and CTAs. No hardcoded CSS selectors for content — works on any PDP
regardless of framework or class names used.

Reviews are still CSS-based with a multi-selector fallback, since review
structures are reasonably consistent across sites.
"""

import re
import json
import anthropic
from datetime import datetime
from typing import List, Optional
from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout
from scraper.models import PDPTextData, Review
from utils.config_loader import load_config, get_env
from utils.logger import get_logger

log = get_logger("text_scraper")
config = load_config()
REVIEWS_LIMIT = config["scraper"]["reviews_limit"]


# ── Review selectors — tried in priority order ─────────────────────────────────
# Add site-specific selectors at the top; generic fallbacks at the bottom.
REVIEW_ITEM_SELECTORS = [
    ".review-body",                  # ManMatters
    "[class*='review-item']",
    "[class*='reviewItem']",
    "[class*='review-card']",
    "[class*='ReviewCard']",
    "[data-testid*='review']",
    "[class*='review']:not(button):not(a)",
]

REVIEW_TEXT_SELECTORS = [
    ".review-body",
    "[class*='review-body']",
    "[class*='review-text']",
    "[class*='reviewBody']",
    "[class*='reviewText']",
    "p",
]

REVIEW_RATING_SELECTORS = [
    ".ratings-stars",
    ".overall-rating",
    "[class*='rating']",
    "[class*='stars']",
    "[aria-label*='star']",
    "[aria-label*='rating']",
]

REVIEW_DATE_SELECTORS = [
    "time",
    "[class*='review-date']",
    "[class*='reviewDate']",
    "[class*='date']",
]


# ── Claude extraction system prompt ────────────────────────────────────────────

EXTRACT_SYSTEM = """You are extracting structured content from a health/wellness
product detail page (PDP). You will receive a structured dump of page elements
in the format [tag] text.

Identify and return exactly what is asked — no extras, no invented content.
Return ONLY valid JSON — no markdown, no explanation.

Schema:
{
  "headline": "the main H1 product headline (not navigation, not breadcrumbs)",
  "subheads": [
    "meaningful section title or feature heading"
  ],
  "body_copy": [
    "each distinct benefit claim, feature description, ingredient info, or usage instruction as a separate string"
  ],
  "cta_texts": [
    "purchase/action button text only"
  ]
}

Rules:
- headline: single H1 — the product name or main claim
- subheads: section titles and feature card headings that describe the product.
  Skip: navigation items, "View All", "Read More", breadcrumbs, footer links,
  cookie notices, "Frequently Bought Together" type upsell headings
- body_copy: benefit claims, feature descriptions, ingredient explanations,
  proof points, usage instructions. Each content block = one list item.
  Skip: prices, delivery info, wallet offers, navigation, rating numbers alone
- cta_texts: buttons that drive purchase or key action (Add to Cart, Buy Now,
  Subscribe, Get Started). Skip: filter/sort buttons, navigation, social share
"""


# ── Page loading helpers ───────────────────────────────────────────────────────

def _scroll_full_page(page: Page):
    """Scroll to bottom so lazy-loaded content renders."""
    page.evaluate("""
        () => new Promise((resolve) => {
            let total = 0;
            const step = 400;
            const interval = setInterval(() => {
                window.scrollBy(0, step);
                total += step;
                if (total >= document.body.scrollHeight) {
                    clearInterval(interval);
                    resolve();
                }
            }, 120);
        })
    """)
    page.wait_for_timeout(1000)
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(300)


def _get_structured_dump(page: Page) -> str:
    """
    Extract a structured element dump from the page.
    Returns lines like: [h1] Product Name, [h2] Section Title, [p] Body text...
    Deduplicates and caps at 400 lines to keep Claude prompt cost low.

    Excludes nav, header, footer, cookie banners, and modals so Claude
    only sees real PDP content — critical for React/dynamic PDPs where
    nav elements outnumber product copy in the early DOM.
    """
    raw = page.evaluate("""() => {
        const lines = [];
        const seen = new Set();

        // Elements whose subtree we always skip
        const SKIP_ROLES = new Set(['navigation','banner','contentinfo',
                                    'dialog','alertdialog','complementary']);
        const SKIP_TAGS  = new Set(['NAV','HEADER','FOOTER','ASIDE',
                                    'SCRIPT','STYLE','NOSCRIPT','IFRAME']);
        const SKIP_CLASS_RE = /cookie|modal|overlay|popup|drawer|sidebar|breadcrumb|cart-|toast|banner-strip|recently-viewed|upsell|cross-sell|wallet|pincode|delivery|shipping-info|check-delivery|installment|payment-option|category-nav|categories-nav|nav-menu|product-discount-tag|affiliate-card/i;

        // Text-pattern noise filter — drop lines whose text matches these
        // regardless of class. Catches site-specific noise (prices, delivery
        // estimates, wallet promos, category nav blobs) without hardcoding selectors.
        function isNoisyText(text) {
            // Price-only: ₹799, ₹999, "20% off", "Incl of all taxes"
            if (/^[₹$€£]?\d[\d\s,\.]*(%?\s*off)?$/.test(text)) return true;
            if (/incl\.?\s*(of\s+)?all\s+tax/i.test(text)) return true;
            // Delivery / logistics noise
            if (/get\s+by\s+\d|order\s+now.*get\s+by|estimated\s+delivery|deliver(y|ed)\s+by/i.test(text)) return true;
            if (/ships?\s+(in|within|by)|free\s+delivery|cod\s+available/i.test(text)) return true;
            // Wallet / payment noise
            if (/mm\s+wallet|save\s+upto|download\s+app|cards?\s+accepted|extra\s+cost|emi\s+available/i.test(text)) return true;
            // Category nav blob — long text concatenated from nav links
            if (/hair\s+regrowth|beard\s+growth|health\s*&\s*fitness/i.test(text) && text.length > 40) return true;
            // Pincode / delivery check widgets
            if (/enter\s+pincode|check\s+delivery|available\s+at/i.test(text)) return true;
            return false;
        }

        function shouldSkip(el) {
            if (SKIP_TAGS.has(el.tagName)) return true;
            const role = el.getAttribute('role') || '';
            if (SKIP_ROLES.has(role)) return true;
            const cls = (el.className || '').toString();
            if (SKIP_CLASS_RE.test(cls)) return true;
            // Walk up — if any ancestor is a skip zone, skip this too
            let p = el.parentElement;
            while (p) {
                if (SKIP_TAGS.has(p.tagName)) return true;
                const pr = p.getAttribute('role') || '';
                if (SKIP_ROLES.has(pr)) return true;
                const pc = (p.className || '').toString();
                if (SKIP_CLASS_RE.test(pc)) return true;
                p = p.parentElement;
            }
            return false;
        }

        // High-signal semantic elements — always collect (after skip check)
        const semantic = ['h1','h2','h3','h4','button'];
        document.querySelectorAll(semantic.join(',')).forEach(el => {
            if (shouldSkip(el)) return;
            const text = (el.innerText || '').trim().replace(/\\s+/g, ' ');
            if (!text || text.length < 4 || text.length > 500) return;
            if (isNoisyText(text)) return;
            const key = el.tagName + '|' + text;
            if (seen.has(key)) return;
            seen.add(key);
            lines.push('[' + el.tagName.toLowerCase() + '] ' + text);
        });

        // Content elements — only standalone meaningful text
        const content = ['p','li','span','div'];
        document.querySelectorAll(content.join(',')).forEach(el => {
            if (shouldSkip(el)) return;
            const text = (el.innerText || '').trim().replace(/\\s+/g, ' ');
            if (!text || text.length < 12 || text.length > 500) return;
            if (isNoisyText(text)) return;
            // Skip pure containers (text = parent text → just nesting duplication)
            const parent = el.parentElement;
            if (parent && (parent.innerText || '').trim() === text) return;
            const key = el.tagName + '|' + text;
            if (seen.has(key)) return;
            seen.add(key);
            lines.push('[' + el.tagName.toLowerCase() + '] ' + text);
        });

        return lines.slice(0, 400).join('\\n');
    }""")
    return raw or ""


# ── Claude extraction ──────────────────────────────────────────────────────────

def _claude_extract(structured_dump: str, url: str) -> dict:
    """
    Send the structured element dump to Claude.
    Returns dict with headline, subheads, body_copy, cta_texts.
    """
    client = anthropic.Anthropic(api_key=get_env("ANTHROPIC_API_KEY"))

    prompt = f"URL: {url}\n\nPAGE ELEMENTS:\n{structured_dump}"

    try:
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=1500,
            system=EXTRACT_SYSTEM,
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.content[0].text.strip()
        # Strip markdown fences if present
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())
    except Exception as e:
        log.warning(f"Claude extraction failed: {e} — returning empty structure")
        return {"headline": "", "subheads": [], "body_copy": [], "cta_texts": []}


# ── Reviews ────────────────────────────────────────────────────────────────────

def _find_review_elements(page: Page):
    """Try review item selectors in priority order until one returns results."""
    for sel in REVIEW_ITEM_SELECTORS:
        try:
            items = page.query_selector_all(sel)
            if items:
                log.info(f"Review selector matched: {sel} → {len(items)} items")
                return items
        except Exception:
            continue
    log.warning("No review elements found with any selector")
    return []


def _get_rating(el) -> Optional[float]:
    for sel in REVIEW_RATING_SELECTORS:
        try:
            rating_el = el.query_selector(sel)
            if rating_el:
                raw = (
                    rating_el.get_attribute("aria-label") or
                    rating_el.get_attribute("data-rating") or
                    rating_el.inner_text() or ""
                )
                nums = re.findall(r"\d+\.?\d*", raw)
                if nums:
                    return float(nums[0])
        except Exception:
            continue
    return None


def _get_review_text(el) -> Optional[str]:
    for sel in REVIEW_TEXT_SELECTORS:
        try:
            text_el = el.query_selector(sel)
            if text_el:
                t = text_el.inner_text().strip()
                if t:
                    return t
        except Exception:
            continue
    return el.inner_text().strip() or None


def _scrape_reviews(page: Page) -> List[Review]:
    """Scrape reviews using multi-selector strategy."""
    # Try to surface more reviews
    for _ in range(3):
        try:
            load_more = page.query_selector(
                "button:has-text('Load more'), button:has-text('Show more'), "
                "[data-load-more]"
            )
            if load_more and load_more.is_visible():
                load_more.click()
                page.wait_for_timeout(1200)
            else:
                break
        except Exception:
            break

    items = _find_review_elements(page)
    log.info(f"Found {len(items)} review elements on page")

    reviews = []
    for item in items[:REVIEWS_LIMIT]:
        try:
            text = _get_review_text(item)
            if not text:
                continue

            # Date
            date_str = None
            for sel in REVIEW_DATE_SELECTORS:
                try:
                    date_el = item.query_selector(sel)
                    if date_el:
                        date_str = date_el.inner_text().strip()
                        break
                except Exception:
                    continue

            reviews.append(Review(
                text=text[:1000],
                date=date_str,
                rating=_get_rating(item)
            ))
        except Exception as e:
            log.debug(f"Skipped a review element: {e}")
            continue

    log.info(f"Scraped {len(reviews)} reviews")
    return reviews


# ── Main scraper ───────────────────────────────────────────────────────────────

def scrape_text(url: str) -> PDPTextData:
    """
    Load the page with Playwright, scroll to trigger lazy content, then:
    1. Capture a structured element dump
    2. Extract meta tags and reviews via DOM
    3. Use Claude to identify headline / subheads / body_copy / CTAs from the dump
    Returns a populated PDPTextData.
    """
    log.info(f"Scraping → {url}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={
                "width":  config["scraper"]["screenshot_width"],
                "height": config["scraper"]["screenshot_height"]
            },
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = ctx.new_page()

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            # Wait for the React app to hydrate and render product content.
            # Try to find a product heading or add-to-cart button; fall back to
            # a flat 4s wait if neither appears (handles A/B variants gracefully).
            try:
                page.wait_for_selector(
                    "h1, [class*='product-title'], [class*='productTitle'], "
                    "[class*='product-name'], button:has-text('Add to Cart'), "
                    "button:has-text('Buy Now')",
                    timeout=8000
                )
            except Exception:
                page.wait_for_timeout(4000)
            _scroll_full_page(page)
            # Extra settle time after scroll — lets lazy sections finish rendering
            page.wait_for_timeout(1500)

            # Meta — reliable cross-site
            meta_title = ""
            try:
                t = page.query_selector("title")
                meta_title = t.inner_text().strip() if t else ""
            except Exception:
                pass

            meta_desc = ""
            try:
                m = page.query_selector("meta[name='description']")
                meta_desc = m.get_attribute("content").strip() if m else ""
            except Exception:
                pass

            # Structured element dump for Claude
            structured_dump = _get_structured_dump(page)
            log.info(f"Structured dump: {len(structured_dump.splitlines())} lines")

            # Full page text (for spell check in copy health scorer)
            full_text = ""
            try:
                full_text = page.inner_text("body")[:50000]
            except Exception:
                pass

            # Reviews (CSS-based with fallback)
            reviews = _scrape_reviews(page)

        except PWTimeout:
            log.error(f"Timeout loading {url}")
            raise
        except Exception as e:
            log.error(f"Error scraping {url}: {e}")
            raise
        finally:
            browser.close()

    # Claude extracts structured content from the element dump
    log.info("Sending element dump to Claude for extraction...")
    extracted = _claude_extract(structured_dump, url)

    headline   = extracted.get("headline", "") or ""
    subheads   = extracted.get("subheads", []) or []
    body_copy  = extracted.get("body_copy", []) or []
    cta_texts  = extracted.get("cta_texts", []) or []

    data = PDPTextData(
        url=url,
        scraped_at=datetime.utcnow().isoformat(),
        meta_title=meta_title,
        meta_description=meta_desc,
        headline=headline,
        subheads=subheads,
        body_copy=body_copy,
        cta_texts=cta_texts,
        full_page_text=full_text,
        reviews=reviews,
        reviews_count_scraped=len(reviews)
    )

    log.info(
        f"Done → headline: {'✓' if headline else '✗'} | "
        f"subheads: {len(subheads)} | body: {len(body_copy)} blocks | "
        f"CTAs: {len(cta_texts)} | reviews: {len(reviews)}"
    )
    return data


def scrape_all_urls(urls: List[str]) -> List[PDPTextData]:
    """Scrape multiple PDP URLs sequentially."""
    results = []
    for url in urls:
        try:
            results.append(scrape_text(url))
        except Exception as e:
            log.error(f"Failed to scrape {url}: {e}")
            results.append(PDPTextData(
                url=url,
                scraped_at=datetime.utcnow().isoformat(),
                full_page_text="SCRAPE_FAILED"
            ))
    return results
