import anthropic
from analyser.models import VisualDesignScore, SubScore
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
    context: IngestedContext
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

    return VisualDesignScore(
        overall=data["overall"],
        human_presence=SubScore(name="Human Presence", **data["human_presence"]),
        proof_prominence=SubScore(name="Proof Prominence", **data["proof_prominence"]),
        ingredient_imagery=SubScore(name="Ingredient Imagery", **data["ingredient_imagery"]),
        before_after=SubScore(name="Before / After", **data["before_after"]),
        lifestyle_shots=SubScore(name="Lifestyle Shots", **data["lifestyle_shots"]),
        visual_hierarchy_brand=SubScore(name="Visual Hierarchy & Brand", **data["visual_hierarchy_brand"]),
        flagged_issues=data.get("flagged_issues", [])
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
