"""Local web UI for the odds scanner.

    python3 -m scanner.server        # then open http://localhost:8765

Endpoints:
    GET  /           the UI
    GET  /api/last   last scan results (cached, costs nothing)
    POST /api/scan   run a fresh scan (1 Odds API credit per enabled sport)
"""
import json
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import budget, config, props
from .scan import (fresh_sportsbook_price, log_scan, refresh_depth,
                   refresh_exchange_quotes, scan_all)
from .trading import kalshi_trader, polymarket_us_trader

PORT = 8765
STATIC = Path(__file__).resolve().parent / "static"
LAST_SCAN_FILE = config.DATA_DIR / "last_scan.json"


def serialize(games, quota, now):
    return {
        "scanned_at": now.isoformat(),
        "credits_remaining": quota.get("remaining"),
        "games": [{
            "sport": g.sport,
            # individual sports read "A vs B"; team sports "away @ home"
            "sep": ("vs" if getattr(config.SPORTS.get(g.sport), "match_mode", "alias")
                    == "name" else "@"),
            "away": g.away,
            "home": g.home,
            "start": g.start.astimezone(timezone.utc).isoformat(),
            "quotes": [{
                "book": q.book,
                "team": q.team,
                "prob": round(q.prob, 5),
                "american": q.american,
                "detail": q.detail,
                "meta": q.meta,
            } for q in g.quotes],
        } for g in games],
    }


def _merge_scan(payload, scanned_sports):
    """Fold a scoped scan back into the full board.

    Auto-refresh only pays for the sport on screen, so the other sports' games
    have to survive from the previous scan — otherwise switching tabs would
    show an empty table until you paid for it again.
    """
    try:
        prev = json.loads(LAST_SCAN_FILE.read_text()).get("games") or []
    except Exception:
        return payload
    keep = [g for g in prev if g.get("sport") not in set(scanned_sports)]
    payload["games"] = sorted(keep + payload["games"], key=lambda g: g["start"])
    return payload


