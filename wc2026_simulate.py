"""wc2026_simulate.py - Monte Carlo full-tournament projection for WC 2026.

Reads:
    wc2026_elo.csv           team ratings produced by wc2026_model.py
    wc2026_goalmodel.json    goal model produced by wc2026_model.py
    wc2026_calibrator.json   3-way calibrator produced by wc2026_improve.py
    wc2026_fixtures.csv      group-stage fixtures (date,home,away,host[,odds_*])

For each of N simulations:
    1. Sample every group-stage match -> goals -> standings
    2. Pick top 2 from each group + best 8 third-placed teams -> 32 teams
    3. Simulate knockout R32 -> R16 -> QF -> SF -> F -> Champion
       (regulation draws resolved via Elo-edge proxy for ET/penalties)
    4. Record each team's furthest round reached

Outputs (stdout + JSON file):
    - per-team group-standing probabilities P(1st/2nd/3rd/4th)
    - tournament-wide P(R32, R16, QF, SF, Final, Champion)
    - a single 'modal' bracket: model's best single guess from groups to final
    - wc2026_baseline.json: full snapshot for post-tournament comparison

Usage:
    python wc2026_simulate.py                       # 10000 sims, default paths
    python wc2026_simulate.py --sims 50000          # more precision
    python wc2026_simulate.py --out my_baseline.json
"""
import os
import sys
import json
import csv
import argparse
import unicodedata
from collections import defaultdict, Counter
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import wc2026_model as M
import calibrator
from wc2026_predict import resolve as _resolve_team, UnknownTeam

HERE = os.path.dirname(os.path.abspath(__file__))


def _p(name):
    return os.path.join(HERE, name)


# --------------------------------- loaders ---------------------------------

def load_ratings(path):
    df = pd.read_csv(path)
    cols_lower = {c.lower(): c for c in df.columns}
    tcol = cols_lower.get("team") or df.columns[0]
    rcol = cols_lower.get("elo") or cols_lower.get("rating") or df.columns[1]
    return {str(r[tcol]).strip(): float(r[rcol]) for _, r in df.iterrows()}


def load_fixtures(path):
    df = pd.read_csv(path)
    cols = {c.lower(): c for c in df.columns}
    hc = cols.get("home_team") or cols.get("home")
    ac = cols.get("away_team") or cols.get("away")
    hostc = cols.get("host")
    hsc = cols.get("home_score") or cols.get("hs")
    asc = cols.get("away_score") or cols.get("as") or cols.get("as_")
    rows = []
    for _, r in df.iterrows():
        h, a = str(r[hc]).strip(), str(r[ac]).strip()
        if not h or not a:
            continue
        host = 0
        if hostc is not None and pd.notna(r[hostc]):
            try:
                host = int(r[hostc])
            except (TypeError, ValueError):
                host = 0
        # Фиксируем сыгранные матчи: если счёт есть — симулятор его не разыгрывает.
        hs, as_ = None, None
        if hsc is not None and pd.notna(r[hsc]):
            try: hs = int(r[hsc])
            except (TypeError, ValueError): hs = None
        if asc is not None and pd.notna(r[asc]):
            try: as_ = int(r[asc])
            except (TypeError, ValueError): as_ = None
        rows.append({"home": h, "away": a, "host": host, "hs": hs, "as_": as_})
    return rows


# ---------------------- groups via union-find on fixtures ------------------
# Official FIFA WC 2026 group letters. Used to label each derived bucket with
# its real group letter rather than the previous alphabetic-by-min-name fallback
# (which scrambled labels, e.g. labelling Argentina's group as A instead of J).
OFFICIAL_GROUPS = {
    "Mexico": "A", "South Africa": "A", "South Korea": "A", "Czech Republic": "A",
    "Canada": "B", "Bosnia and Herzegovina": "B", "Qatar": "B", "Switzerland": "B",
    "Brazil": "C", "Morocco": "C", "Haiti": "C", "Scotland": "C",
    "United States": "D", "Paraguay": "D", "Australia": "D", "Turkey": "D",
    "Germany": "E", "Cura\u00e7ao": "E", "Ivory Coast": "E", "Ecuador": "E",
    "Netherlands": "F", "Japan": "F", "Sweden": "F", "Tunisia": "F",
    "Belgium": "G", "Egypt": "G", "Iran": "G", "New Zealand": "G",
    "Spain": "H", "Cape Verde": "H", "Saudi Arabia": "H", "Uruguay": "H",
    "France": "I", "Senegal": "I", "Iraq": "I", "Norway": "I",
    "Argentina": "J", "Algeria": "J", "Austria": "J", "Jordan": "J",
    "Portugal": "K", "DR Congo": "K", "Uzbekistan": "K", "Colombia": "K",
    "England": "L", "Croatia": "L", "Ghana": "L", "Panama": "L",
}


