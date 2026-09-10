# Cross-book odds scanner

Compares prices for the same game across seven sportsbooks and two prediction
markets, flags arbitrage, sizes a hedge, and places the exchange leg for you.

**Sportsbooks** (via [The Odds API](https://the-odds-api.com)) — DraftKings,
FanDuel, BetMGM, ESPN BET, Fanatics, Fliff, Hard Rock.
**Exchanges** (tradeable) — Kalshi and Polymarket US.
**Sports** — MLB (moneyline + 1+ home runs), NFL, NCAAF, NBA, CBB, ATP, WTA.

Arbitrage here means the sum of the best implied probabilities across venues is
below 1 *after* exchange fees — backing both sides then pays more than it costs
whichever way the game goes.

> **This places real orders with real money.** The Buy and Place order buttons
> are live as soon as exchange credentials are configured; there is no paper
> mode. Nothing here is advice, and none of it guarantees that a price on
> screen is a price you can actually get.

Polymarket data comes from **polymarket.us** (the CFTC-regulated US exchange,
`gateway.polymarket.us`), not the offshore polymarket.com CLOB. They are
separate exchanges with separate order books — prices differ by roughly
0.5–1c — so US traders must price off the US book.

## Quick start

Python 3.9+ (developed on 3.12). macOS or Linux.

```bash
git clone <your-repo-url> sports && cd sports
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then add your Odds API key
python3 -m scanner.server
```

Open <http://localhost:8765>.

**Only `ODDS_API_KEY` is required.** Kalshi and Polymarket US market data are
public and free, so the whole board works with that one key — the exchange
credentials are what turn the Buy buttons on. Every field is documented in
[`.env.example`](.env.example).

`data/` (cached board, scan history, the persisted credit ledger) is created on
first run and is gitignored, as are `.env` and any `.pem`.

### Keys

`.gitignore` covers `.env`, `.env.*`, `*.pem`, `*.key` and `data/`, so nothing
sensitive is committed by default. Two habits worth keeping anyway:

* **Keep the credentials outside the working tree.** `.env` is read from the
  first of `$ODDS_SCANNER_ENV`, then `~/.config/odds-scanner/.env`, then the
  repo's own `.env` — so moving it needs no code change and no flag:

  ```bash
  mkdir -p ~/.config/odds-scanner && chmod 700 ~/.config/odds-scanner
  mv .env ~/.config/odds-scanner/.env
  mv .pem ~/.config/odds-scanner/kalshi-private-key.pem
  chmod 600 ~/.config/odds-scanner/*
  # then set an ABSOLUTE path in that .env:
  # KALSHI_PRIVATE_KEY_PATH=/Users/you/.config/odds-scanner/kalshi-private-key.pem
  ```

  Git already ignores both where they are; this covers everything that is not
  git — zipping the folder, syncing it, sharing it, or pointing a tool at it.
  Note the Polymarket secret is a *value inside `.env`*, not a separate file,
  so moving `.env` is what protects it.
* **Rotate anything that has ever been committed.** Deleting a secret in a
  later commit does not remove it from history; treat it as public and issue a
  new one.

Verify before your first push:

```bash
git ls-files | grep -Ei '\.env$|\.pem$|\.key$'   # must print nothing
```

On macOS you can double-click **Odds Scanner.app** instead: it finds a python
(preferring a `.venv/` in the repo), starts the server if it is not already up,
and opens the UI. It resolves the project directory from its own location, so
it works wherever you cloned to. Drag it to the Dock to keep it handy.

To stop the server:

```bash
kill $(lsof -ti:8765)
```

### What costs money

Only The Odds API charges. Kalshi and Polymarket US market data are free and
need no key.

| what | cost |
|---|---|
| One sportsbook scan of one sport | 1 Odds API credit |
| Adding sportsbooks | **free** — billed per block of 10, and seven is one block |
| Pressing Refresh | 1 credit per enabled sport |
| 1+ HR props | 1 credit **per game** |
| Continuous refresh, live tier | 60 credits/minute, for the sport on screen |
| Continuous refresh, idle tier | 6 credits/minute |

A live-tier hour is 3,600 credits, so read
[Continuous refresh](#continuous-refresh-this-is-the-part-that-costs-money)
before leaning on it. The guards in [`scanner/budget.py`](scanner/budget.py)
bound all of it and are enforced server-side; the defaults assume a
20,000/month plan, so retune them in [`scanner/config.py`](scanner/config.py)
if yours differs. The free tier is 500 credits/month — enough to explore the
board, not enough for the 1-second clock.

## Repository layout

```
scanner/
  server.py            local HTTP server + every API endpoint
  scan.py              orchestrates a scan; merges sources into one board
  budget.py            the spend guards (heartbeat, rate, daily, plan reserve)
  compare.py           matches the same fixture across venues
  config.py            sports, books, cadences, every tunable knob
  props.py             the 1+ home run scanner
  roster.py            resolves team names without a hand-written alias table
  teams.py             MLB alias table + person-name normalisation
  models.py            Quote / SourceEvent / MatchedGame
  sources/             read-only market data: oddsapi, kalshi, polymarket_us
  trading/             order placement: kalshi_trader, polymarket_us_trader
  static/index.html    the entire UI, one file
data/                  generated at runtime, gitignored
```

## Usage

```bash
python3 -m scanner.server        # web UI at http://localhost:8765
python3 -m scanner.scan          # one scan, printed to the terminal
python3 -m scanner.scan --loop   # continuous, self-pacing (terminal only)
```

`--loop` is the original terminal mode: it reads `x-requests-remaining` after
every scan and spreads what is left evenly across the remaining active hours of
the month, so it paces itself to the plan. The web UI does not use it — see
[Continuous refresh](#continuous-refresh-this-is-the-part-that-costs-money) for
how the board paces itself instead.

**Refresh touches only The Odds API** — one request, ~0.2–0.3s. Kalshi and
Polymarket US keep themselves current through their own free feeds, so
re-fetching them on a button press only added latency. Measured breakdown of
the old all-in-one scan:

| part | time | requests |
|---|---|---|
| The Odds API (DK/FD) | 0.31s | 1 |
| Kalshi + order-book depth | 0.86s | ~4 |
| Polymarket US + order-book depth | 0.90s | ~27 |
| **total** | **2.07s** | **~31** |

The Odds API was never the slow part, so narrowing *that* call would save
nothing — the fix was to stop doing the other 30 requests. Refresh now returns
sportsbook quotes only; the exchange quotes already on screen are carried
across so no column blanks out, a price poll fires immediately after, and
top-5 depth follows that. End to end it feels instant (~190ms in the browser).

The UI has a **Refresh** button (1 credit per sport per press), shows every
book's price with the best highlighted, and has book toggle chips — best
price and vig recompute live from just the selected books. It loads the last
scan from `data/last_scan.json` on open, so reopening costs nothing.

The right-hand column shows the **vig** (or, for an arb, the ROI) and holds
the outlined **Place order** button (it fills yellow on hover), which opens the
order ticket for that game.

### Continuous refresh (this is the part that costs money)

The board just updates. There is no switch, because a switch is a thing you
forget: DraftKings/FanDuel and Kalshi/Polymarket both re-read on one shared
clock, at two tiers.

| what is on screen | cadence |
|---|---|
| a live game | **every 1s** |
| nothing started yet | **every 10s** |

A game in progress re-prices constantly and is the only time a 1-second
sportsbook line is worth paying for; a game hours away barely moves, and the
slow tier is 10x cheaper per hour (360 credits/hour vs 3,600). One `/odds`
call bills the same whether it returns 1 game or 85, so the tier is picked by
whether the view **has** a live game, not per game — and it respects the
"Include live games" filter, because a live game you have filtered out is not
one you are watching.

#### What stops it

Since nobody has to remember to switch it off, the **heartbeat is the whole
safety model**. The UI beats every 2 seconds, and only while its tab is
visible AND someone has touched the page within the last 30 minutes. Miss six
seconds of beats and the server stops paying. Walking away, hiding the tab,
switching to another app, sleeping the laptop, closing the browser and
crashing the renderer all stop the beats — so none of them can keep spending.
Coming back re-arms on the very next beat, with nothing to click.

That is what covers "left it running overnight": walk away and the paid half
is off within half an hour, at a worst case of about 1,800 credits.

Behind it, and enforced in `scanner/budget.py` **on the server** so a crashed
browser or a stray `curl` cannot get past them either:

| guard | default |
|---|---|
| heartbeat dead-man | 6s |
| idle (client stops beating) | 30 min |
| rate ceiling | 90 credits/min |
| daily ceiling | 4000 credits, persisted across restarts |
| plan reserve | stops at 2000 left on the key, so Refresh and hedge-sizing still work |
| active hours | 08:00–23:00 local |

There is no longer a run-length or per-run credit limit. With the client
arming itself on every beat, either would disarm and then immediately re-arm —
that is not a guard, just churn.

The heartbeat runs on **its own 2s clock, never the scan's**. They were one
loop at first, which meant the 10s tier beat every 10s and starved its own 6s
dead-man switch.

The credit ceilings are sized against a 20,000/month plan: one sport at the
live tier is 60 credits/minute, so a single careless hour is a fifth of the
month. Change them in `scanner/config.py` if the plan changes.

Two more things keep the cadence affordable:

* **It bills one sport, not seven.** Only the sport on screen is scanned, and
  the result is folded back into the cached board (`_merge_scan`) so the other
  tabs keep their games. 1 credit/second instead of 7. The **Refresh** button
  is still the "update everything" action, at 1 credit per enabled sport.
* **It pays nothing in the 1+ HR view.** Props bill per *game*, so a 1/second
  loop over them would cost dozens of credits a second — the heartbeat stops
  while that tab is open.

Sizing a hedge (`fresh_sportsbook_price`) is counted against the day but
deliberately **never gated** — a spend cap must not be the reason a hedge leg
goes unfilled.

### The header controls

Four things, kept small because the header competes with the board:

| control | what it is |
|---|---|
| circular arrow | Refresh. Spins while a scan is in flight. |
| snail | Slow mode (below). |
| **Live** + switch | Include games that have already started. |
| INSTANT BUY OFF/ON | The instant-buy arm/disarm — click it. |

The live control keeps its **word**. Reduced to a bare switch it showed its
state perfectly and said nothing about what it switched; a toggle with no label
is unguessable until you press it, which is the wrong way round for a control
that changes what the board is priced against. Only the long "Include live
games" phrasing went.

**Instant buy is one control, not two.** There used to be a badge that read the
state and a Safety chip beside it that set the state — the same fact twice. The
badge is the button now: click to arm, click to disarm. `safetyOn` is unchanged
underneath, so the catch behaves exactly as before, and arming still never
survives a reload — a page that loads already armed turns a stray click into a
real order.

With the chip gone the badge has to explain itself, so its tooltip names the
case where releasing the catch still would not arm it: instant buy needs live
games on and an auto amount above zero, and never applies in the HR view.

### Slow mode

The **snail** next to Refresh freezes the whole board. Nothing updates on its
own — no sportsbook scans, no exchange polling, no order-book depth — and the
heartbeat stops, so the server stops authorising spend too. The board holds
whatever Refresh last pulled, and **Refresh becomes the only thing that moves
it**, and the header just reads `manual`. Measured: zero API calls over nine idle seconds, against five per four
seconds live; one press then makes exactly one scan plus a catch-up poll and
stops again.

It is global and it persists, so it survives a reload. Refresh is normally
tucked away while the board re-scans itself; in slow mode it can never hide,
because it is the only way to update. In the HR view it pulls props rather than
moneylines, so a manual refresh means the right thing in every market.

Both controls are icon-only to buy back header width — a circular arrow and a
snail, with the wording in the tooltip and `aria-label`. The refresh icon spins
while a scan is in flight; the button used to swap its text to "Scanning…",
which would now delete the icon from the DOM.

### Row order holds still while you aim at it

The board re-ranks by combined price, cheapest first. That is useful and also
dangerous: at a 1-second cadence a row can move out from under the cursor
between seeing a price and clicking it. So while the pointer is over the table
(or an order ticket is open) the current order is frozen and only the numbers
change.

That freeze was broken in a way that looked like the opposite of a freeze.
It remembered the previous order in a `Map` keyed by **game object identity**,
but every paid scan replaces the whole game list with fresh objects from the
server. So the lookup missed every row, all the sort keys collapsed to the same
fallback value, and the board fell back to the server's own order — by start
time. Hovering the table therefore *caused* the best game to drop to wherever
its kickoff time put it, usually far down the list.

Order is now remembered by `gameKey(g)` — `sport|away|home|start` — which
survives the object swap. `/api/refresh` had to start returning `sport` for
that key to line up across the two payload shapes.

The ranking itself also gets a deterministic tiebreak (start time, then key).
Without one, two games on the same combined price could swap places every tick
for no visible reason. Measured across a 98-row board: zero rows move between
ticks unless a price actually changed.

**An arbitrage outranks the freeze.** Holding the order absolutely had a worse
failure than shuffling: a game that *became* an arb while the cursor was over
the table stayed wherever it had been, sitting below worse prices until you
moved the mouse away. In live mode that is the normal case, because that is
exactly when a price moves far enough to open one. Measured on a 98-row NCAAF
board, every render put a non-arb at the top with a real arb pinned at index 1.

So the hold now applies *within* a class rather than across it: arbs always
render above non-arbs, and nothing shuffles inside either group — which is what
keeps a Place order button from moving out from under a click. Verified zero
violations of "no arb below a non-arb" across a run of live renders.

The trade-off is visible and intended. While you hover, arbs keep their
relative order rather than re-sorting by ROI (top of board reads
`0.9964, 0.9995, 0.9799, 0.9851` — all arbs, order held); move the cursor off
the table and the full ranking snaps back (`0.9799, 0.9851, 0.9964, 0.9995`).

### One clock, and a countdown instead of a timestamp

The sportsbook scan and the exchange poll are a **single tick**. They used to
be two independent loops at the same period — same cadence, different phase —
so a row could pair a DraftKings price from `t` with a Kalshi price from
`t+0.6s`. That skew is exactly what invents an arbitrage that was never
simultaneously available. Now both requests leave together (measured skew:
**≤5ms**) and are applied together, so every row is one snapshot. Application
order still matters and is fixed: `/api/scan` returns a new game list,
`/api/refresh` merges into the one on screen, so the scan lands first. One
render per tick instead of one per response.

The header shows **time to the next tick** — `update in 4s` — and nothing
else. "Last scan: 4:02 PM" made sense when a scan only happened when you
pressed a button; on a shared clock it always read "a second ago". Because
both halves ride the same tick, one number covers everything on screen, which
is also why there is no separate "exchange prices: live" indicator any more.

Tenths on the 1s tier, whole seconds on the 10s tier — and **`updating…`**
whenever a tick is in flight or already overdue. That last state matters: the
exchange round trip alone is ~0.9s, so on the 1s tier there is almost no idle
time to count down and the counter simply sat pinned at `0.0s`, which reads as
a broken clock rather than as "fetching right now". Saying what it is doing is
both honest and calmer to look at.

The one thing the header will say out loud is a stop you would not expect.
Idle and hidden-tab fix themselves the moment you touch the page, so they say
nothing; a spend ceiling or being outside active hours freezes the sportsbook
columns until something changes, so that shows as
`sportsbooks paused — today's 4000 credit cap is spent`.

**Refresh is unchanged**: still the deliberate all-sports action, 1 credit per
enabled sport, and it resets the shared clock around itself.

A hung request cannot stop the clock. `masterTick` carries a generation counter
and an `AbortController` deadline, and reschedules from a `finally`, so no
path — thrown, aborted, or superseded — leaves the clock stopped. Before that,
switching markets left a slow poll for the *old* sport in flight, the in-flight
flag stuck on, the chain never rescheduled, and the countdown froze at `0s`.
Switching markets now also ticks immediately, so the new sport's prices appear
at once rather than up to 10s later.

### Arranging the book row

Three states per book, because they answer different questions:

| state | how | what it means |
|---|---|---|
| in the row, **on** | tap | compared; the best price can come from it |
| in the row, **off** | tap again | greyed, column still there — you can see the price you are choosing not to use, and it is one tap from returning |
| **hidden** | `×` on the chip, or drag it to the Hidden row | gone from the row and from the table |

**Drag a chip to move its column.** Dragging works within the row, from the
row to Hidden, and from Hidden back into any position in the row. **All on** /
**All off** switch every book in the row, and **Add all** puts every hidden
book back. All off is allowed to leave nothing selected — "clear, then switch
on the two I care about" is the point of it, and a forced survivor would just
be a book you then have to remember to turn off.

The arrangement is saved and restored, since the point of arranging it is not
doing it again tomorrow. Hidden books are still **fetched** — the Odds API
bills bookmakers in blocks of ten, so a hidden book costs nothing and
un-hiding is instant rather than a re-scan.

Two implementation notes that are load-bearing:

* **Chips are not rebuilt on a tap.** Only membership and order changes rebuild
  the row; on/off and pin state are painted onto the existing nodes. Replacing
  the node between the two halves of a double-click is what stopped the browser
  from ever counting one — see below.
* **Dragging is pointer events, not HTML5 drag-and-drop.** HTML5 DnD needs a
  `dataTransfer` dance, differs across browsers, and does not fire for touch at
  all. A press only becomes a drag after 5px of movement, so an ordinary tap
  still reaches the click handler untouched — which matters, because tap and
  double-tap on those same chips do the on/off and the pin.

### Dragging books around

The chip you grab follows the cursor and the row opens a gap where it will
land; the arrangement is read back off the DOM on release, so what you drop is
exactly what you were looking at. Drag within the row to reorder columns, out
to the Hidden row to remove a book, or back in at any position. An empty Hidden
row reveals itself while you drag so there is somewhere to aim.

Three things had to be right before it stopped feeling laggy, and none of them
were the drag logic:

* **`.chip` declares `transition: all .12s`.** On an element being
  repositioned every pointermove that means it *eases* toward the cursor and
  never catches it. The override has to be `.chip.chipghost`, not `.chipghost`
  — at equal specificity the later rule wins, and `.chip` is declared later.
* **The ghost moves with `transform: translate3d`, not `left`/`top`**, so each
  move is a compositor transform rather than a layout pass.
* **Hit-testing is deferred to an animation frame.** Reading `elementFromPoint`
  plus every chip's rect and then mutating the DOM, inline, at pointer
  frequency, forces a synchronous layout on every event. Once per frame is
  plenty — the gap only has to be right by the time you can see it.

Hit-testing is 2D. The chip row wraps to several lines, so comparing x alone
picked an insertion point on whichever line came first and dragging between
lines did nothing; vertical distance is weighted heavily so the line you are
over wins before x is considered.

Dragging a greyed-out book to reorder it used to switch it back on, because the
move handler selected anything landing in the row. Only a book that has just
*arrived* in the row gets switched on now.

### Pinning a book to one leg

Single-click a book chip to include or exclude it. **Double-click to pin it**:
one leg of every game must then come from that book, whatever it costs, while
every other selected book still competes for the other leg. Double-click again
to unpin. The chip turns violet and reads `1 LEG`, and the summary line says
`one leg must be <book>`, because a constraint that changes every price on
screen must not be invisible.

That is the shape of a promo you can only use once — a free bet, an odds boost,
a profit-boost token. It is the same one-leg-per-market rule the boosts follow,
and for the same reason: both sides boosted is not something you can place.

Which side gets pinned is not obvious, so it is not guessed: both arrangements
are priced (pinned book on the away leg with the free best on home, and the
reverse) and the cheaper pair wins. Pinning to the side where that book happens
to be cheapest can be the wrong call, since that may be the side another book
beats it on by even more.

Two things follow from the pin, both deliberate:

* **The highlighted cell and the Best column follow the pin**, not the raw
  cheapest price — `sideCells` is handed the pair `analyze()` chose rather than
  recomputing its own.
* **The hedge calculator opens on the pinned pair.** It used to default to the
  cheapest quote per side, which would have silently dropped the constraint on
  the way into the screen that actually places the orders. Every quote is still
  listed, so any leg can still be changed by hand.

A game the pinned book does not quote shows no ROI and no **Place order**
button, since no valid pair exists under the constraint. All its prices are
still on screen.

The double-click is the browser's own click counter (`event.detail === 2`),
which follows the OS double-click speed. The first version hand-rolled the
timing with timestamps, and any hand-picked threshold is wrong for somebody: a
real double-click measured **477ms** here, a 400ms window dropped it, and
double-clicking a chip silently just toggled it off and back on. That only
works because the chip node survives a tap — see above.

The pair of single clicks inside a double cancels itself out, so a pinned book
always ends up selected regardless of what it was before.

The pin is deliberately **not** persisted across reloads or market switches. A
promo belongs to one market and one session, and a constraint that quietly
survives a reload is worse than re-applying it with one double-click.

### The header

**Sport** and **Market** are labelled tracks. Sport is a **wheel** in the
literal sense: the **middle slot is the current sport**, with the previous one
to its left and the next to its right, and you change sport by turning the
wheel under it — drag the track, use the arrows, scroll a trackpad over it, or
click a side slot to bring it round the short way. Three slots are visible, so
adding a tenth league costs nothing on screen.

It is **endless in both directions**. The sport list is laid out five times and
the offset is kept modulo one copy's width; because the pattern repeats
exactly, normalising the offset is invisible, and there is always more than a
screenful of track either side, so it keeps turning forever. Turning past the
last sport lands back on the first with nothing to notice.

The highlight follows your finger, but **the sport only changes once the wheel
settles** — switching live would fire a scan for every pixel dragged. A press
only becomes a turn after 4px, so a plain click still reaches the slot under
it, and the click that ends a drag is suppressed.

An earlier version of this was a scroll container rather than a wheel;
`scroll-snap-type: mandatory` pinned its `scrollLeft` to a snap point and the
arrows did nothing. The wheel drives a `transform` and does its own snapping,
so there is no scroll position to fight over.

**Live games** sits last, to the right of Refresh, so it keeps its position
when Refresh hides — with the order reversed, toggling live slid the chip out
from under the cursor that had just clicked it. **Refresh hides while live
games are shown**, since the board is already re-scanning the visible sport
every second.

The header wraps rather than being `nowrap`. Forcing one line kept it to one
line by letting items *overlap* once the content outgrew the window, which is
worse than a second row. What actually keeps it on one line is that toggling
Live now removes width instead of adding it.

Book chips size to their names; only the table columns are fixed width. A
uniform pill grid made short names look padded out and read worse than the
ragged row you are arranging by hand.

### Nine columns

Seven sportsbooks plus two exchanges is wider than most windows (fewer, if you
have hidden some), so the table
scrolls sideways with **Game and Team pinned left, Best and vig/ROI pinned
right** — a row stays identifiable and **Place order** stays reachable at any
scroll position. Both summary columns have to be pinned, not just the vig:
pinning one left it parked on top of `Best`, hiding the column it summarises.
Column widths are only binding under `table-layout: fixed`; with the default
auto layout the browser sized each book column by its header text instead —
"DraftKings" 119px next to "Kalshi" 56px. The invisible `boost` hover hint is
absolutely positioned for the same reason: left in flow it stole width from the
book name, which a fixed column then truncated to make room for something you
cannot see.
The team column's offset is measured from the rendered table (`--gamew`),
because a CSS `width` on a table cell is only a hint and a hardcoded value left
a sliver of the first book column showing through. Horizontal scroll is
preserved across re-renders, which at a 1-second cadence it has to be.

`--arb-bg` is a 10%-alpha tint, so on a *sticky* cell it let the columns
scrolling underneath show straight through on every arbitrage row. The tint is
now composited over an opaque panel colour rather than replacing it.

### Live exchange prices (always on, free)

Kalshi and Polymarket US are polled continuously and cost **no Odds API
credits**, so they never stop — the **Auto** chip only changes how fast (1s
armed, 10s otherwise; see above). With auto off, only the exchange columns
move: DraftKings and FanDuel stay frozen at the last **Refresh**.

Prices and size are polled on **separate schedules**, because they cost
different amounts and go stale at different rates:

| what | every | cost |
|---|---|---|
| BBO prices, all games | **1s** | 2 requests (one batch feed per venue) |
| BBO size, **thin** tracked markets | **1s** | 1 request per Polymarket market; Kalshi batched |
| BBO size, **deep** tracked markets | **5s** | same, amortised |

Size gets an **adaptive cadence per market**, because resting size is wildly
uneven — a live board can hold anything from **$3 to $600,000** at the touch.
A market with less than `DEPTH_THIN_USD` ($500) resting on its thinner side is
re-checked every second, since that size can vanish between ticks and it is
exactly the case where an order won't fill as displayed. Deeper books are
trusted for five seconds. Markets re-classify themselves as their books fill
and empty.

Measured on a live board: of 15 tracked markets, 2 were thin and polled
sub-second while the other 13 sat on the 5s sweep — about 3.6 requests/second
for depth, versus 6/second if everything were polled at 1s, and it stays flat
as `DEPTH_GAMES` grows. Kalshi costs one batched request for every due ticker;
Polymarket needs one per market, which is what the adaptive tier really
protects.

Knobs (top of the `<script>` in `static/index.html`): `DEPTH_GAMES`,
`DEPTH_THIN_USD`, `DEPTH_THIN_MS`, `DEPTH_DEEP_MS`.

A full scan is ~31 HTTP requests, since Polymarket depth needs a call per game
— far too much to run every second. The 1s poll therefore uses each venue's
single batch feed (`/markets` on Kalshi, `/v2/leagues/{league}/events` on
Polymarket US), which already quotes the top of book exactly (verified against
both venues' book and `/bbo` endpoints), so only the resting *size* is missing
from it. Size is refreshed for the **top 5 games** on the board; everything
else keeps the size measured at the last full scan, and **opening an order
ticket pulls that game's size immediately** whatever its rank, so the screen
you trade from is never stale.

Implementation notes:

- Polls are **never overlapping**: the next request is scheduled 1s (or 10s)
  after the previous one *started*, so a slow round trip delays rather than
  stacks.
- Quotes are updated **in place**, so an open order ticket keeps its stake
  input and just re-prices — it refreshes on every tick.
- Rows **re-rank live** as prices move — except while the cursor is inside the
  table (or an order ticket is open), where the current order is held so a
  Place order button can't slide out from under you. It re-ranks the moment the
  cursor leaves.
- On error the price feed backs off to 3s and the header shows the retry count;
  depth failures are silent since prices keep flowing.

Games that have already started are hidden by default (their scanned prices go
stale fast). The **Include live games** toggle sits at the right of the
controls bar and turns **red** when on; live games are then tagged `LIVE`.

## Trading from the UI

When a venue's credentials are configured, its price cells become clickable
(a green BUY tag appears on hover) and open an order modal: limit price
(prefilled with the current ask), contract count, cost/payout preview,
estimated Kalshi fee, and your live balance. Orders are **limit, buy YES /
buy outcome** only.

- **Kalshi** — works with the existing `KALSHI_API_KEY_ID` +
  `KALSHI_PRIVATE_KEY_PATH` (RSA .pem) in `.env`.

  **Exchange sharding — read this if orders fail.** Kalshi runs separate
  matching engines and holds collateral **per shard**: 0 default, 1 exotics,
  2 crypto/commodities, **3 select sports (including MLB)**. A balance sitting
  on shard 0 cannot trade MLB on shard 3 — the order is rejected with
  `404 user_not_found`. Orders carry the market's own `exchange_index` so they
  route correctly, but the money still has to be there. The trading bar shows
  the per-shard breakdown and offers a transfer control when the sports shard
  is empty (`POST /api/kalshi/transfer`, amounts in dollars; Kalshi's API takes
  centicents). You can also move it in the Kalshi app.

  Note: the request signature covers the **path only** — signing a path with a
  query string attached returns `401 INCORRECT_API_KEY`.

  Orders go to the **V2**
  endpoint `POST /trade-api/v2/portfolio/events/orders`; the legacy
  `/portfolio/orders` path now returns HTTP 410
  (`deprecated_v1_order_endpoint`). V2 quotes from the YES leg in fixed-point
  dollar strings — `side: "bid"` buys YES, `count: "10.00"`,
  `price: "0.4800"` — instead of V1's `action`/`side`/`yes_price` cents.
- **Polymarket US** — uses the Retail API at `api.polymarket.us`. Requests are
  signed with **Ed25519**; there is no wallet private key and **no .pem file**
  (that is Kalshi's scheme). Add to `.env`:
  - `POLYMARKET_KEY_ID` — the Key ID
  - `POLYMARKET_SECRET_KEY` — the Secret Key (base64; **shown only once**)

  Get both by completing identity verification in the Polymarket US iOS app,
  then creating a key at [polymarket.us/developer](https://polymarket.us/developer)
  signed in with the same method (Apple/Google/email) you used in the app.
  Accounts are funded in **USD** (debit card, ACH, or wire) — no crypto wallet,
  no USDC, no Polygon.

Credential edits are picked up without restarting the server — hit **recheck**
in the trading bar. (Changing Python code still needs a restart.) When a venue
won't connect, the exact reason is shown inline in the trading bar.

The header shows each venue's connection status and balance
(`GET /api/trading/status`). Order placement is `POST /api/trade`. The server
binds to 127.0.0.1 only — don't expose it; anyone who can reach it can trade.

## Quickbuy (live mode only)

Turning on **Include live games** reveals an **auto amount** box and an
**Auto-hedge** toggle next to Refresh, and arms a one-click quickbuy on every
connected exchange price. Hovering such a cell shows a red **⚡ N** button
where N is the contract count it will send; clicking it places that limit order
immediately, with no ticket and no confirmation.

The button is deliberately a small target inside the cell rather than the whole
cell, so an exploratory click while reading the board can't fire a live order.
Clicking anywhere else in the cell still opens the normal ticket.

Sizing depends on the toggle:

- **Auto-hedge on** (default): the auto amount is treated as a stake you have
  already placed on the **other** side at the best sportsbook price, and the
  order is sized so profit is equal whichever way the game goes —
  `contracts = amount / other_side_book_probability`. E.g. $50 on Boston at
  +102 → 101 contracts on the Angels, paying $101 either way.
- **Auto-hedge off**: it simply buys the auto amount outright on the side you
  click (`contracts = amount / price`) — a naked directional bet.

The amount, the toggle, and live mode itself persist in `localStorage`.

### Kalshi shards — there is no "sports" shard

Kalshi runs several matching engines and holds collateral **per shard**, so
money on the wrong one cannot fill an order regardless of the headline balance.
The shard is never assumed from the sport, because it varies and Kalshi can
move a series at any time (verified 2026-09-10):

| markets | shard |
|---|---|
| MLB, 1+ HR, ATP, WTA | 3 |
| NFL, NCAAF, NBA | 0 |

`market_shard()` reads `exchange_index` off the market itself and caches it.
An underfunded shard is rejected by Kalshi with an **opaque HTTP 400** that
names neither the shard nor the shortfall, so `place_order` pre-flights the
check and raises something usable instead:

> not enough collateral on Kalshi shard 0, where KXNFLGAME-…-NYG trades: this
> order needs $500.00 and that shard holds $300.00 — you have $200.00 on shard
> 3, which can be moved to shard 0.

`exchange_index: -1` **is** valid — it is Kalshi's auto-route sentinel. Only
out-of-range positives are rejected (`exchange_index_must_be_between_0_and_3`),
so -1 stays the fallback when the market's own shard cannot be read.

An earlier version hardcoded shard 3 as "the sports shard", which was true only
while the board was baseball. Once football arrived it did real damage: the
order ticket showed shard 3's balance on NFL tickets, and `status()` warned
that correctly-placed shard-0 collateral was "stranded" and should be moved —
advice that would have broken football trading. Both now follow the market.

### Fill priority — for hedging a bet you already placed

If the sportsbook leg is already locked, the exchange leg **must** fill: an
unfilled hedge leaves a naked position, which is far worse than paying a few
cents. `HEDGE_FILL_MODE` (on by default) makes that the priority:

- **Orders sweep the book.** The limit is set `HEDGE_MAX_SLIPPAGE_CENTS`
  (default 5c) *through* the touch, not 1c. On a CLOB you are filled at each
  resting level's own price, so the limit is a **ceiling, not the price you
  pay** — it just means you're willing to eat several levels to get done.
- **A moving market is never a reason to refuse.** In price-priority mode a
  jump beyond `MAX_ORDER_SLIPPAGE` rejects the order. That is exactly wrong
  when a hedge depends on it, so in fill mode that guard is off.
- **Good-till-cancelled, deliberately.** Whatever can't fill immediately keeps
  working at a price *above* the market rather than being cancelled, so the
  hedge keeps trying to complete itself.
- **Partial fills are shouted, not whispered.** The result reads
  `PARTIAL: filled 59 of 101, 42 NOT filled — you are short that much hedge`.

**Do you need market orders?** No, and on one venue you couldn't have them:

| venue | market order? | what is used |
|---|---|---|
| Kalshi | **No** — V2 requires `price`; there is no market type | aggressive sweeping limit |
| Polymarket US | Yes (`ORDER_TYPE_MARKET` + slippage tolerance) | aggressive sweeping limit anyway |

An aggressive limit on a CLOB takes the same liquidity a market order would,
at the same prices, but with a ceiling you chose. Polymarket's market order is
immediate-or-cancel, which would silently abandon the unfilled remainder of a
hedge — the sweeping GTC limit keeps working instead, so it is used on both
venues for consistent, predictable behaviour.

### Will it fill? (checked before you click)

The depth poll now also returns **cumulative fillable size** — how many
contracts are available within the sweep distance, not just at the touch —
computed from the same book read, so it costs nothing extra. This matters: one
Kalshi market showed **10 contracts at the touch but 28,477 within 5c**, so
touch depth alone is badly misleading.

The order ticket shows `fillable 310,322 within 5c` per leg, and if the leg is
larger than that it warns **before** you commit:

> **HEDGE WON'T FULLY FILL:** only 310,322 of 872,291 contracts are available
> within 5c — you would be left short 561,969 contracts of cover

The quickbuy tooltip carries the same warning. This is the one risk the code
cannot remove: if the book genuinely isn't deep enough, no order type can
conjure liquidity. What it can do is tell you first.

### Polymarket prices are always YES-denominated

Polymarket's order `price` **always refers to the YES side**, whichever outcome
you are buying — their docs: *"If you want to buy NO at $0.40, you're really
selling YES at $0.60."*

So a NO order must send `1 - what_you_will_pay`. Sending the NO price directly
places the order far from the market and it rests forever: an 84c NO order sent
as `price: 0.89` is read as *paying 11c for NO*, 73c below the touch. That is a
real bug this project shipped and fixed — if a Polymarket NO order ever fails
to fill while manual buying works, check this first.

Rounding follows the same logic: buying YES rounds the wire price **up** a
tick, buying NO rounds it **down**, since paying more for NO means selling YES
lower. Both directions assert the result actually crosses before sending.

### Every order reports whether it filled

All four order paths — quickbuy, hedge-ticket legs, the click-a-cell trade
modal, and prop hedges — confirm with the venue instead of assuming. Filled on
placement shows green instantly; anything else resolves to filled / partial /
not-filled inside 5 seconds. See *Order feedback* above.

### Order latency

Click-to-exchange is ~**85ms**, down from ~350ms. Three things got it there:

| change | effect |
|---|---|
| Pooled keep-alive HTTP sessions in both traders | Kalshi request 133ms → 43ms, Polymarket 162ms → 72ms |
| Background warmer (every 25s) | first click after idling never pays a TLS handshake |
| Reference-price fast path | removes one whole round trip (~70ms) from the click |

Every request used to open a **new TLS connection**; both traders now hold a
pooled session, and a daemon thread keeps it warm with a trivial unauthenticated
GET so an idle session doesn't go cold (verified: still ~110ms after 40s idle).

The fast path matters most. Order placement re-quotes against the live book,
which meant a `GET` book read followed by the `POST` — two sequential round
trips. The UI already polls exchange prices every second, so it now sends that
price with the order as `ref_price` plus its age; when the age is under
`ORDER_REF_MAX_AGE_MS` (1500ms) the server trusts it and goes straight to the
`POST`.

This does not weaken the fill protection, because **a limit order can never
fill above its limit** — the exposure from a sub-second-old reference is
"might rest unfilled", never "filled too high". And it degrades safely: if the
reference is older than 1.5s, the feed is down, or you hand-edit the limit price
in the ticket, the server falls back to the full pre-flight read and the
`MAX_ORDER_SLIPPAGE` chase guard.

Rejected as not worth it: HTTP/2 multiplexing (would need a new dependency for
marginal gain on a single request) and signing orders in the browser (the
private keys must stay server-side).

### Safety is the catch on instant buy

The **Safety** toggle gates one-click buying — it is not a sizing mode:

| Safety | header badge | price cells |
|---|---|---|
| **On** (default) | `INSTANT BUY OFF` (grey) | open the normal order ticket |
| **Off** | ⚡ `INSTANT BUY ON` (red, pulsing) + solid red chip | one click sends a real order |

The badge states only the armed state; the reason it is off is already next to
it (the Safety chip, or an empty amount box). Safety **always starts On** on a
fresh load and is not remembered in the off position — a page that came back
already armed would turn a stray click into a real order. The auto amount is
remembered.

With the catch on, no bolt buttons exist at all, so there is nothing to fire by
accident. Clearing the auto amount disarms it too. Instant buy **always** sizes
as a hedge — the auto amount is treated as a stake already placed on the other
side — because that is the only reason to want one-click speed.

### Order feedback — filled instantly, unfilled within 5s

A resting order is **not** a success: the hedge did not go on. So a fill is
**confirmed with the venue**, never assumed.

| what happened | verdict | when |
|---|---|---|
| placement response already proves a fill | ✅ ORDER FILLED | **instant (0ms)** |
| fills during confirmation | ✅ ORDER FILLED — N contracts | ~0.3s (first check) |
| only partly fills | ⚠️ PARTIAL — filled 59 of 101; you are short 42 contracts of cover | **<5s** (4.4s) |
| never fills | ❌ NOT FILLED — contracts still working; YOU ARE UNHEDGED | **<5s** (4.4s) |
| venue rejected it | ❌ ORDER REJECTED — nothing was placed | instant |

If the placement response doesn't already prove a fill, a `⏳ CONFIRMING FILL…`
toast appears and the order is polled against
`GET /portfolio/orders/{id}` (Kalshi, `fill_count_fp`) or `GET /v1/order/{id}`
(Polymarket US, `cumQuantity`) — about **11 checks** across the window, first
one after 200ms. The loop stops starting checks once there isn't room for one
to return, so the verdict is always on screen **before** 5s rather than after.
Confirmation is free (no Odds API credits).

Colour and dwell time scale with how bad the news is: green 7s, amber 20s, red
30s. Each message carries the venue, contracts and limit, the sportsbook line
the size came from with its age, and the order id. Hedge-ticket legs get the
same confirmation inline.

### The hedge is sized from a fresh line, not the last Refresh

With **Safety On** the contract count depends entirely on the sportsbook price
for the other side — and that price is only as fresh as your last Refresh. On a
live game the gap is not cosmetic: hedging $50 on Boston, the board still read
`+102` (101 contracts) while the live line was `+680` (390 contracts). Sizing
off the stale number would have left the position **3.9x under-hedged**.

So an instant buy re-reads that specific line first. `POST /api/quick_trade`
takes the *inputs* (game, other side, amount) rather than a contract count,
re-reads the sportsbook price, sizes from it, and places the exchange order —
one client round trip for both steps. The toast then reports what it used:

> Kalshi — FILLED 390: … · sized against +680 on FanDuel read 0.4s ago

Because the count is computed at click time, the bolt button shows it as an
estimate (`⚡ ~112`) whenever Safety is on.

Cost and latency:

| click | latency | Odds API credits |
|---|---|---|
| Safety On, line re-read | ~350ms | 1 |
| Safety On, line cached (<5s) | ~97ms | 0 |
| Safety Off (no line needed) | ~90ms | 0 |

The line is cached for `HEDGE_QUOTE_TTL_S` (5s), so firing both legs of
something in quick succession spends one credit, not two. A sportsbook line
does not move meaningfully inside five seconds.

If the game has dropped out of the Odds API feed entirely (common deep into a
live game), the order is **still placed** using the on-screen size, and the
toast says so — an unfilled hedge is worse than an imperfectly sized one.

### Only the best price is armed

Instant buy appears **only on the green best-price cell** for a side. Buying at
a worse price than one visible on the same screen makes no sense, so those
cells aren't clickable-to-fire at all — they still open the normal ticket.

Note the tooltip always names the sportsbook price the hedge is sized against
(e.g. *hedges $50.00 on Boston Red Sox at +102 (FanDuel)*). Sportsbook prices
are frozen at the last **Refresh**, so that assumed price is what the contract
count is derived from — press Refresh (~250ms) if you want the size computed
from current book odds rather than the last snapshot.

## 1+ home runs (the HR tab)

Just another market. Same wheel, same book row, same clock, same order ticket,
same hedge calculator — it refreshes itself like every other view and there is
nothing to press. It used to be the odd one out (a **Scan HR props** button, a
"how many games" box capped at 15, three players per game, no auto-refresh, and
its own bespoke book row) purely because props are billed differently. All of
that is gone.

What remains genuinely different is the billing, and it is handled rather than
worked around:

* **1 credit per GAME, not per sport.** The sportsbook half of a prop only
  exists on the per-event endpoint, so a five-game slate is five credits a
  scan against one for a moneyline refresh.
* **The cadence follows the cost.** The tick period is the slower of the normal
  tier and whatever `AUTO_MAX_CREDITS_PER_MIN` actually affords for the current
  slate size, so it paces itself as the slate grows instead of needing a
  hand-set limit.
* **A scan is reused for `PROPS_CACHE_TTL_S` (5 min).** In practice this does
  most of the rationing: repeat ticks inside the window report
  `credits_spent: 0`, so a whole slate costs ~1 credit/minute rather than the
  arithmetic above.
* **No instant buy.** Deliberate, and the one intentional difference left —
  this view is not for one-click trading.

Two things had to be fixed to make it behave like the rest:

`refreshDerived()` used to bail out early in this view, because props data came
from a manual scan and the live feed would have wiped it. Once props rode the
shared tick, that early return meant the table never re-rendered at all.

The tick also skipped the exchange poll here — or rather, it should have. That
poll matches games by team, and a prop row's teams are `1+ HR` / `No HR`, so
nothing ever matched; and with no sport scope in this view it fanned out across
all seven sports, costing **11 seconds a tick** and pinning the countdown on
`updating…`. It is skipped now, which is safe because the props scan fetches
Kalshi and Polymarket fresh on every call itself — only the sportsbook half is
cached.

Live games are shown here like anywhere else. Hiding the chip meant every
started game was silently filtered out with no way to bring it back, which is
backwards for a market that matters most once the first pitch is thrown.

## Linking a prop to the right market

A 1+ HR row joins three venues on one batter, so the join has to be exact — a
row that pairs one player's book price with another's exchange price looks
perfectly normal and leaves the book leg naked. Two separate defects here, both
now closed:

**The client keyed prop rows by game, not by player.** `gameKey()` is
`sport|away|home|start`, and a prop row's away/home are the two sides of the
bet (`1+ HR` / `No HR`), not teams — so every player in a game produced an
identical key. Measured: **67 rows collapsed to 5 keys, up to 18 players deep.**
The exchange-quote carry-over builds a `Map` from that key, which keeps only
the last of each group and then copied that one player's Kalshi and Polymarket
prices onto every other row in the game. `title` is now part of the key.

That carry-over is also skipped entirely in the HR view. It exists to stop a
column blanking between the sportsbook pull and the next price poll; the props
scan fetches both exchanges itself on every call, so there is nothing to bridge
— and carrying would keep showing a stale price for a player the exchanges had
stopped quoting.

**The server joined on the player's name alone.** Both exchange feeds were
flattened into `{player: market}` across every open market, so a name appearing
twice — a doubleheader, or two dates inside the feed window — silently kept the
last one and could hedge against the wrong game. Entries are now lists, and
`_pick_for_game()` chooses by closest game start within 90 minutes, refusing
rather than guessing when it cannot be established. Kalshi's start comes from
its event ticker, Polymarket's from the market's `gameStartTime`.

Verified across a live slate: 67 rows, 67 distinct keys, 46 carrying both
venues, and every venue reference agreeing with its row's game date.

## The order ticket (primary flow)

Every game row has a yellow **Place order** button in the Vig column. The calculator lets you pick a book
for each side (defaults to the best price) and splits the stake so profit is
identical whichever team wins (`stake_i ∝ implied prob_i`; payout =
total / (p_A + p_B)). Three ways to drive it:

- edit **Total stake** — splits it across both sides;
- edit **one side's stake** — the other side recomputes to hedge it (use this
  when you already hold a position and want the exact hedge size);
- swap books per side — totals and profit/ROI recompute instantly, going red
  when a pairing isn't profitable.

Kalshi/Polymarket US legs (when connected) get a one-click **Buy N @ Xc**
button that places a limit order at the scanned ask for the computed contract
count; **Place both orders** fires both legs when both sides are exchanges.
DK/FD legs show the exact stake and odds to place manually.

Each leg carries the same detail the click-a-cell modal shows, so this is the
only screen you need: whole-contract count, order cost, estimated taker fee,
payout, the BBO price with its resting size, the limit price that will actually
be sent (ask + cross), and your tradeable balance on that venue. It warns
inline when a leg exceeds the size resting at the BBO or your balance.

For Kalshi the balance shown is the **sports-shard** balance, not the account
total — only collateral on shard 3 can fill an MLB order.

Clicking an individual price cell still opens the single-order modal, but it is
no longer the main path.

### Fixing one leg (uneven hedges)

A sportsbook stake of $318.47 looks like exactly what it is, so the size an
even hedge calls for is not always one you want to place. Each leg has a
**🔓 fix** button: lock the stake you actually placed and the other side stays
free to type — the locked leg is never recomputed, by the total box or by the
other leg.

Because an uneven hedge no longer pays the same either way, the totals show the
**P&L of each outcome by name** rather than a single figure:

| leg A (locked) | leg B | if Astros win | if Phillies win |
|---|---|---|---|
| $100 | $162.53 (even) | −$5.52 | −$5.49 |
| $100 | $40 (under-hedged) | **+$117.01** | −$76.73 |
| $100 | $260 (over-hedged) | −$102.99 | **+$51.18** |

The ROI cell reports the worst case, and says "either way" when the two
outcomes actually match.

### Whole contracts

Neither exchange sells fractional contracts, so every exchange size is rounded
to the nearest whole contract (rounded, never truncated; a size that rounds to
0 is rejected rather than silently dropped). Rounding is applied in the UI
*and* re-applied server-side, so a fractional order cannot be submitted even
by calling `/api/trade` directly.

> Careful: polymarket.us's events feed reports `minimumTradeQty: 0.01`, which
> suggests fractional sizes are allowed. They are not — its own docs state
> "all trades are executed in whole event contracts". That field is ignored.
>
> Kalshi is the opposite case: its **V2** API *does* accept fractional
> contracts (`FixedPointCount`, minimum granularity 0.01). We still round
> Kalshi to whole contracts by choice — it keeps both venues consistent and
> hedges legible. To allow fractional Kalshi sizes, relax `roundContracts()`
> in `static/index.html` and the rounding in `kalshi_trader.place_order()`.

Because sizes round, the calculator reports what you will *actually* get
rather than the ideal split: each leg shows its whole-contract count, the
totals show **actual cost**, and profit is the **worst case** across the two
outcomes (with the better outcome noted when rounding makes the sides differ).
The trade modal likewise previews cost, payout, and fee from the rounded
count, and snaps the field when you leave it.

Requires `ODDS_API_KEY` in `.env`. Dependencies: `requests`, `python-dotenv`.

## Output

- Console table per scan, games sorted by lowest combined implied probability;
  true arbs marked `*** ARB x.xx% ROI ***`.
- `data/scans.jsonl` — every quote from every scan (for later analysis).
- `data/opportunities.csv` — arbitrage hits only.

## Profit boosts

Sportsbook column headers hide the control until you want it:

- **Unboosted** — hovering shows a faint `boost` hint; clicking turns the header
  **yellow** and swaps in a field you type the percentage into. Enter (or
  clicking away) applies it, Esc cancels.
- **Boosted** — the header stays yellow and reads e.g. `FanDuel +40%`; hovering
  shows `clear` and **clicking again removes the boost immediately**. To change
  a boost, clear it and click once more.

The table will not re-render while a boost field is open, so the one-second
exchange feed can't tear the field out from under you mid-type.

Boosts **reset when you switch tabs** — a promo applies to a specific market,
so it shouldn't quietly carry from moneylines to home runs.

When editing you set two things: the **percentage**, and an optional
**minimum price** the leg must be at or longer than — promos are usually
conditional ("must be +100 or greater"). A boosted header reads
`FanDuel +50% ≥+100`. Legs shorter than the minimum simply don't qualify and
stay at their raw price.

## Bonus bets

A bonus bet is a free stake that does **not** come back when it wins: $100 at
+200 pays $200, not $300. All its value is in the profit leg, so the way to
bank it is to put it on the longest price you can, hedge the other side with
real money, and keep the difference.

**Double-click a book's column header** to set one; a single click is still a
boost. Hovering a header spells it out: `boost(1) bonus(2)`, each word coloured
like the state it produces. Two fields: the amount, and an optional minimum
price the bonus leg must be at or longer than. Setting one **pins that book**,
because the free stake can only be placed there. Sportsbooks only — the
exchanges have no such promo.

**One more click removes it**, exactly as a click clears a boost. Falling
through to the boost editor instead left a bonus with no way off at all, and
offered to stack a boost on top of it.

### The maths

With `B` the bonus, `dA` the bonus leg's decimal price and `dB` the hedge's:

```
if the bonus leg wins   B(dA - 1) - s        ← stake is NOT returned
if the hedge leg wins   s·dB - s
```

Equal payouts either way gives the stake and the guaranteed profit:

```
s      = B(dA - 1) / dB
profit = B(dA - 1)(dB - 1) / dB
```

The bonus costs nothing, so `profit` is raw extraction and `profit / B` is the
conversion rate. It can never reach 100%. A long leg against a tight hedge gets
close:

| bonus | bonus leg | hedge | stake | banked | rate |
|---|---|---|---|---|---|
| $100 | +150 | −170 | $94.44 | $55.56 | 55.6% |
| $100 | +200 | −200 | $133.33 | $66.67 | 66.7% |
| $100 | +1100 | −1200 | $1015.43 | $84.62 | 84.6% |

Both sides of every game are priced, because which side takes the free stake is
not always obvious — the longest price usually wins, but not when the hedge
back is dreadful. The bonus leg is priced **raw**: a boost is a separate promo
and books do not let you stack one on a bonus bet. The hedge is a real bet, so
it takes the best effective price available, boosts and exchange fees included.

### What the board shows

With a bonus live the board answers a different question — not "where is the
thinnest market" but "where does the free stake bank the most" — so it **ranks
by raw winnings**, highest first, with games the bonus cannot be placed on at
the bottom. The Vig column leads with the money:

```
$84.62
85% of bonus
0.64% vig
```

Unlike a boost, a bonus **follows you between markets**. It is a balance on an
account, not a property of one game list, so it survives switching sport or
market — and its pin has to survive with it. Clearing the pin on a switch while
leaving the bonus set left it half-applied: still priced, but no longer forced
onto the book you can actually place it at.

### A boost has to fit its column

Book columns are a fixed 94px, and the boost control lives in the header, so
everything it shows has to fit inside that. Two states did not:

* **While editing**, the header rendered the book's name plus a percent field,
  the word "min" and a minimum-odds field — about 120px of content. The fields
  were pushed out of frame. The name now steps aside for the duration: which
  column you clicked is obvious, it turns yellow, and the tooltip names the
  book. The separator is `≥` rather than the word.
* **Once applied**, it read `DraftKings+40% ≥+100clear` on one line. The boost
  details now sit on the SECOND line — the same strip the hover hint uses — so
  an applied boost costs no column width at all, and `clear` appears there on
  hover instead of reserving space permanently.

Measured across all three states (idle, editing, applied) at the same 94px:
no overflow.

### One boosted leg per market

A boost is a single promo you can use on **one** bet. Applying it to every
quote from that book would boost both sides of a market at once — something no
promo allows, and which invents arbitrage you could never take.

So per game, each boosted book places its boost on exactly **one** side: the
side where it buys the most (where the boosted price beats the best rival price
on that side by the widest margin, among legs that meet the minimum). The other
side of that book stays raw. Only the chosen leg shows the ▲ marker, and every
downstream number — best price, vig, ranking, hedge sizing — inherits the
constraint because they all read `eProb()`, which honours the plan.

A boost lifts the **profit**, not the stake — so +200 with a 40% boost becomes
**+280**:

    decimal_boosted = 1 + (decimal - 1) × (1 + pct/100)

Setting one re-computes everything downstream from the boosted price: the cell
odds and implied percentage (marked ▲), best-price highlighting, the Best
column, vig/ROI, **row ranking**, the hedge calculator's stake split and
contract counts, and the size an instant buy sends (the boost is passed to the
server so its fresh-line sizing matches). Boosts persist in `localStorage`.

Exchange columns deliberately have **no** boost control: a Kalshi or Polymarket
price is what you actually pay, and must never be re-scaled.

This is global by construction. Boost controls are generated from whatever book
columns the current view has, so a market added later gets them for free —
verified by the HR view, where the control appeared on `betrivers` (a book the
feature was never written against) and a 50% boost turned +420 into +630▲,
flipping the row from 2.26% vig to a +3.38% ROI arb.

### How that is kept consistent

Prices are read through **one layer** rather than at each call site:

```js
Odds.american(p)         // formatting
Odds.boostProb(p, pct)   // the profit-boost transform
eProb(q) / eAmerican(q)  // the effective price of a quote, boosts applied
```

Every comparison, ranking, vig, hedge size and label calls `eProb`/`eAmerican`
(21 and 8 call sites), so the next adjustment plugs into one function instead
of being patched into twenty. The raw `q.prob` from the scan is kept untouched
as the source of truth, and execution prices (`meta.ask`) are never boosted.

## Price semantics

Every quote is normalized to the implied probability you pay per $1 of payout:

- DK/FD: American odds converted (vig included).
- Kalshi: the **top of book** YES ask, plus its 0.07·p·(1−p) taker fee
  (`INCLUDE_KALSHI_FEES`). Kalshi's book returns bids only, so the YES ask is
  derived as `1 − (best NO bid)`.
- Polymarket US: the **top of book** for the side you're taking — long side
  buys at the best offer, short side sells YES into the best bid
  (`1 − bestBid`) — plus its taker fee, read from the market's own
  `feeCoefficient` (0.06 today) rather than hardcoded
  (`INCLUDE_POLYMARKET_US_FEES`).

### Only BBO prices are quoted

Both exchanges are priced strictly at the best bid/offer, because that is the
only price you can realistically expect to fill — no mids, no last-trade
prices. Each exchange quote also carries the **size resting at that price**,
shown in the cell tooltip as `BBO 0.48 x520370`:

- Kalshi depth comes from the batched order-book endpoint
  (`/markets/orderbooks?tickers=…`, repeated params, 20 per call).
- Polymarket US depth comes from `/v1/markets/{slug}/book` — one call per game,
  fetched in parallel. Its ask side is keyed `offers`, and note the `/bbo`
  endpoint's `bidDepth`/`askDepth` are price-**level** counts (e.g. 35/30), not
  contract sizes — don't mistake them for depth.

### Orders are re-quoted live, at the ask

A scan is a snapshot. If the book moves between the scan and your click, an
order priced off that snapshot can land at or below the *current* bid, where it
just rests unfilled — buying at the bid, effectively. So order placement
ignores the scan price as a limit and instead:

1. re-fetches the live top of book at submit time;
2. **refuses to send the order at all** if that fetch fails — it is never
   priced off the scan. (An earlier version fell back to the stale price and
   crossed one tick from *that*, which is exactly how an order gets submitted
   at 25.5c into a 27c ask: below the market, resting unfilled. The slippage
   guard below could not catch it either, because the "live" price it compared
   against *was* the stale one.)
3. places the limit `ORDER_CROSS_CENTS` (default 1c) **through** the live best
   ask, ceil-snapped to the venue tick so it can never round to *below* the
   ask — on a CLOB resting liquidity sets the fill price, so crossing normally
   costs nothing and you fill *at* the ask. Cents, not ticks: a Polymarket US
   tick is only 0.5c, too thin to reliably cross.
4. refuses the order if the live ask is more than `MAX_ORDER_SLIPPAGE`
   (default 3c) above what you were shown, rather than chasing a market that
   ran away.

The confirmation states the limit sent *and* the live ask it crossed, so a
resting order is obvious immediately. Note that exchange fees are charged at
execution and are **not** part of the limit price — a venue UI that displays
fee-inclusive prices will read ~1c higher than the limit we send at even money.

Both knobs live in `scanner/config.py`. The confirmation reports the limit
sent, the live ask, and whether the order filled, partially filled, or rested.

The trade modal shows the available size and warns when your order exceeds it
(the excess rests unfilled rather than filling worse, since these are limit
orders) or when you bid above the best ask. Hedge legs carry the same warning
inline. Depth varies enormously — MLB winner markets are often 100k+ contracts
deep at BBO, but thin markets do appear (one Polymarket US side had 59).

**Exchange fees dominate small edges.** At mid prices the taker fee is ~1.5c
per contract, which is larger than a typical cross-book arb. Turning the fee
flags off makes phantom arbs appear: on one scan the best pair read 0.9983
(an apparent arb) fee-free versus 1.0132 (a 1.3% loss) with fees applied. Leave
them on.

Both exchanges trade **whole contracts** only, so the hedge calculator rounds
sizes and a hedge can be off the ideal split by a few cents. A flagged arb is
executable at displayed prices subject to depth — polymarket.us BBO includes
`bidDepth`/`askDepth`, and books can be thin, so sanity-check on-site.

## Sports covered

| sport | Odds API | Kalshi | Polymarket US | matching | lookahead |
|---|---|---|---|---|---|
| MLB | `baseball_mlb` | `KXMLBGAME` | `mlb` | alias table | 36h |
| NFL | `americanfootball_nfl` | `KXNFLGAME` | `nfl` | roster | 240h |
| NCAAF | `americanfootball_ncaaf` | `KXNCAAFGAME` | `cfb` | roster | 240h |
| NBA | `basketball_nba` | `KXNBAGAME` | `nba` | roster | 36h |
| CBB | `basketball_ncaab` | `KXNCAABGAME` | `cbb` | roster | 36h |
| ATP | `tennis_atp_*` | `KXATPMATCH` | `atp` | player name | 36h |
| WTA | `tennis_wta_*` | `KXWTAMATCH` | `wta` | player name | 36h |

Football looks ~10 days ahead because it is a weekly sport — a 36-hour window
would show an empty board midweek. NBA and CBB are configured but out of season
as of writing, so they return nothing.

### Matching teams without an alias table

Each venue names teams differently, and a hand-written table cannot cover ~260
college programs:

| | NFL | NCAAF |
|---|---|---|
| Odds API | `New England Patriots` | `Miami Hurricanes` |
| Kalshi | `New York G`, `Kansas City` | `UCLA`, `San Jose St.` |
| Polymarket | full name | `Hurricanes` (bare nickname) |

So Kalshi truncates from the front and Polymarket (in college) keeps only the
tail. `match_mode="roster"` resolves against **the current scan's own canonical
names** (`scanner/roster.py`): names match as an anchored token sequence from
either end, with each token a prefix of the canonical one, so *"New York G"*
reaches *New York Giants* and *"San Jose St."* reaches *San Jose State*.

Bare nicknames collide — a Saturday slate has several *Wildcats* — so the two
sides of a fixture are resolved **together**, and only the pairing that
corresponds to a real fixture survives. Where that is still ambiguous the quote
is dropped rather than guessed: on a full college slate 80 of 85 games matched,
the rest being lower-division fixtures the sportsbooks never priced.

Tennis needed two generalisations:

- **No fixed roster.** Team sports resolve names through an alias table; an
  individual sport can't, because the field changes every tournament. Those
  sports set `match_mode="name"` and resolve through `canonical_person()`,
  which strips accents and case so *"Botic van de Zandschulp"* (Odds API) and
  *"Botic Van de Zandschulp"* (Kalshi) are the same entrant. Kalshi carries the
  full name in `yes_sub_title`, so no ticker-code table is needed.
- **Tournament-scoped keys.** The Odds API has no single tennis key — it is
  `tennis_atp_us_open` and so on, appearing and disappearing with the calendar.
  `odds_api_sport_prefix` resolves live keys from the **free** `/v4/sports`
  list at scan time, so nothing breaks when a tournament ends.

### Navigation

Two levels, top-left. A **sport** picks the family; a **subcategory** picks the
market within it:

| sport | subcategories |
|---|---|
| Baseball | Moneyline · 1+ HR |
| Tennis | ATP · WTA |

Adding a market is a line in the `NAV` table rather than another branch in the
render path — a sub either names a `sport` (filters the moneyline dataset) or
sets `props: true` (uses the prop dataset).

Because one sport is on screen at a time, the selection also **scopes the live
feed**: each sport is a separate series and league call per venue, so polling
every enabled sport would multiply the request rate for data you can't see.
The poll narrows to the visible sport (verified: `atp`, `wta`, `mlb` as you
switch), holding it at 2 requests/second however many sports exist.

Each enabled sport costs 1 Odds API credit per Refresh (tennis, 1 per active
tournament), so `ENABLED_SPORTS` is what drives spend — currently 3.

## Adding a sport

1. Add a team table to `scanner/teams.py` (canonical = The Odds API full name;
   aliases = short codes used in Kalshi tickers / Polymarket slugs, city names).
2. Add a `SportConfig` entry in `scanner/config.py` (Odds API sport key, Kalshi
   series ticker, polymarket.us league slug + its game-winner
   `sportsMarketType` — list the available types with
   `curl -s "https://gateway.polymarket.us/v2/leagues/<league>/events?limit=5"`).
3. Append the key to `ENABLED_SPORTS`.

Note: each enabled sport costs 1 extra credit per scan, so scans get
proportionally less frequent.

## Config knobs (`scanner/config.py`)

- `ACTIVE_HOURS_LOCAL` / `LOCAL_TZ` — when to scan (default 11:00–23:00 CT).
- `MONTHLY_CREDITS`, `CREDIT_RESERVE`, `MIN/MAX_INTERVAL_S` — pacing.
- `GAME_HORIZON_HOURS` — only compare games starting within this window.
- `ARB_ALERT_THRESHOLD` — raise above 1.0 to also log near-arbs.

## Known quirks handled

- Kalshi's `occurrence_datetime` is ~3h off; game start is parsed from the
  event ticker (which encodes Eastern time) instead.
- Doubleheaders are matched by team pair + closest start time (±90 min).
- polymarket.us calls the moneyline `baseball_team_full_game_winner` (not
  `moneyline`, as polymarket.com does), and both sides of a game share one
  `marketSlug` — the side is chosen with `long` (YES) vs short (NO).
