"""Parlay pricer: every outcome of a set of legs, quoted as a Kalshi combo.

A parlay of n legs has 2^n outcomes — for two legs, both hit / only the first /
only the second / neither. Exactly one of them happens, so owning all of them
pays $1 whatever the result. That is what makes the set useful: a sportsbook
parlay is one of those outcomes, and buying the OTHER outcomes on an exchange
hedges it the same way a No hedges a single bet.

Where the prices come from:

  * Kalshi builds combos on demand from its multivariate collection
    (POST /multivariate_event_collections/{collection}) — legs are chosen
    market by market, each on its Yes or No side, so "Walker scores and Dobbins
    does not" is a real, tradeable market. It has no resting book, though: the
    37 open combo markets sampled had essentially no orders. The price is
    discovered by a request for quote (POST /communications/rfqs), which makers
    answer privately with a `yes_bid` and a `no_bid`.

    Reading a quote: both numbers are the MAKER's bids (Kalshi rejects a quote
    whose two bids sum past $1, which only makes sense for bids). A maker
    bidding `no_bid` for No is offering to sell Yes at 1 - no_bid, so owning
    the outcome costs 1 - best no_bid.

    An RFQ binds nobody until it is accepted, and each one is deleted as soon
    as its quotes are read, so asking for a price never leaves anything open.

  * Polymarket US has combos and RFQs too (POST /v1/combos, /v1/rfqs), but the
    Retail API marks them "Beta access is required" and this account's key gets
    403 on every one of those endpoints. Until that access is granted the only
    Polymarket-derived number is none at all; the estimate below stands in.

  * The estimate: each leg's own Kalshi market, mid of bid and ask, multiplied
    together. That assumes the legs are independent, which is fair for players
    in different games and only rough for the same game — two players on one
    team compete for the same touchdowns — so same-game sets are flagged.

Nothing here spends Odds API credits.
"""
import itertools
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from .trading import kalshi_trader

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

# The cross-game sports collection. Its associated events include player props
# (KXNFLTD, KXMLBHR) as well as game lines, with no Yes-only restriction.
COLLECTION = "KXMVESPORTSMULTIGAMEEXTENDED-R"

# Series prefixes worth searching — the sports this app covers. The collection
# spans 162 series (La Liga corners, KBO, esports); fetching every one on each
# search would be slow and bury the lines that matter.
SEARCH_PREFIXES = ("KXNFL", "KXNCAAF", "KXMLB", "KXNBA", "KXNCAAB",
                   "KXATP", "KXWTA")

CATALOG_TTL_S = 300
MAX_LEGS = 3                  # 8 outcomes; past that, 2^n quotes gets silly
QUOTE_WAIT_S = 4.0            # how long to collect maker answers per outcome
QUOTE_SETTLE_S = 1.2          # once a quote lands, wait this much for rivals

_catalog = {"at": 0.0, "lines": [], "events": {}}
_combo_cache = {}             # frozenset of (ticker, side) -> combo market ticker


def _f(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if 0 < v < 1 else None


def _norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+. ]", " ", (s or "").lower())).strip()


# ---------------------------------------------------------------- catalog

def _collection_events():
    r = requests.get(f"{KALSHI_API}/multivariate_event_collections/{COLLECTION}",
                     timeout=30)
    r.raise_for_status()
    c = r.json().get("multivariate_contract") or {}
    return {e["ticker"]: e for e in c.get("associated_events") or []}


def _series_events(series):
    """(events, complete) for one series. Kalshi's public API rate-limits hard:
    eight at once got 429 on a third of the series, silently dropping NFL
    spreads and totals from search. So fewer workers, and back off and retry."""
    out, cursor = [], None
    with requests.Session() as s:
        for _ in range(10):
            p = {"series_ticker": series, "status": "open",
                 "with_nested_markets": "true", "limit": 200}
            if cursor:
                p["cursor"] = cursor
            d = None
            for attempt in range(5):
                try:
                    r = s.get(f"{KALSHI_API}/events", params=p, timeout=30)
                    if r.status_code == 429:
                        time.sleep(0.5 * (2 ** attempt))
                        continue
                    r.raise_for_status()
                    d = r.json()
                    break
                except Exception as e:
                    print(f"  [parlay] {series} failed: {e}")
                    break
            if d is None:
                return out, False
            out.extend(d.get("events") or [])
            cursor = d.get("cursor")
            if not cursor or not d.get("events"):
                break
    return out, True


