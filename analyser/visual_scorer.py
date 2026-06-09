import anthropic
from analyser.models import VisualDesignScore, SubScore, SectionFlowScore, SectionFlowIssue
from analyser.claude_client import call_claude_vision, call_claude
from ingester.models import IngestedContext
from scraper.models import PDPTextData
from utils.config_loader import load_config
from utils.logger import get_logger

config = load_config()

log = get_logger("visual_scorer")

SYSTEM = """You are a creative director and visual strategist auditing a health/wellness PDP.
You are looking at screenshots of carousel slides and banners.
Evaluate the visual design across 6 dimensions against the persona and narrative context provided.
Be specific — describe exactly what you see and what's missing.
Return ONLY valid JSON — no explanation, no markdown.

Schema:
{
  "overall": <float 0-10>,
  "human_presence": {
    "score": <float 0-10>,
    "observation": "<is there a human figure? does it match the persona age/lifestyle/emotion?>",
    "suggestion": "<specific fix>"
  },
  "proof_prominence": {
    "score": <float 0-10>,
    "observation": "<are clinical numbers, % stats, certifications visually prominent or buried?>",
    "suggestion": "<specific fix — size, placement, colour>"
  },
  "ingredient_imagery": {
    "score": <float 0-10>,
    "observation": "<are key ingredients shown visually? do they look premium?>",
    "suggestion": "<specific fix>"
  },
  "before_after": {
    "score": <float 0-10>,
    "observation": "<is there a before/after? is the transformation clear and believable?>",
    "suggestion": "<specific fix>"
  },
  "lifestyle_shots": {
    "score": <float 0-10>,
    "observation": "<do lifestyle images show the persona's aspirational life in real context?>",
    "suggestion": "<specific fix>"
  },
  "visual_hierarchy_brand": {
    "score": <float 0-10>,
    "observation": "<is the most important element the first thing the eye goes to? brand consistency?>",
    "suggestion": "<specific fix>"
  },
  "flagged_issues": ["<specific visual issue 1>", "<specific visual issue 2>"]
}"""


def score_visual_design(
    client: anthropic.Anthropic,
    pdp: PDPTextData,
    context: IngestedContext,
    configured_narrative: str = "",
    configured_persona: str = ""
) -> VisualDesignScore:
    log.info(f"Scoring visual design for {pdp.url}")

    # ── Collect image paths ────────────────────────────────────────────────────
    # Zeus mode: use structured ZeusImage list (labelled, full-res CDN images)
    # Playwright mode: use carousel / banner screenshot paths
    image_paths = []
    image_labels = []  # parallel list of semantic labels for the prompt

    if pdp.zeus_sourced and pdp.zeus_images:
        # Build a curated set: hero (up to 9) + key content sections (up to 8)
        zeus_cfg = config.get("zeus", {})
        max_hero = zeus_cfg.get("max_hero_images", 9)
        max_other = zeus_cfg.get("max_banner_images", 3) + zeus_cfg.get("max_carousel_images", 4)

        hero_imgs   = [z for z in pdp.zeus_images if z.position == "hero" and z.local_path][:max_hero]
        content_imgs = [z for z in pdp.zeus_images if z.position != "hero" and z.local_path][:max_other]

        for z in hero_imgs + content_imgs:
            image_paths.append(z.local_path)
            image_labels.append(f"{z.position} ({z.label or z.widget_id})")
    else:
        for slide in pdp.carousels:
            if slide.screenshot_path:
                image_paths.append(slide.screenshot_path)
                image_labels.append(f"carousel slide {slide.index}")
        for banner in pdp.banners:
            if banner.screenshot_path:
                image_paths.append(banner.screenshot_path)
                image_labels.append(f"banner ({banner.location})")

    if not image_paths:
        log.warning("No screenshots found — falling back to text-only visual analysis")
        return _text_only_fallback(client, pdp, context)

    # Cap at 12 images to keep API cost reasonable
    image_paths  = image_paths[:12]
    image_labels = image_labels[:12]
    log.info(f"Sending {len(image_paths)} images to Claude Vision "
             f"({'Zeus CDN' if pdp.zeus_sourced else 'Playwright screenshots'})")

    image_manifest = "\n".join(
        f"  Image {i+1}: {lbl}" for i, lbl in enumerate(image_labels)
    )

    prompt = f"""PERSONA: {context.persona.name}
PERSONA TOP CONCERNS: {', '.join(context.persona.top_concerns)}
PERSONA DESCRIPTION: {context.persona.description}
NARRATIVE EMOTIONAL ARC: {context.narrative.emotional_arc}
KEY PRODUCT CLAIMS: {', '.join(context.product_brief.primary_benefits)}
KEY INGREDIENTS: {', '.join(context.product_brief.key_ingredients)}

You are viewing {len(image_paths)} images from this PDP in display order:
{image_manifest}

Evaluate all 6 visual dimensions. Be precise about what you see in each image."""

    data = call_claude_vision(client, SYSTEM, prompt, image_paths)

    # Section flow analysis — runs on text, independent of images
    flow = score_section_flow(
        client, pdp, context, configured_narrative, configured_persona
    )

    return VisualDesignScore(
        overall=data["overall"],
        human_presence=SubScore(name="Human Presence", **data["human_presence"]),
        proof_prominence=SubScore(name="Proof Prominence", **data["proof_prominence"]),
        ingredient_imagery=SubScore(name="Ingredient Imagery", **data["ingredient_imagery"]),
        before_after=SubScore(name="Before / After", **data["before_after"]),
        lifestyle_shots=SubScore(name="Lifestyle Shots", **data["lifestyle_shots"]),
        visual_hierarchy_brand=SubScore(name="Visual Hierarchy & Brand", **data["visual_hierarchy_brand"]),
        flagged_issues=data.get("flagged_issues", []),
        section_flow=flow,
    )


