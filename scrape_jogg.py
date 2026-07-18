"""scrape_jogg.py – Scrape upcoming races from jogg.se and write to Google Sheet.

Calendar URL (per month, all Sweden, distance 10–43 km):
  https://www.jogg.se/Kalender/Tavlingar.aspx?aar=YYYY&mon=M&fdist=10&tdist=43&...

Each .calendaritem card on the page already contains the registration link
(/Tavling/SignUpBridge.aspx?id=XXX) when one is available — no detail page
fetching needed.

Targets:  Stockholm (region "Stockholm"),
          Göteborg (region "Västra Götaland"),
          Malmö    (region "Skåne")

Environment variables (required in production, optional for --dry-run):
    GOOGLE_SERVICE_ACCOUNT_JSON  – Service account JSON string
    GOOGLE_SHEET_ID              – Target Google Sheet ID

Usage:
    python scrape_jogg.py              # writes to Google Sheet
    python scrape_jogg.py --dry-run    # prints CSV, no sheet write
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from datetime import date, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

BASE_URL = "https://www.jogg.se"

CALENDAR_URL = (
    "https://www.jogg.se/Kalender/Tavlingar.aspx"
    "?fdist=10&tdist=100&type=1&country=1&region=0"
    "&tlopp=False&relay=False&surface=&tridist=0&title=1"
)

# Distance category buckets (km)
DIST_BUCKETS = [
    ("10k",         9.5,  10.49),
    ("11-20k",     10.5,  20.99),
    ("Halvmaraton", 21.0,  22.49),
    ("30k",        22.5,  35.0),
    ("Maraton",    40.0,  43.5),
    ("Ultra",      43.6, 100.0),
]

# How many months ahead to scrape
LOOKAHEAD_MONTHS = 9

SHEET_HEADER = ["name", "date", "city", "county", "distance", "dist_cat", "region", "link"]

SV_MONTHS = {
    "januari": 1, "februari": 2, "mars": 3, "april": 4,
    "maj": 5, "juni": 6, "juli": 7, "augusti": 8,
    "september": 9, "oktober": 10, "november": 11, "december": 12,
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; runclubs-race-scraper/2.0; "
        "+https://runclubs.se)"
    ),
    "Accept-Language": "sv-SE,sv;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _months_ahead(n: int) -> list[tuple[int, int]]:
    """Return list of (year, month) tuples for the next n months including today's."""
    result = []
    today = date.today()
    year, month = today.year, today.month
    for _ in range(n):
        result.append((year, month))
        month += 1
        if month > 12:
            month = 1
            year += 1
    return result


def _parse_km(text: str) -> float | None:
    """Extract km value from strings like '42,20 km', '10 km', '21.1km'."""
    m = re.search(r"([\d][.,\d]*)\s*km", text, re.IGNORECASE)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return None


def _dist_category(km: float) -> str:
    for label, lo, hi in DIST_BUCKETS:
        if lo <= km <= hi:
            return label
    return ""


def _parse_sv_date(text: str) -> date | None:
    """Parse 'lördag 30 maj 2026' → date(2026, 5, 30). Also handles ISO format."""
    text = text.strip().lower()
    # ISO
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    # Swedish: [weekday] day month year
    m = re.search(r"(\d{1,2})\s+([a-zåäö]+)\s+(\d{4})", text)
    if m:
        day  = int(m.group(1))
        mon  = SV_MONTHS.get(m.group(2).lower())
        year = int(m.group(3))
        if mon:
            try:
                return date(year, mon, day)
            except ValueError:
                pass
    return None


# ── Scraping ──────────────────────────────────────────────────────────────────

def _get_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def _fetch_month(session: requests.Session, year: int, month: int) -> BeautifulSoup | None:
    url = f"{CALENDAR_URL}&aar={year}&mon={month}"
    try:
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
        log.info("Fetched %s → %d bytes, encoding=%s", url, len(resp.content), resp.encoding)
        # Debug: log first 2000 chars of HTML to diagnose selector issues
        if os.environ.get("LOG_LEVEL", "").upper() == "DEBUG":
            log.debug("HTML preview:\n%s", resp.text[:2000])
        soup = BeautifulSoup(resp.text, "html.parser")
        # Log all unique div classes to help find the right selector
        div_classes = set()
        for d in soup.find_all("div", class_=True):
            for c in d.get("class", []):
                div_classes.add(c)
        log.info("Div classes on page: %s", sorted(div_classes)[:40])
        return soup
    except requests.RequestException as e:
        log.warning("Failed to fetch %s: %s", url, e)
        return None


