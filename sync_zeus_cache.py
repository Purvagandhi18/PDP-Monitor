"""
Zeus Cache Sync — must be run via Claude Code (uses KAI MCP tools).

This script is NOT executed directly by the Python pipeline.
It documents the steps Claude Code runs before each audit to refresh
the Zeus cache from live KAI data.

USAGE (ask Claude Code):
  "Sync the Zeus cache for all Shilajit Gummies URLs"

WHAT IT DOES:
  For each URL in config.yaml:
    1. pdp_loadProduct(url) → session
    2. pdp_getSection(session) → full PDP JSON
    3. Extracts: imageGallery, displayOrder, rawWidgetIDMapping, sections, reviews.topReviews
    4. Writes to outputs/zeus_cache/{page_id}.json

WHY KAI (not Playwright):
  - KAI returns the raw Zeus CMS data — same source of truth the storefront renders
  - Reviews come with real dateCreated values (critical for freshness scoring)
  - Widget images come with full nested structure (recursive extractor handles depth)
  - No dependency on how the page renders in a browser

CACHE FORMAT (both styles supported):
  sections style (e.g. 2024397):
    image_gallery + sections_order + sections_images + sections_raw
  displayOrder style (e.g. 2024503, 2025001):
    image_gallery + display_order + widgets (raw rawWidgetIDMapping)

NOTE: The zeus_connector.py recursive extractor handles both formats.
If sections_images is empty, it falls back to sections_raw recursion.
If widget.images is empty, it falls back to recursive widget data search.

PAGES CURRENTLY CONFIGURED:
  2024397 — Product First   (sections style)
  2024503 — Daily Energy    (displayOrder style)
  2025001 — Summer          (displayOrder style)

FRESHNESS POLICY:
  Cache files older than 24 hours should be refreshed before a run.
  The pipeline warns if cache age > 24h but does NOT block execution.
"""

# This file is documentation only.
# The actual sync is performed interactively by Claude Code using KAI tools.
# See the docstring above for the process.

if __name__ == "__main__":
    print(__doc__)
