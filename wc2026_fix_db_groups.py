#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""wc2026_fix_db_groups.py

One-shot DB fixer: rewrites baseline.groups in Postgres so the letters
match the OFFICIAL FIFA WC 2026 draw (A-L). Does NOT touch simulation
results (tournament_probs, group_positions, mean_points, modal_forecast)
- only the 'groups' dict keys are remapped via team-majority vote.

Usage:

    # local DB (uses DATABASE_URL from env)
    py -X utf8 wc2026_fix_db_groups.py

    # Railway DB - paste the public URL straight in (or set DATABASE_PUBLIC_URL)
    py -X utf8 wc2026_fix_db_groups.py --db "postgresql://USER:PASS@HOST:PORT/DB"

    py -X utf8 wc2026_fix_db_groups.py --railway        # uses DATABASE_PUBLIC_URL

After running: /reload in the bot (then /forecast).
"""
from __future__ import annotations
import os, sys, json, unicodedata, argparse
from collections import Counter
import string
from urllib.parse import urlparse

try:
    import psycopg2
except ImportError:
    print("[fix] need psycopg2: pip install psycopg2-binary"); sys.exit(2)

# ---- OFFICIAL FIFA WC 2026 groups ---------------------------------------
OFFICIAL_GROUPS = {
    "A": ["Mexico", "South Africa", "South Korea", "Czech Republic"],
    "B": ["Canada", "Bosnia and Herzegovina", "Qatar", "Switzerland"],
    "C": ["Brazil", "Morocco", "Haiti", "Scotland"],
    "D": ["USA", "Paraguay", "Australia", "Turkey"],
    "E": ["Germany", "Cura\u00e7ao", "Ivory Coast", "Ecuador"],
    "F": ["Netherlands", "Japan", "Sweden", "Tunisia"],
    "G": ["Belgium", "Egypt", "Iran", "New Zealand"],
    "H": ["Spain", "Cape Verde", "Saudi Arabia", "Uruguay"],
    "I": ["France", "Senegal", "Iraq", "Norway"],
    "J": ["Argentina", "Algeria", "Austria", "Jordan"],
    "K": ["Portugal", "DR Congo", "Uzbekistan", "Colombia"],
    "L": ["England", "Croatia", "Ghana", "Panama"],
}

ALIASES = {
    "cote d ivoire":"ivory coast",
    "cote divoire":"ivory coast",
    "czechia":"czech republic",
    "united states":"usa",
    "united states of america":"usa",
    "korea republic":"south korea",
    "republic of korea":"south korea",
    "bosnia":"bosnia and herzegovina",
    "bosnia herzegovina":"bosnia and herzegovina",
    "curacao":"cura\u00e7ao",
    "dr congo":"dr congo",
    "democratic republic of the congo":"dr congo",
    "congo dr":"dr congo",
    "cape verde islands":"cape verde",
    "saudi":"saudi arabia",
    "netherland":"netherlands",
    "holland":"netherlands",
}

def _norm(name: str) -> str:
    if not name: return ""
    s = unicodedata.normalize("NFKD", str(name))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower().replace("'", " ").replace("-", " ").replace(".", " ")
    s = " ".join(s.split())
    return ALIASES.get(s, s)

_OFFICIAL_NORM = {}
for L, ts in OFFICIAL_GROUPS.items():
    for t in ts:
        _OFFICIAL_NORM[_norm(t)] = L

def relabel_groups(old: dict) -> tuple[dict, dict]:
    """Return (new_groups, letter_map old_letter->new_letter)."""
    proposals = {}
    for old_letter, teams in old.items():
        votes = Counter()
        for t in teams or []:
            off = _OFFICIAL_NORM.get(_norm(t))
            if off: votes[off] += 1
        proposals[old_letter] = votes
    assigned, used = {}, set()
    # process groups with strongest signal first
    order = sorted(proposals.keys(),
                   key=lambda k: -(proposals[k].most_common(1)[0][1] if proposals[k] else 0))
    for old_letter in order:
        for cand, _ in proposals[old_letter].most_common():
            if cand not in used:
                assigned[old_letter] = cand; used.add(cand); break
    free = [L for L in string.ascii_uppercase[:12] if L not in used]
    for old_letter in old:
        if old_letter not in assigned:
            assigned[old_letter] = free.pop(0); used.add(assigned[old_letter])
    new = {}
    for old_letter, teams in old.items():
        new[assigned[old_letter]] = list(teams or [])
    new_sorted = {L: new[L] for L in sorted(new.keys())}
    return new_sorted, assigned

def relabel_top2(g2: dict):
    """Re-key modal_forecast.group_top2 by official letter (teams stay).
    This dict drives the '1-е и 2-е места по группам' section."""
    if not g2:
        return g2, False
    new = {}
    for old_letter, teams in g2.items():
        off = None
        for t in (teams or []):
            off = _OFFICIAL_NORM.get(_norm(t))
            if off:
                break
        new[off or old_letter] = teams
    out = {L: new[L] for L in sorted(new.keys())}
    return out, (out != g2)

def get_dsn(cli_url: str | None = None, prefer_public: bool = False) -> str:
    if cli_url:
        return cli_url
    if prefer_public:
        dsn = (os.environ.get("DATABASE_PUBLIC_URL")
               or os.environ.get("DATABASE_URL")
               or os.environ.get("POSTGRES_URL"))
    else:
        dsn = (os.environ.get("DATABASE_URL")
               or os.environ.get("DATABASE_PUBLIC_URL")
               or os.environ.get("POSTGRES_URL"))
    if not dsn:
        print("[fix] no DATABASE_URL / DATABASE_PUBLIC_URL set, and no --db given"); sys.exit(2)
    return dsn

def _safe_host(dsn: str) -> str:
    try:
        p = urlparse(dsn)
        host = p.hostname or "?"
        port = p.port or ""
        db = (p.path or "/").lstrip("/") or "?"
        return f"{host}:{port}/{db}"
    except Exception:
        return "?"

def fix_groups_table(cur):
    """Repair the wc2026_groups table (team -> group_name) to official letters.
    This is the table /forecast actually reads via get_group_teams().
    Team spellings are preserved; only group_name is corrected."""
    try:
        cur.execute("SELECT team, group_name FROM wc2026_groups")
    except Exception as e:
        print(f"[fix] wc2026_groups table not found / unreadable: {e}")
        return 0
    rows = cur.fetchall()
    if not rows:
        print("[fix] wc2026_groups table empty, skipping.")
        return 0
    changes = []
    for team, old_letter in rows:
        off = _OFFICIAL_NORM.get(_norm(team))
        if off and off != old_letter:
            changes.append((team, old_letter, off))
    if not changes:
        print("\n[fix] wc2026_groups table already correct.")
        return 0
    print(f"\n[fix] wc2026_groups table: fixing {len(changes)} team(s):")
    for team, old_letter, new_letter in changes:
        print(f"  {team}: {old_letter} -> {new_letter}")
        cur.execute(
            "UPDATE wc2026_groups SET group_name=%s WHERE team=%s",
            (new_letter, team),
        )
    return len(changes)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", help="Postgres connection URL (overrides env)")
    ap.add_argument("--railway", action="store_true",
                    help="Prefer DATABASE_PUBLIC_URL (Railway public endpoint)")
    ap.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    args = ap.parse_args()
    dsn = get_dsn(args.db, prefer_public=args.railway)
    print(f"[fix] target: {_safe_host(dsn)}")
    if not args.yes:
        ans = input("[fix] proceed? [y/N] ").strip().lower()
        if ans not in ("y", "yes", "\u0434", "\u0434\u0430"):
            print("[fix] aborted."); return
    print("[fix] connecting to Postgres ...")
    with psycopg2.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT content FROM wc2026_artifacts WHERE key='baseline'")
            row = cur.fetchone()
            if not row:
                print("[fix] no 'baseline' row in wc2026_artifacts"); sys.exit(1)
            baseline = row[0]
            if isinstance(baseline, (bytes, str)):
                baseline = json.loads(baseline)
            old_groups = baseline.get("groups") or {}
            if not old_groups:
                print("[fix] baseline has no 'groups' field"); sys.exit(1)

            print("[fix] BEFORE:")
            for L in sorted(old_groups.keys()):
                print(f"  {L}: {', '.join(old_groups[L])}")

            new_groups, mapping = relabel_groups(old_groups)

            print("\n[fix] letter remap (old -> new):")
            for old_L in sorted(mapping.keys()):
                arrow = "  same" if mapping[old_L] == old_L else f"  -> {mapping[old_L]}"
                print(f"  {old_L}{arrow}")

            baseline_changed = new_groups != old_groups
            if not baseline_changed:
                print("\n[fix] artifacts.baseline groups already correct.")
            else:
                print("\n[fix] AFTER:")
                for L in sorted(new_groups.keys()):
                    print(f"  {L}: {', '.join(new_groups[L])}")

            # --- ALSO fix the wc2026_groups TABLE (what /forecast reads) ---
            table_changes = fix_groups_table(cur)

            # --- ALSO re-key modal_forecast.group_top2 (the "1-е и 2-е места" block) ---
            modal = baseline.get("modal_forecast") or {}
            old_top2 = modal.get("group_top2") or {}
            new_top2, top2_changed = relabel_top2(old_top2)
            if top2_changed:
                print("\n[fix] modal_forecast.group_top2: re-keying letters -> "
                      + " ".join(sorted(new_top2.keys())))
            else:
                print("\n[fix] modal_forecast.group_top2 already correct (or absent).")

            if not baseline_changed and not table_changes and not top2_changed:
                print("\n[fix] everything already correct, nothing to do.")
                return

            if baseline_changed:
                cur.execute(
                    "UPDATE wc2026_artifacts "
                    "SET content = jsonb_set(content, '{groups}', %s::jsonb) "
                    "WHERE key='baseline'",
                    (json.dumps(new_groups, ensure_ascii=False),),
                )
            if top2_changed:
                cur.execute(
                    "UPDATE wc2026_artifacts "
                    "SET content = jsonb_set(content, '{modal_forecast,group_top2}', %s::jsonb) "
                    "WHERE key='baseline'",
                    (json.dumps(new_top2, ensure_ascii=False),),
                )
        conn.commit()
    print("\n[fix] done. In the bot run /reload (or restart the service), then /forecast.")

if __name__ == "__main__":
    main()
