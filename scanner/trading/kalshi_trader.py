"""Authenticated Kalshi trading client (RSA-PSS request signing).

Auth headers are KALSHI-ACCESS-KEY / -TIMESTAMP / -SIGNATURE, where the
signature is RSA-PSS(SHA256) over "<timestamp_ms><METHOD><path>".

Orders use the V2 endpoint POST /trade-api/v2/portfolio/events/orders; the
legacy /portfolio/orders path now returns HTTP 410 deprecated_v1_order_endpoint.
Docs: https://docs.kalshi.com/api-reference/orders/create-order-v2
"""
import base64
import json
import math
import time
import uuid
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from .. import config

HOST = "https://api.elections.kalshi.com"

# One pooled, keep-alive session for every call. A fresh TLS handshake costs
# ~130ms against this host; a warm connection answers in ~43ms, and an order is
# on the hot path, so the connection is kept open and warmed in the background.
_session = requests.Session()
_session.mount("https://", requests.adapters.HTTPAdapter(
    pool_connections=4, pool_maxsize=8, max_retries=0))
_private_key = None


class TradingNotConfigured(Exception):
    pass


def _load_key():
    global _private_key
    if _private_key is not None:
        return _private_key
    if not config.KALSHI_API_KEY_ID or not config.KALSHI_PRIVATE_KEY_PATH:
        raise TradingNotConfigured(
            "Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH in .env")
    path = Path(config.KALSHI_PRIVATE_KEY_PATH).expanduser()
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent.parent / path
    if not path.exists():
        raise TradingNotConfigured(f"Kalshi private key not found at {path}")
    _private_key = serialization.load_pem_private_key(
        path.read_bytes(), password=None)
    return _private_key


def _headers(method: str, path: str) -> dict:
    key = _load_key()
    ts = str(int(time.time() * 1000))
    msg = (ts + method + path).encode()
    sig = key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": config.KALSHI_API_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "Content-Type": "application/json",
    }


def _request(method: str, path: str, body: dict = None, params: dict = None):
    # NOTE: the signature covers the path only — including a query string in
    # the signed message returns 401 INCORRECT_API_KEY.
    r = _session.request(
        method, HOST + path,
        headers=_headers(method, path),
        params=params,
        data=json.dumps(body) if body is not None else None,
        timeout=30,
    )
    if r.status_code >= 400:
        try:
            detail = r.json()
        except ValueError:
            detail = r.text[:300]
        raise RuntimeError(f"Kalshi {r.status_code}: {detail}")
    return r.json()


# Kalshi runs several matching engines ("shards") and holds collateral PER
# SHARD, so money on the wrong one cannot fill an order no matter what the
# headline balance says. There is no single "sports" shard: MLB and tennis are
# on 3 while NFL, NCAAF and NBA are on 0, and Kalshi can move a series at any
# time. So the shard is never assumed — it is read from the market itself.
SPORTS_SHARD = 3   # only a default for the manual transfer control
_shard_cache = {}  # ticker -> exchange_index


def market_shard(ticker: str):
    """Which matching shard this market trades on, or None if unreadable."""
    if ticker in _shard_cache:
        return _shard_cache[ticker]
    try:
        m = _request("GET", f"/trade-api/v2/markets/{ticker}").get("market") or {}
        idx = m.get("exchange_index")
        idx = int(idx) if idx is not None else None
    except Exception:
        idx = None
    if idx is not None:
        _shard_cache[ticker] = idx
    return idx


def shard_balances() -> dict:
    """exchange_index -> dollars of collateral sitting on that shard."""
    out = {}
    try:
        bal = _request("GET", "/trade-api/v2/portfolio/balance")
        for row in bal.get("balance_breakdown") or []:
            try:
                out[int(row["exchange_index"])] = round(float(row["balance"]), 2)
            except (KeyError, TypeError, ValueError):
                continue
    except Exception:
        pass
    return out


