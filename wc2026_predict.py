"""calibrator.py - apply the 3-way probability calibrator fitted by wc2026_eval.py.

The model is well-ranked but over-confident; wc2026_eval.py fits a recalibrator
    p_cal[k] ~ exp(a_k + b * log p[k])
and saves it to wc2026_calibrator.json. This tiny module loads it and applies it
to a prediction, so the bot shows trustworthy probabilities.

No numpy needed (pure math) so it's safe to import anywhere, including the bot.

Class order is [away, draw, home] (same as wc2026_eval.py / eval_metrics.py).

Integration (wc2026_daily.py predict, or the bot):
    import calibrator
    _CAL = calibrator.load()                 # load once at startup
    ...
    pr = predict(home, away, ratings, gm, neutral=neu)
    pr = calibrator.apply_dict(pr, _CAL)     # one line, returns same dict shape

If the JSON is missing, load() returns None and apply_*() pass probabilities
through unchanged - so it is always safe to wire in.
"""
import os
import json
import math

_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wc2026_calibrator.json")


def load(path=_DEFAULT):
    """Load the calibrator dict, or None if the file is absent/invalid.

    Supports several calibrator types written by wc2026_eval.py / wc2026_improve.py:
      vector      : {"type":"vector","a":[3],"b":float}   (default; legacy {a,b})
      temperature : {"type":"temperature","b":float}
      matrix      : {"type":"matrix","W":[3][3],"c":[3]}
      shrinkage   : {"type":"shrinkage","lam":float,"prior":[3]}
    """
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            c = json.load(f)
    except Exception:
        return None
    t = c.get("type", "vector")
    ok = (
        (t == "vector" and len(c.get("a", [])) == 3 and "b" in c)
        or (t == "temperature" and "b" in c)
        or (t == "matrix" and len(c.get("W", [])) == 3 and len(c.get("c", [])) == 3)
        or (t == "shrinkage" and "lam" in c and len(c.get("prior", [])) == 3)
        or (t == "identity")
    )
    if not ok:
        return None
    c["type"] = t
    return c


def _softmax3(logits):
    m = max(logits)
    es = [math.exp(x - m) for x in logits]
    s = sum(es)
    return es[0] / s, es[1] / s, es[2] / s


def apply_probs(p_away, p_draw, p_home, calib):
    """Return calibrated (p_away, p_draw, p_home). Pass-through if calib is None."""
    if calib is None or calib.get("type") == "identity":
        return p_away, p_draw, p_home
    ps = [p_away, p_draw, p_home]
    t = calib.get("type", "vector")
    if t == "shrinkage":
        lam = calib["lam"]
        pr = calib["prior"]
        q = [(1 - lam) * ps[k] + lam * pr[k] for k in range(3)]
        s = sum(q)
        return q[0] / s, q[1] / s, q[2] / s
    logp = [math.log(max(ps[k], 1e-9)) for k in range(3)]
    if t == "temperature":
        logits = [calib["b"] * logp[k] for k in range(3)]
    elif t == "matrix":
        W = calib["W"]
        c = calib["c"]
        logits = [c[k] + sum(W[k][j] * logp[j] for j in range(3)) for k in range(3)]
    else:  # vector
        a = calib["a"]
        b = calib["b"]
        logits = [a[k] + b * logp[k] for k in range(3)]
    return _softmax3(logits)


def apply_dict(pred, calib):
    """Calibrate a prediction dict with keys p_away/p_draw/p_home. Returns a new dict
    with the same keys (and preserves any other keys like lambdas/O2.5)."""
    qa, qd, qh = apply_probs(pred["p_away"], pred["p_draw"], pred["p_home"], calib)
    out = dict(pred)
    out["p_away"], out["p_draw"], out["p_home"] = qa, qd, qh
    return out


def apply_matrix(P, calib):
    """Calibrate an (n,3) array/list of [away,draw,home] probs. Needs numpy if P is
    an ndarray; works on plain lists too."""
    if calib is None:
        return P
    try:
        import numpy as np
        if isinstance(P, np.ndarray):
            a = np.asarray(calib["a"])[None, :]
            L = a + calib["b"] * np.log(np.clip(P, 1e-9, 1.0))
            L -= L.max(1, keepdims=True)
            e = np.exp(L)
            return e / e.sum(1, keepdims=True)
    except Exception:
        pass
    return [list(apply_probs(r[0], r[1], r[2], calib)) for r in P]


if __name__ == "__main__":
    c = load()
    if c is None:
        print("no wc2026_calibrator.json found (run wc2026_eval.py first).")
    else:
        print(f"loaded calibrator: b={c['b']:.3f}  a={['%+.3f' % x for x in c['a']]}  "
              f"(classes {c.get('classes')})")
        demo = {"p_away": 0.20, "p_draw": 0.25, "p_home": 0.55}
        out = apply_dict(demo, c)
        print(f"demo raw  : away={demo['p_away']:.3f} draw={demo['p_draw']:.3f} home={demo['p_home']:.3f}")
        print(f"demo calib: away={out['p_away']:.3f} draw={out['p_draw']:.3f} home={out['p_home']:.3f}")
        print(f"sum check : {out['p_away']+out['p_draw']+out['p_home']:.6f}")
