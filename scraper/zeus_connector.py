"""
Zeus CMS connector — fetches PDP image data from Zeus via cache or API.

Two modes:
  1. Cache mode  (default): reads from outputs/zeus_cache/{page_id}.json
     Cache files are pre-populated by the agent via KAI tools before each run.
  2. API mode   (optional): if ZEUS_API_URL + ZEUS_API_KEY are set in .env,
     fetches live data from Zeus and refreshes the cache automatically.

Handles two Zeus PDP data structures:
  - "sections" style  (older PDPs like 2024397): imageGallery + sections.order
  - "displayOrder" style (newer PDPs like 2024503): displayOrder + rawWidgetIDMapping

Extracts all visual assets in desktop page display order and returns a flat
list of ZeusImage objects used by the visual scorer.
"""

import json
import re
import requests
from pathlib import Path
from typing import List, Optional
from datetime import datetime

from scraper.models import ZeusImage
from utils.config_loader import load_config, get_env
from utils.logger import get_logger

log = get_logger("zeus_connector")
config = load_config()

ROOT         = Path(__file__).parent.parent
CACHE_DIR    = ROOT / "outputs" / "zeus_cache"
SCREENSHOTS  = ROOT / config["report"]["screenshots_dir"]

# Widget types we skip (videos/reels — can't send to Vision)
SKIP_TYPES = {"REELS_SLIDER"}

# Boost for desktop variants — prefer desktop images over mobile
DESKTOP_PREFERRED = ("-desktop", "-web", "_desktop", "_web")


def _page_id_from_url(url: str) -> Optional[str]:
    """Extract numeric page ID from a ManMatters PDP URL."""
    m = re.search(r"/(\d{6,})(?:[/?]|$)", url)
    return m.group(1) if m else None


# ── Cache helpers ──────────────────────────────────────────────────────────────

def _cache_path(page_id: str) -> Path:
    return CACHE_DIR / f"{page_id}.json"


def _load_cache(page_id: str) -> Optional[dict]:
    path = _cache_path(page_id)
    if path.exists():
        try:
            data = json.loads(path.read_text())
            log.info(f"Zeus cache hit: {page_id} (fetched {data.get('fetched_at', 'unknown')})")
            return data
        except Exception as e:
            log.warning(f"Zeus cache read failed for {page_id}: {e}")
    return None


def _save_cache(page_id: str, data: dict):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    data["fetched_at"] = datetime.utcnow().isoformat()
    _cache_path(page_id).write_text(json.dumps(data, indent=2))
    log.info(f"Zeus cache saved: {page_id}")


# ── Live API fetch (optional) ──────────────────────────────────────────────────

def _fetch_live(page_id: str) -> Optional[dict]:
    """
    Fetch PDP JSON from Zeus API.
    Requires ZEUS_API_URL and ZEUS_API_KEY in .env.
    """
    api_url = get_env("ZEUS_API_URL", required=False, default="")
    api_key = get_env("ZEUS_API_KEY", required=False, default="")

    if not api_url or not api_key:
        return None

    try:
        brand = "MM"  # extend via config if multi-brand
        url = f"{api_url.rstrip('/')}/pdp/{page_id}?brand={brand}"
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=15
        )
        resp.raise_for_status()
        raw = resp.json()
        log.info(f"Zeus live fetch: {page_id}")

        # Normalise live response into our cache schema
        data = _normalise_live(page_id, raw)
        _save_cache(page_id, data)
        return data

    except Exception as e:
        log.warning(f"Zeus live fetch failed for {page_id}: {e}")
        return None


def _normalise_live(page_id: str, raw: dict) -> dict:
    """
    Convert raw Zeus API response into our canonical cache schema.
    Adjust field paths here as the actual API response shape is confirmed.
    """
    return {
        "page_id": page_id,
        "brand": raw.get("brand", "MM"),
        "page_url": raw.get("url", ""),
        "style": "displayOrder" if "displayOrder" in raw else "sections",
        "image_gallery": raw.get("imageGallery", []),
        "display_order": _extract_display_order(raw.get("displayOrder", {})),
        "widgets": _extract_widgets(raw.get("rawWidgetIDMapping", {})),
        "sections_order": raw.get("sections", {}).get("order", []),
        "sections_images": {},
    }


def _extract_display_order(do: dict) -> Optional[dict]:
    if not do:
        return None
    po = do.get("platformOverrides", {}).get("desktop-web", {})
    return {
        "default": do.get("default", []),
        "desktop_top_left": po.get("desktop-web-top-left", []),
        "desktop_top_right": po.get("desktop-web-top-right", []),
        "desktop_bottom": po.get("desktop-web-bottom", []),
    }