def catalog(force=False):
    """Every open market a combo may use, flattened for search. Free, cached."""
    if not force and time.time() - _catalog["at"] < CATALOG_TTL_S and _catalog["lines"]:
        return _catalog
    allowed = _collection_events()
    series = sorted({t.split("-")[0] for t in allowed
                     if t.startswith(SEARCH_PREFIXES)})
    with ThreadPoolExecutor(max_workers=3) as pool:
        batches = list(pool.map(_series_events, series))
    lines, events = [], {}
    missing = [s for s, (_, ok) in zip(series, batches) if not ok]
    for evs, _ in batches:
        for ev in evs:
            et = ev.get("event_ticker")
            meta = allowed.get(et)
            if not meta:
                continue              # open, but not combinable
            events[et] = {"title": ev.get("title"), "sub_title": ev.get("sub_title")}
            for m in ev.get("markets") or []:
                if m.get("status") not in (None, "active", "open"):
                    continue
                yb, ya = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
                nb, na = _f(m.get("no_bid_dollars")), _f(m.get("no_ask_dollars"))
                if ya is None and na is None:
                    continue
                label = m.get("yes_sub_title") or m.get("title") or m["ticker"]
                lines.append({
                    "ticker": m["ticker"], "event_ticker": et,
                    "series": et.split("-")[0],
                    "label": label, "title": m.get("title") or label,
                    "event": ev.get("title") or et,
                    "sub": ev.get("sub_title") or "",
                    "yes_ask": ya, "no_ask": na, "yes_bid": yb, "no_bid": nb,
                    "yes_only": bool(meta.get("is_yes_only")),
                    "close": m.get("expected_expiration_time") or m.get("close_time"),
                    "_hay": _norm(f"{ev.get('title')} {ev.get('sub_title')} "
                                  f"{m.get('title')} {label} {m['ticker']}"),
                })
    # A partial catalog is still worth searching, but not worth keeping for
    # the full TTL — expire it in 20s so the gaps fill in on a later search.
    at = time.time() - (CATALOG_TTL_S - 20 if missing else 0)
    _catalog.update(at=at, lines=lines, events=events, missing=missing)
    return _catalog


def search(q, limit=40):
    toks = _norm(q).split()
    if not toks:
        return []
    hits = [l for l in catalog()["lines"] if all(t in l["_hay"] for t in toks)]
    # "1+" lines first within a player, then the likeliest — the lines people
    # actually parlay, rather than a wall of 4+ touchdown longshots
    def rank(l):
        one = 0 if re.search(r":\s*1\+", l["title"]) else 1
        return (l["event"], one, -(l["yes_ask"] or 0))
    return [{k: v for k, v in l.items() if k != "_hay"}
            for l in sorted(hits, key=rank)[:limit]]


def line(ticker):
    return next((l for l in catalog()["lines"] if l["ticker"] == ticker), None)


# ---------------------------------------------------------------- pricing

def _mid_yes(l):
    """Fair Yes probability from the leg's own market: mid, else the best side."""
    yb, ya = l.get("yes_bid"), l.get("yes_ask")
    if yb and ya:
        return (yb + ya) / 2
    if ya:
        return ya
    na = l.get("no_ask")
    return 1 - na if na else None


def outcomes(legs):
    """All 2^n side combinations of the chosen legs, the chosen sides first.

    legs: [{ticker, event_ticker, side}] as picked in the builder.
    """
    lines = []
    for lg in legs:
        l = line(lg["ticker"]) or {}
        lines.append({**l, **lg})
    flips = list(itertools.product((False, True), repeat=len(lines)))
    out = []
    for f in flips:
        sides, est = [], 1.0
        for l, flip in zip(lines, f):
            side = l["side"] if not flip else ("no" if l["side"] == "yes" else "yes")
            p = _mid_yes(l)
            est = None if (est is None or p is None) else est * (p if side == "yes" else 1 - p)
            sides.append({"ticker": l["ticker"], "event_ticker": l["event_ticker"],
                          "side": side,
                          # the full title: a yes_sub_title like "Kenneth
                          # Walker III: 1+" drops what it is 1+ OF
                          "label": (l.get("title") or l.get("label")
                                    or l["ticker"]).rstrip("?"),
                          "yes_only": l.get("yes_only")})
        out.append({"legs": sides, "est": round(est, 5) if est is not None else None,
                    "blocked": any(s["side"] == "no" and s["yes_only"] for s in sides)})
    same_game = len({l["event_ticker"].split("-", 1)[-1] for l in lines}) < len(lines)
    return out, same_game


def _combo_ticker(legs):
    key = frozenset((l["ticker"], l["side"]) for l in legs)
    if key in _combo_cache:
        return _combo_cache[key]
    r = kalshi_trader._request(
        "POST", f"/trade-api/v2/multivariate_event_collections/{COLLECTION}",
        {"selected_markets": [{"market_ticker": l["ticker"],
                               "event_ticker": l["event_ticker"],
                               "side": l["side"]} for l in legs]})
    _combo_cache[key] = r["market_ticker"]
    return r["market_ticker"]


