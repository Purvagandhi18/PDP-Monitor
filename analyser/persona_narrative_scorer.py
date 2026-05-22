import anthropic
from analyser.models import PersonaNarrativeScore, PersonaMatrixRow, SubScore
from analyser.claude_client import call_claude
from ingester.models import IngestedContext
from scraper.models import PDPTextData
from utils.config_loader import load_config
from utils.logger import get_logger

log = get_logger("persona_narrative_scorer")
config = load_config()

SYSTEM = """You are a brand strategist and conversion copywriter auditing a health/wellness PDP.
You will be given:
  - The CONFIGURED narrative this PDP is meant to target
  - The list of all product personas
  - The full PDP copy

Your job has TWO parts:

PART 1 — Score how well the page executes the configured narrative (5 dimensions).
PART 2 — For EACH persona, identify what the current PDP does RIGHT and what is MISSING
         to serve that persona, given the configured narrative.

Be specific and quote actual copy where relevant.
Return ONLY valid JSON — no explanation, no markdown.

Schema:
{
  "overall": <float 0-10>,
  "hero_banner": {
    "score": <float 0-10>,
    "observation": "<does headline/hero address the configured narrative and the target persona's #1 concern?>",
    "suggestion": "<specific rewrite>"
  },
  "carousel_flow": {
    "score": <float 0-10>,
    "observation": "<do slides follow the narrative arc: hook → problem → solution → proof? quote what's present/missing>",
    "suggestion": "<specific slide-level fix>"
  },
  "banner_alignment": {
    "score": <float 0-10>,
    "observation": "<does banner copy reinforce the configured narrative or feel generic?>",
    "suggestion": "<specific fix>"
  },
  "page_narrative_arc": {
    "score": <float 0-10>,
    "observation": "<does the full page tell a coherent story aligned to the narrative?>",
    "suggestion": "<specific fix>"
  },
  "cta_language": {
    "score": <float 0-10>,
    "observation": "<does the CTA match the emotional state the narrative should leave the persona in?>",
    "suggestion": "<specific rewrite>"
  },
  "flagged_issues": ["<critical gap 1>", "<critical gap 2>"],
  "persona_matrix": [
    {
      "persona": "<persona name>",
      "doing_right": ["<specific copy or element that works for this persona>", ...],
      "missing": ["<what's absent that this persona needs based on the narrative>", ...]
    }
  ]
}"""


def score_persona_narrative(
    client: anthropic.Anthropic,
    pdp: PDPTextData,
    context: IngestedContext,
    configured_narrative: str = "",
    configured_persona: str = ""
) -> PersonaNarrativeScore:
    log.info(f"Scoring persona × narrative for {pdp.url}")

    # Get all personas from config
    product_cfg = _get_product_cfg(pdp.url)
    all_personas = product_cfg.get("personas", [
        context.persona.name
    ]) if product_cfg else [context.persona.name]

    carousel_summary = ""
    if pdp.carousels:
        slides = "\n".join([f"  Slide {s.index}: {s.copy[:200]}" for s in pdp.carousels])
        carousel_summary = f"\nCAROUSEL SLIDES:\n{slides}"

    banner_summary = ""
    if pdp.banners:
        banners = "\n".join([f"  Banner ({b.location}): {b.copy[:200]}" for b in pdp.banners])
        banner_summary = f"\nBANNERS:\n{banners}"

    persona_desc = "\n".join([
        f"  - {p}" for p in all_personas
    ])

    prompt = f"""CONFIGURED NARRATIVE FOR THIS URL: {configured_narrative or context.narrative.core_story}
TARGET PERSONA: {configured_persona or context.persona.name}

ALL PRODUCT PERSONAS (generate persona_matrix for each):
{persona_desc}

NARRATIVE DETAILS:
  Core story: {context.narrative.core_story}
  Emotional arc: {context.narrative.emotional_arc}
  Pillars: {', '.join(context.narrative.pillars)}

PRIMARY PERSONA PROFILE ({context.persona.name}):
  Top concerns: {', '.join(context.persona.top_concerns)}
  Motivations: {', '.join(context.persona.motivations)}
  Objections: {', '.join(context.persona.objections)}
  Language cues: {', '.join(context.persona.language_cues)}

BRAND VOICE:
  Dos: {', '.join(context.brand_voice.dos[:5])}
  Don'ts: {', '.join(context.brand_voice.donts[:5])}

--- PDP CONTENT ---
HEADLINE: {pdp.headline or 'NOT FOUND'}
SUBHEADS: {' | '.join(pdp.subheads[:12]) or 'NONE'}
BODY COPY (first 2500 chars): {' '.join(pdp.body_copy)[:2500]}
CTAs: {' | '.join(pdp.cta_texts) or 'NONE'}
{carousel_summary}
{banner_summary}

Score all 5 dimensions for the configured narrative execution.
Generate persona_matrix for ALL {len(all_personas)} personas listed above.
Be sharp — quote exact copy lines that work or fail."""

    data = call_claude(client, SYSTEM, prompt)

    # Parse persona matrix
    raw_matrix = data.get("persona_matrix", [])
    persona_matrix = [
        PersonaMatrixRow(
            persona=row.get("persona", ""),
            doing_right=row.get("doing_right", []),
            missing=row.get("missing", [])
        )
        for row in raw_matrix if row.get("persona")
    ]

    return PersonaNarrativeScore(
        overall=data["overall"],
        configured_narrative=configured_narrative,
        configured_persona=configured_persona,
        hero_banner=SubScore(name="Hero / Banner", **data["hero_banner"]),
        carousel_flow=SubScore(name="Carousel Flow", **data["carousel_flow"]),
        banner_alignment=SubScore(name="Banner Alignment", **data["banner_alignment"]),
        page_narrative_arc=SubScore(name="Page Narrative Arc", **data["page_narrative_arc"]),
        cta_language=SubScore(name="CTA Language", **data["cta_language"]),
        flagged_issues=data.get("flagged_issues", []),
        persona_matrix=persona_matrix
    )


def _get_product_cfg(url: str) -> dict:
    """Find which product config this URL belongs to."""
    for product in config.get("products", []):
        for u in product.get("urls", []):
            u_str = u["url"] if isinstance(u, dict) else u
            if u_str == url:
                return product
    return {}
