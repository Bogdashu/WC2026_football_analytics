#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
wc2026_seed_kickoffs.py

Одноразовый скрипт: заполняет колонку wc2026_fixtures.kickoff_utc
дефолтными слотами МСК (Москва, UTC+3), чтобы в боте
в /schedule /today /tomorrow /next /match показывалось время.

Слоты выбраны по типовому расписанию ЧМ в Северной Америке:
  4 матча/день (группы)  → 19:00, 22:00, 01:00+1, 04:00+1 МСК
  3 матча/день             → 19:00, 22:00, 01:00+1 МСК
  2 матча/день (плей-офф)  → 22:00, 01:00+1 МСК
  1 матч/день (финалы)    → 22:00 МСК

Это только точка отсчёта. Позже точные времена от FIFA можно перезаписать
ручным UPDATE в PG или расширить этот скрипт.

Использование:
  python wc2026_seed_kickoffs.py             # заполнить только NULL
  python wc2026_seed_kickoffs.py --force     # перезаписать всё (осторожно!)
  python wc2026_seed_kickoffs.py --dry-run   # показать, не писать

ENV: берёт DATABASE_PUBLIC_URL или DATABASE_URL (как бот).
"""
import os, sys, argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone

try:
    import psycopg2
except ImportError:
    sys.exit("\u274c psycopg2 \u043d\u0435 \u0443\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d. pip install psycopg2-binary")

MSK = timezone(timedelta(hours=3))
UTC = timezone.utc

SLOT_HOURS = {
    1: [22],
    2: [22, 25],         # 25 = 01:00 следующего дня МСК
    3: [19, 22, 25],
    4: [19, 22, 25, 28], # 28 = 04:00 следующего дня МСК
}

def slot_utc(d, hour_msk):
    base = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=MSK)
    return (base + timedelta(hours=hour_msk)).astimezone(UTC)

def get_conn():
    url = (os.environ.get("DATABASE_PUBLIC_URL") or os.environ.get("DATABASE_URL") or "").strip()
    if not url:
        sys.exit("\u274c \u041d\u0443\u0436\u043d\u0430 DATABASE_PUBLIC_URL \u0438\u043b\u0438 DATABASE_URL")
    return psycopg2.connect(url, sslmode="require")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="\u043f\u0435\u0440\u0435\u0437\u0430\u043f\u0438\u0441\u0430\u0442\u044c \u0443\u0436\u0435 \u0437\u0430\u043f\u043e\u043b\u043d\u0435\u043d\u043d\u044b\u0435")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE wc2026_fixtures ADD COLUMN IF NOT EXISTS kickoff_utc TIMESTAMPTZ")
            cur.execute(
                "SELECT match_date, home, away, kickoff_utc "
                "FROM wc2026_fixtures "
                "ORDER BY match_date, home, away"
            )
            rows = cur.fetchall()
        conn.commit()

    by_day = defaultdict(list)
    for d, h, a, kt in rows:
        by_day[d].append((h, a, kt))

    plan = []
    skipped_existing = 0
    for d in sorted(by_day):
        items = by_day[d]
        n = len(items)
        slots = SLOT_HOURS.get(n)
        if not slots:
            # нестандартный день: равномерно разложить
            slots = [19 + i * 3 for i in range(n)]
        for (h, a, kt), msk_h in zip(items, slots):
            if kt and not args.force:
                skipped_existing += 1
                continue
            plan.append((d, h, a, slot_utc(d, msk_h)))

    print(f"\U0001f4c5 \u0434\u043d\u0435\u0439:        {len(by_day)}")
    print(f"\u26bd \u043c\u0430\u0442\u0447\u0435\u0439:      {sum(len(v) for v in by_day.values())}")
    print(f"\u270f  \u043a \u0437\u0430\u043f\u0438\u0441\u0438: {len(plan)}")
    print(f"\u23ed  \u043f\u0440\u043e\u043f\u0443\u0449\u0435\u043d\u043e:  {skipped_existing} (\u0443\u0436\u0435 \u0432\u044b\u0441\u0442\u0430\u0432\u043b\u0435\u043d\u043e)\n")

    if not plan:
        print("\u2705 \u0412\u0441\u0451 \u0443\u0436\u0435 \u0432\u044b\u0441\u0442\u0430\u0432\u043b\u0435\u043d\u043e. \u0418\u0441\u043f\u043e\u043b\u044c\u0437\u0443\u0439 --force \u0434\u043b\u044f \u043f\u0435\u0440\u0435\u0437\u0430\u043f\u0438\u0441\u0438."); return

    # preview \u043f\u0435\u0440\u0432\u044b\u0435 8
    for d, h, a, kt in plan[:8]:
        msk = kt.astimezone(MSK)
        print(f"  {d} {msk.strftime('%H:%M')} \u041c\u0421\u041a   {h} \u2014 {a}")
    if len(plan) > 8:
        print(f"  ... \u0438 \u0435\u0449\u0451 {len(plan)-8}")

    if args.dry_run:
        print("\n\U0001f50d dry-run, \u0432 \u0411\u0414 \u043d\u0435 \u043f\u0438\u0448\u0443."); return

    with get_conn() as conn:
        with conn.cursor() as cur:
            for d, h, a, kt in plan:
                cur.execute(
                    "UPDATE wc2026_fixtures SET kickoff_utc=%s "
                    "WHERE match_date=%s AND home=%s AND away=%s",
                    (kt, d, h, a)
                )
        conn.commit()
    print(f"\n\u2705 \u0417\u0430\u043f\u0438\u0441\u0430\u043b kickoff_utc \u0434\u043b\u044f {len(plan)} \u043c\u0430\u0442\u0447\u0435\u0439.")
    print("\u041f\u0435\u0440\u0435\u0437\u0430\u043f\u0443\u0441\u0442\u0438 \u0431\u043e\u0442 (\u0438\u043b\u0438 /reload) \u0447\u0442\u043e\u0431\u044b \u043e\u0431\u043d\u043e\u0432\u0438\u0442\u044c \u043a\u044d\u0448.")

if __name__ == "__main__":
    main()
