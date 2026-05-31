from pydantic import BaseModel
from typing import List, Optional


class SubScore(BaseModel):
    name: str
    score: float          # 0-10
    observation: str      # what was found
    suggestion: str       # what to fix


class ReviewsScore(BaseModel):
    overall: float
    freshness: SubScore
    rating_distribution: SubScore
    theme_alignment: SubScore
    negative_handling: SubScore
    flagged_issues: List[str] = []


class PersonaMatrixRow(BaseModel):
    persona: str              # e.g. "Tejas"
    doing_right: List[str]   # what the PDP does well for this persona
    missing: List[str]        # what's absent / gaps for this persona


class PersonaNarrativeScore(BaseModel):
    overall: float
    configured_narrative: str = ""   # from config.yaml
    configured_persona: str = ""
    hero_banner: SubScore
    carousel_flow: SubScore
    banner_alignment: SubScore
    page_narrative_arc: SubScore
    cta_language: SubScore
    flagged_issues: List[str] = []
    persona_matrix: List[PersonaMatrixRow] = []  # per-persona doing right vs missing


class ClaimFlag(BaseModel):
    text: str            # the exact claim found on the PDP
    status: str          # "ok" | "flagged" | "warning"
    reason: str          # why it's flagged (or confirmed)


class TextInsight(BaseModel):
    category: str    # e.g. "Forbidden Word", "Missing Hook", "Narrative Gap"
    severity: str    # "critical" | "warning" | "ok"
    finding: str     # short headline
    detail: str      # explanation


class CopyHealthScore(BaseModel):
    overall: float
    # Sub-scores (map to 3 Hygiene Check sub-tabs)
    spell_grammar: SubScore
    brand_guidelines: SubScore
    claims_alignment: SubScore
    # Structured flag lists (shown in each sub-tab)
    claims_flags: List[ClaimFlag] = []  # Claims sub-tab
    brand_flags: List[str] = []         # Brand Guidelines sub-tab
    flagged_errors: List[str] = []      # Spell Check sub-tab
    # Text Layer insights
    text_insights: List[TextInsight] = []


class VisualDesignScore(BaseModel):
    overall: float
    human_presence: SubScore
    proof_prominence: SubScore
    ingredient_imagery: SubScore
    before_after: SubScore
    lifestyle_shots: SubScore
    visual_hierarchy_brand: SubScore
    flagged_issues: List[str] = []


class AdGap(BaseModel):
    angle: str              # e.g. "Daily Energy"
    conv_rate: str          # e.g. "3.9%" — why it matters
    what_is_missing: str    # specific copy/element not on PDP
    what_to_add: str        # exact suggestion: copy line, placement, format
    where_to_add: str       # e.g. "Carousel slide 2", "Hero headline", "Banner"


class AdAlignmentScore(BaseModel):
    overall: float
    top_converting_angles: List[str] = []   # angles from ads with high Conv. %
    angles_present_on_pdp: List[str] = []   # which ones are on the PDP
    gaps: List[AdGap] = []                  # rich gap objects with suggestions
    atc_drop_off_addressed: SubScore        # is ATC drop-off addressed in copy?
    flagged_gaps: List[str] = []            # kept for legacy/RCA use


class RCAItem(BaseModel):
    culprit_score: str        # e.g. "Visual Design → Proof Prominence"
    score_value: float
    evidence: str             # exact copy/visual/data point
    why_it_matters: str       # impact on persona / conversion
    fix: str                  # specific, actionable recommendation


class PDPAnalysisResult(BaseModel):
    """Complete analysis result for one PDP URL."""
    url: str
    product_name: str
    analysed_at: str

    # Individual scores
    reviews: ReviewsScore
    persona_narrative: PersonaNarrativeScore
    copy_health: CopyHealthScore
    visual_design: VisualDesignScore
    ad_alignment: AdAlignmentScore

    # Overall
    overall_score: float
    status: str              # "healthy" | "attention" | "critical"
    rca: List[RCAItem] = []  # populated if overall < 8

    # Regression tracking (populated by RegressionAgent after scoring)
    delta: Optional[float] = None           # current - previous overall (None = first run)
    delta_scores: dict = {}                 # per-dimension deltas {"reviews": -0.3, ...}
    regression_flag: bool = False           # True if overall dropped > 0.5 points
