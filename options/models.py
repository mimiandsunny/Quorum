from datetime import date, datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class OptionType(str, Enum):
    CALL = "call"
    PUT = "put"


class OptionChainSource(str, Enum):
    MANUAL = "manual"
    YFINANCE = "yfinance"
    IBKR = "ibkr"
    TEST = "test"


class OptionContractSnapshot(BaseModel):
    contract_symbol: str
    ticker: str
    expiration: date
    option_type: OptionType
    strike: float
    bid: float | None = None
    ask: float | None = None
    last_price: float | None = None
    implied_volatility: float | None = None
    open_interest: int | None = None
    volume: int | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None


class OptionChainSnapshot(BaseModel):
    snapshot_id: str = Field(default_factory=lambda: uuid4().hex)
    ticker: str
    captured_at: datetime = Field(default_factory=datetime.now)
    source: OptionChainSource = OptionChainSource.MANUAL
    underlying_price: float
    expirations: list[date] = Field(default_factory=list)
    contracts: list[OptionContractSnapshot] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


class OptionRank(BaseModel):
    ticker: str
    contract_symbol: str
    expiration: date
    option_type: OptionType
    strike: float
    dte: int
    bid: float | None = None
    ask: float | None = None
    mid: float | None = None
    spread_pct: float | None = None
    implied_volatility: float | None = None
    iv_percentile: float | None = None
    iv_label: str = "unknown"
    open_interest: int
    volume: int
    volume_oi_ratio: float | None = None
    premium_dollars: float | None = None
    liquidity_score: float
    flow_score: float
    rank_score: float
    moneyness_pct: float | None = None
    position_gamma_dollars: float | None = None
    tags: list[str] = Field(default_factory=list)


class OptionsSnapshotSummary(BaseModel):
    ticker: str
    snapshot_id: str
    captured_at: datetime
    underlying_price: float
    expirations: int
    contracts: int
    avg_iv: float | None = None
    cheap_vol: int = 0
    rich_vol: int = 0
    unusual_flow: int = 0
    net_position_gamma_dollars: float | None = None


class OptionsDashboard(BaseModel):
    snapshots: list[OptionsSnapshotSummary] = Field(default_factory=list)
    candidates: list[OptionRank] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
