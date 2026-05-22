from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime


class Review(BaseModel):
    text: str
    rating: Optional[float] = None     # e.g. 4.5
    date: Optional[str] = None         # raw date string from page
    author: Optional[str] = None
    title: Optional[str] = None        # review title/headline
    image_url: Optional[str] = None    # first review image if present


class CarouselSlide(BaseModel):
    index: int                         # slide position (1-based)
    copy: str                          # all text found on the slide
    screenshot_path: Optional[str] = None


class Banner(BaseModel):
    location: str                      # e.g. "hero", "mid-page", "footer"
    copy: str                          # all text found on the banner
    screenshot_path: Optional[str] = None


class ZeusImage(BaseModel):
    """A visual asset fetched directly from Zeus CMS (CDN URL + metadata)."""
    url: str                           # CDN URL (i.mscwlns.co/...)
    position: str                      # semantic position: hero, banner_1, ingredients, etc.
    widget_id: str = ""                # Zeus widget ID (e.g. "first-banner-desktop")
    widget_type: str = ""              # Zeus widget type (BANNER, MEDIA_SLIDER, IMAGE_GALLERY)
    index: int = 0                     # position within widget (0-based)
    label: str = ""                    # human-readable label from Zeus data
    local_path: Optional[str] = None  # local path after download for Claude Vision


class PDPTextData(BaseModel):
    url: str
    scraped_at: str                    # ISO timestamp

    # Copy layers
    meta_title: Optional[str] = None
    meta_description: Optional[str] = None
    headline: Optional[str] = None
    subheads: List[str] = []
    body_copy: List[str] = []
    cta_texts: List[str] = []
    full_page_text: str = ""           # everything combined, for spell check

    # Reviews
    reviews: List[Review] = []
    reviews_count_scraped: int = 0

    # Visuals (filled by visual_scraper)
    carousels: List[CarouselSlide] = []
    banners: List[Banner] = []

    # Zeus CDN images (filled by zeus_connector when cache exists)
    zeus_images: List[ZeusImage] = []
    zeus_sourced: bool = False         # True when visuals came from Zeus, not Playwright

    # Raw HTML (optional, for debugging)
    raw_html: Optional[str] = None


class ScrapeResult(BaseModel):
    product_name: str
    pdp_data: List[PDPTextData]        # one entry per URL
    scraped_at: str
