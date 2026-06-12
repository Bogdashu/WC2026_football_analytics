#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""wc2026_fix_results.py - diagnostika + dozagruzka rezultatov po KANONICHESKIM imenam.

ZAPUSKAT V RAILWAY SHELL:
  python -X utf8 wc2026_fix_results.py

Bezopasno: tolko prostavlyaet home_score/away_score iz football-data.org
po kanonicheskim imenam (Czechia==Czech Republic, Korea Republic==South Korea i t.d.).
Nichego ne udalyaet. Posle zapuska sdelai /update v bote (zachtyot prognozy).
"""
import os, sys, json, unicodedata
import urllib.request, urllib.error
import psycopg2

DB_URL = (os.environ.get("DATABASE_PUBLIC_URL") or os.environ.get("DATABASE_URL") or "").strip()
KEY = os.environ.get("FOOTBALL_DATA_API_KEY", "").strip()
API = "https://api.football-data.org/v4/competitions/WC/matches?season=2026"

# Kanonicheskie alias-y (vse v nizhnem registre, bez diakritiki posle fold).
_CANON = {
    "czechia": "czech republic", "czech": "czech republic",
    "south korea": "south korea", "korea republic": "south korea",
    "republic of korea": "south korea", "korea": "south korea",
    "usa": "united states", "united states of america": "united states",
    "cote d'ivoire": "ivory coast",
    "bosnia": "bosnia and herzegovina", "bosnia-herzegovina": "bosnia and herzegovina",
    "bosnia & herzegovina": "bosnia and herzegovina",
    "congo dr": "dr congo", "democratic republic of congo": "dr congo",
}


def canon(name):
    s = (name or "").strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return _CANON.get(s, s)


if not DB_URL or not KEY:
    sys.exit("Nuzhny DATABASE_PUBLIC_URL i FOOTBALL_DATA_API_KEY (zapuskai v Railway shell).")

req = urllib.request.Request(API, headers={
    "X-Auth-Token": KEY, "User-Agent": "WC2026/1.0", "Accept": "application/json",
})
try:
    data = json.loads(urllib.request.urlopen(req, timeout=30).read())
except Exception as e:
    sys.exit("API nedostupno: " + repr(e))

fin = [m for m in data.get("matches", [])
       if m.get("status") == "FINISHED"
       and (m.get("score", {}).get("fullTime", {}) or {}).get("home") is not None]
print("FINISHED matchey v API:", len(fin))

conn = psycopg2.connect(DB_URL, connect_timeout=20)
with conn.cursor() as cur:
    cur.execute("SELECT match_date, home, away, home_score, away_score FROM wc2026_fixtures")
    rows = cur.fetchall()

idx = {}
for d, h, a, hs, as_ in rows:
    idx.setdefault(frozenset((canon(h), canon(a))), []).append((d, h, a, hs, as_))

print("\n--- stroki fixtures s Korea/Czech (kak hranyatsya v BD) ---")
for d, h, a, hs, as_ in rows:
    blob = canon(h) + " " + canon(a)
    if "korea" in blob or "czech" in blob:
        print("  %s  %s - %s  score=%s:%s" % (d, h, a, hs, as_))

updated = 0
not_found = []
with conn.cursor() as cur:
    for m in fin:
        hr = m["homeTeam"]["name"]
        ar = m["awayTeam"]["name"]
        ft = m["score"]["fullTime"]
        hs, as_ = ft.get("home"), ft.get("away")
        if hs is None or as_ is None:
            continue
        cands = idx.get(frozenset((canon(hr), canon(ar))))
        if not cands:
            not_found.append((hr, ar))
            continue
        for d, fh, fa, _, _ in cands:
            if canon(fh) == canon(hr):
                sh, sa = hs, as_
            else:
                sh, sa = as_, hs
            cur.execute(
                "UPDATE wc2026_fixtures SET home_score=%s, away_score=%s "
                "WHERE match_date=%s AND home=%s AND away=%s",
                (sh, sa, d, fh, fa))
            if cur.rowcount:
                updated += cur.rowcount
                print("  OK: %s %s:%s %s  (sohraneno v stroku '%s - %s')" % (hr, hs, as_, ar, fh, fa))
conn.commit()

with conn.cursor() as cur:
    cur.execute("SELECT COUNT(*) FROM wc2026_fixtures WHERE home_score IS NOT NULL")
    n = cur.fetchone()[0]
conn.close()

print("\nObnovleno strok:", updated)
print("Ne naydeno v BD (imena ne sovpali dazhe po kanonu):", len(not_found))
for hr, ar in not_found:
    print("  API:", hr, "-", ar)
print("Vsego matchey so schyotom v BD:", n)
print("\nGotovo. Teper v bote sdelai /update (ili dozhdis avto-apdeyta) - prognozy zachtutsya.")