def _norm_group_name(s):
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower().strip()


_OFFICIAL_GROUPS_NORM = {_norm_group_name(k): v for k, v in OFFICIAL_GROUPS.items()}
# common aliases seen across different data sources
for _alias, _canon in (
    ("cote d'ivoire", "Ivory Coast"),
    ("c\u00f4te d'ivoire", "Ivory Coast"),
    ("czechia", "Czech Republic"),
    ("usa", "United States"),
    ("us", "United States"),
    ("korea republic", "South Korea"),
    ("republic of korea", "South Korea"),
    ("bosnia", "Bosnia and Herzegovina"),
    ("bosnia & herzegovina", "Bosnia and Herzegovina"),
    ("curacao", "Cura\u00e7ao"),
    ("dr congo", "DR Congo"),
    ("democratic republic of the congo", "DR Congo"),
    ("congo dr", "DR Congo"),
    ("cape verde islands", "Cape Verde"),
    ("saudi", "Saudi Arabia"),
):
    _v = _OFFICIAL_GROUPS_NORM.get(_norm_group_name(_canon))
    if _v:
        _OFFICIAL_GROUPS_NORM[_norm_group_name(_alias)] = _v


def derive_groups(fixtures):
    parent = {}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    teams = set()
    for f in fixtures:
        teams.add(f["home"])
        teams.add(f["away"])
    for t in teams:
        parent[t] = t
    for f in fixtures:
        union(f["home"], f["away"])
    buckets = defaultdict(list)
    for t in teams:
        buckets[find(t)].append(t)
    # Label each bucket with its official FIFA letter (majority vote across
    # known members). Buckets with no recognised members fall back to the next
    # free letter in alphabetical bucket order.
    labelled = {}
    unlabelled = []
    for ts in buckets.values():
        votes = Counter(
            _OFFICIAL_GROUPS_NORM[k]
            for k in (_norm_group_name(t) for t in ts)
            if k in _OFFICIAL_GROUPS_NORM
        )
        placed = False
        for letter, _ in votes.most_common():
            if letter not in labelled:
                labelled[letter] = sorted(ts)
                placed = True
                break
        if not placed:
            unlabelled.append(sorted(ts))
    used = set(labelled)
    for ts in sorted(unlabelled, key=lambda xs: min(xs)):
        for i in range(26):
            c = chr(ord("A") + i)
            if c not in used:
                labelled[c] = ts
                used.add(c)
                break
    return dict(sorted(labelled.items()))


# --------------------- match sampling (uses real model) --------------------

def ensure_elo(team, ratings, default_elo):
    if team not in ratings:
        ratings[team] = default_elo


def match_probs(home, away, ratings, gm, calib, neutral, default_elo):
    ensure_elo(home, ratings, default_elo)
    ensure_elo(away, ratings, default_elo)
    raw = M.predict(home, away, ratings, gm, neutral=neutral)
    return calibrator.apply_dict(raw, calib)


def _lambdas(home, away, ratings, gm, neutral, default_elo):
    ensure_elo(home, ratings, default_elo)
    ensure_elo(away, ratings, default_elo)
    # mirror wc2026_model.predict edge computation; xG blend (if any) is
    # already inside M.predict but we recompute lambdas directly here to drive
    # the score-matrix sampler. Slight inconsistency vs. xG blend is OK for MC.
    rh, ra = ratings[home], ratings[away]
    hfa = 0.0 if neutral else getattr(M, "HOME_ADV_ELO", 90.0)
    edge = (rh + hfa) - ra
    return M.lambdas(edge, gm)


