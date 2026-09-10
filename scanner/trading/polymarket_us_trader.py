"""Polymarket US trading via the Retail API (api.polymarket.us).

Auth is Ed25519: sign "<timestamp_ms><METHOD><path>" with the secret key and
send X-PM-Access-Key / X-PM-Timestamp / X-PM-Signature. No wallet private key
and no .pem file — those belong to Kalshi and the offshore polymarket.com CLOB
respectively.

Get credentials: KYC in the Polymarket US iOS app, then create a key at
polymarket.us/developer. The secret is shown only once.

    POLYMARKET_KEY_ID       the Key ID
    POLYMARKET_SECRET_KEY   the Secret Key (base64; first 32 bytes are the seed)

Markets are whole-contract only (the events feed's minimumTradeQty of 0.01 is
misleading — the exchange executes whole contracts), and a binary instrument's
two sides share one marketSlug: the long side is YES, the short side is NO.

CRITICAL: the `price` on an order **always refers to the YES side**, whichever
outcome you are buying. Polymarket's own docs: "If you want to buy NO at $0.40,
you're really selling YES at $0.60." So a NO order must send `1 - no_price`.
Sending the NO price directly puts the order far from the market and it simply
rests unfilled — which is exactly what happened before this was fixed.
"""
import base64
import json
import time

import requests
from cryptography.hazmat.primitives.asymmetric import ed25519

import math

import requests

from .. import config

HOST = "https://api.polymarket.us"
GATEWAY = "https://gateway.polymarket.us"          # public market data

# Pooled keep-alive sessions: a cold TLS handshake here costs ~160ms versus
# ~72ms warm, and order placement is on the hot path.
def _mk_session():
    s = requests.Session()
    s.mount("https://", requests.adapters.HTTPAdapter(
        pool_connections=4, pool_maxsize=8, max_retries=0))
    return s


_session = _mk_session()      # authenticated order API
_gw_session = _mk_session()   # public market data


def live_quote(market_slug: str, long: bool):
    """Current cost of the given side at the top of book, plus its size.

    long  side (YES) -> buy at the best offer
    short side (NO)  -> sell YES into the best bid, i.e. cost 1 - bestBid
    """
    try:
        r = _gw_session.get(f"{GATEWAY}/v1/markets/{market_slug}/book", timeout=20)
        r.raise_for_status()
        md = r.json().get("marketData", {})
    except Exception:
        return None, 0
    levels = (md.get("offers") or []) if long else (md.get("bids") or [])
    if not levels:
        return None, 0
    top = levels[0]
    px = top.get("px")
    if isinstance(px, dict):
        px = px.get("value")
    try:
        px, qty = float(px), float(top.get("qty") or 0)
    except (TypeError, ValueError):
        return None, 0
    return (px if long else round(1.0 - px, 6)), int(qty)

_signer = None
_signer_key_id = None


class TradingNotConfigured(Exception):
    pass


def _get_signer():
    """Build (and cache) the Ed25519 signer for the configured secret."""
    global _signer, _signer_key_id
    config.reload_env()  # pick up .env edits without a server restart
    key_id = config.POLYMARKET_KEY_ID
    secret = config.POLYMARKET_SECRET_KEY
    if not key_id or not secret:
        raise TradingNotConfigured(
            "Set POLYMARKET_KEY_ID and POLYMARKET_SECRET_KEY in .env — create "
            "them at polymarket.us/developer after verifying your identity in "
            "the Polymarket US app. No wallet private key or .pem is used.")
    if _signer is not None and _signer_key_id == key_id:
        return key_id, _signer
    try:
        raw = base64.b64decode(secret.strip(), validate=False)
    except Exception:
        raise TradingNotConfigured(
            "POLYMARKET_SECRET_KEY is not valid base64 — paste the Secret Key "
            "exactly as shown at polymarket.us/developer.")
    if len(raw) < 32:
        raise TradingNotConfigured(
            f"POLYMARKET_SECRET_KEY decodes to {len(raw)} bytes; expected at "
            f"least 32. Paste the full Secret Key from polymarket.us/developer.")
    _signer = ed25519.Ed25519PrivateKey.from_private_bytes(raw[:32])
    _signer_key_id = key_id
    return key_id, _signer


