# -*- coding: utf-8 -*-
"""wc2026_names.py - EDINYY ISTOCHNIK PRAVDY dlya imyon komand.

Vse skripty (ingest, fix, audit) importiruyut canon() otsyuda, chtoby
napisanie iz football-data.org, elo.csv, squads.csv i bot.py svodilos
k odnomu klyuchu. Logika identichna _norm_team iz bot.py.

canon(name) -> normalizovannyy klyuch (nizhniy registr, bez diakritiki,
bez apostrofov/defisov/tochek), zatem alias k odnomu kanonu.
"""
import unicodedata

# 48 oficialnyh komand ChM-2026 (kak v _OFFICIAL_GROUPS v bot.py).
OFFICIAL = [
    "Mexico", "South Africa", "South Korea", "Czech Republic",
    "Canada", "Bosnia and Herzegovina", "Qatar", "Switzerland",
    "Brazil", "Morocco", "Haiti", "Scotland",
    "USA", "Paraguay", "Australia", "Turkey",
    "Germany", "Curacao", "Ivory Coast", "Ecuador",
    "Netherlands", "Japan", "Sweden", "Tunisia",
    "Belgium", "Egypt", "Iran", "New Zealand",
    "Spain", "Cape Verde", "Saudi Arabia", "Uruguay",
    "France", "Senegal", "Iraq", "Norway",
    "Argentina", "Algeria", "Austria", "Jordan",
    "Portugal", "DR Congo", "Uzbekistan", "Colombia",
    "England", "Croatia", "Ghana", "Panama",
]

# Alias-y: levaya chast - normalizovannyy variant (posle _strip),
# pravaya - normalizovannoe oficialnoe imya.
_ALIASES = {
    # Czechia / Czech Republic
    "czechia": "czech republic", "czech": "czech republic",
    # Korea
    "korea republic": "south korea", "republic of korea": "south korea",
    "korea": "south korea", "korea dpr": "south korea",
    # USA
    "united states": "usa", "united states of america": "usa",
    "united states men s national soccer team": "usa",
    # Ivory Coast
    "cote d ivoire": "ivory coast", "ivory coast": "ivory coast",
    # Cape Verde
    "cabo verde": "cape verde", "cape verde islands": "cape verde",
    # Iran
    "ir iran": "iran", "iran islamic republic": "iran",
    # Turkiye
    "turkiye": "turkey", "turkey": "turkey",
    # DR Congo
    "congo dr": "dr congo", "democratic republic of congo": "dr congo",
    "democratic republic of the congo": "dr congo", "dr congo": "dr congo",
    # Bosnia
    "bosnia": "bosnia and herzegovina",
    "bosnia herzegovina": "bosnia and herzegovina",
    "bosnia and herzegovina": "bosnia and herzegovina",
    # Netherlands
    "holland": "netherlands", "netherland": "netherlands",
    # Saudi
    "saudi": "saudi arabia",
    # Curacao (posle strip diakritika uzhe ubrana)
    "curacao": "curacao",
}


def _strip(name):
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", str(name))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower().replace("'", " ").replace("-", " ").replace(".", " ")
    s = " ".join(s.split())
    return s


def canon(name):
    """Edinyy klyuch dlya sopostavleniya komand."""
    s = _strip(name)
    return _ALIASES.get(s, s)


# Mnozhestvo kanonicheskih klyuchey vseh 48 oficialnyh komand.
OFFICIAL_CANON = set(canon(t) for t in OFFICIAL)


def is_known(name):
    """True esli imya svoditsya k odnoy iz 48 oficialnyh komand."""
    return canon(name) in OFFICIAL_CANON