def _text_only_fallback(
    client: anthropic.Anthropic,
    pdp: PDPTextData,
    context: IngestedContext
) -> VisualDesignScore:
    """Used when screenshots are unavailable."""
    carousel_copy = "\n".join([f"Slide {s.index}: {s.copy}" for s in pdp.carousels if s.copy])
    banner_copy   = "\n".join([f"Banner ({b.location}): {b.copy}" for b in pdp.banners if b.copy])

    # No visual content at all — return a baseline placeholder rather than calling Claude with empty input
    if not carousel_copy and not banner_copy:
        log.warning("No carousel or banner text either — returning placeholder visual score")
        no_data_note = "⚠️ Visual score unavailable — no screenshots captured and no carousel/banner text found. Re-run with screenshots enabled or check CSS selectors."
        placeholder = SubScore(
            name="",
            score=5.0,
            observation="Could not be evaluated — no visual content scraped.",
            suggestion="Fix screenshot capture or CSS selectors and re-run."
        )
        return VisualDesignScore(
            overall=5.0,
            human_presence=SubScore(name="Human Presence", score=5.0,
                observation="No data", suggestion="Re-run with screenshots"),
            proof_prominence=SubScore(name="Proof Prominence", score=5.0,
                observation="No data", suggestion="Re-run with screenshots"),
            ingredient_imagery=SubScore(name="Ingredient Imagery", score=5.0,
                observation="No data", suggestion="Re-run with screenshots"),
            before_after=SubScore(name="Before / After", score=5.0,
                observation="No data", suggestion="Re-run with screenshots"),
            lifestyle_shots=SubScore(name="Lifestyle Shots", score=5.0,
                observation="No data", suggestion="Re-run with screenshots"),
            visual_hierarchy_brand=SubScore(name="Visual Hierarchy & Brand", score=5.0,
                observation="No data", suggestion="Re-run with screenshots"),
            flagged_issues=[no_data_note]
        )

    prompt = f"""PERSONA: {context.persona.name}
PERSONA DESCRIPTION: {context.persona.description}
KEY INGREDIENTS: {', '.join(context.product_brief.key_ingredients)}

No screenshots available. Evaluate based on copy text only.
Note: scores will be limited without visual context. Do not give 0 — use 4-6 range when uncertain.

CAROUSEL TEXT: {carousel_copy or 'NONE'}
BANNER TEXT: {banner_copy or 'NONE'}"""

    data = call_claude(client, SYSTEM, prompt)

    return VisualDesignScore(
        overall=data["overall"],
        human_presence=SubScore(name="Human Presence", **data["human_presence"]),
        proof_prominence=SubScore(name="Proof Prominence", **data["proof_prominence"]),
        ingredient_imagery=SubScore(name="Ingredient Imagery", **data["ingredient_imagery"]),
        before_after=SubScore(name="Before / After", **data["before_after"]),
        lifestyle_shots=SubScore(name="Lifestyle Shots", **data["lifestyle_shots"]),
        visual_hierarchy_brand=SubScore(name="Visual Hierarchy & Brand", **data["visual_hierarchy_brand"]),
        flagged_issues=data.get("flagged_issues", []) + ["⚠️ Scored from text only — no screenshots available"]
    )


