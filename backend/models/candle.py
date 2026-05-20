from datetime import datetime
from pydantic import BaseModel
from typing import Literal


class Candle(BaseModel):
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int

    model_config = {"json_encoders": {datetime: lambda v: v.isoformat()}}


class CandleResponse(BaseModel):
    symbol: str
    interval: str
    count: int
    candles: list[Candle]
    # Provider that supplied this data. Set by get_candles().
    # One of: "tradingview", "mt5", "synthetic", "demo".
    source: str = "synthetic"


IntervalLiteral = Literal["M5", "M15", "M30", "H1", "H4", "D1", "W1"]
