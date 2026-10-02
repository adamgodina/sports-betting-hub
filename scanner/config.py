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
    # Soccer can end level, so a match has THREE outcomes: home, draw, away.
    # Everything downstream reads the outcome list rather than assuming two.
    three_way: bool = False
    # Keep a fixture that BOTH exchanges price even when the sportsbooks have
    # never heard of it. Only worth it where the Odds API's coverage is thin
    # and the exchanges' is not — tennis, where the books go dark between
    # tournaments while Kalshi and Polymarket price a hundred matches a day.
    exchange_only_rows: bool = False
    teams: dict = field(repr=False, default_factory=dict)


# The draw is an outcome, not a team: the venues each name it differently
# ("Draw" on the sportsbooks, "Tie" on Kalshi, its own market on Polymarket),
# so they are all mapped onto this one label.
DRAW = "Draw"


def feeds(value) -> list[str]:
    """The venue feeds behind one sport, as a list.

    Most sports are one series and one league. Tennis is not: ATP and WTA are
    separate everywhere — two Kalshi series, two Polymarket leagues, a fresh
    Odds API key per tournament — but they are ONE thing to look at, so a sport
    may name several feeds and the source modules fetch each in turn.
    """
    if not value:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(v).strip() for v in value if str(v).strip()]


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
    # One tennis board, not one per tour. The Odds API has a separate key per
    # TOURNAMENT (tennis_atp_us_open, tennis_wta_wuhan_open, ...), so the
    # prefix is just "tennis": every tournament currently being priced is
    # picked up, including any the API adds later. Kalshi and Polymarket each
    # split men's and women's, so both feeds are named here.
    #
    # Cost: 1 credit per ACTIVE tournament, which is what the tour calendar
    # happens to be running — typically 2-6, and 0 in the gaps between events.
    "tennis": SportConfig(
        key="tennis",
        odds_api_sport="",                       # resolved from the prefix
        odds_api_sport_prefix="tennis",
        kalshi_series="KXATPMATCH,KXWTAMATCH",
        polymarket_us_league="atp,wta",
        polymarket_us_winner_type="tennis_match_winner",
        match_mode="name",                       # players, not a fixed roster
        exchange_only_rows=True,
        # Tennis start times are "not before", not kick-offs: the venues were
        # measured up to 3h apart on the same match. Pairing plateaus at 4h
        # (12h and 24h find nothing more), so this is slack, not looseness.
        match_tolerance_hours=6,
    ),

    "nhl": SportConfig(
        key="nhl",
        odds_api_sport="icehockey_nhl",
        kalshi_series="KXNHLGAME",
        polymarket_us_league="nhl",
        polymarket_us_winner_type="hockey_team_full_game_winner",
        match_mode="roster",        # Kalshi abbreviates to the city ("Vegas")
        # A moneyline here settles after overtime and the shootout, so it is a
        # two-way market despite hockey's ties in regulation.
        horizon_hours=120,
        match_tolerance_hours=12,   # the ticker carries no clock time
    ),

    # UFC: one event per fight, two entrants, matched by name like tennis.
    # Kalshi's KXUFCFIGHT ticker carries no clock time (KXUFCFIGHT-26SEP29ABUSTA),
    # so the start comes from occurrence_datetime — which is why the tolerance
    # is generous: the sportsbooks time a fight from the CARD's start, and a
    # prelim can be hours from the main event.
    "ufc": SportConfig(
        key="ufc",
        odds_api_sport="mma_mixed_martial_arts",
        kalshi_series="KXUFCFIGHT",
        polymarket_us_league="ufc",
        polymarket_us_winner_type="ufc_fight_winner",
        match_mode="name",
        horizon_hours=336,          # cards are announced weeks out
        match_tolerance_hours=12,
    ),

    # ---- soccer: three-way (home / draw / away) ----------------------------
    # Kalshi prices each outcome as its own market (…-TIE for the draw) and
    # Polymarket as three winner markets per match, so all three venues line
    # up outcome for outcome.
    "epl": SportConfig(
        key="epl",
        odds_api_sport="soccer_epl",
        kalshi_series="KXEPLGAME",
        polymarket_us_league="epl",
        polymarket_us_winner_type="soccer_team_full_time_winner",
        match_mode="roster",
        three_way=True,
        horizon_hours=240,          # league football is a weekly fixture list
        match_tolerance_hours=12,
    ),
    "laliga": SportConfig(
        key="laliga",
        odds_api_sport="soccer_spain_la_liga",
        kalshi_series="KXLALIGAGAME",
        polymarket_us_league="lal",
        polymarket_us_winner_type="soccer_team_full_time_winner",
        match_mode="roster",
        three_way=True,
        horizon_hours=240,
        match_tolerance_hours=12,
    ),
    "seriea": SportConfig(
        key="seriea",
        odds_api_sport="soccer_italy_serie_a",
        kalshi_series="KXSERIEAGAME",
        polymarket_us_league="",     # no Polymarket league for Serie A
        polymarket_us_winner_type="soccer_team_full_time_winner",
        match_mode="roster",
        three_way=True,
        horizon_hours=240,
        match_tolerance_hours=12,
    ),
    "bundesliga": SportConfig(
        key="bundesliga",
        odds_api_sport="soccer_germany_bundesliga",
        kalshi_series="KXBUNDESLIGAGAME",
        polymarket_us_league="bun",
        polymarket_us_winner_type="soccer_team_full_time_winner",
        match_mode="roster",
        three_way=True,
        horizon_hours=240,
        match_tolerance_hours=12,
    ),
    "ligue1": SportConfig(
        key="ligue1",
        odds_api_sport="soccer_france_ligue_one",
        kalshi_series="KXLIGUE1GAME",
        polymarket_us_league="",     # Kalshi + sportsbooks only
        polymarket_us_winner_type="soccer_team_full_time_winner",
        match_mode="roster",
        three_way=True,
        horizon_hours=240,
        match_tolerance_hours=12,
    ),
    "mls": SportConfig(
        key="mls",
        odds_api_sport="soccer_usa_mls",
        kalshi_series="KXMLSGAME",
        polymarket_us_league="mls",
        polymarket_us_winner_type="soccer_team_full_time_winner",
        match_mode="roster",
        three_way=True,
        horizon_hours=240,
        match_tolerance_hours=12,
    ),
}

