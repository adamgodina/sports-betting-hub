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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

from .. import config

from ..models import Quote, SourceEvent
from ..teams import make_resolver


def _nocache():
    """A throwaway query param so the CDN can't hand back a stored copy.

    Both exchanges put their public price endpoints behind a CDN cache —
    Kalshi's /markets for 15s, Polymarket's /v1/markets, /book and /bbo for
    30s — so a poll every second mostly got the same stale answer back. A
    unique param is a cache miss every time (verified on both).
    """
    return {"_": str(time.time_ns())}

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
        r = session.get(f"{GATEWAY}/v1/markets/{slug}/book", params=_nocache(), timeout=20)
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


def _ladder(levels, long):
    """[[cost, size], ...] cheapest first for the side being BOUGHT.

    The touch alone is not a tradeable price when only a handful of contracts
    rest there, so the whole ladder goes to the caller and it pays the
    volume-weighted cost of the size it actually wants.
    """
    out = []
    for lvl in levels or []:
        px = lvl.get("px")
        if isinstance(px, dict):
            px = px.get("value")
        try:
            px, qty = float(px), float(lvl.get("qty") or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        out.append([round(px if long else 1.0 - px, 6), round(qty, 2)])
    out.sort(key=lambda l: l[0])
    return out


# The per-market book endpoint is RATE LIMITED, and hard: measured at roughly
# five reads before Cloudflare answers 429 with Retry-After: 10. Asking for a
# dozen books at once (one board's worth of arb candidates) reliably got five
# answers and seven refusals, so those rows never measured, sat on "checking…",
# and asking again just renewed the ban — while ONE row asked by hand came back
# in 200ms. Hence a cooldown that honours Retry-After and a short cache: books
# a second or two old are fine for sizing a trade, and free.
_BOOK_TTL_S = 2.0            # a book this fresh is simply reused
_BOOK_STALE_S = 30.0         # when the venue says no, an older one beats nothing
_BOOK_MIN_GAP_S = 2.0        # sustained rate it tolerates (measured)
_BOOK_BURST = 4              # ~6 back to back trips it, then it blocks for 10s
_BOOK_COOLDOWN_CAP_S = 10.0
_book_cache: dict[str, tuple[float, dict]] = {}
_book_lock = threading.Lock()
_book_blocked_until = 0.0
_book_tokens = float(_BOOK_BURST)
_book_filled_at = time.time()


def _book_cached(slug, max_age):
    hit = _book_cache.get(slug)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    return None


def _book_throttled():
    return time.time() < _book_blocked_until


def _book_note_429(retry_after):
    global _book_blocked_until
    try:
        wait = float(retry_after)
    except (TypeError, ValueError):
        wait = _BOOK_COOLDOWN_CAP_S
    wait = min(max(wait, 1.0), _BOOK_COOLDOWN_CAP_S)
    with _book_lock:
        _book_blocked_until = max(_book_blocked_until, time.time() + wait)
    return wait


def _book_token(wait_s):
    """Take one request's worth of allowance, or give up. Refills steadily.

    Pacing beats apologising here: a 429 costs ten seconds of every book read,
    so it is cheaper to wait a beat than to ask and be refused.
    """
    global _book_tokens, _book_filled_at
    deadline = time.time() + wait_s
    while True:
        with _book_lock:
            now = time.time()
            _book_tokens = min(float(_BOOK_BURST),
                               _book_tokens + (now - _book_filled_at) / _BOOK_MIN_GAP_S)
            _book_filled_at = now
            if _book_tokens >= 1.0:
                _book_tokens -= 1.0
                return True
            short = (1.0 - _book_tokens) * _BOOK_MIN_GAP_S
        if time.time() + short > deadline:
            return False
        time.sleep(min(short, 0.25))


def _fetch_book_full(session, slug, wait_s: float = 2.0):
    """The raw book for one market, or None.

    The per-market book endpoint is RATE LIMITED, and hard: measured at about
    five reads before Cloudflare answers 429 with Retry-After: 10. Asking for a
    dozen books at once (one board's worth of arb candidates) reliably got five
    answers and seven refusals, so those rows never measured and sat on
    "checking…" — while ONE row asked by hand came back in 200ms, which is
    exactly the difference the board could not explain.

    So reads are paced, and when the allowance is gone the last book measured
    comes back instead of nothing: a ladder 20 seconds old still answers "is
    this arbitrage real at $100" far better than the touch price does.
    """
    fresh = _book_cached(slug, _BOOK_TTL_S)
    if fresh is not None:
        return fresh
    if _book_throttled() or not _book_token(wait_s):
        return _book_cached(slug, _BOOK_STALE_S)
    try:
        r = session.get(f"{GATEWAY}/v1/markets/{slug}/book", params=_nocache(), timeout=20)
        if r.status_code == 429:
            wait = _book_note_429(r.headers.get("retry-after"))
            print(f"  [polymarket_us] book rate limited - pausing {wait:.0f}s")
            return _book_cached(slug, _BOOK_STALE_S)
        r.raise_for_status()
        md = r.json().get("marketData", {})
        with _book_lock:
            _book_cache[slug] = (time.time(), md)
        return md
    except Exception:
        return _book_cached(slug, _BOOK_STALE_S)


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
        # 8 at once was a burst; the limiter counts bursts.
        with ThreadPoolExecutor(max_workers=3) as pool:
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
                                         "sweep": _sweep(offers, True, ask + slip),
                                         "levels": _ladder(offers, True)}
                if bids:
                    bid = _amount(bids[0].get("px"))
                    qty = _amount(bids[0].get("qty")) or 0
                    if bid is not None:
                        cost = round(1.0 - bid, 6)
                        entry["short"] = {"ask": cost, "depth": int(qty),
                                          "sweep": _sweep(bids, False, cost + slip),
                                          "levels": _ladder(bids, False)}
                if entry:
                    out[slug] = entry
    return out


