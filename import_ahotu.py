"""import_ahotu.py – Merge races collected from ahotu.com into the Google Sheet.

ahotu.com sits behind a Cloudflare bot check, so it can't be scraped from
GitHub Actions. Races are collected by hand in a browser into
data/ahotu_*.csv (one line per race, distances separated by ';') and merged
here with the same no-wipe logic as the jogg.se scraper.

Multi-distance races become one row per distance, like jogg.se lists them.

Usage:
    python import_ahotu.py data/ahotu_2026.csv --dry-run
    python import_ahotu.py data/ahotu_2026.csv
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from datetime import date

from scrape_jogg import SHEET_HEADER, _dist_category
from sheets import merge_rows, open_worksheet

log = logging.getLogger(__name__)

EVENT_URL = "https://www.ahotu.com/sv/event/{slug}"


def _format_km(km: float) -> str:
    return f"{int(km)} km" if km == int(km) else f"{km:.1f} km".replace(".", ",")


def load_rows(path: str) -> list[dict]:
    today = date.today().isoformat()
    rows: list[dict] = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["date"] < today:
                continue
            kms = [float(k) for k in r["distances_km"].split(";") if k.strip()]
            for km in kms or [None]:
                name = r["name"]
                if km is not None and len(kms) > 1:
                    name = f"{name} ({_format_km(km)})"
                rows.append({
                    "name":     name,
                    "date":     r["date"],
                    "city":     r["city"],
                    "county":   r["county"],
                    "distance": _format_km(km) if km is not None else "",
                    "dist_cat": _dist_category(km) if km is not None else "",
                    "region":   r["county"],
                    "link":     EVENT_URL.format(slug=r["slug"]),
                })
    return rows


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Merge ahotu CSV → Google Sheet")
    parser.add_argument("csv_path")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change, don't write")
    args = parser.parse_args()

    rows = load_rows(args.csv_path)
    log.info("Loaded %d rows from %s", len(rows), args.csv_path)

    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    if not sheet_id:
        if args.dry_run:
            csv.DictWriter(sys.stdout, fieldnames=SHEET_HEADER).writerows(rows)
            return 0
        log.error("GOOGLE_SHEET_ID environment variable not set")
        return 1

    merge_rows(open_worksheet(sheet_id), rows, source="ahotu", dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
