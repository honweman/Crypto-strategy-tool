from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from uuid import uuid4


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    TRAILING_STOP = "trailing_stop"


class TimeInForce(str, Enum):
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"


class OrderStatus(str, Enum):
    CREATED = "created"
    PREFLIGHT_REJECTED = "preflight_rejected"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class ExternalEvent:
    timestamp: datetime
    symbol: str
    event_type: str
    score: float
    description: str = ""


@dataclass(frozen=True)
class TargetSignal:
    timestamp: datetime
    symbol: str
    target_weight: float
    confidence: float
    reason: str


@dataclass(frozen=True)
class Order:
    timestamp: datetime
    symbol: str
    side: Side
    quantity: float
    order_type: OrderType = OrderType.MARKET
    reason: str = ""
    id: str = field(default_factory=lambda: str(uuid4()))


@dataclass(frozen=True)
class Fill:
    timestamp: datetime
    symbol: str
    side: Side
    quantity: float
    price: float
    fee: float
    order_id: str
    reason: str = ""


def parse_timestamp(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def clamp(value: float, lower: Optional[float] = None, upper: Optional[float] = None) -> float:
    if lower is not None and value < lower:
        return lower
    if upper is not None and value > upper:
        return upper
    return value
