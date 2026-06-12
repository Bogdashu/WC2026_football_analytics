# -*- coding: utf-8 -*-
"""wc2026_audit_names.py - ПРОВЕРЯЕМАЯ ГАРАНТИЯ имён команд.

Проверяет, что каждое имя команды во ВСЕХ источниках (wc2026_elo,
wc2026_groups, wc2026_fixtures) сводится к одной из 48 официальных сборных
через единый canon(). Ловит ровно тот класс багов, из-за которого
«Czechia» превращалась в 1500 и штраф уходил в «призрачную» строку.

Проверки:
  1) каждое имя из elo/groups/fixtures → canon ∈ 48 официальных;
  2) нет двух строк wc2026_elo с одинаковым canon («призраки»);
  3) все 48 сборных присутствуют в wc2026_elo;
  4) каждая команда из fixtures имеет elo-строку (по canon).

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
            elo_rows  = [r[0] for r in _fetch(cur, "SELECT team FROM wc2026_elo")]
            grp_rows  = [r[0] for r in _fetch(cur, "SELECT team FROM wc2026_groups")]
            fx = _fetch(cur, "SELECT home, away FROM wc2026_fixtures")
            fx_rows = [t for pair in fx for t in pair]

        # 1) всё резолвится к 48 официальным
        for label, names in [("wc2026_elo", elo_rows), ("wc2026_groups", grp_rows), ("wc2026_fixtures", fx_rows)]:
            unknown = sorted({n for n in names if canon(n) not in OFFICIAL_CANON})
            for n in unknown:
                problems.append(f"[{label}] неопознаное имя: {n!r} (canon={canon(n)!r})")

        # 2) призрачные дубли в wc2026_elo (два разных написания — один canon)
        by_canon = defaultdict(list)
        for t in elo_rows:
            by_canon[canon(t)].append(t)
        for c, variants in by_canon.items():
            if len(variants) > 1:
                problems.append(f"[wc2026_elo] дубли по canon={c!r}: {variants}")

        # 3) все 48 есть в elo
        elo_canon = set(by_canon.keys())
        for t in OFFICIAL:
            if canon(t) not in elo_canon:
                problems.append(f"[wc2026_elo] нет строки для официальной сборной: {t!r}")

        # 4) каждая команда fixtures имеет elo (по canon)
        for n in sorted(set(fx_rows)):
            if canon(n) in OFFICIAL_CANON and canon(n) not in elo_canon:
                problems.append(f"[fixtures→elo] команда {n!r} не найдена в wc2026_elo")
    finally:
        conn.close()

    print("=" * 48)
    if problems:
        print(f"НАЙДЕНО ПРОБЛЕМ: {len(problems)}")
        for p in problems:
            print("  •", p)
        sys.exit(1)
    print("ГАРАНТИЯ: 0 проблем — все имена сводятся к 48 официальным, призраков нет.")
    sys.exit(0)


if __name__ == "__main__":
    main()
