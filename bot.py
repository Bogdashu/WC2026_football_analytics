#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""bot.py — @wc2026_football_bot  (Phase 2.1, full UX + prediction logging)

WC2026 AI прогнозы + живое обновление + уведомления + запись прогнозов в БД.

ENV:
  BOT_TOKEN                required
  DATABASE_PUBLIC_URL      required
  CHANNEL_ID               optional  (-100... или @WC2026Neuro)
  ADMIN_USER_ID            optional  (your Telegram user id)
  FOOTBALL_DATA_API_KEY    optional  (for /update results ingest)
  THE_ODDS_API_KEY         optional  (live odds; не постится в канал)
  MORNING_POST_UTC_HOUR    optional  (default 2  = 09:00 Новосибирск)
  RESULTS_POST_UTC_HOUR    optional  (default 16 = 23:00 Новосибирск)
"""

import os, sys, math, json, asyncio, logging, difflib, subprocess
import urllib.request, urllib.error
from datetime import date, time as dtime, timedelta, timezone, datetime
from html import escape as html_escape
from collections import defaultdict

import psycopg2
from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s | %(message)s")
log = logging.getLogger("wc2026")

# ---- Globals ----
DB_URL:     str  = ""
ELO:        dict = {}
BASELINE:   dict = {}
CALIBRATOR: dict = {}

HOME_ADV_ELO = 90.0
DEFAULT_ELO  = 1500.0
MAX_NEXT      = 20
SIMS_DISPLAY  = 100000

# Visual separators (defined ONCE so we never hit the
# "literal" "x"*N implicit-concat trap that repeated the header 22x).
SEP  = "\u2501" * 22          # ━━━ heavy rule
DASH = "\u2500" * 22          # ─── light rule

# Stage ladder (fixes the old R32/R16 off-by-one). Codes name the round actually
# PLAYED: R32 = 1/16 финала, R16 = 1/8, QF = 1/4, SF = 1/2, F = финал.
STAGE_LABELS = {
    "R32": "1/16 финала",
    "R16": "1/8 финала",
    "QF":  "1/4 финала (четвертьфинал)",
    "SF":  "1/2 финала (полуфинал)",
    "F":   "Финал",
    "W":   "Чемпион",
}
STAGE_EMOJI = {"R32":"🔟","R16":"⚽","QF":"🏅","SF":"🥈","F":"🏆","W":"👑"}
STAGE_PROB_KEY = {"R32":"P_R32","R16":"P_R16","QF":"P_QF","SF":"P_SF","F":"P_F","W":"P_W"}

RU_MONTHS = {
    1:"января",2:"февраля",3:"марта",4:"апреля",
    5:"мая",6:"июня",7:"июля",8:"августа",
    9:"сентября",10:"октября",11:"ноября",12:"декабря",
}

# ============================================================
# MODAL BRACKET — argmax from 100 000 sims (seed=42, with odds)
# ============================================================
# ===== RU localization helpers (injected) =====
RU_TEAMS = {
    "Argentina":"Аргентина","Brazil":"Бразилия","France":"Франция","Spain":"Испания",
    "England":"Англия","Portugal":"Португалия","Germany":"Германия","Netherlands":"Нидерланды",
    "Belgium":"Бельгия","Croatia":"Хорватия","Italy":"Италия","Uruguay":"Уругвай",
    "Colombia":"Колумбия","Mexico":"Мексика","United States":"США","USA":"США",
    "Canada":"Канада","Japan":"Япония","South Korea":"Южная Корея","Korea Republic":"Южная Корея",
    "Morocco":"Марокко","Senegal":"Сенегал","Switzerland":"Швейцария","Denmark":"Дания",
    "Austria":"Австрия","Turkey":"Турция","Turkiye":"Турция","Türkiye":"Турция",
    "Ecuador":"Эквадор","Iran":"Иран","Australia":"Австралия","Scotland":"Шотландия",
    "Norway":"Норвегия","Sweden":"Швеция","Poland":"Польша","Ukraine":"Украина",
    "Serbia":"Сербия","Wales":"Уэльс","Czech Republic":"Чехия","Czechia":"Чехия",
    "Ivory Coast":"Кот-д'Ивуар","Cote d'Ivoire":"Кот-д'Ивуар","Côte d'Ivoire":"Кот-д'Ивуар",
    "Algeria":"Алжир","Egypt":"Египет","Nigeria":"Нигерия","Ghana":"Гана",
    "Cameroon":"Камерун","Tunisia":"Тунис","Panama":"Панама","Costa Rica":"Коста-Рика",
    "Jamaica":"Ямайка","Paraguay":"Парагвай","Peru":"Перу","Chile":"Чили",
    "Venezuela":"Венесуэла","Bolivia":"Боливия","Saudi Arabia":"Саудовская Аравия","Qatar":"Катар",
    "Iraq":"Ирак","United Arab Emirates":"ОАЭ","UAE":"ОАЭ","Jordan":"Иордания",
    "Uzbekistan":"Узбекистан","New Zealand":"Новая Зеландия","South Africa":"ЮАР","Mali":"Мали",
    "DR Congo":"ДР Конго","Congo DR":"ДР Конго","Cape Verde":"Кабо-Верде","Curacao":"Кюрасао","Curaçao":"Кюрасао",
    "Honduras":"Гондурас","Greece":"Греция","Romania":"Румыния","Hungary":"Венгрия",
    "Slovakia":"Словакия","Slovenia":"Словения","Russia":"Россия",
    "Bosnia-Herzegovina":"Босния и Герцеговина","Bosnia and Herzegovina":"Босния и Герцеговина",
    "Republic of Ireland":"Ирландия","Ireland":"Ирландия","Northern Ireland":"Северная Ирландия",
    "Finland":"Финляндия","Iceland":"Исландия","Albania":"Албания","North Macedonia":"Северная Македония",
    "Georgia":"Грузия","Montenegro":"Черногория","China":"Китай","Indonesia":"Индонезия",
    "Thailand":"Таиланд","Vietnam":"Вьетнам","India":"Индия","Bahrain":"Бахрейн",
    "Oman":"Оман","Kuwait":"Кувейт","Palestine":"Палестина","Angola":"Ангола",
    "Zambia":"Замбия","Kenya":"Кения","Gabon":"Габон","Burkina Faso":"Буркина-Фасо",
    "Guinea":"Гвинея","Benin":"Бенин","Equatorial Guinea":"Экваториальная Гвинея","Namibia":"Намибия",
    "Mozambique":"Мозамбик","Madagascar":"Мадагаскар","Uganda":"Уганда","Tanzania":"Танзания",
    "El Salvador":"Сальвадор","Guatemala":"Гватемала","Trinidad and Tobago":"Тринидад и Тобаго",
    "Suriname":"Суринам","Haiti":"Гаити","Nicaragua":"Никарагуа",
}
def ru_team(name):
    if name is None: return name
    return RU_TEAMS.get(str(name).strip(), str(name))
def rt(x): return esc(ru_team(x))
def _nfkc(s):
    return _ud.normalize("NFKC", s) if isinstance(s, str) else s
def group_xpoints(letter):
    teams=get_group_teams(letter)
    xp={t:0.0 for t in teams}
    for i in range(len(teams)):
        for j in range(i+1,len(teams)):
            a=teams[i]; b=teams[j]
            p_h,p_d,p_a=predict_1x2(a,b,neutral=True)
            xp[a]+=3*p_h+p_d
            xp[b]+=3*p_a+p_d
    return xp
def _parse_modal_knockout_strings(mk):
    """Parse the string form 'CODE (n): A vs B -> W; ...' into bracket rounds.
    Tolerates both the new clean codes (R32/R16/QF/SF/F) and legacy labels."""
    legacy = {"R16":"R32","QF":"R16","SF":"QF","Final":"SF","F":"F","W":"W","R32":"R32"}
    rounds = []
    for line in mk:
        if not isinstance(line, str) or ":" not in line:
            continue
        head, _, body = line.partition(":")
        code = head.strip().split()[0] if head.strip() else ""
        code = legacy.get(code, code)
        matches = []
        for part in body.split(";"):
            part = part.strip()
            if not part or "->" not in part:
                continue
            pair, _, w = part.partition("->")
            w = w.strip()
            ha = pair.split(" vs ")
            h = ha[0].strip() if ha else "?"
            a = ha[1].strip() if len(ha) > 1 else "?"
            matches.append({"home": h, "away": a, "winner": w})
        if matches:
            rounds.append({"code": code, "matches": matches})
    return rounds


def _bracket_blocks_from(data):
    """Render the modal knockout bracket from a baseline/snapshot dict as
    'Команда A ➡️ Команда B → 🏆 Победитель · счёт · NN%' for every stage.
    Falls back gracefully for snapshots produced by the old simulator."""
    data = data or {}
    modal = data.get("modal_forecast", {}) or {}
    bracket = data.get("modal_bracket") or {}
    rounds = bracket.get("rounds") or []
    champ = bracket.get("champion") or modal.get("modal_champion", "?")
    if not rounds:
        mk = data.get("modal_knockout") or modal.get("modal_knockout") or []
        rounds = _parse_modal_knockout_strings(mk)
    if not rounds:
        return ["<i>Полная сетка плей-офф появится после следующего прогона "
                "симулятора (этот снимок сделан старой версией).</i>"]
    out = []
    for rd in rounds:
        code = rd.get("code", "")
        label = STAGE_LABELS.get(code, code or "Плей-офф")
        emoji = STAGE_EMOJI.get(code, "•")
        lines = []
        for m in rd.get("matches", []):
            h = rt(m.get("home")); a = rt(m.get("away")); w = rt(m.get("winner"))
            if m.get("adv") is not None:
                tail = f" · {float(m['adv'])*100:.0f}%"
            else:
                tail = ""
            lines.append(f"  {h} vs {a} → 🏆 <b>{w}</b>{tail}")
        out.append(f"{emoji} <b>{label}:</b>\n" + "\n".join(lines))
    cp = bracket.get("champion_prob")
    cp_txt = f" · {float(cp)*100:.1f}%" if cp else ""
    out.append(f"👑 <b>ЧЕМПИОН: {rt(champ)}</b>{cp_txt}")
    return out


def _bracket_blocks():
    return _bracket_blocks_from(BASELINE)




# ============================================================
# DB
# ============================================================

def get_conn():
    return psycopg2.connect(DB_URL, connect_timeout=20)

SQUAD_VALUE = {}
SQUADS = {}

# ---- official FIFA WC 2026 group letters (self-healing) ----
_OFFICIAL_GROUPS = {
    "A": ["Mexico","South Africa","South Korea","Czech Republic"],
    "B": ["Canada","Bosnia and Herzegovina","Qatar","Switzerland"],
    "C": ["Brazil","Morocco","Haiti","Scotland"],
    "D": ["USA","Paraguay","Australia","Turkey"],
    "E": ["Germany","Cura\u00e7ao","Ivory Coast","Ecuador"],
    "F": ["Netherlands","Japan","Sweden","Tunisia"],
    "G": ["Belgium","Egypt","Iran","New Zealand"],
    "H": ["Spain","Cape Verde","Saudi Arabia","Uruguay"],
    "I": ["France","Senegal","Iraq","Norway"],
    "J": ["Argentina","Algeria","Austria","Jordan"],
    "K": ["Portugal","DR Congo","Uzbekistan","Colombia"],
    "L": ["England","Croatia","Ghana","Panama"],
}
_GROUP_ALIASES = {
    "cote d ivoire":"ivory coast","czechia":"czech republic",
    "united states":"usa","korea republic":"south korea",
    "republic of korea":"south korea","bosnia":"bosnia and herzegovina",
    "curacao":"cura\u00e7ao","dr congo":"dr congo",
    "democratic republic of the congo":"dr congo","congo dr":"dr congo",
    "cape verde islands":"cape verde","saudi":"saudi arabia",
    "netherland":"netherlands","holland":"netherlands",
}
import unicodedata as _ud
def _norm_team(name):
    if not name: return ""
    s = _ud.normalize("NFKD", str(name))
    s = "".join(ch for ch in s if not _ud.combining(ch))
    s = s.lower().replace("'"," ").replace("-"," ").replace("."," ")
    s = " ".join(s.split())
    return _GROUP_ALIASES.get(s, s)
_OFFICIAL_NORM = {}
for _L,_ts in _OFFICIAL_GROUPS.items():
    for _t in _ts: _OFFICIAL_NORM[_norm_team(_t)] = _L

def _relabel_groups_dict(old_groups):
    """Re-letter group keys to official A-L by majority vote on teams."""
    from collections import Counter
    import string
    proposals = {}
    for old_letter, teams in old_groups.items():
        votes = Counter()
        for t in teams or []:
            off = _OFFICIAL_NORM.get(_norm_team(t))
            if off: votes[off] += 1
        proposals[old_letter] = votes
    assigned = {}; used = set()
    order = sorted(proposals.keys(),
                   key=lambda k: -(proposals[k].most_common(1)[0][1] if proposals[k] else 0))
    for old_letter in order:
        for cand,_ in proposals[old_letter].most_common():
            if cand not in used:
                assigned[old_letter] = cand; used.add(cand); break
    free = [L for L in string.ascii_uppercase[:12] if L not in used]
    for old_letter in old_groups:
        if old_letter not in assigned:
            assigned[old_letter] = free.pop(0); used.add(assigned[old_letter])
    new = {}
    for old_letter, teams in old_groups.items():
        new[assigned[old_letter]] = list(teams or [])
    return {L: new[L] for L in sorted(new.keys())}

def _fix_groups_table(cur):
    """Repair wc2026_groups (team->group_name) to official letters.
    This table is what /forecast reads via get_group_teams()."""
    try:
        cur.execute("SELECT team, group_name FROM wc2026_groups")
        rows = cur.fetchall()
    except Exception as e:
        log.warning("_fix_groups_table read failed: %s", e); return 0
    fixed = 0
    for team, old_letter in rows:
        off = _OFFICIAL_NORM.get(_norm_team(team))
        if off and off != old_letter:
            cur.execute("UPDATE wc2026_groups SET group_name=%s WHERE team=%s",
                        (off, team))
            fixed += 1
    return fixed

def _relabel_top2(g2):
    """Re-key modal_forecast.group_top2 by official group letter.
    Team lists stay; only the (possibly wrong) letter keys are fixed."""
    if not g2: return g2, False
    new = {}
    for old_letter, teams in g2.items():
        off = None
        for t in (teams or []):
            off = _OFFICIAL_NORM.get(_norm_team(t))
            if off: break
        new[off or old_letter] = teams
    out = {L: new[L] for L in sorted(new.keys())}
    return out, (out != g2)

def ensure_group_letters():
    """Self-heal group letters across the baseline artifact, the
    wc2026_groups table, and modal_forecast.group_top2. Runs at every
    load; no-op when everything is already official."""
    global _GROUP_MAP
    g = BASELINE.get("groups") or {}
    new = _relabel_groups_dict(g) if g else g
    baseline_needs_fix = bool(g) and new != g
    if baseline_needs_fix:
        BASELINE["groups"] = new
    modal = BASELINE.get("modal_forecast") or {}
    g2 = modal.get("group_top2") or {}
    new_g2, g2_changed = _relabel_top2(g2)
    if g2_changed:
        modal["group_top2"] = new_g2
        BASELINE["modal_forecast"] = modal
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                if baseline_needs_fix:
                    cur.execute(
                        "UPDATE wc2026_artifacts SET content = jsonb_set(content,'{groups}',%s::jsonb) "
                        "WHERE key='baseline'",
                        (json.dumps(new, ensure_ascii=False),))
                    log.info("ensure_group_letters: relabeled baseline groups -> %s",
                             " ".join(sorted(new.keys())))
                if g2_changed:
                    cur.execute(
                        "UPDATE wc2026_artifacts SET content = jsonb_set(content,'{modal_forecast,group_top2}',%s::jsonb) "
                        "WHERE key='baseline'",
                        (json.dumps(new_g2, ensure_ascii=False),))
                    log.info("ensure_group_letters: re-keyed group_top2 -> %s",
                             " ".join(sorted(new_g2.keys())))
                table_fixed = _fix_groups_table(cur)
            conn.commit()
        if table_fixed or g2_changed:
            _GROUP_MAP = None  # bust cache so new letters take effect
        if table_fixed:
            log.info("ensure_group_letters: fixed %d rows in wc2026_groups", table_fixed)
    except Exception as e:
        log.warning("ensure_group_letters DB write failed: %s", e)
    return baseline_needs_fix or g2_changed


def load_all():
    global ELO, BASELINE, CALIBRATOR, SQUAD_VALUE, SQUADS, _GROUP_MAP
    _GROUP_MAP = None  # always re-read group map on reload
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT team, elo FROM wc2026_elo")
            ELO = {t: float(e) for t, e in cur.fetchall()}
            cur.execute("SELECT key, content FROM wc2026_artifacts")
            arts = {k: v for k, v in cur.fetchall()}
    BASELINE   = arts.get("baseline", {}) or {}
    CALIBRATOR = (arts.get("calibrator") or
                  BASELINE.get("calibrator") or
                  arts.get("goalmodel") or {})
    SQUAD_VALUE = arts.get("squad_values", {}) or {}
    SQUADS      = arts.get("squads", {}) or {}
    if not SQUAD_VALUE or not SQUADS:
        _v,_s=_load_squads_from_csv()
        if not SQUAD_VALUE: SQUAD_VALUE=_v
        if not SQUADS: SQUADS=_s
    log.info("Loaded: %d elo | %d contenders | played: %s/%s",
             len(ELO),
             len(BASELINE.get("tournament_probs",{})),
             BASELINE.get("matches_played","?"),
             BASELINE.get("matches_total",72))
    # self-heal: fix wrong group letters in baseline JSON if needed
    try: ensure_group_letters()
    except Exception as e: log.warning("ensure_group_letters: %s", e)

def ensure_predictions_table():
    """Auto-create the prediction log table (idempotent)."""
    ddl = """
    CREATE TABLE IF NOT EXISTS wc2026_predictions (
        id           SERIAL PRIMARY KEY,
        match_date   DATE,
        home         TEXT,
        away         TEXT,
        stage        TEXT DEFAULT 'group',
        pred_code    TEXT,
        pred_label   TEXT,
        confidence   TEXT,
        p_home       FLOAT,
        p_draw       FLOAT,
        p_away       FLOAT,
        odds_1       FLOAT,
        odds_x       FLOAT,
        odds_2       FLOAT,
        predicted_at TIMESTAMP DEFAULT now(),
        actual_home  INT,
        actual_away  INT,
        actual_code  TEXT,
        correct      BOOLEAN,
        resolved_at  TIMESTAMP,
        UNIQUE (match_date, home, away)
    );"""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(ddl)
                cur.execute("ALTER TABLE wc2026_predictions ADD COLUMN IF NOT EXISTS pred_mode TEXT DEFAULT 'prematch'")
                cur.execute("ALTER TABLE wc2026_predictions ADD COLUMN IF NOT EXISTS pred_score_h INT")
                cur.execute("ALTER TABLE wc2026_predictions ADD COLUMN IF NOT EXISTS pred_score_a INT")
                cur.execute("ALTER TABLE wc2026_predictions ADD COLUMN IF NOT EXISTS exact_correct BOOLEAN")
            conn.commit()
        log.info("wc2026_predictions table ready")
    except Exception as e:
        log.warning("ensure_predictions_table: %s", e)

_GROUP_MAP=None
def _load_group_map():
    """Cache {english_team: group_name} once."""
    global _GROUP_MAP
    if _GROUP_MAP is not None: return _GROUP_MAP
    m={}
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT team,group_name FROM wc2026_groups")
                for t,g in cur.fetchall(): m[t]=g
    except Exception as e:
        log.warning("_load_group_map: %s",e)
    _GROUP_MAP=m; return m

def _norm_apo(s):
    return str(s or "").replace("\u2019","'").replace("\u2018","'").replace("\u02bc","'").replace("`","'")

def get_team_group(team):
    """Resolve a team's group, tolerant to spelling/apostrophe/RU-EN variants."""
    if not team: return None
    m=_load_group_map()
    if team in m: return m[team]
    keys=list(m.keys())
    # apostrophe-normalized lookup
    napo={_norm_apo(k).lower():v for k,v in m.items()}
    if _norm_apo(team).lower() in napo: return napo[_norm_apo(team).lower()]
    # bridge "Ivory Coast" vs "Cote d'Ivoire" etc. via Russian canonical name
    rim={ru_team(k):v for k,v in m.items()}
    if ru_team(team) in rim: return rim[ru_team(team)]
    rimL={ru_team(k).lower():v for k,v in m.items()}
    if ru_team(team).lower() in rimL: return rimL[ru_team(team).lower()]
    low={str(k).lower():v for k,v in m.items()}
    if str(team).lower() in low: return low[str(team).lower()]
    cand=difflib.get_close_matches(_norm_apo(team),[_norm_apo(k) for k in keys],n=1,cutoff=0.7)
    if cand:
        for k in keys:
            if _norm_apo(k)==cand[0]: return m[k]
    candr=difflib.get_close_matches(ru_team(team).lower(),list(rimL),n=1,cutoff=0.7)
    return rimL[candr[0]] if candr else None

def get_group_teams(letter):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT team FROM wc2026_groups WHERE group_name=%s ORDER BY team",(letter.upper(),))
                return [r[0] for r in cur.fetchall()]
    except: return []

