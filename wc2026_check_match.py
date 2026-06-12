#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""wc2026_check_match.py — есть ли в football-data.org результат конкретного матча.
​
ЗАПУСКАТЬ В RAILWAY SHELL (там есть ключ и сеть):
  python wc2026_check_match.py            # по умолчанию ищет korea / czech
  python wc2026_check_match.py korea czech
​
Печатает по каждому подходящему матчу дату (UTC), статус и счёт:
  status=FINISHED + счёт  -> результат у API ЕСТЬ, бот подхватит.
  status=IN_PLAY/PAUSED/TIMED/SCHEDULED -> API ЕЩЁ не закрыл матч (ждём).
  ничего не найдено  -> матча нет в фиде (или другое название сборной).
"""
import os, sys, json, urllib.request, urllib.error
​
KEY = os.environ.get("FOOTBALL_DATA_API_KEY", "").strip()
API = "https://api.football-data.org/v4/competitions/WC/matches?season=2026"
wants = [a.lower() for a in sys.argv[1:]] or ["korea", "czech"]
​
if not KEY:
    print("НЕТ FOOTBALL_DATA_API_KEY в env — запускай это в Railway shell.")
    sys.exit(1)
​
req = urllib.request.Request(API, headers={
    "X-Auth-Token": KEY,
    "User-Agent": "WC2026-bot/1.0",
    "Accept": "application/json",
})
try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
except urllib.error.HTTPError as e:
    print(f"HTTP {e.code} {e.reason}")
    if e.code == 403:
        print("=> Ключ/тариф не даёт доступ к WC на free-tier.")
    elif e.code == 429:
        print("=> Rate limit (free 10 запр/мин). Подожди минуту.")
    sys.exit(1)
except Exception as e:
    print("Сеть недоступна:", repr(e))
    sys.exit(1)
​
ms = data.get("matches", [])
print(f"Всего матчей в фиде: {len(ms)}")
hits = 0
for m in ms:
    h = (m.get("homeTeam") or {}).get("name", "") or ""
    a = (m.get("awayTeam") or {}).get("name", "") or ""
    blob = (h + " " + a).lower()
    if any(w in blob for w in wants):
        st = m.get("status")
        sc = (m.get("score") or {}).get("fullTime") or {}
        print("-" * 50)
        print(f"{h} — {a}")
        print(f"  utcDate: {m.get('utcDate')}")
        print(f"  status : {st}")
        print(f"  счёт   : {sc.get('home')}:{sc.get('away')}")
        hits += 1
if not hits:
    print(f"Матч с [{', '.join(wants)}] НЕ НАЙДЕН.")
    print("Либо его нет в фиде, либо другое имя сборной — запусти без аргументов и сверь названия.")
​
