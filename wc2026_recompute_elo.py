# -*- coding: utf-8 -*-
"""Разовая ПЕРЕСБОРКА Elo с нуля (от pre-tournament baseline).

Зачем: раньше начисление Elo читало/писало по «сырому» имени из fixtures.
Если команда приходила как «Czechia», а в wc2026_elo она «Czech Republic» — штраф/бонус
уходил в «призрачную» строку, а каноническая не менялась.
После фикса apply_elo_updates резолвит имена по канону, но уже применённые
матчи (elo_applied=TRUE) не пересчитаются сами. Этот скрипт лечит это разово:
  1) восстанавливает wc2026_elo из wc2026_elo_baseline (канонические имена),
  2) удаляет «призрачные» строки (нет в baseline),
  3) сбрасывает elo_applied и before/after снапшоты на матчах,
  4) заново запускает apply_elo_updates() (уже с канон-резолвом).

Безопасно: baseline = pre-tournament Elo, сыграных матчей мало (групповой этап).
Запуск:  python -X utf8 wc2026_recompute_elo.py
"""
import os, sys, logging
import psycopg2
from wc2026_ingest_results import apply_elo_updates

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("recompute_elo")


def main():
    DB_URL = (os.environ.get("DATABASE_PUBLIC_URL") or os.environ.get("DATABASE_URL") or "").strip()
    if not DB_URL:
        print("Нет DATABASE_PUBLIC_URL / DATABASE_URL"); sys.exit(1)
    conn = psycopg2.connect(DB_URL)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM wc2026_elo_baseline")
            n = int(cur.fetchone()[0])
            if n == 0:
                print("wc2026_elo_baseline пуст — нет pre-tournament снапшота, прерываюсь.")
                sys.exit(2)

            # 1) удаляем призрачные строки (их нет в baseline)
            cur.execute(
                "DELETE FROM wc2026_elo WHERE team NOT IN (SELECT team FROM wc2026_elo_baseline)"
            )
            ghosts = cur.rowcount

            # 2) восстанавливаем Elo и обнуляем surprise_credit
            cur.execute(
                "UPDATE wc2026_elo e SET elo = b.elo, surprise_credit = 0 "
                "FROM wc2026_elo_baseline b WHERE e.team = b.team"
            )
            # добавляем пропущенные (если вдруг были удалены)
            cur.execute(
                "INSERT INTO wc2026_elo (team, elo, surprise_credit) "
                "SELECT team, elo, 0 FROM wc2026_elo_baseline "
                "ON CONFLICT (team) DO NOTHING"
            )

            # 3) сбрасываем флаги применения и снапшоты
            cur.execute(
                "UPDATE wc2026_fixtures SET elo_applied = FALSE, "
                "elo_home_before = NULL, elo_home_after = NULL, "
                "elo_away_before = NULL, elo_away_after = NULL"
            )
        conn.commit()
        log.info("Удалено призрачных строк Elo: %d; Elo сброшен к baseline", ghosts)

        # 4) пересчёт
        applied = apply_elo_updates(conn)
        print(f"ГОТОВО: призрачных строк удалено {ghosts}, пересчитано матчей: {applied}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
