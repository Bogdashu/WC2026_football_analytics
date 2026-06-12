#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""wc2026_audit_results.py - GARANTIYA: proveryaet, chto VSE matchi podtyanutsya.

ZAPUSKAT V RAILWAY SHELL:
  python -X utf8 wc2026_audit_results.py

Nichego NE menyaet (tolko chitaet). Svodit kazhdyy match iz football-data.org
s tablicey wc2026_fixtures po kanonicheskim imenam (wc2026_names.canon) i
pokazyvaet:
  [OK]        match v API uvyazan s strokoy fixtures -> schyot podtyanetsya
  [NO ROW]    match v API est, no v fixtures net pary -> NE podtyanetsya
  [UNKNOWN]   imya komandy iz API ne svoditsya k 48 oficialnym -> nuzhen alias
  [DUP]       odnoy pare API sootvetstvuet >1 stroki fixtures -> dvusmyslennost
A takzhe fixtures, kotorye ne svodyatsya k 48 oficialnym imenam.

Esli v itoge 0 problem - garantiya: vse rezultaty budut podgruzhatsya.
"""
import os, sys, json, collections
import urllib.request
import psycopg2
from wc2026_names import canon, is_known, OFFICIAL_CANON, OFFICIAL

DB_URL = (os.environ.get("DATABASE_PUBLIC_URL") or os.environ.get("DATABASE_URL") or "").strip()
KEY = os.environ.get("FOOTBALL_DATA_API_KEY", "").strip()
API = "https://api.football-data.org/v4/competitions/WC/matches?season=2026"

if not DB_URL or not KEY:
    sys.exit("Nuzhny DATABASE_PUBLIC_URL i FOOTBALL_DATA_API_KEY (zapuskai v Railway shell).")

# --- 1) API ---
req = urllib.request.Request(API, headers={
    "X-Auth-Token": KEY, "User-Agent": "WC2026/1.0", "Accept": "application/json",
})
try:
    data = json.loads(urllib.request.urlopen(req, timeout=30).read())
except Exception as e:
    sys.exit("API nedostupno: " + repr(e))
matches = data.get("matches", [])
print("Matchey v API: %d" % len(matches))

# --- 2) fixtures iz BD ---
conn = psycopg2.connect(DB_URL)
with conn.cursor() as cur:
    cur.execute("SELECT match_date, home, away, home_score, away_score FROM wc2026_fixtures")
    fixtures = cur.fetchall()
print("Strok v wc2026_fixtures: %d\n" % len(fixtures))

# Indeks fixtures po kanonicheskoy pare
fx_index = collections.defaultdict(list)
for fd, fh, fa, hs, as_ in fixtures:
    fx_index[frozenset((canon(fh), canon(fa)))].append((fd, fh, fa, hs, as_))

# --- 3) Proverka so storony API ---
ok = no_row = unknown = dup = 0
problems = []
for m in matches:
    hr = m["homeTeam"].get("name") or ""
    ar = m["awayTeam"].get("name") or ""
    status = m.get("status")
    if not hr or not ar:
        continue  # esche ne izvesten sopernik (pley-off zaglushki)
    bad_names = [n for n in (hr, ar) if not is_known(n)]
    if bad_names:
        unknown += 1
        problems.append("[UNKNOWN] API: %s vs %s  -> net aliasa dlya: %s (canon=%s/%s)"
                        % (hr, ar, ", ".join(bad_names), canon(hr), canon(ar)))
        continue
    cands = fx_index.get(frozenset((canon(hr), canon(ar))), [])
    if not cands:
        no_row += 1
        problems.append("[NO ROW] API: %s vs %s (canon=%s/%s) -> net pary v fixtures"
                        % (hr, ar, canon(hr), canon(ar)))
    elif len(cands) > 1:
        dup += 1
        problems.append("[DUP] API: %s vs %s -> %d strok v fixtures: %s"
                        % (hr, ar, len(cands), [(c[0], c[1], c[2]) for c in cands]))
    else:
        ok += 1

# --- 4) Proverka so storony fixtures (imena, kotorye bot ne uznaet) ---
bad_fixtures = []
for fd, fh, fa, hs, as_ in fixtures:
    bad = [n for n in (fh, fa) if not is_known(n)]
    if bad:
        bad_fixtures.append((fd, fh, fa, bad))

# --- 5) Otchyot ---
print("=" * 48)
print("REZULTAT AUDITA (so storony API):")
print("  [OK]      uvyazano s fixtures : %d" % ok)
print("  [NO ROW]  net pary v fixtures : %d" % no_row)
print("  [UNKNOWN] neizvestnoe imya    : %d" % unknown)
print("  [DUP]     dubli v fixtures    : %d" % dup)
print("=" * 48)
if problems:
    print("\nPROBLEMY (ih nado pochinit):")
    for p in problems:
        print("  " + p)
if bad_fixtures:
    print("\nStroki fixtures s neizvestnymi imenami (dobav alias v wc2026_names.py):")
    for fd, fh, fa, bad in bad_fixtures:
        print("  %s: %s vs %s  -> %s" % (fd, fh, fa, ", ".join(bad)))

if not problems and not bad_fixtures:
    print("\nGARANTIYA: 0 problem. Vse matchi svedeny po imenam -")
    print("rezultaty budut podgruzhatsya korrektno dlya vseh sbornyh.")
else:
    print("\nNAYDENY rashozhdeniya vyshe. Dobav nuzhnye alias-y v _ALIASES")
    print("v wc2026_names.py (odno mesto - srazu dlya ingest/fix/audit/bot).")

conn.close()
