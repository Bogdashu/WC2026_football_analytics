"""wc2026_model.py - national-team goal model for World Cup 2026 (pure numpy, no scipy).

NOT a fine-tune of the RPL model - a NEW model on international data. Only the code
scaffolding is reused. Engine = World-Football Elo + Poisson(Elo->expected goals) with
a Dixon-Coles low-score correction. Elo is updated chronologically, so "retrain daily"
= append new results and call update_elo() - one pass, no refit needed.

INPUT  (see wc2026_data_spec.md):
  intl_results.csv : date,home_team,away_team,home_score,away_score,tournament,city,country,neutral
OUTPUT:
  wc2026_elo.csv       : team,elo   (current ratings, also used by the simulator)
  wc2026_goalmodel.json: fitted goal-model params (supremacy slope, total, rho)

RUN: python wc2026_model.py            # fits + demo prediction (synthetic if no csv)

API used by other scripts:
  ratings, gm = train()                      # full fit from intl_results.csv (or demo)
  predict(home, away, ratings, gm, neutral)  # -> dict with 1x2 / totals / lambdas / matrix
  update_elo(ratings, new_results_df)        # daily incremental update

TUNING NOTE (2026-06, via wc2026_ablate.py on 49,339 matches):
  Component leave-one-out + hyper-parameter sweeps on a 2011->2026 holdout retuned
  three settings, jointly cutting raw log-loss 0.8876 -> 0.8820 (brier 0.5225 -> 0.5191,
  acc 59.2% -> 59.5%):
    * HOME_ADV_ELO   65 -> 90
    * HALFLIFE_YEARS  8 -> 2   (1y was marginally best but within 0.0003; 2 is safer)
    * k_for(...)  per-tournament importance spread -> flat 35 (the spread HURT log-loss)
  Everything else (total~|edge| slope, goal-diff K multiplier, recency decay,
  Dixon-Coles rho) measurably helps and was kept.
"""
import os
import sys
import json
import math
import numpy as np
import pandas as pd
from difflib import get_close_matches

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "intl_results.csv")
ELO_OUT = os.path.join(HERE, "wc2026_elo.csv")
GM_OUT = os.path.join(HERE, "wc2026_goalmodel.json")
XG_PATH = os.path.join(HERE, "team_xg.csv")

# Heuristic: blend current StatsBomb xG-form into Elo-derived expected goals.
# 0.0 = pure Elo+Poisson (original behaviour). Raise toward 1.0 to trust xG more.
# Disable entirely by setting XG_BLEND = 0.0 or deleting team_xg.csv.
XG_BLEND = 0.25
_XG = {}            # team -> (xg_for_pm, xg_against_pm), shrunk toward the global mean
_XG_LOADED = False

INIT_ELO = 1500.0
HOME_ADV_ELO = 90.0          # applied only when neutral == False (retuned 65->90, wc2026_ablate.py)
HALFLIFE_YEARS = 2.0         # time-decay for goal-model fit (retuned 8->2; 1-2y ~equal, 2 = robust)
MAXG = 10                    # score grid 0..MAXG
_FACT = np.array([math.factorial(k) for k in range(MAXG + 1)], dtype=float)
_KS = np.arange(MAXG + 1)


def k_for(tournament):
    """Elo K-factor.

    Ablation (wc2026_ablate.py) on 49k matches showed the per-tournament importance
    spread HURT out-of-time log-loss: friendlies got too small a K, so ratings
    under-reacted on 60%+ of the sample. A flat K generalizes better; the holdout
    optimum was K=35. The old importance map is preserved below (commented) for
    easy revert.
    """
    return 35.0
    # --- previous importance-weighted version (worse out-of-sample, kept for revert) ---
    # t = str(tournament).lower()
    # if "world cup" in t and "qual" not in t:
    #     return 60.0
    # if "qualif" in t:
    #     return 40.0
    # if "nations league" in t:
    #     return 45.0
    # if any(x in t for x in ["euro", "copa am", "african cup", "afc asian",
    #                         "gold cup", "confederations", "championship"]):
    #     return 50.0
    # if "friendly" in t:
    #     return 15.0
    # return 30.0


