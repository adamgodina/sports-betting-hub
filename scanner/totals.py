"""Game totals — over/under on the points scored in a game.

The shape is different from a moneyline in one way that matters: a total has a
LINE, and the venues do not all post the same one. A row here is therefore a
(game, line) pair, with Over and Under as its two outcomes — "Over 47.5" is
only comparable with another venue's 47.5, never with 47.0.

Where the prices come from:

  * sportsbooks — The Odds API `totals` market, ONE credit for the whole sport
    (not per game, the way player props are billed). Each book posts its own
    main line, so a game usually shows one to three distinct lines across the
    eight books.
  * Kalshi — a totals series per sport (KXNFLTOTAL, KXMLBTOTAL), one market
    per line ("Over 45.5 points scored", "Over 5.5 runs scored"). YES is the
    over; NO on that same market is the under, so both sides of a row trade on
    one ticker.
  * Polymarket US — `{sport}_team_full_game_total`, one market per line, with
    Over (long) and Under (short) on the same slug.

Baseball plays the same pair twice in a day, so a venue's entries are kept per
pair as a LIST and matched by closest start time — putting the nightcap's
lines on the first game would read as a free arbitrage.

Only lines at least one sportsbook posts become rows. The exchanges list far
more, but a line no book quotes is a row with nothing to hedge against, and
three dozen of them per game would bury the ones you can act on.
"""
import re
import time
from datetime import datetime, timedelta, timezone

import requests

from . import config
from .roster import Roster
from .sources.kalshi import _start_from_ticker
from .sources.polymarket_us import _nocache

ODDS_HOST = "https://api.the-odds-api.com"
KALSHI_EVENTS = "https://api.elections.kalshi.com/trade-api/v2/events"
PM_GATEWAY = "https://gateway.polymarket.us"
KALSHI_THETA = 0.07


def _parse_iso(v):
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def american_to_prob(odds: float) -> float:
    return 100.0 / (odds + 100.0) if odds >= 100 else -odds / (-odds + 100.0)


def _american(prob: float) -> str:
    d = 1.0 / prob
    return f"+{round((d - 1) * 100)}" if d >= 2 else f"-{round(100 / (d - 1))}"


def _kalshi_prob(price: float) -> float:
    if config.INCLUDE_KALSHI_FEES:
        return price + KALSHI_THETA * price * (1.0 - price)
    return price


def _pm_prob(price: float, theta: float) -> float:
    if config.INCLUDE_POLYMARKET_US_FEES:
        return price + theta * price * (1.0 - price)
    return price


