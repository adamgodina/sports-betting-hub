"""Cross-book odds scanner: DraftKings / FanDuel / Kalshi / Polymarket US.

Usage:
    python3 -m scanner.scan            # one scan
    python3 -m scanner.scan --loop     # scan continuously, pacing itself so the
                                       # Odds API free tier lasts the whole month

Only The Odds API costs credits (1 per enabled sport per scan). Kalshi and
Polymarket US market data come from their free public APIs.
"""
import argparse
import csv
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config
from .compare import MATCH_TOLERANCE, analyze, match_games
from .roster import Roster
from .sources import kalshi, oddsapi, polymarket_us

LOCAL_TZ = ZoneInfo(config.LOCAL_TZ)


# ---------------------------------------------------------------- scanning

def scan_sport(sport_cfg, with_depth: bool = False,
               include_exchanges: bool = True):
    """Scan one sport.

    include_exchanges=False fetches ONLY the Odds API — the single
    credit-spending call, and the only data the exchanges' own free feeds don't
    already keep current. That is what the UI's Refresh button uses: Kalshi and
    Polymarket US prices arrive continuously from the 1s poll, so re-fetching
    them here would just add latency (~29 of ~31 requests, most of the time).

    with_depth only matters when exchanges are included (terminal scans).
    """
    oa_events, quota = oddsapi.fetch(sport_cfg)
    # The Odds API is the spine, so its team names are the canonical roster the
    # exchanges get resolved onto (no hand-written alias table needed).
    roster = Roster(oa_events)
    k_events, pm_events = [], []
    if include_exchanges:
        try:
            k_events = kalshi.fetch(sport_cfg, with_depth=with_depth, roster=roster)
        except Exception as e:
            print(f"  [kalshi] fetch failed: {e}")
        try:
            pm_events = polymarket_us.fetch(sport_cfg, with_depth=with_depth,
                                            roster=roster)
        except Exception as e:
            print(f"  [polymarket_us] fetch failed: {e}")
    games = match_games(sport_cfg.key, oa_events, k_events, pm_events,
                        tolerance=timedelta(
                            hours=getattr(sport_cfg, "match_tolerance_hours", 1.5)))
    return games, quota


def scan_all(with_depth: bool = False, include_exchanges: bool = True,
             sports=None):
    """Scan the enabled sports. Returns (games, quota, scanned_at).

    `sports` narrows it to a subset — the UI passes the sport actually on
    screen, so a refresh costs 1 credit instead of one per enabled sport.
    That 7x matters at a 1/second cadence.
    """
    now = datetime.now(timezone.utc)
    quota = {}
    all_games = []
    keys = [k for k in (sports or config.ENABLED_SPORTS)
            if k in config.SPORTS] or config.ENABLED_SPORTS
    for key in keys:
        sport_cfg = config.SPORTS[key]
        games, quota = scan_sport(sport_cfg, with_depth=with_depth,
                                  include_exchanges=include_exchanges)
        all_games.extend(games)
    return all_games, quota, now


def refresh_exchange_quotes(cached_games, sports=None):
    """Re-poll ONLY the free exchange APIs and match them to a previous scan.

    Costs no Odds API credits and takes 2 HTTP requests per sport (prices only,
    no order-book depth), so it is cheap enough to run every second. The
    sportsbook legs of each game are left untouched — they come from the last
    real scan.

    `cached_games` is the serialized game list from the last scan; returns a
    list of {away, home, start, quotes} carrying only exchange quotes.
    """
    # Only poll the sports actually on screen: each one is a separate series
    # and league call per venue, so polling every enabled sport every second
    # multiplies the request rate for data nobody is looking at.
    keys = [k for k in (sports or config.ENABLED_SPORTS)
            if k in config.SPORTS] or config.ENABLED_SPORTS
    fresh = []
    for key in keys:
        sport_cfg = config.SPORTS[key]
        # Roster-matched sports need the canonical names to resolve against;
        # the poll has no spine of its own, so reuse the cached scan's.
        roster = Roster.from_games([g for g in cached_games
                                    if g.get("sport") == key]) \
            if getattr(sport_cfg, "match_mode", "alias") == "roster" else None
        for src in (kalshi, polymarket_us):
            try:
                fresh.extend(src.fetch(sport_cfg, with_depth=False, roster=roster))
            except Exception as e:
                print(f"  [{src.__name__.rsplit('.', 1)[-1]}] poll failed: {e}")

    out = []
    for g in cached_games:
        teams = frozenset({g["away"], g["home"]})
        try:
            start = datetime.fromisoformat(g["start"])
        except (KeyError, ValueError):
            continue
        quotes = []
        for ev in fresh:
            if ev.teams != teams:
                continue
            gcfg = config.SPORTS.get(g.get("sport")) or sport_cfg
            tol = timedelta(hours=getattr(gcfg, "match_tolerance_hours", 1.5))
            if abs(ev.start - start) > tol:
                continue
            for q in ev.quotes:
                quotes.append({
                    "book": q.book, "team": q.team,
                    "prob": round(q.prob, 5), "american": q.american,
                    "detail": q.detail, "meta": q.meta,
                })
        if quotes:
            # `sport` is carried so the UI can key a game the same way here as
            # in a full scan; without it the two snapshots cannot be lined up
            # unambiguously.
            out.append({"sport": g.get("sport"), "away": g["away"],
                        "home": g["home"], "start": g["start"],
                        "quotes": quotes})
    return out