def _headers(method: str, path: str) -> dict:
    key_id, signer = _get_signer()
    ts = str(int(time.time() * 1000))
    sig = signer.sign(f"{ts}{method}{path}".encode())
    return {
        "X-PM-Access-Key": key_id,
        "X-PM-Timestamp": ts,
        "X-PM-Signature": base64.b64encode(sig).decode(),
        "Content-Type": "application/json",
    }


def _request(method: str, path: str, body: dict = None):
    r = _session.request(
        method, HOST + path,
        headers=_headers(method, path),
        data=json.dumps(body) if body is not None else None,
        timeout=30,
    )
    if r.status_code >= 400:
        try:
            detail = r.json()
            detail = detail.get("message") or detail
        except ValueError:
            detail = r.text[:300]
        raise RuntimeError(f"Polymarket US {r.status_code}: {detail}")
    return r.json()


def status() -> dict:
    try:
        resp = _request("GET", "/v1/account/balances")
        bals = resp.get("balances") or resp.get("userBalances") or []
        usd = next((b for b in bals if (b.get("currency") or "USD") == "USD"),
                   bals[0] if bals else {})
        power = usd.get("buyingPower", usd.get("currentBalance", 0)) or 0
        return {"configured": True, "balance": round(float(power), 2)}
    except TradingNotConfigured as e:
        return {"configured": False, "error": str(e)}
    except Exception as e:
        return {"configured": False, "error": str(e)}


TICK = 0.005


def fillable(market_slug: str, long: bool, max_price: float):
    """(contracts, avg_price) obtainable at or below `max_price` right now.

    Sums every book level we could take, not just the touch — that is what
    tells a hedger whether the whole leg can actually fill.
    """
    try:
        r = _gw_session.get(f"{GATEWAY}/v1/markets/{market_slug}/book", timeout=20)
        r.raise_for_status()
        md = r.json().get("marketData", {})
    except Exception:
        return 0, None
    raw = (md.get("offers") or []) if long else (md.get("bids") or [])
    levels = []
    for lvl in raw:
        px = lvl.get("px")
        if isinstance(px, dict):
            px = px.get("value")
        try:
            px, qty = float(px), float(lvl.get("qty") or 0)
        except (TypeError, ValueError):
            continue
        cost = px if long else round(1.0 - px, 6)   # buying NO = selling YES
        if cost <= max_price + 1e-9:
            levels.append((cost, qty))
    if not levels:
        return 0, None
    levels.sort()
    total = sum(q for _, q in levels)
    spend = sum(c * q for c, q in levels)
    return int(total), (spend / total if total else None)