# One pooled keep-alive session for the events feed. It is polled every second
# in live mode, and a fresh TLS handshake per page was part of the cost.
_feed = requests.Session()
_feed.mount("https://", requests.adapters.HTTPAdapter(
    pool_connections=4, pool_maxsize=12, max_retries=0))
PAGE = 100
PAGE_CAP = 10


def _league_events(league):
    """Every event in a league's feed, pages fetched concurrently.

    The feed pages by offset, and college football runs to ~6 pages at ~1s
    each. Fetched one after another that was 6+ seconds per poll — longer than
    the live tick allows, so every live update was thrown away and the board
    froze. The first page says whether there are more; the rest go out at once.
    """
    url = f"{GATEWAY}/v2/leagues/{league}/events"

    def page(offset):
        r = _feed.get(url, params={"limit": PAGE, "offset": offset}, timeout=30)
        r.raise_for_status()
        return r.json().get("events", [])

    first = page(0)
    if len(first) < PAGE:
        return first
    events = list(first)
    offsets = [PAGE * i for i in range(1, PAGE_CAP)]
    with ThreadPoolExecutor(max_workers=len(offsets)) as pool:
        for chunk in pool.map(page, offsets):   # in order, so a short page ends it
            events.extend(chunk)
            if len(chunk) < PAGE:
                break
    return events


# ---- discovery vs prices -------------------------------------------------
#
# The league events feed is the only way to FIND a league's games, but it
# carries every market of every game — props, quarters, halves — and there is
# no filter that trims it (market type, time window, field selection were all
# tried and ignored). College football is ~87MB across three pages, ~6s per
# fetch. Polled every second, that is what froze live mode: each poll ran past
# the page's deadline and was thrown away.
#
# So the two jobs are split. The feed is fetched at most every
# DISCOVERY_TTL_S, in the background once warm, to learn which winner markets
# exist. The per-second poll then asks /v1/markets for just those slugs — the
# same marketSides, quotes and fee, ~5KB a market instead of ~480KB.
DISCOVERY_TTL_S = 60
SLUGS_PER_CALL = 50             # 100 in one query gets a non-JSON error back
_discovery = {}                 # league -> {"at", "cands", "busy"}
_disc_lock = threading.Lock()