def get_fixtures(start, end=None, limit=30):
    sql="SELECT match_date,home,away,host,odds_1,odds_x,odds_2 FROM wc2026_fixtures WHERE match_date>=%s"
    params=[start]
    if end: sql+=" AND match_date<=%s"; params.append(end)
    sql+=" ORDER BY match_date,home LIMIT %s"; params.append(limit)
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql,params); return cur.fetchall()
    except Exception as e:
        log.warning("get_fixtures: %s",e); return []

_KICKOFFS=None
def _load_kickoffs():
    """Cache kickoff_utc per match. Idempotently adds the column if missing."""
    global _KICKOFFS
    m={}
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("ALTER TABLE wc2026_fixtures ADD COLUMN IF NOT EXISTS kickoff_utc TIMESTAMPTZ")
                cur.execute("SELECT match_date,home,away,kickoff_utc FROM wc2026_fixtures WHERE kickoff_utc IS NOT NULL")
                for d,h,a,kt in cur.fetchall(): m[(d,h,a)]=kt
            conn.commit()
    except Exception as e:
        log.warning("_load_kickoffs: %s",e)
    _KICKOFFS=m

def get_kickoff(d,h,a):
    if _KICKOFFS is None: _load_kickoffs()
    return (_KICKOFFS or {}).get((d,h,a))

def fmt_msk(kt):
    """Render a UTC datetime as 'HH:MM \u041c\u0421\u041a' or '' if absent."""
    if not kt: return ""
    try:
        msk=kt.astimezone(timezone(timedelta(hours=3)))
        return msk.strftime("%H:%M")+" \u041c\u0421\u041a"
    except Exception:
        return ""

def get_finished_fixtures(match_date):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT match_date,home,away,home_score,away_score,host "
                    "FROM wc2026_fixtures WHERE match_date=%s AND home_score IS NOT NULL ORDER BY home",
                    (match_date,))
                return cur.fetchall()
    except: return []

def get_baseline_versions():
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT key, content->>'generated_at', content->>'matches_played', content->>'matches_total' "
                    "FROM wc2026_artifacts WHERE key LIKE 'baseline_%' ORDER BY key DESC LIMIT 15")
                return cur.fetchall()
    except: return []

def get_pending_notification():
    notif = BASELINE.get("pending_notification","")
    return notif or None

def clear_pending_notification():
    if not BASELINE.get("pending_notification"): return
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE wc2026_artifacts SET content = content - 'pending_notification' "
                    "WHERE key='baseline'")
            conn.commit()
        BASELINE.pop("pending_notification", None)
        log.info("Cleared pending_notification")
    except Exception as e:
        log.warning("clear_pending_notification: %s", e)


import re as _re_label
def make_snapshot_label(kind, with_time=False):
    """Единая схема лейблов снапшотов прогнозов: ГГГГ-ММ-ДД[_ЧЧММ]_<тип>.

    kind — тип прогноза:
      'auto'     — ежедневная авто-ресимуляция (полная, 30k)
      'manual'   — ручная полная ресимуляция (/sim_new)
      'prematch' — замороженный предтурнирный baseline
      'ingest'   — снапшот после загрузки результатов (без ресим)
    Лейбл сортируется хронологически; хвост документирует тип прогноза.
    Время (ЧЧММ, UTC) добавляется только при with_time=True (несколько прогонов в день).
    """
    now = datetime.utcnow()
    stamp = now.strftime("%Y-%m-%d")
    if with_time:
        stamp += now.strftime("_%H%M")
    return f"{stamp}_{kind}"


def snapshot_baseline(label, force=False):
    """Copy current 'baseline' content under key 'baseline_<label>'.

    The baseline JSON already contains tournament_probs (per-team R32..W),
    group_positions (per-team 1st..4th), mean_points (xPts), and
    modal_forecast (champion + bracket), so snapshotting the whole baseline
    preserves group+match-level forecasts at that moment in time.
    """
    if not label: return False
    label = _re_label.sub(r"[^A-Za-z0-9_\-]", "_", str(label)).strip("_")[:64]
    if not label: return False
    key = f"baseline_{label}"
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                if force:
                    cur.execute(
                        "INSERT INTO wc2026_artifacts (key, content) "
                        "SELECT %s, content FROM wc2026_artifacts WHERE key='baseline' "
                        "ON CONFLICT (key) DO UPDATE SET content = EXCLUDED.content",
                        (key,))
                else:
                    cur.execute(
                        "INSERT INTO wc2026_artifacts (key, content) "
                        "SELECT %s, content FROM wc2026_artifacts WHERE key='baseline' "
                        "ON CONFLICT (key) DO NOTHING",
                        (key,))
                created = cur.rowcount > 0
            conn.commit()
        log.info("snapshot_baseline: %s -> %s", key, "created" if created else "kept existing")
        return created
    except Exception as e:
        log.warning("snapshot_baseline(%s): %s", key, e)
        return False


