# ⚽ WC2026 Football Analytics

Telegram-бот с прогнозом ЧМ-2026 на основе Elo + Poisson + сезонные коэффициенты.

## Стек
- Python 3.11+
- `python-telegram-bot` (polling)
- PostgreSQL (Railway)
- The Odds API

## Файлы
- `bot.py` — Telegram-бот (Railway worker)
- `wc2026_simulate.py` — симулятор Монте-Карло
- `wc2026_seed_kickoffs.py` — заполнение `kickoff_utc` дефолтными слотами МСК
- `wc2026_upload_baseline.py` — загрузка фриз-бейзлайна в БД
- `wc2026_fix_db_groups.py`, `wc2026_relabel_groups.py` — починка/перепривязка групп
- `wc2026_ingest_results.py` — затягивание реальных результатов

## Деплой
Railway → автодеплой с main. Переменные окружения см. в Variables.

## Канал
@WC2026Neuro
