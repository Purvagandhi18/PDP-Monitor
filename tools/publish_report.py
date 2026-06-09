"""
publish_report.py — pushes the latest report to GitHub Pages (gh-pages branch).

After every run, the latest HTML report is copied to the gh-pages branch as
index.html and pushed. GitHub Pages serves it at:
  https://purvagandhi18.github.io/PDP-Monitor/

Usage:
    python3 tools/publish_report.py --product "Shilajit Gummies"

Returns the live GitHub Pages URL so send_report.py can include it in the email.
"""

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT   = Path(__file__).parent.parent
PAGES_URL   = "https://purvagandhi18.github.io/PDP-Monitor/"
REMOTE      = "origin"
PAGES_BRANCH = "gh-pages"


def _product_slug(name: str) -> str:
    return re.sub(r"[^\w]", "_", name.lower()).strip("_")


def _latest_report(product_name: str) -> Path:
    slug = _product_slug(product_name)
    reports = sorted((REPO_ROOT / "outputs" / "reports").glob(f"{slug}_*.html"))
    if not reports:
        raise FileNotFoundError(f"No report found for '{product_name}'")
    return reports[-1]


def _run(cmd: list, cwd=None) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd or REPO_ROOT)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{result.stderr}")
    return result.stdout.strip()


def publish(product_name: str) -> str:
    """
    Push the latest report for product_name to gh-pages as index.html.
    Returns the live GitHub Pages URL.
    """
    report = _latest_report(product_name)
    print(f"Publishing: {report.name}")

    # Save current branch to restore after
    current_branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])

    try:
        # Fetch latest gh-pages
        _run(["git", "fetch", REMOTE, PAGES_BRANCH])

        # Use worktree to avoid disturbing current branch
        worktree_path = REPO_ROOT / ".gh-pages-worktree"
        if worktree_path.exists():
            shutil.rmtree(worktree_path)

        _run(["git", "worktree", "add", str(worktree_path), f"{REMOTE}/{PAGES_BRANCH}"])

        try:
            # Copy report as index.html
            shutil.copy(report, worktree_path / "index.html")

            # Also keep a dated copy
            dated_name = report.name
            shutil.copy(report, worktree_path / dated_name)

            # Commit and push from worktree
            _run(["git", "add", "index.html", dated_name], cwd=worktree_path)
            _run(["git", "commit", "-m", f"report: {report.name}"], cwd=worktree_path)
            _run(["git", "push", REMOTE, f"HEAD:{PAGES_BRANCH}"], cwd=worktree_path)

            print(f"✓ Published to GitHub Pages")
            print(f"  URL: {PAGES_URL}")
            return PAGES_URL

        finally:
            _run(["git", "worktree", "remove", "--force", str(worktree_path)])

    except Exception as e:
        print(f"✗ Publish failed: {e}", file=sys.stderr)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--product", required=True)
    args = parser.parse_args()
    url = publish(args.product)
    print(url)