def get_snapshot(key):
    """Load a stored snapshot's content (full baseline JSON)."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT content FROM wc2026_artifacts WHERE key=%s",(key,))
                row=cur.fetchone()
                return row[0] if row else None
    except Exception as e:
        log.warning("get_snapshot(%s): %s", key, e); return None


# ============================================================
# PREDICTION LOGGING  (сохраняем каждый прогноз один раз)
# ============================================================

def record_predictions(fixtures):
    """Store each prediction once. fixtures = rows of get_fixtures()."""
    if not fixtures: return 0
    mode = "live" if (BASELINE.get("matches_played") or 0) > 0 else "prematch"
    sql = ("INSERT INTO wc2026_predictions "
           "(match_date,home,away,stage,pred_mode,pred_code,pred_label,confidence,"
           " p_home,p_draw,p_away,odds_1,odds_x,odds_2,pred_score_h,pred_score_a) "
           "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
           "ON CONFLICT (match_date,home,away) DO NOTHING")
    n=0
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("ALTER TABLE wc2026_predictions ADD COLUMN IF NOT EXISTS pred_mode TEXT DEFAULT 'prematch'")
                cur.execute("ALTER TABLE wc2026_predictions ADD COLUMN IF NOT EXISTS pred_score_h INT")
                cur.execute("ALTER TABLE wc2026_predictions ADD COLUMN IF NOT EXISTS pred_score_a INT")
                cur.execute("ALTER TABLE wc2026_predictions ADD COLUMN IF NOT EXISTS exact_correct BOOLEAN")
                for d,home,away,host,o1,ox,o2 in fixtures:
                    neutral=str(host)!="1"
                    p_h,p_d,p_a=predict_1x2(home,away,neutral)
                    outcome,_,c_l,_=predict_natural(p_h,p_d,p_a,home,away)
                    code=outcome_code(p_h,p_d,p_a)
                    stage="group" if get_team_group(home) else "knockout"
                    try:
                        sc=predict_scoreline(home,away,neutral)
                        psh,psa=(sc[0][0],sc[0][1]) if sc else (None,None)
                    except Exception:
                        psh,psa=None,None
                    def _f(x):
                        try: return float(x)
                        except: return None
                    cur.execute(sql,(d,home,away,stage,mode,code,outcome,c_l,
                                     p_h,p_d,p_a,_f(o1),_f(ox),_f(o2),psh,psa))
                    n+=cur.rowcount
            conn.commit()
        log.info("record_predictions: %d new rows",n)
    except Exception as e:
        log.warning("record_predictions: %s",e)
    return n

def resolve_predictions(match_date):
    """Fill in actual results + correctness for a finished day."""
    finished=get_finished_fixtures(match_date)
    if not finished: return 0
    n=0
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                for d,home,away,hs,as_,host in finished:
                    actual="H" if hs>as_ else ("A" if as_>hs else "D")
                    cur.execute(
                        "UPDATE wc2026_predictions "
                        "SET actual_home=%s,actual_away=%s,actual_code=%s,"
                        "    correct=(pred_code=%s),"
                        "    exact_correct=(pred_score_h=%s AND pred_score_a=%s),"
                        "    resolved_at=now() "
                        "WHERE match_date=%s AND home=%s AND away=%s AND actual_code IS NULL",
                        (hs,as_,actual,actual,hs,as_,d,home,away))
                    n+=cur.rowcount
            conn.commit()
    except Exception as e:
        log.warning("resolve_predictions: %s",e)
    return n

def get_accuracy_stats():
    """Overall + by mode (prematch/live) + by stage (group/knockout)."""
    base={"correct":0,"resolved":0,"total":0,"by_mode":{},"by_stage":{}}
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FILTER (WHERE correct IS TRUE),"
                    "       COUNT(*) FILTER (WHERE actual_code IS NOT NULL),"
                    "       COUNT(*) FROM wc2026_predictions")
                c,r,t=cur.fetchone()
                base["correct"],base["resolved"],base["total"]=c or 0,r or 0,t or 0
                cur.execute(
                    "SELECT COALESCE(pred_mode,'prematch'),"
                    "       COUNT(*) FILTER (WHERE correct IS TRUE),"
                    "       COUNT(*) FILTER (WHERE actual_code IS NOT NULL) "
                    "FROM wc2026_predictions GROUP BY 1")
                for m,cc,rr in cur.fetchall(): base["by_mode"][m]=(cc or 0,rr or 0)
                cur.execute(
                    "SELECT COALESCE(stage,'group'),"
                    "       COUNT(*) FILTER (WHERE correct IS TRUE),"
                    "       COUNT(*) FILTER (WHERE actual_code IS NOT NULL) "
                    "FROM wc2026_predictions GROUP BY 1")
                for st,cc,rr in cur.fetchall(): base["by_stage"][st]=(cc or 0,rr or 0)
    except Exception as e:
        log.warning("get_accuracy_stats: %s",e)
    return base


# ============================================================
# PREDICTION MODEL
# ============================================================

def _apply_calibrator(p_a,p_d,p_h):
    W=CALIBRATOR.get("W"); c=CALIBRATOR.get("c")
    if not W or not c or len(W)!=3 or len(c)!=3: return p_h,p_d,p_a
    eps=1e-9
    lp=[math.log(max(p_a,eps)),math.log(max(p_d,eps)),math.log(max(p_h,eps))]
    logits=[c[i]+sum(W[i][j]*lp[j] for j in range(3)) for i in range(3)]
    mx=max(logits); exps=[math.exp(l-mx) for l in logits]; s=sum(exps)
    return exps[2]/s,exps[1]/s,exps[0]/s

SQUAD_ELO_W = 70.0   # max contribution of squad value, in Elo points
DRAW_MAX = 0.32
DRAW_MIN = 0.16

def _load_squads_from_csv():
    """Fallback: load squad values + rosters from CSV files in repo."""
    import csv, os
    val={}; sq={}
    try:
        if os.path.exists("wc2026_squad_values.csv"):
            with open("wc2026_squad_values.csv",encoding="utf-8-sig") as fh:
                for row in csv.DictReader(fh):
                    try: val[row["team"]]=float(row["value_eur"])
                    except: pass
    except Exception as e: log.warning("squad_values csv: %s",e)
    try:
        if os.path.exists("wc2026_squads.csv"):
            with open("wc2026_squads.csv",encoding="utf-8-sig") as fh:
                for row in csv.DictReader(fh):
                    sq.setdefault(row["team"],[]).append(
                        {"number":row.get("number",""),"pos":row.get("pos",""),
                         "player":row.get("player",""),"club":row.get("club",""),
                         "dob":row.get("dob","")})
    except Exception as e: log.warning("squads csv: %s",e)
    return val,sq

def _resolve_squad_key(team):
    """Map any spelling to the key used in SQUAD_VALUE/SQUADS via RU canonical."""
    if not team: return None
    if team in SQUAD_VALUE: return team
    if team in SQUADS: return team
    pool=list(SQUAD_VALUE) or list(SQUADS)
    if not pool: return None
    rv={ru_team(k):k for k in pool}
    k=rv.get(ru_team(team))
    if k: return k
    cand=difflib.get_close_matches(str(team),pool,n=1,cutoff=0.82)
    return cand[0] if cand else None

def squad_value(team):
    k=_resolve_squad_key(team)
    if not k: return None
    try: return float(SQUAD_VALUE.get(k))
    except: return None

def _sv_elo_delta(home,away):
    """Squad-value advantage as Elo-equivalent points (log10 scaled)."""
    vh=squad_value(home); va=squad_value(away)
    if not vh or not va or vh<=0 or va<=0: return 0.0
    d=math.log10(vh)-math.log10(va)
    return max(-160.0,min(160.0,SQUAD_ELO_W*d))

def sensation_note(home,away,p_h,p_d,p_a,o1=None,ox=None,o2=None):
    """Upset / draw signal that makes the forecast bolder & more honest."""
    eh=ELO.get(home,DEFAULT_ELO); ea=ELO.get(away,DEFAULT_ELO)
    model_fav_home=p_h>=p_a
    try:
        if o1 and ox and o2:
            io1,io2=1.0/float(o1),1.0/float(o2)
            if (io1>=io2)!=model_fav_home:
                return "\U0001f3b2 \u0411\u0443\u043a\u043c\u0435\u043a\u0435\u0440\u044b \u0441\u0447\u0438\u0442\u0430\u044e\u0442 \u0438\u043d\u0430\u0447\u0435 \u2014 \u0443 \u043c\u043e\u0434\u0435\u043b\u0438 \u0441\u0432\u043e\u0439 \u0444\u0430\u0432\u043e\u0440\u0438\u0442"
    except: pass
    under,over=(home,away) if eh<ea else (away,home)
    vu,vo=squad_value(under),squad_value(over)
    if vu and vo and vu>vo*1.25:
        return "\U0001f31f \u0421\u043e\u0441\u0442\u0430\u0432 "+ru_team(under)+" \u0434\u043e\u0440\u043e\u0436\u0435 \u2014 \u0432\u043e\u0437\u043c\u043e\u0436\u043d\u0430 \u0441\u0435\u043d\u0441\u0430\u0446\u0438\u044f"
    if abs(p_h-p_a)<0.10 and p_d>=0.26:
        return "\U0001f4a5 \u0421\u0438\u043b\u044b \u0440\u0430\u0432\u043d\u044b \u2014 \u043d\u0438\u0447\u044c\u044f \u0432\u0435\u0441\u044c\u043c\u0430 \u0432\u0435\u0440\u043e\u044f\u0442\u043d\u0430"
    return ""

def predict_1x2(home,away,neutral=False,_elo=None):
    # _elo: опциональный Elo-словарь (для /predict_legacy с frozen baseline Elo).
    # Без него используется глобальный ELO (живой, обновляется после каждого матча).
    src=_elo if _elo is not None else ELO
    eh=src.get(home,DEFAULT_ELO); ea=src.get(away,DEFAULT_ELO)
    diff=(eh+(0.0 if neutral else HOME_ADV_ELO))-ea
    diff+=_sv_elo_delta(home,away)            # squad value (Transfermarkt)
    p_draw=max(DRAW_MIN,min(DRAW_MAX,DRAW_MAX-0.0007*abs(diff)))
    p_hs=1.0/(1.0+10.0**(-diff/400.0))
    rest=1.0-p_draw
    return _apply_calibrator(rest*(1-p_hs), p_draw, rest*p_hs)

def predict_natural(p_h,p_d,p_a,home,away):
    eh=ELO.get(home,DEFAULT_ELO); ea=ELO.get(away,DEFAULT_ELO)
    diff=eh-ea; absd=abs(diff); fav=home if diff>=0 else away
    if   p_h>p_a and p_h>p_d and p_h>0.39:
        outcome=f"Победа {ru_team(home)}" if p_h>0.57 else f"Скорее победит {ru_team(home)}"
    elif p_a>p_h and p_a>p_d and p_a>0.39:
        outcome=f"Победа {ru_team(away)}" if p_a>0.57 else f"Скорее победит {ru_team(away)}"
    else:
        outcome="Ничья / равная борьба"
    mp=max(p_h,p_a)
    if   mp>=0.58 and absd>=180: c_e,c_l="\U0001f525","высокая"; expl=f"{ru_team(fav)} заметно сильнее (Elo +{absd:.0f})"
    elif mp>=0.48:                c_e,c_l="\u26a1","средняя"; expl=f"небольшое преимущество {ru_team(fav)}"
    else:                         c_e,c_l="\u2753","низкая"; expl="команды близки по уровню"
    return outcome,c_e,c_l,expl

def outcome_code(p_h,p_d,p_a):
    if p_h>=p_d and p_h>=p_a: return "H"
    if p_a>=p_h and p_a>=p_d: return "A"
    return "D"

def predict_scoreline(home, away, neutral=False, top=3, max_goals=6):
    """Top-N most likely exact scorelines (independent-Poisson approx).
    Goals expectation per side is derived from the same Elo+squad-value gap
    that predict_1x2 uses, scaled to ~2.55 goals/match (WC average)."""
    eh=ELO.get(home,DEFAULT_ELO); ea=ELO.get(away,DEFAULT_ELO)
    diff=(eh+(0.0 if neutral else HOME_ADV_ELO))-ea
    try: diff+=_sv_elo_delta(home,away)
    except Exception: pass
    base_total=2.55
    edge=max(-0.85,min(0.85,diff/350.0))
    lh=max(0.25,base_total*0.5*(1.0+edge*0.55))
    la=max(0.25,base_total*0.5*(1.0-edge*0.55))
    ph=[math.exp(-lh)*(lh**k)/math.factorial(k) for k in range(max_goals+1)]
    pa=[math.exp(-la)*(la**k)/math.factorial(k) for k in range(max_goals+1)]
    out=[(h,a,ph[h]*pa[a]) for h in range(max_goals+1) for a in range(max_goals+1)]
    out.sort(key=lambda x:x[2],reverse=True)
    return out[:top]

def bar(v,w=10):
    f=max(0,min(w,round(v*w)))
    return "\u2588"*f+"\u2591"*(w-f)


# ============================================================
# FORMATTING
# ============================================================

def esc(s): return html_escape(str(s),quote=False)
def fmt_date_ru(d): return f"{d.day} {RU_MONTHS[d.month]}"
def num_sp(n): return f"{n:,}".replace(",","\u202f")

def split_text(text,max_len=3800):
    if len(text)<=max_len: return [text]
    parts,cur=[],""
    for blk in text.split("\n\n"):
        if len(cur)+len(blk)+2>max_len:
            if cur: parts.append(cur)
            cur=blk
        else: cur=(cur+"\n\n"+blk).strip()
    if cur: parts.append(cur)
    return parts or [text[:max_len]]

def fmt_detail(d,home,away,host,o1=None,ox=None,o2=None,kt=None):
    """Detailed card for the BOT (includes odds + kickoff time + top-3 scores)."""
    neutral=str(host)!="1"
    p_h,p_d,p_a=predict_1x2(home,away,neutral)
    outcome,c_e,c_l,expl=predict_natural(p_h,p_d,p_a,home,away)
    grp=get_team_group(home) or "?"
    host_lbl=f"{rt(home)} дома" if not neutral else "нейтральное поле"
    if kt is None: kt=get_kickoff(d,home,away)
    kt_lbl=fmt_msk(kt)
    date_line=f"\U0001f4c5 {fmt_date_ru(d)}"
    if kt_lbl: date_line+=f" \u00b7 \U0001f552 {kt_lbl}"
    date_line+=f" \u00b7 Группа\u00a0{grp} \u00b7 {host_lbl}"
    lines=[
        f"\U0001f3df <b>{rt(home)}</b>  \u2014  <b>{rt(away)}</b>",
        date_line,"",
        f"\U0001f9e0 <b>Прогноз:</b> {esc(outcome)}",
        f"{c_e} <b>Уверенность:</b> {c_l} \u2014 <i>{esc(expl)}</i>","",
        f"\U0001f3e0 <code>{bar(p_h)}</code> {p_h*100:4.0f}%",
        f"\U0001f91d <code>{bar(p_d)}</code> {p_d*100:4.0f}%",
        f"\u2708\ufe0f <code>{bar(p_a)}</code> {p_a*100:4.0f}%",
    ]
    try:
        sc=predict_scoreline(home,away,neutral)
        if sc:
            sc_str=" \u00b7 ".join(f"<code>{h}:{a}</code> {p*100:.0f}%" for h,a,p in sc)
            lines+=["",f"\U0001f3af <b>Топ-3 счёта:</b> {sc_str}"]
    except Exception:
        pass
    sn=sensation_note(home,away,p_h,p_d,p_a,o1,ox,o2)
    if sn: lines+=["",f"<i>{esc(sn)}</i>"]
    if o1 and ox and o2:
        try: lines+=["",f"\U0001f4b0 <b>Коэффы:</b> <code>1 \u2014 {float(o1):.2f}   X \u2014 {float(ox):.2f}   2 \u2014 {float(o2):.2f}</code>"]
        except: pass
    return "\n".join(lines)

def fmt_channel(d,home,away,host,o1=None,ox=None,o2=None):
    """Public channel card — prediction + probabilities, NO odds/betting."""
    neutral=str(host)!="1"
    p_h,p_d,p_a=predict_1x2(home,away,neutral)
    outcome,c_e,c_l,expl=predict_natural(p_h,p_d,p_a,home,away)
    grp=get_team_group(home) or "?"
    host_lbl=f"{rt(home)} дома" if not neutral else "нейтральное поле"
    lines=[
        f"\U0001f3df <b>{rt(home)}</b>  \u2014  <b>{rt(away)}</b>",
        f"\U0001f4c5 {fmt_date_ru(d)} \u00b7 Группа\u00a0{grp} \u00b7 {host_lbl}","",
        f"\U0001f9e0 <b>Прогноз:</b> {esc(outcome)}",
        f"{c_e} <b>Уверенность:</b> {c_l} \u2014 <i>{esc(expl)}</i>","",
        f"\U0001f3e0 <code>{bar(p_h)}</code> {p_h*100:4.0f}%",
        f"\U0001f91d <code>{bar(p_d)}</code> {p_d*100:4.0f}%",
        f"\u2708\ufe0f <code>{bar(p_a)}</code> {p_a*100:4.0f}%",
    ]
    ms=BASELINE.get("modal_scores",{}).get(f"{home}|{away}") if BASELINE else None
    if ms: lines+=["",f"\U0001f3af <b>\u0421\u0430\u043c\u044b\u0439 \u0432\u0435\u0440\u043e\u044f\u0442\u043d\u044b\u0439 \u0441\u0447\u0451\u0442:</b> <code>{esc(ms)}</code>"]
    sn=sensation_note(home,away,p_h,p_d,p_a,o1,ox,o2)
    if sn: lines+=["",f"<i>{esc(sn)}</i>"]
    return "\n".join(lines)


# ============================================================
# WELCOME / HELP
# ============================================================

WELCOME = (
    "\u26bd\ufe0f <b>WC2026 FOOTBALL BOT</b> \U0001f3c6\n"
    "<i>ИИ-прогнозы на Чемпионат Мира 2026</i>\n"
    f"{SEP}\n\n"
    "\U0001f9e0 <b>Как это работает:</b>\n"
    "• Модель Elo + калибровка по одзам букмекеров\n"
    "• 100\u202f000 симуляций Монте-Карло всего турнира\n"
    "• Прогноз живой: обновляется после каждого игрового дня\n\n"
    f"{DASH}\n\n"
    "\U0001f4ca <b>ПРОГНОЗЫ</b>\n"
    "\U0001f3c6 /forecast — полный прогноз на весь ЧМ\n"
    "\U0001f5fa /modal — сетка плей-офф R32\u21921/8\u2192ЧФ\u2192ПФ\u2192финал\n"
    "\U0001f947 /baseline — топ-15 претендентов на трофей\n"
    "\U0001f4c2 /history — архив обновлений прогноза\n\n"
    "\u23f1 <b>МАТЧИ</b>\n"
    "\U0001f4c6 /today · /tomorrow — матчи дня\n"
    "\u23ed /next [N] — следующие N матчей (с коэффами)\n"
    "\U0001f193 /match Аргентина Испания — прогноз на конкретный матч\n\n"
    "\U0001f30d <b>СТАТИСТИКА</b>\n"
    "\U0001f3f4 /team Argentina — профиль команды\n"
    "\U0001f3d9 /group A — группа A\u2013L\n"
    "\U0001f4cb /standings — все 12 групп\n"
    "\U0001f3af /stats — точность прогнозов бота\n\n"
    "\u2139\ufe0f /about — о модели · /help — справка\n"
    f"{SEP}\n"
    "\U0001f4e2 Канал: @WC2026Neuro · \U0001f916 @wc2026_football_bot"
)

HELP = (
    "\U0001f4cb <b>ПОЛНЫЙ СПИСОК КОМАНД</b>\n"
    f"{SEP}\n\n"
    "\U0001f4ca <b>Прогнозы</b>\n"
    "/forecast — чемпион, финал, полуфиналы, группы\n"
    "/modal — сетка 1/16\u21921/8\u2192ЧФ\u2192ПФ\u2192финал (каждый матч)\n"
    "/baseline — топ-15 чемпионов + графика\n"
    "/history — архив версий прогноза по датам\n\n"
    "\u23f1 <b>Матчи</b>\n"
    "/today, /tomorrow — все матчи дня + коэффы\n"
    "/next [N] — следующие N матчей (по умолчанию 5)\n"
    "/match A B [YYYY-MM-DD] — прогноз на конкретный матч\n\n"
    "\U0001f30d <b>Статистика</b>\n"
    "/team Название — Elo, группа, вероятности по раундам\n"
    "/group X — группа A\u2013L с прогнозом\n"
    "/standings — все 12 групп кратко\n"
    "/stats — сколько прогнозов сбылось\n\n"
    "\u2139\ufe0f /about · /help"
)


# ============================================================
# HANDLERS
# ============================================================

def is_admin(uid): a=os.environ.get("ADMIN_USER_ID",""); return not a or str(uid)==a

async def cmd_start(u,c): await u.message.reply_text(WELCOME,parse_mode=ParseMode.HTML,disable_web_page_preview=True)
async def cmd_help(u,c):  await u.message.reply_text(HELP,   parse_mode=ParseMode.HTML)

async def cmd_about(u,c):
    played = BASELINE.get("matches_played",0)
    total  = BASELINE.get("matches_total", 72)
    gen    = BASELINE.get("resimulated_at") or (BASELINE.get("generated_at","") or "")[:10]
    sims   = BASELINE.get("sims",SIMS_DISPLAY)
    form   = "\u2705 включён" if BASELINE.get("form_bonus_applied") else "\u2014 базовый прогноз"
    text=(
        "\u2139\ufe0f <b>О БОТЕ И МОДЕЛИ</b>\n"
        f"{SEP}\n\n"
        "\U0001f9e0 <b>Модель:</b> Elo-рейтинг + калибровка по одзам\n"
        f"\U0001f3b2 <b>Симуляций:</b> {num_sp(sims)} розыгрышей турнира\n"
        f"\U0001f504 <b>Сыграно:</b> {played}/{total} матчей\n"
        f"\U0001f4c5 <b>Обновлено:</b> {gen or '—'}\n"
        f"\U0001f4c8 <b>Форм-бонус:</b> {form}\n"
        "\U0001f4b0 <b>Одзы:</b> The Odds API + football-data.org\n\n"
        f"{DASH}\n"
        "<i>Прогнозы носят информационный характер. Ставки — ваш риск.</i>"
    )
    await u.message.reply_text(text,parse_mode=ParseMode.HTML)

async def cmd_reload(u,c):
    if not is_admin(u.effective_user.id):
        await u.message.reply_text("\u274c Нет доступа."); return
    await u.message.reply_text("\u23f3 Перечитываю данные\u2026")
    load_all()
    played=BASELINE.get("matches_played",0); total=BASELINE.get("matches_total",72)
    await u.message.reply_text(
        f"\u2705 Готово. Elo: {len(ELO)} команд · Сыграно: {played}/{total}",
        parse_mode=ParseMode.HTML)

async def cmd_baseline(u,c):
    probs=BASELINE.get("tournament_probs",{})
    if not probs:
        await u.message.reply_text("\u26a0\ufe0f Baseline не загружен. /reload"); return
    top=sorted(probs.items(),key=lambda x:x[1]["P_W"],reverse=True)[:15]
    gen=(BASELINE.get("resimulated_at") or (BASELINE.get("generated_at","") or "")[:10] or "\u2014")
    sims=BASELINE.get("sims",SIMS_DISPLAY)
    played=BASELINE.get("matches_played",0); total=BASELINE.get("matches_total",72)
    label="Базовый прогноз" if not played else f"Обновлён ({played}/{total})"
    medals={1:"\U0001f947",2:"\U0001f948",3:"\U0001f949"}
    max_pw=top[0][1]["P_W"] if top else 1
    lines=[
        "\U0001f3c6 <b>ТОП-15 ПРЕТЕНДЕНТОВ НА ТРОФЕЙ</b>",
        f"<i>По итогам {num_sp(sims)} виртуальных розыгрышей ЧМ-2026\n"
        f"{label} от {gen}</i>",
        "",
    ]
    for i,(t,tp) in enumerate(top,1):
        rank=medals.get(i, f"<code>{i:>2}</code>")
        pw=tp["P_W"]*100
        b=bar(tp["P_W"]/max_pw)
        lines.append(f"{rank} <b>{rt(t)}</b>  <code>{b} {pw:5.2f}%</code>")
    champ=top[0][0]; champ_pw=top[0][1]["P_W"]
    champ_n=int(round(champ_pw*sims))
    lines+=[
        "",
        f"\U0001f4cc <b>Самый частый чемпион: {rt(champ)}</b>",
        f"<i>{champ_pw*100:.1f}% = в {num_sp(champ_n)} симуляциях из {num_sp(sims)} "
        f"команда выиграла кубок.</i>",
        "\U0001f4d6 Подробнее о модели: /about · сетка: /modal",
    ]
    for p in split_text("\n".join(lines)):
        await u.message.reply_text(p,parse_mode=ParseMode.HTML)

async def cmd_forecast(u,c):
    probs=BASELINE.get("tournament_probs",{})
    modal=BASELINE.get("modal_forecast",{})
    if not probs:
        await u.message.reply_text("\u26a0\ufe0f Baseline не загружен. /reload"); return
    g2=modal.get("group_top2",{})
    gp=BASELINE.get("group_positions",{}) or {}
    played=BASELINE.get("matches_played",0); total=BASELINE.get("matches_total",72)
    parts=[]
    parts.append(
        "\U0001f52e <b>ПОЛНЫЙ ПРОГНОЗ ЧМ-2026</b>\n"
        f"<i>{num_sp(SIMS_DISPLAY)} симуляций · сыграно {played}/{total} матчей</i>\n"
        f"{SEP}\n"
        "<i>По порядку: 1) групповой этап \u2192 2) кто выходит \u2192 3) сетка плей-офф.</i>"
    )
    st1=["1\ufe0f\u20e3 <b>ГРУППОВОЙ ЭТАП</b>",
         "<i>Ранжир по xОч (ожидаемые очки) · Вых% — выйти из группы · 1м% — выиграть группу · \U0001f3c6 \u2014 шанс стать чемпионом</i>",""]
    for letter in "ABCDEFGHIJKL":
        teams=get_group_teams(letter)
        if not teams: continue
        xp=group_xpoints(letter)
        teams.sort(key=lambda t:(xp.get(t,0),probs.get(t,{}).get("P_R32",0)),reverse=True)
        st1.append(f"<b>Группа {letter}</b>")
        for i,t in enumerate(teams,1):
            tp=probs.get(t,{})
            win=(gp.get(t,{}) or {}).get("1",0) or 0
            mark="\U0001f947" if i==1 else ("\U0001f948" if i==2 else ("\U0001f949" if i==3 else "\u25aa\ufe0f"))
            st1.append(
                f"{mark} <b>{rt(t)}</b> \u2014 Вых {tp.get('P_R32',0)*100:.0f}% · "
                f"1м {win*100:.0f}% · "
                f"xОч {xp.get(t,0):.1f} · \U0001f3c6 {tp.get('P_W',0)*100:.1f}%"
            )
        st1.append("")
    parts.append("\n".join(st1).rstrip())
    st2=["2\ufe0f\u20e3 <b>КТО ВЫХОДИТ В ПЛЕЙ-ОФФ (32 команды)</b>",
         "<i>Из каждой группы проходят 2 лучших + 8 лучших команд с 3-го места.</i>","",
         "\U0001f947\U0001f948 <b>1-е и 2-е места по группам:</b>"]
    for letter in "ABCDEFGHIJKL":
        pair=g2.get(letter,[])
        p1=pair[0] if pair else "?"; p2=pair[1] if len(pair)>1 else "?"
        st2.append(f"<code>{letter}</code> {rt(p1)} · {rt(p2)}")
    thirds=third_place_qualifiers()
    if thirds:
        st2+=["","\U0001f949 <b>8 лучших с 3-го места (тоже в плей-офф):</b>"]
        for t,letter,p in thirds:
            st2.append(f"  {rt(t)} <code>[{letter}]</code> \u2014 {p*100:.0f}% на выход")
    parts.append("\n".join(st2))
    st3="3\ufe0f\u20e3 <b>ПЛЕЙ-ОФФ \u2014 ПОЛНАЯ СЕТКА</b>\n<i>самый частый исход каждого матча</i>\n\n"+f"\n{DASH}\n\n".join(_bracket_blocks())
    parts.append(st3)
    text=f"\n{DASH}\n\n".join(parts)
    for p in split_text(text):
        await u.message.reply_text(p,parse_mode=ParseMode.HTML)

async def cmd_modal(u,c):
    header=("\U0001f5fa <b>СЕТКА ПЛЕЙ-ОФФ</b>\n"
            f"<i>{num_sp(SIMS_DISPLAY)} симуляций · самый частый исход каждого матча</i>\n"
            f"{SEP}")
    text=header+"\n\n"+f"\n{DASH}\n\n".join(_bracket_blocks())+"\n\n<i>Полный прогноз (группы \u2192 выход \u2192 плей-офф): /forecast · вероятности: /baseline</i>"
    for p in split_text(text):
        await u.message.reply_text(p,parse_mode=ParseMode.HTML)

async def _send_fixtures(u,title,rows):
    lines=[f"\u26bd <b>{title}</b>",f"{SEP}",""]
    for r in rows: lines+=[fmt_detail(*r),f"{DASH}",""]
    for p in split_text("\n".join(lines)):
        await u.message.reply_text(p,parse_mode=ParseMode.HTML)

async def cmd_today(u,c):
    today=date.today()
    rows=get_fixtures(today,today,limit=MAX_NEXT)
    if not rows:
        await u.message.reply_text(
            f"\u26bd Сегодня ({fmt_date_ru(today)}) матчей нет.\n\u2192 /next — ближайшие",
            parse_mode=ParseMode.HTML); return
    await _send_fixtures(u,f"МАТЧИ {fmt_date_ru(today).upper()}",rows)

async def cmd_tomorrow(u,c):
    tomorrow=date.today()+timedelta(days=1)
    rows=get_fixtures(tomorrow,tomorrow,limit=MAX_NEXT)
    if not rows:
        await u.message.reply_text(
            f"\u26bd Завтра ({fmt_date_ru(tomorrow)}) матчей нет.\n\u2192 /next — ближайшие",
            parse_mode=ParseMode.HTML); return
    await _send_fixtures(u,f"МАТЧИ {fmt_date_ru(tomorrow).upper()}",rows)

async def cmd_next(u,c):
    n=5
    if c.args:
        try: n=min(int(c.args[0]),MAX_NEXT)
        except: pass
    rows=get_fixtures(date.today(),limit=n)
    if not rows:
        await u.message.reply_text("\u26bd Матчей не найдено."); return
    await _send_fixtures(u,f"СЛЕДУЮЩИЕ МАТЧИ ({len(rows)})",rows)

def _resolve_team(name):
    """Best-effort team-name resolver (en/ru/aliases/fuzzy)."""
    if not name: return None
    name=name.strip()
    all_t=list(ELO.keys())
    if name in all_t: return name
    low={t.lower():t for t in all_t}
    if name.lower() in low: return low[name.lower()]
    rv={ru_team(t).lower():t for t in all_t}
    if name.lower() in rv: return rv[name.lower()]
    nm={_norm_team(t):t for t in all_t}
    if _norm_team(name) in nm: return nm[_norm_team(name)]
    cand=difflib.get_close_matches(name,all_t,n=1,cutoff=0.6)
    if cand: return cand[0]
    candr=difflib.get_close_matches(name.lower(),list(rv),n=1,cutoff=0.6)
    if candr: return rv[candr[0]]
    return next((t for t in all_t if name.lower() in t.lower()), None)

async def cmd_match(u,c):
    """/match TeamA TeamB [YYYY-MM-DD] — forecast for a specific match."""
    import re as _re
    if not c.args or len(c.args)<2:
        await u.message.reply_text(
            "\U0001f3df <b>Прогноз на конкретный матч</b>\n\n"
            "<b>Использование:</b>\n"
            "<code>/match Аргентина Испания</code>\n"
            "<code>/match Argentina Spain 2026-07-19</code>\n"
            "<code>/match Saudi Arabia South Korea 2026-06-15</code>\n\n"
            "Порядок команд неважен. Дата необязательна.\n"
            "Если матч есть в расписании — покажу реальную карточку с временем МСК.\n"
            "Если нет — гипотетический прогноз на нейтральном поле.",
            parse_mode=ParseMode.HTML); return
    args=list(c.args)
    target_date=None
    if _re.fullmatch(r"\d{4}-\d{2}-\d{2}", args[-1]):
        try: target_date=date.fromisoformat(args.pop())
        except Exception: pass
    if len(args)<2:
        await u.message.reply_text("❌ Укажи две команды.", parse_mode=ParseMode.HTML); return
    team_a=team_b=None
    for k in range(1,len(args)):
        a=" ".join(args[:k]); b=" ".join(args[k:])
        ta=_resolve_team(a); tb=_resolve_team(b)
        if ta and tb and ta!=tb:
            team_a, team_b = ta, tb; break
    if not team_a or not team_b:
        await u.message.reply_text(
            f"❌ Не смог распознать команды из <i>{esc(' '.join(args))}</i>. Попробуй полные названия.",
            parse_mode=ParseMode.HTML); return
    fixture=None
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                if target_date:
                    cur.execute(
                        "SELECT match_date,home,away,host,odds_1,odds_x,odds_2 "
                        "FROM wc2026_fixtures "
                        "WHERE match_date=%s AND ((home=%s AND away=%s) OR (home=%s AND away=%s)) LIMIT 1",
                        (target_date, team_a, team_b, team_b, team_a))
                else:
                    cur.execute(
                        "SELECT match_date,home,away,host,odds_1,odds_x,odds_2 "
                        "FROM wc2026_fixtures "
                        "WHERE (home=%s AND away=%s) OR (home=%s AND away=%s) "
                        "ORDER BY match_date LIMIT 1",
                        (team_a, team_b, team_b, team_a))
                fixture=cur.fetchone()
    except Exception as e:
        log.warning("cmd_match fixture lookup: %s", e)
    if fixture:
        card=fmt_detail(*fixture)
        await u.message.reply_text(
            f"\u26bd <b>МАТЧ ИЗ РАСПИСАНИЯ</b>\n{SEP}\n\n{card}",
            parse_mode=ParseMode.HTML); return
    d=target_date or date.today()
    card=fmt_detail(d, team_a, team_b, host="0")
    head="\U0001f52e <b>ГИПОТЕТИЧЕСКИЙ МАТЧ</b>\n<i>В расписании не нашёл — считаю на нейтральном поле</i>"
    await u.message.reply_text(f"{head}\n{SEP}\n\n{card}", parse_mode=ParseMode.HTML)

async def cmd_team(u,c):
    if not c.args:
        await u.message.reply_text("Использование: /team Аргентина  (или /team Argentina)"); return
    name=" ".join(c.args)
    all_t=list(ELO.keys())
    team=None
    if name in all_t: team=name
    if not team:
        low={t.lower():t for t in all_t}
        if name.strip().lower() in low: team=low[name.strip().lower()]
    if not team:
        rv={ru_team(t).lower():t for t in all_t}
        if name.strip().lower() in rv: team=rv[name.strip().lower()]
        else:
            cand=difflib.get_close_matches(name,all_t,n=1,cutoff=0.6)
            if cand: team=cand[0]
            else:
                candr=difflib.get_close_matches(name.lower(),list(rv),n=1,cutoff=0.6)
                if candr: team=rv[candr[0]]
                else:
                    team=next((t for t in all_t if name.lower() in t.lower()),None)
    if not team:
        await u.message.reply_text(f"\u274c Команда \u00ab{esc(name)}\u00bb не найдена.",parse_mode=ParseMode.HTML); return
    elo=ELO.get(team,DEFAULT_ELO)
    grp=get_team_group(team) or "?"
    tp=BASELINE.get("tournament_probs",{}).get(team,{})
    lines=[f"\U0001f3f4 <b>{rt(team)}</b>",
           f"\U0001f3d9 Группа\u00a0{grp} \u00b7 Elo: <code>{elo:.0f}</code>",
           "<i>Elo — сила команды; проценты — шанс пройти каждый раунд (прогноз модели).</i>",
           f"{SEP}",""]
    if tp:
        rounds=[("Плей-офф 1/16","P_R32"),("1/8 финала","P_R16"),
                ("1/4 финала","P_QF"),("Полуфинал 1/2","P_SF"),
                ("Финал","P_F"),("\U0001f3c6 Чемпион","P_W")]
        lines.append("\U0001f4ca <b>Вероятности по раундам:</b>")
        for lbl,key in rounds:
            v=tp.get(key,0)
            lines.append(f"{lbl:<16} <code>{bar(v)}</code> {v*100:5.1f}%")
    rows=get_fixtures(date.today(),limit=60)
    near=[r for r in rows if r[1]==team or r[2]==team][:3]
    if near:
        lines+=["","\U0001f4c5 <b>Ближайшие матчи:</b>"]
        for r in near:
            opp=r[2] if r[1]==team else r[1]
            lines.append(f"  {fmt_date_ru(r[0])}: vs {rt(opp)}")
    await u.message.reply_text("\n".join(lines),parse_mode=ParseMode.HTML)

async def cmd_group(u,c):
    if not c.args:
        await u.message.reply_text("Использование: /group A"); return
    letter=c.args[0].upper()
    if letter not in "ABCDEFGHIJKL" or len(letter)!=1:
        await u.message.reply_text("Группы: A\u2013L. Например: /group F"); return
    teams=get_group_teams(letter)
    if not teams:
        await u.message.reply_text("\u274c Группа не найдена."); return
    probs=BASELINE.get("tournament_probs",{})
    teams.sort(key=lambda t:(BASELINE.get("mean_points",{}).get(t,0.0),probs.get(t,{}).get("P_R32",0)),reverse=True)
    lines=[f"\U0001f3d9 <b>ГРУППА\u00a0{letter}</b>",
           "<i>«Выход» — шанс выйти из группы (топ-2 + лучшие 3-и места); \U0001f3c6 — шанс стать чемпионом.</i>",
           f"{SEP}",""]
    for i,t in enumerate(teams,1):
        tp=probs.get(t,{}); elo=ELO.get(t,DEFAULT_ELO)
        pr32=tp.get("P_R32",0); pw=tp.get("P_W",0)
        lines.append(
            f"<b>{i}. {rt(t)}</b>  Elo <code>{elo:.0f}</code>\n"
            f"   Выход <code>{bar(pr32)}</code> {pr32*100:.0f}%  \u00b7  \U0001f3c6 {pw*100:.1f}%"
        )
    modal=BASELINE.get("modal_forecast",{}).get("group_top2",{}).get(letter)
    if modal:
        lines+=["",f"\U0001f4ca <b>Прогноз:</b> выйдут \U0001f947 <b>{rt(modal[0])}</b> и \U0001f948 {rt(modal[1])}"]
    await u.message.reply_text("\n".join(lines),parse_mode=ParseMode.HTML)

async def cmd_standings(u,c):
    modal=BASELINE.get("modal_forecast",{}).get("group_top2",{})
    probs=BASELINE.get("tournament_probs",{})
    lines=["\U0001f3d9 <b>ВСЕ 12 ГРУПП — ПРОГНОЗ</b>",
           "<i>\U0001f947\U0001f948 — кто выйдет в плей-офф по версии нейросети</i>",f"{SEP}",""]
    for letter in "ABCDEFGHIJKL":
        pair=modal.get(letter,[])
        p1=pair[0] if pair else "?"; p2=pair[1] if len(pair)>1 else "?"
        lines.append(f"<b>Группа {letter}:</b> \U0001f947 {rt(p1)} \u00b7 \U0001f948 {rt(p2)}")
    thirds=third_place_qualifiers()
    if thirds:
        lines+=["",SEP,"\U0001f949 <b>Лучшие 3-и места — 8 команд в плей-офф:</b>",
                "<i>в формате на 48 команд проходят 8 лучших сборных с 3-го места</i>",""]
        for t,letter,p in thirds:
            lines.append(f"  {rt(t)} <code>[{letter}]</code> \u2014 Шанс выхода {p*100:.0f}%")
    lines+=["","<i>Подробный расклад: /group F</i>"]
    await u.message.reply_text("\n".join(lines),parse_mode=ParseMode.HTML)

async def cmd_stats(u,c):
    _s=get_accuracy_stats()
    correct,resolved,totalp=_s["correct"],_s["resolved"],_s["total"]
    if not resolved:
        await u.message.reply_text(
            "\U0001f3af <b>ТОЧНОСТЬ ПРОГНОЗОВ</b>\n"
            f"{SEP}\n\n"
            f"\U0001f4dd Записано прогнозов: <b>{totalp}</b>\n"
            "\u23f3 Сыгранных матчей пока нет — статистика появится после первых игр (11 июня).",
            parse_mode=ParseMode.HTML); return
    pct=correct/resolved*100
    await u.message.reply_text(
        "\U0001f3af <b>ТОЧНОСТЬ ПРОГНОЗОВ НЕЙРОСЕТИ</b>\n"
        f"{SEP}\n\n"
        f"\u2705 Угадано: <b>{correct}/{resolved}</b>\n"
        f"\U0001f4ca Точность: <b>{pct:.1f}%</b>  <code>{bar(correct/resolved)}</code>\n"
        f"\U0001f4dd Всего прогнозов в базе: {totalp}\n\n"
        "<i>Каждый прогноз сохраняется и сверяется с реальным результатом.</i>",
        parse_mode=ParseMode.HTML)
    _s2=get_accuracy_stats()
    if _s2["resolved"]:
        def _ln(lbl,pair):
            cc,rr=pair
            if not rr: return lbl+": <i>\u043d\u0435\u0442 \u0441\u044b\u0433\u0440\u0430\u043d\u043d\u044b\u0445</i>"
            return f"{lbl}: <b>{cc}/{rr}</b> ({cc/rr*100:.0f}%)  <code>{bar(cc/rr)}</code>"
        pm=_s2["by_mode"].get("prematch",(0,0)); lv=_s2["by_mode"].get("live",(0,0))
        grp=_s2["by_stage"].get("group",(0,0)); ko=_s2["by_stage"].get("knockout",(0,0))
        out=["\U0001f3af <b>\u0414\u0412\u0410 \u0422\u0420\u0415\u041a\u0410 \u041f\u0420\u041e\u0413\u041d\u041e\u0417\u041e\u0412</b>", SEP, "",
            "<b>\u0414\u0432\u0430 \u0442\u0440\u0435\u043a\u0430 \u043f\u0440\u043e\u0433\u043d\u043e\u0437\u043e\u0432:</b>",
            _ln("\U0001f52e \u0417\u0430\u0440\u0430\u043d\u0435\u0435 (\u0431\u0435\u0437 \u0444\u043e\u0440\u043c\u044b)", pm),
            _ln("\u26a1 \u041f\u043e \u0445\u043e\u0434\u0443 (\u0441 \u0444\u043e\u0440\u043c\u043e\u0439)", lv), "",
            "<b>\u041f\u043e \u044d\u0442\u0430\u043f\u0430\u043c:</b>",
            _ln("\U0001f3df \u0413\u0440\u0443\u043f\u043f\u044b", grp),
            _ln("\U0001f3c6 \u041f\u043b\u0435\u0439-\u043e\u0444\u0444", ko), "",
            "<i>\u00ab\u0417\u0430\u0440\u0430\u043d\u0435\u0435\u00bb \u2014 \u043f\u0440\u043e\u0433\u043d\u043e\u0437\u044b \u0434\u043e \u0441\u0442\u0430\u0440\u0442\u0430. \u00ab\u041f\u043e \u0445\u043e\u0434\u0443\u00bb \u2014 \u043f\u0435\u0440\u0435\u0441\u0447\u0451\u0442 \u0441 \u0444\u043e\u0440\u043c\u043e\u0439.</i>"]
        await u.message.reply_text("\n".join(out), parse_mode=ParseMode.HTML)
    # ---- exact-score ("точный счёт") track ----
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FILTER (WHERE exact_correct IS TRUE),"
                    "       COUNT(*) FILTER (WHERE pred_score_h IS NOT NULL AND actual_home IS NOT NULL) "
                    "FROM wc2026_predictions")
                ec, er = cur.fetchone()
                ec, er = ec or 0, er or 0
        if er:
            await u.message.reply_text(
                "\U0001f3af <b>ТОЧНЫЙ СЧЁТ</b>\n"
                f"{SEP}\n\n"
                f"Угадан точный счёт: <b>{ec}/{er}</b> ({ec/er*100:.1f}%)  <code>{bar(ec/er)}</code>\n\n"
                "<i>\u042dто сложно: \u0443 \u043b\u044e\u0434\u0435\u0439 \u043e\u0431\u044b\u0447\u043d\u043e 6\u201310%. \u0421\u0447\u0438\u0442\u0430\u0435\u0442\u0441\u044f \u0441\u0442\u0440\u043e\u0433\u043e: \u0440\u043e\u0432\u043d\u043e \u0442\u043e\u0442 \u0436\u0435 \u0441\u0447\u0451\u0442, \u0447\u0442\u043e \u043f\u0440\u0435\u0434\u0441\u043a\u0430\u0437\u0430\u043d (\u0438\u0437 \u0422\u043e\u043f-3 \u0431\u0435\u0440\u0451\u043c \u22161).</i>",
                parse_mode=ParseMode.HTML)
    except Exception as _ex:
        log.warning("exact-score block: %s", _ex)

async def cmd_history(u,c):
    versions=get_baseline_versions()
    if not versions:
        await u.message.reply_text(
            "\U0001f4c2 <b>ИСТОРИЯ ОБНОВЛЕНИЙ</b>\n"
            f"{SEP}\n\n"
            "Прогноз ещё не обновлялся — турнир не начался.\n"
            "Первые версии появятся после 11 июня.",
            parse_mode=ParseMode.HTML); return
    lines=["\U0001f4c2 <b>ИСТОРИЯ ОБНОВЛЕНИЙ ПРОГНОЗА</b>",f"{SEP}",""]
    for key,gen_at,played,total in versions:
        date_str=key.replace("baseline_","")
        lines.append(f"\U0001f5d3 <b>{date_str}</b> \u2014 сыграно {played or 0}/{total or 72}")
    lines+=["","<i>Обновляется автоматически после игровых дней.</i>",
            "<i>Подробное сравнение: /snapshots и /diff &lt;ярлык&gt;</i>"]
    await u.message.reply_text("\n".join(lines),parse_mode=ParseMode.HTML)


async def cmd_snapshots(u,c):
    """Список ВСЕХ снимков прогноза с прогрессом и модальным чемпионом."""
    rows=[]
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT key, content->>'generated_at', content->>'matches_played', "
                    "       content->>'matches_total', "
                    "       content->'modal_forecast'->>'modal_champion' "
                    "FROM wc2026_artifacts WHERE key LIKE 'baseline_%' "
                    "ORDER BY key")
                rows=cur.fetchall()
    except Exception as e:
        log.warning("cmd_snapshots: %s",e)
    if not rows:
        await u.message.reply_text(
            "\U0001f5c2 <b>СНИМКИ ПРОГНОЗА</b>\n"
            f"{SEP}\n\n"
            "Пока ни одного снимка. Они создаются:\n"
            "\u2022 при загрузке через wc2026_upload_baseline.py (с --label)\n"
            "\u2022 автоматически после каждого игрового дня (00:30 Нск)\n"
            "\u2022 при /update вручную",
            parse_mode=ParseMode.HTML); return
    lines=["\U0001f5c2 <b>ВСЕ СНИМКИ ПРОГНОЗА</b>",f"{SEP}",
           f"<i>Всего: {len(rows)} \u2014 каждый снимок сохраняет JSON целиком "
           "(группы, матчи, шансы каждой команды).</i>",""]
    for key,gen,played,total,champ in rows:
        label=key.replace("baseline_","")
        played=int(played) if played else 0
        total=int(total) if total else 72
        ch=ru_team(champ) if champ else "?"
        lines.append(f"\U0001f5d3 <code>{esc(label)}</code> \u2014 {played}/{total} \u00b7 \U0001f3c6 {esc(ch)}")
    lines+=["",
            "<b>Сравнить два снимка:</b>",
            "<code>/diff &lt;ярлык&gt;</code> \u2014 разница с текущим",
            "<code>/diff &lt;ярлык1&gt; &lt;ярлык2&gt;</code> \u2014 любые два",
            "",
            "<i>Пример: /diff prematch_FROZEN</i>"]
    await u.message.reply_text("\n".join(lines),parse_mode=ParseMode.HTML)


async def cmd_diff(u,c):
    """/diff <ярлык> [<ярлык2>] — сравнить два снимка (или один с live)."""
    args=(c.args if c and getattr(c,"args",None) else [])
    if not args:
        await u.message.reply_text(
            "\u2139\ufe0f <b>Использование</b>\n"
            f"{SEP}\n\n"
            "<code>/diff &lt;ярлык&gt;</code> \u2014 сравнить с текущим прогнозом\n"
            "<code>/diff &lt;ярлык1&gt; &lt;ярлык2&gt;</code> \u2014 сравнить два снимка\n\n"
            "Список ярлыков: /snapshots",
            parse_mode=ParseMode.HTML); return
    def _resolve_key(lbl):
        return lbl if lbl.startswith("baseline_") or lbl=="baseline" else f"baseline_{lbl}"
    key_a=_resolve_key(args[0])
    key_b=_resolve_key(args[1]) if len(args)>=2 else "baseline"
    a=get_snapshot(key_a); b=get_snapshot(key_b)
    if not a:
        await u.message.reply_text(
            f"\u274c Снимок не найден: <code>{esc(key_a)}</code>\n/snapshots \u2014 список доступных.",
            parse_mode=ParseMode.HTML); return
    if not b:
        await u.message.reply_text(
            f"\u274c Снимок не найден: <code>{esc(key_b)}</code>",
            parse_mode=ParseMode.HTML); return
    pa=a.get("tournament_probs",{}) or {}
    pb=b.get("tournament_probs",{}) or {}
    teams=sorted(set(pa)|set(pb))
    deltas=[]
    for t in teams:
        wa=(pa.get(t) or {}).get("P_W",0.0) or 0.0
        wb=(pb.get(t) or {}).get("P_W",0.0) or 0.0
        if wa or wb:
            deltas.append((t,float(wa)*100,float(wb)*100,(float(wb)-float(wa))*100))
    deltas.sort(key=lambda r:-abs(r[3]))
    top=deltas[:15]
    lbl_a=key_a.replace("baseline_","") if key_a!="baseline" else "live"
    lbl_b=key_b.replace("baseline_","") if key_b!="baseline" else "live"
    pa_played=a.get('matches_played',0) or 0
    pb_played=b.get('matches_played',0) or 0
    pb_total=b.get('matches_total',72) or 72
    lines=[f"\U0001f504 <b>СРАВНЕНИЕ ПРОГНОЗОВ</b>",f"{SEP}",
           f"\U0001f4cc <code>{esc(lbl_a)}</code> \u2192 <code>{esc(lbl_b)}</code>",
           f"<i>Сыграно: {pa_played} \u2192 {pb_played} / {pb_total}</i>",
           "","<b>Топ-15 изменений P(чемпион):</b>"]
    shown=0
    for t,wa,wb,d in top:
        if abs(d)<0.005: continue
        arrow="\U0001f4c8" if d>0 else ("\U0001f4c9" if d<0 else "\u27a1\ufe0f")
        sign="+" if d>=0 else ""
        lines.append(f"{arrow} <b>{esc(ru_team(t))}</b>: {wa:.2f}% \u2192 {wb:.2f}% (<b>{sign}{d:.2f}пп</b>)")
        shown+=1
    if shown==0:
        lines.append("<i>заметных изменений нет (все дельты &lt; 0.01пп)</i>")
    champ_a=(a.get("modal_forecast") or {}).get("modal_champion")
    champ_b=(b.get("modal_forecast") or {}).get("modal_champion")
    if champ_a or champ_b:
        if champ_a==champ_b:
            lines+=["",f"\U0001f3c6 Модальный чемпион не изменился: <b>{esc(ru_team(champ_a) if champ_a else '?')}</b>"]
        else:
            lines+=["",f"\U0001f3c6 Чемпион (модель): <b>{esc(ru_team(champ_a) if champ_a else '?')}</b> \u2192 <b>{esc(ru_team(champ_b) if champ_b else '?')}</b>"]
    gpa=a.get("group_positions",{}) or {}
    gpb=b.get("group_positions",{}) or {}
    if gpa and gpb:
        gshifts=[]
        for t in sorted(set(gpa)|set(gpb)):
            ta=(gpa.get(t) or {}).get("1",0) or 0
            tb=(gpb.get(t) or {}).get("1",0) or 0
            if abs(float(tb)-float(ta))>=0.05:
                gshifts.append((t,float(ta)*100,float(tb)*100,(float(tb)-float(ta))*100))
        gshifts.sort(key=lambda r:-abs(r[3]))
        if gshifts:
            lines+=["","<b>Заметные сдвиги P(1-е место в группе):</b>"]
            for t,ta,tb,d in gshifts[:8]:
                arrow="\U0001f4c8" if d>0 else "\U0001f4c9"
                sign="+" if d>=0 else ""
                lines.append(f"{arrow} <b>{esc(ru_team(t))}</b>: {ta:.0f}% \u2192 {tb:.0f}% (<b>{sign}{d:.0f}пп</b>)")
    await u.message.reply_text("\n".join(lines),parse_mode=ParseMode.HTML)


# ============================================================
# /update — admin: ingest results + notify (baseline frozen, no resim)
# ============================================================

async def cmd_update(u,c):
    if not is_admin(u.effective_user.id):
        await u.message.reply_text("\u274c Нет доступа."); return
    if _UPDATING:
        await u.message.reply_text("\u23f3 Обновление уже выполняется — подождите немного."); return
    ch=os.environ.get("CHANNEL_ID","")
    api_key=os.environ.get("FOOTBALL_DATA_API_KEY","")
    if not api_key:
        await u.message.reply_text("\u274c FOOTBALL_DATA_API_KEY не задан."); return
    await u.message.reply_text("\U0001f504 <b>Обновление…</b>\n\u23f3 Шаг 1/2: подгружаю результаты",parse_mode=ParseMode.HTML)
    env=os.environ.copy()
    try:
        r1=subprocess.run([sys.executable,"-X","utf8","wc2026_ingest_results.py"],
                          capture_output=True,text=True,timeout=180,env=env)
        log.info("ingest: %s",r1.stdout[-500:])
        if r1.returncode!=0:
            await u.message.reply_text(f"\u274c Ошибка загрузки:\n<code>{esc(r1.stderr[-300:])}</code>",parse_mode=ParseMode.HTML); return
    except Exception as e:
        await u.message.reply_text(f"\u274c Исключение: {esc(str(e))}",parse_mode=ParseMode.HTML); return
    await u.message.reply_text("\u2705 Результаты загружены.\n\u23f3 Обновляю таблицу и прогнозы\u2026 (базовый прогноз заморожен)",parse_mode=ParseMode.HTML)
    load_all()
    # snapshot the current baseline state for permanent history
    snapshot_baseline(make_snapshot_label("ingest"))
    # resolve yesterday's predictions too
    resolve_predictions(date.today()-timedelta(days=1))
    resolve_predictions(date.today())
    played=BASELINE.get("matches_played",0); total=BASELINE.get("matches_total",72)
    notif=get_pending_notification()
    if notif and ch:
        for p in split_text(notif):
            await c.bot.send_message(chat_id=ch,text=p,parse_mode=ParseMode.HTML)
        clear_pending_notification()
        await u.message.reply_text("\u2705 Обновлено + уведомление в канале.",parse_mode=ParseMode.HTML)
    elif notif:
        await u.message.reply_text(f"\u2705 Обновлено (канал не настроен).\n\n{notif}",parse_mode=ParseMode.HTML)
        clear_pending_notification()
    else:
        await u.message.reply_text(
            f"\u2705 Обновлено! Сыграно {played}/{total}.\nЗначимых изменений нет (delta < 2%).",
            parse_mode=ParseMode.HTML)


# ============================================================
# CHANNEL POSTING  (no betting/odds content)
# ============================================================

async def _post_today_to_channel(bot,ch):
    today=date.today()
    rows=get_fixtures(today,today,limit=20)
    if not rows: return False
    record_predictions(rows)   # сохраняем прогнозы один раз
    lines=[
        f"\u26bd <b>МАТЧИ {fmt_date_ru(today).upper()}</b>",
        "<i>Прогнозы нейросети на игровой день</i>",f"{SEP}",""
    ]
    for r in rows: lines+=[fmt_channel(*r),f"{DASH}",""]
    lines.append("\U0001f916 Все прогнозы и вероятности — @wc2026_football_bot")
    for p in split_text("\n".join(lines)):
        await bot.send_message(chat_id=ch,text=p,parse_mode=ParseMode.HTML)
    return True

async def _post_forecast_to_channel(bot,ch):
    probs=BASELINE.get("tournament_probs",{}); modal=BASELINE.get("modal_forecast",{})
    if not probs: return False
    g2=modal.get("group_top2",{})
    top5=sorted(probs.items(),key=lambda x:x[1]["P_W"],reverse=True)[:5]
    medals=["🥇","🥈","🥉","4️⃣","5️⃣"]
    bracket = BASELINE.get("modal_bracket") or {}
    champ = bracket.get("champion") or modal.get("modal_champion", "?")
    f_h, f_a, f_w, f_l = "?", "?", "?", "?"
    # The FINAL is the round whose code is "F" (exactly one match). The old code
    # mistook the semifinal ("Final (2)" in legacy output) for the final.
    fin_rounds = [rd for rd in bracket.get("rounds", []) if rd.get("code") == "F"]
    if not fin_rounds:
        fin_rounds = _parse_modal_knockout_strings(
            BASELINE.get("modal_knockout") or modal.get("modal_knockout") or [])
        fin_rounds = [rd for rd in fin_rounds if rd.get("code") == "F"]
    if fin_rounds and fin_rounds[0].get("matches"):
        fm = fin_rounds[0]["matches"][0]
        f_h, f_a, f_w = fm.get("home", "?"), fm.get("away", "?"), fm.get("winner", "?")
        f_l = f_a if f_w == f_h else f_h
    text=(
        "🌍 <b>ПРОГНОЗ НА ЧМ-2026</b>\n"
        f"<i>Нейросеть сыграла турнир {num_sp(SIMS_DISPLAY)} раз</i>\n"
        f"{SEP}\n\n"
        "🏆 <b>ГЛАВНЫЕ ФАВОРИТЫ:</b>\n"
        + "".join(f"  {medals[i]} <b>{rt(t)}</b> — {tp['P_W']*100:.1f}%\n"
                  for i,(t,tp) in enumerate(top5))
        + (
            f"\n🥅 <b>ФИНАЛ: {rt(f_h)} vs {rt(f_a)}</b>\n"
            f"🏆 <b>Чемпион: {rt(f_w)}</b>\n"
            f"🥈 Финалист: <b>{rt(f_l)}</b>\n\n"
        )
        + f"\n{DASH}\n📊 <b>ПОБЕДИТЕЛИ ГРУПП:</b>\n"
        + "".join(
            f"<code>{letter}</code>  🥇 <b>{rt((g2.get(letter,['?'])+['?'])[0])}</b> · "
            f"{rt((g2.get(letter,['?','?'])+['?','?'])[1])}\n"
            for letter in "ABCDEFGHIJKL")
        + "\n<i>Прогноз на каждый матч — ежедневно. Подробнее — @wc2026_football_bot</i>"
    )
    for p in split_text(text):
        await bot.send_message(chat_id=ch,text=p,parse_mode=ParseMode.HTML)
    return True

async def cmd_post_preview(u,c):
    if not is_admin(u.effective_user.id):
        await u.message.reply_text("\u274c Нет доступа."); return
    ch=os.environ.get("CHANNEL_ID","")
    if not ch: await u.message.reply_text("\u274c CHANNEL_ID не задан."); return
    ok=await _post_today_to_channel(c.bot,ch)
    await u.message.reply_text("\u2705 Пост отправлен." if ok else "\u26bd Сегодня матчей нет.")

async def cmd_post_forecast(u,c):
    if not is_admin(u.effective_user.id):
        await u.message.reply_text("\u274c Нет доступа."); return
    ch=os.environ.get("CHANNEL_ID","")
    if not ch: await u.message.reply_text("\u274c CHANNEL_ID не задан."); return
    await u.message.reply_text("\u23f3 Публикую прогноз в канал…")
    await _post_forecast_to_channel(c.bot,ch)
    await u.message.reply_text("\u2705 Готово.")


# ============================================================
# SCHEDULED JOBS
# ============================================================

async def job_morning(ctx):
    ch=os.environ.get("CHANNEL_ID","")
    if ch: await _post_today_to_channel(ctx.bot,ch)

async def job_results(ctx):
    ch=os.environ.get("CHANNEL_ID","")
    if not ch: return
    yesterday=date.today()-timedelta(days=1)
    finished=get_finished_fixtures(yesterday)
    if not finished: return
    resolve_predictions(yesterday)   # сверяем прогнозы с реальностью
    correct=total=0
    lines=[f"\U0001f4cb <b>ИТОГИ {fmt_date_ru(yesterday).upper()}</b>",
           "<i>Прогноз нейросети vs реальность</i>",f"{SEP}",""]
    for d,home,away,hs,as_,host in finished:
        p_h,p_d,p_a=predict_1x2(home,away,str(host)!="1")
        pred=outcome_code(p_h,p_d,p_a)
        actual="H" if hs>as_ else ("A" if as_>hs else "D")
        pred_txt={"H":f"Победа {ru_team(home)}","D":"Ничья","A":f"Победа {ru_team(away)}"}[pred]
        ok=pred==actual; correct+=int(ok); total+=1
        block=[
            f"\U0001f3df <b>{rt(home)}</b> {hs}:{as_} <b>{rt(away)}</b>",
            f"\U0001f916 Прогноз: {esc(pred_txt)} — {'\u2705 сбылся' if ok else '\u274c мимо'}",
        ]
        chg=get_match_elo_change(d,home,away)
        if chg and chg[0] is not None:
            eh_b,eh_a,ea_b,ea_a=chg
            dh=eh_a-eh_b; da=ea_a-ea_b
            def _sg(x): return (f"+{x:.0f}" if x>=0 else f"{x:.0f}")
            block.append(
                f"\U0001f4ca Elo: {rt(home)} <code>{eh_b:.0f}→{eh_a:.0f}</code> (<b>{_sg(dh)}</b>) · "
                f"{rt(away)} <code>{ea_b:.0f}→{ea_a:.0f}</code> (<b>{_sg(da)}</b>)"
            )
        block.append("")
        lines+=block
    if total:
        _s=get_accuracy_stats(); c_all,r_all=_s["correct"],_s["resolved"]
        season=f" \u00b7 за турнир: {c_all}/{r_all}" if r_all else ""
        lines+=[f"{SEP}",
                f"\U0001f3af <b>Точность дня: {correct}/{total} ({correct/total*100:.0f}%)</b>{season}",
                "\U0001f916 @wc2026_football_bot"]
    for p in split_text("\n".join(lines)):
        await ctx.bot.send_message(chat_id=ch,text=p,parse_mode=ParseMode.HTML)

async def _post_elo_summary_to_channel(bot,ch,title_suffix=""):
    """Сводный пост: как турнир переписал Elo всех команд (pre → now).
    Сортируется по Δ: топ-рост и топ-падение."""
    base=get_elo_baseline()
    cur =get_current_elo_db()
    if not base or not cur: return False
    rows=[]
    for t,e_now in cur.items():
        e_pre=base.get(t)
        if e_pre is None: continue
        d=e_now-e_pre
        if abs(d)<0.5: continue
        rows.append((t,e_pre,e_now,d))
    if not rows: return False
    rows.sort(key=lambda x:x[3],reverse=True)
    top_up=[r for r in rows if r[3]>0][:10]
    top_dn=[r for r in rows if r[3]<0]
    top_dn=sorted(top_dn,key=lambda x:x[3])[:10]
    title=f"\U0001f4ca <b>ИЗМЕНЕНИЯ ELO{(' ' + title_suffix) if title_suffix else ''}</b>"
    lines=[title,"<i>Как турнир переписал силу команд</i>",SEP,""]
    def _sg(x): return (f"+{x:.0f}" if x>=0 else f"{x:.0f}")
    if top_up:
        lines.append("\U0001f680 <b>Кто прибавил</b>")
        for i,(t,p,n,d) in enumerate(top_up,1):
            lines.append(f"{i:>2}. <b>{rt(t)}</b>  <code>{p:.0f}</code>→<code>{n:.0f}</code>  <b>{_sg(d)}</b>")
        lines.append("")
    if top_dn:
        lines.append("\U0001f4c9 <b>Кто просел</b>")
        for i,(t,p,n,d) in enumerate(top_dn,1):
            lines.append(f"{i:>2}. <b>{rt(t)}</b>  <code>{p:.0f}</code>→<code>{n:.0f}</code>  <b>{_sg(d)}</b>")
        lines.append("")
    lines.append(SEP)
    lines.append("\U0001f916 @wc2026_football_bot")
    for p in split_text("\n".join(lines)):
        await bot.send_message(chat_id=ch,text=p,parse_mode=ParseMode.HTML)
    return True

async def cmd_elo_summary(u,c):
    if not is_admin(u.effective_user.id): return
    ch=os.environ.get("CHANNEL_ID","")
    if not ch:
        await u.message.reply_text("\u26a0\ufe0f CHANNEL_ID не задан"); return
    suffix=" ".join(c.args).strip() if getattr(c,"args",None) else ""
    ok=await _post_elo_summary_to_channel(c.bot,ch,title_suffix=suffix)
    await u.message.reply_text("\u2705 Отправлено" if ok else "\u26a0\ufe0f Нет данных (нужно ≥1 сыгранный матч)")

async def job_check_notifications(ctx):
    load_all()
    notif=get_pending_notification()
    ch=os.environ.get("CHANNEL_ID","")
    if notif and ch:
        for p in split_text(notif):
            await ctx.bot.send_message(chat_id=ch,text=p,parse_mode=ParseMode.HTML)
        clear_pending_notification()
        log.info("Sent pending forecast update notification")


# ============================================================
# REALITY HELPERS — schedule / results / standings / value
# ============================================================

_UPDATING=False

def ensure_score_columns():
    """Ensure result columns exist so /results and /table work with zero manual setup."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("ALTER TABLE wc2026_fixtures "
                            "ADD COLUMN IF NOT EXISTS home_score INT, "
                            "ADD COLUMN IF NOT EXISTS away_score INT")
            conn.commit()
        log.info("Ensured home_score/away_score columns")
    except Exception as e:
        log.warning("ensure_score_columns: %s",e)