def _parse_cards(soup: BeautifulSoup) -> list[dict]:
    """Extract race data from .calendaritem cards on the page."""
    # Try multiple selectors in case class name differs
    cards = (
        soup.select("div.calendaritem")
        or soup.select(".calendaritem")
        or soup.select("div.competitionitem")
        or soup.select("div.race-item")
        or soup.select("div.event-item")
    )
    log.info("Found %d race card elements", len(cards))

    races: list[dict] = []
    for card in cards:
        # ── Race name + detail link ──────────────────────────────────────────
        name_tag = card.find("a", href=re.compile(r"Tavling\.aspx\?id=", re.IGNORECASE))
        if not name_tag:
            continue
        name = name_tag.get_text(strip=True)
        if not name:
            continue

        # ── Registration link ────────────────────────────────────────────────
        reg_tag = card.find("a", href=re.compile(r"SignUpBridge\.aspx\?id=", re.IGNORECASE))
        reg_href = reg_tag["href"] if reg_tag else ""
        link = urljoin(BASE_URL, reg_href) if reg_href else ""

        # ── Full card text for date / distance / region / city ───────────────
        full_text = card.get_text(" ", strip=True)

        # Date: look for Swedish date pattern in the card text
        race_date = None
        for chunk in full_text.split("  "):
            race_date = _parse_sv_date(chunk.strip())
            if race_date:
                break
        if not race_date:
            race_date = _parse_sv_date(full_text)
        if not race_date:
            log.debug("Could not parse date from card: %s", full_text[:120])
            continue

        # Distance: "42,20 km" or "10 km"
        km_match = re.search(r"([\d][.,\d]*)\s*km", full_text, re.IGNORECASE)
        km_str   = km_match.group(0) if km_match else ""
        km_val   = _parse_km(km_str)
        if km_val is not None:
            # Normalise: "21,10 km" → "21,1 km"
            if km_val == int(km_val):
                distance_str = f"{int(km_val)} km"
            else:
                distance_str = f"{km_val:.1f} km".replace(".", ",")
        else:
            distance_str = km_str.strip()

        # County: read directly from div.county ("Sverige / Västra Götaland")
        region_raw = ""
        county_el = card.find("div", class_="county")
        if county_el:
            county_text = county_el.get_text(strip=True)
            region_match = re.search(r"Sverige\s*/\s*(.+)$", county_text, re.IGNORECASE)
            if region_match:
                region_raw = region_match.group(1).strip()

        # City: read directly from div.city
        city_el = card.find("div", class_="city")
        city = city_el.get_text(strip=True) if city_el else ""

        races.append({
            "name":     name,
            "date":     race_date.isoformat(),
            "city":     city,
            "county":   region_raw,
            "distance": distance_str,
            "dist_cat": _dist_category(km_val) if km_val is not None else "",
            "region":   region_raw,
            "link":     link,
        })

    return races


# ── Main pipeline ─────────────────────────────────────────────────────────────

def scrape_all() -> list[dict]:
    session   = _get_session()
    today     = date.today()
    all_races: list[dict] = []
    seen:      set[str]   = set()

    for year, month in _months_ahead(LOOKAHEAD_MONTHS):
        log.info("Scraping %04d-%02d…", year, month)
        soup = _fetch_month(session, year, month)
        if soup is None:
            continue

        page_races = _parse_cards(soup)
        log.info("  → %d matching races", len(page_races))

        for r in page_races:
            if date.fromisoformat(r["date"]) < today:
                continue
            key = f"{r['name'].lower()}|{r['date']}"
            if key in seen:
                continue
            seen.add(key)
            all_races.append(r)

        time.sleep(0.5)

    all_races.sort(key=lambda x: x["date"])
    return all_races


# ── Google Sheets ─────────────────────────────────────────────────────────────

def write_to_sheet(rows: list[dict], sheet_id: str) -> None:
    import gspread
    from google.oauth2.service_account import Credentials

    raw = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    creds = Credentials.from_service_account_info(
        json.loads(raw),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    ws = gc.open_by_key(sheet_id).get_worksheet(0)
    ws.clear()

    data = [SHEET_HEADER] + [[r[col] for col in SHEET_HEADER] for r in rows]
    ws.update(range_name="A1", values=data)
    log.info("Wrote %d rows (+ header) to sheet %s", len(rows), sheet_id)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Scrape jogg.se races → Google Sheet")
    parser.add_argument("--dry-run", action="store_true", help="Print CSV, don't write to sheet")
    args = parser.parse_args()

    races = scrape_all()
    log.info("Total races: %d", len(races))

    if args.dry_run:
        import csv, sys
        writer = csv.DictWriter(sys.stdout, fieldnames=SHEET_HEADER)
        writer.writeheader()
        writer.writerows(races)
        return 0

    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    if not sheet_id:
        log.error("GOOGLE_SHEET_ID environment variable not set")
        return 1

    write_to_sheet(races, sheet_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