def _extract_widgets(mapping: dict) -> dict:
    """Flatten rawWidgetIDMapping → {widget_id: {type, title, images}}."""
    out = {}
    for wid, wdata in mapping.items():
        if not isinstance(wdata, dict):
            continue
        wtype = wdata.get("type", "")
        if wtype in SKIP_TYPES:
            out[wid] = {"type": wtype, "images": []}
            continue

        images = []
        wd = wdata.get("widgetData", {})

        # BANNER
        media = wd.get("media", {})
        if media.get("mediaType") == "image" and media.get("source"):
            images.append(media["source"])

        # MEDIA_SLIDER / MEDIA_WITH_PROGRESS_SLIDER
        for item in wd.get("items", []):
            m = item.get("media", {})
            if m.get("mediaType") == "image" and m.get("source"):
                images.append(m["source"])

        # MARQUEE_WITH_SCROLL
        for tag in wd.get("marqueeTags", []):
            img = tag.get("image", {})
            if img.get("url"):
                images.append(img["url"])

        header = wdata.get("header", {})
        title = header.get("title", "") or header.get("label", "")
        out[wid] = {"type": wtype, "title": title, "images": images}

    return out


# ── Main entry point ───────────────────────────────────────────────────────────

def get_zeus_reviews(url: str):
    """
    Return review list from Zeus cache for a PDP URL.
    Returns list of dicts: {rating, author, title, body, dateCreated, image_url}
    Returns [] if no Zeus review data is cached.
    """
    from scraper.models import Review
    page_id = _page_id_from_url(url)
    if not page_id:
        return []
    # Load cache first so manually-curated reviews (dates, ratings) are always used.
    # Live fetch is a fallback only — it may return image data without a "reviews" key.
    data = _load_cache(page_id) or _fetch_live(page_id)
    if not data:
        return []
    raw_reviews = data.get("reviews", {}).get("topReviews", [])
    reviews = []
    for r in raw_reviews:
        images = r.get("images", [])
        img_url = images[0].get("image", "") if images else None
        reviews.append(Review(
            text=r.get("body", ""),
            rating=float(r.get("rating", 0)) if r.get("rating") else None,
            date=r.get("dateCreated", ""),
            author=r.get("author", ""),
            title=r.get("title", ""),
            image_url=img_url,
        ))
    log.info(f"Zeus reviews: {len(reviews)} for page {page_id}")
    return reviews


def get_zeus_images(url: str) -> List[ZeusImage]:
    """
    Given a PDP URL, return all visual assets in desktop page order.
    Falls back to empty list if no Zeus data is available (Playwright takes over).
    """
    page_id = _page_id_from_url(url)
    if not page_id:
        log.debug(f"Could not extract page_id from URL: {url}")
        return []

    # Try live API first, then cache
    data = _fetch_live(page_id) or _load_cache(page_id)
    if not data:
        log.info(f"No Zeus data for {page_id} — Playwright will handle visuals")
        return []

    style = data.get("style", "sections")
    if style == "displayOrder":
        return _extract_display_order_images(data)
    else:
        return _extract_sections_images(data)


# ── Extraction: displayOrder style (new PDPs) ──────────────────────────────────

def _extract_display_order_images(data: dict) -> List[ZeusImage]:
    images: List[ZeusImage] = []
    do = data.get("display_order") or {}
    widgets = data.get("widgets") or {}

    # Hero gallery always comes first (maps to pdp-hero-slider)
    for i, item in enumerate(data.get("image_gallery", [])):
        url = item.get("original") or item.get("url", "")
        if url and _is_image(url):
            images.append(ZeusImage(
                url=url,
                position="hero",
                widget_id="pdp-hero-slider",
                widget_type="IMAGE_GALLERY",
                index=i,
                label=item.get("label", f"hero_{i+1}"),
            ))

    # Walk desktop bottom order (richest content)
    desktop_order = do.get("desktop_bottom", do.get("default", []))
    seen_widget_ids = set()

    for widget_id in desktop_order:
        if widget_id in seen_widget_ids:
            continue
        seen_widget_ids.add(widget_id)

        widget = widgets.get(widget_id)
        if not widget:
            continue

        wtype = widget.get("type", "")
        if wtype in SKIP_TYPES:
            continue

        position = _widget_position(widget_id, wtype)
        for i, img_url in enumerate(widget.get("images", [])):
            if img_url and _is_image(img_url):
                images.append(ZeusImage(
                    url=img_url,
                    position=position,
                    widget_id=widget_id,
                    widget_type=wtype,
                    index=i,
                    label=widget.get("title", "") or f"{widget_id}_{i+1}",
                ))

    log.info(f"Zeus extracted {len(images)} images for displayOrder PDP")
    return images


# ── Extraction: sections style (older PDPs) ────────────────────────────────────

