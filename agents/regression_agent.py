"""
Regression Agent — compares the current run's scores against the previous run.

Flags any PDP where overall score dropped by more than THRESHOLD points.
Also flags per-dimension drops so the team knows exactly which tab regressed.

Annotates each PDPAnalysisResult in-place with:
  - result.delta         : float  (current - previous overall, None if no history)
  - result.delta_scores  : dict   (per-dimension deltas)
  - result.regression_flag: bool  (True if overall drop > threshold)

Returns a list of RegressionAlert objects for the report and logs.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Dict

from analyser.models import PDPAnalysisResult
from utils.logger import get_logger

log = get_logger("regression_agent")

REGRESSION_THRESHOLD = 0.5   # drop > 0.5 points = regression
IMPROVEMENT_THRESHOLD = 0.5  # gain > 0.5 points = notable improvement

DIM_LABELS = {
    "reviews":           "Reviews & Ratings",
    "persona_narrative": "Narrative × Persona",
    "copy_health":       "Copy Health",
    "visual_design":     "Visual Design",
    "ad_alignment":      "Ad Alignment",
}


@dataclass
class RegressionAlert:
    url: str
    product_name: str
    current_score: float
    previous_score: float
    delta: float                          # negative = regression
    dim_deltas: Dict[str, float] = field(default_factory=dict)  # per-dimension
    likely_cause: str = ""               # which dimension drove the drop


@dataclass
class ImprovementNote:
    url: str
    current_score: float
    previous_score: float
    delta: float


def detect_regression(
    results: List[PDPAnalysisResult],
    previous_run: Optional[dict]
) -> tuple:
    """
    Compare current results against the previous run snapshot.

    Annotates each PDPAnalysisResult with delta + regression_flag.
    Returns (regression_alerts, improvement_notes).
    """
    alerts: List[RegressionAlert] = []
    improvements: List[ImprovementNote] = []

    if not previous_run:
        log.info("No previous run — skipping regression detection")
        for r in results:
            r.delta = None
            r.delta_scores = {}
            r.regression_flag = False
        return alerts, improvements

    # Build lookup: url → previous PDP data
    prev_by_url = {p["url"]: p for p in previous_run.get("pdps", [])}
    prev_date = previous_run.get("run_date", "unknown")

    log.info(f"Comparing against previous run from {prev_date}")

    for result in results:
        prev = prev_by_url.get(result.url)

        if not prev:
            log.info(f"  No previous data for {result.url} — first run for this URL")
            result.delta = None
            result.delta_scores = {}
            result.regression_flag = False
            continue

        prev_overall = prev["overall_score"]
        current_overall = result.overall_score
        delta = round(current_overall - prev_overall, 2)

        # Per-dimension deltas
        prev_scores = prev.get("scores", {})
        dim_deltas = {
            "reviews":           round(result.reviews.overall           - prev_scores.get("reviews", result.reviews.overall), 2),
            "persona_narrative": round(result.persona_narrative.overall  - prev_scores.get("persona_narrative", result.persona_narrative.overall), 2),
            "copy_health":       round(result.copy_health.overall        - prev_scores.get("copy_health", result.copy_health.overall), 2),
            "visual_design":     round(result.visual_design.overall      - prev_scores.get("visual_design", result.visual_design.overall), 2),
            "ad_alignment":      round(result.ad_alignment.overall       - prev_scores.get("ad_alignment", result.ad_alignment.overall), 2),
        }

        # Annotate the result
        result.delta = delta
        result.delta_scores = dim_deltas
        result.regression_flag = delta < -REGRESSION_THRESHOLD

        if result.regression_flag:
            # Identify the dimension that dropped the most
            worst_dim = min(dim_deltas, key=dim_deltas.get)
            worst_drop = dim_deltas[worst_dim]
            likely_cause = (
                f"{DIM_LABELS[worst_dim]} dropped {worst_drop:+.1f} points "
                f"(from {prev_scores.get(worst_dim, '?'):.1f} → "
                f"{getattr(result, _dim_to_attr(worst_dim)).overall:.1f})"
            )

            alert = RegressionAlert(
                url=result.url,
                product_name=result.product_name,
                current_score=current_overall,
                previous_score=prev_overall,
                delta=delta,
                dim_deltas=dim_deltas,
                likely_cause=likely_cause,
            )
            alerts.append(alert)
            log.warning(
                f"  🔴 REGRESSION: {result.url}\n"
                f"     {prev_overall:.1f} → {current_overall:.1f} ({delta:+.2f})\n"
                f"     Likely cause: {likely_cause}"
            )

        elif delta > IMPROVEMENT_THRESHOLD:
            improvements.append(ImprovementNote(
                url=result.url,
                current_score=current_overall,
                previous_score=prev_overall,
                delta=delta,
            ))
            log.info(
                f"  ✅ IMPROVEMENT: {result.url}  "
                f"{prev_overall:.1f} → {current_overall:.1f} ({delta:+.2f})"
            )
        else:
            log.info(
                f"  ↔  STABLE: {result.url}  "
                f"{prev_overall:.1f} → {current_overall:.1f} ({delta:+.2f})"
            )

    return alerts, improvements


def _dim_to_attr(dim_key: str) -> str:
    """Map storage key to PDPAnalysisResult attribute name."""
    return {
        "reviews":           "reviews",
        "persona_narrative": "persona_narrative",
        "copy_health":       "copy_health",
        "visual_design":     "visual_design",
        "ad_alignment":      "ad_alignment",
    }.get(dim_key, dim_key)
