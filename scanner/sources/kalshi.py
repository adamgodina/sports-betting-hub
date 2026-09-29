"""Kalshi game-winner markets via their free public market-data API.

No auth or credits needed for read-only market data. Each game is one event
(e.g. KXMLBGAME-26SEP111420PITCHC) with one binary market per team; buying
YES on a team at the best ask is equivalent to backing that team.

Prices are the **top of book** (BBO) — the only realistically fillable price —
and are taken from the order book so we also learn the size available there.
Kalshi's book returns bids only: a NO bid at price p is the same as a YES ask
at 1-p, so best YES ask = 1 - (best NO bid), with that level's size as depth.
"""
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import time

import requests

from .. import config

from ..models import Quote, SourceEvent
from ..teams import build_code_set, make_resolver


def _nocache():
    """A throwaway query param so the CDN can't hand back a stored copy.

    Both exchanges put their public price endpoints behind a CDN cache —
    Kalshi's /markets for 15s, Polymarket's /v1/markets, /book and /bbo for
    30s — so a poll every second mostly got the same stale answer back. A
    unique param is a cache miss every time (verified on both).
    """
    return {"_": str(time.time_ns())}

API = "https://api.elections.kalshi.com/trade-api/v2/markets"

# e.g. KXMLBGAME-26SEP081905COLNYY -> 2026 Sep 08 19:05 ET. The API's
# occurrence_datetime field is unreliable (observed 3h off), so the game
# start is parsed from the event ticker, which encodes it in Eastern time.
_TICKER_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})[A-Z]")
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
_ET = ZoneInfo("America/New_York")

def _horizon(sport_cfg):
    """How far ahead this sport lists fixtures."""
    return getattr(sport_cfg, "horizon_hours", 0) or config.GAME_HORIZON_HOURS


def _start_from_ticker(event_ticker: str):
    m = _TICKER_RE.search(event_ticker)
    if not m:
        return None
    yy, mon, dd, hh, mm = m.groups()
    month = _MONTHS.get(mon)
    if not month:
        return None
    try:
        local = datetime(2000 + int(yy), month, int(dd), int(hh), int(mm), tzinfo=_ET)
    except ValueError:
        return None
    return local.astimezone(timezone.utc)


KALSHI_THETA = 0.07          # taker fee coefficient: theta * p * (1-p)


def _effective_prob(price: float) -> float:
    """Fold Kalshi's taker fee (~0.07*p*(1-p) per contract) into the price."""
    if config.INCLUDE_KALSHI_FEES:
        return price + KALSHI_THETA * price * (1.0 - price)
    return price


ORDERBOOKS = "https://api.elections.kalshi.com/trade-api/v2/markets/orderbooks"


def _ladder(levels, flip):
    """[[cost, size], ...] cheapest first for one side of a Kalshi book.

    Kalshi quotes both sides as BIDS, so the ask for the side you are buying is
    1 - (the other side's bid): YES asks come from the NO bids and vice versa.
    """
    out = []
    for lvl in levels or []:
        try:
            px, qty = float(lvl[0]), float(lvl[1])
        except (TypeError, ValueError, IndexError):
            continue
        if qty > 0:
            # Kalshi trades fractional contracts (0.01 granularity), so a
            # level's size is NOT an integer — int() turned 0.4 into a level
            # that looks empty.
            out.append([round(1.0 - px, 4) if flip else round(px, 4),
                        round(qty, 2)])
    out.sort(key=lambda l: l[0])
    return out


def fetch_books(tickers: list[str], sweep_cents: float = None) -> dict:
    """ticker -> {ask, depth, sweep, levels, no_*} from one batched book read.

    BOTH sides come back. A Kalshi market is one ticker with two tradeable
    sides, and the prop and totals boards buy the NO side of it (the under, the
    "no home run"), so pricing those off the YES ladder priced the wrong side
    of the book entirely — and cheaply enough to invent arbitrage.

    `levels` is the YES-ask ladder, `no_levels` the NO-ask ladder, each
    [[cost, size], ...] cheapest first. That is what makes a real price
    possible: 3 contracts at the touch and 4,000 a cent behind it is not the
    same market as 4,000 at the touch.
    """
    if sweep_cents is None:
        sweep_cents = config.HEDGE_MAX_SLIPPAGE_CENTS
    slip = sweep_cents / 100.0
    out = {}
    for i in range(0, len(tickers), 20):          # batch cap
        chunk = tickers[i:i + 20]
        try:
            r = requests.get(ORDERBOOKS,
                             params=[("tickers", t) for t in chunk],
                             timeout=30)
            r.raise_for_status()
            books = r.json().get("orderbooks", [])
        except Exception as e:
            print(f"  [kalshi] orderbook batch failed: {e}")
            continue
        for ob in books:
            fp = ob.get("orderbook_fp") or {}
            yes_ask = _ladder(fp.get("no_dollars"), flip=True)    # NO bids
            no_ask = _ladder(fp.get("yes_dollars"), flip=True)    # YES bids
            if not yes_ask and not no_ask:
                continue
            entry = {}
            if yes_ask:
                entry.update({
                    "ask": yes_ask[0][0], "depth": yes_ask[0][1],
                    "sweep": int(sum(q for px, q in yes_ask
                                     if px <= yes_ask[0][0] + slip + 1e-9)),
                    "levels": yes_ask})
            if no_ask:
                entry.update({
                    "no_ask": no_ask[0][0], "no_depth": no_ask[0][1],
                    "no_sweep": int(sum(q for px, q in no_ask
                                        if px <= no_ask[0][0] + slip + 1e-9)),
                    "no_levels": no_ask})
            out[ob.get("ticker")] = entry
    return out