def _extract_sections_images(data: dict) -> List[ZeusImage]:
    images: List[ZeusImage] = []

    # Hero gallery
    for i, item in enumerate(data.get("image_gallery", [])):
        url = item.get("original") or item.get("url", "")
        if url and _is_image(url):
            images.append(ZeusImage(
                url=url,
                position="hero",
                widget_id="imageGallery",
                widget_type="IMAGE_GALLERY",
                index=i,
                label=item.get("label", f"hero_{i+1}"),
            ))

    # Section images (clinical proof, how it works, ingredients, etc.)
    section_images = data.get("sections_images") or {}
    section_order = data.get("sections_order") or list(section_images.keys())

    for section_key in section_order:
        urls = section_images.get(section_key, [])
        position = _section_position(section_key)
        for i, url in enumerate(urls):
            if url and _is_image(url):
                images.append(ZeusImage(
                    url=url,
                    position=position,
                    widget_id=section_key,
                    widget_type="SECTION",
                    index=i,
                    label=f"{section_key}_{i+1}",
                ))

    log.info(f"Zeus extracted {len(images)} images for sections PDP")
    return images


# ── Helpers ────────────────────────────────────────────────────────────────────

def _is_image(url: str) -> bool:
    """Skip videos. GIFs are OK (Claude Vision can analyse them as stills)."""
    lower = url.lower()
    return not any(ext in lower for ext in (".mp4", ".webm", ".mov", "video.gumlet.io"))


def _widget_position(widget_id: str, widget_type: str) -> str:
    """Derive semantic position from widget ID and type."""
    wid = widget_id.lower()
    if "banner" in wid:
        if "first" in wid or "1" in wid:
            return "banner_1"
        if "second" in wid or "2" in wid:
            return "banner_2"
        if "third" in wid or "3" in wid:
            return "banner_3"
        return "banner"
    if "testimonial" in wid or "customer" in wid or "review" in wid:
        return "testimonials"
    if "ingredient" in wid or "carousel-3" in wid:
        return "ingredients"
    if "carousel-5" in wid or "benefit" in wid or "card" in wid:
        return "benefits"
    if "carousel-6" in wid:
        return "testimonials"
    if "marquee" in wid:
        return "social_proof"
    if "compare" in wid or "how-we" in wid:
        return "comparison"
    if "clinical" in wid or "proof" in wid:
        return "clinical_proof"
    if widget_type == "BANNER":
        return "banner"
    return "content"


def _section_position(section_key: str) -> str:
    key = section_key.lower()
    if "clinical" in key or "proof" in key:
        return "clinical_proof"
    if "how" in key and "work" in key:
        return "how_it_works"
    if "ingredient" in key:
        return "ingredients"
    if "compare" in key:
        return "comparison"
    if "review" in key:
        return "testimonials"
    if "gif" in key or "banner" in key:
        return "banner"
    return "content"


# ── Convenience: download images locally ──────────────────────────────────────

def clear_url_images(url_slug: str):
    """
    Delete all previously downloaded images for a URL slug —
    both Zeus CDN images and Playwright screenshots.
    Called at the start of each run so visuals are always fresh.
    """
    if not SCREENSHOTS.exists():
        return
    patterns = [
        f"{url_slug}_zeus_*",
        f"{url_slug}_carousel*",
        f"{url_slug}_banner*",
        f"{url_slug}_section_*",
    ]
    deleted = 0
    for pattern in patterns:
        for f in SCREENSHOTS.glob(pattern):
            try:
                f.unlink()
                deleted += 1
            except Exception as e:
                log.warning(f"Could not delete {f.name}: {e}")
    if deleted:
        log.info(f"Cleared {deleted} old image(s) for slug '{url_slug}'")


def download_zeus_images(zeus_images: List[ZeusImage], url_slug: str) -> List[ZeusImage]:
    """
    Download each ZeusImage fresh from the CDN to the screenshots directory.
    Always overwrites — old images are cleared before this is called.
    Returns the list with local_path filled in.
    """
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    updated = []

    for img in zeus_images:
        # Derive a clean filename from the URL
        fname_raw = img.url.split("?")[0].split("/")[-1]
        fname = re.sub(r"[^\w.\-]", "_", fname_raw)[:80]
        local = SCREENSHOTS / f"{url_slug}_zeus_{img.position}_{img.index}_{fname}"

        try:
            r = requests.get(img.url, timeout=15)
            r.raise_for_status()
            local.write_bytes(r.content)
            log.debug(f"Downloaded: {local.name}")
            updated.append(ZeusImage(
                url=img.url,
                position=img.position,
                widget_id=img.widget_id,
                widget_type=img.widget_type,
                index=img.index,
                label=img.label,
                local_path=str(local),
            ))
        except Exception as e:
            log.warning(f"Failed to download {img.url}: {e}")
            updated.append(img)  # keep without local_path

    downloaded = sum(1 for i in updated if i.local_path)
    log.info(f"Zeus images downloaded: {downloaded}/{len(zeus_images)}")
    return updated
