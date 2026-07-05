# -*- coding: utf-8 -*-
"""
WC2026 — ресимуляция плей-офф с точки старта 1/8 (известные 16 команд).

Монте-Карло по РЕАЛЬНОЙ сетке 1/8 с текущим Elo (пост-1/16). Обновляет
текущий baseline в wc2026_artifacts: пересчитывает P_R16/P_QF/P_SF/P_F/P_W,
модальную сетку и чемпиона; групповые данные (group_positions, mean_points)
сохраняются как есть. Старый baseline снапшотится в baseline_<label>.

Запуск:
  py -X utf8 wc2026_resim_ko.py --sims 100000 --seed 42 --label after_R32
"""
import os, sys, json, argparse, random, logging
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("resim_ko")

import psycopg2
import bot  # bot.py запускает polling только под __main__

DB = (os.environ.get("DATABASE_PUBLIC_URL") or os.environ.get("DATABASE_URL") or "").strip()
if not DB:
    sys.exit("Set DATABASE_PUBLIC_URL")
bot.DB_URL = DB

# Реальная сетка 1/8 в bracket-порядке (пары -> четвертьфиналы -> полуфиналы -> финал)
R16_BRACKET = [
    ("Paraguay", "France"), ("Canada", "Morocco"),
    ("Portugal", "Spain"), ("United States", "Belgium"),
    ("Brazil", "Norway"), ("Mexico", "England"),
    ("Argentina", "Egypt"), ("Switzerland", "Colombia"),
]
STAGES = ["R16", "QF", "SF", "F", "W"]  # достигнутые стадии (W = чемпион)

# --- «последний танец» и фактор сенсации (те же, что в wc2026_simulate.py) ---
LEGEND_ELO = {
    "Argentina": 15.0, "Portugal": 15.0, "Croatia": 10.0, "Germany": 9.0,
    "Brazil": 9.0, "Egypt": 7.0, "Bosnia": 6.0, "England": 4.0,
    "Korea Republic": 4.0, "Belgium": 4.0, "Austria": 3.0, "Mexico": 3.0,
}
# Стадия, ЗА ВЫХОД в которую играется матч: 1/8 -> QF, 1/4 -> SF, ПФ -> F, финал -> W
KO_ROUND_MULT = {"QF": 0.60, "SF": 0.80, "F": 1.00, "W": 1.40}


def _legend_bonus(team, next_stage, legend_factor):
    if legend_factor <= 0.0:
        return 0.0
    return LEGEND_ELO.get(team, 0.0) * KO_ROUND_MULT.get(next_stage, 1.0) * legend_factor


