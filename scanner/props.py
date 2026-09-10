"""1+ home run scanner.

A different shape from the moneyline scanner, because the two sides live on
different venues:

  * Sportsbooks quote **Over 0.5 home runs** (i.e. "1+ HR") and that is ALL
    they quote — there is no Under side to buy. Verified against the feed: a
    game returns Over outcomes at point 0.5 and zero Unders.

    Careful with the market key: the books split across two of them.
    `batter_home_runs` carries BetRivers ONLY, while DraftKings and FanDuel
    post the same line under `batter_home_runs_alternate`. Querying only the
    former makes it look like DK/FD do not offer home run props at all. Each
    key bills its own credit per game — see config.PROPS_HR_MARKETS.

    Books are limited to config.ODDS_API_BOOKMAKERS, the same list the
    moneyline scan uses, so every view shows the books actually bet at.
  * Kalshi's KXMLBHR series has a "<Player>: 1+ home runs?" market with BOTH a
    Yes and a No side.
  * Polymarket US has them too, as `baseball_player_home_runs` with line 1 —
    but ONLY via `/v1/events?slug=<game>`, which returns ~459 markets for a
    game. The `/v2/leagues/mlb/events` feed truncates to the 15 team markets
    and shows no props at all, which is easy to mistake for "no coverage".

So the trade this scanner is built for is: back 1+ HR at a book, hedge it by
buying **No** on Kalshi. Those are the two sides of one binary event, so the
usual arithmetic applies — if book_yes_prob + kalshi_no_prob < 1 it is an
arbitrage, and the equal-payout hedge size is stake / book_yes_prob contracts.

Cost: the sportsbook half comes from The Odds API's per-event endpoint, which
bills **1 credit per game** (the event list itself is free). A full slate is
therefore ~15 credits, versus 1 for a moneyline scan.
"""
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import requests

from . import config
from .sources.kalshi import _start_from_ticker

ODDS_HOST = "https://api.the-odds-api.com"
KALSHI_MARKETS = "https://api.elections.kalshi.com/trade-api/v2/markets"
HR_SERIES = "KXMLBHR"
HR_POINT = 0.5                      # the 1+ line
KALSHI_THETA = 0.07                 # taker fee coefficient


# A player's name alone is NOT a safe join key. The exchange feeds cover every
# open market, which can span a doubleheader or two dates at once, so the same
# name can legitimately appear more than once. Keying a flat dict by name means
# the last one silently wins and a row can end up hedged against a DIFFERENT
# GAME. Entries are therefore kept as lists and chosen by whose game start is
# closest, with anything outside this window refused rather than guessed.
PROP_MATCH_TOLERANCE = timedelta(minutes=90)


def _parse_iso(v):
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _pick_for_game(entries, when):
    """The entry belonging to the same game as `when`, or None.

    Refusing is the right answer when it cannot be established: a wrong hedge
    is far worse than a missing one, because it leaves the book leg naked while
    looking covered.
    """
    if not entries:
        return None
    dated = [e for e in entries if e.get("start")]
    if when is None or not dated:
        # Nothing to disambiguate with — only safe if there is no ambiguity.
        return entries[0] if len(entries) == 1 else None
    best = min(dated, key=lambda e: abs(e["start"] - when))
    return best if abs(best["start"] - when) <= PROP_MATCH_TOLERANCE else None


def norm_player(name: str) -> str:
    """Loose key for matching a player across venues."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z ]", "", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def american_to_prob(odds: float) -> float:
    return 100.0 / (odds + 100.0) if odds >= 100 else -odds / (-odds + 100.0)


def _kalshi_prob(price: float) -> float:
    """Fold Kalshi's taker fee into a price so it compares to book vig."""
    if config.INCLUDE_KALSHI_FEES:
        return price + KALSHI_THETA * price * (1.0 - price)
    return price


# ----------------------------------------------------------- Kalshi side

