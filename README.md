# runclubs-races-scraper

Collects upcoming running races and merges them into a Google Sheet. Part of the [runclubs.se](https://runclubs.se) data pipeline.

## Pipeline

```
jogg.se calendar   →  scrape_jogg.py   (GitHub Actions, manual dispatch)  ┐
ahotu.com (manual) →  import_ahotu.py  (run locally from data/ahotu_*.csv) ┴→ Google Sheet "Loppkalendern feed" / Races
                   →  page generator (amandahultin/runclubs)
                   →  runclubs.se/loppkalender
```

## Sources

- **jogg.se** – running races in all of Sweden, 5–1000 km, parkruns excluded. One row per race distance.
- **ahotu.com** – sits behind a Cloudflare bot check, so it can't be scraped from Actions.
  Races are collected in a browser into `data/ahotu_<year>.csv` (one line per race,
  distances separated by `;`) and merged with `python import_ahotu.py data/ahotu_2026.csv`.
  Collected 2026-09-24 from the Ultramaraton/Maraton/Halvmaraton/10 km filter.

## How the sheet is written (no wipe)

Both importers use `sheets.merge_rows`, which never clears the sheet:

- A row matching an existing one (same name + date, or same date/city with a similar
  name and distance) is updated **only if it came from the same source**. A match from
  another source just gets its empty cells filled — so the same race listed on both
  jogg.se and ahotu shows up once.
- New races are appended. Nothing is deleted; past races stay (the page generator skips them).
- Columns the scrapers don't own — e.g. `official_link` — are never touched.

## Google Sheet columns

| Col | Field         | Example              |
|-----|---------------|----------------------|
| A   | name          | Göteborgsvarvet      |
| B   | date          | 2026-05-16           |
| C   | city          | Göteborg             |
| D   | county        | Västra Götaland      |
| E   | distance      | 21,1 km              |
| F   | dist_cat      | Halvmaraton          |
| G   | region        | Västra Götaland      |
| H   | link          | registration link (jogg) or event page (ahotu) |
| I   | official_link | set by hand, preferred by the page generator   |
| J   | source        | `jogg` / `ahotu`     |

## Setup

### 1. Google Sheet

Create a Google Sheet (or use the existing one). Share it with the service account's email address (Editor access).

### 2. GitHub secrets

Add these secrets to the repository (`Settings → Secrets → Actions`):

| Secret | Value |
|--------|-------|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Full JSON of the service account key |
| `GOOGLE_SHEET_ID` | The Google Sheet ID (from its URL) |

### 3. Run locally

```bash
pip install -r requirements.txt

export GOOGLE_SERVICE_ACCOUNT_JSON='{"type":"service_account",...}'
export GOOGLE_SHEET_ID='1zVTWU3a-...'

# Dry run (prints CSV, no sheet write)
python scrape_jogg.py --dry-run

# Full run (merges into the sheet)
python scrape_jogg.py

# ahotu: preview the merge, then write
python import_ahotu.py data/ahotu_2026.csv --dry-run
python import_ahotu.py data/ahotu_2026.csv
```
