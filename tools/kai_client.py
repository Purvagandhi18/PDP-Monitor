"""
KAI MCP Client — Python-native MCP SSE client for the Mosaic PDP MCP server.

Connects directly to the MCP PDP server via SSE transport (no Claude Code
dependency) and fetches live Zeus CMS data for any ManMatters PDP URL.

Server: MOSAIC_MCP_URL in .env (defaults to staging)
Protocol: MCP over SSE (SSE GET + JSON-RPC POST)
Auth: None required (server is open on staging; production may need a token)

Usage:
    from tools.kai_client import fetch_pdp_json
    data = fetch_pdp_json("https://manmatters.com/dp/2025001")
    # returns full PDP dict with imageGallery, rawWidgetIDMapping, sections, reviews
"""

import json
import threading
import queue
import time
from typing import Optional

import httpx

from utils.config_loader import get_env
from utils.logger import get_logger

log = get_logger("kai_client")

# ── Config ─────────────────────────────────────────────────────────────────────

DEFAULT_MCP_URL = "https://stg-pdp-mcp.mosaicwellness.in"

# How long to wait for a tool response (PDP fetch can take 10–20s)
TOOL_TIMEOUT_SECS = 45

# How many SSE drain iterations before giving up
MAX_DRAIN_ITERS = 60


# ── SSE reader ─────────────────────────────────────────────────────────────────

def _sse_reader(stream, q: queue.Queue) -> None:
    """
    Background thread: reads SSE lines from an httpx stream and puts
    complete events onto the queue as (event_type, data) tuples.
    """
    event_type = None
    data_buf = []
    try:
        for line in stream.iter_lines():
            if line.startswith("event:"):
                event_type = line[6:].strip()
                data_buf = []
            elif line.startswith("data:"):
                data_buf.append(line[5:].strip())
            elif line == "" and data_buf:
                q.put((event_type, "\n".join(data_buf)))
                event_type = None
                data_buf = []
    except Exception as e:
        log.debug(f"SSE reader ended: {e}")
    finally:
        q.put(("_closed", ""))


# ── MCP session ────────────────────────────────────────────────────────────────

class _MCPSession:
    """
    Manages a single MCP SSE session lifecycle:
    open → initialize → call tool → close.
    """

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._events: queue.Queue = queue.Queue()
        self._stream_ctx = None
        self._post_url: Optional[str] = None
        self._http = httpx.Client(timeout=TOOL_TIMEOUT_SECS)

    def __enter__(self):
        self._stream_ctx = httpx.stream(
            "GET", f"{self.base_url}/sse",
            headers={"Accept": "text/event-stream"},
            timeout=TOOL_TIMEOUT_SECS + 30,
        )
        stream = self._stream_ctx.__enter__()
        threading.Thread(
            target=_sse_reader, args=(stream, self._events), daemon=True
        ).start()

        # Wait for endpoint event
        evt, data = self._get(timeout=10)
        if evt != "endpoint":
            raise RuntimeError(f"Expected 'endpoint' event, got: {evt!r}")
        self._post_url = (
            self.base_url + data if data.startswith("/") else data
        )
        log.debug(f"MCP session endpoint: {self._post_url}")

        # MCP initialize handshake
        self._post({
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "pdp-monitor", "version": "2.0"},
            },
        })
        self._drain_until_id(0, timeout=10)

        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self

    def __exit__(self, *_):
        try:
            self._http.close()
        except Exception:
            pass
        if self._stream_ctx:
            try:
                self._stream_ctx.__exit__(None, None, None)
            except Exception:
                pass

    def call(self, tool: str, arguments: dict, call_id: int = 1) -> dict:
        """Call an MCP tool and return the JSON-RPC result dict."""
        self._post({
            "jsonrpc": "2.0", "id": call_id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        })
        return self._drain_until_id(call_id, timeout=TOOL_TIMEOUT_SECS)

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _post(self, payload: dict) -> None:
        self._http.post(
            self._post_url,
            json=payload,
            headers={"Content-Type": "application/json"},
        )

    def _get(self, timeout: float = 5.0):
        return self._events.get(timeout=timeout)

    def _drain_until_id(self, target_id: int, timeout: float) -> dict:
        """Drain SSE events until we find one with id == target_id."""
        deadline = time.monotonic() + timeout
        for _ in range(MAX_DRAIN_ITERS):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                evt, data = self._events.get(timeout=min(remaining, 4))
                if evt == "_closed":
                    raise RuntimeError("SSE stream closed unexpectedly")
                try:
                    parsed = json.loads(data)
                    if parsed.get("id") == target_id:
                        return parsed
                except json.JSONDecodeError:
                    pass
            except queue.Empty:
                continue
        raise TimeoutError(
            f"No MCP response for id={target_id} within {timeout}s"
        )


# ── Public API ─────────────────────────────────────────────────────────────────

