"""Resolving one venue's team names onto another's, without hand-written tables.

The three venues name the same team three different ways, and the differences
are systematic rather than arbitrary:

    Odds API (the spine)   New England Patriots      Miami Hurricanes
    Kalshi                 "New York G", "Dallas"    "UCLA", "San Jose St."
    Polymarket             full name                 "Hurricanes" (nickname)

So Kalshi truncates from the front and Polymarket (in college) keeps only the
tail. A fixed alias table can cover 30 NFL teams but not ~260 college programs,
and bare nicknames collide badly — a Saturday slate can have four different
"Wildcats".

This resolves against the roster of the current scan instead. Names are matched
as an anchored token sequence (each token a prefix of the canonical one, so
"New York G" reaches "New York Giants" and "San Jose St." reaches "San Jose
State"), from either end. Where that is ambiguous, the two sides of a fixture
are resolved **together**: only one pairing usually survives, which is what
kills the "Wildcats" problem.
"""
import re
import unicodedata

_ABBREV = {"st": "state", "univ": "university", "intl": "international"}


def _tokens(name: str):
    s = unicodedata.normalize("NFKD", str(name or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    out = []
    for t in s.split():
        out.append(_ABBREV.get(t, t))
    return out


def _anchored(src, canon):
    """True if `src` tokens prefix-match `canon` from the front or the back."""
    if not src or len(src) > len(canon):
        return False
    for seq, ref in ((src, canon[:len(src)]), (src, canon[-len(src):])):
        if all(c.startswith(s) or s.startswith(c) for s, c in zip(seq, ref)):
            return True
    return False


class Roster:
    """The canonical names of one scan, plus the fixtures they appear in."""

    def __init__(self, events):
        self.names = sorted({t for ev in events for t in ev.teams})
        self._tok = {n: _tokens(n) for n in self.names}
        self.fixtures = [(ev.teams, ev.start) for ev in events]

    @classmethod
    def from_games(cls, games):
        """Build from serialized games (the cached scan), not SourceEvents.

        The live poll has no fresh spine of its own — the sportsbook side only
        moves on a Refresh — so it reuses the canonical names from the last
        scan.
        """
        class _E:
            def __init__(self, away, home, start):
                self.teams = frozenset({away, home})
                self.start = start
        return cls([_E(g["away"], g["home"], g.get("start")) for g in games
                    if g.get("away") and g.get("home")])

    def candidates(self, raw):
        t = _tokens(raw)
        if not t:
            return []
        exact = [n for n in self.names if self._tok[n] == t]
        if exact:
            return exact
        return [n for n in self.names if _anchored(t, self._tok[n])]

    def resolve(self, raw):
        """A single unambiguous name, else None."""
        c = self.candidates(raw)
        return c[0] if len(c) == 1 else None

    def resolve_pair(self, raw_a, raw_b):
        """Resolve both sides of a fixture together.

        Ambiguity usually disappears once the opponent has to agree — only one
        pairing corresponds to a real fixture on the board.
        """
        ca, cb = self.candidates(raw_a), self.candidates(raw_b)
        if not ca or not cb:
            return None, None
        hits = [(a, b) for a in ca for b in cb
                if a != b and any(fx == frozenset({a, b}) for fx, _ in self.fixtures)]
        if len(hits) == 1:
            return hits[0]
        # no fixture agreed; fall back to each side being unique on its own
        return (ca[0] if len(ca) == 1 else None,
                cb[0] if len(cb) == 1 else None)
