from __future__ import annotations

import csv
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Union

from .models import Bar, ExternalEvent, parse_timestamp

Event = Union[Bar, ExternalEvent]


class CsvBarDataSource:
    """Reads OHLCV bars from a CSV file."""

    def __init__(self, path: str | Path, symbols: set[str] | None = None) -> None:
        self.path = Path(path)
        self.symbols = symbols

    def __iter__(self) -> Iterator[Bar]:
        with self.path.open(newline="") as file:
            for row in csv.DictReader(file):
                symbol = row["symbol"].strip().upper()
                if self.symbols and symbol not in self.symbols:
                    continue
                yield Bar(
                    timestamp=parse_timestamp(row["timestamp"]),
                    symbol=symbol,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                )


class CsvExternalEventSource:
    """Reads timestamped external events such as news, on-chain, or social signals."""

    def __init__(self, path: str | Path, symbols: set[str] | None = None) -> None:
        self.path = Path(path)
        self.symbols = symbols

    def __iter__(self) -> Iterator[ExternalEvent]:
        if not self.path.exists():
            return
        with self.path.open(newline="") as file:
            for row in csv.DictReader(file):
                symbol = row["symbol"].strip().upper()
                if self.symbols and symbol not in self.symbols:
                    continue
                yield ExternalEvent(
                    timestamp=parse_timestamp(row["timestamp"]),
                    symbol=symbol,
                    event_type=row["event_type"].strip(),
                    score=float(row["score"]),
                    description=row.get("description", "").strip(),
                )


class MergedEventFeed:
    """Combines multiple event streams into timestamp order."""

    def __init__(self, sources: Iterable[Iterable[Event]]) -> None:
        self.sources = list(sources)

    def __iter__(self) -> Iterator[Event]:
        events: list[Event] = []
        for source in self.sources:
            events.extend(source)
        events.sort(key=lambda event: event.timestamp)
        yield from events

