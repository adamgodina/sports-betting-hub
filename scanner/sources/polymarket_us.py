"""Polymarket US (polymarket.us) market data — the CFTC-regulated US exchange.

This is a DIFFERENT exchange from polymarket.com with its own order book, so
US traders must price off this one. Market data is public (no API key).

Structure: each game has one binary instrument per market type, addressed by
`marketSlug`. The winner market's two sides are the same instrument, and every
price we publish is the **top of book** (BBO) — the only realistically
fillable price:
    long  side (YES) -> buy at best ask
    short side (NO)  -> sell YES at best bid, i.e. cost 1 - bestBid
We read the full book (`/v1/markets/{slug}/book`) rather than the events feed's
`quote` so we also get the **size available at that price**. Note the ask side
is keyed `offers` there, and the `/bbo` endpoint's bidDepth/askDepth are
price-LEVEL counts (35/30), not contract sizes.

Fees: polymarket.us charges a taker fee of feeCoefficient * p * (1-p) per
contract (0.06 today; polymarket.com had none), folded into the effective price.

Sizing: whole contracts only, on this venue and on Kalshi.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

from .. import config

from ..models import Quote, SourceEvent
from ..teams import make_resolver

GATEWAY = "https://gateway.polymarket.us"
DEFAULT_THETA = 0.06

def _horizon(sport_cfg):
    """How far ahead this sport lists fixtures."""
    return getattr(sport_cfg, "horizon_hours", 0) or config.GAME_HORIZON_HOURS


def _effective_prob(price: float, theta: float) -> float:
    if config.INCLUDE_POLYMARKET_US_FEES:
        return price + theta * price * (1.0 - price)
    return price


def _parse(ts: str):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _amount(obj):
    if isinstance(obj, dict):
        obj = obj.get("value")
    try:
        return float(obj)
    except (TypeError, ValueError):
        return None


def _fetch_book(session, slug):
    """Top of book for one market: (ask_px, ask_qty, bid_px, bid_qty)."""
    try:
        r = session.get(f"{GATEWAY}/v1/markets/{slug}/book", timeout=20)
        r.raise_for_status()
        md = r.json().get("marketData", {})
    except Exception as e:
        print(f"  [polymarket_us] book fetch failed for {slug}: {e}")
        return None
    # the ask side is keyed "offers" on this API
    bids = md.get("bids") or []
    asks = md.get("offers") or md.get("asks") or []
    top = lambda side: (_amount(side[0].get("px")), _amount(side[0].get("qty"))) \
        if side else (None, None)
    ask_px, ask_qty = top(asks)
    bid_px, bid_qty = top(bids)
    return ask_px, ask_qty, bid_px, bid_qty


def _sweep(levels, long, cap):
    """Total size obtainable at or below `cap` for the given side."""
    total = 0.0
    for lvl in levels or []:
        px = lvl.get("px")
        if isinstance(px, dict):
            px = px.get("value")
        try:
            px, qty = float(px), float(lvl.get("qty") or 0)
        except (TypeError, ValueError):
            continue
        cost = px if long else round(1.0 - px, 6)
        if cost <= cap + 1e-9:
            total += qty
    return int(total)


def _fetch_book_full(session, slug):
    try:
        r = session.get(f"{GATEWAY}/v1/markets/{slug}/book", timeout=20)
        r.raise_for_status()
        return r.json().get("marketData", {})
    except Exception:
        return None


def fetch_books(slugs, sweep_cents: float = None) -> dict:
    """slug -> {long: {ask, depth, sweep}, short: {...}}.

    `sweep` is the size obtainable within `sweep_cents` of the touch — what a
    hedger needs, since an aggressive order eats several levels.
    """
    if sweep_cents is None:
        sweep_cents = config.HEDGE_MAX_SLIPPAGE_CENTS
    slip = sweep_cents / 100.0
    out = {}
    with requests.Session() as session:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for slug, md in zip(slugs, pool.map(
                    lambda sl: _fetch_book_full(session, sl), slugs)):
                if not md:
                    continue
                bids = md.get("bids") or []
                offers = md.get("offers") or md.get("asks") or []
                entry = {}
                if offers:
                    ask = _amount(offers[0].get("px"))
                    qty = _amount(offers[0].get("qty")) or 0
                    if ask is not None:
                        entry["long"] = {"ask": ask, "depth": int(qty),
                                         "sweep": _sweep(offers, True, ask + slip)}
                if bids:
                    bid = _amount(bids[0].get("px"))
                    qty = _amount(bids[0].get("qty")) or 0
                    if bid is not None:
                        cost = round(1.0 - bid, 6)
                        entry["short"] = {"ask": cost, "depth": int(qty),
                                          "sweep": _sweep(bids, False, cost + slip)}
                if entry:
                    out[slug] = entry
    return out


def fetch(sport_cfg, with_depth: bool = True, roster=None) -> list[SourceEvent]:
    """with_depth=False skips the per-game book calls: 1 request instead of ~27,
    prices only. Each side's `quote` in the events feed already IS its top of
    book (verified against /bbo on every market), so prices stay exact — only
    the resting size is unavailable. Used by the 1/second exchange poll."""
    league = sport_cfg.polymarket_us_league
    winner_type = sport_cfg.polymarket_us_winner_type
    if not league or not winner_type:
        return []

    by_roster = getattr(sport_cfg, "match_mode", "alias") == "roster"
    resolve = make_resolver(sport_cfg)
    horizon = datetime.now(timezone.utc) + timedelta(hours=_horizon(sport_cfg))

    events, offset = [], 0
    for _ in range(10):  # pagination safety cap
        r = requests.get(f"{GATEWAY}/v2/leagues/{league}/events",
                         params={"limit": 100, "offset": offset}, timeout=30)
        r.raise_for_status()
        page = r.json().get("events", [])
        events.extend(page)
        if len(page) < 100:
            break
        offset += 100

    # keep only in-horizon games that have a winner market
    candidates = []
    for ev in events:
        start = _parse(ev.get("startTime") or ev.get("startDate"))
        if start is None or start > horizon or ev.get("ended"):
            continue
        mkt = next((m for m in ev.get("markets", [])
                    if m.get("sportsMarketType") == winner_type), None)
        if mkt and mkt.get("slug"):
            candidates.append((ev, mkt, start))
    if not candidates:
        return []

    # pull each market's top of book in parallel (public, free)
    if with_depth:
        with requests.Session() as session:
            with ThreadPoolExecutor(max_workers=8) as pool:
                books = list(pool.map(lambda c: _fetch_book(session, c[1]["slug"]),
                                      candidates))
    else:
        books = [None] * len(candidates)

    out, unmapped = [], []
    for (ev, mkt, start), book in zip(candidates, books):
        if book is None and with_depth:
            continue
        if book:
            ask_px, ask_qty, bid_px, bid_qty = book
        else:
            # fast poll: take each side's quote (already the BBO), size unknown
            ask_px = ask_qty = bid_px = bid_qty = None
        market_slug = mkt["slug"]
        theta = _amount(mkt.get("feeCoefficient"))
        if theta is None:
            theta = DEFAULT_THETA

        # side -> (BBO cost, size available at it)
        long_side = (ask_px, ask_qty)
        short_side = ((1.0 - bid_px) if bid_px is not None else None, bid_qty)

        se = SourceEvent(source="polymarket_us", teams=frozenset(), start=start)
        sides = mkt.get("marketSides", [])
        raws = [((sd.get("team") or {}).get("name") or sd.get("description"))
                for sd in sides]
        if by_roster and roster is not None and len(raws) == 2:
            # resolve both sides together — college nicknames ("Wildcats") are
            # only unambiguous once the opponent has to agree
            a, b = roster.resolve_pair(raws[0], raws[1])
            names = {raws[0]: a, raws[1]: b}
        else:
            names = {r: resolve(r) for r in raws}
        for side, raw_team in zip(sides, raws):
            team = names.get(raw_team)
            if not team:
                unmapped.append(raw_team)      # summarised after the loop
                continue
            if with_depth:
                price, qty = long_side if side.get("long") else short_side
            else:
                price, qty = _amount(side.get("quote")), None
            if price is None or not 0 < price < 1 or not side.get("tradable", True):
                continue
            se.teams = se.teams | {team}
            depth = int(qty) if qty else (None if not with_depth else 0)
            se.quotes.append(Quote(
                book="polymarket_us", team=team,
                prob=_effective_prob(price, theta),
                detail=f"BBO {price:.4f}" + (f" x{depth}" if depth else ""),
                meta={"market_slug": market_slug,
                      "ask": price,
                      "long": bool(side.get("long")),
                      "depth": depth},
            ))
        if len(se.teams) == 2:
            out.append(se)
    if unmapped:
        # Mostly fixtures the sportsbooks don't price at all (lower divisions),
        # plus nicknames too ambiguous to resolve. Refusing beats guessing, so
        # this is a count rather than a wall of warnings.
        print(f"  [polymarket_us] {len(unmapped)} entrants not on the board "
              f"(e.g. {', '.join(sorted(set(unmapped))[:3])})")
    return out
