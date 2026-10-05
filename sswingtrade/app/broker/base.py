"""BrokerInterface: swap PaperBroker for a real broker (Clear/XP/BTG) without touching strategies."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from app.risk.engine import RiskApproval


@dataclass
class OrderRequest:
    ticker: str
    side: str                       # "buy" | "sell"
    qty: int
    ref_price: float                # price the decision was based on
    signal_date: datetime           # candle date of the decision
    stop_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    atr: Optional[float] = None
    metadata: Optional[dict] = None


class BrokerInterface(ABC):
    name = "abstract"

    @abstractmethod
    async def submit_order(self, req: OrderRequest, approval: RiskApproval):
        """MUST verify `approval` (verify_approval) before doing anything."""

    @abstractmethod
    async def cancel_order(self, order_id: int) -> bool: ...
