import anthropic
from analyser.models import ReviewsScore, SubScore
from analyser.claude_client import call_claude
from ingester.models import IngestedContext
from scraper.models import PDPTextData
from utils.logger import get_logger

log = get_logger("reviews_scorer")

SYSTEM = """You are a CRO analyst auditing product reviews on a health/wellness PDP.
Evaluate the reviews against the product context provided.
Return ONLY valid JSON — no explanation, no markdown.

Schema:
{
  "overall": <float 0-10>,
  "freshness": {
    "score": <float 0-10>,
    "observation": "<what you found>",
    "suggestion": "<what to fix>"
  },
  "rating_distribution": {
    "score": <float 0-10>,
    "observation": "<what you found>",
    "suggestion": "<what to fix>"
  },
  "theme_alignment": {
    "score": <float 0-10>,
    "observation": "<what you found — do review themes match product claims?>",
    "suggestion": "<what to fix>"
  },
  "negative_handling": {
    "score": <float 0-10>,
    "observation": "<are negative reviews addressed or ignored?>",
    "suggestion": "<what to fix>"
  },
  "flagged_issues": ["<specific issue 1>", "<specific issue 2>"]
}"""


def score_reviews(
    client: anthropic.Anthropic,
    pdp: PDPTextData,
    context: IngestedContext
) -> ReviewsScore:
    log.info(f"Scoring reviews for {pdp.url} ({len(pdp.reviews)} reviews)")

    if not pdp.reviews:
        log.warning("No reviews found — returning zero scores")
        empty = SubScore(name="n/a", score=0, observation="No reviews found on page", suggestion="Add customer reviews")
        return ReviewsScore(
            overall=0,
            freshness=empty,
            rating_distribution=empty,
            theme_alignment=empty,
            negative_handling=empty,
            flagged_issues=["No reviews scraped — check CSS selectors in config.yaml"]
        )

    # Format reviews for Claude
    reviews_text = "\n".join([
        f"[{i+1}] Rating: {r.rating or 'N/A'} | Date: {r.date or 'N/A'} | {r.text[:300]}"
        for i, r in enumerate(pdp.reviews[:50])
    ])

    prompt = f"""PRODUCT: {context.product_name}
PRODUCT CLAIMS: {', '.join(context.product_brief.primary_benefits)}
PERSONA TOP CONCERNS: {', '.join(context.persona.top_concerns)}

TOP {len(pdp.reviews)} REVIEWS:
{reviews_text}

Evaluate these reviews across all 4 dimensions. Be specific — quote actual reviews where relevant."""

    data = call_claude(client, SYSTEM, prompt)

    return ReviewsScore(
        overall=data["overall"],
        freshness=SubScore(name="Freshness", **data["freshness"]),
        rating_distribution=SubScore(name="Rating Distribution", **data["rating_distribution"]),
        theme_alignment=SubScore(name="Theme Alignment", **data["theme_alignment"]),
        negative_handling=SubScore(name="Negative Handling", **data["negative_handling"]),
        flagged_issues=data.get("flagged_issues", [])
    )
