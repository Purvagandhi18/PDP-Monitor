# PDP Monitor

Automated audit tool for Man Matters product detail pages. Scrapes every PDP, scores it across five dimensions using Claude AI, and produces a single interactive HTML report. Designed to catch copy drift, visual gaps, and narrative misalignment before they hurt conversion — without anyone manually reviewing a page.

---

## What It Does

1. **Ingests** your persona, brand guidelines, product brief, and narrative from PDFs/docs
2. **Scrapes** each PDP — text via Playwright + Claude extraction, visuals directly from the Zeus CDN
3. **Scores** five dimensions per URL using Claude as the judge
4. **Builds** an interactive HTML report with per-tab breakdowns, flagged issues, and a live score cascade
5. **Generates RCA** (Root Cause Analysis) for any PDP below 8.0

---

## Scoring Model

Every PDP gets an **Overall Score /10**, weighted across five tabs:

| Tab | Weight | What It Checks |
|-----|--------|----------------|
| **Hygiene Check** | 15% | Claims accuracy, brand voice violations, spelling & grammar |
| **Narrative × Persona** | 30% | How well the page executes the configured narrative for the target persona |
| **Visual Layer** | 30% | Hero images, carousel flow, ingredient shots, proof points, lifestyle imagery — scored via Claude Vision on Zeus CDN images |
| **Text Layer** | 15% | Forbidden words, missing hooks, weak CTAs, tone mismatches |
| **Reviews & Ratings** | 15% | Review freshness, rating distribution, theme alignment with the narrative |

Tab scores roll up into the PDP Overall. All PDP scores average into the **Product Health Score** shown in the report hero bar.

### Score Bands

| Score | Status |
|-------|--------|
| 8.0 – 10 | ✅ Healthy |
| 6.5 – 7.9 | ⚠️ Attention — RCA generated |
| Below 6.5 | 🔴 Critical — RCA generated |

---

## Project Structure

```
pdp-monitor/
├── main.py                     # Entry point — orchestrates the full pipeline
├── config.yaml                 # Products, URLs, scoring weights, Zeus settings
├── requirements.txt
│
├── inputs/
│   ├── pdfs/
│   │   ├── persona.pdf         # Target customer profile
│   │   ├── brand_guidelines.pdf
│   │   ├── product_brief.docx
│   │   └── narrative.pdf       # Narrative pillars and claims
│   └── urls/
│
├── ingester/                   # Reads PDFs → extracts structured persona/narrative/brand data
│   ├── extractor.py            # Claude-powered extraction from raw PDF text
│   ├── pdf_reader.py           # pdfplumber + python-docx reader
│   └── models.py
│
├── scraper/                    # Data collection layer
│   ├── text_scraper.py         # Playwright + Claude extraction of headline/subheads/body/CTAs
│   ├── visual_scraper.py       # Downloads Zeus CDN images for Claude Vision scoring
│   ├── zeus_connector.py       # Zeus CMS cache loader (sections + displayOrder formats)
│   ├── sheets_connector.py     # Google Sheets pull for ad performance data
│   └── models.py
│
├── analyser/                   # Scoring engine
│   ├── engine.py               # Orchestrates all five scorers per URL
│   ├── reviews_scorer.py       # Freshness, rating distribution, narrative alignment
│   ├── persona_narrative_scorer.py  # Hero, carousel, banners, page arc, CTAs
│   ├── copy_health_scorer.py   # Claims, brand voice, spelling, forbidden words
│   ├── visual_scorer.py        # Claude Vision on Zeus images
│   ├── ad_alignment_scorer.py  # PDP vs ad performance data from Sheets
│   ├── rca.py                  # Root cause analysis for sub-8 scores
│   └── claude_client.py        # Shared Anthropic client wrapper
│
├── report/
│   ├── builder.py              # Jinja2 renderer
│   └── template.html           # Interactive HTML report template
│
├── outputs/
│   ├── reports/                # Generated HTML reports (one per run, date-stamped)
│   ├── zeus_cache/             # Zeus CMS JSON snapshots (2024397.json, 2024503.json)
│   ├── screenshots/            # Playwright screenshots (gitignored)
│   └── run_YYYYMMDD.log
│
└── utils/
    ├── config_loader.py
    └── logger.py
```

---

## Setup

### 1. Clone and create virtual environment

```bash
cd pdp-monitor
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### 2. Set environment variables

```bash
cp .env.example .env
```

Open `.env` and fill in:

```
ANTHROPIC_API_KEY=your_key_here
ZEUS_API_URL=                  # optional — leave blank to use pre-fetched cache
ZEUS_API_KEY=                  # optional
```

### 3. Add your input PDFs

Drop these into `inputs/pdfs/`:

| File | Purpose |
|------|---------|
| `persona.pdf` | Target customer profile (Tejas / Aakash / Fitness Buyer) |
| `brand_guidelines.pdf` | Tone, voice, forbidden words, visual rules |
| `product_brief.docx` | Ingredients, claims, usage, key differentiators |
| `narrative.pdf` | Narrative pillars and core claims per campaign |

### 4. Configure your product

Edit `config.yaml` — add/update URLs and map each URL to its narrative and persona:

```yaml
products:
  - name: "Shilajit Gummies"
    urls:
      - url: "https://manmatters.com/dp/shilajit-gummies/2024397"
        narrative: "Product First"
        persona: "Tejas"
      - url: "https://manmatters.com/dp/2024503"
        narrative: "Daily Energy"
        persona: "Tejas"
    pdfs:
      persona: "inputs/pdfs/persona.pdf"
      brand_guidelines: "inputs/pdfs/brand_guidelines.pdf"
      product_brief: "inputs/pdfs/product_brief.docx"
      narrative: "inputs/pdfs/narrative.pdf"