def _discover(league, winner_type):
    """Scan the feed for (event key, winner market, start). Updates the cache.

    Soccer lists THREE winner markets per match — one per team and one for the
    draw — so every one is kept and they are grouped by event later.
    """
    cands = []
    for ev in _league_events(league):
        start = _parse(ev.get("startTime") or ev.get("startDate"))
        if start is None or ev.get("ended"):
            continue
        key = ev.get("slug") or ev.get("id")
        for mkt in ev.get("markets", []):
            if mkt.get("sportsMarketType") == winner_type and mkt.get("slug"):
                cands.append((key, mkt, start))
    with _disc_lock:
        _discovery[league] = {"at": time.time(), "cands": cands, "busy": False}
    return cands


def _refresh_in_background(league, winner_type):
    try:
        _discover(league, winner_type)
    except Exception as e:
        print(f"  [polymarket_us] discovery failed: {e}")
        with _disc_lock:
            if league in _discovery:
                _discovery[league]["busy"] = False


def _candidates(league, winner_type, fast):
    """Known winner markets. A full scan (fast=False) always re-discovers; the
    fast poll uses the cache and refreshes it off the request path."""
    with _disc_lock:
        d = _discovery.get(league)
        stale = d is None or time.time() - d["at"] > DISCOVERY_TTL_S
        if fast and d is not None and stale and not d["busy"]:
            d["busy"] = True
            threading.Thread(target=_refresh_in_background,
                             args=(league, winner_type), daemon=True).start()
    if d is None or not fast:
        return _discover(league, winner_type)
    return d["cands"]


def _fresh_markets(slugs):
    """slug -> current market (with live marketSides quotes), batched."""
    chunks = [slugs[i:i + SLUGS_PER_CALL] for i in range(0, len(slugs), SLUGS_PER_CALL)]

    def one(chunk):
        try:
            r = _feed.get(f"{GATEWAY}/v1/markets", params={"slug": chunk, **_nocache()}, timeout=15)
            r.raise_for_status()
            d = r.json()
            return d.get("markets", d if isinstance(d, list) else [])
        except Exception as e:
            print(f"  [polymarket_us] price poll failed: {e}")
            return []   # those games simply keep their last price on screen

    out = {}
    with ThreadPoolExecutor(max_workers=max(1, len(chunks))) as pool:
        for ms in pool.map(one, chunks):
            for m in ms:
                if m.get("slug"):
                    out[m["slug"]] = m
    return out


