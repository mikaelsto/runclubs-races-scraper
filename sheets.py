"""sheets.py – Merge scraped races into the Google Sheet without wiping it.

Rows are matched against what is already in the sheet:

  1. Exact match on normalised name + date.
  2. Fuzzy match: same date, same city (or one side blank), similar name and
     compatible distance. This catches the same race listed under slightly
     different names by different sources ("Sylvesterloppet i Göteborg" vs
     "Sylvesterloppet Göteborg").

A matched row from the *same* source gets its scraped columns refreshed.
A matched row from a *different* source is left alone, except that empty
cells are filled in. Unmatched rows are appended. Rows are never deleted,
and columns the scrapers don't own (e.g. `official_link`) are never touched.
"""

from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from difflib import SequenceMatcher

log = logging.getLogger(__name__)

# Columns written by the scrapers, in sheet order. `source` is appended to the
# header automatically if the sheet doesn't have it yet.
SCRAPED_COLUMNS = ["name", "date", "city", "county", "distance", "dist_cat", "region", "link"]
SOURCE_COLUMN = "source"

# Existing rows without a source value predate this column and came from jogg.se.
DEFAULT_SOURCE = "jogg"

_STOPWORDS = {"i", "the", "run", "loppet", "lopp", "km", "k"}


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _base_name(name: str) -> str:
    """Normalised name without a trailing distance suffix like '(10 km)'."""
    name = re.sub(r"\(\s*[\d.,]+\s*km\s*\)\s*$", "", name, flags=re.IGNORECASE)
    words = [w for w in _norm(name).split() if w not in _STOPWORDS]
    return " ".join(words)


def _km(distance: str) -> float | None:
    m = re.search(r"([\d]+(?:[.,]\d+)?)\s*km", distance or "", re.IGNORECASE)
    return float(m.group(1).replace(",", ".")) if m else None


def _exact_key(row: dict) -> str:
    return f"{_norm(row.get('name', ''))}|{row.get('date', '')}"


def _is_fuzzy_match(a: dict, b: dict) -> bool:
    if a.get("date") != b.get("date"):
        return False
    city_a, city_b = _norm(a.get("city", "")), _norm(b.get("city", ""))
    if city_a and city_b and city_a != city_b:
        return False
    km_a, km_b = _km(a.get("distance", "")), _km(b.get("distance", ""))
    if km_a is not None and km_b is not None and abs(km_a - km_b) > 0.6:
        return False
    name_a, name_b = _base_name(a.get("name", "")), _base_name(b.get("name", ""))
    if not name_a or not name_b:
        return False
    if name_a in name_b or name_b in name_a:
        return True
    return SequenceMatcher(None, name_a, name_b).ratio() >= 0.75


def open_worksheet(sheet_id: str):
    import gspread
    from google.oauth2.service_account import Credentials

    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw:
        info = json.loads(raw)
    else:
        with open(os.environ["GOOGLE_APPLICATION_CREDENTIALS"]) as f:
            info = json.load(f)
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return gspread.authorize(creds).open_by_key(sheet_id).get_worksheet(0)


def merge_rows(ws, rows: list[dict], source: str, dry_run: bool = False) -> dict:
    """Upsert `rows` into worksheet `ws`. Returns counts per action."""
    values = ws.get_all_values()
    original_header = list(values[0]) if values else []
    header = list(original_header)
    existing_rows = values[1:]

    # Make sure every column we write exists in the header.
    for col in SCRAPED_COLUMNS + [SOURCE_COLUMN]:
        if col not in header:
            header.append(col)
    width = len(header)
    idx = {col: i for i, col in enumerate(header)}

    table = [r + [""] * (width - len(r)) for r in existing_rows]
    as_dicts = [dict(zip(header, r)) for r in table]
    exact = {_exact_key(d): i for i, d in enumerate(as_dicts)}

    changed: set[int] = set()
    counts = {"updated": 0, "filled": 0, "unchanged": 0, "appended": 0}

    for row in rows:
        row = {**row, SOURCE_COLUMN: source}
        i = exact.get(_exact_key(row))
        if i is None:
            i = next((j for j, d in enumerate(as_dicts) if _is_fuzzy_match(d, row)), None)

        if i is None:
            as_dicts.append(row)
            table.append([row.get(col, "") for col in header])
            exact[_exact_key(row)] = len(table) - 1
            counts["appended"] += 1
            continue

        current = as_dicts[i]
        same_source = (current.get(SOURCE_COLUMN) or DEFAULT_SOURCE) == source
        action = "unchanged"
        for col in SCRAPED_COLUMNS + [SOURCE_COLUMN]:
            new, old = row.get(col, ""), current.get(col, "")
            if col == SOURCE_COLUMN and not same_source:
                continue
            if new == old or (not new and col != SOURCE_COLUMN):
                continue
            if same_source or not old:
                current[col] = new
                table[i][idx[col]] = new
                changed.add(i)
                action = "updated" if same_source else "filled"
        counts[action] += 1

    appended = table[len(existing_rows):]
    # Rows appended in this run are written by append_rows, not batch_update.
    changed = {i for i in changed if i < len(existing_rows)}
    log.info("Merge (%s): %s", source, counts)
    if dry_run:
        return counts

    if header != original_header:
        ws.update(range_name="A1", values=[header])
    if changed:
        from gspread.utils import rowcol_to_a1
        ws.batch_update([
            {
                "range": f"A{i + 2}:{rowcol_to_a1(i + 2, width)}",
                "values": [table[i]],
            }
            for i in sorted(changed)
        ])
    if appended:
        ws.append_rows(appended, value_input_option="RAW", table_range="A1")
    return counts