# ── Section Flow Analysis ──────────────────────────────────────────────────────

SECTION_FLOW_SYSTEM = """You are a CRO specialist analysing the section order of a health/wellness PDP.

You will receive:
- The configured narrative and target persona for this URL
- The actual section headings in current page order (top → bottom)

Your job: score how well the section sequence matches the optimal conversion narrative arc,
identify what's in the wrong position, what's missing, and what's redundant.

The optimal arc for a health product PDP:
  1. Hook / Hero — product name + core claim
  2. Problem — make the persona feel seen (pain point)
  3. Solution — introduce the product as the answer
  4. How It Works — mechanism / science (builds trust)
  5. Ingredients / What's Inside — proof of quality
  6. Social Proof — before/after, reviews, real results
  7. Clinical / Certifications — third-party validation
  8. How to Use — reduce friction
  9. Comparison / Why Us — handle objections
  10. Trust Signals — certifications, press, awards
  11. FAQ — handle remaining objections
  12. Final CTA — close

Return ONLY valid JSON — no markdown.

Schema:
{
  "score": <float 0-10>,
  "observation": "<2-3 sentences: what's working and what's broken in the current flow>",
  "current_order": ["Section Heading 1", "Section Heading 2", ...],
  "missing_sections": ["section that should exist but is absent"],
  "out_of_order": [
    {
      "section": "<heading>",
      "current_position": <int>,
      "recommended_position": <int>,
      "reason": "<why it should move — reference the persona and narrative>"
    }
  ],
  "redundant_sections": ["heading that duplicates another section's content"],
  "suggestion": "<single highest-priority reorder action — specific, actionable, persona-referenced>"
}"""


def score_section_flow(
    client: anthropic.Anthropic,
    pdp: PDPTextData,
    context: IngestedContext,
    configured_narrative: str = "",
    configured_persona: str = ""
) -> SectionFlowScore:
    """
    Analyse the order of section headings on the PDP against the optimal
    narrative arc for the configured persona and narrative.
    Returns a SectionFlowScore with reorder recommendations.
    """
    if not pdp.subheads:
        log.warning(f"No section headings found for {pdp.url} — skipping section flow analysis")
        return SectionFlowScore(
            score=5.0,
            observation="No section headings extracted — cannot analyse page flow.",
            suggestion="Ensure the scraper captures H2/H3 section headings from the PDP."
        )

    numbered = "\n".join(f"  {i+1}. {h}" for i, h in enumerate(pdp.subheads))

    prompt = f"""URL: {pdp.url}
CONFIGURED NARRATIVE: {configured_narrative or context.narrative.core_story}
TARGET PERSONA: {configured_persona or context.persona.name}
PERSONA TOP CONCERNS: {', '.join(context.persona.top_concerns[:3])}
PERSONA CORE MOTIVATION: {context.narrative.emotional_arc}

SECTION HEADINGS IN CURRENT PAGE ORDER (top → bottom):
{numbered}

Analyse whether this section sequence follows the optimal narrative arc for this persona.
Be specific — reference actual section headings from the list above."""

    log.info(f"Section flow: analysing {len(pdp.subheads)} headings for {pdp.url}")
    data = call_claude(client, SECTION_FLOW_SYSTEM, prompt)

    out_of_order = [
        SectionFlowIssue(
            section=item.get("section", ""),
            current_position=item.get("current_position", 0),
            recommended_position=item.get("recommended_position", 0),
            reason=item.get("reason", "")
        )
        for item in data.get("out_of_order", [])
        if item.get("section")
    ]

    flow = SectionFlowScore(
        score=data.get("score", 5.0),
        current_order=data.get("current_order", pdp.subheads),
        missing_sections=data.get("missing_sections", []),
        out_of_order=out_of_order,
        redundant_sections=data.get("redundant_sections", []),
        observation=data.get("observation", ""),
        suggestion=data.get("suggestion", "")
    )

    log.info(
        f"Section flow scored {flow.score}/10 — "
        f"{len(out_of_order)} out-of-order, "
        f"{len(flow.missing_sections)} missing"
    )
    return flow
