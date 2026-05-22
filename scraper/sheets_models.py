from pydantic import BaseModel
from typing import List, Optional


class AdPerformance(BaseModel):
    hook: str                                # ad name (column: Ads)
    conversion_rate: Optional[float] = None  # Conv. %
    atc_rate: Optional[float] = None         # ATC %
    hook_rate: Optional[float] = None        # Hook Rate
    roas: Optional[float] = None             # ROAS
    spends: Optional[int] = None             # Spends
    ncs: Optional[int] = None               # New Customers
    ranking: Optional[int] = None           # Ranking (1 = best)


class URLPerformance(BaseModel):
    url: str
    spends: Optional[int] = None
    ncs: Optional[int] = None               # New Customers
    roas: Optional[float] = None
    atc_rate: Optional[float] = None        # ATC % — drop-off signal
    conversion_rate: Optional[float] = None # Conv. %
    is_active_pdp: bool = False             # True if URL matches our config


class SheetsData(BaseModel):
    """All data pulled from the Umbrella Sheet for one product."""
    product_name: str
    top_ads: List[AdPerformance] = []        # top N ads sorted by ranking
    url_stats: List[URLPerformance] = []     # all URLs in sheet
    pulled_at: str
