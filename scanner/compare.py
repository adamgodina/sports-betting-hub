"""Match games across sources and build the comparison report."""
from datetime import timedelta

from .models import MatchedGame

MATCH_TOLERANCE = timedelta(minutes=90)  # start-time tolerance (handles doubleheaders)


def _name_tokens(name):
    return [t for t in str(name).lower().replace("-", " ").split() if len(t) > 1]


def _same_entrant(a, b):
    """Two spellings of one competitor.

    Venues disagree about how MUCH of a name to print: the sportsbooks and
    Polymarket say "Norma Dumont", Kalshi says "Norma Dumont Viana", and exact
    equality drops that fight's exchange prices off the row entirely. So one
    name's words being contained in the other's counts as the same person.

    Deliberately not fuzzy beyond that — no edit distance, no nicknames. Two
    different fighters sharing a row is a worse failure than a missing price,
    and the caller only accepts a match when BOTH entrants agree AND the start
    times are within tolerance.

    BOTH names must carry at least two words, or the rule is far too weak: a
    single shared token is not a person. (A test caught "A Team" matching
    "C Team" — one-letter words are dropped as initials, leaving both as the
    single token "team".) A name printed as a bare surname falls back to
    needing an exact match, which is the safe direction to fail in.
    """
    ta, tb = set(_name_tokens(a)), set(_name_tokens(b))
    if len(ta) < 2 or len(tb) < 2:
        return False
    return ta <= tb or tb <= ta


def _entrants_match(spine_event, cand, loose):
    if cand.teams == spine_event.teams:
        return True
    if not loose:
        return False
    a, b = sorted(spine_event.teams), sorted(cand.teams)
    if len(a) != 2 or len(b) != 2:
        return False
    return ((_same_entrant(a[0], b[0]) and _same_entrant(a[1], b[1])) or
            (_same_entrant(a[0], b[1]) and _same_entrant(a[1], b[0])))


def _find_match(spine_event, candidates, tolerance=MATCH_TOLERANCE, loose=False):
    """Closest-in-time candidate with the same two teams, within tolerance.

    Closest-wins does the real work; the tolerance is only a sanity bound. It
    has to be generous outside baseball because Kalshi's occurrence_datetime
    runs 3h fast whenever its ticker has no clock time in it (NFL, NCAAF).

    `loose` allows one venue to print more of a competitor's name than another
    — see _same_entrant. Individual sports only.
    """
    same_teams = [c for c in candidates if _entrants_match(spine_event, c, loose)]
    if not same_teams:
        return None
    best = min(same_teams, key=lambda c: abs(c.start - spine_event.start))
    return best if abs(best.start - spine_event.start) <= tolerance else None


def _align_quotes(match, spine_event, draw_label):
    """Re-label a matched event's quotes with the SPINE's spelling.

    A row looks up its prices by outcome name, so a quote that still says
    "Norma Dumont Viana" while the row says "Norma Dumont" is a quote nobody
    can see. Matching on a looser rule than equality means renaming too.
    """
    out = []
    for q in match.quotes:
        if q.team != draw_label and q.team not in spine_event.teams:
            for name in spine_event.teams:
                if _same_entrant(q.team, name):
                    q.team = name
                    break
        out.append(q)
    return out


def _exchange_rows(sport_key, kalshi_events, polymarket_events,
                   used_k, used_p, tol, loose_names, draw_label):
    """Fixtures the sportsbooks don't price at all, but BOTH exchanges do.

    The Odds API is the spine everywhere else, and for good reason: it decides
    what a fixture is called and when it starts. But its tennis coverage is one
    key per tournament and it goes dark between them — so for weeks at a time
    it has nothing, while Kalshi and Polymarket are pricing a hundred matches
    each. Kalshi against Polymarket is already two venues; a row with both is
    as tradeable as any other, and dropping it because DraftKings never heard
    of the match is throwing away the whole board.

    Only pairs are kept. A row quoted by ONE venue can't be compared with
    anything, so it would be noise rather than an opportunity.
    """
    rows = []
    spare_p = [p for p in polymarket_events if id(p) not in used_p]
    taken = set()
    for k in kalshi_events:
        if id(k) in used_k:
            continue
        pool = [p for p in spare_p if id(p) not in taken]
        m = _find_match(k, pool, tol, loose=loose_names)
        if not m:
            continue
        taken.add(id(m))
        names = sorted(k.teams)          # no home or away in an individual sport
        if len(names) != 2:
            continue
        quotes = list(k.quotes) + (_align_quotes(m, k, draw_label)
                                   if loose_names else list(m.quotes))
        rows.append(MatchedGame(sport=sport_key, home=names[1], away=names[0],
                                start=k.start, quotes=quotes, outcomes=names))
    return rows


def match_games(sport_key, oddsapi_events, kalshi_events, polymarket_events,
                tolerance=None, three_way=False, draw_label="Draw",
                loose_names=False, exchange_only=False):
    """Use The Odds API events as the spine; attach Kalshi/Polymarket quotes."""
    tol = tolerance or MATCH_TOLERANCE
    games = []
    used_k, used_p = set(), set()
    for ev in oddsapi_events:
        # The draw sits between the two sides, the way a scoreboard reads it.
        outcomes = [ev.away, draw_label, ev.home] if three_way else [ev.away, ev.home]
        game = MatchedGame(sport=sport_key, home=ev.home, away=ev.away,
                           start=ev.start, quotes=list(ev.quotes),
                           outcomes=outcomes)
        for candidates, used in ((kalshi_events, used_k),
                                 (polymarket_events, used_p)):
            m = _find_match(ev, candidates, tol, loose=loose_names)
            if m:
                used.add(id(m))
                game.quotes.extend(_align_quotes(m, ev, draw_label)
                                   if loose_names else m.quotes)
        games.append(game)
    if exchange_only:
        games.extend(_exchange_rows(sport_key, kalshi_events, polymarket_events,
                                    used_k, used_p, tol, loose_names, draw_label))
    return sorted(games, key=lambda g: g.start)


def analyze(game: MatchedGame):
    """Best price per outcome and the combined implied-probability sum.

    sum < 1.0 means an arbitrage: backing EVERY outcome at the best books
    guarantees profit of (1/sum - 1) on total stake. Soccer has three of them
    rather than two, which is the only difference.
    """
    best = {t: game.best(t) for t in game.sides()}
    if any(b is None for b in best.values()):
        return None
    total = sum(b.prob for b in best.values())
    return {
        "best": best,
        "best_home": best.get(game.home),
        "best_away": best.get(game.away),
        "total_prob": total,
        "arb_roi": (1.0 / total - 1.0) if total < 1.0 else 0.0,
    }