def sample_score(home, away, ratings, gm, neutral, rng, default_elo):
    lh, la = _lambdas(home, away, ratings, gm, neutral, default_elo)
    rho = float(gm.get("rho", -0.04))
    Mtx = M._score_matrix(lh, la, rho)
    flat = np.clip(np.asarray(Mtx, dtype=float).flatten(), 0.0, None)
    s = flat.sum()
    if s <= 0:
        return 0, 0
    flat = flat / s
    idx = rng.choice(flat.size, p=flat)
    n = Mtx.shape[0]
    return int(idx // n), int(idx % n)


# --------------- knockout "last dance" / surprise factors ------------------
# Veteran superstars chasing a glorious career finale lift their team a notch
# in DECISIVE (knockout) games only. Values are Elo-equivalent bonuses, scaled
# by round (bigger as the stage gets bigger) and by the --legend-factor flag.
# Teams not listed get nothing; teams not in this WC are simply never matched.
LEGEND_ELO = {
    "Argentina": 45.0,  # Messi — почти наверняка последний ЧМ
    "Portugal":  45.0,  # Ronaldo — прощальный турнир
    "Croatia":   40.0,  # Modric — последний танец
    "Brazil":    35.0,  # Neymar — последний шанс на титул
    "France":    25.0,  # Mbappe — в ��оп-форме, гонится за величием
    "Egypt":     20.0,  # Salah — последний большой шанс
    "Poland":    18.0,  # Lewandowski — закрыть карьеру красиво
}
# Decisiveness multiplier: the later the round, the stronger the legacy pull.
# (Round labels: R16=1/16, QF=1/8, SF=1/4, F=1/2, W=final match.)
KO_ROUND_MULT = {"R16": 0.40, "QF": 0.60, "SF": 0.80, "F": 1.00, "W": 1.40}


def _legend_bonus(team, round_name, legend_factor):
    """Elo-equivalent 'last dance' bonus for a team in a given knockout round."""
    if legend_factor <= 0.0:
        return 0.0
    return LEGEND_ELO.get(team, 0.0) * KO_ROUND_MULT.get(round_name, 1.0) * legend_factor


def sample_knockout(home, away, ratings, gm, calib, rng, default_elo,
                    round_name="R16", legend_factor=0.0, surprise=0.0):
    """Sample a knockout winner. Regulation draws -> resolve via Elo edge proxy.

    The Elo-edge proxy approximates ET + penalties: the higher-rated team
    wins a regulation draw with p = 0.5 + 0.0005 * (rating_h - rating_a),
    clipped to [0.25, 0.75]. Magnitudes match historical KO data.

    Two optional DECISIVE-MATCH flavour knobs (both OFF by default, so the
    frozen pre-tournament baseline stays byte-for-byte reproducible):
      * legend_factor: veteran-superstar 'last dance' lift, knockout only,
        scaled up in later rounds via KO_ROUND_MULT.
      * surprise: small per-match chance of a giant-killing upset where the
        underdog's win share is amplified — keeps things probabilistic
        (never a hard 'X always beats Y').
    """
    ensure_elo(home, ratings, default_elo)
    ensure_elo(away, ratings, default_elo)
    hb = _legend_bonus(home, round_name, legend_factor)
    ab = _legend_bonus(away, round_name, legend_factor)
    oh, oa = ratings[home], ratings[away]
    if hb:
        ratings[home] = oh + hb
    if ab:
        ratings[away] = oa + ab
    try:
        p = match_probs(home, away, ratings, gm, calib, neutral=True, default_elo=default_elo)
        ph, pa = p["p_home"], p["p_away"]
        # Occasional giant-killing: amplify the underdog's win share.
        if surprise > 0.0 and rng.random() < surprise:
            if ph >= pa:
                shift = min(0.25, pa * 0.8)
                ph -= shift; pa += shift
            else:
                shift = min(0.25, ph * 0.8)
                pa -= shift; ph += shift
        u = rng.random()
        if u < ph:
            return home
        if u < ph + pa:
            return away
        edge = ratings[home] - ratings[away]
        p_home = max(0.25, min(0.75, 0.5 + 0.0005 * edge))
        return home if rng.random() < p_home else away
    finally:
        ratings[home] = oh
        ratings[away] = oa


# ------------------------------ group stage --------------------------------

def sim_group(teams, gfixtures, ratings, gm, calib, rng, default_elo):
    tbl = {t: {"pts": 0, "gf": 0, "ga": 0} for t in teams}
    for f in gfixtures:
        neutral = (f["host"] == 0)
        # Сыгранный матч — берем реальный счёт как факт, иначе семплируем.
        if f.get("hs") is not None and f.get("as_") is not None:
            hg, ag = int(f["hs"]), int(f["as_"])
        else:
            hg, ag = sample_score(f["home"], f["away"], ratings, gm, neutral, rng, default_elo)
        h, a = f["home"], f["away"]
        tbl[h]["gf"] += hg
        tbl[h]["ga"] += ag
        tbl[a]["gf"] += ag
        tbl[a]["ga"] += hg
        if hg > ag:
            tbl[h]["pts"] += 3
        elif hg < ag:
            tbl[a]["pts"] += 3
        else:
            tbl[h]["pts"] += 1
            tbl[a]["pts"] += 1
    for t in teams:
        tbl[t]["gd"] = tbl[t]["gf"] - tbl[t]["ga"]
    standings = sorted(teams, key=lambda t: (
        -tbl[t]["pts"], -tbl[t]["gd"], -tbl[t]["gf"], rng.random()
    ))
    return standings, tbl


# ------------------------- FIFA-2026 R32 bracket ---------------------------
# Official FIFA-2026 bracket for the Round of 32 (12 winners + 12 runners-up + 8 best thirds).
# Each entry is a pair (slot_a, slot_b) of teams that face each other in R32.
# Listed in bracket order — adjacent pairs meet in R16, etc. (left half then right half).
# Slot encoding:
#   "1X" → winner of group X
#   "2X" → runner-up of group X
#   "3X" → one of the 8 best third-placed teams, allocated to this slot by FIFA matrix
# Sources: ESPN bracket map + FIFA-2026 regulations.
FIFA_R32_SLOTS = [
    # ---- LEFT HALF (top → bottom) ----
    ("1E", "3rd_ABCDF"),
    ("1I", "3rd_CDFGH"),
    ("2A", "2B"),
    ("1F", "2C"),
    ("2K", "2L"),
    ("1H", "2J"),
    ("1D", "3rd_BEFIJ"),
    ("1G", "3rd_AEHIJ"),
    # ---- RIGHT HALF (top → bottom) ----
    ("1C", "2F"),
    ("2E", "2I"),
    ("1A", "3rd_CEFHI"),
    ("1L", "3rd_EHIJK"),
    ("1J", "2H"),
    ("2D", "2G"),
    ("1B", "3rd_EFGIJ"),
    ("1K", "3rd_DEIJL"),
]

# Each "3rd_*" slot can be filled only by a third-placed team coming from one of these groups
# (FIFA's official third-place allocation matrix — ensures no group-stage rematches in R32).
FIFA_3RD_ALLOWED = {
    "3rd_ABCDF": frozenset("ABCDF"),
    "3rd_CDFGH": frozenset("CDFGH"),
    "3rd_BEFIJ": frozenset("BEFIJ"),
    "3rd_AEHIJ": frozenset("AEHIJ"),
    "3rd_CEFHI": frozenset("CEFHI"),
    "3rd_EHIJK": frozenset("EHIJK"),
    "3rd_EFGIJ": frozenset("EFGIJ"),
    "3rd_DEIJL": frozenset("DEIJL"),
}


def _assign_thirds_to_slots(best_thirds_with_g, rng):
    """Greedy assignment of 8 best thirds to 8 FIFA bracket slots.

    best_thirds_with_g: list of (group_letter, team_name), the 8 best third-placed teams.
    Returns: dict slot_id (e.g. '3rd_ABCDF') → team_name.

    Strategy: process slots with fewest feasible candidates first (most-constrained-first),
    randomize ties via rng. Falls back to any remaining team if the FIFA matrix is
    infeasible for this particular set of 8 groups (rare; sims continue cleanly).
    """
    available = list(best_thirds_with_g)
    assigned = {}
    slot_ids = list(FIFA_3RD_ALLOWED.keys())
    # Shuffle for tie-break variety, then sort by constraint tightness
    rng.shuffle(slot_ids)
    slot_ids.sort(key=lambda s: sum(1 for g, _ in available if g in FIFA_3RD_ALLOWED[s]))
    for slot in slot_ids:
        cands = [i for i, (g, _) in enumerate(available) if g in FIFA_3RD_ALLOWED[slot]]
        if not cands:  # rare fallback — FIFA matrix infeasible for this group set
            cands = list(range(len(available)))
        idx = cands[int(rng.integers(0, len(cands)))] if len(cands) > 1 else cands[0]
        assigned[slot] = available[idx][1]
        available.pop(idx)
    return assigned


def build_fifa_r32_bracket(qualifiers_by_g, best_thirds_with_g, rng):
    """Build the 32-team bracket array in FIFA-2026 R32 order.

    qualifiers_by_g: dict group_letter → sequence indexable by [0] (1st) and [1] (2nd).
    best_thirds_with_g: list of (group_letter, team) — 8 best thirds.
    Returns: list of 32 teams; pairs (bracket[0],bracket[1]), (bracket[2],bracket[3]), …
             face off in R32 per the official bracket.
    """
    third_assignment = _assign_thirds_to_slots(best_thirds_with_g, rng)
    bracket = []
    for slot_a, slot_b in FIFA_R32_SLOTS:
        for slot in (slot_a, slot_b):
            if slot.startswith("1"):
                bracket.append(qualifiers_by_g[slot[1]][0])
            elif slot.startswith("2"):
                bracket.append(qualifiers_by_g[slot[1]][1])
            else:  # 3rd-place slot
                bracket.append(third_assignment[slot])
    return bracket


# ------------------------------ orchestration ------------------------------

def run(args):
    rng = np.random.default_rng(args.seed)
    ratings = load_ratings(args.elo)
    with open(args.goalmodel, encoding="utf-8") as f:
        gm = json.load(f)
    calib = calibrator.load(args.calibrator)
    fixtures = load_fixtures(args.fixtures)
    # canonicalize fixture names against ratings using predict.py's resolver
    # (handles accents, alias groups like Cote d'Ivoire <-> Ivory Coast, fuzzy)
    for f in fixtures:
        for k in ("home", "away"):
            try:
                f[k] = _resolve_team(f[k], ratings)
            except UnknownTeam:
                pass  # TBD/placeholder team - keep name, default-Elo fallback
    groups = derive_groups(fixtures)

    all_teams = sorted({t for ts in groups.values() for t in ts})
    placeholders = [t for t in all_teams if t not in ratings]
    print(f"loaded {len(ratings)} teams, {len(fixtures)} fixtures, derived "
          f"{len(groups)} groups ({sum(len(ts) for ts in groups.values())} teams)")
    if placeholders:
        print(f"  WARN: {len(placeholders)} placeholders without Elo -> "
              f"defaulting to {args.default_elo:.0f}:")
        for t in placeholders:
            print(f"    - {t}")

    # group -> its fixtures
    team_to_g = {t: g for g, ts in groups.items() for t in ts}
    g_fix = defaultdict(list)
    for f in fixtures:
        g_fix[team_to_g[f["home"]]].append(f)

    sims = args.sims
    pos_counts = defaultdict(lambda: Counter())   # team -> {1,2,3,4: count}
    reach = defaultdict(lambda: Counter())        # team -> {R32,R16,QF,SF,F,W: count}
    pts_sum = defaultdict(float)                  # team -> total points across sims
    gd_sum = defaultdict(float)

    if args.legend_factor > 0 or args.surprise > 0:
        print(f"  [decisive-match flavour] legend_factor={args.legend_factor} "
              f"surprise={args.surprise} (knockout rounds only)")
    print(f"\nrunning {sims:,} simulations...")
    for s in range(sims):
        # ------ group stage ------
        thirds = []
        qualifiers_by_g = {}
        for g, ts in groups.items():
            standings, tbl = sim_group(ts, g_fix[g], ratings, gm, calib, rng, args.default_elo)
            for pos, t in enumerate(standings, 1):
                pos_counts[t][pos] += 1
                pts_sum[t] += tbl[t]["pts"]
                gd_sum[t] += tbl[t]["gd"]
            qualifiers_by_g[g] = standings
            t3 = standings[2]
            thirds.append((tbl[t3]["pts"], tbl[t3]["gd"], tbl[t3]["gf"], g, t3))

        # best 8 thirds (keep group letter for FIFA bracket assignment)
        thirds.sort(key=lambda x: (-x[0], -x[1], -x[2]))
        top8 = thirds[:8]
        best_thirds = [t for _, _, _, _, t in top8]
        best_thirds_with_g = [(g, t) for _, _, _, g, t in top8]
        r32 = [qualifiers_by_g[g][0] for g in groups] + \
              [qualifiers_by_g[g][1] for g in groups] + best_thirds  # 12 + 12 + 8 = 32
        for t in r32:
            reach[t]["R32"] += 1

        # ------ knockout (FIFA-2026 official bracket) ------
        # Bracket follows the published R32 slot map: winners face matrix-assigned
        # third-placed teams (different groups guaranteed), runners-up have fixed
        # cross-group pairs (2A-2B, 2K-2L, 2D-2G, 2E-2I), etc. This is materially
        # more accurate than random shuffling, especially for top-half vs bottom-half
        # collision probabilities.
        bracket = build_fifa_r32_bracket(qualifiers_by_g, best_thirds_with_g, rng)
        for round_name in ("R16", "QF", "SF", "F", "W"):
            nxt = []
            for i in range(0, len(bracket), 2):
                h, a = bracket[i], bracket[i + 1]
                w = sample_knockout(h, a, ratings, gm, calib, rng, args.default_elo,
                                    round_name=round_name,
                                    legend_factor=args.legend_factor,
                                    surprise=args.surprise)
                nxt.append(w)
                reach[w][round_name] += 1
            bracket = nxt

        if (s + 1) % max(1, sims // 10) == 0:
            print(f"  {s + 1:>6,}/{sims:,}")

    # ------ report: group standings ------
    print("\n================ GROUP STANDINGS (Monte Carlo) ================")
    print("P(top2) = expected to advance directly; P(R32) includes best-third path\n")
    for g in sorted(groups):
        ts = groups[g]
        rows = []
        for t in ts:
            p1 = pos_counts[t][1] / sims
            p2 = pos_counts[t][2] / sims
            p3 = pos_counts[t][3] / sims
            p4 = pos_counts[t][4] / sims
            pr32 = reach[t]["R32"] / sims
            mean_pts = pts_sum[t] / sims
            rows.append((t, p1, p2, p3, p4, pr32, mean_pts))
        rows.sort(key=lambda r: -r[5])
        print(f"Group {g}:")
        print(f"  {'team':<24}{'P(1st)':>9}{'P(2nd)':>9}{'P(3rd)':>9}{'P(4th)':>9}{'P(R32)':>9}{'mean pts':>10}")
        for t, p1, p2, p3, p4, pr32, mpts in rows:
            print(f"  {t:<24}{p1*100:>8.1f}%{p2*100:>8.1f}%{p3*100:>8.1f}%{p4*100:>8.1f}%{pr32*100:>8.1f}%{mpts:>9.2f}")
        print()

    # ------ report: tournament-wide ------
    print("================ TITLE CONTENDERS ================")
    contenders = sorted(
        ((t, reach[t]["W"] / sims, reach[t]["F"] / sims, reach[t]["SF"] / sims,
          reach[t]["QF"] / sims, reach[t]["R16"] / sims, reach[t]["R32"] / sims)
         for t in reach),
        key=lambda r: -r[1]
    )
    print(f"  {'team':<24}{'P(Champ)':>10}{'P(Final)':>10}{'P(SF)':>9}{'P(QF)':>9}{'P(R16)':>9}{'P(R32)':>9}")
    for row in contenders[:24]:
        t, pw, pf, psf, pqf, pr16, pr32 = row
        print(f"  {t:<24}{pw*100:>9.2f}%{pf*100:>9.2f}%{psf*100:>8.1f}%{pqf*100:>8.1f}%{pr16*100:>8.1f}%{pr32*100:>8.1f}%")

    # ------ modal bracket: model's single best-guess path ------
    print("\n================ MODAL FORECAST (single most-likely path) ================")
    modal_top2 = {}
    print("Group winners and runners-up by P(1st)/P(2nd):")
    for g in sorted(groups):
        ts = groups[g]
        sorted_by_p1 = sorted(ts, key=lambda t: -pos_counts[t][1])
        first = sorted_by_p1[0]
        sorted_by_p2 = sorted([t for t in ts if t != first], key=lambda t: -pos_counts[t][2])
        second = sorted_by_p2[0]
        modal_top2[g] = (first, second)
        print(f"  Group {g}: 1st {first}  |  2nd {second}")

    # propagate through bracket using KO probabilities (deterministic argmax),
    # with the optional 'last dance' legend lift applied per round.
    def ko_winner_modal(h, a, round_name="R16"):
        hb = _legend_bonus(h, round_name, args.legend_factor)
        ab = _legend_bonus(a, round_name, args.legend_factor)
        ensure_elo(h, ratings, args.default_elo)
        ensure_elo(a, ratings, args.default_elo)
        oh, oa = ratings[h], ratings[a]
        if hb:
            ratings[h] = oh + hb
        if ab:
            ratings[a] = oa + ab
        try:
            p = match_probs(h, a, ratings, gm, calib, neutral=True, default_elo=args.default_elo)
            return h if p["p_home"] >= p["p_away"] else a
        finally:
            ratings[h] = oh
            ratings[a] = oa

    # Build a modal R32 via the simplified pairing used in MC averaging:
    # take winners (1st), runners-up (2nd), and top 8 third-placed by mean pts.
    third_candidates = []
    for g in sorted(groups):
        ts = groups[g]
        sorted_by_p3 = sorted(ts, key=lambda t: -pos_counts[t][3])
        t3 = sorted_by_p3[0]
        third_candidates.append((pts_sum[t3] / sims, gd_sum[t3] / sims, g, t3))
    third_candidates.sort(key=lambda r: (-r[0], -r[1]))
    modal_thirds_with_g = [(g, t) for _, _, g, t in third_candidates[:8]]
    modal_thirds = [t for _, t in modal_thirds_with_g]
    # Build modal R32 in official FIFA bracket order (deterministic — seeded rng).
    modal_qualifiers_by_g = {g: list(modal_top2[g]) for g in groups}
    modal_rng = np.random.default_rng(0)
    modal_r32 = build_fifa_r32_bracket(modal_qualifiers_by_g, modal_thirds_with_g, modal_rng)
    print(f"\nModal R32 field ({len(modal_r32)} teams, FIFA bracket order): " + ", ".join(modal_r32))

    print("\nModal knockout path (deterministic argmax of model probabilities):")
    current = list(modal_r32)
    for round_name, label in (("R16", "R16"), ("QF", "QF"), ("SF", "SF"), ("F", "Final"), ("W", "Champion")):
        nxt = []
        for i in range(0, len(current), 2):
            h, a = current[i], current[i + 1]
            w = ko_winner_modal(h, a, round_name)
            nxt.append(w)
        if round_name == "W":
            print(f"  {label}: {nxt[0]}")
        else:
            pairs = [f"{current[i]} vs {current[i+1]} -> {nxt[i // 2]}" for i in range(0, len(current), 2)]
            print(f"  -> {label} ({len(nxt)}): " + "; ".join(pairs))
        current = nxt
    modal_champion = current[0]

    # ------ save baseline ------
    out = {
        "sims": sims,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "groups": groups,
        "tournament_probs": {
            t: {
                "P_R32": reach[t]["R32"] / sims,
                "P_R16": reach[t]["R16"] / sims,
                "P_QF":  reach[t]["QF"]  / sims,
                "P_SF":  reach[t]["SF"]  / sims,
                "P_F":   reach[t]["F"]   / sims,
                "P_W":   reach[t]["W"]   / sims,
            }
            for t in reach
        },
        "group_positions": {
            t: {str(k): pos_counts[t][k] / sims for k in (1, 2, 3, 4)}
            for t in pos_counts
        },
        "mean_points": {t: pts_sum[t] / sims for t in pts_sum},
        "modal_forecast": {
            "group_top2": {g: list(p) for g, p in modal_top2.items()},
            "modal_champion": modal_champion,
        },
        "legend_factor": args.legend_factor,
        "surprise_rate": args.surprise,
        "placeholders_defaulted": placeholders,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n[saved baseline] {args.out}")
    print("Run this script ONCE before the tournament starts and keep the JSON")
    print("untouched - it's your reference forecast for post-WC comparison.")


def main():
    ap = argparse.ArgumentParser(description="Monte Carlo simulator for WC2026.")
    ap.add_argument("--sims", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--elo", default=_p("wc2026_elo.csv"))
    ap.add_argument("--goalmodel", default=_p("wc2026_goalmodel.json"))
    ap.add_argument("--calibrator", default=_p("wc2026_calibrator.json"))
    ap.add_argument("--fixtures", default=_p("wc2026_fixtures.csv"))
    ap.add_argument("--out", default=_p("wc2026_baseline.json"))
    ap.add_argument("--default-elo", type=float, default=1500.0,
                    help="Elo for placeholder teams (TBD playoff winners)")
    ap.add_argument("--legend-factor", type=float, default=0.0,
                    help="Veteran-superstar 'last dance' lift in knockout games "
                         "(0=off, ~1.0=on). Keep 0 for the frozen pre-WC baseline.")
    ap.add_argument("--surprise", type=float, default=0.0,
                    help="Per-knockout-match chance (0..1) of a giant-killing upset. "
                         "Keep 0 for the frozen pre-WC baseline.")
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