def _sleep(s):
    time.sleep(s)


def _open_rfq(market_ticker, contracts):
    # replace_existing stays OFF: outcomes are quoted and bought in parallel,
    # and "delete existing RFQs" must never be able to close a sibling's.
    return kalshi_trader._request(
        "POST", "/trade-api/v2/communications/rfqs",
        {"market_ticker": market_ticker, "contracts": int(max(1, contracts)),
         "rest_remainder": False})["id"]


def _delete_rfq(rid):
    try:
        kalshi_trader._request("DELETE", f"/trade-api/v2/communications/rfqs/{rid}")
    except Exception:
        pass                           # already closed is fine


def _collect_quotes(rid):
    """Every open quote on this RFQ, gathered for up to QUOTE_WAIT_S and for
    QUOTE_SETTLE_S after the first lands (so a slower, better maker can still
    beat the first responder). Latest version of each maker's quote wins."""
    got = {}
    t0, first = time.time(), None
    while time.time() - t0 < QUOTE_WAIT_S:
        if first and time.time() - first >= QUOTE_SETTLE_S:
            break
        try:
            qs = kalshi_trader._request(
                "GET", "/trade-api/v2/communications/quotes",
                params={"rfq_id": rid}).get("quotes") or []
        except Exception:
            qs = []
        for q in qs:
            if q.get("status") not in (None, "open"):
                continue
            got[q.get("id")] = q
            first = first or time.time()
        _sleep(0.4)
    return list(got.values())


def quote_outcome(legs, contracts):
    """Ask Kalshi's makers for this one outcome. Never accepts anything."""
    res = {"venue": "kalshi", "quotes": 0, "yes_cost": None, "no_cost": None,
           "market_ticker": None, "error": None}
    try:
        mt = _combo_ticker(legs)
        res["market_ticker"] = mt
        rid = _open_rfq(mt, contracts)
    except Exception as e:
        res["error"] = str(e)
        return res
    try:
        quotes = _collect_quotes(rid)
    finally:
        _delete_rfq(rid)
    nbs = [v for v in (_f(q.get("no_bid_dollars")) for q in quotes) if v]
    ybs = [v for v in (_f(q.get("yes_bid_dollars")) for q in quotes) if v]
    res["quotes"] = len(quotes)
    if nbs:
        res["yes_cost"] = round(1 - max(nbs), 4)   # owning this outcome
    if ybs:
        res["no_cost"] = round(1 - max(ybs), 4)
    return res


def quote_all(legs, contracts=100):
    if not 2 <= len(legs) <= MAX_LEGS:
        raise ValueError(f"pick 2 to {MAX_LEGS} legs")
    if len({l["ticker"] for l in legs}) != len(legs):
        raise ValueError("the same line twice")
    rows, same_game = outcomes(legs)
    with ThreadPoolExecutor(max_workers=len(rows)) as pool:
        quotes = list(pool.map(
            lambda r: None if r["blocked"] else quote_outcome(r["legs"], contracts),
            rows))
    for r, q in zip(rows, quotes):
        r["kalshi"] = q or {"error": "a leg here is Yes-only on Kalshi"}
    return {"outcomes": rows, "same_game": same_game, "contracts": contracts,
            "polymarket": {"available": False,
                           "why": "Polymarket US combos need beta API access "
                                  "(this key gets 403)"}}


# ---------------------------------------------------------------- buying

# Which `accepted_side` BUYS the combo. Kalshi's REST docs only say "the side of
# the quote to accept (yes or no)". What pins it down:
#   * a quote's yes_bid/no_bid are the MAKER's bids — Kalshi rejects a quote
#     whose two sum past $1, which is only coherent for bids;
#   * the FIX spec for the same action: "BUY accepts the maker's NO quote and
#     SELL accepts the maker's YES quote".
# So owning the outcome = accepting the maker's No bid, at 1 - no_bid. The
# example in Kalshi's own docs sums to exactly $1, where both readings agree,
# so this is not proven against a live fill — which is why every buy reads the
# resulting order back and reports its outcome_side (see buy_outcome).
ACCEPT_SIDE_TO_BUY = "no"
KALSHI_THETA = 0.07
BUY_MAX_SLIPPAGE = 0.03       # refuse if a fresh quote is > shown + 3c
ACCEPT_WAIT_S = 8.0           # 3s maker last look + 1s execution timer + slack


def _quote_state(rid, qid):
    d = kalshi_trader._request(
        "GET", f"/trade-api/v2/communications/rfqs/{rid}/quotes/{qid}")
    return d.get("quote") or d