def _is_draw(market) -> bool:
    """Kalshi prices soccer's draw as its own market in the same event."""
    sub = (market.get("yes_sub_title") or "").strip().lower()
    return market["ticker"].rsplit("-", 1)[-1] == "TIE" or sub in ("tie", "draw")


def fetch(sport_cfg, with_depth: bool = True, roster=None) -> list[SourceEvent]:
    """with_depth=False skips the order-book calls: 1 request instead of ~4,
    prices only (the markets feed's yes_ask already IS the top of book — this
    was verified against the book: yes_ask == 1 - best NO bid on every market).
    Used by the 1/second exchange poll."""
    mode = getattr(sport_cfg, "match_mode", "alias")
    by_name, by_roster = mode == "name", mode == "roster"
    resolve = make_resolver(sport_cfg)
    code_map = {} if (by_name or by_roster) else build_code_set(sport_cfg.teams)
    horizon = datetime.now(timezone.utc) + timedelta(hours=_horizon(sport_cfg))

    # A sport may sit on more than one series — tennis is ATP and WTA, which
    # are separate series here but one board to look at.
    markets = []
    for series in config.feeds(sport_cfg.kalshi_series):
        cursor = None
        for _ in range(20):  # pagination safety cap
            params = {"series_ticker": series, "status": "open", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            r = requests.get(API, params={**params, **_nocache()}, timeout=30)
            r.raise_for_status()
            d = r.json()
            markets.extend(d.get("markets", []))
            cursor = d.get("cursor")
            if not cursor or not d.get("markets"):
                break

    by_event, kept = {}, []
    for m in markets:
        start = _start_from_ticker(m.get("event_ticker", ""))
        if start is None:
            try:
                start = datetime.fromisoformat(m["occurrence_datetime"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
        if start > horizon:
            continue
        if getattr(sport_cfg, "three_way", False) and _is_draw(m):
            team = config.DRAW
        elif by_name:
            # individual sports: the market carries the player's own name
            team = resolve(m.get("yes_sub_title") or m.get("title", "").replace(" wins", ""))
        elif by_roster:
            team = None            # filled in below, per fixture
        else:
            team = code_map.get(m["ticker"].rsplit("-", 1)[-1])
        if team is None and not by_roster:
            print(f"  [kalshi] unmapped entrant in {m['ticker']}")
            continue
        kept.append((m, team, start))

    if by_roster and roster is not None:
        # Kalshi abbreviates from the front ("New York G"); resolve the two
        # sides of each fixture together so near-identical names separate.
        per_event = {}
        for i, (m, t, _) in enumerate(kept):
            if t is None:              # the draw is already named
                per_event.setdefault(m.get("event_ticker"), []).append(i)
        resolved = {}
        for idxs in per_event.values():
            raws = [kept[i][0].get("yes_sub_title") for i in idxs]
            if len(idxs) == 2:
                a, b = roster.resolve_pair(raws[0], raws[1])
                resolved[idxs[0]], resolved[idxs[1]] = a, b
            else:
                for i, r in zip(idxs, raws):
                    resolved[i] = roster.resolve(r)
        kept = [(m, (t if t is not None else resolved.get(i)), st)
                for i, (m, t, st) in enumerate(kept)
                if t is not None or resolved.get(i)]

    # top of book (with size) for every market we kept
    books = fetch_books([m["ticker"] for m, _, _ in kept]) if with_depth else {}

    for m, team, start in kept:
        book = books.get(m["ticker"])
        if book:
            yes_ask, depth = book["ask"], book["depth"]
        elif with_depth:
            # no resting NO bids -> fall back to the feed's quoted ask, size unknown
            yes_ask, depth = float(m.get("yes_ask_dollars") or 0), 0
        else:
            # fast poll: feed ask is the BBO; depth None = "unchanged/unknown"
            yes_ask, depth = float(m.get("yes_ask_dollars") or 0), None
        yes_bid = float(m.get("yes_bid_dollars") or 0)
        if not 0 < yes_ask < 1:
            continue
        ev = by_event.setdefault(m["event_ticker"], SourceEvent(
            source="kalshi", teams=frozenset(), start=start))
        # the draw is an outcome, not a team — it must not join the pair the
        # fixture is matched on
        if team != config.DRAW:
            ev.teams = ev.teams | {team}
        ev.quotes.append(Quote(
            book="kalshi", team=team,
            prob=_effective_prob(yes_ask),
            detail=f"BBO {yes_ask:.2f}" + (f" x{depth}" if depth else ""),
            meta={"ticker": m["ticker"], "ask": yes_ask, "bid": yes_bid,
                  "theta": KALSHI_THETA if config.INCLUDE_KALSHI_FEES else 0.0,
                  "depth": depth,
                  "sweep": (book or {}).get("sweep"),
                  "levels": (book or {}).get("levels"),
                  # matching shard for this market (MLB is 3); -1 auto-routes
                  "exchange_index": m.get("exchange_index", -1)},
        ))
    return [ev for ev in by_event.values() if len(ev.teams) == 2]
