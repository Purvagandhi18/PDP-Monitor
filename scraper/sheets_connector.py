import gspread
import google.auth
from datetime import datetime
from typing import Optional, List
from scraper.sheets_models import AdPerformance, URLPerformance, SheetsData
from utils.config_loader import load_config
from utils.logger import get_logger

log = get_logger("sheets_connector")
config = load_config()
SHEETS_CFG = config["google_sheets"]


# ── Auth ───────────────────────────────────────────────────────────────────────

def _get_client() -> gspread.Client:
    """Authenticate using gcloud Application Default Credentials."""
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.readonly"
    ]
    credentials, _ = google.auth.default(scopes=scopes)
    return gspread.authorize(credentials)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _safe_float(value) -> Optional[float]:
    if not value or str(value).strip() in ("-", "#DIV/0!", "#VALUE!", ""):
        return None
    try:
        return float(str(value).replace("%", "").replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _safe_int(value) -> Optional[int]:
    if not value or str(value).strip() in ("-", "#DIV/0!", "#VALUE!", ""):
        return None
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (ValueError, TypeError):
        return None


def _rows_to_dicts(rows: List[list]) -> List[dict]:
    """Convert a 2D list (header row + data rows) into list of dicts."""
    if not rows or len(rows) < 2:
        return []
    headers = [str(h).strip() for h in rows[0]]
    result = []
    for row in rows[1:]:
        # Pad short rows with empty strings
        padded = row + [""] * (len(headers) - len(row))
        result.append(dict(zip(headers, padded)))
    return result


def _get_col(row: dict, key: str) -> str:
    """Case-insensitive column lookup."""
    for k, v in row.items():
        if k.strip().lower() == key.strip().lower():
            return str(v).strip()
    return ""


# ── Product filter ─────────────────────────────────────────────────────────────

def _set_product_filter(ws: gspread.Worksheet, product_name: str):
    """
    Set the product dropdown (cell A2) to the correct product.
    This filters the sheet data before we read it.
    """
    try:
        current = ws.acell("A2").value
        if current == product_name:
            log.info(f"Product filter already set to: {product_name}")
            return
        ws.update_acell("A2", product_name)
        log.info(f"Product filter set to: {product_name}")
        # Small wait for sheet to recalculate formulas
        import time; time.sleep(2)
    except Exception as e:
        log.warning(f"Could not set product filter: {e}. Reading sheet as-is.")


# ── PWDD Tab (Ads) ─────────────────────────────────────────────────────────────

def _pull_top_ads(sheet: gspread.Spreadsheet, product_name: str) -> List[AdPerformance]:
    """
    Read the Ads section from the PWDD tab.
    Header is at row 43, data starts row 44.
    Columns: Ads | Spends | NCs | CAC | AOV | ROAS | Prepaid % | CTR |
             CPC | CPM | Hook Rate | ATC % | Purchase % | Ranking | ...
    """
    ads_cfg = SHEETS_CFG["ads_tab"]
    top_n   = ads_cfg.get("top_n", 30)

    try:
        ws = sheet.worksheet(ads_cfg["name"])
    except gspread.WorksheetNotFound:
        log.error(f"Tab '{ads_cfg['name']}' not found. Check config.yaml → google_sheets.ads_tab.name")
        return []

    # Set dropdown filter to this product
    _set_product_filter(ws, product_name)

    # Read from row 43 (header) to row 43+top_n+5 (buffer)
    header_row = ads_cfg.get("header_row", 43)
    end_row    = header_row + top_n + 10
    raw = ws.get(f"A{header_row}:P{end_row}")
    if raw:
        log.info(f"PWDD header row found: {raw[0]}")
    rows = _rows_to_dicts(raw)
    log.info(f"PWDD ads section: {len(rows)} rows read")

    ads = []
    for row in rows:
        hook = _get_col(row, "Ads")
        if not hook or hook in ("-", ""):
            continue

        ads.append(AdPerformance(
            hook=hook,
            conversion_rate=_safe_float(_get_col(row, "Conv. %")),
            atc_rate=_safe_float(_get_col(row, "ATC %")),
            hook_rate=_safe_float(_get_col(row, "Hook Rate")),
            roas=_safe_float(_get_col(row, "ROAS")),
            spends=_safe_int(_get_col(row, "Spends")),
            ncs=_safe_int(_get_col(row, "NCs")),
            ranking=_safe_int(_get_col(row, "Ranking")),
        ))

        if len(ads) >= top_n:
            break

    # Sort by ranking if available
    ads_with_rank = [a for a in ads if a.ranking is not None]
    ads_without   = [a for a in ads if a.ranking is None]
    ads = sorted(ads_with_rank, key=lambda a: a.ranking) + ads_without

    log.info(f"Pulled {len(ads)} ads for {product_name}")
    return ads


# ── URL Tab ────────────────────────────────────────────────────────────────────

def _pull_url_stats(sheet: gspread.Spreadsheet, product_name: str, urls: List[str]) -> List[URLPerformance]:
    """
    Read the URL section from the URL tab.
    Header is at row 4, data starts row 5.
    Columns: URL | Spends | NCs | CAC | AOV | ROAS | Prepaid % | CTR |
             CPC | CPM | ATC % | Purchase % | Spends % | NCs %
    """
    url_cfg = SHEETS_CFG["url_tab"]

    try:
        ws = sheet.worksheet(url_cfg["name"])
    except gspread.WorksheetNotFound:
        log.error(f"Tab '{url_cfg['name']}' not found. Check config.yaml → google_sheets.url_tab.name")
        return []

    # Set dropdown filter to this product
    _set_product_filter(ws, product_name)

    # Read from row 4 (header) — URL section is small (typically <20 rows)
    header_row = url_cfg.get("header_row", 4)
    raw = ws.get(f"A{header_row}:N30")
    rows = _rows_to_dicts(raw)
    log.info(f"URL tab: {len(rows)} rows read")

    stats = []
    for row in rows:
        row_url = _get_col(row, "URL").strip().rstrip("/")
        if not row_url or not row_url.startswith("http"):
            continue

        # Match against our configured PDP URLs
        matched = any(
            u.strip().rstrip("/") == row_url or
            row_url in u or u in row_url
            for u in urls
        )

        stats.append(URLPerformance(
            url=row_url,
            spends=_safe_int(_get_col(row, "Spends")),
            ncs=_safe_int(_get_col(row, "NCs")),
            roas=_safe_float(_get_col(row, "ROAS")),
            atc_rate=_safe_float(_get_col(row, "ATC %")),
            conversion_rate=_safe_float(_get_col(row, "Conv. %")),
            is_active_pdp=matched
        ))

    log.info(f"Found {len(stats)} URLs in sheet ({sum(1 for s in stats if s.is_active_pdp)} matched our PDPs)")
    return stats


# ── Main ────────────────────────────────────────────────────────────────────────

def pull_sheets_data(product_name: str, urls: List[str]) -> SheetsData:
    """
    Main entry point.
    Sets the product dropdown, reads top ads + URL performance.
    Returns SheetsData passed to the analyser.
    """
    log.info(f"Connecting to Umbrella Sheet → {product_name}")

    try:
        client = _get_client()
        sheet  = client.open_by_key(SHEETS_CFG["sheet_id"])
        log.info(f"Sheet opened: {sheet.title}")
    except google.auth.exceptions.DefaultCredentialsError:
        log.error(
            "Google credentials not found.\n"
            "Run: gcloud auth application-default login"
        )
        raise
    except gspread.SpreadsheetNotFound:
        log.error(
            "Sheet not found. Check google_sheets.sheet_id in config.yaml\n"
            "and make sure the sheet is shared with purva.gandhi@mosaicwellness.in"
        )
        raise

    top_ads   = _pull_top_ads(sheet, product_name)
    url_stats = _pull_url_stats(sheet, product_name, urls)

    return SheetsData(
        product_name=product_name,
        top_ads=top_ads,
        url_stats=url_stats,
        pulled_at=datetime.utcnow().isoformat()
    )
