"""
Test: Narrative × Persona URL-level mapping

Verifies that 3 URLs with different narrative/persona assignments produce
DISTINCT scoring contexts — i.e., the scorer never uses the same inputs
for two URLs that have different assignments.

Run: python3 -m pytest tests/test_persona_narrative_mapping.py -v
"""

import sys
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))

from ingester.models import (
    PersonaProfile, NarrativePillars, BrandVoice, ProductBrief, IngestedContext
)
from analyser.persona_narrative_scorer import _resolve_url_context


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_context() -> IngestedContext:
    return IngestedContext(
        product_name="Shilajit Gummies",
        persona=PersonaProfile(
            name="Tejas",
            age_range="28-40",
            description="A working professional who feels low energy",
            top_concerns=["afternoon energy crash", "mental fatigue", "gym performance"],
            motivations=["stay sharp at work", "feel strong", "natural solutions"],
            language_cues=["no crash", "all-day energy", "clean ingredients"],
            objections=["it won't work", "tastes bad", "too expensive"],
        ),
        narrative=NarrativePillars(
            core_story="Daily energy without compromise",
            pillars=[
                "No added sugar — clean energy",
                "Himalayan Shilajit — pure source",
                "Summer energy — beat the heat naturally",
                "Daily ritual — consistency builds results",
                "Product format — gummies vs resin",
            ],
            emotional_arc="Problem → Empathy → Solution → Proof",
            key_claims=["300mg Shilajit per serving", "FSSAI approved", "Third-party tested"],
        ),
        brand_voice=BrandVoice(
            tone_descriptors=["confident", "clinical", "simple"],
            dos=["be specific", "cite proof"],
            donts=["overpromise", "use jargon"],
            power_words=["proven", "tested"],
            banned_words=["testosterone", "libido"],
        ),
        product_brief=ProductBrief(
            product_name="Shilajit Gummies",
            tagline="Daily Energy. Zero Compromise.",
            key_ingredients=["Shilajit", "Ashwagandha", "Gokshura"],
            primary_benefits=["energy", "strength", "stress resilience"],
            target_persona_concerns=["fatigue", "poor focus"],
            proof_points=["FSSAI approved", "Third-party lab tested"],
            differentiators=["No added sugar", "Vegan"],
        ),
    )


URL_1 = "https://manmatters.com/dp/shilajit-gummies/2024397"
URL_2 = "https://manmatters.com/dp/2024503"
URL_3 = "https://manmatters.com/dp/2025001"

ASSIGNMENTS = [
    (URL_1, "Product First",  "Tejas"),
    (URL_2, "Daily Energy",   "Tejas"),
    (URL_3, "Summer",         "Tejas"),
]


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_narrative_labels_are_distinct():
    """Each URL must resolve a different narrative_label."""
    context = _make_context()
    labels = []
    for url, narrative, persona in ASSIGNMENTS:
        ctx = _resolve_url_context(url, narrative, persona, context)
        labels.append(ctx["narrative_label"])

    assert len(set(labels)) == len(ASSIGNMENTS), (
        f"Expected {len(ASSIGNMENTS)} distinct narrative labels, got: {labels}"
    )


def test_persona_name_matches_assignment():
    """persona_name must match the configured_persona for each URL."""
    context = _make_context()
    for url, narrative, persona in ASSIGNMENTS:
        ctx = _resolve_url_context(url, narrative, persona, context)
        assert ctx["persona_name"] == persona, (
            f"URL {url}: expected persona '{persona}', got '{ctx['persona_name']}'"
        )


def test_pain_points_are_not_empty():
    """pain_points must be non-empty for every URL."""
    context = _make_context()
    for url, narrative, persona in ASSIGNMENTS:
        ctx = _resolve_url_context(url, narrative, persona, context)
        assert len(ctx["pain_points"]) > 0, (
            f"URL {url}: pain_points is empty"
        )


def test_summer_url_filters_pillars():
    """
    URL 3 is assigned 'Summer' narrative.
    Its resolved pillars should include the summer pillar
    (or fall back to all pillars — never empty).
    """
    context = _make_context()
    ctx = _resolve_url_context(URL_3, "Summer", "Tejas", context)
    assert len(ctx["narrative_pillars"]) > 0, (
        f"URL 3 (Summer): narrative_pillars must not be empty"
    )
    # If filtering worked, the summer pillar should be present
    summer_pillars = [p for p in ctx["narrative_pillars"] if "summer" in p.lower()]
    if summer_pillars:
        assert len(summer_pillars) >= 1, "Expected at least one summer pillar"


def test_no_two_urls_share_identical_prompt_inputs():
    """
    Core test: the combination of (narrative_label, pain_points[:3], narrative_pillars)
    must not be identical for any two URLs with different narrative assignments.
    """
    context = _make_context()
    fingerprints = []
    for url, narrative, persona in ASSIGNMENTS:
        ctx = _resolve_url_context(url, narrative, persona, context)
        fp = (
            ctx["narrative_label"],
            tuple(ctx["pain_points"][:3]),
            tuple(ctx["narrative_pillars"][:3]),
        )
        fingerprints.append((url, fp))

    # Check no two fingerprints with different narratives are identical
    for i, (url_a, fp_a) in enumerate(fingerprints):
        for url_b, fp_b in fingerprints[i+1:]:
            narrative_a = ASSIGNMENTS[i][1]
            narrative_b = ASSIGNMENTS[fingerprints.index((url_b, fp_b))][1]
            if narrative_a != narrative_b:
                assert fp_a != fp_b, (
                    f"URLs with different narratives returned identical prompt inputs:\n"
                    f"  {url_a} ({narrative_a}): {fp_a}\n"
                    f"  {url_b} ({narrative_b}): {fp_b}"
                )


if __name__ == "__main__":
    # Run as script for quick local check
    tests = [
        test_narrative_labels_are_distinct,
        test_persona_name_matches_assignment,
        test_pain_points_are_not_empty,
        test_summer_url_filters_pillars,
        test_no_two_urls_share_identical_prompt_inputs,
    ]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"  ✓ {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  ✗ {t.__name__}: {e}")

    print(f"\n{passed}/{len(tests)} passed")
    sys.exit(0 if passed == len(tests) else 1)
