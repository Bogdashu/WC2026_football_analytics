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

# ---- УМНОЕ обновление Elo по ходу ЧМ (5 улучшений) ----
# 1) G_CAP=2.0 — лимит на разгром (anti-flukes)
# 2) xG-based результат если xG доступен в wc2026_fixtures.xg_home/xg_away,
#    иначе fallback на реальный счёт (1/0/0.5)
# 3) Round-aware K: group<R32<R16<QF<SF<F (финал двигает сильнее)
# 4) Damp K при огромном разрыве Elo (>300): K *= 0.7
# 5) Bayesian shrinkage с НАКОПИТЕЛЬНОЙ ПАМЯТЬЮ (surprise_credit):
#    Не строгий стрик подряд, а decay-сумма «сюрпризности» за все матчи команды.
#    Победа над фаворитом + ничья + ещё победа — копит credit, ничья не сбрасывает.
#    credit ↑ → shrinkage ↓ → форма пропускается в Elo (команда играет реально сильнее).
ELO_K_BY_ROUND = {"group": 50, "r32": 60, "r16": 70, "qf": 80, "sf": 90, "f": 95, "3rd": 65}
ELO_HFA = 80                  # бонус хозяину поля (host=1)
DEFAULT_ELO = 1500
G_CAP = 2.0                   # лимит множителя разницы мячей
ELO_GAP_DAMP_THRESHOLD = 300  # с какого разрыва дампим K
ELO_GAP_DAMP_FACTOR = 0.7     # на сколько дампим
UPSET_THRESHOLD = 0.4         # |result - expected| > 0.4 = сенсация (триггер shrinkage)
SHRINK_BASE = 0.25            # max shrink при credit=0 (первый сюрприз в карьере)
SHRINK_CREDIT_FULL = 0.8      # credit ≥ этого → shrink=0 (форма подтверждена)
SURPRISE_DECAY = 0.85         # ослабление credit с каждым новым матчем команды
SURPRISE_FLOOR = 0.15         # |r-e| ниже этого не считаем за сюрприз (шум модели)
XG_SCALE = 0.8                # масштаб для sigmoid от xG-diff

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

from wc2026_names import canon  # edinyy istochnik pravdy dlya imyon komand


def api_get(path: str, api_key: str) -> dict:
    import time, socket, http.client
    url = API_BASE + path
    headers = {
        "X-Auth-Token": api_key,
        "User-Agent": "WC2026-bot/1.0 (+https://github.com/Bogdashu/WC2026_football_analytics)",
        "Accept": "application/json",
        "Connection": "close",
    }
    last_err = None
    for attempt in range(1, 7):
        try:
            req = urllib.request.Request(url, headers=headers)
            log.info("GET %s (attempt %d)", url, attempt)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            # 429 = rate limit: respect Retry-After then retry; other HTTP errors are fatal.
            if e.code == 429 and attempt < 6:
                try:
                    wait = int(e.headers.get("Retry-After", "6") or "6")
                except Exception:
                    wait = 6
                log.warning("api_get 429 rate-limited; waiting %ss", wait)
                time.sleep(wait); last_err = e; continue
            raise
        except (urllib.error.URLError, http.client.RemoteDisconnected,
                http.client.IncompleteRead, ConnectionError,
                socket.timeout, OSError) as e:
            # Transient network failure (e.g. RemoteDisconnected) -> exponential backoff retry.
            last_err = e
            wait = min(2 ** attempt, 30)
            log.warning("api_get transient error (%r); retry %d/6 in %ss", e, attempt, wait)
            time.sleep(wait)
    raise RuntimeError(
        "api_get failed after retries: " + repr(last_err) +
        " — likely a temporary football-data.org/Cloudflare outage, or the host is "
        "dropping the cloud (Railway) egress IP. Run wc2026_check_api.py to diagnose.")