```

### 5. Add Zeus cache files (if not using live API)

Drop pre-fetched Zeus CMS JSON files into `outputs/zeus_cache/` named by page ID:

```
outputs/zeus_cache/2024397.json
outputs/zeus_cache/2024503.json
```

Two formats are supported: `sections` style (2024397) and `displayOrder` style (2024503). The connector auto-detects which format is present.

---

## Running

```bash
# Run once immediately
python main.py --run-now

# Run for a specific product only
python main.py --run-now --product "Shilajit Gummies"

# Run on a daily schedule (8am)
python main.py --schedule
```

Reports are saved to `outputs/reports/shilajit_gummies_YYYYMMDD.html`.

---

## The Interactive Report

Open any HTML report in a browser. No server needed — it's a single self-contained file.

### Tick / Untick scoring

Every flagged issue and suggestion in the report is actionable:

- **SSR rows** (Narrative, Visual, Reviews tabs) — each row is a scored sub-dimension. Marking it resolved sets that sub-dimension to **10/10**, which immediately recalculates the tab score → PDP overall → Product Health Score in the hero bar.
- **Hygiene flags** (Claims / Brand / Spell sub-tabs) — dismissing individual flags scales the sub-score linearly toward 10 as more items are cleared.
- **Text Layer insights** — same linear scaling for critical and warning items.

The score cascade is live — one tick in Reviews ripples up to the hero bar in real time. The report doubles as a prioritisation worksheet: you can see exactly how much each fix is worth before doing it.

### Hygiene sub-tabs

The Hygiene tab has three sub-tabs:

| Sub-tab | What it flags |
|---------|---------------|
| **Claims** | Unsubstantiated efficacy claims, missing qualifiers |
| **Brand Guidelines** | Tone violations, forbidden words, off-brand framing |
| **Spell Check** | Typos, grammatical errors, inconsistent capitalisation |

---

## How Images Are Scored

The visual audit does not rely on screenshots. Zeus cache files (`outputs/zeus_cache/{page_id}.json`) are loaded at run time and every image — hero gallery, clinical proof slides, ingredient carousels, comparison banners — is downloaded from the Zeus CDN and sent to Claude Vision for analysis. This means the scorer sees the same images the customer sees, in page display order, and can flag missing proof-point images, weak lifestyle shots, or broken hierarchy with exact positional context.

---

## How Reviews Are Scored

Customer reviews are pulled from the Zeus cache, not scraped from the storefront (which renders them dynamically and inconsistently). Each review card shows the author, star rating, date, and title exactly as stored in Zeus. The scorer evaluates:

- **Freshness** — how recent the reviews are relative to today
- **Rating distribution** — skew of 1–5 star ratings
- **Narrative alignment** — whether review language reinforces the page's configured narrative pillar

---

## Text Extraction — How It Works

Playwright loads and fully scrolls each PDP to trigger lazy-loaded content. A structured DOM dump is captured in the format `[tag] text` (h1, h2, h3, h4, button, p, li, span, div). Noise is removed before the dump is sent to Claude:

**Class-based exclusions:** nav, header, footer, cookie banners, modals, wallet widgets, delivery panels, pincode widgets, installment banners, affiliate cards, recently-viewed carousels.

**Text-pattern exclusions (`isNoisyText`):** price strings (`₹799`), tax lines (`Incl of all taxes`), delivery estimates (`Get by 27th May`), wallet promos (`MM Wallet`, `Save upto`), category nav blobs (`Hair Regrowth Beard Growth...`), pincode prompts.

Claude then identifies headline, subheads, body_copy, and cta_texts from the clean dump — no hardcoded CSS selectors for content, so extraction works regardless of the site's framework or class naming.

---

## Git Branches

| Branch | Purpose |
|--------|---------|
| `main` | V1 — stable baseline |
| `v2-clarity` | V2 — active development (Microsoft Clarity integration, scraper improvements) |

To roll back to V1: `git checkout main`

---

## Environment Variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `ANTHROPIC_API_KEY` | Yes | Claude API — extraction, scoring, RCA |
| `ZEUS_API_URL` | No | Live Zeus CMS fetch (uses cache if blank) |
| `ZEUS_API_KEY` | No | Zeus API auth |
| `MIXPANEL_PROJECT_ID` | No | Future Mixpanel integration |
| `GMAIL_SENDER` | No | Future email delivery |

---

## Known Limitations

- **Ad Alignment score defaults to 5.0** when Google Sheets is not connected. Connect the Umbrella Sheet by adding its service account credentials to `.env`.
- **Zeus cache is a snapshot** — re-fetch or update `outputs/zeus_cache/*.json` when the product page changes significantly.
- **Reviews in 2024503.json** are currently seeded from URL 1 data. Replace with actual URL 2 reviews when available.
- **Model:** `claude-opus-4-5` is used for all extraction and scoring. Swap in `config.yaml` or `analyser/claude_client.py` to use a faster/cheaper model.
