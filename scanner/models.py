"""Shared data structures and odds math."""
from dataclasses import dataclass, field
from datetime import datetime
from math import inf


@dataclass
class Quote:
    """Price to back one team at one book.

    prob = implied probability you pay (cost per $1 of payout, vig/fees
    included). Lower is better for the bettor.
    """
    book: str
    team: str
    prob: float
    detail: str = ""   # e.g. raw american odds or bid/ask
    meta: dict = None  # venue identifiers needed to trade (ticker / token_id)

    @property
    def decimal(self) -> float:
        return 1.0 / self.prob if self.prob > 0 else inf

    @property
    def american(self) -> str:
        d = self.decimal
        if d >= 2:
            return f"+{round((d - 1) * 100)}"
        return f"-{round(100 / (d - 1))}"


@dataclass
class SourceEvent:
    """One game as seen by one source."""
    source: str
    teams: frozenset          # two canonical team names
    start: datetime           # UTC
    quotes: list = field(default_factory=list)  # list[Quote]


@dataclass
class MatchedGame:
    """One game with quotes merged across all sources."""
    sport: str
    home: str
    away: str
    start: datetime
    quotes: list = field(default_factory=list)

    def best(self, team: str):
        qs = [q for q in self.quotes if q.team == team]
        return min(qs, key=lambda q: q.prob) if qs else None

    def books_for(self, team: str):
        return sorted((q for q in self.quotes if q.team == team), key=lambda q: q.prob)


def american_to_prob(odds: float) -> float:
    if odds >= 100:
        return 100.0 / (odds + 100.0)
    return -odds / (-odds + 100.0)