def _f(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if 0 < v < 1 else None


# ------------------------------------------------------------ sportsbooks

def fetch_books(tc):
    """[{away, home, start, lines: {line: {book: (over_prob, under_prob)}}}].

    ONE Odds API credit for the whole sport.
    """
    r = requests.get(f"{ODDS_HOST}/v4/sports/{tc.odds_api_sport}/odds",
                     params={"apiKey": config.ODDS_API_KEY, "regions": "us",
                             "markets": "totals",
                             "bookmakers": config.ODDS_API_BOOKMAKERS,
                             "oddsFormat": "american"}, timeout=30)
    r.raise_for_status()
    quota = {"remaining": r.headers.get("x-requests-remaining"),
             "used": r.headers.get("x-requests-used")}
    cutoff = datetime.now(timezone.utc) + timedelta(hours=tc.horizon_hours)
    games = []
    for ev in r.json():
        start = _parse_iso(ev.get("commence_time"))
        if start is None or start > cutoff:
            continue
        lines = {}
        for bk in ev.get("bookmakers", []):
            for mkt in bk.get("markets", []):
                if mkt.get("key") != "totals":
                    continue
                for o in mkt.get("outcomes", []):
                    point = o.get("point")
                    side = str(o.get("name", "")).strip().lower()
                    if point is None or side not in ("over", "under"):
                        continue
                    try:
                        prob = american_to_prob(float(o["price"]))
                    except (KeyError, TypeError, ValueError):
                        continue
                    entry = lines.setdefault(float(point), {}).setdefault(bk["key"], {})
                    entry[side] = prob
        if lines:
            games.append({"away": ev.get("away_team"), "home": ev.get("home_team"),
                          "start": start, "lines": lines})
    return games, quota


# ------------------------------------------------------------------ Kalshi

_TITLE_SPLIT = re.compile(r"\s+vs\.?\s+", re.I)


def fetch_kalshi(tc, roster):
    """(away, home) -> {line: {"ticker", "over", "under", "exchange_index"}}.

    Free. Kalshi names the two sides in the event title the way it does
    everywhere else ("New York G vs Los Angeles R: Total Points"), so the
    roster resolves them together.
    """
    out, cursor = {}, None
    for _ in range(10):
        params = {"series_ticker": tc.kalshi_series, "status": "open",
                  "with_nested_markets": "true", "limit": 200, **_nocache()}
        if cursor:
            params["cursor"] = cursor
        try:
            r = requests.get(KALSHI_EVENTS, params=params, timeout=30)
            r.raise_for_status()
            d = r.json()
        except Exception as e:
            print(f"  [totals/kalshi] fetch failed: {e}")
            break
        for ev in d.get("events", []):
            title = (ev.get("title") or "").split(":")[0]
            parts = _TITLE_SPLIT.split(title)
            if len(parts) != 2:
                continue
            a, b = roster.resolve_pair(parts[0].strip(), parts[1].strip())
            if not a or not b:
                continue
            start = _start_from_ticker(ev.get("event_ticker", ""))
            by_line = {}
            for m in ev.get("markets", []):
                line = m.get("floor_strike")
                if line is None:
                    continue
                by_line[float(line)] = {
                    "ticker": m["ticker"],
                    "over": _f(m.get("yes_ask_dollars")),
                    "under": _f(m.get("no_ask_dollars")),
                    "exchange_index": m.get("exchange_index", -1),
                }
            if by_line:
                # A pair can appear twice in a day (doubleheaders), so entries
                # are kept as a list and chosen by start time below.
                out.setdefault(frozenset((a, b)), []).append(
                    {"lines": by_line, "start": start})
        cursor = d.get("cursor")
        if not cursor or not d.get("events"):
            break
    return out


# -------------------------------------------------------------- Polymarket

def fetch_pm(tc, roster):
    """(away, home) -> {line: {"market_slug", "over", "under", "fee"}}. Free."""
    if not tc.pm_league:
        return {}
    out = {}
    try:
        r = requests.get(f"{PM_GATEWAY}/v2/leagues/{tc.pm_league}/events",
                         params={"limit": 100, **_nocache()}, timeout=30)
        r.raise_for_status()
        events = r.json().get("events", [])
    except Exception as e:
        print(f"  [totals/pm] fetch failed: {e}")
        return {}
    for ev in events:
        if ev.get("ended"):
            continue
        names = [t.get("name") for t in (ev.get("teams") or []) if t.get("name")]
        if len(names) != 2:
            # fall back to the winner market, which names both sides
            for m in ev.get("markets", []):
                if m.get("sportsMarketType", "").endswith("full_game_winner"):
                    names = [(sd.get("team") or {}).get("name")
                             for sd in m.get("marketSides", [])]
                    break
        names = [n for n in names if n]
        if len(names) != 2:
            continue
        a, b = roster.resolve_pair(names[0], names[1])
        if not a or not b:
            continue
        by_line = {}
        for m in ev.get("markets", []):
            if m.get("sportsMarketType") != tc.pm_market_type or m.get("closed"):
                continue
            line = m.get("line")
            if line is None or not m.get("slug"):
                continue
            over = under = None
            for sd in m.get("marketSides", []):
                q = sd.get("quote")
                v = _f(q.get("value") if isinstance(q, dict) else q)
                if v is None or not sd.get("tradable", True):
                    continue
                if sd.get("long"):
                    over = v
                else:
                    under = v
            if over or under:
                fee = m.get("feeCoefficient")
                by_line[float(line)] = {
                    "market_slug": m["slug"], "over": over, "under": under,
                    "fee": float(fee) if fee is not None else 0.06,
                }
        if by_line:
            out.setdefault(frozenset((a, b)), []).append(
                {"lines": by_line, "start": _parse_iso(ev.get("startTime"))})
    return out


# ------------------------------------------------------------------- scan

def scan(tc):
    """Rows of (game, line) with every venue's over and under on it."""
    games, quota = fetch_books(tc)

    class _E:                       # the spine, for the roster
        def __init__(self, g):
            self.teams = frozenset({g["away"], g["home"]})
            self.start = g["start"]
    roster = Roster([_E(g) for g in games])

    kal = fetch_kalshi(tc, roster)
    pm = fetch_pm(tc, roster)
    tol = timedelta(hours=tc.match_tolerance_hours)

    def venue_for(store, g):
        """That game's lines at one venue, or {}.

        Baseball plays the same pair twice in a day, so the pair alone is not
        an identity: the entry whose start is closest wins, and anything
        outside the tolerance is refused rather than guessed — putting game
        two's lines on game one would look like a free arbitrage.
        """
        entries = store.get(frozenset({g["away"], g["home"]})) or []
        if not entries:
            return {}
        dated = [e for e in entries if e.get("start") and g.get("start")]
        if not dated:
            return entries[0]["lines"] if len(entries) == 1 else {}
        best = min(dated, key=lambda e: abs(e["start"] - g["start"]))
        return best["lines"] if abs(best["start"] - g["start"]) <= tol else {}

    rows, books_seen = [], set()
    for g in games:
        k_lines = venue_for(kal, g)
        p_lines = venue_for(pm, g)
        for line, by_book in sorted(g["lines"].items()):
            quotes = []

            def add(book, side, prob, meta=None, detail=""):
                if not prob or not 0 < prob < 1:
                    return
                quotes.append({"book": book, "team": side,
                               "prob": round(prob, 5), "american": _american(prob),
                               "detail": detail, "meta": meta})

            over_label = f"{tc.over_label} {line:g}"
            under_label = f"{tc.under_label} {line:g}"
            for book, sides in by_book.items():
                books_seen.add(book)
                add(book, over_label, sides.get("over"))
                add(book, under_label, sides.get("under"))

            k = k_lines.get(line)
            if k:
                idx = k.get("exchange_index", -1)
                if k.get("over"):
                    add("kalshi", over_label, _kalshi_prob(k["over"]),
                        {"ticker": k["ticker"], "ask": k["over"],
                         "exchange_index": idx, "buy_no": False,
                         "theta": KALSHI_THETA if config.INCLUDE_KALSHI_FEES else 0.0},
                        f"ask {k['over']:.2f}")
                if k.get("under"):
                    # the under is the NO side of that same "Over X" market
                    add("kalshi", under_label, _kalshi_prob(k["under"]),
                        {"ticker": k["ticker"], "ask": k["under"],
                         "exchange_index": idx, "buy_no": True,
                         "theta": KALSHI_THETA if config.INCLUDE_KALSHI_FEES else 0.0},
                        f"ask {k['under']:.2f}")

            p = p_lines.get(line)
            if p:
                theta = p.get("fee") or 0.06
                if p.get("over"):
                    add("polymarket_us", over_label, _pm_prob(p["over"], theta),
                        {"market_slug": p["market_slug"], "ask": p["over"],
                         "long": True,
                         "theta": theta if config.INCLUDE_POLYMARKET_US_FEES else 0.0}, f"ask {p['over']:.3f}")
                if p.get("under"):
                    add("polymarket_us", under_label, _pm_prob(p["under"], theta),
                        {"market_slug": p["market_slug"], "ask": p["under"],
                         "long": False,
                         "theta": theta if config.INCLUDE_POLYMARKET_US_FEES else 0.0}, f"ask {p['under']:.3f}")

            if len(quotes) < 2:
                continue
            best_over = min((q["prob"] for q in quotes if q["team"] == over_label),
                            default=None)
            best_under = min((q["prob"] for q in quotes if q["team"] == under_label),
                             default=None)
            total = (best_over + best_under) if (best_over and best_under) else None
            rows.append({
                "away": g["away"], "home": g["home"], "start": g["start"],
                "line": line, "over": over_label, "under": under_label,
                "quotes": quotes,
                "total_prob": round(total, 5) if total else None,
                "venues": sorted({q["book"] for q in quotes}),
            })

    rows.sort(key=lambda r: (r["total_prob"] is None,
                             r["total_prob"] or 9, r["start"]))
    return rows, {"remaining": int(quota["remaining"]) if quota.get("remaining") else None,
                  "events_scanned": len(games),
                  "credits_spent": 1,          # one call covers the whole sport
                  "cached_games": 0,
                  "top_per_game": 0,
                  "lines": len(rows),
                  "books_seen": sorted(books_seen)}


def to_games(rows, tc):
    """Reshape into the SAME payload the moneyline board renders.

    away/home are the two outcomes ("Over 47.5" / "Under 47.5"), the title is
    the fixture and the line rides in `matchup`, so the row reads
    "NY Giants @ LA Rams · Total 47.5" with an Over and an Under row.
    """
    return [{
        "sport": tc.sport_tag,
        "away": r["over"], "home": r["under"],
        "outcomes": [r["over"], r["under"]],
        "start": r["start"].astimezone(timezone.utc).isoformat(),
        "title": f"{r['away']} @ {r['home']}",
        "matchup": f"Total {r['line']:g}",
        "quotes": r["quotes"],
    } for r in rows]