def ensure_artifacts_columns():
    """Ensure wc2026_artifacts has updated_at so /history and /set_live order snapshots correctly."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("ALTER TABLE wc2026_artifacts "
                            "ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW()")
            conn.commit()
        log.info("Ensured wc2026_artifacts.updated_at column")
    except Exception as e:
        log.warning("ensure_artifacts_columns: %s",e)

def third_place_qualifiers():
    """Model estimate of the 8 best third-placed teams that reach the knockouts."""
    probs=BASELINE.get("tournament_probs",{})
    thirds=[]
    for letter in "ABCDEFGHIJKL":
        teams=get_group_teams(letter)
        if len(teams)<3: continue
        teams.sort(key=lambda t:(BASELINE.get("mean_points",{}).get(t,0.0),probs.get(t,{}).get("P_R32",0)),reverse=True)
        third=teams[2]
        thirds.append((third,letter,probs.get(third,{}).get("P_R32",0)))
    thirds.sort(key=lambda x:x[2],reverse=True)
    return thirds[:8]

def get_all_finished():
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT match_date,home,away,home_score,away_score,host "
                    "FROM wc2026_fixtures WHERE home_score IS NOT NULL "
                    "ORDER BY match_date,home")
                return cur.fetchall()
    except Exception as e:
        log.warning("get_all_finished: %s",e); return []

def get_match_elo_change(d,home,away):
    """Возвращает (eh_before, eh_after, ea_before, ea_after) для матча или None."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT elo_home_before, elo_home_after, elo_away_before, elo_away_after "
                    "FROM wc2026_fixtures WHERE match_date=%s AND home=%s AND away=%s",
                    (d,home,away))
                row=cur.fetchone()
                if not row or row[0] is None: return None
                return tuple(float(x) if x is not None else None for x in row)
    except Exception as e:
        log.warning("get_match_elo_change: %s",e); return None