def buy_outcome(legs, contracts, max_price):
    """Buy `contracts` of one outcome's combo. Real money.

    Fresh RFQ -> best maker's No bid -> refuse if the price is above
    `max_price` (capped at what was shown + BUY_MAX_SLIPPAGE by the caller) or
    the shard can't cover it -> accept -> wait for the maker's last look and
    execution -> read the resulting order back.
    """
    count = int(round(float(contracts)))
    if count < 1:
        raise ValueError("contracts must round to at least 1")
    cap = float(max_price)
    if not 0 < cap < 1:
        raise ValueError("max price must be between 0 and 1")
    mt = _combo_ticker(legs)
    rid = _open_rfq(mt, count)
    accepted, best, price = False, None, None
    try:
        quotes = [q for q in _collect_quotes(rid) if _f(q.get("no_bid_dollars"))]
        if not quotes:
            raise ValueError("no Kalshi maker quoted this outcome just now — "
                             "nothing was bought")
        best = max(quotes, key=lambda q: _f(q.get("no_bid_dollars")))
        price = round(1 - _f(best.get("no_bid_dollars")), 4)
        if price > cap + 1e-9:
            raise ValueError(
                f"the fresh quote is {price * 100:.1f}¢, above your "
                f"{cap * 100:.1f}¢ cap — nothing was bought")
        offered = _f_count(best.get("no_contracts_fp")) or _f_count(best.get("contracts_fp"))
        if offered is not None and offered + 1e-9 < count:
            raise ValueError(f"the maker only offered {offered:g} of {count} "
                             f"contracts — nothing was bought")
        fee = math.ceil(KALSHI_THETA * count * price * (1 - price) * 100) / 100
        need = round(count * price + fee, 2)
        idx = kalshi_trader.market_shard(mt)
        if idx is not None and 0 <= idx <= 3:
            have = kalshi_trader.shard_balances().get(idx)
            if have is not None and have + 1e-9 < need:
                raise ValueError(
                    f"not enough collateral on Kalshi shard {idx}, where this "
                    f"parlay trades: it needs ${need:,.2f} and the shard holds "
                    f"${have:,.2f} — nothing was bought")
        kalshi_trader._request(
            "PUT", f"/trade-api/v2/communications/rfqs/{rid}/quotes/{best['id']}/accept",
            {"accepted_side": ACCEPT_SIDE_TO_BUY})
        accepted = True
    finally:
        if not accepted:
            _delete_rfq(rid)

    state, t0 = {}, time.time()
    while time.time() - t0 < ACCEPT_WAIT_S:
        try:
            state = _quote_state(rid, best["id"]) or state
        except Exception:
            pass
        if (state.get("status") in ("executed", "cancelled")
                or state.get("rfq_creator_order_id")):
            if state.get("status") != "confirmed":
                break
        _sleep(0.4)

    order_id = state.get("rfq_creator_order_id")
    names = " + ".join(f"{l['side'].upper()} {l.get('label') or l['ticker']}"
                       for l in legs)
    out = {"venue": "kalshi", "order_id": order_id, "contracts": count,
           "price": price, "market_ticker": mt, "wrong_side": False,
           "detail": f"buy {count} × parlay [{names}] at {price * 100:.1f}¢ "
                     f"(cap {cap * 100:.1f}¢)"}
    if not order_id:
        why = state.get("cancellation_reason") or state.get("status") or "no answer"
        out["status"] = (f"NOT FILLED — the maker did not confirm ({why}); "
                         f"nothing was bought")
        return out
    try:
        o = kalshi_trader._request(
            "GET", f"/trade-api/v2/portfolio/orders/{order_id}").get("order") or {}
    except Exception:
        o = {}
    side = o.get("outcome_side") or {"bid": "yes", "ask": "no"}.get(o.get("book_side"))
    if side and side != "yes":
        # The one thing the docs could not settle. Loud, and never green.
        out["wrong_side"] = True
        out["status"] = (f"WRONG SIDE — Kalshi recorded this as {side.upper()} on "
                         f"the parlay, not YES. Check your Kalshi positions now.")
        return out
    filled = _f_count(o.get("fill_count_fp")) or 0.0
    remaining = _f_count(o.get("remaining_count_fp"))
    if filled >= count - 1e-9:
        out["status"] = f"FILLED {filled:g}"
    elif filled:
        out["status"] = f"PARTIAL: filled {filled:g} of {count}"
    else:
        out["status"] = (f"NOT FILLED — order {order_id[:8]} shows 0 of {count} "
                         f"filled{f', {remaining:g} resting' if remaining else ''}")
    return out


def _f_count(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