def gd_mult(gd):
    gd = abs(int(gd))
    if gd <= 1:
        return 1.0
    if gd == 2:
        return 1.5
    return (11.0 + gd) / 8.0


def _prep(df):
    df = df.copy()
    df.columns = [c.strip().lower() for c in df.columns]
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date", "home_team", "away_team", "home_score", "away_score"])
    df["home_score"] = df["home_score"].astype(int)
    df["away_score"] = df["away_score"].astype(int)
    if "neutral" not in df.columns:
        df["neutral"] = True
    df["neutral"] = df["neutral"].astype(str).str.lower().isin(["true", "1", "yes"])
    if "tournament" not in df.columns:
        df["tournament"] = "Friendly"
    return df.sort_values("date").reset_index(drop=True)


def compute_elo(df, base=None):
    """Chronological Elo. Returns (ratings dict, pre_home array, pre_away array)."""
    R = {} if base is None else dict(base)
    pre_h = np.empty(len(df))
    pre_a = np.empty(len(df))
    h = df["home_team"].values
    a = df["away_team"].values
    hs = df["home_score"].values
    as_ = df["away_score"].values
    neu = df["neutral"].values
    trn = df["tournament"].values
    for i in range(len(df)):
        rh = R.get(h[i], INIT_ELO)
        ra = R.get(a[i], INIT_ELO)
        pre_h[i] = rh
        pre_a[i] = ra
        hfa = 0.0 if neu[i] else HOME_ADV_ELO
        we = 1.0 / (1.0 + 10 ** (-((rh + hfa) - ra) / 400.0))
        w = 1.0 if hs[i] > as_[i] else (0.0 if hs[i] < as_[i] else 0.5)
        k = k_for(trn[i]) * gd_mult(hs[i] - as_[i])
        delta = k * (w - we)
        R[h[i]] = rh + delta
        R[a[i]] = ra - delta
    return R, pre_h, pre_a


def _tau(M, lh, la, rho):
    M = M.copy()
    M[0, 0] *= 1.0 - lh * la * rho
    M[0, 1] *= 1.0 + lh * rho
    M[1, 0] *= 1.0 + la * rho
    M[1, 1] *= 1.0 - rho
    return M


def _score_matrix(lh, la, rho):
    ph = np.exp(-lh) * lh ** _KS / _FACT
    pa = np.exp(-la) * la ** _KS / _FACT
    M = np.outer(ph, pa)
    M = _tau(M, lh, la, rho)
    M = np.clip(M, 1e-15, None)
    return M / M.sum()


def fit_goal_model(df, pre_h, pre_a):
    """Map pre-match Elo edge -> expected goals. Returns dict of params."""
    hfa = np.where(df["neutral"].values, 0.0, HOME_ADV_ELO)
    edge = (pre_h + hfa) - pre_a
    sup = (df["home_score"].values - df["away_score"].values).astype(float)
    tot = (df["home_score"].values + df["away_score"].values).astype(float)
    days = (df["date"].max() - df["date"]).dt.days.values.astype(float)
    w = 0.5 ** (days / 365.25 / HALFLIFE_YEARS)
    # weighted LS: supremacy ~ s0 + s1*edge
    X = np.column_stack([np.ones_like(edge), edge])
    A = X.T @ (w[:, None] * X)
    bvec = X.T @ (w * sup)
    s0, s1 = np.linalg.solve(A, bvec)
    # weighted LS: total goals ~ t0 + t1*|edge|  (mismatches score slightly more)
    Xt = np.column_stack([np.ones_like(edge), np.abs(edge)])
    At = Xt.T @ (w[:, None] * Xt)
    bt = Xt.T @ (w * tot)
    t0, t1 = np.linalg.solve(At, bt)
    # grid-search Dixon-Coles rho on weighted low-score log-likelihood
    best_rho, best_ll = 0.0, -1e18
    hs = df["home_score"].values
    as_ = df["away_score"].values
    lam_h = np.clip((t0 + t1 * np.abs(edge) + (s0 + s1 * edge)) / 2.0, 0.05, 6.0)
    lam_a = np.clip((t0 + t1 * np.abs(edge) - (s0 + s1 * edge)) / 2.0, 0.05, 6.0)
    low = (hs <= 1) & (as_ <= 1)
    for rho in np.linspace(-0.2, 0.2, 41):
        corr = np.ones(len(df))
        corr[low & (hs == 0) & (as_ == 0)] = 1.0 - lam_h[low & (hs == 0) & (as_ == 0)] * lam_a[low & (hs == 0) & (as_ == 0)] * rho
        corr[low & (hs == 0) & (as_ == 1)] = 1.0 + lam_h[low & (hs == 0) & (as_ == 1)] * rho
        corr[low & (hs == 1) & (as_ == 0)] = 1.0 + lam_a[low & (hs == 1) & (as_ == 0)] * rho
        corr[low & (hs == 1) & (as_ == 1)] = 1.0 - rho
        corr = np.clip(corr, 1e-6, None)
        ll = np.sum(w * np.log(corr))
        if ll > best_ll:
            best_ll, best_rho = ll, rho
    return {"s0": float(s0), "s1": float(s1), "t0": float(t0), "t1": float(t1),
            "rho": float(best_rho)}


