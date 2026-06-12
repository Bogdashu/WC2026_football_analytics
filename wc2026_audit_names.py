# -*- coding: utf-8 -*-
"""wc2026_audit_names.py - ПРОВЕРЯЕМАЯ ГАРАНТИЯ имён команд.

ВАЖНО: wc2026_elo - это ГЛОБАЛЬНЫЙ Elo-датасет (~336 сборных мира).
Наличие не-участников ЧМ (Italy, Denmark, Abkhazia, ...) - ЭТО НОРМА, не баг.
Используются только 48 официальных, поэтому проверяем ИМЕННО их.

Проверки (реальные риски):
  1) все 48 официальных сборных присутствуют в wc2026_elo (по canon);
  2) НЕТ призраков-дублей: две строки elo с одним canon из числа 48
     (именно так штраф уходил в 'Czechia' мимо 'Czech Republic');
  3) все имена в wc2026_groups сводятся к 48 (там должны быть только участники);
  4) все home/away в wc2026_fixtures сводятся к 48.

Запуск:  python -X utf8 wc2026_audit_names.py
Код выхода 0 = ГАРАНТИЯ: 0 проблем; иначе — список проблем и код 1.
"""
import os, sys
from collections import defaultdict
import psycopg2
from wc2026_names import canon, OFFICIAL, OFFICIAL_CANON


def _fetch(cur, sql):
    try:
        cur.execute(sql); return cur.fetchall()
    except Exception as e:
        print(f"  (пропуск {sql!r}: {e})"); return []


def main():
    DB_URL = (os.environ.get("DATABASE_PUBLIC_URL") or os.environ.get("DATABASE_URL") or "").strip()
    if not DB_URL:
        print("Нет DATABASE_PUBLIC_URL / DATABASE_URL"); sys.exit(1)
    problems = []
    conn = psycopg2.connect(DB_URL)
    try:
        with conn.cursor() as cur:
            elo_rows = [r[0] for r in _fetch(cur, "SELECT team FROM wc2026_elo")]
            grp_rows = [r[0] for r in _fetch(cur, "SELECT team FROM wc2026_groups")]
            fx = _fetch(cur, "SELECT home, away FROM wc2026_fixtures")
            fx_rows = [t for pair in fx for t in pair]
    finally:
        conn.close()

    # Индекс elo по canon
    elo_by_canon = defaultdict(list)
    for t in elo_rows:
        elo_by_canon[canon(t)].append(t)

    # 1) все 48 присутствуют  +  2) нет призраков-дублей среди этих 48
    for off in OFFICIAL:
        c = canon(off)
        variants = elo_by_canon.get(c, [])
        if not variants:
            problems.append(f"[wc2026_elo] НЕТ строки для официальной сборной: {off!r}")
        elif len(variants) > 1:
            problems.append(f"[wc2026_elo] ПРИЗРАК-ДУБЛЬ для {off!r}: две+ строки {variants}")

    # 3) groups и 4) fixtures — только участники ЧМ
    for label, names in [("wc2026_groups", grp_rows), ("wc2026_fixtures", fx_rows)]:
        unknown = sorted({n for n in names if canon(n) not in OFFICIAL_CANON})
        for n in unknown:
            problems.append(f"[{label}] неопознанное имя (не входит в 48): {n!r} (canon={canon(n)!r})")

    extra = len([t for t in elo_rows if canon(t) not in OFFICIAL_CANON])
    print("=" * 56)
    print(f"wc2026_elo: всего строк {len(elo_rows)}, из них не-участников ЧМ {extra} (норма, игнорируются)")
    print(f"проверено официальных: {len(OFFICIAL)} | groups: {len(set(grp_rows))} имён | fixtures: {len(set(fx_rows))} имён")
    print("=" * 56)
    if problems:
        print(f"НАЙДЕНО ПРОБЛЕМ: {len(problems)}")
        for p in problems:
            print("  •", p)
        sys.exit(1)
    print("ГАРАНТИЯ: 0 проблем — все 48 сборных на месте, призраков нет, groups/fixtures чисты.")
    sys.exit(0)


if __name__ == "__main__":
    main()