def fetch_kalshi_hr() -> dict:
    """normalised player -> {yes, no, tickers, event_ticker, start}. Free."""
    out, cursor = {}, None
    for _ in range(10):
        params = {"series_ticker": HR_SERIES, "status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        r = requests.get(KALSHI_MARKETS, params=params, timeout=30)
        r.raise_for_status()
        d = r.json()
        for m in d.get("markets", []):
            if not m["ticker"].endswith("-1"):      # "-1" == the 1+ line
                continue
            title = m.get("title") or ""
            player = title.split(":")[0].strip()
            key = norm_player(player)
            if not key:
                continue
            yes = float(m.get("yes_ask_dollars") or 0)
            no = float(m.get("no_ask_dollars") or 0)
            out.setdefault(key, []).append({
                "player": player,
                "ticker": m["ticker"],
                "event_ticker": m.get("event_ticker"),
                "start": _start_from_ticker(m.get("event_ticker", "")),
                "yes_ask": yes if 0 < yes < 1 else None,
                "no_ask": no if 0 < no < 1 else None,
                "exchange_index": m.get("exchange_index", -1),
            })
        cursor = d.get("cursor")
        if not cursor or not d.get("markets"):
            break
    return out


PM_GATEWAY = "https://gateway.polymarket.us"


def fetch_polymarket_hr(game_slugs=None) -> dict:
    """normalised player -> Polymarket US 1+ HR market. Free, no API key.

    NOTE: props are only exposed on /v1/events?slug=<game>; the leagues feed
    truncates to team markets.
    """
    out = {}
    with requests.Session() as session:
        if game_slugs is None:
            try:
                r = session.get(f"{PM_GATEWAY}/v2/leagues/mlb/events",
                                params={"limit": 100}, timeout=30)
                r.raise_for_status()
                game_slugs = [e["slug"] for e in r.json().get("events", [])
                              if e.get("slug")]
            except Exception as e:
                print(f"  [props/pm] league list failed: {e}")
                return out

        def one(slug):
            try:
                r = session.get(f"{PM_GATEWAY}/v1/events", params={"slug": slug},
                                timeout=30)
                r.raise_for_status()
                d = r.json()
                return (d.get("events") or [d])[0]
            except Exception:
                return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            for ev in pool.map(one, game_slugs):
                if not ev:
                    continue
                for m in ev.get("markets", []):
                    if m.get("sportsMarketType") != "baseball_player_home_runs":
                        continue
                    if m.get("line") != 1:          # only the 1+ line
                        continue
                    title = m.get("title") or ""
                    player = re.sub(r"\s*\d\+.*$", "", title).strip()
                    key = norm_player(player)
                    if not key:
                        continue
                    yes = no = None
                    for side in m.get("marketSides", []):
                        q = side.get("quote")
                        v = q.get("value") if isinstance(q, dict) else q
                        try:
                            v = float(v)
                        except (TypeError, ValueError):
                            continue
                        if not 0 < v < 1 or not side.get("tradable", True):
                            continue
                        if side.get("long"):
                            yes = v
                        else:
                            no = v
                    if yes is None and no is None:
                        continue
                    out.setdefault(key, []).append({
                        "player": player, "market_slug": m.get("slug"),
                        "yes_ask": yes, "no_ask": no,
                        "start": _parse_iso(m.get("gameStartTime")
                                            or ev.get("startTime")),
                        "fee": m.get("feeCoefficient") or 0.06})
    return out


# ----------------------------------------------------- sportsbook side

def list_events():
    """Upcoming MLB events. This endpoint is FREE (0 credits)."""
    r = requests.get(f"{ODDS_HOST}/v4/sports/baseball_mlb/events",
                     params={"apiKey": config.ODDS_API_KEY}, timeout=30)
    r.raise_for_status()
    return r.json()


_props_cache = {}      # event_id -> (fetched_at, best, meta)


def _event_props(session, event_id):
    """Best Over-0.5 price per player for one game.

    Costs 1 CREDIT unless this event was fetched within
    config.PROPS_CACHE_TTL_S, in which case the cached copy is reused for free.
    """
    hit = _props_cache.get(event_id)
    if hit and (time.time() - hit[0]) < config.PROPS_CACHE_TTL_S:
        meta = dict(hit[2] or {})
        meta["cached"] = True
        return hit[1], meta
    try:
        r = session.get(
            f"{ODDS_HOST}/v4/sports/baseball_mlb/events/{event_id}/odds",
            params={"apiKey": config.ODDS_API_KEY, "regions": "us",
                    "markets": ",".join(config.PROPS_HR_MARKETS),
                    # same books as every other scan — the ones actually bet at
                    "bookmakers": config.ODDS_API_BOOKMAKERS,
                    "oddsFormat": "american"}, timeout=30)
        r.raise_for_status()
        d = r.json()
    except Exception as e:
        print(f"  [props] event {event_id[:8]} failed: {e}")
        return {}, None
    best = {}
    for bk in d.get("bookmakers", []):
        for mk in bk.get("markets", []):
            if mk.get("key") not in config.PROPS_HR_MARKETS:
                continue
            for o in mk.get("outcomes", []):
                if o.get("name") != "Over" or o.get("point") != HR_POINT:
                    continue
                player = o.get("description") or ""
                key = norm_player(player)
                if not key:
                    continue
                prob = american_to_prob(float(o["price"]))
                cur = best.get(key)
                if cur is None or prob < cur["prob"]:
                    best[key] = {"player": player, "book": bk["key"],
                                 "american": f"{float(o['price']):+.0f}",
                                 "prob": prob}
    meta = {"away": d.get("away_team"), "home": d.get("home_team"),
            "start": d.get("commence_time"),
            "books": sorted({b["key"] for b in d.get("bookmakers", [])}),
            "cached": False,
            "remaining": r.headers.get("x-requests-remaining")}
    _props_cache[event_id] = (time.time(), best, meta)
    return best, meta


def scan(max_events: int = None, top_per_game: int = None):
    """Match book 1+ HR prices against Kalshi's Yes/No on the same player.

    `max_events` limits how many GAMES are covered — the only real lever on
    cost, since each game is 1 credit however many players it returns.
    `top_per_game` keeps just the N likeliest hitters per game (shortest book
    price). That is presentation only: the credit was already spent fetching
    the game, so it declutters rather than saves.
    """
    if top_per_game is None:
        top_per_game = config.PROPS_TOP_PER_GAME
    top_per_game = top_per_game or 0          # 0 / None = keep every player
    kalshi = fetch_kalshi_hr()
    try:
        pmarket = fetch_polymarket_hr()
    except Exception as e:
        print(f"  [props/pm] fetch failed: {e}")
        pmarket = {}
    events = list_events()
    if max_events:
        events = events[:max_events]

    rows, remaining, books_seen = [], None, set()
    with requests.Session() as session:
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda e: _event_props(session, e["id"]), events))

    billed = 0
    for ev, (book_best, meta) in zip(events, results):
        ev_start = _parse_iso(ev.get("commence_time"))
        if meta and meta.get("remaining"):
            remaining = meta["remaining"]
        if meta:
            books_seen.update(meta.get("books") or [])
            if not meta.get("cached"):
                billed += len(config.PROPS_HR_MARKETS)   # 1 credit per market
        # keep the likeliest hitters in this game (highest book probability)
        keep = sorted(book_best.items(), key=lambda kv: -kv[1]["prob"])
        game_rows = 0
        for key, b in keep:
            if top_per_game and game_rows >= top_per_game:
                break
            k = _pick_for_game(kalshi.get(key), ev_start)
            pm = _pick_for_game(pmarket.get(key), ev_start)
            k_no = k.get("no_ask") if k else None
            pm_no = pm.get("no_ask") if pm else None
            if not k_no and not pm_no:
                continue          # no hedge available -> not actionable
            game_rows += 1

            # effective cost of each venue's No side, fees folded in
            opts = []
            if k_no:
                opts.append(("kalshi", k_no, _kalshi_prob(k_no)))
            if pm_no:
                theta = pm.get("fee") or 0.06
                eff = pm_no + theta * pm_no * (1 - pm_no) \
                    if config.INCLUDE_POLYMARKET_US_FEES else pm_no
                opts.append(("polymarket_us", pm_no, eff))
            venue, raw_no, no_prob = min(opts, key=lambda o: o[2])
            total = b["prob"] + no_prob
            rows.append({
                "player": b["player"],
                "away": ev.get("away_team"), "home": ev.get("home_team"),
                "start": ev.get("commence_time"),
                "book": b["book"], "book_american": b["american"],
                "book_prob": round(b["prob"], 5),
                "kalshi_ticker": k["ticker"] if k else None,
                "kalshi_exchange_index": (k or {}).get("exchange_index", -1),
                "kalshi_yes": (k or {}).get("yes_ask"),
                "kalshi_no": k_no,
                "pm_slug": (pm or {}).get("market_slug"),
                "pm_yes": (pm or {}).get("yes_ask"),
                "pm_no": pm_no,
                "hedge_venue": venue,
                "hedge_no": raw_no,
                "no_prob": round(no_prob, 5),
                "total_prob": round(total, 5),
                "arb_roi": round((1.0 / total - 1.0) * 100, 3) if total < 1 else 0.0,
            })
    rows.sort(key=lambda r: r["total_prob"])
    return rows, {"remaining": int(remaining) if remaining else None,
                  "events_scanned": len(events),
                  "credits_spent": billed,
                  "cached_games": len(events) - billed,
                  "top_per_game": top_per_game,
                  "books_seen": sorted(books_seen)}


