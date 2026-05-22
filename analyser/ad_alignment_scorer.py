import anthropic
from analyser.models import AdAlignmentScore, AdGap, SubScore
from analyser.claude_client import call_claude
from ingester.models import IngestedContext
from scraper.models import PDPTextData
from scraper.sheets_models import SheetsData
from utils.logger import get_logger

log = get_logger("ad_alignment_scorer")

SYSTEM = """You are a performance marketing analyst auditing whether a PDP capitalises on what's working in paid ads.
Your job: identify converting ad angles, check which are missing from the PDP, and write specific copy suggestions.
Be a copywriter, not just an analyst — suggest actual lines, not vague directions.
Return ONLY valid JSON — no explanation, no markdown.

Schema:
{
  "overall": <float 0-10>,
  "top_converting_angles": ["<angle 1>", "<angle 2>", "..."],
  "angles_present_on_pdp": ["<angle present>", "..."],
  "gaps": [
    {
      "angle": "<angle name e.g. Daily Energy>",
      "conv_rate": "<e.g. 3.9%>",
      "what_is_missing": "<what specifically is absent — e.g. no mention of afternoon crash, no energy timeline>",
      "what_to_add": "<exact copy suggestion — write the actual line or bullet to add>",
      "where_to_add": "<specific placement — e.g. Carousel slide 2 headline, Hero subhead, Banner>"
    }
  ],
  "atc_drop_off_addressed": {
    "score": <float 0-10>,
    "observation": "<is ATC % high? what friction in the copy is likely causing drop-off?>",
    "suggestion": "<specific copy fix to reduce ATC drop-off>"
  },
  "flagged_gaps": ["<one-line summary of each gap>"]
}"""


def _extract_angle_from_hook(hook: str) -> str:
    """
    Parse ad hook names like:
    'shilajit_gummies_int_vo_hindi_harsh_fit_daily_energy_pure_natural_ingredients_00188'
    into readable angles like 'daily energy, pure natural ingredients'
    """
    # Remove product prefix and ad ID suffix (last segment with numbers)
    parts = hook.lower().split("_")
    # Drop first 2 (product slug) and last 1 (ad ID)
    middle = parts[2:-1] if len(parts) > 3 else parts
    # Remove common non-semantic tokens
    stop_words = {"int", "vo", "inf", "bof", "tof", "mof", "hindi", "english",
                  "hinglish", "static", "reel", "ugc", "none", "pan", "india",
                  "si", "bca", "asc", "cbo", "before", "after"}
    tokens = [p for p in middle if p not in stop_words and len(p) > 2]
    return " ".join(tokens)


def score_ad_alignment(
    client: anthropic.Anthropic,
    pdp: PDPTextData,
    context: IngestedContext,
    sheets: SheetsData
) -> AdAlignmentScore:
    log.info(f"Scoring ad alignment for {pdp.url}")

    if not sheets.top_ads:
        log.warning("No ad data from sheets — returning low score")
        empty_sub = SubScore(
            name="ATC Drop-off Addressed",
            score=5,
            observation="No ad data available from Umbrella Sheet",
            suggestion="Check Google Sheets connection"
        )
        return AdAlignmentScore(
            overall=5.0,
            atc_drop_off_addressed=empty_sub,
            flagged_gaps=["No ad data available — check Sheets connector"]
        )

    # Get URL-level performance for this specific PDP
    url_stat = next(
        (s for s in sheets.url_stats if s.is_active_pdp and pdp.url.rstrip("/") in s.url),
        None
    )

    # Format top ads with their metrics
    ads_summary = []
    for ad in sheets.top_ads[:30]:
        angle = _extract_angle_from_hook(ad.hook)
        conv  = f"{ad.conversion_rate:.1f}%" if ad.conversion_rate else "N/A"
        atc   = f"{ad.atc_rate:.1f}%" if ad.atc_rate else "N/A"
        rank  = f"#{ad.ranking}" if ad.ranking else "N/A"
        ads_summary.append(f"  {rank} | Conv: {conv} | ATC: {atc} | Angle: {angle}")

    url_perf = ""
    if url_stat:
        url_perf = f"""
PDP PERFORMANCE:
  URL: {url_stat.url}
  Conv. %: {url_stat.conversion_rate or 'N/A'}%
  ATC %: {url_stat.atc_rate or 'N/A'}%
  NCs: {url_stat.ncs or 'N/A'}
  ROAS: {url_stat.roas or 'N/A'}"""

    prompt = f"""PRODUCT: {context.product_name}
PERSONA TOP CONCERNS: {', '.join(context.persona.top_concerns)}
{url_perf}

TOP {len(sheets.top_ads)} ADS (ranked by performance):
{chr(10).join(ads_summary)}

PDP CONTENT:
HEADLINE: {pdp.headline or 'NOT FOUND'}
SUBHEADS: {' | '.join(pdp.subheads[:10])}
BODY COPY: {' '.join(pdp.body_copy)[:2000]}
CTAs: {' | '.join(pdp.cta_texts)}
CAROUSEL SLIDES: {' | '.join([s.copy[:100] for s in pdp.carousels])}

Identify the top 5 converting angles from the ad names.
Check which are present/missing on the PDP.
If ATC % is high, identify what friction in the PDP copy might be causing it."""

    data = call_claude(client, SYSTEM, prompt)

    gaps = [
        AdGap(
            angle=g["angle"],
            conv_rate=g.get("conv_rate", "N/A"),
            what_is_missing=g["what_is_missing"],
            what_to_add=g["what_to_add"],
            where_to_add=g["where_to_add"]
        )
        for g in data.get("gaps", [])
    ]

    return AdAlignmentScore(
        overall=data["overall"],
        top_converting_angles=data.get("top_converting_angles", []),
        angles_present_on_pdp=data.get("angles_present_on_pdp", []),
        gaps=gaps,
        atc_drop_off_addressed=SubScore(
            name="ATC Drop-off Addressed",
            **data["atc_drop_off_addressed"]
        ),
        flagged_gaps=data.get("flagged_gaps", [])
    )