def get_elo_baseline():
    """Pre-tournament Elo snapshot (захватывается при первом ingest-е результатов)."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT team, elo FROM wc2026_elo_baseline")
                return {t:float(e) for t,e in cur.fetchall()}
    except Exception as e:
        log.warning("get_elo_baseline: %s",e); return {}

def get_current_elo_db():
    """Текущий Elo из БД (свежее чтение, не кешированный ELO-словарь)."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT team, elo FROM wc2026_elo")
                return {t:float(e) for t,e in cur.fetchall()}
    except Exception as e:
        log.warning("get_current_elo_db: %s",e); return {}

def get_meta(key,default=None):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM wc2026_meta WHERE key=%s",(key,))
                row=cur.fetchone()
                return row[0] if row else default
    except Exception:
        return default

def set_meta(key,value):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO wc2026_meta (key, value) VALUES (%s, %s) "
                    "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                    (key,str(value)))
            conn.commit()
    except Exception as e:
        log.warning("set_meta: %s",e)

def get_recent_finished(limit=20):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT match_date,home,away,home_score,away_score,host "
                    "FROM wc2026_fixtures WHERE home_score IS NOT NULL "
                    "ORDER BY match_date DESC,home LIMIT %s",(limit,))
                return cur.fetchall()
    except Exception as e:
        log.warning("get_recent_finished: %s",e); return []

def count_finished():
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM wc2026_fixtures WHERE home_score IS NOT NULL")
                return int(cur.fetchone()[0])
    except Exception:
        return 0

def compute_real_standings(letter,finished=None):
    teams=get_group_teams(letter)
    if not teams: return []
    tset=set(teams)
    st={t:{"P":0,"W":0,"D":0,"L":0,"GF":0,"GA":0,"PTS":0} for t in teams}
    for d,home,away,hs,as_,host in (finished if finished is not None else get_all_finished()):
        if home in tset and away in tset:
            sh=st[home]; sa=st[away]
            sh["P"]+=1; sa["P"]+=1
            sh["GF"]+=hs; sh["GA"]+=as_; sa["GF"]+=as_; sa["GA"]+=hs
            if hs>as_: sh["W"]+=1; sh["PTS"]+=3; sa["L"]+=1
            elif as_>hs: sa["W"]+=1; sa["PTS"]+=3; sh["L"]+=1
            else: sh["D"]+=1; sa["D"]+=1; sh["PTS"]+=1; sa["PTS"]+=1
    ranked=sorted(teams,key=lambda t:(st[t]["PTS"],st[t]["GF"]-st[t]["GA"],st[t]["GF"]),reverse=True)
    return [(t,st[t]) for t in ranked]

def group_advance_ranked(letter):
    teams=get_group_teams(letter)
    probs=BASELINE.get("tournament_probs",{})
    teams.sort(key=lambda t:(BASELINE.get("mean_points",{}).get(t,0.0),probs.get(t,{}).get("P_R32",0)),reverse=True)
    return [(t,probs.get(t,{}).get("P_R32",0)) for t in teams]

def value_bets(limit=60,edge_min=0.05,odd_min=1.5,odd_max=7.0,min_prob=0.30):
    out=[]
    for d,home,away,host,o1,ox,o2 in get_fixtures(date.today(),limit=limit):
        try: o1=float(o1); ox=float(ox); o2=float(o2)
        except: continue
        if not (o1 and ox and o2): continue
        neutral=str(host)!="1"
        p_h,p_d,p_a=predict_1x2(home,away,neutral)
        overround=(1.0/o1)+(1.0/ox)+(1.0/o2)
        for code,label,mp,odd in (("1",home,p_h,o1),("X","Ничья",p_d,ox),("2",away,p_a,o2)):
            implied=(1.0/odd)/overround
            edge=mp-implied
            if edge>=edge_min and odd_min<=odd<=odd_max and mp>=min_prob:
                out.append((d,home,away,code,label,odd,mp,implied,edge))
    out.sort(key=lambda x:x[8],reverse=True)
    return out


# ============================================================
# REALITY COMMANDS — /schedule /results /table /value
# ============================================================

async def cmd_schedule(u,c):
    n=12; show_all=False
    if c.args:
        if c.args[0].lower() in ("all","все","всё"): show_all=True
        else:
            try: n=min(int(c.args[0]),40)
            except: pass
    rows=get_fixtures(date.today(),limit=200 if show_all else n)
    if not rows:
        await u.message.reply_text("📅 Ближайших матчей не найдено.",parse_mode=ParseMode.HTML); return
    lines=["📅 <b>РАСПИСАНИЕ МАТЧЕЙ</b>","<i>🌍 Только факты · без прогнозов (прогноз — /today, /next)</i>",SEP]
    _MSK=timezone(timedelta(hours=3))
    items=[]
    for d,home,away,host,o1,ox,o2 in rows:
        kt=get_kickoff(d,home,away)
        if kt:
            msk_dt=kt.astimezone(_MSK); msk_d=msk_dt.date()
        else:
            msk_dt=None; msk_d=d
        items.append((msk_d,msk_dt,d,home,away,host))
    # Группируем и сортируем по МСК-календарному дню (а не по local NA match_date)
    items.sort(key=lambda x:(x[0], x[1] or datetime(x[0].year,x[0].month,x[0].day,tzinfo=_MSK), x[3]))
    cur_day=None
    for msk_d,msk_dt,d,home,away,host in items:
        if msk_d!=cur_day:
            cur_day=msk_d; lines.append(""); lines.append(f"📆 <b>{fmt_date_ru(msk_d)}</b>")
        grp=get_team_group(home) or "?"
        kt_lbl=(msk_dt.strftime("%H:%M")+" \u041c\u0421\u041a") if msk_dt else ""
        tprefix=f"🕒 {kt_lbl}  " if kt_lbl else ""
        lines.append(f"  {tprefix}{rt(home)} — {rt(away)}  <code>[{grp}]</code>")
    lines.append(""); lines.append(SEP)
    lines.append("ℹ️ Прогнозы на матчи: /today · /tomorrow · /next")
    for p in split_text("\n".join(lines)):
        await u.message.reply_text(p,parse_mode=ParseMode.HTML)

async def cmd_results(u,c):
    n=10; show_all=False
    if c.args:
        if str(c.args[0]).lower() in ("all","все","всё"):
            show_all=True
        else:
            try: n=min(int(c.args[0]),100)
            except: pass
    rows=get_all_finished() if show_all else get_recent_finished(limit=n)
    if not rows:
        lines=["📊 <b>РЕЗУЛЬТАТЫ</b>",SEP,"",
               "Сыгранных матчей пока нет — турнир стартует 11 июня.",
               "ℹ️ Прогнозы: /forecast · расписание: /schedule"]
        await u.message.reply_text("\n".join(lines),parse_mode=ParseMode.HTML); return
    lines=["📊 <b>РЕЗУЛЬТАТЫ МАТЧЕЙ</b>","<i>🌍 Реальность vs 🤖 прогноз</i>",SEP,""]
    cor=tot=0
    for d,home,away,hs,as_,host in rows:
        p_h,p_d,p_a=predict_1x2(home,away,str(host)!="1")
        pred=outcome_code(p_h,p_d,p_a)
        actual="H" if hs>as_ else ("A" if as_>hs else "D")
        ok=pred==actual; cor+=int(ok); tot+=1
        mark="✅" if ok else "❌"
        lines.append(f"📆 {fmt_date_ru(d)}  🏟 <b>{rt(home)}</b> {hs}:{as_} <b>{rt(away)}</b>  {mark}")
    lines.append("")
    if tot:
        lines.append(SEP)
        lines.append(f"🎯 <b>Точность показанных: {cor}/{tot} ({cor/tot*100:.0f}%)</b>")
        lines.append("ℹ️ Полная статистика бота: /stats")
    for p in split_text("\n".join(lines)):
        await u.message.reply_text(p,parse_mode=ParseMode.HTML)

async def cmd_table(u,c):
    letters=[c.args[0].upper()] if (c.args and len(c.args[0])==1 and c.args[0].upper() in "ABCDEFGHIJKL") else list("ABCDEFGHIJKL")
    finished=get_all_finished()
    played=len(finished)
    if played:
        head=f"<i>🌍 Факт · сыграно матчей: {played} · И-игры · О-очки · ±-разница</i>"
    else:
        head="<i>🔮 Матчи не сыграны — прогноз мест. ✅ прямой выход · 🟡 претендент на лучшее 3-е место</i>"
    blocks=["🏁 <b>ТУРНИРНЫЕ ТАБЛИЦЫ ГРУПП</b>",head,SEP]
    for letter in letters:
        standings=compute_real_standings(letter,finished=finished)
        any_played=any(s["P"]>0 for _,s in standings)
        lines=["",f"🏙 <b>Группа {letter}</b>"]
        if any_played:
            lines.append("<code> #  Команда        И  О   ±</code>")
            for i,(t,s) in enumerate(standings,1):
                gd=s["GF"]-s["GA"]; nm=esc(ru_team(t))[:13]; mk=" ✅" if i<=2 else ""
                lines.append(f"<code>{i}. {nm:<13}{s['P']:>2}{s['PTS']:>3}{gd:>+4}</code>{mk}")
        else:
            mp_=BASELINE.get("mean_points",{}) or {}
            probs_=BASELINE.get("tournament_probs",{}) or {}
            modal_pair=(BASELINE.get("modal_forecast",{}) or {}).get("group_top2",{}).get(letter)
            all_t=get_group_teams(letter)
            if modal_pair and len(modal_pair)>=2 and modal_pair[0] in all_t and modal_pair[1] in all_t:
                rest=[t for t in all_t if t not in modal_pair[:2]]
                rest.sort(key=lambda t:(mp_.get(t,0.0),probs_.get(t,{}).get("P_R32",0)),reverse=True)
                ranked=[modal_pair[0],modal_pair[1]]+rest
            else:
                ranked=sorted(all_t,key=lambda t:(mp_.get(t,0.0),probs_.get(t,{}).get("P_R32",0)),reverse=True)
            third_set=set(t for t,_,_ in third_place_qualifiers())
            for i,t in enumerate(ranked,1):
                p=probs_.get(t,{}).get("P_R32",0)
                if i<=2: mk="✅"
                elif i==3 and t in third_set: mk="🟡"
                else: mk="▫️"
                lines.append(f"{i}. {mk} <b>{rt(t)}</b>  <i>{p*100:.0f}% на выход</i>")
        blocks.append("\n".join(lines))
    blocks.append("")
    gl=letters[0] if len(letters)==1 else "A"
    blocks.append(f"ℹ️ Прогноз группы: /group {gl} · расписание матчей: /schedule")
    for p in split_text("\n".join(blocks)):
        await u.message.reply_text(p,parse_mode=ParseMode.HTML)

async def cmd_value(u,c):
    bets=value_bets()
    lines=["💰 <b>VALUE-СТАВКИ</b>","<i>Где модель видит перевес над букмекером</i>",SEP,""]
    if not bets:
        lines.append("Сейчас явных value-ставок нет (или коэффициенты не загружены).")
        lines.append("ℹ️ Нужны коэффы (THE_ODDS_API_KEY) и ближайшие матчи.")
    else:
        nm={"1":"П1","X":"Ничья","2":"П2"}
        for d,home,away,code,label,odd,mp,implied,edge in bets[:12]:
            lines.append(f"🏟 <b>{rt(home)}</b> — <b>{rt(away)}</b> · {fmt_date_ru(d)}")
            lines.append(f"   🎯 Ставка: <b>{nm[code]}</b> @ <code>{odd:.2f}</code>")
            lines.append(f"   📈 Модель {mp*100:.0f}% vs букмекер {implied*100:.0f}% → +{edge*100:.0f}% value")
            lines.append("")
    lines.append(SEP)
    lines.append("⚠️ <i>Только информация, не финансовый совет. Ставки — ваш риск.</i>")
    lines.append("🔒 Раздел только в боте — в канал не постится.")
    for p in split_text("\n".join(lines)):
        await u.message.reply_text(p,parse_mode=ParseMode.HTML)


# ============================================================
# AUTO-UPDATE (resource-aware) + RU COMMAND MENU
# ============================================================

def _export_elo_to_csv(path="wc2026_elo.csv"):
    """Дамп wc2026_elo из БД в CSV (для wc2026_simulate.py, который читает CSV)."""
    import csv as _csv
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT team, elo FROM wc2026_elo ORDER BY team")
            rows = cur.fetchall()
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = _csv.writer(f)
        w.writerow(["team", "elo"])
        for t, e in rows:
            w.writerow([t, float(e)])
    log.info("Exported %d Elo rows to %s", len(rows), path)


