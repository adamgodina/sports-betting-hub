"""Scanner configuration.

To add a sport: add a SportConfig to SPORTS with the source identifiers,
a team table in teams.py, and put its key in ENABLED_SPORTS.
Each enabled sport costs 1 Odds API credit per scan (1 region-equivalent,
1 market), so more sports = fewer scans per month on the free tier.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from .teams import MLB_TEAMS

def _find_env() -> Path:
    """Where the credentials live, most explicit first.

    Secrets are happier outside the working tree: a .env sitting in the repo is
    safe from git (it is ignored) but not from everything else you might do to
    a folder — zip it, sync it, share it, point a tool at it. So a config-dir
    copy wins if it exists, and $ODDS_SCANNER_ENV overrides everything.

    The in-repo .env remains the fallback, so an existing checkout keeps
    working with no changes.
    """
    override = os.environ.get("ODDS_SCANNER_ENV")
    if override:
        return Path(override).expanduser()
    shared = Path.home() / ".config" / "odds-scanner" / ".env"
    if shared.is_file():
        return shared
    return Path(__file__).resolve().parent.parent / ".env"


ENV_PATH = _find_env()
load_dotenv(ENV_PATH)

ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")

# ---- trading credentials (optional; enables the Buy buttons in the UI) ----
KALSHI_API_KEY_ID = os.environ.get("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY_PATH = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")

# Polymarket US (polymarket.us): API keys from polymarket.us/developer after
# completing KYC in the iOS app. Ed25519-signed requests — no wallet private
# key, no .pem. (The offshore polymarket.com CLOB is not usable from the US.)
POLYMARKET_KEY_ID = os.environ.get("POLYMARKET_KEY_ID", "")
POLYMARKET_SECRET_KEY = os.environ.get("POLYMARKET_SECRET_KEY", "")


@dataclass
class SportConfig:
    key: str                    # our short name
    odds_api_sport: str         # The Odds API sport key (fixed-key leagues)
    kalshi_series: str          # Kalshi series ticker for game winner markets
    polymarket_us_league: str = ""       # polymarket.us league slug (e.g. "mlb")
    polymarket_us_winner_type: str = ""  # its game-winner sportsMarketType
    # Tennis is per tournament, and tournaments come and go, so the Odds API
    # key is resolved from a prefix against the live (free) sports list.
    odds_api_sport_prefix: str = ""
    # How a venue's team names are mapped onto The Odds API's:
    #   "alias"  – a hand-written table (MLB)
    #   "roster" – matched against the scan's own canonical names, so no table
    #              is needed; the only workable option for ~260 college teams
    #   "name"   – individual entrants matched by the shape of their name
    match_mode: str = "alias"
    # Daily sports need a day or two of lookahead; weekly ones (football) would
    # show almost nothing midweek, so they look further out.
    horizon_hours: float = 0        # 0 = use GAME_HORIZON_HOURS
    # How far apart two sources' start times may be and still be the same
    # fixture. Baseball stays tight because doubleheaders put the same pair on
    # the same day; other sports need slack because Kalshi's
    # occurrence_datetime runs 3h fast when its ticker carries no clock time.
    match_tolerance_hours: float = 1.5
    teams: dict = field(repr=False, default_factory=dict)


SPORTS = {
    "mlb": SportConfig(
        key="mlb",
        odds_api_sport="baseball_mlb",
        kalshi_series="KXMLBGAME",
        polymarket_us_league="mlb",
        polymarket_us_winner_type="baseball_team_full_game_winner",
        teams=MLB_TEAMS,
    ),
    "nfl": SportConfig(
        key="nfl",
        odds_api_sport="americanfootball_nfl",
        kalshi_series="KXNFLGAME",
        polymarket_us_league="nfl",
        polymarket_us_winner_type="football_team_full_game_winner",
        match_mode="roster",
        horizon_hours=240,          # ~10 days: football is a weekly sport
        match_tolerance_hours=12,
    ),
    "ncaaf": SportConfig(
        key="ncaaf",
        odds_api_sport="americanfootball_ncaaf",
        kalshi_series="KXNCAAFGAME",
        polymarket_us_league="cfb",          # Polymarket calls it cfb
        polymarket_us_winner_type="football_team_full_game_winner",
        match_mode="roster",
        horizon_hours=240,
        match_tolerance_hours=12,
    ),
    "nba": SportConfig(
        key="nba",
        odds_api_sport="basketball_nba",
        kalshi_series="KXNBAGAME",
        polymarket_us_league="nba",
        polymarket_us_winner_type="basketball_team_full_game_winner",
        match_mode="roster",
        match_tolerance_hours=12,
    ),
    "cbb": SportConfig(
        key="cbb",
        odds_api_sport="basketball_ncaab",
        kalshi_series="KXNCAABGAME",
        polymarket_us_league="cbb",
        polymarket_us_winner_type="basketball_team_full_game_winner",
        match_mode="roster",
        match_tolerance_hours=12,
    ),
    "atp": SportConfig(
        key="atp",
        odds_api_sport="",                       # resolved from the prefix
        odds_api_sport_prefix="tennis_atp",
        kalshi_series="KXATPMATCH",
        polymarket_us_league="atp",
        polymarket_us_winner_type="tennis_match_winner",
        match_mode="name",                       # players, not a fixed roster
    ),
    "wta": SportConfig(
        key="wta",
        odds_api_sport="",
        odds_api_sport_prefix="tennis_wta",
        kalshi_series="KXWTAMATCH",
        polymarket_us_league="wta",
        polymarket_us_winner_type="tennis_match_winner",
        match_mode="name",
    ),
}

# Each enabled sport costs 1 Odds API credit per Refresh (tennis costs 1 per
# active tournament), so this list is what drives spend.
ENABLED_SPORTS = ["mlb", "nfl", "ncaaf", "nba", "cbb", "atp", "wta"]

# The sportsbooks used everywhere — moneylines and player props alike.
# The Odds API bills bookmakers in blocks of ten, so all seven of these cost
# the SAME 1 credit per sport per request that two did (verified against
# x-requests-last). Adding books is free; adding sports or markets is not.
ODDS_API_BOOKMAKERS = ",".join([
    "draftkings",
    "fanduel",
    "betmgm",
    "espnbet",
    "fanatics",
    "fliff",
    "hardrockbet",
])

# ---- scan pacing (terminal loop; the UI paces itself, see AUTO_* below) ----
MONTHLY_CREDITS = 500
CREDIT_RESERVE = 25          # keep a buffer so we never hit 0 mid-month
MIN_INTERVAL_S = 5 * 60      # never scan more often than this
MAX_INTERVAL_S = 4 * 3600    # never wait longer than this inside the active window
ACTIVE_HOURS_LOCAL = (11, 23)  # only scan between 11:00 and 23:00 local time
LOCAL_TZ = "America/Chicago"

# ---- auto-refresh spend guards (paid Odds API plan) ----
# A 1/second sportsbook refresh is only affordable because these caps make it
# impossible to leave running. Every one of them is enforced SERVER-SIDE in
# budget.py, so a forgotten tab, a sleeping laptop, or a stray curl cannot
# spend past them — the browser loop is the polite layer, not the guard.
# Two tiers, because that is where the value actually is: a game in progress
# re-prices constantly and is the only time a 1-second sportsbook line is worth
# paying for, while a game hours away barely moves. One /odds call bills the
# same whether it returns 1 game or 85, so the tier is chosen by whether the
# view HAS a live game, not per game.
AUTO_LIVE_INTERVAL_S = 1.0     # a live game is on screen
AUTO_IDLE_INTERVAL_S = 10.0    # nothing has started yet — 10x cheaper per hour
# Only the sport on screen is refreshed (1 credit), never all of ENABLED_SPORTS
# (7). This is the single biggest lever on spend, so it is not optional.
AUTO_SCOPE_TO_VISIBLE = True

# There is no on/off switch: the board just updates, and the ONLY thing keeping
# that from running unattended is the dead-man switch. The UI beats every two
# seconds, and only while its tab is visible AND someone has touched the page
# within AUTO_IDLE_STOP_S. Miss this many seconds of beats — idle, hidden tab,
# sleeping laptop, closed browser, crashed renderer — and the server stops
# spending. Coming back re-arms it on the next beat, with no click.
AUTO_HEARTBEAT_TIMEOUT_S = 6.0
# No mouse or keyboard in the tab for this long and the UI stops beating.
# This is the guard against leaving it going overnight: walk away, and the paid
# half is off within half an hour.
AUTO_IDLE_STOP_S = 1800.0      # 30 minutes
# A hard outer boundary on the clock, regardless of activity.
AUTO_ACTIVE_HOURS_LOCAL = (8, 23)

# Hard ceilings on spend, sized against the plan actually on the key
# (20,000 credits/month as of 2026-09-09). The arithmetic that matters:
# one sport at the live tier is 60 credits/minute = 3,600/hour, so a single
# careless hour is a fifth of the month; at the idle tier it is 360/hour, or
# ~55 hours of pre-game browsing. Since the board now updates without being
# asked, the daily cap is the real backstop behind the dead-man switch.
# AUTO_MAX_CREDITS_PER_MIN also catches a runaway client that ignores the
# tiers entirely.
AUTO_MAX_CREDITS_PER_MIN = 90
AUTO_DAILY_CREDIT_CAP = 4000   # auto + manual; ~2h of live-tier or ~11h of idle

# Floor under the PLAN's own x-requests-remaining, which is the only number
# that can't drift: auto-refresh stops rather than spend the last of it, so
# there is always something left for the Refresh button and for sizing a hedge
# (fresh_sportsbook_price is deliberately never gated). Raise this to whatever
# you want to keep in reserve on a bigger plan.
AUTO_MIN_PLAN_REMAINING = 2000

# only compare games starting within this many hours from now
GAME_HORIZON_HOURS = 36

# Both exchanges charge a taker fee of theta * p * (1-p) per contract (Kalshi
# 0.07, Polymarket US 0.06). Fold them into the effective price so exchange
# quotes compare apples-to-apples against sportsbook vig.
INCLUDE_KALSHI_FEES = True
INCLUDE_POLYMARKET_US_FEES = True

# flag opportunities where the sum of best implied probabilities is below this
# (1.0 = true arbitrage; slightly above 1.0 still shows near-arbs in the report)
ARB_ALERT_THRESHOLD = 1.0

# ---- order placement ----
# Orders are re-quoted against the live top of book at submit time (a price
# from an old scan can end up at or below the current bid, where it just rests
# unfilled). If the live book cannot be read the order is REFUSED — never
# priced off the stale scan, which silently recreates that bug.
#
# ORDER_CROSS_CENTS bids this far THROUGH the live best ask so the order still
# crosses if the book ticks up in flight. Expressed in cents (not ticks,
# which differ per venue: 1c on Kalshi, 0.5c on Polymarket US). On a CLOB the
# resting order sets the fill price, so crossing normally costs nothing.
ORDER_CROSS_CENTS = 1.0
# Refuse to chase: if the live ask is more than this above the price shown in
# the UI, the order is rejected instead of filling much worse.
MAX_ORDER_SLIPPAGE = 0.03
# The UI polls exchange prices every second. If it hands us a reference price
# at least this fresh, the order skips its own pre-flight book read — one less
# network round trip on the click path (~60ms). A limit order can never fill
# above its limit, so the exposure from a slightly stale reference is "might
# rest unfilled", never "filled too high".
ORDER_REF_MAX_AGE_MS = 1500

# ---- fill priority (hedging) ----
# When you have already locked a bet at a sportsbook, the exchange leg MUST
# fill: an unfilled hedge leaves a naked position, which is far worse than
# paying a few cents of slippage. With HEDGE_FILL_MODE on, orders are priced to
# sweep the book instead of sitting at the touch, and a market that has moved
# is NEVER a reason to refuse the order (that would leave you unhedged).
# On a CLOB you still pay each resting level's own price, so an aggressive
# limit is a ceiling, not the price you pay.
HEDGE_FILL_MODE = True
HEDGE_MAX_SLIPPAGE_CENTS = 5.0     # how far through the book we will pay

# Before an instant buy, the sportsbook price the hedge is sized against is
# re-read from The Odds API so the contract count matches the current line
# rather than the last Refresh. That costs 1 credit per uncached read, so
# results are reused for this many seconds — a sportsbook line does not move
# meaningfully inside that window, and it keeps a burst of clicks from
# spending a credit each.
HEDGE_QUOTE_TTL_S = 5.0

# ---- 1+ HR prop scanning ----
# The Odds API bills 1 credit PER GAME for player props (one call returns every
# player in that game), so the only ways to spend less are to scan fewer games
# and to not re-fetch one you already have. Props barely move, so a scan result
# is reused for this long — a repeat scan inside the window is free.
PROPS_CACHE_TTL_S = 300.0
PROPS_TOP_PER_GAME = 3      # keep only the N likeliest hitters per game

# Which Odds API market key(s) to read the 1+ HR line from. The books split
# across two keys and EACH key costs its own credit per game:
#   batter_home_runs            -> BetRivers only
#   batter_home_runs_alternate  -> DraftKings, FanDuel, BetMGM, Bovada,
#                                  BetOnline, MyBookie
# Default is the alternate key alone: it is the one carrying DraftKings and
# FanDuel, for 1 credit per game. (The standard key only adds BetRivers, which
# is not used, so there is no reason to pay the second credit.)
PROPS_HR_MARKETS = ("batter_home_runs_alternate",)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def reload_env():
    """Re-read .env so credential edits apply without restarting the server."""
    global ENV_PATH, ODDS_API_KEY, KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH
    global POLYMARKET_KEY_ID, POLYMARKET_SECRET_KEY
    ENV_PATH = _find_env()      # re-resolve: the file may have just moved
    load_dotenv(ENV_PATH, override=True)
    ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
    KALSHI_API_KEY_ID = os.environ.get("KALSHI_API_KEY_ID", "")
    KALSHI_PRIVATE_KEY_PATH = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
    POLYMARKET_KEY_ID = os.environ.get("POLYMARKET_KEY_ID", "")
    POLYMARKET_SECRET_KEY = os.environ.get("POLYMARKET_SECRET_KEY", "")
