import anthropic
from datetime import datetime
from typing import List, Optional, Tuple
from analyser.models import ReviewsScore, SubScore
from analyser.claude_client import call_claude
from ingester.models import IngestedContext
from scraper.models import PDPTextData, Review
from utils.logger import get_logger

log = get_logger("reviews_scorer")

# ── Date parsing helpers ───────────────────────────────────────────────────────

_DATE_FORMATS = [
    "%Y-%m-%d",       # Zeus canonical: 2026-05-10
    "%d/%m/%Y",       # 10/05/2026
    "%d-%m-%Y",       # 10-05-2026
    "%d %b %Y",       # 10 May 2026
    "%d %B %Y",       # 10 May 2026 (full month)
    "%B %d, %Y",      # May 10, 2026
    "%b %d, %Y",      # May 10, 2026 (short)
    "%Y/%m/%d",       # 2026/05/10
    "%d/%m/%y",       # 06/01/26  (RCL __NEXT_DATA__, day-first)
    "%m/%d/%y",       # 01/06/26  (fallback, month-first)
    "%d-%m-%y",       # 06-01-26
]


def _parse_date(date_str: Optional[str]) -> Optional[datetime]:
    """Try to parse a raw date string into a datetime. Returns None on failure."""
    if not date_str:
        return None
    s = date_str.strip()
    if s.lower() in ("", "n/a", "null", "none", "–", "-"):
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    log.debug(f"Could not parse review date: '{date_str}'")
    return None


def _audit_dates(reviews: List[Review]) -> Tuple[bool, int]:
    """
    Returns (has_any_valid_date, count_with_valid_date).
    Logs a warning if date coverage is partial or absent.
    """
    parsed = [_parse_date(r.date) for r in reviews]
    valid_count = sum(1 for d in parsed if d is not None)
    has_dates = valid_count > 0
    if not has_dates:
        log.warning(
            f"Review date audit: 0/{len(reviews)} reviews have parseable dates. "
            f"Raw samples: {[r.date for r in reviews[:5]]}"
        )
    elif valid_count < len(reviews):
        log.warning(
            f"Review date audit: {valid_count}/{len(reviews)} dates parsed — "
            f"freshness score may be unreliable."
        )
    else:
        log.info(f"Review date audit: all {valid_count}/{len(reviews)} dates parsed OK")
    return has_dates, valid_count

SYSTEM = """You are a CRO analyst auditing product reviews on a health/wellness PDP.
Evaluate the reviews against the product context provided.
Return ONLY valid JSON — no explanation, no markdown.

CRITICAL — judging freshness:
- The prompt gives you TODAY'S DATE. Judge every review date relative to THAT
  date, not your own assumptions about what year it is.
- Each review includes its computed age in days. A positive age means the review
  is in the PAST (valid). Only an age that is NEGATIVE is a future date.
- Do NOT call a review "future-dated" or "fabricated" just because its year looks
  high to you (e.g. 2026). Trust the provided age-in-days; it is computed from
  today's date. Reviews from the last 1-3 months are FRESH and score high.

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

    # ── Date audit — must run before sending to Claude ────────────────────────
    has_dates, date_count = _audit_dates(pdp.reviews)
    incomplete_dates = not has_dates

    if incomplete_dates:
        freshness_sub = SubScore(
            name="Freshness",
            score=0.0,
            observation=(
                f"All {len(pdp.reviews)} reviews are missing parseable dates. "
                f"Raw date values: {[r.date for r in pdp.reviews[:5]]}. "
                f"Freshness cannot be scored reliably without date data."
            ),
            suggestion=(
                "Ensure Zeus cache 'dateCreated' field is populated for all reviews. "
                "Check the Zeus cache file for this page ID and verify the topReviews "
                "entries each contain a valid dateCreated value."
            )
        )
        log.warning(
            f"score_reviews: returning score_status=incomplete_data for {pdp.url} — "
            f"no review dates available, freshness score set to null"
        )

    # Format reviews for Claude — include parsed date + computed age in days so
    # Claude judges freshness against the real current date, not its own guess.
    now = datetime.utcnow()

    def _age_label(r: Review) -> str:
        d = _parse_date(r.date)
        if not d:
            return "age: unknown (unparseable date)"
        days = (now - d).days
        if days < 0:
            return f"age: {-days} days in the FUTURE (invalid)"
        return f"age: {days} days ago"

    reviews_text = "\n".join([
        f"[{i+1}] Rating: {r.rating or 'N/A'} | "
        f"Date: {r.date or 'MISSING'} | {_age_label(r)} | "
        f"{r.text[:300]}"
        for i, r in enumerate(pdp.reviews[:50])
    ])

    prompt = f"""PRODUCT: {context.product_name}
PRODUCT CLAIMS: {', '.join(context.product_brief.primary_benefits)}
PERSONA TOP CONCERNS: {', '.join(context.persona.top_concerns)}

TODAY'S DATE: {now.strftime('%d %B %Y')} ({now.strftime('%Y-%m-%d')})
Judge review freshness relative to TODAY'S DATE above. Each review's age in days
is pre-computed for you — trust it. Recent reviews (last 1-3 months) are fresh.

TOP {len(pdp.reviews)} REVIEWS:
{reviews_text}

Evaluate these reviews across all 4 dimensions. Be specific — quote actual reviews where relevant."""

    data = call_claude(client, SYSTEM, prompt)

    # When dates were missing, override Claude's freshness with our explicit null marker.
    # Claude may have hallucinated a freshness score — discard it.
    if incomplete_dates:
        freshness = freshness_sub
        # Penalise overall: freshness is 25% of what Claude would have computed.
        # We set it to 0 since we cannot verify recency at all.
        overall = data.get("overall", 5.0)
        score_status = "incomplete_data"
        freshness_warning = (
            "Review dates missing from Zeus cache; freshness cannot be scored reliably. "
            f"Dates received: {[r.date for r in pdp.reviews[:5]]}"
        )
        log.warning(f"score_reviews: freshness forced to 0 (incomplete_data) for {pdp.url}")
    else:
        freshness = SubScore(name="Freshness", **data["freshness"])
        overall = data["overall"]
        score_status = None
        freshness_warning = None

    return ReviewsScore(
        overall=overall,
        freshness=freshness,
        rating_distribution=SubScore(name="Rating Distribution", **data["rating_distribution"]),
        theme_alignment=SubScore(name="Theme Alignment", **data["theme_alignment"]),
        negative_handling=SubScore(name="Negative Handling", **data["negative_handling"]),
        flagged_issues=data.get("flagged_issues", []),
        score_status=score_status,
        freshness_warning=freshness_warning,
    )