def _export_fixtures_to_csv(path="wc2026_fixtures.csv"):
    """Дамп wc2026_fixtures из БД в CSV ВКЛЮЧАЯ home_score/away_score сыгранных матчей.
    Симулятор увидит уже сыгранные матчи как факт и не будет их разыгрывать."""
    import csv as _csv
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT match_date, home, away, host, home_score, away_score "
                "FROM wc2026_fixtures ORDER BY match_date, home"
            )
            rows = cur.fetchall()
    played = 0
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = _csv.writer(f)
        w.writerow(["date", "home", "away", "host", "home_score", "away_score", "odds_1", "odds_x", "odds_2"])
        for md, h, a, host, hs, as_ in rows:
            if hs is not None and as_ is not None:
                played += 1
            w.writerow([
                md.isoformat() if hasattr(md, "isoformat") else (md or ""),
                h, a, int(host) if host is not None else 0,
                "" if hs is None else int(hs),
                "" if as_ is None else int(as_),
                "", "", "",
            ])
    log.info("Exported %d fixtures to %s (%d already played)", len(rows), path, played)


async def _run_auto_update(bot):
    global _UPDATING
    api_key=os.environ.get("FOOTBALL_DATA_API_KEY","")
    if not api_key:
        log.info("auto-update skipped: no FOOTBALL_DATA_API_KEY"); return
    if _UPDATING:
        log.info("auto-update skipped: another update is in progress"); return
    _UPDATING=True
    sims=os.environ.get("AUTO_UPDATE_SIMS","30000")
    env=os.environ.copy()
    try:
        before=count_finished()
        subprocess.run([sys.executable,"-X","utf8","wc2026_ingest_results.py"],
                       capture_output=True,text=True,timeout=90,env=env)
        # Best-effort: pull fresh 1X2 bookmaker odds (independent of new results).
        # Safe to fail — sensation_note & /match cards just fall back to stored values.
        if os.environ.get("THE_ODDS_API_KEY"):
            try:
                ro = subprocess.run(
                    [sys.executable, "-X", "utf8", "wc2026_odds_fetch.py"],
                    capture_output=True, text=True, timeout=60, env=env)
                if ro.returncode == 0:
                    log.info("auto-update: fresh odds fetched")
                else:
                    log.warning("auto-update: odds fetch rc=%s stderr=%s",
                                ro.returncode, (ro.stderr or "")[:300])
            except Exception as e:
                log.warning("auto-update: odds fetch exception: %s", e)
        else:
            log.info("auto-update: THE_ODDS_API_KEY not set, skipping odds refresh")
        after=count_finished()
        if after<=before:
            log.info("auto-update: no new results (have %d)",after); return
        log.info("auto-update: %d new results ingested → running re-sim %s", after-before, sims)

        # 1. Дамп свежих Elo + сыгранных счетов из БД в CSV.
        # Симулятор увидит уже сыгранные матчи как факт (не будет их разыгрывать).
        try:
            _export_elo_to_csv()
            _export_fixtures_to_csv()
        except Exception as e:
            log.warning("auto-update: CSV export failed: %s", e)

        # 2. Ре-симуляция 30k прокрутов.
        try:
            r1 = subprocess.run(
                [sys.executable, "-X", "utf8", "wc2026_simulate.py",
                 "--sims", str(sims), "--out", "wc2026_baseline.json"],
                capture_output=True, text=True, timeout=900, env=env)
            if r1.returncode != 0:
                log.warning("auto-update: simulate.py rc=%s stderr=%s",
                            r1.returncode, (r1.stderr or "")[:500])
            else:
                log.info("auto-update: simulate.py OK (sims=%s)", sims)
        except Exception as e:
            log.warning("auto-update: simulate.py exception: %s", e)

        # 3. Заливка нового BASELINE в БД.
        try:
            label = make_snapshot_label("auto")
            r2 = subprocess.run(
                [sys.executable, "-X", "utf8", "wc2026_upload_baseline.py",
                 "wc2026_baseline.json", "--label", label],
                capture_output=True, text=True, timeout=120, env=env)
            if r2.returncode != 0:
                log.warning("auto-update: upload_baseline.py rc=%s stderr=%s",
                            r2.returncode, (r2.stderr or "")[:500])
            else:
                log.info("auto-update: BASELINE uploaded (label=%s)", label)
        except Exception as e:
            log.warning("auto-update: upload_baseline.py exception: %s", e)

        # 4. Перезагружаем в память, сохраняем снапшот, разрешаем прогнозы.
        load_all()
        snapshot_baseline(make_snapshot_label("auto"))
        resolve_predictions(date.today())
        resolve_predictions(date.today()-timedelta(days=1))
        ch=os.environ.get("CHANNEL_ID","")
        notif=get_pending_notification()
        if notif and ch:
            for p in split_text(notif):
                await bot.send_message(chat_id=ch,text=p,parse_mode=ParseMode.HTML)
            clear_pending_notification()
            log.info("auto-update: posted forecast-change notification")

        # 5. Один раз в конце группового этапа — постим сводку «как турнир переписал Elo».
        try:
            if ch and count_finished()>=72 and get_meta("group_stage_summary_posted")!="1":
                ok=await _post_elo_summary_to_channel(bot,ch,title_suffix="ПЕРЕД ПЛЕЙ-ОФФ")
                if ok:
                    set_meta("group_stage_summary_posted","1")
                    log.info("auto-update: posted group-stage Elo summary")
        except Exception as e:
            log.warning("auto-update: elo-summary post failed: %s",e)
    except Exception as e:
        log.warning("auto-update error: %s",e)
    finally:
        _UPDATING=False

async def job_auto_update(ctx):
    if os.environ.get("AUTO_UPDATE","1")=="0":
        return
    await _run_auto_update(ctx.bot)

def _resolve_team_input(q):
    """User input (RU or EN) -> key used in SQUADS/SQUAD_VALUE."""
    if not q: return None
    pool=list(SQUADS) or list(SQUAD_VALUE)
    if not pool: return None
    if q in pool: return q
    low={k.lower():k for k in pool}
    if q.strip().lower() in low: return low[q.strip().lower()]
    rv={ru_team(k).lower():k for k in pool}
    if q.strip().lower() in rv: return rv[q.strip().lower()]
    cand=difflib.get_close_matches(q,pool,n=1,cutoff=0.6)
    if cand: return cand[0]
    candr=difflib.get_close_matches(q.lower(),list(rv),n=1,cutoff=0.6)
    return rv[candr[0]] if candr else None

async def cmd_squad(u,c):
    if not SQUADS and not SQUAD_VALUE:
        await u.message.reply_text("\u0414\u0430\u043d\u043d\u044b\u0435 \u0441\u043e\u0441\u0442\u0430\u0432\u043e\u0432 \u0435\u0449\u0451 \u043d\u0435 \u0437\u0430\u0433\u0440\u0443\u0436\u0435\u043d\u044b."); return
    arg=" ".join(c.args).strip() if getattr(c,"args",None) else ""
    if not arg:
        ranked=sorted(SQUAD_VALUE.items(),key=lambda x:-(x[1] or 0))
        lines=["\U0001f30d <b>\u0420\u0415\u0410\u041b\u042c\u041d\u041e\u0421\u0422\u042c \u00b7 \u0421\u041e\u0421\u0422\u0410\u0412\u042b \u0427\u041c-2026</b>",SEP,"",
               "<i>\u0420\u044b\u043d\u043e\u0447\u043d\u0430\u044f \u0441\u0442\u043e\u0438\u043c\u043e\u0441\u0442\u044c \u0441\u043e\u0441\u0442\u0430\u0432\u043e\u0432 (Transfermarkt). \u041a\u043e\u043c\u0430\u043d\u0434\u0430: <code>/squad \u0411\u0440\u0430\u0437\u0438\u043b\u0438\u044f</code></i>",""]
        for i,(t,v) in enumerate(ranked,1):
            lines.append(f"{i:>2}. {rt(t)} \u2014 \u20ac{(v or 0)/1e6:.0f} \u043c\u043b\u043d")
        for p in split_text("\n".join(lines)):
            await u.message.reply_text(p,parse_mode=ParseMode.HTML)
        return
    key=_resolve_team_input(arg)
    if not key:
        await u.message.reply_text(f"\u041d\u0435 \u043d\u0430\u0448\u0451\u043b \u043a\u043e\u043c\u0430\u043d\u0434\u0443 \u00ab{esc(arg)}\u00bb. \u041e\u0442\u043a\u0440\u043e\u0439 /squad \u0431\u0435\u0437 \u0430\u0440\u0433\u0443\u043c\u0435\u043d\u0442\u0430 \u2014 \u0442\u0430\u043c \u0441\u043f\u0438\u0441\u043e\u043a."); return
    v=SQUAD_VALUE.get(key); players=SQUADS.get(key,[])
    head=[f"\U0001f3f4 <b>{rt(key)} \u2014 \u0441\u043e\u0441\u0442\u0430\u0432 \u0427\u041c-2026</b>",SEP]
    if v: head.append(f"\U0001f4b0 \u0421\u0442\u043e\u0438\u043c\u043e\u0441\u0442\u044c \u0441\u043e\u0441\u0442\u0430\u0432\u0430: <b>\u20ac{v/1e6:.0f} \u043c\u043b\u043d</b> (Transfermarkt)")
    POSRU={"GK":"\U0001f9e4 \u0412\u0440\u0430\u0442\u0430\u0440\u0438","DF":"\U0001f6e1 \u0417\u0430\u0449\u0438\u0442\u043d\u0438\u043a\u0438","MF":"\u2699\ufe0f \u041f\u043e\u043b\u0443\u0437\u0430\u0449\u0438\u0442\u0430","FW":"\u26bd \u041d\u0430\u043f\u0430\u0434\u0435\u043d\u0438\u0435"}
    body=[]
    for pos in ["GK","DF","MF","FW"]:
        grp=[pl for pl in players if pl.get("pos")==pos]
        if not grp: continue
        body.append(f"\n<b>{POSRU[pos]}</b>")
        for pl in grp:
            body.append(f"  {pl.get('number','')}. {esc(_nfkc(pl.get('player','')))} \u00b7 <i>{esc(_nfkc(pl.get('club','')))}</i>")
    for p in split_text("\n".join(head+body)):
        await u.message.reply_text(p,parse_mode=ParseMode.HTML)

# ============================================================
# DEV-ONLY: LEGACY MODE (для сравнения «до» и «после» improvements)
# ============================================================
# Что значит "LEGACY" здесь = система ДО последних нововведений:
#   - frozen pre-tournament Elo (wc2026_elo_baseline), а не живой live-Elo
#   - smart Elo formula не применяется (она и не нужна — мы используем
#     snapshot, который был ДО первого ingest-результатов)
#   - всё остальное (squad-value, calibrator, Poisson) идентично — это
#     было ДО нововведений тоже.
# Современная (NEW) система = predict_1x2() без _elo → берёт live ELO,
# обновлённый по smart-credit формуле в wc2026_ingest_results.py.

def _export_elo_baseline_to_csv(path="wc2026_elo_baseline.csv"):
    """Дамп frozen pre-tournament Elo в CSV для wc2026_simulate.py."""
    import csv as _csv
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT team, elo FROM wc2026_elo_baseline ORDER BY team")
            rows = cur.fetchall()
    if not rows:
        # Fallback: baseline ещё не захвачен (ни одного матча не сыграно) → дамп текущего Elo.
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT team, elo FROM wc2026_elo ORDER BY team")
                rows = cur.fetchall()
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = _csv.writer(f)
        w.writerow(["team", "elo"])
        for t, e in rows:
            w.writerow([t, float(e)])
    return len(rows)


async def cmd_predict_legacy(u,c):
    """/predict_legacy [N] — следующие N матчей: side-by-side СТАРАЯ vs НОВАЯ.
    Старая = frozen pre-tournament Elo. Новая = live Elo после ingest-обновлений."""
    if not is_admin(u.effective_user.id):
        await u.message.reply_text("\U0001f512 Только для разработчика."); return
    n=5
    if c.args:
        try: n=min(int(c.args[0]),MAX_NEXT)
        except: pass
    rows=get_fixtures(date.today(),limit=n)
    if not rows:
        await u.message.reply_text("\u26bd \u041c\u0430\u0442\u0447\u0435\u0439 \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u043e."); return
    baseline_elo = get_elo_baseline()
    if not baseline_elo:
        await u.message.reply_text(
            "\u26a0\ufe0f <code>wc2026_elo_baseline</code> \u043f\u0443\u0441\u0442\u0430.\n"
            "Snapshot \u0437\u0430\u0445\u0432\u0430\u0442\u044b\u0432\u0430\u0435\u0442\u0441\u044f \u0430\u0432\u0442\u043e\u043c\u0430\u0442\u0438\u0447\u0435\u0441\u043a\u0438 \u043f\u0440\u0438 \u043f\u0435\u0440\u0432\u043e\u043c ingest-\u0435 \u0440\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442\u043e\u0432.\n"
            "\u041f\u043e\u0441\u043b\u0435 \u0441\u0442\u0430\u0440\u0442\u0430 \u0427\u041c (\u0441 \u043f\u0435\u0440\u0432\u044b\u043c \u0441\u044b\u0433\u0440\u0430\u043d\u043d\u044b\u043c \u043c\u0430\u0442\u0447\u0435\u043c) \u043a\u043e\u043c\u0430\u043d\u0434\u0430 \u0437\u0430\u0440\u0430\u0431\u043e\u0442\u0430\u0435\u0442.",
            parse_mode=ParseMode.HTML); return
    def _winner(ph, pd, pa):
        if pd >= ph and pd >= pa: return "X"
        return "1" if ph >= pa else "2"
    lines=[
        "\U0001f52c <b>\u0421\u0420\u0410\u0412\u041d\u0415\u041d\u0418\u0415: \u0421\u0422\u0410\u0420\u0410\u042f vs \u041d\u041e\u0412\u0410\u042f \u0421\u0418\u0421\u0422\u0415\u041c\u0410</b>",
        f"<i>\u041f\u0440\u043e\u0433\u043d\u043e\u0437 \u043d\u0430 \u0441\u043b\u0435\u0434\u0443\u044e\u0449\u0438\u0435 {len(rows)} \u043c\u0430\u0442\u0447\u0435\u0439</i>",
        "",
        "\U0001f7e2 <b>\u0421\u0422\u0410\u0420.</b> = frozen pre-tournament Elo (\u0434\u043e \u043d\u043e\u0432\u043e\u0432\u0432\u0435\u0434\u0435\u043d\u0438\u0439)",
        "\U0001f535 <b>\u041d\u041e\u0412.</b>  = live Elo (\u043e\u0431\u043d\u043e\u0432\u043b\u044f\u0435\u0442\u0441\u044f \u043f\u043e\u0441\u043b\u0435 \u043a\u0430\u0436\u0434\u043e\u0433\u043e \u043c\u0430\u0442\u0447\u0430 + smart-credit)",
        SEP,
    ]
    flips=0
    for md, h, a, host, o1, ox, o2 in rows:
        neutral = str(host) != "1"
        nh, nd, na = predict_1x2(h, a, neutral)
        lh, ld, la = predict_1x2(h, a, neutral, _elo=baseline_elo)
        nw, lw = _winner(nh, nd, na), _winner(lh, ld, la)
        flip_emoji = "\u26a0\ufe0f \u0418\u0421\u0425\u041e\u0414 \u0418\u0417\u041c\u0415\u041d\u0418\u041b\u0421\u042f" if nw != lw else ""
        if nw != lw: flips += 1
        eh_b = baseline_elo.get(h, DEFAULT_ELO); ea_b = baseline_elo.get(a, DEFAULT_ELO)
        eh_n = ELO.get(h, DEFAULT_ELO); ea_n = ELO.get(a, DEFAULT_ELO)
        lines += [
            "",
            f"\U0001f3df <b>{esc(rt(h))}</b> \u2014 <b>{esc(rt(a))}</b>  <code>{md}</code>",
            f"   Elo: {rt(h)} {eh_b:.0f}\u2192{eh_n:.0f} ({eh_n-eh_b:+.0f})  |  {rt(a)} {ea_b:.0f}\u2192{ea_n:.0f} ({ea_n-ea_b:+.0f})",
            f"<code>\u0421\u0422\u0410\u0420. {lh*100:4.0f}% / {ld*100:4.0f}% / {la*100:4.0f}%  [{lw}]</code>",
            f"<code>\u041d\u041e\u0412.  {nh*100:4.0f}% / {nd*100:4.0f}% / {na*100:4.0f}%  [{nw}]</code> {flip_emoji}",
            f"   \u0394: 1 {(nh-lh)*100:+.1f}%  X {(nd-ld)*100:+.1f}%  2 {(na-la)*100:+.1f}%",
        ]
    lines.append("")
    if flips:
        lines.append(f"\u26a0\ufe0f \u0421\u0438\u0441\u0442\u0435\u043c\u0430 \u0438\u0437\u043c\u0435\u043d\u0438\u043b\u0430 \u0444\u0430\u0432\u043e\u0440\u0438\u0442\u0430 \u0432 <b>{flips}/{len(rows)}</b> \u043c\u0430\u0442\u0447\u0430\u0445.")
    else:
        lines.append("\u2705 \u0424\u0430\u0432\u043e\u0440\u0438\u0442 \u043d\u0438 \u0432 \u043e\u0434\u043d\u043e\u043c \u043c\u0430\u0442\u0447\u0435 \u043d\u0435 \u043f\u043e\u043c\u0435\u043d\u044f\u043b\u0441\u044f \u2014 \u0445\u0430\u0440\u0430\u043a\u0442\u0435\u0440 \u043f\u0440\u043e\u0433\u043d\u043e\u0437\u043e\u0432 \u0441\u0442\u0430\u0431\u0438\u043b\u044c\u043d\u044b\u0439.")
    for p in split_text("\n".join(lines)):
        await u.message.reply_text(p, parse_mode=ParseMode.HTML)


