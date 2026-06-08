#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""wc2026_ingest_results.py

Fetch real WC2026 match results from football-data.org and update wc2026_fixtures.
Also adds home_score/away_score columns if not present.

Usage:
  python -X utf8 wc2026_ingest_results.py

ENV:
  DATABASE_PUBLIC_URL   required
  FOOTBALL_DATA_API_KEY required  (football-data.org free tier key)
"""

import os
import sys
import json
import logging
import time
from datetime import date

import psycopg2
import urllib.request
import urllib.error

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s | %(message)s",
)
log = logging.getLogger("ingest")

API_BASE = "https://api.football-data.org/v4"
COMP_CODE = "WC"  # FIFA World Cup 2026

# football-data.org team name -> our DB team name
NAME_MAP = {
    "Mexico":               "Mexico",
    "USA":                  "United States",
    "United States":        "United States",
    "Canada":               "Canada",
    "Argentina":            "Argentina",
    "Brazil":               "Brazil",
    "Spain":                "Spain",
    "France":               "France",
    "Germany":              "Germany",
    "England":              "England",
    "Portugal":             "Portugal",
    "Netherlands":          "Netherlands",
    "Belgium":              "Belgium",
    "Colombia":             "Colombia",
    "Ecuador":              "Ecuador",
    "Uruguay":              "Uruguay",
    "Japan":                "Japan",
    "South Korea":          "South Korea",
    "Republic of Korea":    "South Korea",
    "Korea Republic":       "South Korea",
    "Morocco":              "Morocco",
    "Senegal":              "Senegal",
    "Nigeria":              "Nigeria",
    "Turkey":               "Turkey",
    "Switzerland":          "Switzerland",
    "Austria":              "Austria",
    "Croatia":              "Croatia",
    "Norway":               "Norway",
    "Iran":                 "Iran",
    "Saudi Arabia":         "Saudi Arabia",
    "Australia":            "Australia",
    "New Zealand":          "New Zealand",
    "Qatar":                "Qatar",
    "Egypt":                "Egypt",
    "Algeria":              "Algeria",
    "Tunisia":              "Tunisia",
    "Ghana":                "Ghana",
    "Ivory Coast":          "Ivory Coast",
    "Cote d'Ivoire":        "Ivory Coast",
    "Côte d'Ivoire":        "Ivory Coast",
    "Czech Republic":       "Czech Republic",
    "Czechia":              "Czech Republic",
    "Bosnia and Herzegovina": "Bosnia and Herzegovina",
    "Bosnia-Herzegovina":   "Bosnia and Herzegovina",
    "Bosnia & Herzegovina": "Bosnia and Herzegovina",
    "Scotland":             "Scotland",
    "Paraguay":             "Paraguay",
    "Haiti":                "Haiti",
    "Cape Verde":           "Cape Verde",
    "DR Congo":             "DR Congo",
    "Congo DR":             "DR Congo",
    "Democratic Republic of Congo": "DR Congo",
    "Uzbekistan":           "Uzbekistan",
    "Jordan":               "Jordan",
    "Iraq":                 "Iraq",
    "Panama":               "Panama",
    "South Africa":         "South Africa",
    "Curaçao":              "Curaçao",
    "Curacao":              "Curaçao",
    "Sweden":               "Sweden",
    "Portugal":             "Portugal",
    "Scotland":             "Scotland",
}


def normalize(name: str) -> str:
    return NAME_MAP.get(name, name)


def api_get(path: str, api_key: str) -> dict:
    url = API_BASE + path
    req = urllib.request.Request(url, headers={"X-Auth-Token": api_key})
    log.info("GET %s", url)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def ensure_score_columns(conn):
    with conn.cursor() as cur:
        cur.execute("""
            ALTER TABLE wc2026_fixtures
            ADD COLUMN IF NOT EXISTS home_score INT,
            ADD COLUMN IF NOT EXISTS away_score INT
        """)
    conn.commit()
    log.info("Ensured home_score/away_score columns exist")


def main():
    DB_URL = (os.environ.get("DATABASE_PUBLIC_URL") or os.environ.get("DATABASE_URL") or "").strip()
    API_KEY = os.environ.get("FOOTBALL_DATA_API_KEY", "").strip()
    if not DB_URL:
        sys.exit("Set DATABASE_PUBLIC_URL")
    if not API_KEY:
        sys.exit("Set FOOTBALL_DATA_API_KEY")

    conn = psycopg2.connect(DB_URL, connect_timeout=20)
    ensure_score_columns(conn)

    # Fetch all WC2026 matches
    try:
        data = api_get(f"/competitions/{COMP_CODE}/matches?season=2026", API_KEY)
    except urllib.error.HTTPError as e:
        # Try without season filter if API returns error
        log.warning("Season filter failed (%s), trying without...", e.code)
        time.sleep(1)
        data = api_get(f"/competitions/{COMP_CODE}/matches", API_KEY)

    matches = data.get("matches", [])
    log.info("Fetched %d matches from API", len(matches))

    finished = [m for m in matches if m.get("status") == "FINISHED"]
    log.info("%d finished matches", len(finished))

    updated = 0
    not_found = []
    for m in finished:
        home_raw = m["homeTeam"]["name"]
        away_raw = m["awayTeam"]["name"]
        home = normalize(home_raw)
        away = normalize(away_raw)
        score = m.get("score", {}).get("fullTime", {})
        hs = score.get("home")
        as_ = score.get("away")
        if hs is None or as_ is None:
            log.warning("No score for %s vs %s", home, away)
            continue

        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE wc2026_fixtures
                SET home_score = %s, away_score = %s
                WHERE (home = %s AND away = %s)
                   OR (home = %s AND away = %s)
                """,
                (hs, as_, home, away, away, home),  # also try reversed
            )
            if cur.rowcount == 0:
                not_found.append((home_raw, away_raw, home, away))
            else:
                updated += cur.rowcount
        conn.commit()

    log.info("Updated %d fixture rows with scores", updated)
    if not_found:
        log.warning("Not matched in DB (%d):", len(not_found))
        for r, a, h2, a2 in not_found:
            log.warning("  raw: '%s' vs '%s'  ->  normalized: '%s' vs '%s'", r, a, h2, a2)

    # Summary
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM wc2026_fixtures WHERE home_score IS NOT NULL")
        n = cur.fetchone()[0]
    log.info("Total fixtures with scores in DB: %d", n)

    conn.close()
    print(f"\n=== Done ===")
    print(f"API matches fetched : {len(matches)}")
    print(f"Finished            : {len(finished)}")
    print(f"Updated in DB       : {updated}")
    print(f"Not matched         : {len(not_found)}")
    print(f"Total scored in DB  : {n}")


if __name__ == "__main__":
    main()
