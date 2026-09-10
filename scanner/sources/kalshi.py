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

import requests

from .. import config

from ..models import Quote, SourceEvent
from ..teams import build_code_set, make_resolver

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


def _effective_prob(price: float) -> float:
    """Fold Kalshi's taker fee (~0.07*p*(1-p) per contract) into the price."""
    if config.INCLUDE_KALSHI_FEES:
        return price + 0.07 * price * (1.0 - price)
    return price


ORDERBOOKS = "https://api.elections.kalshi.com/trade-api/v2/markets/orderbooks"


def fetch_books(tickers: list[str], sweep_cents: float = None) -> dict:
    """ticker -> (yes_ask, ask_depth, sweep_qty) from one batched book read.

    sweep_qty is the total size obtainable at or below (ask + sweep_cents) —
    what a hedger actually needs to know, since an aggressive order eats
    several levels, not just the touch.
    """
    if sweep_cents is None:
        sweep_cents = config.HEDGE_MAX_SLIPPAGE_CENTS
    out = {}
    for i in range(0, len(tickers), 20):          # batch cap
        chunk = tickers[i:i + 20]
        try:
            r = requests.get(ORDERBOOKS,
                             params=[("tickers", t) for t in chunk], timeout=30)
            r.raise_for_status()
            books = r.json().get("orderbooks", [])
        except Exception as e:
            print(f"  [kalshi] orderbook batch failed: {e}")
            continue
        for ob in books:
            fp = ob.get("orderbook_fp") or {}
            no_levels = fp.get("no_dollars") or []
            best = None
            for lvl in no_levels:
                try:
                    px, qty = float(lvl[0]), float(lvl[1])
                except (TypeError, ValueError, IndexError):
                    continue
                if best is None or px > best[0]:
                    best = (px, qty)
            if best:
                # a NO bid at px is a YES ask at 1-px
                ask = round(1.0 - best[0], 4)
                cap = ask + sweep_cents / 100.0
                sweep = 0.0
                for lvl in no_levels:
                    try:
                        px, qty = float(lvl[0]), float(lvl[1])
                    except (TypeError, ValueError, IndexError):
                        continue
                    if round(1.0 - px, 4) <= cap + 1e-9:
                        sweep += qty
                out[ob.get("ticker")] = (ask, int(best[1]), int(sweep))
    return out


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

    markets, cursor = [], None
    for _ in range(20):  # pagination safety cap
        params = {"series_ticker": sport_cfg.kalshi_series, "status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        r = requests.get(API, params=params, timeout=30)
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
        if by_name:
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
        for i, (m, _, _) in enumerate(kept):
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
        kept = [(m, resolved.get(i), st) for i, (m, _, st) in enumerate(kept)
                if resolved.get(i)]

    # top of book (with size) for every market we kept
    books = fetch_books([m["ticker"] for m, _, _ in kept]) if with_depth else {}

    for m, team, start in kept:
        book = books.get(m["ticker"])
        if book:
            yes_ask, depth = book[0], book[1]
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
        ev.teams = ev.teams | {team}
        ev.quotes.append(Quote(
            book="kalshi", team=team,
            prob=_effective_prob(yes_ask),
            detail=f"BBO {yes_ask:.2f}" + (f" x{depth}" if depth else ""),
            meta={"ticker": m["ticker"], "ask": yes_ask, "bid": yes_bid,
                  "depth": depth,
                  "sweep": (book[2] if book and len(book) > 2 else None),
                  # matching shard for this market (MLB is 3); -1 auto-routes
                  "exchange_index": m.get("exchange_index", -1)},
        ))
    return [ev for ev in by_event.values() if len(ev.teams) == 2]
