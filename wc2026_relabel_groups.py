#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Re-label group letters (A-L) in an existing baseline JSON.

Uses the same OFFICIAL FIFA 2026 group assignment + majority-vote +
alphabetic-fallback logic as the updated wc2026_simulate.py.
Does NOT re-simulate anything — just rewrites the 'groups' field
(and 'group_positions' keys if present) in-place. Tiny + instant.

Usage:
  py -X utf8 wc2026_relabel_groups.py wc2026_baseline_MAX.json
  py -X utf8 wc2026_relabel_groups.py wc2026_baseline_MAX.json --out wc2026_baseline_RELABELED.json
"""
import argparse
import json
import string
import sys
import unicodedata
from collections import Counter

# ---- official FIFA WC 2026 groups (must match wc2026_simulate.py) ----
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

_ALIASES = {
    "cote d ivoire": "ivory coast",
    "czechia": "czech republic",
    "usa": "usa",
    "united states": "usa",
    "korea republic": "south korea",
    "republic of korea": "south korea",
    "bosnia": "bosnia and herzegovina",
    "curacao": "cura\u00e7ao",
    "dr congo": "dr congo",
    "congo dr": "dr congo",
    "democratic republic of the congo": "dr congo",
    "cape verde islands": "cape verde",
    "saudi arabia": "saudi arabia",
    "saudi": "saudi arabia",
    "netherland": "netherlands",
    "holland": "netherlands",
}

def _norm(name):
    if not name: return ""
    s = unicodedata.normalize("NFKD", str(name))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower().replace("'", " ").replace("-", " ").replace(".", " ")
    s = " ".join(s.split())
    return _ALIASES.get(s, s)

_OFFICIAL_NORM = {}
for letter, teams in OFFICIAL_GROUPS.items():
    for t in teams:
        _OFFICIAL_NORM[_norm(t)] = letter

def relabel_groups(old_groups):
    """old_groups: dict[old_letter -> list[team_name]].
    Returns dict[new_letter -> list[team_name]] (official A-L labels).
    """
    # 1) majority vote: for each old group, count how many of its teams
    #    belong to each official letter; assign by the most frequent letter.
    proposals = {}  # old_letter -> [(official_letter, vote_count)]
    for old_letter, teams in old_groups.items():
        votes = Counter()
        for t in teams:
            off = _OFFICIAL_NORM.get(_norm(t))
            if off: votes[off] += 1
        proposals[old_letter] = votes

    # 2) resolve conflicts: greedy by highest vote, with alphabetic fallback
    assigned = {}  # old_letter -> new_letter
    used = set()
    # sort old groups by their winning vote count (highest first) so strongest match wins ties
    order = sorted(proposals.keys(),
                   key=lambda k: -(proposals[k].most_common(1)[0][1] if proposals[k] else 0))
    for old_letter in order:
        for cand_letter, _ in proposals[old_letter].most_common():
            if cand_letter not in used:
                assigned[old_letter] = cand_letter
                used.add(cand_letter)
                break
    # fallback for any unresolved (e.g. empty votes): give the first free letter
    free = [L for L in string.ascii_uppercase[:12] if L not in used]
    for old_letter in old_groups:
        if old_letter not in assigned:
            assigned[old_letter] = free.pop(0)
            used.add(assigned[old_letter])

    # 3) build new dict, sorted A..L
    new_groups = {}
    for old_letter, teams in old_groups.items():
        new_groups[assigned[old_letter]] = list(teams)
    return {L: new_groups[L] for L in sorted(new_groups.keys())}

ap = argparse.ArgumentParser()
ap.add_argument("file", help="baseline JSON to relabel")
ap.add_argument("--out", default=None, help="output path (default: overwrite input)")
args = ap.parse_args()

with open(args.file, encoding="utf-8") as f:
    data = json.load(f)

old = data.get("groups") or {}
if not old:
    sys.exit("\u274c JSON has no 'groups' field \u2014 nothing to relabel.")

new = relabel_groups(old)

# print mapping (old -> new) for transparency
print("Group letter remap:")
for old_letter in sorted(old):
    # find which new letter has the same team list as old[old_letter]
    new_letter = next((L for L,ts in new.items() if ts == old[old_letter]), "?")
    sample = old[old_letter][0] if old[old_letter] else "?"
    print(f"  {old_letter} -> {new_letter}  (e.g. {sample})")

data["groups"] = new

out_path = args.out or args.file
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

print(f"\n\u2705 Saved relabeled baseline -> {out_path}")
print("Next: py -X utf8 wc2026_upload_baseline.py " + out_path + " --label prematch_FROZEN")