def match_probs(h, a, next_stage="QF", legend_factor=0.0):
    """Вероятности прохода с учётом legend-бонуса (временный сдвиг Elo)."""
    hb = _legend_bonus(h, next_stage, legend_factor)
    ab = _legend_bonus(a, next_stage, legend_factor)
    oh, oa = bot.ELO.get(h), bot.ELO.get(a)
    try:
        if hb and oh is not None: bot.ELO[h] = oh + hb
        if ab and oa is not None: bot.ELO[a] = oa + ab
        p_h, p_d, p_a = bot.predict_1x2(h, a, True)  # нейтральное поле
    finally:
        if oh is not None: bot.ELO[h] = oh
        if oa is not None: bot.ELO[a] = oa
    return p_h + p_d / 2.0, p_a + p_d / 2.0, (p_h, p_d, p_a)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sims", type=int, default=100000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--label", default="after_R32")
    ap.add_argument("--legend-factor", type=float, default=0.0,
                    help="усиление «последнего танца» легенд (как в wc2026_simulate.py)")
    ap.add_argument("--surprise", type=float, default=0.0,
                    help="шанс сенсации на матч: андердогу усиливается доля побед")
    args = ap.parse_args()

    # Elo и baseline читаем напрямую из БД (без сетевых источников load_all —
    # Transfermarkt и т.п. могут быть недоступны, а для ресимуляции не нужны).
    conn0 = psycopg2.connect(DB)
    with conn0.cursor() as cur0:
        cur0.execute("SELECT team, elo FROM wc2026_elo")
        bot.ELO.clear()
        bot.ELO.update({t: float(e) for t, e in cur0.fetchall()})
        cur0.execute("SELECT content FROM wc2026_artifacts WHERE key='baseline'")
        row0 = cur0.fetchone()
        if row0:
            content = row0[0] if isinstance(row0[0], dict) else json.loads(row0[0])
            try:
                bot.BASELINE.clear(); bot.BASELINE.update(content)
            except Exception:
                bot.BASELINE = content
    conn0.close()
    log.info("Elo из БД: %d команд", len(bot.ELO))
    rng = random.Random(args.seed)
    alive = [t for pair in R16_BRACKET for t in pair]
    reach = {t: {s: 0 for s in STAGES} for t in alive}
    for t in alive:
        reach[t]["R16"] = args.sims

    # кэш вероятностей пар (ключ включает стадию — legend-бонус растёт по раундам)
    cache = {}
    def adv_prob(h, a, nxt="QF"):
        key = (h, a, nxt)
        if key not in cache:
            cache[key] = match_probs(h, a, nxt, args.legend_factor)
        return cache[key]

    if args.legend_factor > 0 or args.surprise > 0:
        log.info("[decisive-match flavour] legend_factor=%s surprise=%s",
                 args.legend_factor, args.surprise)

    for _ in range(args.sims):
        current = [t for pair in R16_BRACKET for t in pair]
        for nxt_stage in ("QF", "SF", "F", "W"):
            winners = []
            for i in range(0, len(current), 2):
                h, a = current[i], current[i + 1]
                ph, pa, _ = adv_prob(h, a, nxt_stage)
                # Сенсация: изредка усиливаем долю побед андердога (как в simulate.py)
                if args.surprise > 0.0 and rng.random() < args.surprise:
                    if ph >= pa:
                        shift = min(0.25, pa * 0.8); ph -= shift; pa += shift
                    else:
                        shift = min(0.25, ph * 0.8); pa -= shift; ph += shift
                w = h if rng.random() < ph / (ph + pa) else a
                winners.append(w)
                reach[w][nxt_stage] += 1
            current = winners

    probs = {t: {s: reach[t][s] / args.sims for s in STAGES} for t in alive}

    # Модальная сетка (детерминированный argmax; legend-бонус учтён, сенсации нет)
    NEXT_OF = {"R16": "QF", "QF": "SF", "SF": "F", "F": "W"}
    rounds = []
    current = [t for pair in R16_BRACKET for t in pair]
    for play_code in ("R16", "QF", "SF", "F"):
        matches = []
        nxt = []
        for i in range(0, len(current), 2):
            h, a = current[i], current[i + 1]
            ph, pa, (p_h, p_d, p_a) = adv_prob(h, a, NEXT_OF[play_code])
            w = h if ph >= pa else a
            score = None
            try:
                nat = "H" if w == h else "A"
                bs = bot.best_score_for(h, a, True, nat)
                if bs: score = f"{int(bs[0])}:{int(bs[1])}"
            except Exception:
                pass
            matches.append({"home": h, "away": a, "winner": w, "score": score or "-",
                            "p_home": round(p_h, 4), "p_draw": round(p_d, 4),
                            "p_away": round(p_a, 4), "adv": round(max(ph, pa) / (ph + pa), 4)})
            nxt.append(w)
        rounds.append({"code": play_code, "matches": matches})
        log.info("%s: %s", play_code, "; ".join(f"{m['home']} vs {m['away']} -> {m['winner']}" for m in matches))
        current = nxt
    champion = current[0]
    log.info("Modal champion: %s (P_W=%.1f%%)", champion, probs[champion]["W"] * 100)

    # Патч baseline
    conn = psycopg2.connect(DB)
    cur = conn.cursor()

    # Реально сыгранная 1/16 — в начало сетки (факт, как группы): счёт и
    # прошедший дальше, с серией пенальти в скобках при ничьей.
    cur.execute(
        "SELECT match_date, home, away, home_score, away_score, advance_winner, "
        "pens_home, pens_away FROM wc2026_fixtures WHERE round='r32' "
        "AND home_score IS NOT NULL ORDER BY match_date, home")
    r32_matches = []
    for _, h, a, hs, as_, adv, ph_, pa_ in cur.fetchall():
        if hs > as_: w = h
        elif as_ > hs: w = a
        else: w = adv or "?"
        score = f"{int(hs)}:{int(as_)}"
        if ph_ is not None and pa_ is not None:
            score += f" (пен. {int(ph_)}:{int(pa_)})"
        r32_matches.append({"home": h, "away": a, "winner": w, "score": score,
                            "played": True})
    if r32_matches:
        rounds.insert(0, {"code": "R32", "matches": r32_matches})
        log.info("R32 (факт): %d сыгранных матчей добавлены в сетку", len(r32_matches))
    cur.execute("SELECT content FROM wc2026_artifacts WHERE key='baseline'")
    row = cur.fetchone()
    if not row:
        sys.exit("baseline не найден в wc2026_artifacts")
    base = row[0] if isinstance(row[0], dict) else json.loads(row[0])

    tp = base.get("tournament_probs", {}) or {}
    alive_set = set(alive)
    for team, d in tp.items():
        if team in alive_set:
            d["P_R16"] = 1.0
            for s in ("QF", "SF", "F", "W"):
                d[f"P_{s}"] = round(probs[team][s], 4)
        else:
            for s in ("R16", "QF", "SF", "F", "W"):
                if f"P_{s}" in d: d[f"P_{s}"] = 0.0
    base["tournament_probs"] = tp
    base["modal_bracket"] = {"rounds": rounds, "champion": champion,
                             "champion_prob": round(probs[champion]["W"], 4)}
    if isinstance(base.get("modal_forecast"), dict):
        base["modal_forecast"]["modal_champion"] = champion
    base["generated_at"] = datetime.now(timezone.utc).isoformat()
    base["resim_note"] = (f"KO-resim from real R16 bracket · sims={args.sims} · label={args.label}"
                          f" · legend_factor={args.legend_factor} · surprise={args.surprise}")
    cur.execute("SELECT count(*) FROM wc2026_fixtures WHERE home_score IS NOT NULL")
    base["matches_played"] = int(cur.fetchone()[0])

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    snap_key = f"baseline_{stamp}_{args.label}"
    cur.execute("""INSERT INTO wc2026_artifacts(key, content) VALUES (%s, %s)
                   ON CONFLICT (key) DO UPDATE SET content=EXCLUDED.content""",
                (snap_key, json.dumps(base)))
    cur.execute("UPDATE wc2026_artifacts SET content=%s WHERE key='baseline'",
                (json.dumps(base),))
    conn.commit()
    conn.close()
    print(f"\nBaseline обновлён + снапшот {snap_key}.")
    print("Топ-5 претендентов:")
    for t, d in sorted(probs.items(), key=lambda x: -x[1]["W"])[:5]:
        print(f"  {t}: чемпион {d['W']*100:.1f}% · финал {d['F']*100:.1f}%")


if __name__ == "__main__":
    main()
