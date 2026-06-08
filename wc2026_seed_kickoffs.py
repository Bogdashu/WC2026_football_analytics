#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
wc2026_seed_kickoffs.py — точные времена FIFA для группового этапа ЧМ-2026.

Заполняет колонку wc2026_fixtures.kickoff_utc реальными временами стартов
(72 матча группового этапа). Времена указаны в МСК (UTC+3) и конвертируются в UTC
перед записью в БД. Бот сам отображает их обратно в МСК.

Плей-офф плейсхолдеры из CSV также мэппятся на реальные команды:
  Winner UEFA Playoff A → Bosnia and Herzegovina
  Winner UEFA Playoff B → Sweden
  Winner UEFA Playoff C → Turkey
  Winner UEFA Playoff D → Czech Republic
  Winner FIFA Playoff 1 → DR Congo
  Winner FIFA Playoff 2 → Iraq

Использование:
  python wc2026_seed_kickoffs.py             # обновить все kickoff_utc
  python wc2026_seed_kickoffs.py --dry-run   # показать, не писать

ENV: DATABASE_PUBLIC_URL или DATABASE_URL.
"""
import os, sys, argparse
from datetime import datetime, timedelta, timezone

try:
    import psycopg2
except ImportError:
    sys.exit("\u274c psycopg2 не установлен. pip install psycopg2-binary")

MSK = timezone(timedelta(hours=3))
UTC = timezone.utc

# Плейсхолдеры → реальные команды (для совместимости со старым CSV)
PLAYOFF_ALIAS = {
    "Winner UEFA Playoff A": "Bosnia and Herzegovina",
    "Winner UEFA Playoff B": "Sweden",
    "Winner UEFA Playoff C": "Turkey",
    "Winner UEFA Playoff D": "Czech Republic",
    "Winner FIFA Playoff 1": "DR Congo",
    "Winner FIFA Playoff 2": "Iraq",
}

# Все 72 матча группового этапа.
# Формат: (МСК дата-время "YYYY-MM-DD HH:MM", home, away)
# Время — момент старта в Москве (UTC+3).
FIXTURES = [
    # 11 июня
    ("2026-06-11 22:00", "Mexico", "South Africa"),
    # 12 июня
    ("2026-06-12 05:00", "South Korea", "Czech Republic"),
    ("2026-06-12 22:00", "Canada", "Bosnia and Herzegovina"),
    # 13 июня
    ("2026-06-13 04:00", "United States", "Paraguay"),
    ("2026-06-13 22:00", "Qatar", "Switzerland"),
    # 14 июня
    ("2026-06-14 01:00", "Brazil", "Morocco"),
    ("2026-06-14 04:00", "Haiti", "Scotland"),
    ("2026-06-14 07:00", "Australia", "Turkey"),
    ("2026-06-14 20:00", "Germany", "Cura\u00e7ao"),
    ("2026-06-14 23:00", "Netherlands", "Japan"),
    # 15 июня
    ("2026-06-15 02:00", "C\u00f4te d'Ivoire", "Ecuador"),
    ("2026-06-15 05:00", "Sweden", "Tunisia"),
    ("2026-06-15 19:00", "Spain", "Cape Verde"),
    ("2026-06-15 22:00", "Belgium", "Egypt"),
    # 16 июня
    ("2026-06-16 01:00", "Saudi Arabia", "Uruguay"),
    ("2026-06-16 04:00", "Iran", "New Zealand"),
    ("2026-06-16 22:00", "France", "Senegal"),
    # 17 июня
    ("2026-06-17 01:00", "Iraq", "Norway"),
    ("2026-06-17 04:00", "Argentina", "Algeria"),
    ("2026-06-17 07:00", "Austria", "Jordan"),
    ("2026-06-17 20:00", "Portugal", "DR Congo"),
    ("2026-06-17 23:00", "England", "Croatia"),
    # 18 июня
    ("2026-06-18 02:00", "Ghana", "Panama"),
    ("2026-06-18 05:00", "Uzbekistan", "Colombia"),
    ("2026-06-18 19:00", "Czech Republic", "South Africa"),
    ("2026-06-18 22:00", "Switzerland", "Bosnia and Herzegovina"),
    # 19 июня
    ("2026-06-19 01:00", "Canada", "Qatar"),
    ("2026-06-19 04:00", "Mexico", "South Korea"),
    ("2026-06-19 22:00", "United States", "Australia"),
    # 20 июня
    ("2026-06-20 01:00", "Scotland", "Morocco"),
    ("2026-06-20 03:30", "Brazil", "Haiti"),
    ("2026-06-20 06:00", "Turkey", "Paraguay"),
    ("2026-06-20 20:00", "Netherlands", "Sweden"),
    ("2026-06-20 23:00", "Germany", "C\u00f4te d'Ivoire"),
    # 21 июня
    ("2026-06-21 03:00", "Ecuador", "Cura\u00e7ao"),
    ("2026-06-21 07:00", "Tunisia", "Japan"),
    ("2026-06-21 19:00", "Spain", "Saudi Arabia"),
    ("2026-06-21 22:00", "Belgium", "Iran"),
    # 22 июня
    ("2026-06-22 01:00", "Uruguay", "Cape Verde"),
    ("2026-06-22 04:00", "New Zealand", "Egypt"),
    ("2026-06-22 20:00", "Argentina", "Austria"),
    # 23 июня
    ("2026-06-23 00:00", "France", "Iraq"),
    ("2026-06-23 03:00", "Norway", "Senegal"),
    ("2026-06-23 06:00", "Jordan", "Algeria"),
    ("2026-06-23 20:00", "Portugal", "Uzbekistan"),
    ("2026-06-23 23:00", "England", "Ghana"),
    # 24 июня
    ("2026-06-24 02:00", "Panama", "Croatia"),
    ("2026-06-24 05:00", "Colombia", "DR Congo"),
    ("2026-06-24 22:00", "Switzerland", "Canada"),
    ("2026-06-24 22:00", "Bosnia and Herzegovina", "Qatar"),
    # 25 июня (последний тур групп — параллельные матчи)
    ("2026-06-25 01:00", "Morocco", "Haiti"),
    ("2026-06-25 01:00", "Scotland", "Brazil"),
    ("2026-06-25 04:00", "South Africa", "South Korea"),
    ("2026-06-25 04:00", "Czech Republic", "Mexico"),
    ("2026-06-25 23:00", "Ecuador", "Germany"),
    ("2026-06-25 23:00", "Cura\u00e7ao", "C\u00f4te d'Ivoire"),
    # 26 июня
    ("2026-06-26 02:00", "Tunisia", "Netherlands"),
    ("2026-06-26 02:00", "Japan", "Sweden"),
    ("2026-06-26 05:00", "Paraguay", "Australia"),
    ("2026-06-26 05:00", "Turkey", "United States"),
    ("2026-06-26 22:00", "Norway", "France"),
    ("2026-06-26 22:00", "Senegal", "Iraq"),
    # 27 июня
    ("2026-06-27 03:00", "Uruguay", "Spain"),
    ("2026-06-27 03:00", "Cape Verde", "Saudi Arabia"),
    ("2026-06-27 06:00", "New Zealand", "Belgium"),
    ("2026-06-27 06:00", "Egypt", "Iran"),
    # 28 июня
    ("2026-06-28 00:00", "Panama", "England"),
    ("2026-06-28 00:00", "Croatia", "Ghana"),
    ("2026-06-28 02:30", "Colombia", "Portugal"),
    ("2026-06-28 02:30", "DR Congo", "Uzbekistan"),
    ("2026-06-28 05:00", "Jordan", "Argentina"),
    ("2026-06-28 05:00", "Algeria", "Austria"),
]

def msk_to_utc(s: str) -> datetime:
    dt = datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=MSK)
    return dt.astimezone(UTC)

def get_conn():
    url = (os.environ.get("DATABASE_PUBLIC_URL") or os.environ.get("DATABASE_URL") or "").strip()
    if not url:
        sys.exit("\u274c Нужна DATABASE_PUBLIC_URL или DATABASE_URL")
    return psycopg2.connect(url, sslmode="require")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    # Обратный мэппинг — на случай если в БД остались плейсхолдеры,
    # пробуем оба варианта (alias и реальное имя).
    reverse_alias = {v: k for k, v in PLAYOFF_ALIAS.items()}

    plan = []
    for msk_str, home, away in FIXTURES:
        utc_dt = msk_to_utc(msk_str)
        plan.append((utc_dt, home, away, msk_str))

    print(f"📋 Подготовлено {len(plan)} матчей. Пример:")
    for row in plan[:5]:
        print(f"  {row[3]} МСК  →  {row[0].isoformat()}  {row[1]} vs {row[2]}")
    print("  ...")

    if args.dry_run:
        print("\n🔍 --dry-run: ничего не пишу.")
        return

    updated = 0
    not_found = []
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE wc2026_fixtures ADD COLUMN IF NOT EXISTS kickoff_utc TIMESTAMPTZ")
            for utc_dt, home, away, msk_str in plan:
                # Пробуем оригинальное имя; если 0 строк — пробуем placeholder вариант.
                names_to_try = [(home, away)]
                if home in reverse_alias:
                    names_to_try.append((reverse_alias[home], away))
                if away in reverse_alias:
                    names_to_try.append((home, reverse_alias[away]))
                hit = False
                for h_try, a_try in names_to_try:
                    cur.execute(
                        "UPDATE wc2026_fixtures SET kickoff_utc = %s "
                        "WHERE home = %s AND away = %s",
                        (utc_dt, h_try, a_try),
                    )
                    if cur.rowcount > 0:
                        updated += cur.rowcount
                        hit = True
                        break
                if not hit:
                    not_found.append((msk_str, home, away))
        conn.commit()

    print(f"\n✅ Обновлено строк: {updated} / {len(plan)}")
    if not_found:
        print(f"⚠️ Не нашлись в БД ({len(not_found)}):")
        for msk_str, h, a in not_found:
            print(f"  {msk_str}  {h} vs {a}")

if __name__ == "__main__":
    main()
