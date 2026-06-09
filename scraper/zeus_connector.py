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
from typing import List, Optional, Set
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

# ── Recursive image extraction constants ──────────────────────────────────────

# Field names that are known to carry image URLs or nested image structures
IMAGE_BEARING_FIELDS: Set[str] = {
    "image", "imageUrl", "image_url", "desktopImage", "mobileImage",
    "bannerImage", "media", "assets", "slides", "carouselItems",
    "heroImages", "original", "url", "source", "src", "images",
    "thumbnail", "thumbnailUrl", "coverImage", "backgroundImage",
}

# Known CDN domains for URL validation
IMAGE_CDN_DOMAINS = ("mscwlns.co", "cloudfront.net", "imgix.net",
                     "cloudinary.com", "imagekit.io", "cdn.")

# Image file extensions
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".avif")


def _looks_like_image_url(val: str) -> bool:
    """Return True if the string looks like an image CDN URL."""
    if not isinstance(val, str) or len(val) < 15:
        return False
    lower = val.lower()
    if not lower.startswith("http"):
        return False
    # Skip videos
    if any(ext in lower for ext in (".mp4", ".webm", ".mov", "video.")):
        return False
    # Known image extension in path (before query string)
    path = lower.split("?")[0]
    if any(path.endswith(ext) for ext in IMAGE_EXTENSIONS):
        return True
    # Known CDN domain — treat as image even without extension (CDN transforms)
    if any(cdn in lower for cdn in IMAGE_CDN_DOMAINS):
        return True
    return False


def _recursive_find_images(
    obj,
    found: List[str],
    visited: Optional[Set[int]] = None,
    depth: int = 0,
) -> None:
    """
    Recursively walk a Zeus widget/section dict and collect all image URLs.

    Strategy:
    - Strings: include if looks like image URL
    - Dicts: check known image-bearing field names; always recurse into nested dicts/lists
    - Lists: recurse into every item

    visited tracks object ids to prevent cycles.
    depth cap prevents runaway recursion on pathological structures.
    """
    if depth > 12:
        return
    if visited is None:
        visited = set()

    obj_id = id(obj)
    if obj_id in visited:
        return
    visited.add(obj_id)

    if isinstance(obj, str):
        if _looks_like_image_url(obj) and obj not in found:
            found.append(obj)

    elif isinstance(obj, list):
        for item in obj:
            _recursive_find_images(item, found, visited, depth + 1)

    elif isinstance(obj, dict):
        for key, val in obj.items():
            if val is None:
                continue
            if key in IMAGE_BEARING_FIELDS:
                # This field is known to carry images — recurse with priority
                _recursive_find_images(val, found, visited, depth + 1)
            elif isinstance(val, (dict, list)):
                # Always recurse into nested structures regardless of key name
                _recursive_find_images(val, found, visited, depth + 1)
            elif isinstance(val, str) and _looks_like_image_url(val):
                # Catch image URLs stored under arbitrary keys
                if val not in found:
                    found.append(val)


def _page_id_from_url(url: str) -> Optional[str]:
    """
    Extract a unique cache key from a ManMatters PDP URL.
    Includes a domain suffix when the same page ID exists on multiple domains
    (e.g. manmatters.com vs manmatters.co → 141464 vs 141464-co).
    """
    m = re.search(r"/(\d{6,})(?:[/?]|$)", url)
    if not m:
        return None
    page_id = m.group(1)
    # Append domain suffix for non-.com domains to avoid cache collisions
    if "manmatters.co/" in url or url.rstrip("/").endswith("manmatters.co"):
        return f"{page_id}-co"
    return page_id


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
    data["fetched_at"] = datetime.utcnow().isoformat() + "Z"
    _cache_path(page_id).write_text(json.dumps(data, indent=2))
    log.info(f"Zeus cache saved: {page_id}")


def _cache_is_stale(page_id: str) -> bool:
    """
    Return True if the cache file doesn't exist or is older than
    zeus.cache_max_age_minutes from config.yaml (default 5 min).
    """
    max_age_minutes = config.get("zeus", {}).get("cache_max_age_minutes", 5)
    path = _cache_path(page_id)
    if not path.exists():
        return True
    try:
        data = json.loads(path.read_text())
        fetched_str = data.get("fetched_at", "")
        if not fetched_str:
            return True
        fetched = datetime.fromisoformat(fetched_str.rstrip("Z"))
        age_minutes = (datetime.utcnow() - fetched).total_seconds() / 60
        log.debug(f"Cache age for {page_id}: {age_minutes:.1f} min (max={max_age_minutes})")
        return age_minutes > max_age_minutes
    except Exception:
        return True


def _is_staging_url(mcp_url: str) -> bool:
    return "stg-" in mcp_url or "staging" in mcp_url or "stg." in mcp_url


