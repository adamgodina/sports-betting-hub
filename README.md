# Odds Scanner

A local board that lines up sportsbook odds against Kalshi and Polymarket US,
flags arbitrage, and places the exchange side of a hedge for you. Able to 
automatically maximize and calculate optimal hedging for profit boosts and bonus bets.

- **Sportsbooks:** DraftKings, FanDuel, BetMGM, ESPN BET, Fanatics, Fliff,
  Hard Rock, Caesars, BetRivers (via [The Odds API](https://the-odds-api.com))
- **Exchanges:** Kalshi and Polymarket US (prices are free and public)

| Sport | Markets |
|---|---|
| Baseball | Moneyline, 1+ HR, 2+ TB, Totals |
| Football | NFL, NCAAF, 1+ TD, Totals |
| Basketball | NBA, CBB |
| Hockey | NHL, 1+ Goal |
| Soccer | EPL, La Liga, Serie A, Bundesliga, Ligue 1, MLS (three-way) |
| Tennis | Matches |
| UFC | Fights |

## Setup and run

Requires macOS and Python 3.9 or newer (from python.org, Homebrew, or Apple's
developer tools).

Double-click **Odds Scanner.app**, or run `./start.sh` in Terminal. The first
run:

1. Creates `~/.config/odds-scanner/.env` and opens it. Add your
   `ODDS_API_KEY`, save, and start again.
2. Builds a `.venv` in the project and installs the packages (about a minute).
3. Starts the server and opens http://localhost:8765.

macOS asks once to let Odds Scanner access the folder the project is in.
Click Allow. If you downloaded the project as a zip, macOS may also block the
app; run `xattr -dr com.apple.quarantine .` in the project folder.

Kalshi and Polymarket US keys are optional and only needed to place orders.
Keep them, and the Kalshi `.pem`, in `~/.config/odds-scanner/`, not in the
project.

Server log: `~/Library/Logs/OddsScanner/server.log`. To stop the server:
`kill $(lsof -ti:8765)`.

## Using the board

- **Sport and market:** turn the wheel at the top left, then pick a market tab.
- **Books row:** tap a book to include or exclude it, drag to reorder or hide
  it, double-click to pin it (one leg must come from that book).
- **ROI column:** the return on a $100 hedge across the best prices, sized
  against real order-book depth. The line under it is the vig at the top of
  the book. Change the $100 with **arb check for $** above the table.
- **ARB tag:** the hedge pays either way. `checking…` means the order books
  are still being read.
- **Place order:** opens the hedge ticket. It sizes every leg, buys the
  exchange legs, and confirms the fill. Sportsbook legs you place yourself.
- **Boosts and bonus bets:** click a book's column header to add a profit
  boost, double-click it for a bonus bet. With a bonus bet or a boost max bet
  set, the board ranks by dollars won.
- **Parlay:** builds a Kalshi combo from searched legs and quotes every
  outcome.

### Updating

| Control | What it does |
|---|---|
| Refresh | Pulls fresh sportsbook odds for every sport |
| Slow mode (snail) | Nothing updates until you press Refresh |
| Live | Includes games in progress. With one on screen, the board updates every second, otherwise every 10 seconds |

Exchange prices are free and update continuously. Sportsbook updates stop on
their own when the tab is hidden or you have been idle for 30 minutes.

## Cost

Only The Odds API costs credits.

| Action | Credits |
|---|---|
| Refresh | 1 per sport (14) |
| Live updates | 1 per second with a live game on screen, 1 per 10 seconds otherwise |
| Totals scan | 1 |
| Prop scan (1+ HR, 2+ TB, 1+ TD, 1+ Goal) | 2 per game |

Server-side caps (90 per minute, 4,000 per day) apply to everything. Books are
billed in blocks of 10, so a tenth book is free and an eleventh doubles every
request.

## Placing orders

Orders use real money on your own accounts. Every order is a limit order that
crosses the book up to 5 cents to make sure a hedge fills, and the ticket
reports filled, partial, or resting within 5 seconds.

- **Kalshi:** API key ID and RSA `.pem` from Account > API keys. Collateral is
  held per exchange shard; the ticket shows the balance on the shard that
  market trades on.
- **Polymarket US:** keys from polymarket.us/developer after verifying your
  identity in their iOS app. This is not polymarket.com.

## When something looks wrong

- **A book's column is empty:** that book does not offer the market right
  now. Tennis sportsbook odds disappear between tournaments, for example.
- **Header says "prices 2m old":** the window was hidden. Bring it forward and
  it catches up.
- **An arb vanishes after opening the ticket:** the top price had only a few
  contracts behind it. The board prices real size, so this is rare.

## Project layout

```
scanner/
  server.py        web server and API
  config.py        sports, books, markets, spend limits
  scan.py          builds the board
  compare.py       matches a fixture across venues
  props.py         player props
  totals.py        game totals
  parlay.py        Kalshi combos
  budget.py        credit guards
  sources/         Odds API, Kalshi, Polymarket US market data
  trading/         Kalshi and Polymarket US order placement
  static/          the web UI
start.sh           setup and launch (the app runs this)
```

`python3 -m scanner.scan` prints one scan to the terminal.