def fetch_pdp_json(product_url: str) -> Optional[dict]:
    """
    Fetch the full Zeus CMS PDP JSON for a product URL.

    Returns the raw PDP dict (same structure as Zeus cache content key),
    or None if the fetch fails.

    The dict contains:
      imageGallery, rawWidgetIDMapping, displayOrder, sections (with reviews),
      productInfo, etc.
    """
    mcp_url = get_env("MOSAIC_MCP_URL", required=False, default=DEFAULT_MCP_URL)
    log.info(f"KAI: fetching PDP JSON via MCP ({mcp_url}) for {product_url}")

    try:
        with _MCPSession(mcp_url) as session:
            result = session.call(
                "get_product_json",
                {"productUrl": product_url},
                call_id=1,
            )

        # Unwrap JSON-RPC result
        if "error" in result:
            log.error(
                f"KAI MCP error for {product_url}: "
                f"{result['error'].get('message', result['error'])}"
            )
            return None

        content = result.get("result", {}).get("content", [])
        if not content:
            log.error(f"KAI: empty content in response for {product_url}")
            return None

        text = content[0].get("text", "")
        pdp = json.loads(text)
        log.info(
            f"KAI: fetched {product_url} — "
            f"gallery={len(pdp.get('imageGallery', []))} "
            f"widgets={len(pdp.get('rawWidgetIDMapping', {}))} "
            f"reviews={len(pdp.get('sections', {}).get('reviews', {}).get('topReviews', []))}"
        )
        return pdp

    except TimeoutError as e:
        log.error(f"KAI: timeout fetching {product_url}: {e}")
    except Exception as e:
        log.error(f"KAI: failed to fetch {product_url}: {e}")
    return None


def build_zeus_cache(product_url: str, page_id: str) -> Optional[dict]:
    """
    Fetch live PDP JSON from KAI and normalise into the Zeus cache schema
    used by zeus_connector.py.

    Returns a cache dict ready to be written to outputs/zeus_cache/{page_id}.json,
    or None if the fetch failed.
    """
    pdp = fetch_pdp_json(product_url)
    if not pdp:
        return None

    do = pdp.get("displayOrder", {})
    po = do.get("platformOverrides", {}).get("desktop-web", {})

    # Determine style
    has_widgets = bool(pdp.get("rawWidgetIDMapping"))
    has_display_order = bool(do.get("default") or po)
    style = "displayOrder" if (has_widgets or has_display_order) else "sections"

    # Sections-style: extract sections_raw and sections_images
    sections = pdp.get("sections", {})
    sections_images: dict = {}
    sections_raw: dict = {}

    for k, v in sections.items():
        if k in ("order", "recentlyViewed", "frequentlyBoughtTogether", "reviews"):
            continue
        if isinstance(v, dict):
            sections_raw[k] = v
            imgs = []
            for field in ("images", "imageUrl", "image"):
                val = v.get(field)
                if isinstance(val, list):
                    imgs.extend(u for u in val if isinstance(u, str) and u.startswith("http"))
                elif isinstance(val, str) and val.startswith("http"):
                    imgs.append(val)
            if imgs:
                sections_images[k] = imgs

    # Also extract growthLanding — this top-level key contains rich visual sections
    # (customerReview/What our men say, uses/How to use, caseStudy, etc.)
    # that are NOT inside sections{} and would otherwise be missed.
    GROWTH_VISUAL_SECTIONS = {
        "customerReview", "uses", "highlights", "caseStudy",
        "treats", "safeAndEffective", "imageGallery",
    }
    growth_landing = pdp.get("growthLanding", {})
    for k, v in growth_landing.items():
        if k in GROWTH_VISUAL_SECTIONS and isinstance(v, (dict, list)):
            sections_raw[f"gl_{k}"] = v
            log.debug(f"KAI: added growthLanding.{k} → sections_raw.gl_{k}")

    cache = {
        "page_id": page_id,
        "brand": "MM",
        "page_url": product_url,
        "fetched_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        "style": style,
        "image_gallery": pdp.get("imageGallery", []),
        "display_order": {
            "default":          do.get("default", []),
            "desktop_top_left": po.get("desktop-web-top-left", []),
            "desktop_top_right":po.get("desktop-web-top-right", []),
            "desktop_bottom":   po.get("desktop-web-bottom", []),
        },
        "widgets": pdp.get("rawWidgetIDMapping", {}),
        "sections_order": sections.get("order", list(sections_raw.keys())),
        "sections_images": sections_images,
        "sections_raw": sections_raw,
        "reviews": {
            "topReviews": sections.get("reviews", {}).get("topReviews", [])
        },
    }

    reviews = cache["reviews"]["topReviews"]
    log.info(
        f"KAI cache built for {page_id}: "
        f"gallery={len(cache['image_gallery'])} "
        f"widgets={len(cache['widgets'])} "
        f"sections_raw={len(sections_raw)} "
        f"reviews={len(reviews)}"
    )
    return cache