def _kai_sync(page_id: str, url: str) -> Optional[dict]:
    """
    Fetch live PDP data from the KAI MCP server and write to cache.

    Staging guard: if MOSAIC_MCP_URL points to the staging server AND
    a cache already exists, we skip overwriting — staging data is test data
    (wrong products, wrong reviews). Only production MCP overwrites existing cache.
    Missing cache is always populated regardless of staging/prod.

    Returns the new/existing cache dict, or None if unavailable.
    """
    from utils.config_loader import get_env
    from tools.kai_client import build_zeus_cache, DEFAULT_MCP_URL

    mcp_url = get_env("MOSAIC_MCP_URL", required=False, default=DEFAULT_MCP_URL)
    is_staging = _is_staging_url(mcp_url)
    cache_exists = _cache_path(page_id).exists()

    if is_staging and cache_exists:
        log.info(
            f"KAI sync skipped for {page_id}: staging MCP + cache already exists. "
            f"Set MOSAIC_MCP_URL to production endpoint to enable live sync."
        )
        return None

    if is_staging:
        log.warning(
            f"KAI syncing {page_id} from STAGING MCP — review dates may be test data. "
            f"Set MOSAIC_MCP_URL to production endpoint for accurate reviews."
        )

    try:
        cache = build_zeus_cache(url, page_id)
        if cache:
            _save_cache(page_id, cache)
            log.info(f"KAI sync complete for {page_id} (staging={is_staging})")
            return cache
        else:
            log.warning(f"KAI sync returned no data for {page_id}")
    except Exception as e:
        log.warning(f"KAI sync failed for {page_id}: {e} — falling back to existing cache")
    return None


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

    Always attempts KAI sync first (refreshes cache if stale >24h or missing).
    Falls back to existing cache if KAI is unavailable.
    Returns [] only if no data is available at all.
    """
    from scraper.models import Review
    page_id = _page_id_from_url(url)
    if not page_id:
        log.warning(f"Zeus reviews: could not extract page_id from {url}")
        return []

    # KAI sync: refresh cache if stale or missing
    if _cache_is_stale(page_id):
        log.info(f"Zeus cache stale/missing for {page_id} — syncing via KAI")
        data = _kai_sync(page_id, url)
    else:
        data = None

    # Load from cache (either just-synced or existing)
    if not data:
        data = _load_cache(page_id) or _fetch_live(page_id)

    if not data:
        log.warning(f"Zeus reviews: no data available for {page_id}")
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

    dates_present = sum(1 for rev in reviews if rev.date)
    log.info(
        f"Zeus reviews: {len(reviews)} for page {page_id} "
        f"({dates_present}/{len(reviews)} with dateCreated)"
    )
    return reviews


def get_zeus_images(url: str) -> List[ZeusImage]:
    """
    Given a PDP URL, return all visual assets in desktop page order.

    Returns a structured extraction error log entry (not empty list) if Zeus
    data exists but no images could be found, so callers can distinguish
    "no Zeus data" from "Zeus data present but image extraction failed".
    """
    page_id = _page_id_from_url(url)
    if not page_id:
        log.warning(f"Zeus: could not extract page_id from URL: {url}")
        return []

    # KAI sync: refresh cache if stale or missing (runs on every pipeline call)
    if _cache_is_stale(page_id):
        log.info(f"Zeus cache stale/missing for {page_id} — syncing via KAI")
        data = _kai_sync(page_id, url)
    else:
        data = None

    # Load from cache (just-synced or existing) — fall back to live API last
    if not data:
        data = _load_cache(page_id) or _fetch_live(page_id)

    if not data:
        log.info(f"Zeus: no data for page {page_id} — Playwright will handle visuals")
        return []

    style = data.get("style", "sections")
    log.info(f"Zeus: extracting images for page {page_id} (style={style})")

    if style == "displayOrder":
        images = _extract_display_order_images(data)
    else:
        images = _extract_sections_images(data)

    if not images:
        log.error(
            f"Zeus extraction error: page {page_id} (style={style}) — "
            f"no visual assets found despite cache existing. "
            f"Cache keys present: {list(data.keys())}. "
            f"widgets count: {len(data.get('widgets', {}))}. "
            f"sections_images count: {len(data.get('sections_images', {}))}. "
            f"image_gallery count: {len(data.get('image_gallery', []))}."
        )
    else:
        log.info(f"Zeus extracted {len(images)} images for {page_id}")

    return images


# ── Extraction: displayOrder style (new PDPs) ──────────────────────────────────

def _extract_display_order_images(data: dict) -> List[ZeusImage]:
    """
    Extract images from displayOrder-style Zeus cache.

    Two passes per widget:
    1. Read pre-flattened images[] array (fast path for well-formed cache)
    2. Recursively search raw widget data for any image-bearing fields
       (catches images in non-standard / UNKNOWN widget structures)
    """
    images: List[ZeusImage] = []
    seen_urls: Set[str] = set()
    do = data.get("display_order") or {}
    widgets = data.get("widgets") or {}

    def _add(url: str, position: str, widget_id: str, wtype: str,
             idx: int, label: str):
        if url and _is_image(url) and url not in seen_urls:
            seen_urls.add(url)
            itype = _position_to_image_type(position)
            images.append(ZeusImage(
                url=url, position=position, widget_id=widget_id,
                widget_type=wtype, index=idx, label=label, image_type=itype,
            ))

    # Hero gallery always first
    for i, item in enumerate(data.get("image_gallery", [])):
        url = item.get("original") or item.get("url", "")
        _add(url, "hero", "pdp-hero-slider", "IMAGE_GALLERY", i,
             item.get("label", f"hero_{i+1}"))

    # Walk desktop bottom order (richest content), fall back to default
    desktop_order = do.get("desktop_bottom", do.get("default", []))
    seen_widget_ids: Set[str] = set()

    for display_pos, widget_id in enumerate(desktop_order):
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

        # Pass 1: pre-flattened images list (fast)
        pre_flat = widget.get("images", [])
        for i, img_url in enumerate(pre_flat):
            _add(img_url, position, widget_id, wtype, i,
                 widget.get("title", "") or f"{widget_id}_{i+1}")

        # Pass 2: recursive search through full widget data
        # (catches UNKNOWN widgets and non-standard field names)
        recursive_found: List[str] = []
        _recursive_find_images(widget, recursive_found)
        for i, img_url in enumerate(recursive_found):
            _add(img_url, position, widget_id, wtype,
                 len(pre_flat) + i, f"{widget_id}_recursive_{i+1}")

    if not images:
        log.warning("displayOrder extraction: no images found in any widget")
    else:
        log.debug(f"displayOrder extraction: {len(images)} unique images across "
                  f"{len(seen_widget_ids)} widgets")
    return images


# ── Extraction: sections style (older PDPs) ────────────────────────────────────

def _extract_sections_images(data: dict) -> List[ZeusImage]:
    """
    Extract images from sections-style Zeus cache.

    Two passes per section:
    1. Read pre-extracted sections_images lists (fast path)
    2. Recursively search raw section data if present (catches unlisted sections)
    """
    images: List[ZeusImage] = []
    seen_urls: Set[str] = set()

    def _add(url: str, position: str, widget_id: str, wtype: str,
             idx: int, label: str):
        if url and _is_image(url) and url not in seen_urls:
            seen_urls.add(url)
            itype = _position_to_image_type(position)
            images.append(ZeusImage(
                url=url, position=position, widget_id=widget_id,
                widget_type=wtype, index=idx, label=label, image_type=itype,
            ))

    # Hero gallery
    for i, item in enumerate(data.get("image_gallery", [])):
        url = item.get("original") or item.get("url", "")
        _add(url, "hero", "imageGallery", "IMAGE_GALLERY", i,
             item.get("label", f"hero_{i+1}"))

    # Section images — walk in sections_order so we follow page layout
    section_images = data.get("sections_images") or {}
    sections_raw = data.get("sections_raw") or {}   # raw section data if present
    section_order = data.get("sections_order") or list(section_images.keys())
    # Always include sections_raw keys not already in section_order
    # (catches gl_* growthLanding sections added after initial cache build)
    section_order = list(section_order) + [
        k for k in sections_raw if k not in section_order
    ]

    for section_key in section_order:
        position = _section_position(section_key)

        # Pass 1: pre-extracted URL list
        pre_urls = section_images.get(section_key, [])
        for i, url in enumerate(pre_urls):
            _add(url, position, section_key, "SECTION", i, f"{section_key}_{i+1}")

        # Pass 2: recursive search in raw section data if available
        raw_section = sections_raw.get(section_key)
        if raw_section:
            recursive_found: List[str] = []
            _recursive_find_images(raw_section, recursive_found)
            for i, url in enumerate(recursive_found):
                _add(url, position, section_key, "SECTION",
                     len(pre_urls) + i, f"{section_key}_recursive_{i+1}")

    if not images:
        log.warning("sections extraction: no images found — "
                    f"sections_order has {len(section_order)} keys, "
                    f"sections_images has {len(section_images)} populated")
    else:
        log.debug(f"sections extraction: {len(images)} unique images from "
                  f"{len(section_images)} section image lists")
    return images


# ── Helpers ────────────────────────────────────────────────────────────────────

def _position_to_image_type(position: str) -> str:
    """Map semantic position to normalised image_type for the visual scorer."""
    mapping = {
        "hero":          "hero",
        "banner_1":      "banner",
        "banner_2":      "banner",
        "banner_3":      "banner",
        "banner":        "banner",
        "testimonials":  "testimonial",
        "ingredients":   "carousel",
        "benefits":      "carousel",
        "social_proof":  "banner",
        "comparison":    "comparison",
        "clinical_proof":"section",
        "how_it_works":  "section",
        "content":       "section",
    }
    return mapping.get(position, "section")


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