def lambdas(edge, gm):
    sup = gm["s0"] + gm["s1"] * edge
    tot = gm["t0"] + gm["t1"] * abs(edge)
    lh = float(np.clip((tot + sup) / 2.0, 0.05, 6.0))
    la = float(np.clip((tot - sup) / 2.0, 0.05, 6.0))
    return lh, la


def _load_xg(k=8.0):
    """team -> (xg_for_pm, xg_against_pm), each shrunk toward the global mean by matches."""
    global _XG, _XG_LOADED
    _XG_LOADED = True
    _XG = {}
    if not os.path.exists(XG_PATH):
        return _XG
    t = pd.read_csv(XG_PATH)
    t.columns = [c.strip().lower() for c in t.columns]
    if not {"team", "xg_for_pm", "xg_against_pm"}.issubset(t.columns):
        return _XG
    n = pd.to_numeric(t.get("matches", 1), errors="coerce").fillna(1).to_numpy(float)
    f = pd.to_numeric(t["xg_for_pm"], errors="coerce").fillna(0).to_numpy(float)
    a = pd.to_numeric(t["xg_against_pm"], errors="coerce").fillna(0).to_numpy(float)
    mu_f = float(np.average(f, weights=n)) if n.sum() else 1.4
    mu_a = float(np.average(a, weights=n)) if n.sum() else 1.4
    f_sh = (n * f + k * mu_f) / (n + k)
    a_sh = (n * a + k * mu_a) / (n + k)
    _XG = {str(tm): (float(fi), float(ai)) for tm, fi, ai in zip(t["team"], f_sh, a_sh)}
    return _XG


def _ensure_xg():
    if not _XG_LOADED:
        _load_xg()


def _xg_blend(home, away, lh, la):
    """Nudge Elo-derived lambdas toward current xG form (only if both teams known)."""
    _ensure_xg()
    if XG_BLEND <= 0 or home not in _XG or away not in _XG:
        return lh, la
    fh, ah = _XG[home]
    fa, aa = _XG[away]
    lh_xg = 0.5 * (fh + aa)   # home attack vs away defense
    la_xg = 0.5 * (fa + ah)   # away attack vs home defense
    lh = (1.0 - XG_BLEND) * lh + XG_BLEND * lh_xg
    la = (1.0 - XG_BLEND) * la + XG_BLEND * la_xg
    return float(np.clip(lh, 0.05, 6.0)), float(np.clip(la, 0.05, 6.0))


def predict(home, away, ratings, gm, neutral=True, line=2.5):
    rh = ratings.get(home, INIT_ELO)
    ra = ratings.get(away, INIT_ELO)
    hfa = 0.0 if neutral else HOME_ADV_ELO
    edge = (rh + hfa) - ra
    lh, la = lambdas(edge, gm)
    lh, la = _xg_blend(home, away, lh, la)
    M = _score_matrix(lh, la, gm["rho"])
    iu = np.triu_indices(MAXG + 1, k=1)
    il = np.tril_indices(MAXG + 1, k=-1)
    p_home = float(M[il].sum())
    p_away = float(M[iu].sum())
    p_draw = float(np.trace(M))
    gx, gy = np.meshgrid(_KS, _KS, indexing="ij")
    over = float(M[(gx + gy) > line].sum())
    return {"home": home, "away": away, "neutral": neutral,
            "lambda_home": lh, "lambda_away": la,
            "p_home": p_home, "p_draw": p_draw, "p_away": p_away,
            "p_over": over, "p_under": 1.0 - over, "line": line,
            "matrix": M}