def status() -> dict:
    """Configured check + balance (also proves the key signs correctly).

    The per-shard breakdown is reported without judging it. An earlier version
    called anything outside shard 3 "stranded", which was wrong the moment the
    board grew past baseball: NFL, NCAAF and NBA markets live on shard 0, so
    that warning told you to move money away from the shard that needed it.
    Whether a shard is funded correctly depends on what you are about to buy,
    so the check now happens at ORDER time, where the market is known.
    """
    try:
        bal = _request("GET", "/trade-api/v2/portfolio/balance")
        shards = {}
        for row in bal.get("balance_breakdown") or []:
            try:
                shards[int(row["exchange_index"])] = round(float(row["balance"]), 2)
            except (KeyError, TypeError, ValueError):
                continue
        total = round(bal.get("balance", 0) / 100.0, 2)
        out = {"configured": True, "balance": total, "shards": shards}
        if total and shards and not any(v > 0 for v in shards.values()):
            out["warning"] = (
                f"${total:.2f} shows as your balance but every matching shard "
                f"reads $0.00 — orders will be rejected until collateral lands.")
        return out
    except TradingNotConfigured as e:
        return {"configured": False, "error": str(e)}
    except Exception as e:
        return {"configured": False, "error": str(e)}


def live_ask(ticker: str):
    """Current best YES ask and its size, straight from the book."""
    try:
        ob = _request("GET", f"/trade-api/v2/markets/{ticker}/orderbook")
    except Exception:
        return None, 0, None
    fp = ob.get("orderbook_fp") or ob.get("orderbook") or {}
    best = None
    for lvl in fp.get("no_dollars") or []:
        try:
            px, qty = float(lvl[0]), float(lvl[1])
        except (TypeError, ValueError, IndexError):
            continue
        if best is None or px > best[0]:
            best = (px, qty)
    if best is None:
        return None, 0, None
    # a NO bid at px is a YES ask at 1-px
    return round(1.0 - best[0], 4), int(best[1]), None


def fillable(ticker: str, max_price: float):
    """(contracts, avg_price) obtainable at or below `max_price` right now.

    Kalshi's book returns bids only: a NO bid at p is a YES ask at 1-p, so
    every NO bid with p >= 1 - max_price is liquidity we can take. Summing
    those levels is what tells a hedger whether the whole leg can fill, rather
    than just the top of book.
    """
    try:
        ob = _request("GET", f"/trade-api/v2/markets/{ticker}/orderbook")
    except Exception:
        return 0, None
    fp = ob.get("orderbook_fp") or ob.get("orderbook") or {}
    levels = []
    for lvl in fp.get("no_dollars") or []:
        try:
            px, qty = float(lvl[0]), float(lvl[1])
        except (TypeError, ValueError, IndexError):
            continue
        ask = round(1.0 - px, 4)
        if ask <= max_price + 1e-9:
            levels.append((ask, qty))
    if not levels:
        return 0, None
    levels.sort()                       # cheapest asks first
    total = sum(q for _, q in levels)
    cost = sum(a * q for a, q in levels)
    return int(total), (cost / total if total else None)


def live_no_ask(ticker: str):
    """Current best price to BUY NO, and its size.

    Kalshi's book is bids-only: the YES bids are what a NO buyer sells into, so
    the best NO ask is 1 - (highest YES bid).
    """
    try:
        ob = _request("GET", f"/trade-api/v2/markets/{ticker}/orderbook")
    except Exception:
        return None, 0
    fp = ob.get("orderbook_fp") or ob.get("orderbook") or {}
    best = None
    for lvl in fp.get("yes_dollars") or []:
        try:
            px, qty = float(lvl[0]), float(lvl[1])
        except (TypeError, ValueError, IndexError):
            continue
        if best is None or px > best[0]:
            best = (px, qty)
    if best is None:
        return None, 0
    return round(1.0 - best[0], 4), int(best[1])


def transfer_to_shard(amount_dollars: float, destination_shard: int = SPORTS_SHARD,
                      source_shard: int = 0) -> dict:
    """Move collateral between exchange shards (amount is in dollars).

    Required before trading a market on a shard your balance isn't on. This is
    a movement of the user's funds, so it is only ever called when the user
    explicitly asks for it.
    """
    if amount_dollars <= 0:
        raise ValueError("amount must be positive")
    centicents = int(round(float(amount_dollars) * 10000))  # $1 = 10,000
    body = {
        "source": "event_contract",
        "destination": "event_contract",
        "amount": centicents,
        "source_exchange_shard": int(source_shard),
        "destination_exchange_shard": int(destination_shard),
    }
    resp = _request("POST", "/trade-api/v2/portfolio/intra_exchange_instance_transfer",
                    body)
    return {"moved": round(amount_dollars, 2),
            "from_shard": source_shard, "to_shard": destination_shard,
            "raw": resp}


