from datetime import datetime
from typing import List
from analyser.models import PDPAnalysisResult
from analyser.claude_client import get_client
from analyser.reviews_scorer import score_reviews
from analyser.persona_narrative_scorer import score_persona_narrative
from analyser.copy_health_scorer import score_copy_health
from analyser.visual_scorer import score_visual_design
from analyser.ad_alignment_scorer import score_ad_alignment
from analyser.rca import generate_rca
from ingester.models import IngestedContext
from scraper.models import PDPTextData
from scraper.sheets_models import SheetsData
from utils.config_loader import load_config
from utils.logger import get_logger

log = get_logger("engine")
config = load_config()
WEIGHTS = config["scoring"]["weights"]
BANDS   = config["scoring"]["bands"]


def _compute_overall(scores: dict) -> float:
    """Weighted average of all 5 scores."""
    return round(
        scores["reviews"]           * WEIGHTS["reviews"] +
        scores["persona_narrative"] * WEIGHTS["persona_narrative"] +
        scores["copy_health"]       * WEIGHTS["copy_health"] +
        scores["visual_design"]     * WEIGHTS["visual_design"] +
        scores["ad_alignment"]      * WEIGHTS["ad_pdp_alignment"],
        2
    )


def _get_status(score: float) -> str:
    if score >= BANDS["healthy"]:
        return "healthy"
    elif score >= BANDS["attention"]:
        return "attention"
    return "critical"


def analyse_pdp(
    pdp: PDPTextData,
    context: IngestedContext,
    sheets: SheetsData,
    url_cfg: dict = None
) -> PDPAnalysisResult:
    """
    Run all 5 scorers on a single PDP.
    url_cfg: optional {url, narrative, persona} from config.yaml
    """
    log.info(f"{'='*60}")
    log.info(f"Analysing: {pdp.url}")
    log.info(f"{'='*60}")

    client = get_client()
    cfg_narrative = (url_cfg or {}).get("narrative") or ""
    cfg_persona   = (url_cfg or {}).get("persona")   or ""

    # ── Run all scorers ────────────────────────────────────────
    log.info("1/5 Reviews...")
    reviews = score_reviews(client, pdp, context)
    log.info(f"    → {reviews.overall:.1f}/10")

    log.info("2/5 Persona × Narrative...")
    pn = score_persona_narrative(client, pdp, context,
                                  configured_narrative=cfg_narrative,
                                  configured_persona=cfg_persona)
    log.info(f"    → {pn.overall:.1f}/10")

    log.info("3/5 Copy Health...")
    copy = score_copy_health(client, pdp, context,
                              configured_narrative=cfg_narrative)
    log.info(f"    → {copy.overall:.1f}/10")

    log.info("4/5 Visual Design...")
    visual = score_visual_design(client, pdp, context,
                                  configured_narrative=cfg_narrative,
                                  configured_persona=cfg_persona)
    log.info(f"    → {visual.overall:.1f}/10")

    log.info("5/5 Ad Alignment...")
    ads = score_ad_alignment(client, pdp, context, sheets)
    log.info(f"    → {ads.overall:.1f}/10")

    # ── Overall score ──────────────────────────────────────────
    overall = _compute_overall({
        "reviews":           reviews.overall,
        "persona_narrative": pn.overall,
        "copy_health":       copy.overall,
        "visual_design":     visual.overall,
        "ad_alignment":      ads.overall
    })
    status = _get_status(overall)

    log.info(f"OVERALL: {overall:.1f}/10 → {status.upper()}")

    # ── Build result ───────────────────────────────────────────
    result = PDPAnalysisResult(
        url=pdp.url,
        product_name=context.product_name,
        analysed_at=datetime.utcnow().isoformat(),
        reviews=reviews,
        persona_narrative=pn,
        copy_health=copy,
        visual_design=visual,
        ad_alignment=ads,
        overall_score=overall,
        status=status
    )

    # ── RCA if below target ────────────────────────────────────
    if status != "healthy":
        log.info("Generating RCA...")
        result.rca = generate_rca(client, result)
        log.info(f"RCA: {len(result.rca)} items")

    return result


def analyse_all(
    pdp_list: List[PDPTextData],
    context: IngestedContext,
    sheets: SheetsData,
    url_meta: dict = None
) -> List[PDPAnalysisResult]:
    """Analyse all PDPs for a product. Retries once on transient API errors."""
    import time
    results = []
    for pdp in pdp_list:
        cfg = (url_meta or {}).get(pdp.url)
        last_error = None
        for attempt in range(1, 3):  # up to 2 attempts
            try:
                results.append(analyse_pdp(pdp, context, sheets, url_cfg=cfg))
                last_error = None
                break
            except Exception as e:
                last_error = e
                is_transient = any(
                    kw in str(e).lower()
                    for kw in ("timeout", "connection", "timed out", "network", "overloaded")
                )
                if is_transient and attempt < 2:
                    log.warning(f"Transient error on attempt {attempt} for {pdp.url}: {e} — retrying in 15s")
                    time.sleep(15)
                else:
                    break
        if last_error:
            log.error(f"Analysis failed for {pdp.url} after {attempt} attempt(s): {last_error}")
    return results