def update_elo(ratings, new_df):
    """Daily incremental update: feed new finished matches, ratings mutate in place."""
    new_df = _prep(new_df)
    R, _, _ = compute_elo(new_df, base=ratings)
    ratings.update(R)
    return ratings


def normalize_names(names, known):
    """Fuzzy-map fixture team names onto the results name space."""
    out = {}
    known = list(known)
    for n in names:
        if n in known:
            out[n] = n
            continue
        m = get_close_matches(n, known, n=1, cutoff=0.85)
        out[n] = m[0] if m else n
    return out


def _demo_df(seed=42):
    rng = np.random.default_rng(seed)
    teams = [f"T{i:02d}" for i in range(24)]
    atk = {t: rng.normal(0, 0.35) for t in teams}
    df_def = {t: rng.normal(0, 0.30) for t in teams}
    ha = 0.25
    rows = []
    start = pd.Timestamp("2014-01-01")
    trns = ["Friendly", "FIFA World Cup qualification", "UEFA Nations League", "FIFA World Cup"]
    for d in range(4000):
        h, a = rng.choice(teams, 2, replace=False)
        neu = rng.random() < 0.4
        lh = math.exp(atk[h] - df_def[a] + (0 if neu else ha) + 0.15)
        la = math.exp(atk[a] - df_def[h])
        hs, as_ = rng.poisson(lh), rng.poisson(la)
        rows.append([start + pd.Timedelta(days=d * 1.0), h, a, hs, as_,
                     trns[rng.integers(0, 4)], "", "", neu])
    return pd.DataFrame(rows, columns=["date", "home_team", "away_team", "home_score",
                                       "away_score", "tournament", "city", "country", "neutral"])


def train():
    if os.path.exists(RESULTS):
        df = _prep(pd.read_csv(RESULTS))
        print(f"loaded {len(df)} international matches from intl_results.csv")
        demo = False
    else:
        df = _prep(_demo_df())
        print(f"[DEMO] no intl_results.csv -> synthetic {len(df)} matches (numbers are fake)")
        demo = True
    ratings, pre_h, pre_a = compute_elo(df)
    gm = fit_goal_model(df, pre_h, pre_a)
    print(f"goal model: supremacy={gm['s0']:.3f}+{gm['s1']*100:.3f}/100Elo  "
          f"total={gm['t0']:.2f}+{gm['t1']*100:.3f}/100Elo  rho={gm['rho']:+.3f}")
    _load_xg()
    if _XG:
        print(f"xG form: loaded {len(_XG)} teams from team_xg.csv  (blend={XG_BLEND:.2f})")
    else:
        print("xG form: team_xg.csv not found -> pure Elo+Poisson (blend off)")
    top = sorted(ratings.items(), key=lambda kv: -kv[1])[:15]
    print("\ntop 15 by Elo:")
    for t, r in top:
        print(f"  {t:<24} {r:7.1f}")
    if not demo:
        pd.DataFrame(sorted(ratings.items(), key=lambda kv: -kv[1]),
                     columns=["team", "elo"]).to_csv(ELO_OUT, index=False)
        json.dump(gm, open(GM_OUT, "w"))
        print(f"\n[saved] {ELO_OUT}\n[saved] {GM_OUT}")
    # demo prediction
    a, b = top[0][0], top[5][0]
    pr = predict(a, b, ratings, gm, neutral=True)
    print(f"\nexample (neutral)  {a} vs {b}:")
    print(f"  lambdas {pr['lambda_home']:.2f}-{pr['lambda_away']:.2f} | "
          f"1={pr['p_home']:.3f} X={pr['p_draw']:.3f} 2={pr['p_away']:.3f} | "
          f"O{pr['line']}={pr['p_over']:.3f}")
    return ratings, gm


if __name__ == "__main__":
    train()