def fetch(sport_cfg, with_depth: bool = True, roster=None) -> list[SourceEvent]:
    """with_depth=False skips the per-game book calls: 1 request instead of ~27,
    prices only. Each side's `quote` in the events feed already IS its top of
    book (verified against /bbo on every market), so prices stay exact — only
    the resting size is unavailable. Used by the 1/second exchange poll."""
    leagues = config.feeds(sport_cfg.polymarket_us_league)
    winner_type = sport_cfg.polymarket_us_winner_type
    if not leagues or not winner_type:
        return []

    by_roster = getattr(sport_cfg, "match_mode", "alias") == "roster"
    resolve = make_resolver(sport_cfg)
    horizon = datetime.now(timezone.utc) + timedelta(hours=_horizon(sport_cfg))

    # In-horizon games that have a winner market. Soccer has three per match
    # (each side and the draw), so markets are grouped back into their event.
    three = bool(getattr(sport_cfg, "three_way", False))
    # One sport can span several leagues (tennis is ATP + WTA); each keeps its
    # own discovery cache, so they are fetched separately and pooled here.
    candidates = [(key, m, st) for lg in leagues
                  for key, m, st in _candidates(lg, winner_type, fast=not with_depth)
                  if st <= horizon]
    if not candidates:
        return []
    if not with_depth:
        # prices for just these markets, not the whole feed again
        fresh = _fresh_markets([m["slug"] for _, m, _ in candidates])
        candidates = [(key, fresh[m["slug"]], st) for key, m, st in candidates
                      if m["slug"] in fresh and not fresh[m["slug"]].get("closed")]

    # pull each market's top of book in parallel (public, free)
    books = {}
    if with_depth:
        slugs = [m["slug"] for _, m, _ in candidates]
        with requests.Session() as session:
            with ThreadPoolExecutor(max_workers=8) as pool:
                books = dict(zip(slugs, pool.map(
                    lambda sl: _fetch_book(session, sl), slugs)))

    groups = {}
    for key, mkt, start in candidates:
        groups.setdefault(key, []).append((mkt, start))

    out, unmapped = [], []
    for entries in groups.values():
        se = SourceEvent(source="polymarket_us", teams=frozenset(),
                         start=entries[0][1])
        for mkt, _ in entries:
            market_slug = mkt["slug"]
            book = books.get(market_slug) if with_depth else None
            if with_depth and book is None:
                continue
            ask_px, ask_qty, bid_px, bid_qty = book if book else (None,) * 4
            theta = _amount(mkt.get("feeCoefficient"))
            if theta is None:
                theta = DEFAULT_THETA

            # side -> (BBO cost, size available at it)
            long_side = (ask_px, ask_qty)
            short_side = ((1.0 - bid_px) if bid_px is not None else None, bid_qty)

            sides = mkt.get("marketSides", [])
            raws = [((sd.get("team") or {}).get("name") or sd.get("description"))
                    for sd in sides]
            if three:
                # A three-way market's NO side is "anything else" — two
                # outcomes at once, not something you can hold — so only the
                # YES side of each of the three markets is a price here. The
                # draw has its own market, whose sides are named Yes/No.
                is_draw = market_slug.endswith("-draw") or all(
                    str(r).strip().lower() in ("yes", "no") for r in raws if r)
                names = {}
                for sd, raw in zip(sides, raws):
                    if not sd.get("long"):
                        continue
                    names[raw] = config.DRAW if is_draw else (
                        roster.resolve(raw) if (by_roster and roster is not None)
                        else resolve(raw))
            elif by_roster and roster is not None and len(raws) == 2:
                # resolve both sides together — college nicknames ("Wildcats")
                # are only unambiguous once the opponent has to agree
                a, b = roster.resolve_pair(raws[0], raws[1])
                names = {raws[0]: a, raws[1]: b}
            else:
                names = {r: resolve(r) for r in raws}

            for side, raw_team in zip(sides, raws):
                if three and not side.get("long"):
                    continue
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
                # the draw is an outcome, not a team: it must not join the pair
                # the fixture is matched on
                if team != config.DRAW:
                    se.teams = se.teams | {team}
                depth = int(qty) if qty else (None if not with_depth else 0)
                se.quotes.append(Quote(
                    book="polymarket_us", team=team,
                    prob=_effective_prob(price, theta),
                    detail=f"BBO {price:.4f}" + (f" x{depth}" if depth else ""),
                    meta={"market_slug": market_slug,
                          "ask": price,
                          "long": bool(side.get("long")),
                          "depth": depth,
                          # the fee actually folded into `prob`, so the client
                          # can re-price at another size the same way
                          "theta": theta if config.INCLUDE_POLYMARKET_US_FEES else 0.0},
                ))
        if len(se.teams) == 2:
            out.append(se)
    if unmapped:
        # Mostly fixtures the sportsbooks don't price at all (lower divisions),
        # plus nicknames too ambiguous to resolve. Refusing beats guessing, so
        # this is a count rather than a wall of warnings.
        print(f"  [polymarket_us] {len(unmapped)} entrants not on the board "
              f"(e.g. {', '.join(sorted(set(str(u) for u in unmapped))[:3])})")
    return out
