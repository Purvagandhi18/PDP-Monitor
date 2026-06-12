#!/usr/bin/env python3
"""
PDP Monitor — V2
Usage:
  python main.py --run-now                                  # interactive product picker
  python main.py --run-now --product "Shilajit Gummies"     # specific product directly
  python main.py --run-now --product "Hair Regrowth Kit S3" # specific product directly
  python main.py --schedule                                  # run daily at 8am (all products)
  python main.py --schedule --time 09:00
"""

import argparse
import re
import sys
import time
import schedule
from datetime import datetime
from pathlib import Path
from typing import List

from ingester import ingest
from scraper import scrape_all_urls, enrich_with_visuals, pull_sheets_data
from scraper.zeus_connector import clear_url_images
from analyser import analyse_all
from report import build_report
from tools.storage_tool import save_run, load_previous_run
from agents.regression_agent import detect_regression
from utils.config_loader import load_config
from utils.logger import get_logger

log = get_logger("main")
config = load_config()


# ── Core pipeline ──────────────────────────────────────────────────────────────

def run_product(product_cfg: dict):
    """Full pipeline for one product."""
    name = product_cfg["name"]
    # Support both old list-of-strings and new list-of-dicts URL format
    raw_urls = product_cfg["urls"]
    url_cfgs = []
    for u in raw_urls:
        if isinstance(u, dict):
            url_cfgs.append(u)
        else:
            url_cfgs.append({"url": u, "narrative": None, "persona": None})
    urls = [u["url"] for u in url_cfgs]
    # Build url → config map for scorers
    url_meta = {u["url"]: u for u in url_cfgs}

    log.info(f"")
    log.info(f"{'━'*60}")
    log.info(f"  Starting: {name}")
    log.info(f"  URLs: {len(urls)}")
    log.info(f"  Time: {datetime.now().strftime('%H:%M:%S')}")
    log.info(f"{'━'*60}")

    # ── Step 1: Ingest PDFs ────────────────────────────────────
    log.info("[1/5] Ingesting PDFs...")
    try:
        context = ingest(product_cfg)
        log.info(f"      ✓ Persona: {context.persona.name}")
        log.info(f"      ✓ Concerns: {len(context.persona.top_concerns)}")
        log.info(f"      ✓ Pillars: {len(context.narrative.pillars)}")
    except Exception as e:
        log.error(f"PDF ingestion failed: {e}")
        log.error("Check that all PDFs exist in inputs/pdfs/ and are named correctly in config.yaml")
        return None

    # ── Step 2: Scrape PDPs ────────────────────────────────────
    log.info("[2/5] Scraping PDPs...")
    try:
        pdp_list = scrape_all_urls(urls)
        log.info(f"      ✓ Scraped {len(pdp_list)} URLs")
    except Exception as e:
        log.error(f"Scraping failed: {e}")
        return None

    # ── Step 3: Visual scrape (carousels + banners) ────────────
    log.info("[3/5] Scraping visuals...")
    enriched = []
    for pdp in pdp_list:
        if pdp.full_page_text == "SCRAPE_FAILED":
            log.warning(f"      Skipping visuals for failed scrape: {pdp.url}")
            enriched.append(pdp)
            continue
        try:
            # Always clear old images before downloading fresh ones
            url_slug = re.sub(r"[^\w]", "_", pdp.url)[:60]
            clear_url_images(url_slug)

            enriched.append(enrich_with_visuals(pdp))
            log.info(f"      ✓ {pdp.url} → {len(pdp.carousels)} slides, {len(pdp.banners)} banners")
        except Exception as e:
            log.warning(f"      Visual scrape failed for {pdp.url}: {e} — continuing without visuals")
            enriched.append(pdp)

    # ── Step 4: Pull Google Sheets ─────────────────────────────
    log.info("[4/5] Pulling Umbrella Sheet...")
    try:
        sheets = pull_sheets_data(name, urls)
        log.info(f"      ✓ {len(sheets.top_ads)} ads | {len(sheets.url_stats)} URLs")
    except Exception as e:
        log.warning(f"      Sheets pull failed: {e} — continuing without ad data")
        from scraper.sheets_models import SheetsData
        from datetime import datetime as dt
        sheets = SheetsData(
            product_name=name,
            pulled_at=dt.utcnow().isoformat()
        )

    # ── Step 5: Analyse ────────────────────────────────────────
    log.info("[5/5] Analysing...")
    results = analyse_all(enriched, context, sheets, url_meta=url_meta)

    if not results:
        log.error("Analysis returned no results")
        return None

    # ── Step 6: Save run snapshot ──────────────────────────────
    run_file = save_run(name, results)

    # ── Step 7: Regression detection ──────────────────────────
    previous_run = load_previous_run(name, before_run_id=run_file.stem)
    alerts, improvements = detect_regression(results, previous_run)

    # ── Summary log ───────────────────────────────────────────
    for r in results:
        delta_str = f" ({r.delta:+.1f})" if r.delta is not None else " (first run)"
        reg_str   = " 🔴 REGRESSION" if r.regression_flag else ""
        log.info(f"      {r.url}")
        log.info(f"        Reviews:      {r.reviews.overall:.1f}")
        log.info(f"        P×N:          {r.persona_narrative.overall:.1f}")
        log.info(f"        Copy Health:  {r.copy_health.overall:.1f}")
        log.info(f"        Visual:       {r.visual_design.overall:.1f}")
        log.info(f"        Ad Alignment: {r.ad_alignment.overall:.1f}")
        log.info(f"        ── OVERALL:   {r.overall_score:.1f}{delta_str} [{r.status.upper()}]{reg_str}")
        if r.rca:
            log.info(f"        ── RCA:       {len(r.rca)} items")

    if alerts:
        log.warning(f"")
        log.warning(f"  ⚠️  {len(alerts)} regression(s) detected:")
        for a in alerts:
            log.warning(f"     • {a.url}")
            log.warning(f"       {a.previous_score:.1f} → {a.current_score:.1f} ({a.delta:+.2f})")
            log.warning(f"       {a.likely_cause}")

    if improvements:
        log.info(f"")
        log.info(f"  📈 {len(improvements)} improvement(s):")
        for i in improvements:
            log.info(f"     • {i.url}  {i.previous_score:.1f} → {i.current_score:.1f} ({i.delta:+.2f})")

    # ── Build report ───────────────────────────────────────────
    report_path = build_report(results, sheets, name, pdp_list=enriched,
                               regression_alerts=alerts)
    log.info(f"")
    log.info(f"  ✅ Report saved → {report_path}")
    log.info(f"{'━'*60}")

    return report_path