def place_order(ticker: str, price: float, contracts: float,
                exchange_index: int = -1, ref_price: float = None,
                ref_age_ms: float = None, buy_no: bool = False) -> dict:
    """Buy YES on `ticker` — priced to actually fill.

    `price` is the ask the UI showed; it is treated as a reference, not the
    limit. The live top of book is re-fetched here because a price from an
    earlier scan can now sit at or below the current bid, where the order just
    rests unfilled. We then bid `ORDER_CROSS_TICKS` through the live ask so the
    order still crosses if the book ticks up in flight — on a CLOB you fill at
    the resting ask, so crossing normally costs nothing. If the market has run
    more than MAX_ORDER_SLIPPAGE above what you saw, the order is refused
    rather than chasing.

    `exchange_index` routes to the right matching shard; anything outside 0-3
    is resolved from the market itself. Kalshi holds collateral per shard, and
    an underfunded shard is rejected with an opaque HTTP 400, so that is
    checked here first with a message naming the shard and the shortfall.

    Uses the V2 endpoint; the legacy /portfolio/orders returns HTTP 410.
    Whole contracts only — rounded to nearest, never truncated.
    """
    shown = float(price)
    if not 0 < shown < 1:
        raise ValueError("price must be between 0.01 and 0.99")
    count = int(round(float(contracts)))
    if count < 1:
        raise ValueError(
            f"contracts must round to at least 1 whole contract (got {contracts})")

    # Fast path: trust a reference price the live feed just fetched, and skip
    # our own book read. Falls through to the read whenever it isn't fresh.
    ask, depth = None, 0
    if (ref_price and ref_age_ms is not None
            and ref_age_ms <= config.ORDER_REF_MAX_AGE_MS
            and 0 < float(ref_price) < 1):
        ask = float(ref_price)
    elif buy_no:
        ask, depth = live_no_ask(ticker)
    else:
        ask, depth, _ = live_ask(ticker)
    if ask is None:
        # Never price off the stale scan — that is how an order ends up resting
        # below the current ask. Fail loudly instead.
        raise ValueError(
            "could not read the live Kalshi order book (no resting offers), so "
            "the order was NOT sent rather than priced off a stale scan.")
    if not config.HEDGE_FILL_MODE and ask > shown + config.MAX_ORDER_SLIPPAGE:
        # price-priority mode only: refusing is safe when nothing is riding on it
        raise ValueError(
            f"market moved — best ask is now {ask * 100:.0f}c vs the "
            f"{shown * 100:.0f}c shown (limit {config.MAX_ORDER_SLIPPAGE * 100:.0f}c). "
            f"Rescan and try again rather than chasing it.")

    # Fill priority: bid far enough through the book to sweep it. Kalshi V2 has
    # no market order (price is required), so an aggressive limit IS the market
    # order — and a CLOB fills you at each resting level's price, so this is a
    # ceiling, not what you pay.
    cross = (config.HEDGE_MAX_SLIPPAGE_CENTS if config.HEDGE_FILL_MODE
             else config.ORDER_CROSS_CENTS) / 100.0
    limit = ask + cross                       # what we are willing to PAY
    if buy_no:
        # Buying NO is expressed as SELLING YES: paying `limit` for NO means
        # selling YES at 1 - limit, and paying more for NO means selling YES
        # lower. Floor at 1c so the order stays valid.
        cents = min(99, max(1, math.floor(round((1.0 - limit) * 100, 6))))
    else:
        cents = min(99, max(1, math.ceil(round(limit * 100, 6))))
        if cents < round(ask * 100):
            raise ValueError(
                f"internal: computed limit {cents}c below live ask "
                f"{ask * 100:.0f}c; not sent")
    # Which shard this market matches on, and whether the money is there.
    # Kalshi's own rejection for an underfunded shard is an opaque HTTP 400,
    # which says nothing about WHICH shard or how much is missing, so the check
    # happens here where both are known. Never assumed from the sport: MLB and
    # tennis sit on shard 3, NFL/NCAAF/NBA on shard 0, and Kalshi can move a
    # series whenever it likes.
    idx = int(exchange_index)
    if not 0 <= idx <= 3:
        looked_up = market_shard(ticker)
        if looked_up is not None:
            idx = looked_up
    if 0 <= idx <= 3:
        need = round(count * (cents / 100.0), 2)
        have = shard_balances().get(idx)
        if have is not None and have + 1e-9 < need:
            other = {k: v for k, v in shard_balances().items() if k != idx and v > 0}
            extra = (" — you have "
                     + ", ".join(f"${v:,.2f} on shard {k}" for k, v in sorted(other.items()))
                     + f", which can be moved to shard {idx}") if other else ""
            raise ValueError(
                f"not enough collateral on Kalshi shard {idx}, where {ticker} "
                f"trades: this order needs ${need:,.2f} and that shard holds "
                f"${have:,.2f}{extra}.")

    body = {
        "ticker": ticker,
        "client_order_id": str(uuid.uuid4()),
        "side": "ask" if buy_no else "bid",         # ask = sell YES = buy NO
        "count": f"{count}.00",                     # fixed-point contracts
        "price": f"{cents / 100:.4f}",              # fixed-point dollars
        # GTC: sweep what is available now and leave any remainder working
        # above the market, so a hedge keeps trying to complete itself.
        "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
        # -1 is Kalshi's auto-route sentinel and is accepted; any OTHER
        # out-of-range value is rejected with a 400, so it is the only fallback.
        "exchange_index": idx if 0 <= idx <= 3 else -1,
    }
    resp = _request("POST", "/trade-api/v2/portfolio/events/orders", body)
    try:
        filled = float(resp.get("fill_count") or 0)
        resting = float(resp.get("remaining_count") or 0)
    except (TypeError, ValueError):
        filled = resting = 0.0
    if filled and not resting:
        status_txt = f"FILLED {filled:g}"
    elif filled:
        status_txt = (f"PARTIAL: filled {filled:g} of {count}, "
                      f"{resting:g} STILL RESTING — you are short that much hedge")
    else:
        status_txt = (f"NOT FILLED — all {count} resting at {cents}c; "
                      f"your other leg is unhedged")
    side_txt = "NO" if buy_no else "YES"
    detail = (f"buy {count} {side_txt} {ticker} — "
              f"{'sell-YES limit' if buy_no else 'limit'} {cents}c "
              f"({side_txt} ask {ask * 100:.0f}c + {cross * 100:.0f}c sweep)")
    if depth:
        detail += f" x{depth:,}"
    return {
        "venue": "kalshi",
        "order_id": resp.get("order_id"),
        "status": status_txt,
        "detail": detail,
    }


def warm():
    """Open/refresh the pooled TLS connection so the next order skips the
    handshake. Cheap, unauthenticated, safe to call on a timer."""
    try:
        _session.get(HOST + "/trade-api/v2/exchange/status", timeout=10)
        return True
    except Exception:
        return False


_DEAD = ("canceled", "cancelled", "rejected", "expired")


def order_status(order_id: str, requested: float = None) -> dict:
    """Live fill state for one order, so a fill can be CONFIRMED rather than
    assumed. `terminal` means there is no point polling again."""
    o = _request("GET", f"/trade-api/v2/portfolio/orders/{order_id}").get("order", {})

    def num(*keys):
        for k in keys:
            v = o.get(k)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        return None

    filled = num("fill_count_fp", "fill_count") or 0.0
    remaining = num("remaining_count_fp", "remaining_count")
    status = (o.get("status") or "").lower()
    want = requested if requested is not None else (
        filled + (remaining or 0.0))
    done = (remaining == 0 and filled > 0) or (want and filled >= want - 1e-9)
    return {"venue": "kalshi", "order_id": order_id, "status": status,
            "filled": filled, "remaining": remaining, "requested": want,
            "terminal": bool(done or any(d in status for d in _DEAD))}
