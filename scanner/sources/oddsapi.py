"""DraftKings/FanDuel moneyline odds via The Odds API.

This is the only source that consumes paid credits: 1 credit per sport per
scan (h2h market, <=10 named bookmakers). Quota headers from each response
are returned so the loop can pace itself.
"""
from datetime import datetime, timedelta, timezone

import requests

from .. import config

from ..models import Quote, SourceEvent, american_to_prob
from ..teams import make_resolver

API_HOST = "https://api.the-odds-api.com"

def _horizon(sport_cfg):
    """How far ahead this sport lists fixtures."""
    return getattr(sport_cfg, "horizon_hours", 0) or config.GAME_HORIZON_HOURS


def active_sport_keys(sport_cfg) -> list[str]:
    """Odds API sport keys for this sport.

    Team leagues are a single fixed key. Tennis is per tournament
    (tennis_atp_us_open, ...), and those come and go, so a prefix is resolved
    against the live sports list — that endpoint is free (0 credits).
    """
    if not sport_cfg.odds_api_sport_prefix:
        return [sport_cfg.odds_api_sport]
    try:
        r = requests.get(f"{API_HOST}/v4/sports",
                         params={"apiKey": config.ODDS_API_KEY}, timeout=30)
        r.raise_for_status()
        return [s["key"] for s in r.json()
                if s.get("active") and s["key"].startswith(sport_cfg.odds_api_sport_prefix)]
    except Exception as e:
        print(f"  [oddsapi] sport list failed: {e}")
        return []


def fetch(sport_cfg) -> tuple[list[SourceEvent], dict]:
    """Returns (events, quota). One credit per sport key fetched."""
    events, quota = [], {}
    for key in active_sport_keys(sport_cfg):
        evs, quota = _fetch_one(sport_cfg, key)
        events.extend(evs)
    return events, quota


def _fetch_one(sport_cfg, sport_key) -> tuple[list[SourceEvent], dict]:
    url = f"{API_HOST}/v4/sports/{sport_key}/odds"
    r = requests.get(url, params={
        "apiKey": config.ODDS_API_KEY,
        "regions": "us",
        "markets": "h2h",
        "bookmakers": config.ODDS_API_BOOKMAKERS,
        "oddsFormat": "american",
    }, timeout=30)
    r.raise_for_status()

    quota = {}
    for name, header in (("remaining", "x-requests-remaining"), ("used", "x-requests-used")):
        try:
            quota[name] = int(float(r.headers.get(header, "")))
        except ValueError:
            quota[name] = None

    resolve = make_resolver(sport_cfg)
    # The Odds API returns a whole season for some sports (270 NFL games), but
    # the exchanges only list near-term fixtures, so anything past the horizon
    # is unhedgeable noise on the board.
    horizon = datetime.now(timezone.utc) + timedelta(hours=_horizon(sport_cfg))
    events = []
    for ev in r.json():
        home = resolve(ev.get("home_team"))
        away = resolve(ev.get("away_team"))
        if not home or not away:
            if getattr(sport_cfg, "match_mode", "alias") == "alias":
                print(f"  [oddsapi] unmapped: {ev.get('away_team')} @ {ev.get('home_team')}")
            continue
        start = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        if start > horizon:
            continue
        se = SourceEvent(source="oddsapi", teams=frozenset({home, away}), start=start)
        se.home, se.away = home, away
        for bk in ev.get("bookmakers", []):
            book = bk["key"]
            for mkt in bk.get("markets", []):
                if mkt["key"] != "h2h":
                    continue
                for oc in mkt.get("outcomes", []):
                    team = resolve(oc["name"])
                    if not team:
                        continue
                    price = float(oc["price"])
                    se.quotes.append(Quote(
                        book=book, team=team,
                        prob=american_to_prob(price),
                        detail=f"{price:+.0f}",
                    ))
        events.append(se)
    return events, quota
