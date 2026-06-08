#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Upload a baseline JSON to wc2026_artifacts in Railway DB.

Always writes to two keys:
  - 'baseline'              the live/current snapshot (the bot reads this)
  - 'baseline_<label>'      a permanent versioned snapshot (history)

Default label = today's date (UTC). You can pass any label:
  py -X utf8 wc2026_upload_baseline.py wc2026_baseline.json
  py -X utf8 wc2026_upload_baseline.py wc2026_baseline.json --label prematch_FROZEN
  py -X utf8 wc2026_upload_baseline.py wc2026_baseline_KO.json --label after_groups
  py -X utf8 wc2026_upload_baseline.py wc2026_baseline.json --label 2026-06-19_after_R1

Common stage labels we recommend:
  prematch_FROZEN           the PDF-frozen pre-tournament baseline
  after_R1 / after_R2 / after_R3   after each group-stage round
  after_groups              once group stage is complete
  after_R16, after_QF, after_SF, after_F

Versioned rows are visible in the bot via /history and /snapshots.
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

import psycopg2

ap = argparse.ArgumentParser()
ap.add_argument("file", help="path to baseline JSON")
ap.add_argument("--label", default=None,
                help="version label (default: today's UTC date)")
ap.add_argument("--live-only", action="store_true",
                help="only update 'baseline' (skip versioned snapshot)")
args = ap.parse_args()

with open(args.file, encoding="utf-8") as f:
    data = json.load(f)

label = args.label or datetime.now(timezone.utc).strftime("%Y-%m-%d")
# sanitize label: letters/digits/_/- only, max 64 chars
label = re.sub(r"[^A-Za-z0-9_\-]", "_", label).strip("_")[:64] or "snapshot"
versioned_key = f"baseline_{label}"

DB_URL = (os.environ.get("DATABASE_PUBLIC_URL") or os.environ.get("DATABASE_URL") or "").strip()
if not DB_URL:
    sys.exit("Set DATABASE_PUBLIC_URL (or DATABASE_URL)")

payload = json.dumps(data)

with psycopg2.connect(DB_URL) as conn:
    with conn.cursor() as cur:
        # 1) live key (always overwrite)
        cur.execute(
            "INSERT INTO wc2026_artifacts (key, content) VALUES ('baseline', %s) "
            "ON CONFLICT (key) DO UPDATE SET content = EXCLUDED.content",
            (payload,),
        )
        # 2) versioned key (overwrite if same label re-uploaded, otherwise insert new)
        if not args.live_only:
            cur.execute(
                "INSERT INTO wc2026_artifacts (key, content) VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET content = EXCLUDED.content",
                (versioned_key, payload),
            )
    conn.commit()

print(f"Uploaded '{args.file}'")
print(f"  -> wc2026_artifacts.key='baseline' (live)")
if not args.live_only:
    print(f"  -> wc2026_artifacts.key='{versioned_key}' (versioned snapshot)")
print(f"  modal_champion: {data.get('modal_forecast', {}).get('modal_champion')}")
print(f"  sims: {data.get('sims')}")
print(f"  generated_at: {data.get('generated_at')}")
print(f"  matches_played: {data.get('matches_played', 0)} / {data.get('matches_total', 72)}")
