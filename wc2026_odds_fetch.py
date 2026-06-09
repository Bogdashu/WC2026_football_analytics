"""wc2026_odds_fetch.py - merge live 1X2 odds into wc2026_fixtures.csv.

v4:
  - TRUE failover: if the primary provider returns 0 usable odds (or errors),
    automatically fall back to the next available provider.
      default order: odds-api (if THE_ODDS_API_KEY) -> football-data (if FOOTBALL_DATA_API_KEY)
      --provider X forces a single provider (no failover).
  - date tolerance: matches on API_date, API_date-1, API_date+1
    (handles UTC vs local-host-city date offsets for late kickoffs)
  - aliases are EMBEDDED here (no import from wc2026_predict)
    so the matching is deterministic and easy to debug
  - covers '&', '-', 'and' variants of Bosnia, plus Czechia/Czech Republic,
    USA/United States, Cape Verde Islands, etc.

Providers:
  - the-odds-api.com  (preferred, set THE_ODDS_API_KEY in .env)
  - football-data.org (fallback, free tier has 0 WC odds in practice)

Usage:
  python -X utf8 wc2026_odds_fetch.py --dry-run --verbose
  python -X utf8 wc2026_odds_fetch.py --provider odds-api
  python -X utf8 wc2026_odds_fetch.py --provider football-data
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import sys
import unicodedata
from pathlib import Path
from statistics import median
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests  # type: ignore
except ImportError:
    print("ERROR: 'requests' not installed. Run: pip install requests", file=sys.stderr)
    sys.exit(1)

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except ImportError:
    pass  # dotenv is optional

# ------------------------------------------------------------------ aliases
# Embedded so behavior is deterministic. Each set is a group of equivalent
# names. All names are LOWERCASE + ACCENT-STRIPPED (matches _norm output).
ALIAS_GROUPS: List[set] = [
    {"cote d'ivoire", "ivory coast"},
    {"south korea", "korea republic", "korea"},
    {"north korea", "korea dpr"},
    {"united states", "usa", "united states of america", "u.s.a.", "us"},
    {"iran", "ir iran", "islamic republic of iran"},
    {"cape verde", "cabo verde", "cape verde islands"},
    {"china", "china pr", "china p.r."},
    {"dr congo", "congo dr", "democratic republic of the congo", "democratic republic of congo"},
    {"czechia", "czech republic"},
    {"turkey", "turkiye"},
    {"bosnia and herzegovina", "bosnia-herzegovina", "bosnia & herzegovina", "bosnia"},
    {"republic of ireland", "ireland"},
    {"saudi arabia", "ksa"},
    {"new zealand", "nz"},
]


def _norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().strip()
    s = s.replace("\u2019", "'").replace("`", "'")
    return s


ALIAS_IDX: Dict[str, str] = {}
for _grp in ALIAS_GROUPS:
    _canon = sorted(_grp)[0]
    for _name in _grp:
        ALIAS_IDX[_norm(_name)] = _canon


def canonical(name: str) -> str:
    n = _norm(name)
    return ALIAS_IDX.get(n, n)


# ------------------------------------------------------------------ fixtures
def load_fixtures(path: str) -> List[Dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    for r in rows:
        r["date"] = (r.get("date") or "")[:10]
    return rows


def save_fixtures(path: str, rows: List[Dict[str, Any]]) -> None:
    cols = ["date", "home", "away", "host", "odds_1", "odds_x", "odds_2"]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=cols)
        writer.writeheader()
        for r in rows:
            writer.writerow({c: r.get(c, "") for c in cols})


def build_fixture_index(rows: List[Dict[str, Any]]) -> Dict[Tuple[str, str, str], int]:
    """Index (date, canonical_home, canonical_away) -> row index."""
    idx = {}
    for i, r in enumerate(rows):
        k = (r["date"], canonical(r.get("home", "")), canonical(r.get("away", "")))
        idx[k] = i
    return idx


def date_shift(date_iso: str, days: int) -> str:
    d = dt.date.fromisoformat(date_iso) + dt.timedelta(days=days)
    return d.isoformat()


def find_fixture(idx: Dict[Tuple[str, str, str], int],
                 api_date: str, api_home: str, api_away: str
                 ) -> Tuple[Optional[int], bool, int]:
    """Try (date+delta, home, away) for delta in (0, -1, +1) and flipped.
    Returns (row_index_or_None, flipped, delta_used).
    """
    ch, ca = canonical(api_home), canonical(api_away)
    for delta in (0, -1, 1):
        d = date_shift(api_date, delta)
        i = idx.get((d, ch, ca))
        if i is not None:
            return i, False, delta
        i = idx.get((d, ca, ch))
        if i is not None:
            return i, True, delta
    return None, False, 0


# ------------------------------------------------------------------ providers
def fetch_odds_api(api_key: str) -> List[Dict[str, Any]]:
    url = "https://api.the-odds-api.com/v4/sports/soccer_fifa_world_cup/odds"
    params = {
        "apiKey": api_key,
        "regions": "eu,uk,us",
        "markets": "h2h",
        "oddsFormat": "decimal",
        "dateFormat": "iso",
    }
    print(f"GET {url}  (provider: the-odds-api.com)")
    r = requests.get(url, params=params, timeout=30)
    if r.status_code == 422:
        print(f"NOTE: 422 from odds-api - sport key likely not yet active. body: {r.text[:300]}")
        return []
    r.raise_for_status()
    used = r.headers.get("x-requests-used", "?")
    rem = r.headers.get("x-requests-remaining", "?")
    print(f"quota used: {used}  remaining: {rem}")
    return r.json()


def parse_odds_api(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert odds-api raw events to normalized [{date, home, away, o1, ox, o2}]."""
    out = []
    for ev in events:
        commence = ev.get("commence_time", "")
        date_iso = commence[:10] if commence else ""
        home = ev.get("home_team", "") or ""
        away = ev.get("away_team", "") or ""
        h_prices: List[float] = []
        d_prices: List[float] = []
        a_prices: List[float] = []
        for bk in ev.get("bookmakers", []):
            for mkt in bk.get("markets", []):
                if mkt.get("key") != "h2h":
                    continue
                for oc in mkt.get("outcomes", []):
                    name = oc.get("name", "")
                    price = oc.get("price")
                    if price is None:
                        continue
                    if _norm(name) == _norm(home):
                        h_prices.append(float(price))
                    elif _norm(name) == _norm(away):
                        a_prices.append(float(price))
                    elif _norm(name) == "draw":
                        d_prices.append(float(price))
        if not (h_prices and d_prices and a_prices):
            continue
        out.append({
            "date_iso": date_iso,
            "home": home,
            "away": away,
            "odds_1": round(median(h_prices), 3),
            "odds_x": round(median(d_prices), 3),
            "odds_2": round(median(a_prices), 3),
        })
    return out


