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

# ---- Elo обновление после каждого матча ЧМ (форма по ходу турнира) ----
# eloratings.net стандарт: K=60 для ЧМ, HFA=80 для одной из хозяев (США/Канада/Мексика).
ELO_K = 60
ELO_HFA = 80          # бонус команде-хозяину поля (только если host=1)
DEFAULT_ELO = 1500    # для команд, внезапно пропущенных в wc2026_elo

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
            ADD COLUMN IF NOT EXISTS away_score INT,
            ADD COLUMN IF NOT EXISTS elo_applied BOOLEAN DEFAULT FALSE
        """)
    conn.commit()
    log.info("Ensured home_score/away_score/elo_applied columns exist")


def _expected_score(elo_a: float, elo_b: float) -> float:
    """Ожидаемый результат команды A против B (0..1) по Elo."""
    return 1.0 / (1.0 + 10.0 ** ((elo_b - elo_a) / 400.0))


def _goal_diff_multiplier(gd: int) -> float:
    """Множитель по разнице мячей (World Football Elo)."""
    g = abs(gd)
    if g <= 1: return 1.0
    if g == 2: return 1.5
    return (11.0 + g) / 8.0   # 3:1.75, 4:1.875, 5:2.0 …


def _elo_update_for_match(elo_h: float, elo_a: float, hs: int, as_: int, host: int):
    """Возвращает (new_elo_home, new_elo_away)."""
    # Эффективный Elo хозяина с учётом HFA (если host=1, т.е. хозяева в своёй стране).
    elo_h_eff = elo_h + (ELO_HFA if int(host) == 1 else 0)
    e_home = _expected_score(elo_h_eff, elo_a)
    e_away = 1.0 - e_home

    if hs > as_:   r_home, r_away = 1.0, 0.0
    elif hs < as_: r_home, r_away = 0.0, 1.0
    else:          r_home, r_away = 0.5, 0.5

    g = _goal_diff_multiplier(hs - as_)
    new_h = elo_h + ELO_K * g * (r_home - e_home)
    new_a = elo_a + ELO_K * g * (r_away - e_away)
    return new_h, new_a


def apply_elo_updates(conn):
    """Обновляет wc2026_elo по всем закрытым матчам, где elo_applied=FALSE.
    Проход в хронологическом порядке (match_date → home), чтобы 2-й тур
    использовал рейтинги после 1-го тура. Идемпотентно: флаг elo_applied не даёт удвоить."""
    # Загружаем все текущие Elo в память.
    elo: dict = {}
    with conn.cursor() as cur:
        cur.execute("SELECT team, elo FROM wc2026_elo")
        for t, e in cur.fetchall():
            elo[t] = float(e)

    if not elo:
        log.warning("wc2026_elo пуста — пропускаю Elo обновление")
        return 0

    # Берём все закрытые матчи без применённого Elo, в порядке игры.
    with conn.cursor() as cur:
        cur.execute("""
            SELECT match_date, home, away, home_score, away_score, COALESCE(host, 0)
            FROM wc2026_fixtures
            WHERE home_score IS NOT NULL
              AND away_score IS NOT NULL
              AND COALESCE(elo_applied, FALSE) = FALSE
            ORDER BY match_date, home
        """)
        pending = cur.fetchall()

    if not pending:
        log.info("Elo: нет новых матчей для обновления")
        return 0

    applied = 0
    with conn.cursor() as cur:
        for d, home, away, hs, as_, host in pending:
            eh = elo.get(home, DEFAULT_ELO)
            ea = elo.get(away, DEFAULT_ELO)
            new_h, new_a = _elo_update_for_match(eh, ea, int(hs), int(as_), int(host))
            elo[home] = new_h
            elo[away] = new_a
            # Сохраняем обновленные рейтинги (UPSERT).
            cur.execute(
                "INSERT INTO wc2026_elo (team, elo) VALUES (%s, %s) "
                "ON CONFLICT (team) DO UPDATE SET elo = EXCLUDED.elo",
                (home, new_h),
            )
            cur.execute(
                "INSERT INTO wc2026_elo (team, elo) VALUES (%s, %s) "
                "ON CONFLICT (team) DO UPDATE SET elo = EXCLUDED.elo",
                (away, new_a),
            )
            cur.execute(
                "UPDATE wc2026_fixtures SET elo_applied = TRUE "
                "WHERE match_date = %s AND home = %s AND away = %s",
                (d, home, away),
            )
            applied += 1
            log.info("Elo: %s %d:%d %s | %s: %.0f→%.0f | %s: %.0f→%.0f",
                     home, hs, as_, away, home, eh, new_h, away, ea, new_a)
    conn.commit()
    log.info("Elo: обновлено %d матчей", applied)
    return applied


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

    # Обновляем Elo по всем новым закрытым матчам (форма по ходу ЧМ).
    elo_applied = apply_elo_updates(conn)

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
    print(f"Elo updates applied : {elo_applied}")


if __name__ == "__main__":
    main()
