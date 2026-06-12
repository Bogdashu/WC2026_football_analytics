"""wc2026_predict.py - print a CALIBRATED 1X2 prediction for one or many matches.

Loads the artifacts your training step already writes:
    wc2026_elo.csv        (team ratings)
    wc2026_goalmodel.json (goal model params)
    wc2026_calibrator.json(3-way calibrator from wc2026_eval.py; optional)
and reuses wc2026_model.predict(), then applies calibrator.apply_dict().

Usage:
    python wc2026_predict.py "Spain" "Colombia"            # one match (home/away)
    python wc2026_predict.py "Spain" "Colombia" --neutral  # neutral venue (WC group/KO)
    python wc2026_predict.py --fixtures wc2026_fixtures.csv --neutral
    python wc2026_predict.py "Spain" "Colombia" --raw      # also show pre-calibration probs

Why a separate file: it leaves wc2026_daily.py untouched. If you'd rather wire it
in-place, the same 2 lines (calibrator.load once + calibrator.apply_dict after
predict) drop into your existing predict mode - see the README block at the bottom.
"""
import os
import sys
import csv
import json
import argparse
import difflib
import unicodedata

import wc2026_model as M
import calibrator

HERE = os.path.dirname(os.path.abspath(__file__))


def _p(name):
    return os.path.join(HERE, name)


def load_ratings(path):
    """Read wc2026_elo.csv into {team: rating}. Auto-detects team/rating columns."""
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise SystemExit(f"{path} is empty")
    cols = list(rows[0].keys())

    def pick(cands, fallback_numeric):
        for c in cands:
            for col in cols:
                if col.strip().lower() == c:
                    return col
        # fallback: first column that is (non-)numeric
        for col in cols:
            v = rows[0][col]
            is_num = _is_float(v)
            if is_num == fallback_numeric:
                return col
        return cols[0]

    team_col = pick(["team", "team_name", "name", "country"], fallback_numeric=False)
    rate_col = pick(["elo", "rating", "r", "score"], fallback_numeric=True)
    ratings = {}
    for r in rows:
        t = (r[team_col] or "").strip()
        if not t:
            continue
        try:
            ratings[t] = float(r[rate_col])
        except (TypeError, ValueError):
            pass
    if not ratings:
        raise SystemExit(f"could not parse ratings from {path} (team={team_col}, rating={rate_col})")
    return ratings, team_col, rate_col


def _is_float(v):
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


class UnknownTeam(Exception):
    pass


# Common FIFA-vs-dataset name variants. Each group lists equivalent spellings;
# if the fixture uses one and wc2026_elo.csv uses another, we map between them.
ALIAS_GROUPS = [
    {"cote d'ivoire", "ivory coast"},
    {"south korea", "korea republic", "korea"},
    {"north korea", "korea dpr"},
    {"united states", "usa", "united states of america"},
    {"iran", "ir iran"},
    {"cape verde", "cabo verde"},
    {"china", "china pr"},
    {"dr congo", "congo dr", "democratic republic of the congo"},
    {"czechia", "czech republic"},
    {"turkey", "turkiye"},
    {"bosnia and herzegovina", "bosnia-herzegovina", "bosnia"},
    {"republic of ireland", "ireland"},
]


def _norm(s):
    """Lowercase, strip accents and curly apostrophes - for tolerant matching."""
    s = (s or "").replace("\u2019", "'").strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower()


_IDX = None


def _index(ratings):
    """Map normalized team name -> canonical key in wc2026_elo.csv (built once)."""
    global _IDX
    if _IDX is None:
        _IDX = {_norm(k): k for k in ratings}
    return _IDX


def resolve(name, ratings):
    """Exact, case/accent-insensitive, alias, then fuzzy match. Raise UnknownTeam."""
    if name in ratings:
        return name
    idx = _index(ratings)
    n = _norm(name)
    if n in idx:
        return idx[n]
    # alias groups (e.g. Cote d'Ivoire <-> Ivory Coast)
    for group in ALIAS_GROUPS:
        if n in group:
            for alt in group:
                if alt in idx:
                    print(f"  (note: '{name}' -> '{idx[alt]}')")
                    return idx[alt]
    # fuzzy on normalized names (handles typos)
    near = difflib.get_close_matches(n, list(idx), n=1, cutoff=0.6)
    if near:
        if idx[near[0]].lower() != name.lower():
            print(f"  (note: '{name}' -> '{idx[near[0]]}')")
        return idx[near[0]]
    raise UnknownTeam(name)