# --- fresh sportsbook line, cached briefly, for sizing a hedge ---
_book_cache = {"at": 0.0, "events": None, "quota": {}}


def fresh_sportsbook_price(sport_key, away, home, team):
    """Current best sportsbook price for `team` in that game.

    Returns (prob, american, book, quota, age_s) or (None, ...) if the game or
    team isn't in the feed. Costs 1 Odds API credit per uncached read; results
    are reused for config.HEDGE_QUOTE_TTL_S seconds.
    """
    now = time.time()
    age = now - _book_cache["at"]
    if _book_cache["events"] is None or age > config.HEDGE_QUOTE_TTL_S:
        sport_cfg = config.SPORTS[sport_key]
        events, quota = oddsapi.fetch(sport_cfg)
        _book_cache.update({"at": now, "events": events, "quota": quota})
        age = 0.0
    for ev in _book_cache["events"] or []:
        if {ev.away, ev.home} != {away, home}:
            continue
        best = None
        for q in ev.quotes:
            if q.team == team and (best is None or q.prob < best.prob):
                best = q
        if best:
            return best.prob, best.american, best.book, _book_cache["quota"], age
    return None, None, None, _book_cache["quota"], age


def refresh_depth(kalshi_tickers, polymarket_slugs):
    """Resting size at the BBO for SPECIFIC markets only.

    This is the expensive half of a scan (Polymarket needs one order-book call
    per game), so the UI asks for it on just the few games it cares about, on a
    slower cadence than the price poll. Still free — no Odds API credits.
    """
    out = {"kalshi": {}, "polymarket_us": {}}

    if kalshi_tickers:
        try:
            for ticker, bk in kalshi.fetch_books(kalshi_tickers).items():
                out["kalshi"][ticker] = {
                    "ask": bk[0], "depth": bk[1],
                    "sweep": bk[2] if len(bk) > 2 else None}
        except Exception as e:
            print(f"  [kalshi] depth refresh failed: {e}")

    if polymarket_slugs:
        try:
            out["polymarket_us"] = polymarket_us.fetch_books(polymarket_slugs)
        except Exception as e:
            print(f"  [polymarket_us] depth refresh failed: {e}")
    return out


def run_scan():
    all_games, quota, now = scan_all(with_depth=True, include_exchanges=True)
    report(all_games, now, quota)
    log_scan(all_games, now)
    return quota


# ---------------------------------------------------------------- reporting

def _fmt_side(game, team):
    quotes = game.books_for(team)
    if not quotes:
        return f"{team:<22} (no quotes)"
    best = quotes[0]
    rest = "  ".join(f"{q.book[:2]} {q.american}" for q in quotes[1:])
    return (f"{team:<22} best {best.book:<14} {best.american:>6} "
            f"(p={best.prob:.3f})  | {rest}")


def report(games, now, quota):
    local = now.astimezone(LOCAL_TZ)
    print(f"\n{'=' * 78}")
    print(f"SCAN {local:%Y-%m-%d %H:%M %Z}  |  Odds API credits remaining: "
          f"{quota.get('remaining', '?')}")
    print(f"{'=' * 78}")
    if not games:
        print("No upcoming games found.")
        return

    ranked = []
    for g in games:
        a = analyze(g)
        if a:
            ranked.append((g, a))
    ranked.sort(key=lambda ga: ga[1]["total_prob"])

    for g, a in ranked:
        start_local = g.start.astimezone(LOCAL_TZ)
        n_books = len({q.book for q in g.quotes})
        tag = ""
        if a["total_prob"] < config.ARB_ALERT_THRESHOLD:
            tag = f"  *** ARB {a['arb_roi'] * 100:.2f}% ROI ***"
        print(f"\n[{g.sport.upper()}] {g.away} @ {g.home}  "
              f"{start_local:%a %H:%M}  ({n_books} books)  "
              f"sum={a['total_prob']:.4f}{tag}")
        print(f"  {_fmt_side(g, g.away)}")
        print(f"  {_fmt_side(g, g.home)}")

    arbs = [x for x in ranked if x[1]["total_prob"] < config.ARB_ALERT_THRESHOLD]
    print(f"\n{len(ranked)} games compared, {len(arbs)} arbitrage opportunities.")


