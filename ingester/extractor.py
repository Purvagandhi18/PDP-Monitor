import json
import anthropic
from ingester.models import (
    PersonaProfile, NarrativePillars,
    BrandVoice, ProductBrief, IngestedContext
)
from ingester.pdf_reader import load_all_pdfs
from utils.config_loader import get_env
from utils.logger import get_logger

log = get_logger("extractor")


def _call_claude(client: anthropic.Anthropic, system: str, user: str) -> dict:
    """Call Claude and parse JSON response."""
    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2048,
        system=system,
        messages=[{"role": "user", "content": user}]
    )
    text = response.content[0].text.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()

    return json.loads(text)


def extract_persona(client: anthropic.Anthropic, raw_text: str) -> PersonaProfile:
    log.info("Extracting persona profile...")
    system = (
        "You are a brand strategist. Extract structured persona data from the document. "
        "Return ONLY valid JSON matching this exact schema — no explanation, no markdown:\n"
        "{\n"
        '  "name": "persona name or label",\n'
        '  "age_range": "e.g. 28-40",\n'
        '  "description": "2-3 sentence summary of who they are",\n'
        '  "top_concerns": ["concern 1", "concern 2", "concern 3"],\n'
        '  "motivations": ["motivation 1", "motivation 2"],\n'
        '  "language_cues": ["phrase or word they use", "..."],\n'
        '  "objections": ["objection 1", "objection 2"]\n'
        "}"
    )
    data = _call_claude(client, system, f"PERSONA DOCUMENT:\n\n{raw_text}")
    profile = PersonaProfile(**data)
    log.info(f"Persona extracted → {profile.name} | Concerns: {len(profile.top_concerns)}")
    return profile


def extract_narrative(client: anthropic.Anthropic, raw_text: str) -> NarrativePillars:
    # Return a default if narrative doc not provided
    if raw_text.startswith("[") and "not provided" in raw_text:
        log.warning("Narrative PDF not found — using placeholder. Add inputs/pdfs/narrative.pdf for better scoring.")
        return NarrativePillars(
            core_story="Narrative document not provided",
            pillars=["Add narrative.pdf to inputs/pdfs/ for full analysis"],
            emotional_arc="problem → solution → proof",
            key_claims=[]
        )

    log.info("Extracting narrative pillars...")
    system = (
        "You are a brand storyteller. Extract the narrative structure from this document. "
        "Return ONLY valid JSON matching this exact schema — no explanation, no markdown:\n"
        "{\n"
        '  "core_story": "one-line story arc",\n'
        '  "pillars": ["pillar 1", "pillar 2", "pillar 3"],\n'
        '  "emotional_arc": "problem → empathy → solution → proof (customised to this brand)",\n'
        '  "key_claims": ["claim 1", "claim 2", "claim 3"]\n'
        "}"
    )
    data = _call_claude(client, system, f"NARRATIVE DOCUMENT:\n\n{raw_text}")
    narrative = NarrativePillars(**data)
    log.info(f"Narrative extracted → {len(narrative.pillars)} pillars, {len(narrative.key_claims)} claims")
    return narrative


def extract_brand_voice(client: anthropic.Anthropic, raw_text: str) -> BrandVoice:
    log.info("Extracting brand voice guidelines...")
    system = (
        "You are a copy director. Extract brand voice rules from this guidelines document. "
        "Return ONLY valid JSON matching this exact schema — no explanation, no markdown:\n"
        "{\n"
        '  "tone_descriptors": ["e.g. warm", "confident", "no-fluff"],\n'
        '  "dos": ["do this in copy", "..."],\n'
        '  "donts": ["never do this", "..."],\n'
        '  "power_words": ["words the brand owns", "..."],\n'
        '  "banned_words": ["words never to use", "..."]\n'
        "}"
    )
    data = _call_claude(client, system, f"BRAND GUIDELINES DOCUMENT:\n\n{raw_text}")
    voice = BrandVoice(**data)
    log.info(f"Brand voice extracted → tone: {voice.tone_descriptors}")
    return voice


def extract_product_brief(client: anthropic.Anthropic, raw_text: str) -> ProductBrief:
    log.info("Extracting product brief...")
    system = (
        "You are a product marketer. Extract structured product information from this brief. "
        "Return ONLY valid JSON matching this exact schema — no explanation, no markdown:\n"
        "{\n"
        '  "product_name": "full product name",\n'
        '  "tagline": "the product tagline",\n'
        '  "key_ingredients": ["ingredient 1", "..."],\n'
        '  "primary_benefits": ["benefit 1", "benefit 2"],\n'
        '  "target_persona_concerns": ["concern this product addresses", "..."],\n'
        '  "proof_points": ["clinical study / certification / award", "..."],\n'
        '  "differentiators": ["what makes it different", "..."]\n'
        "}"
    )
    data = _call_claude(client, system, f"PRODUCT BRIEF DOCUMENT:\n\n{raw_text}")
    brief = ProductBrief(**data)
    log.info(f"Product brief extracted → {brief.product_name}")
    return brief


def ingest(product_config: dict) -> IngestedContext:
    """
    Main entry point. Pass a product's config block.
    Returns a fully populated IngestedContext.
    """
    client = anthropic.Anthropic(api_key=get_env("ANTHROPIC_API_KEY"))

    log.info(f"Starting ingestion for: {product_config['name']}")
    raw_texts = load_all_pdfs(product_config["pdfs"])

    persona   = extract_persona(client, raw_texts["persona"])
    narrative = extract_narrative(client, raw_texts["narrative"])
    voice     = extract_brand_voice(client, raw_texts["brand_guidelines"])
    brief     = extract_product_brief(client, raw_texts["product_brief"])

    context = IngestedContext(
        persona=persona,
        narrative=narrative,
        brand_voice=voice,
        product_brief=brief,
        product_name=product_config["name"]
    )

    log.info(f"Ingestion complete for: {context.product_name}")
    return context