# Each enabled sport costs 1 Odds API credit per Refresh (tennis costs 1 per
# active tournament), so this list is what drives spend.
ENABLED_SPORTS = ["mlb", "nfl", "ncaaf", "nba", "cbb", "nhl", "tennis", "ufc",
                  "epl", "laliga", "seriea", "bundesliga", "ligue1", "mls"]

# The sportsbooks used everywhere — moneylines and player props alike.
# The Odds API bills bookmakers in blocks of ten, so all nine of these cost
# the SAME 1 credit per sport per request that two did (verified against
# x-requests-last, including with BetRivers as the ninth). ONE more book is
# free; an eleventh doubles the cost of every request, props included.
ODDS_API_BOOKMAKERS = ",".join([
    "draftkings",
    "fanduel",
    "betmgm",
    "espnbet",
    "fanatics",
    "fliff",
    "hardrockbet",
    "williamhill_us",   # Caesars — the Odds API still uses the William Hill key
    "betrivers",
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

# ---- player prop markets ----
# A prop view is a two-outcome player market: the books quote only the "yes"
# side, and the hedge comes from the exchanges' "no". Everything that differs
# between one prop and the next lives here, so adding another is an entry in
# PROP_MARKETS rather than a second scanner.
@dataclass
class PropConfig:
    key: str                      # our short name, used by the API and the UI
    label: str                    # the yes side, as a column reads it
    no_label: str                 # the no side
    sport_tag: str                # `sport` on the rows; keeps game keys distinct
    odds_api_sport: str
    odds_api_markets: tuple       # 1 CREDIT PER GAME for each key listed
    outcome_name: str             # the outcome to keep ("Over" / "Yes")
    outcome_point: float = None   # and its point, where the market has one
    # Per-market override of the pair above, because the SAME bet has a
    # different shape depending on the key it is posted under: an anytime
    # touchdown is "Yes" with no point under `player_anytime_td` and "Over"
    # at 0.5 under `player_tds_over`. Without this, adding the second key
    # billed for a market whose every outcome was then thrown away.
    market_outcomes: dict = field(repr=False, default_factory=dict)
    # Kalshi's prop tickers usually say WHEN the game is
    # (KXMLBHR-26SEP291400PHIATL), which is what picks the right market when a
    # player has two open. Some don't (KXNHLGOAL-26SEP29VANEDM): football gets
    # away with that because a team plays once a week, hockey does not — a
    # team can play on consecutive nights, and a player then has two open
    # markets the matcher cannot tell apart, so it refuses both. When this is
    # set, a ticker with no clock time falls back to occurrence_datetime and
    # is matched within this many hours. (That field runs up to ~3h off the
    # real start, which is why it is slack rather than a tight window;
    # closest-wins does the actual choosing, and back-to-backs are a day apart.)
    kalshi_occurrence_slack_hours: float = 0.0
    kalshi_series: str = ""
    kalshi_suffix: str = "-1"     # the 1+ line within the series
    pm_market_type: str = ""
    pm_league: str = ""
    pm_line: int = 1
    # How far ahead to cover. This is the cost dial: props bill 1 credit PER
    # GAME, and the free events endpoint hands back the whole SEASON for
    # football — 212 NFL games, which is 212 credits and a board full of
    # fixtures a fortnight away that no exchange has priced yet.
    horizon_hours: float = 36


# ---- game totals (over/under points) --------------------------------------
# A total has a LINE, and the venues post different ones, so a row is a
# (game, line) pair rather than a game. Unlike player props this costs ONE
# Odds API credit for the whole sport, not one per game.
@dataclass
class TotalConfig:
    key: str
    sport_tag: str                  # which board sport these rows belong to
    odds_api_sport: str
    kalshi_series: str
    pm_league: str = ""
    pm_market_type: str = ""
    over_label: str = "Over"
    under_label: str = "Under"
    horizon_hours: float = 240
    match_tolerance_hours: float = 12


TOTAL_MARKETS = {
    "nfl_total": TotalConfig(
        key="nfl_total",
        sport_tag="nfl",
        odds_api_sport="americanfootball_nfl",
        kalshi_series="KXNFLTOTAL",
        pm_league="nfl",
        pm_market_type="football_team_full_game_total",
    ),
    "mlb_total": TotalConfig(
        key="mlb_total",
        sport_tag="mlb",
        odds_api_sport="baseball_mlb",
        kalshi_series="KXMLBTOTAL",
        pm_league="mlb",
        pm_market_type="baseball_team_full_game_total",
        horizon_hours=36,
        # tight, because a doubleheader puts the same pair on the same day
        match_tolerance_hours=1.5,
    ),
}


PROP_MARKETS = {
    "mlb_hr": PropConfig(
        key="mlb_hr", label="1+ HR", no_label="No HR", sport_tag="mlb-hr",
        odds_api_sport="baseball_mlb",
        # The books split home runs across two keys and EACH bills its own
        # credit per game. Measured on Phillies @ Braves:
        #   batter_home_runs_alternate -> DraftKings, FanDuel, BetMGM,
        #                                 ESPN BET, Fanatics (Hard Rock: 2)
        #   batter_home_runs           -> Caesars, Fliff, ESPN BET,
        #                                 Hard Rock (18)
        # Caesars and Fliff post ONLY under the second key, so with just the
        # first their columns were empty on every player. Both keys use
        # "Over" at 0.5, so no market_outcomes override is needed.
        #
        # 2 CREDITS PER GAME. That's ~8 a scan in the postseason (4 games) but
        # ~50 in the regular season (~25 games in 36h) — drop the second key
        # when the season restarts if that's too steep.
        odds_api_markets=("batter_home_runs_alternate", "batter_home_runs"),
        outcome_name="Over", outcome_point=0.5,
        kalshi_series="KXMLBHR",
        pm_market_type="baseball_player_home_runs", pm_league="mlb",
    ),
    "mlb_tb": PropConfig(
        key="mlb_tb", label="2+ TB", no_label="0-1 TB", sport_tag="mlb-tb",
        odds_api_sport="baseball_mlb",
        # Caesars, BetMGM and Fliff post only the standard key; FanDuel and BetRivers only the alternate.
        odds_api_markets=("batter_total_bases_alternate", "batter_total_bases"),
        outcome_name="Over", outcome_point=1.5,
        kalshi_series="KXMLBTB", kalshi_suffix="-2",
        pm_market_type="baseball_player_total_bases", pm_league="mlb", pm_line=2,
    ),
    "nfl_td": PropConfig(
        key="nfl_td", label="1+ TD", no_label="No TD", sport_tag="nfl-td",
        odds_api_sport="americanfootball_nfl",
        # Two keys for one bet. `player_anytime_td` carries six of the seven
        # books; ESPN BET posts touchdowns ONLY under `player_tds_over`, so
        # without the second key its column is empty on every player. Verified
        # on a live game: anytime-TD returned draftkings, caesars, fanduel,
        # fliff, betmgm, hardrock and fanatics but no ESPN BET, while
        # tds_over returned 27 ESPN BET players.
        #
        # THIS DOUBLES THE COST of a TD scan — props bill 1 credit per market
        # PER GAME, so a 15-game slate is 30 credits rather than 15. Drop the
        # second key to halve it again.
        odds_api_markets=("player_anytime_td", "player_tds_over"),
        outcome_name="Yes", outcome_point=None,
        market_outcomes={"player_tds_over": ("Over", 0.5)},
        kalshi_series="KXNFLTD",
        pm_market_type="football_player_touchdowns", pm_league="nfl",
        horizon_hours=120,      # Thursday through Monday: one week's slate
    ),
    "nhl_goal": PropConfig(
        key="nhl_goal", label="1+ Goal", no_label="No Goal", sport_tag="nhl-goal",
        odds_api_sport="icehockey_nhl",
        # Split across two keys exactly like touchdowns, just with different
        # books on each side. Measured on Panthers @ Hurricanes:
        #   player_goal_scorer_anytime -> DraftKings, Caesars, FanDuel, BetMGM,
        #                                 Fanatics ("Yes", no point)
        #   player_goals               -> Hard Rock, ESPN BET, FanDuel
        #                                 ("Over" at 0.5)
        # Neither key alone covers the board, so both are queried: 2 CREDITS
        # PER GAME. Fliff posts neither.
        odds_api_markets=("player_goal_scorer_anytime", "player_goals"),
        outcome_name="Yes", outcome_point=None,
        market_outcomes={"player_goals": ("Over", 0.5)},
        kalshi_series="KXNHLGOAL",
        pm_market_type="hockey_player_goals", pm_league="nhl",
        horizon_hours=36,       # a daily sport: tonight and tomorrow
        kalshi_occurrence_slack_hours=6,
    ),
}

# ---- prop scanning ----
# The Odds API bills 1 credit PER GAME for player props (one call returns every
# player in that game), so the only ways to spend less are to scan fewer games
# and to not re-fetch one you already have. Props barely move, so a scan result
# is reused for this long — a repeat scan inside the window is free.
PROPS_CACHE_TTL_S = 300.0
# 0 = every player the books price. This was 3 back when props were being
# rationed by hand; it never saved a credit (the credit is spent per GAME, so
# the whole slate of players arrives in the same response) and it hid most of
# the board. Set it to a positive number to trim the table again.
PROPS_TOP_PER_GAME = 0


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
