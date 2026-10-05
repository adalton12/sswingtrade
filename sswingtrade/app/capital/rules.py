"""
Capital rules - FASE 7 (pure functions, no I/O).

Spec: weekly working capital R$500, R$100 per operation/day, profits reinvested
(compound), position size scales with equity (R$100 of R$500 = 20%; equity doubles -> R$200).
Interpretation used here:
  * per-operation/day budget  = equity * per_op_pct         (per_op_pct = 100/500 = 20%)
  * the daily budget may be split in up to `max_entries_per_day` entries
  * simultaneous exposure     <= equity * max_exposure_pct  (working capital = previous capital + profits + new deposits)
"""

import math
from dataclasses import dataclass
from typing import List, Optional

from app.config import settings


@dataclass
class CapitalRules:
    per_op_pct: float = settings.MAX_DAILY_ALLOCATION / settings.MAX_WEEKLY_CAPITAL
    max_entries_per_day: int = settings.MAX_ENTRIES_PER_DAY
    max_exposure_pct: float = 1.0
    max_risk_per_trade_pct: float = settings.MAX_RISK_PER_TRADE_PCT
    fee_rate: float = settings.FEE_RATE


@dataclass
class SizingResult:
    qty: int
    allocation: float          # R$ actually allocated (qty * price)
    budget_cap: float          # R$ cap from the daily budget (after split)
    exposure_room: float
    risk_amount: float         # R$ lost if the stop is hit (excl. costs)
    binding: str               # which limit determined the size
    reasons: List[str]


def daily_budget(equity: float, rules: Optional[CapitalRules] = None) -> float:
    rules = rules or CapitalRules()
    return round(max(0.0, equity) * rules.per_op_pct, 2)


def size_position(equity: float, cash: float, exposure: float, daily_spent: float, price: float,
                  stop_price: Optional[float] = None, split: int = 1,
                  rules: Optional[CapitalRules] = None) -> SizingResult:
    """How many shares can be bought under every capital limit."""
    rules = rules or CapitalRules()
    reasons: List[str] = []
    if price <= 0:
        return SizingResult(0, 0.0, 0.0, 0.0, 0.0, "invalid_price", ["invalid price"])

    budget_left = max(0.0, daily_budget(equity, rules) - daily_spent)
    per_entry_cap = min(budget_left, daily_budget(equity, rules) / max(1, min(split, rules.max_entries_per_day)))
    exposure_room = max(0.0, equity * rules.max_exposure_pct - exposure)

    limits = {"daily_budget": per_entry_cap, "exposure": exposure_room, "cash": max(0.0, cash)}
    unit = price * (1 + rules.fee_rate)
    qtys = {k: int(math.floor(v / unit)) for k, v in limits.items()}

    if stop_price is not None and stop_price < price:
        risk_cap = equity * rules.max_risk_per_trade_pct / 100
        qtys["risk_per_trade"] = int(math.floor(risk_cap / (price - stop_price)))

    binding = min(qtys, key=qtys.get)
    qty = max(0, qtys[binding])
    if qty < 1:
        reasons.append(f"cannot afford 1 share under '{binding}' limit "
                       f"(price R$ {price:.2f}, cap R$ {limits.get(binding, 0):.2f})")
    risk_amount = qty * (price - stop_price) if stop_price is not None and stop_price < price else 0.0
    return SizingResult(qty, round(qty * price, 2), round(per_entry_cap, 2), round(exposure_room, 2),
                        round(risk_amount, 2), binding, reasons)


def compound_projection(start_equity: float, weeks: int, weekly_return_pct: float,
                        weekly_deposit: float = 0.0, monthly_deposit: float = 0.0) -> List[dict]:
    """
    Deterministic what-if (NOT a forecast): each week equity grows by weekly_return_pct on the
    equity at the start of the week (after the weekly deposit); monthly deposit every 4th week.
    """
    out, eq, deposited = [], float(start_equity), float(start_equity)
    for w in range(1, weeks + 1):
        eq += weekly_deposit
        deposited += weekly_deposit
        if monthly_deposit and w % 4 == 1 and w > 1:
            eq += monthly_deposit
            deposited += monthly_deposit
        profit = eq * weekly_return_pct / 100
        eq += profit
        out.append({"week": w, "equity": round(eq, 2), "deposited": round(deposited, 2),
                    "profit_week": round(profit, 2), "profit_total": round(eq - deposited, 2),
                    "per_op_budget": daily_budget(eq)})
    return out