async def cmd_sim_legacy(u,c):
    """/sim_legacy [sims=10000] — \u043f\u043e\u043b\u043d\u0430\u044f \u0441\u0438\u043c\u0443\u043b\u044f\u0446\u0438\u044f \u0442\u0443\u0440\u043d\u0438\u0440\u0430 \u043d\u0430 frozen pre-tournament Elo.
    \u0420\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442 \u0441\u043e\u0445\u0440\u0430\u043d\u044f\u0435\u0442\u0441\u044f \u0432 wc2026_baseline_legacy.json \u0438 \u041d\u0415 \u043f\u0435\u0440\u0435\u0437\u0430\u043f\u0438\u0441\u044b\u0432\u0430\u0435\u0442 \u043e\u0441\u043d\u043e\u0432\u043d\u043e\u0439 BASELINE."""
    if not is_admin(u.effective_user.id):
        await u.message.reply_text("\U0001f512 \u0422\u043e\u043b\u044c\u043a\u043e \u0434\u043b\u044f \u0440\u0430\u0437\u0440\u0430\u0431\u043e\u0442\u0447\u0438\u043a\u0430."); return
    sims=10000
    if c.args:
        try: sims=max(1000,min(int(c.args[0]),100000))
        except: pass
    await u.message.reply_text(
        f"\U0001f52c \u0417\u0430\u043f\u0443\u0441\u043a\u0430\u044e LEGACY-\u0441\u0438\u043c\u0443\u043b\u044f\u0446\u0438\u044e \u043d\u0430 <b>{sims:,}</b> \u043f\u0440\u043e\u043a\u0440\u0443\u0442\u043e\u0432\n"
        "<i>frozen pre-tournament Elo \u2014 \u043a\u0430\u043a \u0431\u044b\u043b\u043e \u0434\u043e \u043d\u043e\u0432\u043e\u0432\u0432\u0435\u0434\u0435\u043d\u0438\u0439</i>\n"
        "\u2026 1\u20133 \u043c\u0438\u043d\u0443\u0442\u044b. \u041f\u043e \u0433\u043e\u0442\u043e\u0432\u043d\u043e\u0441\u0442\u0438 \u0432\u0435\u0440\u043d\u0443 \u0442\u043e\u043f-15.", parse_mode=ParseMode.HTML)
    try:
        _export_elo_baseline_to_csv("wc2026_elo_baseline.csv")
        _export_fixtures_to_csv()
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-X", "utf8", "wc2026_simulate.py",
             "--sims", str(sims),
             "--elo", "wc2026_elo_baseline.csv",
             "--out", "wc2026_baseline_legacy.json"],
            capture_output=True, text=True, timeout=600
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "")[-800:]
            await u.message.reply_text(f"\u274c Sim \u0443\u043f\u0430\u043b:\n<pre>{esc(err)}</pre>", parse_mode=ParseMode.HTML); return
        with open("wc2026_baseline_legacy.json", encoding="utf-8") as f:
            legacy = json.load(f)
        tp_leg = legacy.get("tournament_probs", {})
        ranked_leg = sorted(tp_leg.items(), key=lambda kv: -kv[1].get("P_W", 0))
        modal_leg = legacy.get("modal_forecast", {}).get("modal_champion", "\u2014")
        tp_new = BASELINE.get("tournament_probs", {})
        modal_new = BASELINE.get("modal_forecast", {}).get("modal_champion", "\u2014")
        played = legacy.get("matches_played", 0)
        total = legacy.get("matches_total", 72)
        lines = [
            "\U0001f52c <b>LEGACY SIMULATION</b>",
            f"<i>{sims:,} \u043f\u0440\u043e\u043a\u0440\u0443\u0442\u043e\u0432 \u00b7 frozen pre-tournament Elo \u00b7 \u043d\u0438\u043a\u0430\u043a\u0438\u0445 in-tournament \u0443\u043b\u0443\u0447\u0448\u0435\u043d\u0438\u0439</i>",
            f"<i>\u0421\u044b\u0433\u0440\u0430\u043d\u043e \u043c\u0430\u0442\u0447\u0435\u0439: {played}/{total} (\u0438\u0445 \u0440\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442\u044b \u0437\u0430\u0444\u0438\u043a\u0441\u0438\u0440\u043e\u0432\u0430\u043d\u044b, \u043d\u0435 \u0440\u0435\u0441\u0438\u043c\u0443\u043b\u0438\u0440\u0443\u044e\u0442\u0441\u044f)</i>",
            SEP,
            f"\U0001f3c6 <b>\u0427\u0435\u043c\u043f\u0438\u043e\u043d (\u0421\u0422\u0410\u0420.):</b> {esc(ru_team(modal_leg))}",
            f"\U0001f3c6 <b>\u0427\u0435\u043c\u043f\u0438\u043e\u043d (\u041d\u041e\u0412.):</b> {esc(ru_team(modal_new))}",
            "",
            f"<b>\u041f\u041e\u041b\u041d\u042b\u0419 \u0420\u0415\u0419\u0422\u0418\u041d\u0413 ({len(ranked_leg)} \u043a\u043e\u043c\u0430\u043d\u0434) \u2014 \u043f\u043e \u0421\u0422\u0410\u0420\u041e\u0419 \u0441\u0438\u0441\u0442\u0435\u043c\u0435:</b>",
            "<pre>",
            f"{'#':<3}{'\u041a\u043e\u043c\u0430\u043d\u0434\u0430':<18}{'P_W':>7}{'P_F':>7}{'P_SF':>7}{'P_QF':>7}{'R16':>7}",
        ]
        for i, (team, probs) in enumerate(ranked_leg, 1):
            pw  = probs.get("P_W",  0)*100
            pf  = probs.get("P_F",  0)*100
            psf = probs.get("P_SF", 0)*100
            pqf = probs.get("P_QF", 0)*100
            pr16= probs.get("P_R16",0)*100
            lines.append(f"{i:<3}{ru_team(team)[:18]:<18}{pw:>6.2f}%{pf:>6.2f}%{psf:>6.2f}%{pqf:>6.2f}%{pr16:>6.1f}%")
        lines.append("</pre>")
        lines.append("")
        lines.append("<b>\u0414\u0435\u043b\u044c\u0442\u0430 P(W) \u0421\u0422\u0410\u0420. \u2192 \u041d\u041e\u0412. (\u0442\u043e\u043f-20):</b>")
        lines.append("<pre>")
        lines.append(f"{'#':<3}{'\u041a\u043e\u043c\u0430\u043d\u0434\u0430':<20}{'\u0421\u0422\u0410\u0420.':>8}{'\u041d\u041e\u0412.':>8}{'\u0394':>8}")
        for i, (team, probs) in enumerate(ranked_leg[:20], 1):
            pw_leg = probs.get("P_W", 0)*100
            pw_new = tp_new.get(team, {}).get("P_W", 0)*100
            delta = pw_new - pw_leg
            arrow = "\u2191" if delta > 0.1 else ("\u2193" if delta < -0.1 else "\u00b7")
            lines.append(f"{i:<3}{ru_team(team)[:20]:<20}{pw_leg:>7.2f}%{pw_new:>7.2f}%{delta:>+7.2f}{arrow}")
        lines.append("</pre>")
        lines.append(f"\n\U0001f4be \u0424\u0430\u0439\u043b: <code>wc2026_baseline_legacy.json</code>")
        lines.append("\U0001f4dd \u041f\u043e\u0432\u0442\u043e\u0440 \u0431\u0435\u0437 \u0441\u0438\u043c\u0443\u043b\u044f\u0446\u0438\u0438: <code>/compare_top</code>")
        for p in split_text("\n".join(lines)):
            await u.message.reply_text(p, parse_mode=ParseMode.HTML)
    except subprocess.TimeoutExpired:
        await u.message.reply_text("\u274c Sim timeout (10 \u043c\u0438\u043d). \u041f\u043e\u043f\u0440\u043e\u0431\u0443\u0439 \u043c\u0435\u043d\u044c\u0448\u0435 sims.")
    except Exception as e:
        await u.message.reply_text(f"\u274c \u041e\u0448\u0438\u0431\u043a\u0430: <pre>{esc(str(e))}</pre>", parse_mode=ParseMode.HTML)


async def cmd_sim_new(u,c):
    """/sim_new [sims=20000] \u2014 \u0440\u0443\u0447\u043d\u043e\u0439 \u0440\u0435\u0441\u0438\u043c \u041d\u041e\u0412\u041e\u0419 \u0441\u0438\u0441\u0442\u0435\u043c\u044b (live Elo + smart-credit).
    \u041f\u0435\u0440\u0435\u0437\u0430\u043f\u0438\u0441\u044b\u0432\u0430\u0435\u0442 \u043e\u0441\u043d\u043e\u0432\u043d\u043e\u0439 BASELINE \u0432 \u0411\u0414 (\u043a\u0430\u043a job_auto_update), \u0441\u044b\u0433\u0440\u0430\u043d\u043d\u044b\u0435 \u043c\u0430\u0442\u0447\u0438 \u0437\u0430\u0444\u0438\u043a\u0441\u0438\u0440\u043e\u0432\u0430\u043d\u044b."""
    global ELO, BASELINE
    if not is_admin(u.effective_user.id):
        await u.message.reply_text("\U0001f512 \u0422\u043e\u043b\u044c\u043a\u043e \u0434\u043b\u044f \u0440\u0430\u0437\u0440\u0430\u0431\u043e\u0442\u0447\u0438\u043a\u0430."); return
    sims=20000
    if c.args:
        try: sims=max(1000,min(int(c.args[0]),100000))
        except: pass
    await u.message.reply_text(
        f"\U0001f680 \u0417\u0430\u043f\u0443\u0441\u043a\u0430\u044e \u041d\u041e\u0412\u0423\u042e \u0441\u0438\u043c\u0443\u043b\u044f\u0446\u0438\u044e \u043d\u0430 <b>{sims:,}</b> \u043f\u0440\u043e\u043a\u0440\u0443\u0442\u043e\u0432\n"
        "<i>live Elo (\u0441 smart-credit \u0438 in-tournament \u043e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u044f\u043c\u0438)</i>\n"
        "<i>\u0421\u044b\u0433\u0440\u0430\u043d\u043d\u044b\u0435 \u043c\u0430\u0442\u0447\u0438 \u043f\u043e\u0434\u0441\u0442\u0430\u0432\u043b\u044f\u044e\u0442\u0441\u044f \u043a\u0430\u043a \u0444\u0430\u043a\u0442</i>\n"
        f"\u26a0\ufe0f <b>\u041f\u0435\u0440\u0435\u0437\u0430\u043f\u0438\u0448\u0435\u0442 \u043e\u0441\u043d\u043e\u0432\u043d\u043e\u0439 BASELINE</b> \u0432 \u0411\u0414.\n"
        "\u2026 2\u20135 \u043c\u0438\u043d\u0443\u0442. \u0411\u0443\u0434\u0435\u0442 \u043f\u043e\u043b\u043d\u044b\u0439 \u0440\u0435\u0439\u0442\u0438\u043d\u0433 \u043a\u043e\u043c\u0430\u043d\u0434.", parse_mode=ParseMode.HTML)
    try:
        _export_elo_to_csv("wc2026_elo.csv")
        _export_fixtures_to_csv()
        old_modal = BASELINE.get("modal_forecast", {}).get("modal_champion", "\u2014")
        old_tp = dict(BASELINE.get("tournament_probs", {}))
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-X", "utf8", "wc2026_simulate.py",
             "--sims", str(sims),
             "--elo", "wc2026_elo.csv",
             "--out", "wc2026_baseline.json"],
            capture_output=True, text=True, timeout=900
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "")[-800:]
            await u.message.reply_text(f"\u274c Sim \u0443\u043f\u0430\u043b:\n<pre>{esc(err)}</pre>", parse_mode=ParseMode.HTML); return
        # \u0417\u0430\u043b\u0438\u0432\u0430\u0435\u043c \u0432 \u0411\u0414 + \u0432\u0435\u0440\u0441\u0438\u043e\u043d\u043d\u044b\u0439 \u0441\u043d\u044d\u043f\u0448\u043e\u0442
        label = make_snapshot_label("manual", with_time=True)
        upload = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-X", "utf8", "wc2026_upload_baseline.py",
             "wc2026_baseline.json", "--label", label],
            capture_output=True, text=True, timeout=120
        )
        if upload.returncode != 0:
            err = (upload.stderr or upload.stdout or "")[-500:]
            await u.message.reply_text(f"\u26a0\ufe0f Sim \u043e\u043a, \u043d\u043e upload \u0443\u043f\u0430\u043b:\n<pre>{esc(err)}</pre>", parse_mode=ParseMode.HTML)
        # \u041f\u0435\u0440\u0435\u0437\u0430\u0433\u0440\u0443\u0436\u0430\u0435\u043c BASELINE \u0432 \u043f\u0440\u043e\u0446\u0435\u0441\u0441\u0435
        load_all()
        with open("wc2026_baseline.json", encoding="utf-8") as f:
            new_data = json.load(f)
        tp = new_data.get("tournament_probs", {})
        ranked = sorted(tp.items(), key=lambda kv: -kv[1].get("P_W", 0))
        new_modal = new_data.get("modal_forecast", {}).get("modal_champion", "\u2014")
        played = new_data.get("matches_played", 0)
        total = new_data.get("matches_total", 72)
        lines = [
            "\U0001f680 <b>\u041d\u041e\u0412\u0410\u042f \u0421\u0418\u041c\u0423\u041b\u042f\u0426\u0418\u042f</b>",
            f"<i>{sims:,} \u043f\u0440\u043e\u043a\u0440\u0443\u0442\u043e\u0432 \u00b7 live Elo + smart-credit</i>",
            f"<i>\u0421\u044b\u0433\u0440\u0430\u043d\u043e: {played}/{total} (\u0440\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442\u044b \u0437\u0430\u0444\u0438\u043a\u0441\u0438\u0440\u043e\u0432\u0430\u043d\u044b)</i>",
            f"<i>\U0001f4cc \u0421\u043d\u044d\u043f\u0448\u043e\u0442: <code>{label}</code></i>",
            SEP,
            f"\U0001f3c6 <b>\u0427\u0435\u043c\u043f\u0438\u043e\u043d:</b> {esc(ru_team(new_modal))}",
        ]
        if old_modal != new_modal:
            lines.append(f"   <i>\u0431\u044b\u043b\u043e: {esc(ru_team(old_modal))} \u2192 \u0441\u0442\u0430\u043b\u043e: {esc(ru_team(new_modal))}</i>")
        lines += [
            "",
            f"<b>\u041f\u041e\u041b\u041d\u042b\u0419 \u0420\u0415\u0419\u0422\u0418\u041d\u0413 ({len(ranked)} \u043a\u043e\u043c\u0430\u043d\u0434)</b>",
            "<pre>",
            f"{'#':<3}{'\u041a\u043e\u043c\u0430\u043d\u0434\u0430':<18}{'P_W':>7}{'P_F':>7}{'P_SF':>7}{'P_QF':>7}{'R16':>7}",
        ]
        for i, (team, probs) in enumerate(ranked, 1):
            pw  = probs.get("P_W",  0)*100
            pf  = probs.get("P_F",  0)*100
            psf = probs.get("P_SF", 0)*100
            pqf = probs.get("P_QF", 0)*100
            pr16= probs.get("P_R16",0)*100
            lines.append(f"{i:<3}{ru_team(team)[:18]:<18}{pw:>6.2f}%{pf:>6.2f}%{psf:>6.2f}%{pqf:>6.2f}%{pr16:>6.1f}%")
        lines.append("</pre>")
        # \u0414\u0435\u043b\u044c\u0442\u0430 \u043e\u0442\u043d\u043e\u0441\u0438\u0442\u0435\u043b\u044c\u043d\u043e \u043f\u0440\u0435\u0434\u044b\u0434\u0443\u0449\u0435\u0433\u043e BASELINE
        if old_tp:
            lines += ["", "<b>\u0427\u0442\u043e \u0438\u0437\u043c\u0435\u043d\u0438\u043b\u043e\u0441\u044c \u043e\u0442\u043d\u043e\u0441\u0438\u0442\u0435\u043b\u044c\u043d\u043e \u043f\u0440\u0435\u0434\u044b\u0434\u0443\u0449\u0435\u0433\u043e BASELINE (\u0442\u043e\u043f-10 \u0434\u0432\u0438\u0436\u0435\u043d\u0438\u0439):</b>"]
            deltas = []
            for t, p in tp.items():
                pw_new = p.get("P_W", 0)*100
                pw_old = old_tp.get(t, {}).get("P_W", 0)*100
                deltas.append((t, pw_old, pw_new, pw_new-pw_old))
            deltas.sort(key=lambda r: -abs(r[3]))
            lines.append("<pre>")
            lines.append(f"{'\u041a\u043e\u043c\u0430\u043d\u0434\u0430':<20}{'\u0431\u044b\u043b\u043e':>8}{'\u0441\u0442\u0430\u043b\u043e':>9}{'\u0394':>8}")
            for t, old_p, new_p, d in deltas[:10]:
                ar = "\u2191" if d > 0.1 else ("\u2193" if d < -0.1 else "\u00b7")
                lines.append(f"{ru_team(t)[:20]:<20}{old_p:>7.2f}%{new_p:>8.2f}%{d:>+7.2f}{ar}")
            lines.append("</pre>")
        lines.append(f"\n\u2705 BASELINE \u043e\u0431\u043d\u043e\u0432\u043b\u0451\u043d \u0432 \u0411\u0414. /modal, /forecast \u0438 \u043a\u0430\u043d\u0430\u043b \u0442\u0435\u043f\u0435\u0440\u044c \u0438\u0441\u043f\u043e\u043b\u044c\u0437\u0443\u044e\u0442 \u044d\u0442\u0443 \u0432\u0435\u0440\u0441\u0438\u044e.")
        for p in split_text("\n".join(lines)):
            await u.message.reply_text(p, parse_mode=ParseMode.HTML)
    except subprocess.TimeoutExpired:
        await u.message.reply_text("\u274c Sim timeout (15 \u043c\u0438\u043d). \u041f\u043e\u043f\u0440\u043e\u0431\u0443\u0439 \u043c\u0435\u043d\u044c\u0448\u0435 sims.")
    except Exception as e:
        await u.message.reply_text(f"\u274c \u041e\u0448\u0438\u0431\u043a\u0430: <pre>{esc(str(e))}</pre>", parse_mode=ParseMode.HTML)



# ============================================================
# АРХИВ И СУПЕР-СРАВНЕНИЕ
# ============================================================

def _resolve_team_name(name, pool):
    """Resolve a RU/EN team name (fuzzy) against a pool of canonical EN names."""
    if not name:
        return None
    name = name.strip()
    if name in pool:
        return name
    low = {t.lower(): t for t in pool}
    if name.lower() in low:
        return low[name.lower()]
    rv = {ru_team(t).lower(): t for t in pool}
    if name.lower() in rv:
        return rv[name.lower()]
    cand = difflib.get_close_matches(name, list(pool), n=1, cutoff=0.6)
    if cand:
        return cand[0]
    candr = difflib.get_close_matches(name.lower(), list(rv), n=1, cutoff=0.6)
    if candr:
        return rv[candr[0]]
    return next((t for t in pool
                 if name.lower() in t.lower() or name.lower() in ru_team(t).lower()), None)


def _split_two_teams(tokens, pool):
    """Split word tokens into two team names (each may be multi-word)."""
    for i in range(1, len(tokens)):
        a = _resolve_team_name(" ".join(tokens[:i]), pool)
        b = _resolve_team_name(" ".join(tokens[i:]), pool)
        if a and b:
            return a, b
    return None, None


def _find_match_info(data, a, b):
    """Find a modal prediction for a vs b in a snapshot dict.
    Returns a dict with _home/_away/_stage plus p_home/p_draw/p_away/score/winner/adv."""
    data = data or {}
    for rd in (data.get("modal_bracket") or {}).get("rounds", []):
        for m in rd.get("matches", []):
            if {m.get("home"), m.get("away")} == {a, b}:
                d = dict(m)
                d["_home"] = m.get("home"); d["_away"] = m.get("away"); d["_stage"] = rd.get("code")
                return d
    mm = data.get("modal_matches", {}) or {}
    for key in (f"{a}|{b}", f"{b}|{a}"):
        if key in mm:
            d = dict(mm[key]); kh, ka = key.split("|", 1)
            d["_home"] = kh; d["_away"] = ka; d["_stage"] = mm[key].get("stage")
            return d
    return None


async def cmd_reload(u, c):
    if not is_admin(u.effective_user.id): return
    load_all()
    await u.message.reply_text("✅ База обновлена из БД! Бот использует свежий прогноз.")

async def cmd_set_live(u, c):
    if not is_admin(u.effective_user.id): return
    if not c.args: return await u.message.reply_text("Использование: /set_live 2026-06-09_prematch")
    lbl = c.args[0]
    import json
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT content FROM wc2026_artifacts WHERE key=%s", (f"baseline_{lbl}",))
            r = cur.fetchone()
            if not r: return await u.message.reply_text("❌ Снимок не найден.")
            cur.execute("UPDATE wc2026_artifacts SET content=%s, updated_at=NOW() WHERE key='baseline'", (json.dumps(r[0]),))
            conn.commit()
    load_all()
    await u.message.reply_text(f"✅ Снимок {lbl} загружен в бота как основной!")

async def cmd_history(u, c):
    rows = []
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT key, updated_at, content->>'matches_played', "
                    "       content->>'matches_total', "
                    "       content->'modal_forecast'->>'modal_champion' "
                    "FROM wc2026_artifacts WHERE key LIKE 'baseline_%' "
                    "ORDER BY updated_at DESC NULLS LAST, key DESC LIMIT 20")
                rows = cur.fetchall()
    except Exception as e:
        log.warning("cmd_history: %s", e)
    if not rows:
        return await u.message.reply_text("📂 Архив пуст. Прогнозы появятся после первого прогона.")
    lines = ["📜 <b>АРХИВ ПРОГНОЗОВ</b>",
             "<i>Нажми на команду, чтобы открыть архивный прогноз. "
             "Можно уточнить команду или матч: /view_&lt;label&gt; Аргентина Бразилия</i>", ""]
    for k, dt, played, total, champ in rows:
        lbl = k.replace("baseline_", "")
        if lbl in ('legacy', 'legacy_fixed'): continue
        lbl_safe = lbl.replace("-", "_")
        when = (dt.strftime('%d.%m.%Y %H:%M') + " UTC") if dt else ""
        pl = int(played) if played else 0
        tot = int(total) if total else 72
        ch = ru_team(champ) if champ else "?"
        head = f"📅 <code>{esc(lbl)}</code>"
        if when: head += f" · {when}"
        lines.append(head)
        lines.append(f"   {pl}/{tot} · 🏆 {esc(ch)} · 👉 /view_{lbl_safe}\n")
    await u.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

def _list_snapshot_keys():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT key FROM wc2026_artifacts WHERE key LIKE 'baseline%'")
            return [row[0] for row in cur.fetchall()]

def _resolve_snapshot_key(token):
    # Map a user-typed label to a real artifact key. Returns (key, label) or (None, token).
    token = (token or "").strip()
    if token.startswith("/view_"):
        token = token[len("/view_"):]
    low = token.lower()
    if not token or low in ("live", "baseline", "now", "current"):
        return "baseline", "live"
    keys = _list_snapshot_keys()
    labels = {}
    for k in keys:
        if k == "baseline":
            labels["live"] = k
        elif k.startswith("baseline_"):
            labels[k[len("baseline_"):]] = k
    def norm(x):
        return x.lower().replace("-", "_")
    nt = norm(token)
    for lbl, k in labels.items():
        if norm(lbl) == nt:
            return k, lbl
    if token in keys:
        return token, (token[len("baseline_"):] if token.startswith("baseline_") else "live")
    cands = [(lbl, k) for lbl, k in labels.items() if norm(lbl).startswith(nt) or nt in norm(lbl)]
    if cands:
        cands.sort(key=lambda x: x[0])
        return cands[-1][1], cands[-1][0]
    return None, token

def _fmt_bracket_lines(data):
    # Playoff bracket lines, supporting new (modal_bracket) and old (modal_knockout) formats.
    out = []
    br = data.get("modal_bracket") or {}
    rounds = br.get("rounds") if isinstance(br, dict) else None
    if rounds:
        names = {"R32": "1/16 финала", "R16": "1/8 финала", "QF": "1/4 финала",
                 "SF": "1/2 финала", "F": "Финал"}
        for rnd in rounds:
            code = rnd.get("code", "")
            out.append("")
            out.append(f"<b>{names.get(code, code)}:</b>")
            for m in rnd.get("matches", []):
                h = ru_team(m.get("home", "?")); a = ru_team(m.get("away", "?"))
                w = m.get("winner"); adv = m.get("adv")
                tail = ""
                if w:
                    tail += f" → 🏆 <b>{esc(ru_team(w))}</b>"
                if isinstance(adv, (int, float)):
                    tail += f" · {adv*100:.0f}%"
                out.append(f"• {esc(h)} vs {esc(a)}{tail}")
        return out
    ko = data.get("modal_knockout")
    if not ko:
        ko = (data.get("modal_forecast") or {}).get("modal_knockout")
    if ko:
        out.append("")
        out.append("<b>Плей-офф:</b>")
        for line in ko:
            out.append("• " + str(line).replace("->", "➡️"))
    return out

