"""scrape_jogg.py – Scrape upcoming races from jogg.se → Google Sheet.

Targets:  Stockholm (Stockholms), Göteborg (Västra Götalands), Malmö (Skåne)
Filters:  distance ≥ 10 km, within the next 180 days
Output:   Google Sheet (first tab) with columns:
          A:name  B:date  C:city  D:county  E:distance  F:dist_cat  G:region  H:link

Environment variables (required in production, optional for --dry-run):
    GOOGLE_SERVICE_ACCOUNT_JSON  – Service account JSON string
    GOOGLE_SHEET_ID              – Target Google Sheet ID

Usage:
    python scrape_jogg.py              # writes to Google Sheet
    python scrape_jogg.py --dry-run    # prints rows, no sheet write
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

BASE_URL     = "https://www.jogg.se"
CALENDAR_URL = "https://www.jogg.se/Tavling/Kalender.aspx"

# jogg.se county filter values (Swedish: Länskod)
# These are the numeric IDs used in the county dropdown on jogg.se
COUNTY_IDS: dict[str, int] = {
    "Stockholms":      1,
    "Skåne":           12,
    "Västra Götalands": 14,
}

# County → display region (matches runclubs.se regions)
COUNTY_REGION: dict[str, str] = {
    "Stockholms":      "Stockholm",
    "Skåne":           "Malmö",
    "Västra Götalands": "Göteborg",
}

# Distance category buckets (km)
DIST_BUCKETS = [
    ("10k",         9.5,  10.49),
    ("11-20k",     10.5,  20.99),
    ("Halvmaraton", 21.0,  22.49),
    ("30k",        22.5,  35.0),
    ("Maraton",    40.0,  43.5),
]

# How far ahead to look
LOOKAHEAD_DAYS = 180

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; runclubs-race-scraper/1.0; "
        "+https://runclubs.se)"
    ),
    "Accept-Language": "sv-SE,sv;q=0.9",
}

SHEET_HEADER = ["name", "date", "city", "county", "distance", "dist_cat", "region", "link"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_km(text: str) -> float | None:
    """Extract a km value from strings like '42,195 km', '10 km', '21.1km'."""
    if not text:
        return None
    m = re.search(r"([\d.,]+)\s*km", text, re.IGNORECASE)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return None


def _dist_category(km: float) -> str | None:
    for label, lo, hi in DIST_BUCKETS:
        if lo <= km <= hi:
            return label
    return None


def _parse_date_sv(text: str) -> date | None:
    """Parse Swedish date strings: '2025-06-15', '15 jun 2025', '15/6 2025'."""
    text = text.strip()
    # ISO format
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        pass
    # Swedish short month names
    sv_months = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "maj": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "okt": 10, "nov": 11, "dec": 12,
    }
    m = re.match(r"(\d{1,2})\s+([a-z]+)\s+(\d{4})", text, re.IGNORECASE)
    if m:
        day, mon_str, year = int(m.group(1)), m.group(2).lower()[:3], int(m.group(3))
        mon = sv_months.get(mon_str)
        if mon:
            try:
                return date(year, mon, day)
            except ValueError:
                pass
    # DD/M YYYY or D/MM YYYY
    m = re.match(r"(\d{1,2})[/\-](\d{1,2})\s+(\d{4})", text)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            pass
    return None


# ── Scraping ──────────────────────────────────────────────────────────────────

def _get_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def _fetch_calendar_page(session: requests.Session, county_id: int, page: int = 1) -> BeautifulSoup | None:
    """Fetch one page of the jogg.se calendar filtered by county."""
    params = {
        "lan":   county_id,
        "sida":  page,
        "typ":   "lopning",     # running only
        "sortering": "datum",
    }
    try:
        resp = session.get(CALENDAR_URL, params=params, timeout=20)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as e:
        log.warning("Failed to fetch calendar county=%d page=%d: %s", county_id, page, e)
        return None


def _extract_races_from_page(soup: BeautifulSoup, county_name: str) -> list[dict]:
    """Parse race rows from a calendar page.

    jogg.se renders a table or list of races. We try several selectors
    to be resilient against minor HTML changes.
    """
    races: list[dict] = []

    # Try table rows first (common on jogg.se)
    rows = soup.select("table.tavlingar tr, table.kalender tr, #ctl00_ContentPlaceHolder1_GridView1 tr")
    if not rows:
        # Try list items / divs
        rows = soup.select(".tavling-rad, .race-item, .event-row, li.tavling")

    if not rows:
        log.debug("No rows found in county %s page", county_name)
        return races

    for row in rows:
        cells = row.find_all(["td", "li"])
        if len(cells) < 3:
            continue

        # Extract text from all cells
        texts = [c.get_text(strip=True) for c in cells]

        # Skip header rows
        if any(t.lower() in ("datum", "tävling", "ort", "distans") for t in texts[:3]):
            continue

        # Try to find date, name, city, distance from the row
        raw_date_str = texts[0] if texts else ""
        race_date = _parse_date_sv(raw_date_str)
        if not race_date:
            # Try other cells for a date
            for t in texts[1:4]:
                race_date = _parse_date_sv(t)
                if race_date:
                    break

        if not race_date:
            continue

        # Race name — look for an <a> link
        name_cell = cells[1] if len(cells) > 1 else cells[0]
        link_tag = name_cell.find("a") or row.find("a")
        name = link_tag.get_text(strip=True) if link_tag else texts[1] if len(texts) > 1 else ""
        if not name:
            continue

        # Relative link to race detail page
        href = link_tag["href"] if link_tag and link_tag.get("href") else ""
        detail_url = urljoin(BASE_URL, href) if href else ""

        # City
        city = texts[2] if len(texts) > 2 else ""

        # Distance — may be in cell 3 or 4
        distance_raw = ""
        for t in texts[3:6]:
            if re.search(r"\d.*km", t, re.IGNORECASE):
                distance_raw = t
                break

        races.append({
            "name":        name.strip(),
            "date":        race_date.isoformat(),
            "city":        city.strip(),
            "county":      county_name,
            "distance_raw": distance_raw.strip(),
            "detail_url":  detail_url,
        })

    return races


def _fetch_detail(session: requests.Session, url: str) -> dict:
    """Fetch race detail page → extract registration link and distance if missing."""
    result = {"link": "", "distance_raw": ""}
    if not url:
        return result
    try:
        resp = session.get(url, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Registration link: look for "Anmäl dig", "Anmälan", "Registrera"
        for text in ("anmäl dig", "anmälan", "registrera", "anmäl", "sign up"):
            tag = soup.find("a", string=re.compile(text, re.IGNORECASE))
            if not tag:
                tag = soup.find("a", href=re.compile(r"anmal|anmalan|register|signup", re.IGNORECASE))
            if tag and tag.get("href"):
                href = tag["href"]
                # Resolve relative links
                if href.startswith("http"):
                    result["link"] = href
                else:
                    result["link"] = urljoin(url, href)
                break

        # Distance if not already found
        for selector in (".distans", ".distance", "#distans", "td.distance"):
            el = soup.select_one(selector)
            if el:
                t = el.get_text(strip=True)
                if re.search(r"\d.*km", t, re.IGNORECASE):
                    result["distance_raw"] = t
                    break

        if not result["distance_raw"]:
            # Search full page text for distance pattern
            page_text = soup.get_text(" ")
            m = re.search(r"(\d[\d.,]*\s*km)", page_text, re.IGNORECASE)
            if m:
                result["distance_raw"] = m.group(1).strip()

    except requests.RequestException as e:
        log.debug("Failed to fetch detail %s: %s", url, e)

    return result


def scrape_county(
    session: requests.Session,
    county_name: str,
    county_id: int,
    date_from: date,
    date_to: date,
    fetch_details: bool = True,
) -> list[dict]:
    """Scrape all races for one county, paginating as needed."""
    log.info("Scraping %s (id=%d)…", county_name, county_id)
    all_races: list[dict] = []

    for page in range(1, 20):  # safety limit: 20 pages per county
        soup = _fetch_calendar_page(session, county_id, page)
        if soup is None:
            break

        page_races = _extract_races_from_page(soup, county_name)
        if not page_races:
            break

        # Filter by date range
        filtered: list[dict] = []
        past_count = 0
        for r in page_races:
            try:
                d = date.fromisoformat(r["date"])
            except ValueError:
                continue
            if d < date_from:
                past_count += 1
                continue
            if d > date_to:
                continue
            filtered.append(r)

        all_races.extend(filtered)

        # If all rows on this page are in the past, we're done paginating
        if past_count == len(page_races):
            break

        time.sleep(0.5)  # polite crawl delay

    log.info("  Found %d candidate races for %s", len(all_races), county_name)

    if not fetch_details:
        return all_races

    # Fetch detail pages for registration links
    enriched: list[dict] = []
    for i, r in enumerate(all_races):
        if i > 0 and i % 10 == 0:
            log.info("  Fetching details %d/%d…", i, len(all_races))
        detail = _fetch_detail(session, r.get("detail_url", ""))
        if not r["distance_raw"] and detail["distance_raw"]:
            r["distance_raw"] = detail["distance_raw"]
        r["link"] = detail["link"]
        enriched.append(r)
        time.sleep(0.3)

    return enriched


# ── Data processing ───────────────────────────────────────────────────────────

def process_races(raw: list[dict]) -> list[dict]:
    """Filter, deduplicate, and enrich raw scraped races."""
    seen: set[str] = set()
    result: list[dict] = []

    for r in raw:
        # Normalise distance
        km = _parse_km(r.get("distance_raw", ""))
        if km is not None and km < 9.5:
            continue  # shorter than 10 km — skip

        dist_cat = _dist_category(km) if km is not None else None

        # Distance display string
        if km is not None:
            if km == int(km):
                distance_str = f"{int(km)} km"
            else:
                distance_str = f"{km:.1f} km".replace(".", ",")
        else:
            distance_str = r.get("distance_raw", "").strip()

        county   = r["county"]
        region   = COUNTY_REGION.get(county, "")
        if not region:
            continue

        # Dedup by (name, date)
        key = f"{r['name'].lower()}|{r['date']}"
        if key in seen:
            continue
        seen.add(key)

        result.append({
            "name":     r["name"],
            "date":     r["date"],
            "city":     r.get("city", ""),
            "county":   county,
            "distance": distance_str,
            "dist_cat": dist_cat or "",
            "region":   region,
            "link":     r.get("link", ""),
        })

    result.sort(key=lambda x: x["date"])
    return result


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

    # Clear existing data
    ws.clear()

    # Write header + rows
    data = [SHEET_HEADER]
    for r in rows:
        data.append([r[col] for col in SHEET_HEADER])

    ws.update(range_name="A1", values=data)
    log.info("Wrote %d rows (+ header) to sheet %s", len(rows), sheet_id)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Scrape jogg.se races → Google Sheet")
    parser.add_argument("--dry-run", action="store_true", help="Print rows, don't write to sheet")
    parser.add_argument("--no-details", action="store_true", help="Skip detail page fetching (faster, no reg links)")
    args = parser.parse_args()

    today     = date.today()
    date_from = today
    date_to   = today + timedelta(days=LOOKAHEAD_DAYS)

    session = _get_session()
    all_raw: list[dict] = []

    for county_name, county_id in COUNTY_IDS.items():
        races = scrape_county(
            session, county_name, county_id,
            date_from, date_to,
            fetch_details=not args.no_details,
        )
        all_raw.extend(races)

    processed = process_races(all_raw)
    log.info("Total races after processing: %d", len(processed))

    if args.dry_run:
        import csv, sys
        writer = csv.DictWriter(sys.stdout, fieldnames=SHEET_HEADER)
        writer.writeheader()
        writer.writerows(processed)
        return 0

    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    if not sheet_id:
        log.error("GOOGLE_SHEET_ID environment variable not set")
        return 1

    write_to_sheet(processed, sheet_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