class Handler(BaseHTTPRequestHandler):

    def _send(self, code, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode())

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, (STATIC / "index.html").read_bytes(),
                       "text/html; charset=utf-8")
        elif self.path == "/api/last":
            if LAST_SCAN_FILE.exists():
                self._send(200, LAST_SCAN_FILE.read_bytes())
            else:
                self._send_json({"games": None})
        elif self.path == "/api/refresh":
            # Free exchange re-poll (Kalshi + Polymarket US). No Odds API
            # credits, no order-book depth — safe to call every second.
            try:
                if not LAST_SCAN_FILE.exists():
                    self._send_json({"error": "scan first"}, 400)
                    return
                cached = json.loads(LAST_SCAN_FILE.read_text()).get("games") or []
                want = self.headers.get("X-Sports")
                games = refresh_exchange_quotes(
                    cached, [s for s in (want or "").split(",") if s] or None)
                self._send_json({
                    "refreshed_at": datetime.now(timezone.utc).isoformat(),
                    "games": games,
                })
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        elif self.path == "/api/budget":
            self._send_json(budget.status())
        elif self.path == "/api/trading/status":
            self._send_json({
                "kalshi": kalshi_trader.status(),
                "polymarket_us": polymarket_us_trader.status(),
            })
        else:
            self._send_json({"error": "not found"}, 404)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_POST(self):
        if self.path == "/api/scan":
            # The ONLY endpoint that spends money. Every call goes through the
            # budget guards, whether it came from the Refresh button or the
            # 1/second auto loop.
            try:
                req = self._read_body()
                want = [s for s in (req.get("sports") or []) if s in config.SPORTS]
                auto = bool(req.get("auto"))
                budget.gate(estimate=len(want) or len(config.ENABLED_SPORTS),
                            auto=auto)
                # Odds API only — exchange prices arrive from /api/refresh
                games, quota, now = scan_all(include_exchanges=False,
                                             sports=want or None)
                budget.note_spend(quota, estimate=len(want) or
                                  len(config.ENABLED_SPORTS))
                if not auto:
                    log_scan(games, now)   # history, not worth 1/s of disk
                payload = serialize(games, quota, now)
                if want:
                    payload = _merge_scan(payload, want)
                config.DATA_DIR.mkdir(exist_ok=True)
                LAST_SCAN_FILE.write_text(json.dumps(payload))
                payload["budget"] = budget.status()
                self._send_json(payload)
            except budget.BudgetError as e:
                self._send_json({"error": str(e), "budget": budget.status(),
                                 "blocked": True}, 429)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        elif self.path == "/api/trade":
            try:
                req = self._read_body()
                venue = req.get("venue")
                price = float(req["price"])
                contracts = float(req["contracts"])
                ref_price = req.get("ref_price")
                ref_age = req.get("ref_age_ms")
                if venue == "kalshi":
                    result = kalshi_trader.place_order(
                        ticker=req["ticker"], price=price,
                        contracts=contracts,
                        exchange_index=int(req.get("exchange_index", -1)),
                        ref_price=ref_price, ref_age_ms=ref_age,
                        buy_no=bool(req.get("buy_no")))
                elif venue == "polymarket_us":
                    result = polymarket_us_trader.place_order(
                        market_slug=req["market_slug"], price=price,
                        contracts=contracts,
                        long=bool(req.get("long", True)),
                        ref_price=ref_price, ref_age_ms=ref_age)
                else:
                    raise ValueError(f"unknown venue {venue!r}")
                self._send_json(result)
            except (KeyError, ValueError) as e:
                self._send_json({"error": f"bad request: {e}"}, 400)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        elif self.path == "/api/quick_trade":
            # Instant buy. Re-reads the sportsbook line the hedge is sized
            # against so the contract count matches the CURRENT price, then
            # places the exchange order — one client round trip for both.
            try:
                req = self._read_body()
                venue = req.get("venue")
                amount = float(req.get("amount") or 0)
                safety = bool(req.get("safety", True))
                contracts = float(req.get("fallback_contracts") or 0)
                sized = {"mode": "outright" if not safety else "hedge"}

                if safety:
                    if amount <= 0:
                        raise ValueError("amount must be positive")
                    other = req["other_team"]
                    prob, american, book, quota, age = fresh_sportsbook_price(
                        req.get("sport", "mlb"), req["away"], req["home"], other)
                    # counted against the day, deliberately not gated: a budget
                    # cap must never be the reason a hedge leg goes unfilled
                    if age is not None and age < 0.5:
                        budget.note_spend(quota, estimate=1)
                    boost = float(req.get("boost_pct") or 0)
                    if prob and boost:
                        # a profit boost lifts the profit portion, so the leg
                        # pays more and the hedge must be sized larger
                        d = 1.0 / prob
                        prob = 1.0 / (1.0 + (d - 1.0) * (1.0 + boost / 100.0))
                        sized["boost_pct"] = boost
                    if prob:
                        contracts = amount / prob
                        sized.update({"book": book, "american": american,
                                      "prob": round(prob, 5),
                                      "quote_age_s": round(age, 2),
                                      "credits_remaining": quota.get("remaining")})
                    elif contracts > 0:
                        # never block a hedge because the line vanished from the
                        # feed (common once a game is well underway)
                        sized.update({"book": None,
                                      "note": "no live sportsbook line for "
                                              f"{other}; used the on-screen size"})
                    else:
                        raise ValueError(
                            f"no sportsbook price for {other} and no fallback size")

                if contracts <= 0:
                    raise ValueError("could not determine a size")

                ref_price = req.get("ref_price")
                ref_age = req.get("ref_age_ms")
                if venue == "kalshi":
                    result = kalshi_trader.place_order(
                        ticker=req["ticker"], price=float(req["price"]),
                        contracts=contracts,
                        exchange_index=int(req.get("exchange_index", -1)),
                        ref_price=ref_price, ref_age_ms=ref_age,
                        buy_no=bool(req.get("buy_no")))
                elif venue == "polymarket_us":
                    result = polymarket_us_trader.place_order(
                        market_slug=req["market_slug"], price=float(req["price"]),
                        contracts=contracts,
                        long=bool(req.get("long", True)),
                        ref_price=ref_price, ref_age_ms=ref_age)
                else:
                    raise ValueError(f"unknown venue {venue!r}")
                result["sized"] = sized
                result["contracts"] = round(contracts, 4)
                self._send_json(result)
            except (KeyError, ValueError) as e:
                self._send_json({"error": f"bad request: {e}"}, 400)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        elif self.path == "/api/order_status":
            # Confirm a fill instead of assuming one. Cheap and idempotent, so
            # the UI can poll it for a few seconds after placing.
            try:
                req = self._read_body()
                venue = req.get("venue")
                oid = req["order_id"]
                want = req.get("requested")
                want = float(want) if want is not None else None
                if venue == "kalshi":
                    self._send_json(kalshi_trader.order_status(oid, want))
                elif venue == "polymarket_us":
                    self._send_json(polymarket_us_trader.order_status(oid, want))
                else:
                    raise ValueError(f"unknown venue {venue!r}")
            except (KeyError, ValueError) as e:
                self._send_json({"error": f"bad request: {e}"}, 400)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        elif self.path == "/api/scan_props":
            # 1+ home run scanner. Costs 1 Odds API credit PER GAME (the
            # sportsbook half is only on the per-event endpoint), so the caller
            # says how many games to cover.
            try:
                req = self._read_body()
                n = req.get("max_events")
                top = req.get("top_per_game")
                budget.gate(estimate=int(n or 5), auto=False)
                rows, quota = props.scan(
                    max_events=int(n) if n else None,
                    top_per_game=int(top) if top else None)
                budget.note_spend(quota, estimate=int(quota.get("credits_spent") or 0))
                # same payload shape as /api/scan so the UI renders it with the
                # ordinary table, ticket and hedge calculator
                self._send_json({
                    "scanned_at": datetime.now(timezone.utc).isoformat(),
                    "credits_remaining": quota.get("remaining"),
                    "games": props.to_games(rows),
                    "quota": quota,
                })
            except budget.BudgetError as e:
                self._send_json({"error": str(e), "budget": budget.status(),
                                 "blocked": True}, 429)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        elif self.path == "/api/depth":
            # BBO size for a handful of named markets (free; no credits).
            try:
                req = self._read_body()
                self._send_json(refresh_depth(
                    req.get("kalshi_tickers") or [],
                    req.get("polymarket_slugs") or []))
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        elif self.path == "/api/auto":
            # The heartbeat, which is both the dead-man switch and the thing
            # that enables spending. The UI beats only while its tab is visible
            # AND recently touched, so walking away, hiding the tab, sleeping
            # the laptop or closing the browser all stop the spend on their own
            # within a few seconds — there is no switch to forget.
            try:
                req = self._read_body()
                action = req.get("action")
                sports = [s for s in (req.get("sports") or []) if s in config.SPORTS]
                if action == "beat":
                    # also what turns spending on — see budget.beat
                    self._send_json(budget.beat(sports or None))
                elif action == "stop":
                    self._send_json(budget.disarm(req.get("reason") or "stopped"))
                else:
                    raise ValueError(f"unknown action {action!r}")
            except budget.BudgetError as e:
                self._send_json({"error": str(e), "budget": budget.status()}, 429)
            except (KeyError, ValueError) as e:
                self._send_json({"error": f"bad request: {e}"}, 400)
        elif self.path == "/api/kalshi/transfer":
            # Moves the user's own collateral between Kalshi matching shards.
            # Only ever reached by an explicit click in the UI.
            try:
                req = self._read_body()
                result = kalshi_trader.transfer_to_shard(
                    amount_dollars=float(req["amount"]),
                    destination_shard=int(req.get("to_shard",
                                                  kalshi_trader.SPORTS_SHARD)),
                    source_shard=int(req.get("from_shard", 0)))
                self._send_json(result)
            except (KeyError, ValueError) as e:
                self._send_json({"error": f"bad request: {e}"}, 400)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        else:
            self._send_json({"error": "not found"}, 404)

    def log_message(self, fmt, *args):
        print(f"  {self.address_string()} {fmt % args}")


def _keep_connections_warm(period=25):
    """Hold the traders' TLS connections open so a click never pays a
    handshake. Two trivial unauthenticated GETs per cycle."""
    def loop():
        while True:
            for trader in (kalshi_trader, polymarket_us_trader):
                try:
                    trader.warm()
                except Exception:
                    pass
            time.sleep(period)
    t = threading.Thread(target=loop, daemon=True)
    t.start()


def main():
    if not config.ODDS_API_KEY:
        raise SystemExit("ODDS_API_KEY not set (check your .env)")
    _keep_connections_warm()
    budget.watchdog()      # expires an armed run even if nothing calls in
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Odds scanner UI on http://localhost:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