async def cmd_view(u, c):
    import json
    text = (u.message.text or "").strip()
    parts = text.split()
    cmd = parts[0] if parts else "/view"
    if cmd.startswith("/view_"):
        token = cmd[len("/view_"):]
        extra = parts[1:]
    else:
        token = parts[1] if len(parts) > 1 else "live"
        extra = parts[2:]

    key, lbl = _resolve_snapshot_key(token)
    if not key:
        avail = _list_snapshot_keys()
        labs = sorted(set(("live" if k == "baseline" else k[len("baseline_"):]) for k in avail))
        shown = "\n".join(f"• <code>{esc(x)}</code>" for x in labs[:30]) or "—"
        return await u.message.reply_text(
            f"❌ Версия «<code>{esc(token)}</code>» не найдена.\n\nДоступные версии (см. /snapshots):\n{shown}",
            parse_mode=ParseMode.HTML)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT content FROM wc2026_artifacts WHERE key=%s", (key,))
            r = cur.fetchone()
    if not r:
        return await u.message.reply_text("❌ Снимок не найден.")
    data = json.loads(r[0]) if isinstance(r[0], str) else r[0]
    probs = data.get("tournament_probs", {}) or {}
    pool = list(probs.keys()) or list(ELO.keys())

    # /view <label> TeamA TeamB  -> single match in this snapshot
    if len(extra) >= 2:
        ta, tb = _split_two_teams(extra, pool)
        if not ta or not tb:
            return await u.message.reply_text("❌ Не удалось распознать команды.")
        info = _find_match_info(data, ta, tb)
        ml = [f"🔎 <b>МАТЧ В ПРОГНОЗЕ ({esc(lbl)})</b>",
              f"🏟 <b>{rt(ta)}</b> — <b>{rt(tb)}</b>", f"{SEP}"]
        if not info:
            ml.append("<i>Этого матча нет в данной версии прогноза.</i>")
        else:
            ph = (info.get("p_home") or 0)*100; pd = (info.get("p_draw") or 0)*100; pawy = (info.get("p_away") or 0)*100
            w = info.get("winner"); sc = info.get("score", "?"); adv = info.get("adv")
            ml.append(f"П1 {ph:.0f}% · Х {pd:.0f}% · П2 {pawy:.0f}%")
            ml.append(f"🎯 Модальный счёт: <code>{esc(sc)}</code>")
            if w:
                advt = f" ({adv*100:.0f}% проходит)" if isinstance(adv, (int, float)) else ""
                ml.append(f"➡️ Дальше проходит: <b>{rt(w)}</b>{advt}")
        for part in split_text("\n".join(ml)):
            await u.message.reply_text(part, parse_mode=ParseMode.HTML)
        return

    # /view <label> Team  -> one team's stage probabilities
    if len(extra) == 1:
        tname = _resolve_team_name(extra[0], pool)
        if not tname or tname not in probs:
            return await u.message.reply_text("❌ Команда не найдена в этой версии.")
        tp = probs.get(tname, {}) or {}
        order = [("P_R32", "1/16"), ("P_R16", "1/8"), ("P_QF", "1/4"),
                 ("P_SF", "1/2"), ("P_F", "Финал"), ("P_W", "Чемпион")]
        tl = [f"🏴 <b>{rt(tname)} — прогноз ({esc(lbl)})</b>", f"{SEP}"]
        for kk, nm in order:
            tl.append(f"{nm}: <b>{(tp.get(kk, 0) or 0)*100:.1f}%</b>")
        for part in split_text("\n".join(tl)):
            await u.message.reply_text(part, parse_mode=ParseMode.HTML)
        return

    # /view <label>  -> full archived forecast
    modal = data.get("modal_forecast", {}) or {}
    champ = modal.get("modal_champion") or (data.get("modal_bracket") or {}).get("champion") or "?"
    played = data.get("matches_played", 0); total = data.get("matches_total", 72)
    lines = [
        f"🕰 <b>АРХИВНАЯ ВЕРСИЯ ПРОГНОЗА: {esc(lbl)}</b>",
        f"<i>Сыграно матчей на момент снимка: {played}/{total}</i>",
        f"<i>Модальный чемпион: <b>{esc(ru_team(champ))}</b></i>",
        "", "🏆 <b>ТОП-10 ПРЕТЕНДЕНТОВ (шанс на кубок):</b>",
    ]
    top = sorted(probs.items(), key=lambda x: (x[1] or {}).get("P_W", 0), reverse=True)[:10]
    for i, (t, tp) in enumerate(top, 1):
        tp = tp or {}
        pw = tp.get("P_W", 0)*100; pf = tp.get("P_F", 0)*100
        lines.append(f"{i}. <b>{esc(ru_team(t))}</b> — 🥇 {pw:.1f}% (финал: {pf:.1f}%)")
    lines += _fmt_bracket_lines(data)
    lines.append("")
    lines.append(f"<i>Сравнить с другой версией: /compare {esc(lbl)} &lt;другая&gt;</i>")
    for part in split_text("\n".join(lines)):
        await u.message.reply_text(part, parse_mode=ParseMode.HTML)


async def cmd_compare_top(u, c):
    if not is_admin(u.effective_user.id): return
    if len(c.args) < 2:
        await u.message.reply_text(
            "Использование:\n"
            "  /compare live 2026-06-09_prematch — по всем стадиям\n"
            "  /compare live 2026-06-09_prematch Аргентина Бразилия — конкретный матч")
        return

    key_a = c.args[0]
    key_b = c.args[1]
    match_tokens = c.args[2:]
    if key_a == "live": key_a = "baseline"
    elif not key_a.startswith("baseline"): key_a = f"baseline_{key_a}"
    if key_b == "live": key_b = "baseline"
    elif not key_b.startswith("baseline"): key_b = f"baseline_{key_b}"
    
    import json
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT content FROM wc2026_artifacts WHERE key=%s", (key_a,))
            ra = cur.fetchone()
            cur.execute("SELECT content FROM wc2026_artifacts WHERE key=%s", (key_b,))
            rb = cur.fetchone()
        
    if not ra: return await u.message.reply_text(f"❌ Снимок не найден: <code>{key_a}</code>", parse_mode=ParseMode.HTML)
    if not rb: return await u.message.reply_text(f"❌ Снимок не найден: <code>{key_b}</code>", parse_mode=ParseMode.HTML)
        
    a = json.loads(ra[0]) if isinstance(ra[0], str) else ra[0]
    b = json.loads(rb[0]) if isinstance(rb[0], str) else rb[0]

    lbl_a0 = key_a.replace("baseline_","") if key_a!="baseline" else "live"
    lbl_b0 = key_b.replace("baseline_","") if key_b!="baseline" else "live"
    if match_tokens:
        pool = (list((a.get("tournament_probs") or {}).keys())
                or list((b.get("tournament_probs") or {}).keys()) or list(ELO.keys()))
        ta, tb = _split_two_teams(match_tokens, pool)
        if not ta or not tb:
            return await u.message.reply_text("❌ Не удалось распознать команды для сравнения матча.")
        ia = _find_match_info(a, ta, tb)
        ib = _find_match_info(b, ta, tb)
        def _mline(info):
            if not info: return "  <i>нет прогноза в этом снимке</i>"
            ph=(info.get("p_home") or 0)*100; pd=(info.get("p_draw") or 0)*100; pawy=(info.get("p_away") or 0)*100
            w=info.get("winner"); sc=info.get("score","?")
            wt=f" · ➡️ {rt(w)}" if w else ""
            return f"  П1 {ph:.0f}% · Х {pd:.0f}% · П2 {pawy:.0f}% · 🎯 <code>{esc(sc)}</code>{wt}"
        mlines=["🔄 <b>СРАВНЕНИЕ МАТЧА</b>",
                f"🏟 <b>{rt(ta)}</b> — <b>{rt(tb)}</b>", f"{SEP}",
                f"📌 <code>{esc(lbl_a0)}</code>:", _mline(ia),
                f"📌 <code>{esc(lbl_b0)}</code>:", _mline(ib)]
        for p in split_text("\n".join(mlines)):
            await u.message.reply_text(p, parse_mode=ParseMode.HTML)
        return

    pa = a.get("tournament_probs", {}) or {}
    pb = b.get("tournament_probs", {}) or {}
    gpa = a.get("group_positions", {}) or {}
    gpb = b.get("group_positions", {}) or {}

    def get_top_deltas(stage_key, dict_a, dict_b, is_group=False, min_delta=0.01):
        shifts = []
        for t in sorted(set(dict_a)|set(dict_b)):
            va = dict_a.get(t, {}); vb = dict_b.get(t, {})
            ta = ((va.get(stage_key, 0) if isinstance(va, dict) else 0) or 0) * 100
            tb = ((vb.get(stage_key, 0) if isinstance(vb, dict) else 0) or 0) * 100
            d = tb - ta
            if abs(d) >= min_delta * 100: shifts.append((t, ta, tb, d))
        shifts.sort(key=lambda r: -abs(r[3]))
        return shifts[:5]

    def format_shifts(shifts, title):
        if not shifts: return []
        res = ["", f"<b>{title}:</b>"]
        for t, ta, tb, d in shifts:
            arrow = "📈" if d > 0 else "📉"
            sign = "+" if d >= 0 else ""
            res.append(f"{arrow} <b>{esc(ru_team(t))}</b>: {ta:.1f}% → {tb:.1f}% (<b>{sign}{d:.1f}пп</b>)")
        return res

    lbl_a = key_a.replace("baseline_","") if key_a!="baseline" else "live"
    lbl_b = key_b.replace("baseline_","") if key_b!="baseline" else "live"
    pa_played = a.get('matches_played',0) or 0
    pb_played = b.get('matches_played',0) or 0
    pb_total = b.get('matches_total',72) or 72
    
    lines=[f"🔄 <b>СУПЕР-СРАВНЕНИЕ ПРОГНОЗОВ</b>", f"{SEP}",
           f"📌 <code>{esc(lbl_a)}</code> → <code>{esc(lbl_b)}</code>",
           f"<i>Сыграно: {pa_played} → {pb_played} / {pb_total}</i>"]

    champ_a = (a.get("modal_forecast") or {}).get("modal_champion")
    champ_b = (b.get("modal_forecast") or {}).get("modal_champion")
    if champ_a or champ_b:
        if champ_a == champ_b:
            lines += ["", f"🏆 Модальный чемпион не изменился: <b>{esc(ru_team(champ_a) if champ_a else '?')}</b>"]
        else:
            lines += ["", f"🏆 Чемпион (модель): <b>{esc(ru_team(champ_a) if champ_a else '?')}</b> → <b>{esc(ru_team(champ_b) if champ_b else '?')}</b>"]

    lines += format_shifts(get_top_deltas("P_W", pa, pb), "Топ изменений: Шанс на ЧЕМПИОНСТВО")
    lines += format_shifts(get_top_deltas("P_F", pa, pb), "Топ изменений: Выход в ФИНАЛ")
    lines += format_shifts(get_top_deltas("P_SF", pa, pb), "Топ изменений: Выход в 1/2 ПОЛУФИНАЛ")
    lines += format_shifts(get_top_deltas("P_QF", pa, pb), "Топ изменений: Выход в 1/4 ЧЕТВЕРТЬФИНАЛ")
    lines += format_shifts(get_top_deltas("P_R16", pa, pb), "Топ изменений: Выход в 1/8 ФИНАЛА")
    lines += format_shifts(get_top_deltas("P_R32", pa, pb), "Топ изменений: Выход в 1/16 ПЛЕЙ-ОФФ")
    lines += format_shifts(get_top_deltas("1", gpa, gpb, is_group=True, min_delta=0.015), "Топ изменений: 1-Е МЕСТО В ГРУППЕ")

    if len(lines) < 7: lines.append("<i>Заметных изменений нет (все дельты < 1 пп)</i>")

    for p in split_text(chr(10).join(lines)):
        await u.message.reply_text(p, parse_mode=ParseMode.HTML)


async def _post_init(app):
    from telegram import BotCommand
    cmds=[
        BotCommand("forecast","🏆 Полный прогноз турнира"),
        BotCommand("modal","🗺 Только сетка плей-офф"),
        BotCommand("baseline","🥇 Топ-15 претендентов"),
        BotCommand("schedule","📅 Расписание матчей"),
        BotCommand("results","📊 Результаты матчей"),
        BotCommand("table","🏁 Таблицы групп"),
        BotCommand("today","📆 Матчи сегодня"),
        BotCommand("tomorrow","📆 Матчи завтра"),
        BotCommand("next","⏭ Следующие матчи"),
        BotCommand("team","🏴 Профиль команды"),
        BotCommand("group","🏙 Группа A–L"),
        BotCommand("standings","📋 Прогноз всех групп"),
        BotCommand("value","💰 Value-ставки"),
        BotCommand("stats","🎯 Точность прогнозов"),
        BotCommand("history","📂 История обновлений"),
        BotCommand("snapshots","🗂 Все снимки прогноза"),
        BotCommand("diff","🔄 Сравнить снимки"),
        BotCommand("view","🕰 Архивная версия прогноза"),
        BotCommand("compare","🔄 Сравнить две версии"),
        BotCommand("about","ℹ️ О модели"),
        BotCommand("help","❓ Все команды"),
        BotCommand("squad","\U0001f30d \u0420\u0435\u0430\u043b\u044c\u043d\u044b\u0435 \u0441\u043e\u0441\u0442\u0430\u0432\u044b"),
    ]
    try:
        await app.bot.set_my_commands(cmds)
        log.info("RU command menu set (%d)",len(cmds))
    except Exception as e:
        log.warning("set_my_commands: %s",e)


# ---- Russian UI text (overrides earlier defs) ----
WELCOME = "\n".join([
    "⚽️ <b>WC2026 FOOTBALL BOT</b> 🏆",
    "<i>ИИ-прогнозы на Чемпионат Мира 2026</i>",
    SEP,
    "",
    "🧠 <b>Как это работает:</b>",
    "• Elo-рейтинг + калибровка по коэффициентам",
    "• 100 000 симуляций Монте-Карло всего турнира",
    "• Прогноз живой: пересчёт после каждого игрового дня",
    "",
    DASH,
    "🔮 <b>ПРОГНОЗЫ</b> <i>(мнение нейросети)</i>",
    "🏆 /forecast — полный прогноз: группы → выход → плей-офф",
    "🗺 /modal — только сетка плей-офф (каждый матч)",
    "🥇 /baseline — топ-15 претендентов",
    "📋 /standings — все 12 групп + 8 лучших 3-х мест",
    "🏙 /group — расклад группы, например <code>/group F</code>",
    "🏴 /team — профиль, например <code>/team Argentina</code>",
    "📆 /today · /tomorrow — матчи дня с прогнозом",
    "⏭ /next [N] — следующие матчи с прогнозом",
    "🆚 /match A B [YYYY-MM-DD] — прогноз на конкретный матч",
    "",
    "🌍 <b>РЕАЛЬНОСТЬ</b> <i>(только факты)</i>",
    "📅 /schedule — расписание матчей (без прогнозов)",
    "📊 /results — реальные счета + сверка с прогнозом",
    "🏁 /table — таблицы групп по очкам (фактические)",
    "👥 /squad — реальные составы и их стоимость (Transfermarkt)",
    "",
    "📈 <b>СТАТИСТИКА</b>",
    "🎯 /stats — точность прогнозов нейросети",
    "📂 /history — архив версий прогноза",
    "🗂 /snapshots — список всех сохранённых версий",
    "🕰 /view &lt;версия&gt; — открыть архивную версию (можно +команда или +2 команды)",
    "🔄 /compare live &lt;версия&gt; — сравнить две версии по всем стадиям",
    "",
    "💰 <b>СТАВКИ (только в боте, не в канал)</b>",
    "💰 /value — где модель видит перевес над букмекером",
    "",
    "ℹ️ /about — о модели · ❓ /help — справка",
    SEP,
    "📢 Канал: @WC2026Neuro · 🤖 @wc2026_football_bot",
])

HELP = "\n".join([
    "📋 <b>ВСЕ КОМАНДЫ</b>",
    SEP,
    "",
    "🔮 <b>Прогнозы</b> <i>(мнение нейросети)</i>",
    "/forecast — весь турнир: группы → выход → плей-офф",
    "/modal — полная сетка плей-офф (каждый матч)",
    "/baseline — топ-15 чемпионов",
    "/standings — все 12 групп + 8 лучших 3-х мест",
    "/group — расклад группы, например <code>/group F</code>",
    "/team — профиль, например <code>/team Argentina</code>",
    "/today · /tomorrow — матчи дня с прогнозом",
    "/next [N] — следующие N матчей с прогнозом",
    "",
    "🌍 <b>Реальность</b> <i>(только факты)</i>",
    "/schedule [N|all] — расписание матчей",
    "/results [N] — реальные счета + сверка прогноза",
    "/table [X] — таблицы групп по очкам (фактические)",
    "/squad [команда] — реальные составы и их рыночная стоимость",
    "",
    "📈 <b>Статистика</b>",
    "/stats — точность прогнозов нейросети",
    "/history — архив версий прогноза",
    "/snapshots — список всех сохранённых версий прогноза",
    "/view &lt;версия&gt; [команда] [и соперник] — открыть архивную версию",
    "",
    "🛠 <b>Только для админа</b>",
    "/update — подгрузить свежие результаты матчей",
    "/set_live &lt;версия&gt; — сделать версию текущей (live)",
    "/compare live &lt;версия&gt; [команда соперник] — сравнить версии",
    "",
    "💰 <b>Ставки</b> <i>(только в боте)</i>",
    "/value — value-беты по коэффициентам",
    "",
    "ℹ️ /about · ❓ /help",
])


# ============================================================
# MAIN
# ============================================================

def main():
    global DB_URL
    DB_URL=(os.environ.get("DATABASE_PUBLIC_URL") or os.environ.get("DATABASE_URL") or "").strip()
    if not DB_URL: sys.exit("\u274c Set DATABASE_PUBLIC_URL")
    token=os.environ.get("BOT_TOKEN","").strip()
    if not token: sys.exit("\u274c Set BOT_TOKEN")

    load_all()
    ensure_predictions_table()
    ensure_score_columns()
    ensure_artifacts_columns()

    try: asyncio.get_event_loop()
    except RuntimeError: asyncio.set_event_loop(asyncio.new_event_loop())

    app=Application.builder().token(token).post_init(_post_init).build()

    handlers=[
        ("start",cmd_start),("help",cmd_help),("about",cmd_about),("reload",cmd_reload),
        ("set_live",cmd_set_live),("compare",cmd_compare_top),
        ("baseline",cmd_baseline),("forecast",cmd_forecast),("modal",cmd_modal),
        ("today",cmd_today),("tomorrow",cmd_tomorrow),("next",cmd_next),
        ("match",cmd_match),
        ("team",cmd_team),("group",cmd_group),("standings",cmd_standings),
        ("stats",cmd_stats),("history",cmd_history),("snapshots",cmd_snapshots),
        ("diff",cmd_diff),("update",cmd_update),
        ("view",cmd_view),
        ("schedule",cmd_schedule),("results",cmd_results),
        ("table",cmd_table),("value",cmd_value),("squad",cmd_squad),
        ("post_preview",cmd_post_preview),("post_forecast",cmd_post_forecast),
        ("elo_summary",cmd_elo_summary),
        # Dev-only legacy comparison commands
        ("predict_legacy",cmd_predict_legacy),
        ("sim_legacy",cmd_sim_legacy),
        ("sim_new",cmd_sim_new),
        ("compare_top",cmd_compare_top),
    ]
    for name,fn in handlers:
        app.add_handler(CommandHandler(name,fn))
    
    from telegram.ext import MessageHandler, filters
    app.add_handler(MessageHandler(filters.Regex(r"^/view_"), cmd_view))

    async def _on_error(update, context):
        log.exception("Unhandled handler error", exc_info=context.error)
        try:
            msg = getattr(update, "effective_message", None)
            if msg is not None:
                await msg.reply_text("❌ Внутренняя ошибка при обработке запроса — уже залогировано, попробуйте ещё раз.")
        except Exception:
            pass
    app.add_error_handler(_on_error)

    if app.job_queue:
        mh=int(os.environ.get("MORNING_POST_UTC_HOUR","2"))
        rh=int(os.environ.get("RESULTS_POST_UTC_HOUR","16"))
        app.job_queue.run_daily(job_morning, time=dtime(mh,0,tzinfo=timezone.utc))
        app.job_queue.run_daily(job_results, time=dtime(rh,0,tzinfo=timezone.utc))
        app.job_queue.run_repeating(job_check_notifications, interval=7200, first=300)
        if os.environ.get("AUTO_UPDATE","1")!="0":
            ah=int(os.environ.get("AUTO_UPDATE_UTC_HOUR","17"))
            app.job_queue.run_daily(job_auto_update, time=dtime(ah,30,tzinfo=timezone.utc))
            log.info("Auto-update scheduled: %02d:30 UTC (sims=%s)",ah,os.environ.get("AUTO_UPDATE_SIMS","30000"))
        log.info("Scheduled: morning=%02d:00 UTC, results=%02d:00 UTC, notify=2h",mh,rh)

    log.info("Bot started (%d commands)",len(handlers))
    app.run_polling(drop_pending_updates=True)


if __name__=="__main__":
    main()