def fair_odds(p):
    return "-" if p <= 0 else f"{1.0 / p:.2f}"


def market_probs(spec):
    """Convert decimal odds '1,X,2' (e.g. '2.10,3.40,3.50') to fair probabilities.

    Removes the bookmaker overround by normalizing 1/odds to sum to 1.
    Returns {p_home, p_draw, p_away, overround_pct}.
    """
    if spec is None:
        return None
    parts = [s.strip() for s in str(spec).split(",")]
    if len(parts) != 3:
        raise ValueError(f"--market expects 3 decimal odds '1,X,2', got {spec!r}")
    try:
        o1, ox, o2 = (float(x) for x in parts)
    except ValueError:
        raise ValueError(f"--market values must be decimal odds, got {spec!r}")
    if min(o1, ox, o2) <= 1.0:
        raise ValueError(f"--market odds must be > 1.0, got {spec!r}")
    raw = (1.0 / o1, 1.0 / ox, 1.0 / o2)
    s = sum(raw)
    return {
        "p_home": raw[0] / s,
        "p_draw": raw[1] / s,
        "p_away": raw[2] / s,
        "overround_pct": (s - 1.0) * 100.0,
    }


def blend_with_market(cal, market, weight):
    """Linear opinion pool: (1-w)*model + w*market, renormalized for safety."""
    w = max(0.0, min(1.0, float(weight)))
    out = {
        "p_home": (1.0 - w) * cal["p_home"] + w * market["p_home"],
        "p_draw": (1.0 - w) * cal["p_draw"] + w * market["p_draw"],
        "p_away": (1.0 - w) * cal["p_away"] + w * market["p_away"],
    }
    s = out["p_home"] + out["p_draw"] + out["p_away"]
    if s > 0:
        out = {k: v / s for k, v in out.items()}
    return out


def predict_one(home, away, ratings, gm, calib, neutral, show_raw, market=None, weight=0.5):
    try:
        h = resolve(home, ratings)
        a = resolve(away, ratings)
    except UnknownTeam as e:
        print(f"\n{home}  vs  {away}\n  skip: unknown team '{e}' (not in wc2026_elo.csv "
              f"- likely a TBD/playoff placeholder)")
        return None
    raw = M.predict(h, a, ratings, gm, neutral=neutral)
    cal = calibrator.apply_dict(raw, calib)
    venue = "neutral" if neutral else f"{h} home"
    print(f"\n{h}  vs  {away if a == away else a}   ({venue})")
    if show_raw:
        print("  raw   : "
              f"1={raw['p_home']:.3f}  X={raw['p_draw']:.3f}  2={raw['p_away']:.3f}")
    print("  model : "
          f"1={cal['p_home']:.3f}  X={cal['p_draw']:.3f}  2={cal['p_away']:.3f}    "
          f"fair 1={fair_odds(cal['p_home'])} X={fair_odds(cal['p_draw'])} 2={fair_odds(cal['p_away'])}")
    if market is None:
        return cal
    print("  market: "
          f"1={market['p_home']:.3f}  X={market['p_draw']:.3f}  2={market['p_away']:.3f}    "
          f"(overround {market['overround_pct']:+.1f}%)")
    blend = blend_with_market(cal, market, weight)
    print(f"  BLEND : "
          f"1={blend['p_home']:.3f}  X={blend['p_draw']:.3f}  2={blend['p_away']:.3f}    "
          f"fair 1={fair_odds(blend['p_home'])} X={fair_odds(blend['p_draw'])} 2={fair_odds(blend['p_away'])}    "
          f"[w_market={weight:.2f}]")
    return blend