def ensure_score_columns(conn):
    with conn.cursor() as cur:
        cur.execute("""
            ALTER TABLE wc2026_fixtures
            ADD COLUMN IF NOT EXISTS home_score       INT,
            ADD COLUMN IF NOT EXISTS away_score       INT,
            ADD COLUMN IF NOT EXISTS xg_home          FLOAT,
            ADD COLUMN IF NOT EXISTS xg_away          FLOAT,
            ADD COLUMN IF NOT EXISTS round            TEXT DEFAULT 'group',
            ADD COLUMN IF NOT EXISTS advance_winner   TEXT,
            ADD COLUMN IF NOT EXISTS elo_applied      BOOLEAN DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS elo_home_before  FLOAT,
            ADD COLUMN IF NOT EXISTS elo_home_after   FLOAT,
            ADD COLUMN IF NOT EXISTS elo_away_before  FLOAT,
            ADD COLUMN IF NOT EXISTS elo_away_after   FLOAT
        """)
        cur.execute("""
            ALTER TABLE wc2026_elo
            ADD COLUMN IF NOT EXISTS surprise_streak INT   DEFAULT 0,
            ADD COLUMN IF NOT EXISTS surprise_credit FLOAT DEFAULT 0
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS wc2026_elo_baseline (
                team        TEXT PRIMARY KEY,
                elo         FLOAT NOT NULL,
                captured_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS wc2026_meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
    conn.commit()
    log.info("Ensured fixtures/elo columns + baseline/meta tables")


def _expected_score(elo_a: float, elo_b: float) -> float:
    """Ожидаемый результат команды A против B (0..1) по Elo."""
    return 1.0 / (1.0 + 10.0 ** ((elo_b - elo_a) / 400.0))


def _goal_diff_multiplier(gd: int) -> float:
    """Множитель по разнице мячей (World Football Elo). Капнут на G_CAP=2.0."""
    g = abs(gd)
    if g <= 1: m = 1.0
    elif g == 2: m = 1.5
    else: m = (11.0 + g) / 8.0   # 3:1.75, 4:1.875, 5:2.0, 6:2.125...
    return min(m, G_CAP)


def _result_from_xg(xg_h: float, xg_a: float) -> float:
    """Мягкий результат [0..1] из xG-diff (идея #2).
    1:0 с xG 0.4 vs 2.1 → result около 0.12 (хозяева не должны были выиграть)"""
    import math
    diff = float(xg_h) - float(xg_a)
    return 1.0 / (1.0 + math.exp(-diff / XG_SCALE))


def _elo_update_for_match(elo_h, elo_a, hs, as_, host, *,
                           round_name="group", xg_h=None, xg_a=None,
                           credit_h=0.0, credit_a=0.0):
    """Умный Elo update со SMART-CREDIT (накопительная память сюрпризов).

    5 шагов: K по раунду → damp при gap>300 → result xG-aware → G с капом →
    shrinkage по credit (НЕ строгий стрик, а decay-сумма по всем матчам).

    credit копится так: после каждого матча команды добавляется
        contrib = max(0, |r-e| - SURPRISE_FLOOR)
    к credit'у с decay'ем:  new_credit = old_credit * SURPRISE_DECAY + contrib
    Это значит: ничья/ожидаемый результат не обнуляют credit, а только мягко
    его сворачивают — серия «победа сильного → ничья → ещё победа» сохраняет память.

    Возвращает (new_h, new_a, new_credit_h, new_credit_a, dbg)."""
    # 1+3) K по раунду
    K = ELO_K_BY_ROUND.get((round_name or "group").lower(), 60)
    # 4) Дамп при огромном разрыве (бразиля vs гаити)
    if abs(elo_h - elo_a) > ELO_GAP_DAMP_THRESHOLD:
        K *= ELO_GAP_DAMP_FACTOR
    # HFA
    elo_h_eff = elo_h + (ELO_HFA if int(host) == 1 else 0)
    e_home = _expected_score(elo_h_eff, elo_a)
    e_away = 1.0 - e_home
    # 2) Результат: xG если есть, иначе фактический счёт
    if xg_h is not None and xg_a is not None:
        r_home = _result_from_xg(xg_h, xg_a)
        r_away = 1.0 - r_home
        result_src = "xG"
    else:
        if hs > as_: r_home, r_away = 1.0, 0.0
        elif hs < as_: r_home, r_away = 0.0, 1.0
        else: r_home, r_away = 0.5, 0.5
        result_src = "goals"
    # 1) Множитель разницы (с G_CAP)
    g_mult = _goal_diff_multiplier(int(hs) - int(as_))
    base_dh = K * g_mult * (r_home - e_home)
    base_da = K * g_mult * (r_away - e_away)

    # 5) Smart shrinkage по PRE-match credit'у каждой команды.
    # Сюрприз с высоким накопленным credit'ом → форма реальна → почти не душим.
    is_upset = abs(r_home - e_home) > UPSET_THRESHOLD
    def _shrink(c):
        if not is_upset:
            return 0.0
        return SHRINK_BASE * max(0.0, 1.0 - float(c) / SHRINK_CREDIT_FULL)
    sh_h = _shrink(credit_h)
    sh_a = _shrink(credit_a)
    dh = base_dh * (1.0 - sh_h)
    da = base_da * (1.0 - sh_a)
    new_h = elo_h + dh
    new_a = elo_a + da

    # Обновляем credit обеих команд (матч одинаково «удивителен» для обеих).
    surprise_contrib = max(0.0, abs(r_home - e_home) - SURPRISE_FLOOR)
    new_credit_h = float(credit_h) * SURPRISE_DECAY + surprise_contrib
    new_credit_a = float(credit_a) * SURPRISE_DECAY + surprise_contrib

    dbg = {
        "K": round(K, 1), "G": round(g_mult, 2),
        "expected_h": round(e_home, 3), "result_h": round(r_home, 3),
        "upset": is_upset, "src": result_src,
        "sh_h": round(sh_h, 3), "sh_a": round(sh_a, 3),
        "dh": round(dh, 1), "da": round(da, 1),
        "contrib": round(surprise_contrib, 3),
    }
    return new_h, new_a, new_credit_h, new_credit_a, dbg


def apply_elo_updates(conn):
    """УМНОЕ обновление wc2026_elo по всем закрытым матчам с elo_applied=FALSE.
    Хронологический проход, идемпотентность через флаг elo_applied.
    Учитывает: round, xG (если есть), gap-damp, G-cap, credit-aware shrinkage.
    Также сохраняет ПО-МАТЧЕВЫЙ снапшот elo (before/after) прямо в wc2026_fixtures —
    чтобы пост-сверка результатов мог показать «было→стало» для каждой команды."""
    # 0) Захватываем pre-tournament baseline один раз (для пост-сводки).
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM wc2026_elo_baseline")
        if int(cur.fetchone()[0]) == 0:
            cur.execute(
                "INSERT INTO wc2026_elo_baseline (team, elo) "
                "SELECT team, elo FROM wc2026_elo "
                "ON CONFLICT (team) DO NOTHING"
            )
            conn.commit()
            log.info("wc2026_elo_baseline: snapshot pre-tournament Elo captured")

    # Загружаем Elo + credit в память.
    elo: dict = {}
    credit: dict = {}
    with conn.cursor() as cur:
        cur.execute("SELECT team, elo, COALESCE(surprise_credit, 0) FROM wc2026_elo")
        for t, e, c in cur.fetchall():
            elo[t] = float(e)
            credit[t] = float(c)

    if not elo:
        log.warning("wc2026_elo пуста — пропускаю Elo обновление")
        return 0

    # Разрешаем имя команды из fixtures в КЛЮЧ wc2026_elo по канону,
    # чтобы варианты (Czechia/Czech Republic) не плодили «призрачные» строки.
    from wc2026_names import canon as _canon
    _elo_index = {_canon(k): k for k in elo}
    def _resolve_elo_key(name):
        return _elo_index.get(_canon(name), name)

    # Все закрытые матчи без применённого Elo, в порядке игры, с round + xG.
    with conn.cursor() as cur:
        cur.execute("""
            SELECT match_date, home, away, home_score, away_score,
                   COALESCE(NULLIF(host, ''), '0'), COALESCE(round, 'group'),
                   xg_home, xg_away
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
        for d, home, away, hs, as_, host, rnd, xg_h, xg_a in pending:
            hk = _resolve_elo_key(home); ak = _resolve_elo_key(away)
            eh = elo.get(hk, DEFAULT_ELO)
            ea = elo.get(ak, DEFAULT_ELO)
            ch = credit.get(hk, 0.0)
            ca = credit.get(ak, 0.0)
            new_h, new_a, new_ch, new_ca, dbg = _elo_update_for_match(
                eh, ea, int(hs), int(as_), int(host),
                round_name=rnd, xg_h=xg_h, xg_a=xg_a,
                credit_h=ch, credit_a=ca,
            )
            elo[hk] = new_h
            elo[ak] = new_a
            credit[hk] = new_ch
            credit[ak] = new_ca
            # UPSERT Elo + credit для обеих команд.
            cur.execute(
                "INSERT INTO wc2026_elo (team, elo, surprise_credit) VALUES (%s, %s, %s) "
                "ON CONFLICT (team) DO UPDATE SET elo = EXCLUDED.elo, surprise_credit = EXCLUDED.surprise_credit",
                (hk, new_h, new_ch),
            )
            cur.execute(
                "INSERT INTO wc2026_elo (team, elo, surprise_credit) VALUES (%s, %s, %s) "
                "ON CONFLICT (team) DO UPDATE SET elo = EXCLUDED.elo, surprise_credit = EXCLUDED.surprise_credit",
                (ak, new_a, new_ca),
            )
            # Снапшот before/after в строку матча (для постов «было→стало»).
            cur.execute(
                "UPDATE wc2026_fixtures SET elo_applied = TRUE, "
                "elo_home_before = %s, elo_home_after = %s, "
                "elo_away_before = %s, elo_away_after = %s "
                "WHERE match_date = %s AND home = %s AND away = %s",
                (eh, new_h, ea, new_a, d, home, away),
            )
            applied += 1
            log.info(
                "Elo[%s] %s %d:%d %s | K=%s G=%s exp=%s r=%s %s sh=(%.2f,%.2f) +cr=%s | "
                "%s: %.0f→%.0f cr%.2f→%.2f | %s: %.0f→%.0f cr%.2f→%.2f",
                rnd, home, hs, as_, away,
                dbg["K"], dbg["G"], dbg["expected_h"], dbg["result_h"], dbg["src"],
                dbg["sh_h"], dbg["sh_a"], dbg["contrib"],
                home, eh, new_h, ch, new_ch,
                away, ea, new_a, ca, new_ca,
            )
    conn.commit()
    log.info("Elo: обновлено %d матчей (smart-credit)", applied)
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
    except (urllib.error.HTTPError, RuntimeError) as e:
        # Try without season filter if API returns an error OR dropped the connection
        log.warning("Season-filtered request failed (%s); retrying without season filter...", e)
        time.sleep(2)
        data = api_get(f"/competitions/{COMP_CODE}/matches", API_KEY)

    matches = data.get("matches", [])
    log.info("Fetched %d matches from API", len(matches))

    finished = [m for m in matches if m.get("status") == "FINISHED"]
    log.info("%d finished matches", len(finished))

    # Index ALL fixtures by the CANONICAL pair of team names, so spelling
    # variants (Czechia/Czech Republic, Korea Republic/South Korea, ...) line up
    # with whatever football-data.org returns.
    fixtures_index = {}
    with conn.cursor() as cur:
        cur.execute("SELECT match_date, home, away FROM wc2026_fixtures")
        for fd, fh, fa in cur.fetchall():
            fixtures_index.setdefault(frozenset((canon(fh), canon(fa))), []).append((fd, fh, fa))

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
        # Плей-офф: кто прошёл дальше при ничьей (доп. время / серия пенальти).
        # football-data: score.winner = HOME_TEAM / AWAY_TEAM / DRAW.
        winner_flag = (m.get("score", {}) or {}).get("winner")
        adv_raw = None
        if hs == as_ and winner_flag in ("HOME_TEAM", "AWAY_TEAM"):
            adv_raw = home_raw if winner_flag == "HOME_TEAM" else away_raw

        cands = fixtures_index.get(frozenset((canon(home_raw), canon(away_raw))), [])
        if not cands:
            not_found.append((home_raw, away_raw, home, away))
            continue
        with conn.cursor() as cur:
            for fd, fh, fa in cands:
                # Orient the score to the fixture's stored home/away order.
                if canon(fh) == canon(home_raw):
                    sh, sa = hs, as_
                else:
                    sh, sa = as_, hs
                adv = None
                if adv_raw is not None:
                    adv = fh if canon(fh) == canon(adv_raw) else fa
                cur.execute(
                    "UPDATE wc2026_fixtures SET home_score = %s, away_score = %s, "
                    "advance_winner = COALESCE(%s, advance_winner) "
                    "WHERE match_date = %s AND home = %s AND away = %s",
                    (sh, sa, adv, fd, fh, fa),
                )
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
