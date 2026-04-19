# runclubs-races-scraper

Scrapes upcoming running races from [jogg.se](https://www.jogg.se) and writes them to a Google Sheet. Part of the [runclubs.se](https://runclubs.se) data pipeline.

## Pipeline

```
jogg.se calendar  →  this scraper (mikaelsto/runclubs-races-scraper)
                  →  Google Sheet (columns: name, date, city, county, distance, dist_cat, region, link)
                  →  page generator (amandahultin/runclubs)
                  →  runclubs.se/kommande-lopp
```

Scraper runs **Monday 05:00 UTC**. Page generator runs **Monday 08:30 UTC**.

## Google Sheet columns

| Col | Field      | Example              |
|-----|------------|----------------------|
| A   | name       | Göteborgsvarvet      |
| B   | date       | 2025-05-17           |
| C   | city       | Göteborg             |
| D   | county     | Västra Götalands     |
| E   | distance   | 21,1 km              |
| F   | dist_cat   | Halvmaraton          |
| G   | region     | Göteborg             |
| H   | link       | https://... (or empty if no reg link found) |

## Target regions

- **Stockholm** (Stockholms county)
- **Göteborg** (Västra Götalands county)
- **Malmö** (Skåne county)

Races shorter than 10 km are excluded.

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

# Full run
python scrape_jogg.py

# Fast mode (skip detail pages, no registration links)
python scrape_jogg.py --no-details --dry-run
```