def place_order(market_slug: str, price: float, contracts: float,
                long: bool = True, ref_price: float = None,
                ref_age_ms: float = None) -> dict:
    """Limit-buy `contracts` on one side of `market_slug`, priced to fill.

    long=True buys YES (the instrument's long side); long=False buys NO.

    `price` is the quote the UI showed and is only a reference: the live top of
    book is re-fetched here, because a stale price can land at or below the
    current bid and simply rest unfilled. We then cross by
    ORDER_CROSS_TICKS ticks so the order fills even if the book ticks away;
    resting liquidity sets the actual fill price, so crossing normally costs
    nothing. Refused if the market has moved more than MAX_ORDER_SLIPPAGE.

    Polymarket US executes whole contracts only — rounded, never truncated.
    """
    shown = float(price)
    if not 0 < shown < 1:
        raise ValueError("price must be between 0 and 1")
    qty = int(round(float(contracts)))
    if qty < 1:
        raise ValueError(
            f"contracts must round to at least 1 whole contract (got {contracts})")

    # Fast path: trust a reference price the live feed just fetched, and skip
    # our own book read. Falls through to the read whenever it isn't fresh.
    live, depth = None, 0
    if (ref_price and ref_age_ms is not None
            and ref_age_ms <= config.ORDER_REF_MAX_AGE_MS
            and 0 < float(ref_price) < 1):
        live = float(ref_price)
    else:
        live, depth = live_quote(market_slug, long)
    if live is None:
        # Never price off the stale scan — that is how an order ends up resting
        # below the current ask. Fail loudly instead.
        raise ValueError(
            "could not read the live Polymarket US order book, so the order "
            "was NOT sent (refusing to price it off a stale scan). Try again.")
    if not config.HEDGE_FILL_MODE and live > shown + config.MAX_ORDER_SLIPPAGE:
        # price-priority mode only: never refuse when a hedge is riding on it
        raise ValueError(
            f"market moved — this side now costs {live * 100:.1f}c vs the "
            f"{shown * 100:.1f}c shown (limit {config.MAX_ORDER_SLIPPAGE * 100:.0f}c). "
            f"Rescan and try again rather than chasing it.")

    common = {
        "marketSlug": market_slug,
        "quantity": qty,
        "outcomeSide": "OUTCOME_SIDE_YES" if long else "OUTCOME_SIDE_NO",
        "action": "ORDER_ACTION_BUY",
        "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_MANUAL",
    }
    slip = config.HEDGE_MAX_SLIPPAGE_CENTS / 100.0

    cross = (slip if config.HEDGE_FILL_MODE
             else config.ORDER_CROSS_CENTS / 100.0)
    # `pay` is what we are willing to pay for the side we are buying.
    pay = min(0.995, live + cross)

    # Translate to a YES-denominated order price. Buying YES: that IS the
    # price, rounded UP a tick so it can't land below the ask. Buying NO:
    # paying more for NO means selling YES LOWER, so it is 1 - pay, rounded
    # DOWN a tick for the same reason.
    if long:
        wire = round(math.ceil(round(pay / TICK, 6)) * TICK, 4)
        if wire < live:
            raise ValueError(
                f"internal: YES limit {wire} below live ask {live}; not sent")
    else:
        wire = round(math.floor(round((1.0 - pay) / TICK, 6)) * TICK, 4)
        if wire > 1.0 - live:
            raise ValueError(
                f"internal: NO order priced at YES {wire}, which pays only "
                f"{1 - wire:.4f} for NO vs a {live:.4f} ask; not sent")
    wire = min(0.999, max(0.001, wire))

    body = dict(common, type="ORDER_TYPE_LIMIT",
                price={"value": f"{wire:.4f}", "currency": "USD"},
                tif="TIME_IN_FORCE_GOOD_TILL_CANCEL")
    how = (f"{'buy YES' if long else 'buy NO'} up to "
           f"{pay * 100:.1f}c — sent as YES price {wire * 100:.1f}c")
    resp = _request("POST", "/v1/orders", body)
    order = resp.get("order") or resp
    try:
        filled = float(order.get("filledQuantity") or order.get("fillCount") or 0)
    except (TypeError, ValueError):
        filled = 0.0
    remaining = max(0.0, qty - filled)
    if filled and not remaining:
        status_txt = f"FILLED {filled:g}"
    elif filled:
        status_txt = (f"PARTIAL: filled {filled:g} of {qty}, {remaining:g} "
                      f"NOT filled — you are short that much hedge")
    else:
        status_txt = order.get("status") or "submitted"
    return {
        "venue": "polymarket_us",
        "order_id": order.get("orderId") or order.get("id"),
        "status": status_txt,
        "detail": (f"buy {qty} {'YES' if long else 'NO'} {market_slug} — {how}, "
                   f"live {live * 100:.1f}c"),
    }


def warm():
    """Keep both pooled TLS connections open so the next order skips the
    handshake."""
    ok = False
    for sess, url in ((_gw_session, f"{GATEWAY}/v1/health"),
                      (_session, f"{HOST}/v1/health")):
        try:
            sess.get(url, timeout=10)
            ok = True
        except Exception:
            pass
    return ok


_DEAD = ("CANCEL", "REJECT", "EXPIRE", "DONE_FOR_DAY")


def order_status(order_id: str, requested: float = None) -> dict:
    """Live fill state for one order (cumQuantity filled / leavesQuantity
    remaining), so a fill can be CONFIRMED rather than assumed."""
    resp = _request("GET", f"/v1/order/{order_id}")
    o = resp.get("order") or resp

    def num(key):
        v = o.get(key)
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    filled = num("cumQuantity") or 0.0
    remaining = num("leavesQuantity")
    want = requested if requested is not None else (num("quantity") or
                                                    filled + (remaining or 0.0))
    state = str(o.get("state") or o.get("status") or "").upper()
    done = (remaining == 0 and filled > 0) or (want and filled >= want - 1e-9)
    return {"venue": "polymarket_us", "order_id": order_id, "status": state,
            "filled": filled, "remaining": remaining, "requested": want,
            "terminal": bool(done or any(d in state for d in _DEAD))}