# ---------------------------------------------------------------- logging

def log_scan(games, now):
    config.DATA_DIR.mkdir(exist_ok=True)
    with open(config.DATA_DIR / "scans.jsonl", "a") as f:
        for g in games:
            a = analyze(g)
            f.write(json.dumps({
                "scanned_at": now.isoformat(),
                "sport": g.sport,
                "away": g.away,
                "home": g.home,
                "start": g.start.isoformat(),
                "quotes": [{"book": q.book, "team": q.team,
                            "prob": round(q.prob, 4), "detail": q.detail}
                           for q in g.quotes],
                "total_prob": round(a["total_prob"], 4) if a else None,
            }) + "\n")

    arb_rows = []
    for g in games:
        a = analyze(g)
        if a and a["total_prob"] < config.ARB_ALERT_THRESHOLD:
            arb_rows.append([
                now.isoformat(), g.sport, g.away, g.home, g.start.isoformat(),
                a["best_away"].book, a["best_away"].american,
                a["best_home"].book, a["best_home"].american,
                round(a["total_prob"], 4), round(a["arb_roi"] * 100, 2),
            ])
    if arb_rows:
        path = config.DATA_DIR / "opportunities.csv"
        new = not path.exists()
        with open(path, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["scanned_at", "sport", "away", "home", "start",
                            "away_book", "away_odds", "home_book", "home_odds",
                            "total_prob", "roi_pct"])
            w.writerows(arb_rows)


# ---------------------------------------------------------------- pacing

def _month_end(now):
    if now.month == 12:
        return datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
    return datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)


def _active_seconds_until(now_local, end_utc):
    """Seconds inside the daily active window between now and month end."""
    h0, h1 = config.ACTIVE_HOURS_LOCAL
    total, day = 0.0, now_local
    end_local = end_utc.astimezone(LOCAL_TZ)
    while day.date() <= end_local.date():
        w_start = day.replace(hour=h0, minute=0, second=0, microsecond=0)
        w_end = day.replace(hour=h1 - 1, minute=59, second=59, microsecond=0)
        lo, hi = max(w_start, now_local), min(w_end, end_local)
        if hi > lo:
            total += (hi - lo).total_seconds()
        day += timedelta(days=1)
    return total


def _in_active_window(now_local):
    h0, h1 = config.ACTIVE_HOURS_LOCAL
    return h0 <= now_local.hour < h1


def _next_window_open(now_local):
    h0, _ = config.ACTIVE_HOURS_LOCAL
    nxt = now_local.replace(hour=h0, minute=0, second=0, microsecond=0)
    if now_local.hour >= h0:
        nxt += timedelta(days=1)
    return nxt


def next_interval(remaining_credits):
    """Spread remaining credits evenly over active hours left this month."""
    now = datetime.now(timezone.utc)
    now_local = now.astimezone(LOCAL_TZ)
    credits_per_scan = len(config.ENABLED_SPORTS)
    usable = max((remaining_credits or 0) - config.CREDIT_RESERVE, 0)
    scans_left = usable // credits_per_scan
    if scans_left <= 0:
        # out of budget: wait for the monthly reset
        return (_month_end(now) - now).total_seconds() + 60, "credit reserve reached"
    active_s = _active_seconds_until(now_local, _month_end(now))
    interval = active_s / scans_left
    interval = max(config.MIN_INTERVAL_S, min(config.MAX_INTERVAL_S, interval))
    return interval, f"{scans_left} scans left this month"


def loop():
    while True:
        now_local = datetime.now(LOCAL_TZ)
        if not _in_active_window(now_local):
            nxt = _next_window_open(now_local)
            wait = (nxt - now_local).total_seconds()
            print(f"Outside active window ({config.ACTIVE_HOURS_LOCAL[0]}:00-"
                  f"{config.ACTIVE_HOURS_LOCAL[1]}:00). Sleeping until {nxt:%H:%M}...")
            time.sleep(max(wait, 1))
            continue
        try:
            quota = run_scan()
        except Exception as e:
            print(f"Scan failed: {e}; retrying in 10 min")
            time.sleep(600)
            continue
        interval, why = next_interval(quota.get("remaining"))
        nxt = datetime.now(LOCAL_TZ) + timedelta(seconds=interval)
        print(f"Next scan at {nxt:%H:%M} ({interval / 60:.0f} min — {why})")
        time.sleep(interval)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--loop", action="store_true",
                   help="run continuously, pacing scans to the monthly credit budget")
    args = p.parse_args()
    if not config.ODDS_API_KEY:
        sys.exit("ODDS_API_KEY not set (check your .env)")
    if args.loop:
        loop()
    else:
        run_scan()


if __name__ == "__main__":
    main()