def fetch_football_data(api_key: str) -> List[Dict[str, Any]]:
    url = "https://api.football-data.org/v4/competitions/WC/matches"
    print(f"GET {url}  (provider: football-data.org)")
    r = requests.get(url, headers={"X-Auth-Token": api_key}, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get("matches", [])


def parse_football_data(matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for m in matches:
        ud = m.get("utcDate", "")
        date_iso = ud[:10] if ud else ""
        home = (m.get("homeTeam") or {}).get("name", "") or ""
        away = (m.get("awayTeam") or {}).get("name", "") or ""
        odds = m.get("odds") or {}
        if not (odds.get("homeWin") and odds.get("draw") and odds.get("awayWin")):
            continue
        out.append({
            "date_iso": date_iso,
            "home": home,
            "away": away,
            "odds_1": float(odds["homeWin"]),
            "odds_x": float(odds["draw"]),
            "odds_2": float(odds["awayWin"]),
        })
    return out


# ------------------------------------------------------------------ provider dispatch
def get_events(provider: str, odds_key: str, fd_key: str) -> List[Dict[str, Any]]:
    """Fetch + parse one provider. Returns normalized events (may be empty).
    Raises on hard network/HTTP errors so the caller can fail over."""
    if provider == "odds-api":
        if not odds_key:
            print("skip odds-api: THE_ODDS_API_KEY not set")
            return []
        raw = fetch_odds_api(odds_key)
        events = parse_odds_api(raw)
        print(f"odds-api -> {len(raw)} raw events, {len(events)} with usable 1X2")
        return events
    elif provider == "football-data":
        if not fd_key:
            print("skip football-data: FOOTBALL_DATA_API_KEY not set")
            return []
        raw = fetch_football_data(fd_key)
        events = parse_football_data(raw)
        print(f"football-data -> {len(raw)} matches, {len(events)} with usable 1X2")
        return events
    else:
        print(f"unknown provider: {provider}")
        return []


def collect_events(order: List[str], odds_key: str, fd_key: str
                   ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Try providers in order; return (events, used_provider) for the first one
    that yields >=1 usable event. Network/HTTP errors on a provider are caught
    and treated as 'unavailable' so we fail over to the next."""
    for i, prov in enumerate(order):
        is_last = (i == len(order) - 1)
        try:
            events = get_events(prov, odds_key, fd_key)
        except Exception as e:
            print(f"provider '{prov}' FAILED: {e.__class__.__name__}: {e}")
            events = []
        if events:
            return events, prov
        if not is_last:
            print(f"provider '{prov}' gave 0 usable odds -> failing over to '{order[i + 1]}'")
        else:
            print(f"provider '{prov}' gave 0 usable odds (no more providers to try)")
    return [], None


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", default="wc2026_fixtures.csv")
    ap.add_argument("--out", default=None,
                    help="Write to this path instead of --fixtures")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--provider", choices=["odds-api", "football-data"], default=None,
                    help="Force a single provider (disables failover). "
                         "Default: try odds-api then football-data by available keys")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="List every unmatched API event")
    args = ap.parse_args()

    odds_key = os.environ.get("THE_ODDS_API_KEY", "").strip()
    fd_key = os.environ.get("FOOTBALL_DATA_API_KEY", "").strip()

    # build provider order (with failover) or a single forced provider
    if args.provider:
        order = [args.provider]
    else:
        order = []
        if odds_key:
            order.append("odds-api")
        if fd_key:
            order.append("football-data")
    if not order:
        print("ERROR: no API keys set (need THE_ODDS_API_KEY and/or FOOTBALL_DATA_API_KEY)",
              file=sys.stderr)
        sys.exit(1)

    print(f"provider order: {' -> '.join(order)}")
    events, used_provider = collect_events(order, odds_key, fd_key)
    print(f"using provider: {used_provider or 'none (no odds from any source)'}")

    fixtures = load_fixtures(args.fixtures)
    print(f"loaded {len(fixtures)} fixtures from {args.fixtures}")
    idx = build_fixture_index(fixtures)

    updated = 0
    unmatched: List[Tuple[str, str, str]] = []
    matched_with_offset = 0
    for ev in events:
        row_i, flipped, delta = find_fixture(idx, ev["date_iso"], ev["home"], ev["away"])
        if row_i is None:
            unmatched.append((ev["date_iso"], ev["home"], ev["away"]))
            continue
        if delta != 0:
            matched_with_offset += 1
        o1, ox, o2 = ev["odds_1"], ev["odds_x"], ev["odds_2"]
        if flipped:
            o1, o2 = o2, o1  # swap to match fixtures' home/away orientation
        fixtures[row_i]["odds_1"] = f"{o1:.3f}"
        fixtures[row_i]["odds_x"] = f"{ox:.3f}"
        fixtures[row_i]["odds_2"] = f"{o2:.3f}"
        updated += 1

    print("\n================ Summary ================")
    print(f"provider used                : {used_provider or 'none'}")
    print(f"events from API              : {len(events)}")
    print(f"fixtures updated with odds   : {updated}")
    print(f"  matched with date offset   : {matched_with_offset}  (UTC vs local-host)")
    print(f"unmatched (date/team)        : {len(unmatched)}")

    if unmatched:
        cap = 999 if args.verbose else 8
        print(f"\nUnmatched API events (showing {min(len(unmatched), cap)} of {len(unmatched)}):")
        for d, h, a in unmatched[:cap]:
            print(f"  {d}  {h} vs {a}")
        if len(unmatched) > cap and not args.verbose:
            print(f"  ... ({len(unmatched) - cap} more, run with --verbose for full list)")

    if args.dry_run:
        print("\n[DRY RUN] No files written. Re-run without --dry-run to save.")
        return

    if updated == 0:
        print("\nNo odds matched -> leaving fixtures file unchanged.")
        return

    out_path = args.out or args.fixtures
    save_fixtures(out_path, fixtures)
    print(f"\nWrote {len(fixtures)} fixtures to {out_path}")


if __name__ == "__main__":
    main()