def _american(prob: float) -> str:
    d = 1.0 / prob
    return f"+{round((d - 1) * 100)}" if d >= 2 else f"-{round(100 / (d - 1))}"


def to_games(rows):
    """Reshape prop rows into the SAME structure the moneyline scan returns.

    A "1+ home runs" prop is just a two-outcome market, so it slots into the
    existing game shape and the whole UI — best-price highlighting, vig, the
    order ticket, the hedge calculator, fill confirmation — works unchanged:

        away = "<player> 1+ HR"   (the Yes side: book, Kalshi yes, PM yes)
        home = "<player> no HR"   (the No side: Kalshi no, PM no)
    """
    games = []
    for r in rows:
        # Short outcome labels: the player's name is the row title, so the
        # Team column reads "1+ HR" / "No HR" the way it reads a team name.
        yes_team, no_team = "1+ HR", "No HR"
        q = []

        def add(book, team, prob, meta=None, detail=""):
            if not prob or not 0 < prob < 1:
                return
            q.append({"book": book, "team": team, "prob": round(prob, 5),
                      "american": _american(prob), "detail": detail, "meta": meta})

        add(r["book"], yes_team, r["book_prob"], None, r["book_american"])

        if r.get("kalshi_ticker"):
            ki = r.get("kalshi_exchange_index", -1)
            if r.get("kalshi_yes"):
                add("kalshi", yes_team, _kalshi_prob(r["kalshi_yes"]),
                    {"ticker": r["kalshi_ticker"], "ask": r["kalshi_yes"],
                     "exchange_index": ki, "buy_no": False},
                    f"ask {r['kalshi_yes']:.2f}")
            if r.get("kalshi_no"):
                add("kalshi", no_team, _kalshi_prob(r["kalshi_no"]),
                    {"ticker": r["kalshi_ticker"], "ask": r["kalshi_no"],
                     "exchange_index": ki, "buy_no": True},
                    f"ask {r['kalshi_no']:.2f}")

        if r.get("pm_slug"):
            theta = 0.06
            eff = lambda v: (v + theta * v * (1 - v)
                             if config.INCLUDE_POLYMARKET_US_FEES else v)
            if r.get("pm_yes"):
                add("polymarket_us", yes_team, eff(r["pm_yes"]),
                    {"market_slug": r["pm_slug"], "ask": r["pm_yes"], "long": True},
                    f"ask {r['pm_yes']:.3f}")
            if r.get("pm_no"):
                add("polymarket_us", no_team, eff(r["pm_no"]),
                    {"market_slug": r["pm_slug"], "ask": r["pm_no"], "long": False},
                    f"ask {r['pm_no']:.3f}")

        games.append({"sport": "mlb-hr", "away": yes_team, "home": no_team,
                      "start": r["start"], "title": r["player"],
                      "matchup": f"{r['away']} @ {r['home']}",
                      "quotes": q})
    return games
