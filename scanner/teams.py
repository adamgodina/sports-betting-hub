"""Name normalization for both team and individual sports.

Team sports match through an alias table keyed on The Odds API's full names
(Kalshi ticker codes, Polymarket slugs, city short names all map back to it).
Individual sports have no fixed roster, so they match on the shape of the
player's name instead — see canonical_person().
"""
import re
import unicodedata

# canonical name -> list of aliases (codes, cities, kalshi sub_titles)
MLB_TEAMS = {
    "Arizona Diamondbacks": ["ari", "az", "arizona", "diamondbacks"],
    "Athletics": ["ath", "oak", "oakland", "athletics", "las vegas"],
    "Atlanta Braves": ["atl", "atlanta", "braves"],
    "Baltimore Orioles": ["bal", "baltimore", "orioles"],
    "Boston Red Sox": ["bos", "boston", "red sox"],
    "Chicago Cubs": ["chc", "chicago c", "cubs"],
    "Chicago White Sox": ["cws", "chw", "chicago ws", "chicago w", "white sox"],
    "Cincinnati Reds": ["cin", "cincinnati", "reds"],
    "Cleveland Guardians": ["cle", "cleveland", "guardians"],
    "Colorado Rockies": ["col", "colorado", "rockies"],
    "Detroit Tigers": ["det", "detroit", "tigers"],
    "Houston Astros": ["hou", "houston", "astros"],
    "Kansas City Royals": ["kc", "kcr", "kansas city", "royals"],
    "Los Angeles Angels": ["laa", "los angeles a", "angels"],
    "Los Angeles Dodgers": ["lad", "los angeles d", "dodgers"],
    "Miami Marlins": ["mia", "miami", "marlins"],
    "Milwaukee Brewers": ["mil", "milwaukee", "brewers"],
    "Minnesota Twins": ["min", "minnesota", "twins"],
    "New York Mets": ["nym", "new york m", "mets"],
    "New York Yankees": ["nyy", "new york y", "yankees"],
    "Philadelphia Phillies": ["phi", "philadelphia", "phillies"],
    "Pittsburgh Pirates": ["pit", "pittsburgh", "pirates"],
    "San Diego Padres": ["sd", "sdp", "san diego", "padres"],
    "San Francisco Giants": ["sf", "sfg", "san francisco", "giants"],
    "Seattle Mariners": ["sea", "seattle", "mariners"],
    "St. Louis Cardinals": ["stl", "st. louis", "st louis", "cardinals"],
    "Tampa Bay Rays": ["tb", "tbr", "tampa bay", "rays"],
    "Texas Rangers": ["tex", "texas", "rangers"],
    "Toronto Blue Jays": ["tor", "toronto", "blue jays"],
    "Washington Nationals": ["wsh", "was", "washington", "nationals"],
}


def build_alias_map(teams: dict) -> dict:
    """alias (lowercased) -> canonical name, including the canonical name itself."""
    out = {}
    for canonical, aliases in teams.items():
        out[canonical.lower()] = canonical
        for a in aliases:
            out[a.lower()] = canonical
    return out


def build_code_set(teams: dict) -> dict:
    """uppercase short code -> canonical, for parsing Kalshi ticker suffixes."""
    out = {}
    for canonical, aliases in teams.items():
        for a in aliases:
            if len(a) <= 3 and " " not in a:
                out[a.upper()] = canonical
    return out


def normalize(name: str, alias_map: dict):
    """Resolve a source's team string to a canonical name, or None."""
    if not name:
        return None
    return alias_map.get(name.strip().lower())


def canonical_person(name: str):
    """Canonical form of a player's name, for sports with no fixed roster.

    Team sports have a closed set of names, so they match through an alias
    table. Individual sports don't — the field changes every tournament — so
    names are matched by shape instead. Venues disagree on case and accents
    ("Botic van de Zandschulp" vs "Botic Van de Zandschulp"), so those are
    normalised away and the result is title-cased for display.
    """
    if not name:
        return None
    s = unicodedata.normalize("NFKD", str(name))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^A-Za-z' -]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return None
    return " ".join(w[:1].upper() + w[1:].lower() for w in s.split())


def make_resolver(sport_cfg):
    """One name resolver per sport, so every source agrees on identity.

    In "roster" mode The Odds API's own names ARE the canonical set, so this
    passes them through untouched; the exchanges are mapped onto them by
    roster.Roster instead.
    """
    mode = getattr(sport_cfg, "match_mode", "alias")
    if mode == "name":
        return canonical_person
    if mode == "roster":
        return lambda raw: (str(raw).strip() or None) if raw else None
    alias = build_alias_map(sport_cfg.teams)
    return lambda raw: normalize(raw, alias)
