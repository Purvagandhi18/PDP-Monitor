# PDP Monitor

Automated daily audit of Product Detail Pages. Scores each PDP across Reviews, Persona × Narrative, and Copy Health. Triggers a Root Cause Analysis when any PDP drops below 8/10.

## Setup

### 1. Install dependencies
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### 2. Add your credentials
```bash
cp .env.example .env
# Open .env and fill in your API keys
```

### 3. Add your PDFs
Drop the following into `inputs/pdfs/`:
- `persona.pdf`
- `brand_guidelines.pdf`
- `product_brief.pdf`
- `narrative.pdf`

### 4. Configure your product
Edit `config.yaml`:
- Add your PDP URLs
- Update CSS selectors to match your site
- Set your Mixpanel event names

### 5. Run
```bash
# Run once
python main.py --run-now

# Run on schedule (8am daily)
python main.py --schedule
```

## Output
Reports saved to `outputs/reports/` as HTML files.

## Scoring
| Score | Status |
|-------|--------|
| 8.0 – 10 | ✅ Healthy |
| 6.5 – 7.9 | ⚠️ Attention + RCA |
| Below 6.5 | 🔴 Critical + RCA |
