"""Transaction costs for B3 swing trade (cash equities)."""

from dataclasses import dataclass


@dataclass
class CostModel:
    """
    fee_rate: B3 emolumentos + liquidacao per side as fraction of notional
              (~0.0325%; check current B3 table, it changes).
    brokerage: fixed brokerage per order in R$ (0 at most Brazilian brokers for stocks).
    slippage_bps: adverse price impact per execution in basis points.
    """
    fee_rate: float = 0.000325
    brokerage: float = 0.0
    slippage_bps: float = 5.0

    def buy_price(self, price: float) -> float:
        return price * (1 + self.slippage_bps / 10_000)

    def sell_price(self, price: float) -> float:
        return price * (1 - self.slippage_bps / 10_000)

    def fees(self, notional: float) -> float:
        return notional * self.fee_rate + self.brokerage
