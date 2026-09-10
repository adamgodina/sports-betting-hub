"""Match games across sources and build the comparison report."""
from datetime import timedelta

from .models import MatchedGame

MATCH_TOLERANCE = timedelta(minutes=90)  # start-time tolerance (handles doubleheaders)


def _find_match(spine_event, candidates, tolerance=MATCH_TOLERANCE):
    """Closest-in-time candidate with the same two teams, within tolerance.

    Closest-wins does the real work; the tolerance is only a sanity bound. It
    has to be generous outside baseball because Kalshi's occurrence_datetime
    runs 3h fast whenever its ticker has no clock time in it (NFL, NCAAF).
    """
    same_teams = [c for c in candidates if c.teams == spine_event.teams]
    if not same_teams:
        return None
    best = min(same_teams, key=lambda c: abs(c.start - spine_event.start))
    return best if abs(best.start - spine_event.start) <= tolerance else None


def match_games(sport_key, oddsapi_events, kalshi_events, polymarket_events,
                tolerance=None):
    """Use The Odds API events as the spine; attach Kalshi/Polymarket quotes."""
    tol = tolerance or MATCH_TOLERANCE
    games = []
    for ev in oddsapi_events:
        game = MatchedGame(sport=sport_key, home=ev.home, away=ev.away,
                           start=ev.start, quotes=list(ev.quotes))
        for candidates in (kalshi_events, polymarket_events):
            m = _find_match(ev, candidates, tol)
            if m:
                game.quotes.extend(m.quotes)
        games.append(game)
    return sorted(games, key=lambda g: g.start)


def analyze(game: MatchedGame):
    """Best price per side and the combined implied-probability sum.

    sum < 1.0 means an arbitrage: backing both sides at the best books
    guarantees profit of (1/sum - 1) on total stake.
    """
    best_home = game.best(game.home)
    best_away = game.best(game.away)
    if not best_home or not best_away:
        return None
    total = best_home.prob + best_away.prob
    return {
        "best_home": best_home,
        "best_away": best_away,
        "total_prob": total,
        "arb_roi": (1.0 / total - 1.0) if total < 1.0 else 0.0,
    }