def _pick_products(product_filter: str = None) -> list:
    """Return the list of product configs to run, prompting if not specified."""
    all_products = config["products"]

    if product_filter:
        matched = [p for p in all_products if p["name"].lower() == product_filter.lower()]
        if not matched:
            log.error(f"Product '{product_filter}' not found in config.yaml")
            log.error(f"Available: {[p['name'] for p in all_products]}")
            sys.exit(1)
        return matched

    # Interactive picker
    print("\nWhich product(s) do you want to run?")
    print("  0  All products")
    for i, p in enumerate(all_products, 1):
        print(f"  {i}  {p['name']}")
    print()

    while True:
        try:
            choice = input("Enter number (0 to run all): ").strip()
            idx = int(choice)
        except (ValueError, EOFError):
            print("  Please enter a number.")
            continue

        if idx == 0:
            return all_products
        if 1 <= idx <= len(all_products):
            return [all_products[idx - 1]]
        print(f"  Enter a number between 0 and {len(all_products)}.")


def run_all(product_filter: str = None):
    """Run pipeline for all products (or one specific product)."""
    products = _pick_products(product_filter)

    log.info(f"Running PDP Monitor for {len(products)} product(s)")
    report_paths = []

    for product_cfg in products:
        path = run_product(product_cfg)
        if path:
            report_paths.append(path)

    log.info(f"")
    log.info(f"Done. {len(report_paths)}/{len(products)} reports generated.")
    for p in report_paths:
        log.info(f"  → {p}")

    # Open reports in browser automatically
    if report_paths:
        _open_reports(report_paths)

    return report_paths


def _open_reports(paths: List[str]):
    """Open generated reports in the default browser."""
    import subprocess
    for path in paths:
        try:
            subprocess.Popen(["open", path])  # macOS
        except Exception:
            pass


# ── Scheduler ──────────────────────────────────────────────────────────────────

def scheduled_job():
    log.info(f"Scheduled run starting at {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    run_all()


def start_scheduler(run_time: str = "08:00"):
    """Run daily at the specified time (default 8am)."""
    log.info(f"Scheduler started — will run daily at {run_time}")
    log.info(f"Keep this terminal window open. Press Ctrl+C to stop.")

    schedule.every().day.at(run_time).do(scheduled_job)

    # Show next run time
    next_run = schedule.next_run()
    log.info(f"Next run: {next_run}")

    while True:
        schedule.run_pending()
        time.sleep(30)  # check every 30 seconds


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="PDP Monitor — daily PDP audit tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py --run-now
  python main.py --run-now --product "Shilajit Gummies"
  python main.py --schedule
  python main.py --schedule --time 09:00
        """
    )
    parser.add_argument(
        "--run-now",
        action="store_true",
        help="Run the full pipeline immediately"
    )
    parser.add_argument(
        "--schedule",
        action="store_true",
        help="Start scheduler (runs daily at --time, default 08:00)"
    )
    parser.add_argument(
        "--time",
        default="08:00",
        help="Time to run daily report in HH:MM format (default: 08:00)"
    )
    parser.add_argument(
        "--product",
        default=None,
        help="Run for a specific product only (must match name in config.yaml)"
    )

    args = parser.parse_args()

    if not args.run_now and not args.schedule:
        parser.print_help()
        sys.exit(0)

    # Validate .env
    _check_env()

    if args.run_now:
        run_all(product_filter=args.product)

    if args.schedule:
        if args.run_now:
            log.info("Ran once — now starting scheduler...")
        start_scheduler(run_time=args.time)


def _check_env():
    """Warn about missing credentials before starting."""
    from dotenv import load_dotenv
    import os
    load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=True)

    issues = []
    if not os.getenv("ANTHROPIC_API_KEY"):
        issues.append("ANTHROPIC_API_KEY not set in .env")

    if issues:
        log.error("Missing credentials:")
        for issue in issues:
            log.error(f"  • {issue}")
        log.error("Copy .env.example to .env and fill in your keys.")
        sys.exit(1)


if __name__ == "__main__":
    main()