def read_fixtures(path):
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise SystemExit(f"{path} is empty")
    cols = {c.lower(): c for c in rows[0].keys()}
    hc = cols.get("home_team") or cols.get("home")
    ac = cols.get("away_team") or cols.get("away")
    if not hc or not ac:
        raise SystemExit(f"{path} needs home_team/away_team columns (got {list(rows[0])})")
    o1c = cols.get("odds_1") or cols.get("home_odds") or cols.get("1")
    oxc = cols.get("odds_x") or cols.get("draw_odds") or cols.get("x")
    o2c = cols.get("odds_2") or cols.get("away_odds") or cols.get("2")
    has_odds = bool(o1c and oxc and o2c)
    host_c = cols.get("host")
    neut_c = cols.get("neutral")
    has_venue = bool(host_c or neut_c)
    out = []
    for r in rows:
        if not (r.get(hc) and r.get(ac)):
            continue
        spec = None
        if has_odds and r.get(o1c) and r.get(oxc) and r.get(o2c):
            spec = f"{r[o1c]},{r[oxc]},{r[o2c]}"
        neutral_override = None
        if host_c and str(r.get(host_c, "")).strip() != "":
            try:
                neutral_override = (int(str(r[host_c]).strip()) == 0)
            except ValueError:
                neutral_override = None
        elif neut_c and str(r.get(neut_c, "")).strip() != "":
            v = str(r[neut_c]).strip().lower()
            if v in ("1", "true", "yes", "y", "t"):
                neutral_override = True
            elif v in ("0", "false", "no", "n", "f"):
                neutral_override = False
        out.append((r[hc], r[ac], spec, neutral_override))
    return out, has_odds, has_venue


def main():
    ap = argparse.ArgumentParser(description="Calibrated 1X2 predictions for WC2026.")
    ap.add_argument("home", nargs="?", help="home team")
    ap.add_argument("away", nargs="?", help="away team")
    ap.add_argument("--neutral", action="store_true", help="neutral venue (no home edge)")
    ap.add_argument("--fixtures", help="CSV with home_team/away_team (optionally odds_1/odds_x/odds_2) columns")
    ap.add_argument("--elo", default=_p("wc2026_elo.csv"))
    ap.add_argument("--goalmodel", default=_p("wc2026_goalmodel.json"))
    ap.add_argument("--calibrator", default=_p("wc2026_calibrator.json"))
    ap.add_argument("--xg-blend", type=float, default=None,
                    help="override M.XG_BLEND (recommended 0.20 from ablation)")
    ap.add_argument("--raw", action="store_true", help="also print pre-calibration probs")
    ap.add_argument("--market", default=None,
                    help="bookmaker decimal odds '1,X,2' (e.g. 2.10,3.40,3.50) - blends model with market")
    ap.add_argument("--market-weight", type=float, default=0.5,
                    help="weight on market in linear blend [0..1], default 0.5")
    args = ap.parse_args()

    if args.xg_blend is not None:
        M.XG_BLEND = args.xg_blend

    ratings, tcol, rcol = load_ratings(args.elo)
    with open(args.goalmodel, encoding="utf-8") as f:
        gm = json.load(f)
    calib = calibrator.load(args.calibrator)

    print(f"loaded {len(ratings)} teams from {os.path.basename(args.elo)} "
          f"(team='{tcol}', rating='{rcol}')")
    if calib:
        ct = calib.get("type", "vector")
        detail = ("b=%.3f" % calib["b"]) if "b" in calib else (
            "lam=%.2f" % calib["lam"] if ct == "shrinkage" else "")
        cal_status = f"ON ({ct}{', ' + detail if detail else ''})"
    else:
        cal_status = "OFF (file missing)"
    print(f"goal model: {os.path.basename(args.goalmodel)}   "
          f"calibrator: {cal_status}   "
          f"XG_BLEND={getattr(M, 'XG_BLEND', 'n/a')}")

    cli_market = market_probs(args.market) if args.market else None
    w = args.market_weight

    if args.fixtures:
        fixtures, has_odds, has_venue = read_fixtures(args.fixtures)
        if has_odds:
            print(f"  market columns detected in {os.path.basename(args.fixtures)} "
                  f"-> blending model with market (w_market={w:.2f})")
        elif cli_market is not None:
            print(f"  CLI --market applied to every fixture (w_market={w:.2f})")
        if has_venue:
            print("  venue column detected (host/neutral) -> per-row override; CLI --neutral is fallback")
        for home, away, spec, neut_override in fixtures:
            m = market_probs(spec) if spec else cli_market
            n = args.neutral if neut_override is None else neut_override
            predict_one(home, away, ratings, gm, calib, n, args.raw,
                        market=m, weight=w)
    elif args.home and args.away:
        predict_one(args.home, args.away, ratings, gm, calib, args.neutral, args.raw,
                    market=cli_market, weight=w)
    else:
        ap.error("give HOME and AWAY, or --fixtures FILE")


if __name__ == "__main__":
    main()
