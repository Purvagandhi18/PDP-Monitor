import anthropic
from typing import List
from analyser.models import (
    PDPAnalysisResult, ReviewsScore, PersonaNarrativeScore,
    CopyHealthScore, VisualDesignScore, AdAlignmentScore, RCAItem
)
from analyser.claude_client import call_claude
from utils.config_loader import load_config
from utils.logger import get_logger

log = get_logger("rca")
config = load_config()


SYSTEM = """You are a conversion optimisation expert.
Given a PDP audit with scores below the target of 8.0, identify the root causes.
For each culprit, provide a sharp, actionable RCA.
Return ONLY valid JSON — no explanation, no markdown.

Schema:
{
  "rca_items": [
    {
      "culprit_score": "<Score Name → Sub-metric Name>",
      "score_value": <float>,
      "evidence": "<exact copy line / visual observation / data point that proves the issue>",
      "why_it_matters": "<why this hurts conversion for this specific persona>",
      "fix": "<specific, actionable fix — not vague advice>"
    }
  ]
}"""


def _collect_culprits(
    reviews: ReviewsScore,
    pn: PersonaNarrativeScore,
    copy: CopyHealthScore,
    visual: VisualDesignScore,
    ads: AdAlignmentScore,
    threshold: float = 7.0
) -> List[dict]:
    """Collect all sub-scores below threshold as potential culprits."""
    culprits = []

    def check(score_name, sub):
        if sub.score < threshold:
            culprits.append({
                "score": f"{score_name} → {sub.name}",
                "value": sub.score,
                "observation": sub.observation,
                "suggestion": sub.suggestion
            })

    # Reviews
    check("Reviews", reviews.freshness)
    check("Reviews", reviews.theme_alignment)
    check("Reviews", reviews.negative_handling)

    # Persona × Narrative
    check("Persona × Narrative", pn.hero_banner)
    check("Persona × Narrative", pn.carousel_flow)
    check("Persona × Narrative", pn.banner_alignment)
    check("Persona × Narrative", pn.page_narrative_arc)
    check("Persona × Narrative", pn.cta_language)

    # Copy Health
    check("Copy Health", copy.spell_grammar)
    check("Copy Health", copy.brand_guidelines)
    check("Copy Health", copy.claims_alignment)

    # Visual Design
    check("Visual Design", visual.human_presence)
    check("Visual Design", visual.proof_prominence)
    check("Visual Design", visual.ingredient_imagery)
    check("Visual Design", visual.before_after)
    check("Visual Design", visual.lifestyle_shots)
    check("Visual Design", visual.visual_hierarchy_brand)

    # Ad Alignment
    check("Ad Alignment", ads.atc_drop_off_addressed)

    # Sort by lowest score first (biggest problems first)
    return sorted(culprits, key=lambda c: c["value"])


def generate_rca(
    client: anthropic.Anthropic,
    result: PDPAnalysisResult
) -> List[RCAItem]:
    """Generate RCA items for a PDP that scored below 8.0."""
    threshold = config["scoring"]["rca_threshold"]

    if result.overall_score >= threshold:
        log.info(f"Score {result.overall_score:.1f} ≥ {threshold} — no RCA needed")
        return []

    log.info(f"Score {result.overall_score:.1f} < {threshold} — generating RCA")

    culprits = _collect_culprits(
        result.reviews,
        result.persona_narrative,
        result.copy_health,
        result.visual_design,
        result.ad_alignment
    )

    if not culprits:
        return []

    # Format culprits for Claude
    culprits_text = "\n".join([
        f"  [{c['score']}] Score: {c['value']:.1f}/10\n"
        f"  Observation: {c['observation']}\n"
        f"  Suggestion: {c['suggestion']}\n"
        for c in culprits[:6]   # top 6 culprits max
    ])

    prompt = f"""PRODUCT: {result.product_name}
URL: {result.url}
OVERALL SCORE: {result.overall_score:.1f}/10 (target: {threshold})

CULPRIT SUB-SCORES (lowest first):
{culprits_text}

MISSING AD ANGLES: {', '.join([g.angle for g in result.ad_alignment.gaps]) or 'None identified'}
COPY ERRORS: {', '.join(result.copy_health.flagged_errors[:5]) or 'None'}

Generate a sharp RCA for each culprit.
Be specific — use evidence from the observations above.
Prioritise by business impact."""

    data = call_claude(client, SYSTEM, prompt)

    rca_items = [
        RCAItem(
            culprit_score=item["culprit_score"],
            score_value=item["score_value"],
            evidence=item["evidence"],
            why_it_matters=item["why_it_matters"],
            fix=item["fix"]
        )
        for item in data.get("rca_items", [])
    ]

    log.info(f"Generated {len(rca_items)} RCA items")
    return rca_items
