"""
PaperBroker - FASE 8. Simulated execution with B3 costs, slippage and daily candles.

Timing (no look-ahead): an approved order is stored as PENDING with `signal_date` = candle of the
decision. It is FILLED at the OPEN of the first candle AFTER signal_date (+ slippage). Open positions
are managed day by day on later candles: stop first when both levels are touched, gap fills at the
open, time exit after MAX_HOLD_DAYS. Every fill/exit updates the capital ledger.
"""

import math
from datetime import datetime, timedelta
from typing import List, Optional

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.backtesting.costs import CostModel
from app.backtesting.engine import _exit_for_day
from app.broker.base import BrokerInterface, OrderRequest
from app.capital import service as cap
from app.runtime.params import params
from app.models import (Account, MarketCandle, Order, OperationType, OrderStatus, Position, PositionStatus)
from app.risk.engine import RiskApproval, RiskViolation, verify_approval
from app.services.logger import logger


def _f(v) -> float:
    return float(v) if v is not None else 0.0


class PaperBroker(BrokerInterface):
    name = "paper"

    def __init__(self, db: AsyncSession, account: Account, costs: Optional[CostModel] = None):
        self.db, self.account = db, account
        self.costs = costs or CostModel(fee_rate=params.get("costs.fee_rate_pct") / 100,
                                        brokerage=params.get("costs.brokerage"),
                                        slippage_bps=params.get("costs.slippage_bps"))

    # ------------------------------------------------------------------ orders
    async def submit_order(self, req: OrderRequest, approval: RiskApproval) -> Order:
        if req.side != "buy":
            raise RiskViolation("PaperBroker only opens long positions via submit_order")
        verify_approval(approval, req.ticker.upper(), req.side, req.qty, req.ref_price)
        order = Order(
            account_id=self.account.id, ticker=req.ticker.upper(), operation_type=OperationType.BUY,
            quantity=req.qty, target_price=round(req.ref_price, 2), status=OrderStatus.PENDING,
            signal_date=req.signal_date, stop_loss_price=req.stop_price, take_profit_price=req.take_profit_price,
            atr_value=req.atr, extra_data=req.metadata or {},
        )
        self.db.add(order)
        await self.db.flush()
        await self.db.commit()
        return order

    async def cancel_order(self, order_id: int) -> bool:
        o = await self.db.get(Order, order_id)
        if o and o.status == OrderStatus.PENDING:
            o.status = OrderStatus.CANCELLED
            await self.db.commit()
            return True
        return False

    # ------------------------------------------------------------------ helpers
    async def _candles_after(self, ticker: str, after: datetime) -> List[MarketCandle]:
        return list((await self.db.execute(select(MarketCandle).where(and_(
            MarketCandle.ticker == ticker, MarketCandle.date > after)).order_by(MarketCandle.date.asc()))).scalars().all())

    def _affordable_qty(self, wanted: int, cash: float, entry: float) -> int:
        """Largest qty <= wanted whose cost (notional + fees + brokerage, each rounded to cents) fits in `cash`."""
        unit = entry * (1 + self.costs.fee_rate) + 1e-12
        qty = min(wanted, int(math.floor(max(0.0, cash - self.costs.brokerage) / unit)))
        while qty >= 1:
            notional = round(entry * qty, 2)
            if notional + round(self.costs.fees(notional), 2) <= cash + 1e-9:
                break
            qty -= 1
        return max(qty, 0)

    # ------------------------------------------------------------------ fills
    async def process_pending(self) -> List[dict]:
        """Fill pending BUY orders at the next candle's open."""
        events = []
        pending = (await self.db.execute(select(Order).where(
            Order.account_id == self.account.id, Order.status == OrderStatus.PENDING,
            Order.operation_type == OperationType.BUY).order_by(Order.id))).scalars().all()
        for o in pending:
            candles = await self._candles_after(o.ticker, o.signal_date)
            if not candles:
                if (datetime.utcnow() - o.created_at) > timedelta(days=3):
                    o.status = OrderStatus.CANCELLED
                    events.append({"order_id": o.id, "ticker": o.ticker, "event": "expired"})
                continue
            c = candles[0]
            entry = round(self.costs.buy_price(_f(c.open_price)), 2)
            qty = self._affordable_qty(o.quantity, _f(self.account.available_balance), entry)
            if qty < 1:
                o.status = OrderStatus.REJECTED
                events.append({"order_id": o.id, "ticker": o.ticker, "event": "rejected_no_cash"})
                continue
            notional = round(entry * qty, 2)
            fees = round(self.costs.fees(notional), 2)
            risk = max(_f(o.target_price) - _f(o.stop_loss_price), 0.0)
            rr = (_f(o.take_profit_price) - _f(o.target_price)) / risk if risk > 0 else params.get("risk.risk_reward")
            stop, tp = round(entry - risk, 2), round(entry + rr * risk, 2)   # keep decided distances

            try:
                await cap.reserve_for_buy(self.db, self.account, notional, fees, {"ticker": o.ticker, "order_id": o.id})
            except ValueError:      # never let one unaffordable order abort the whole daily cycle
                o.status = OrderStatus.REJECTED
                events.append({"order_id": o.id, "ticker": o.ticker, "event": "rejected_no_cash"})
                continue
            meta = dict(o.extra_data or {})
            pos = Position(
                account_id=self.account.id, ticker=o.ticker, operation_type=OperationType.BUY, quantity=qty,
                entry_price=entry, entry_date=c.date, stop_loss_price=stop, take_profit_price=tp, atr_value=o.atr_value,
                fees=fees, status=PositionStatus.OPEN,
                technical_score=meta.get("technical_score"), news_score=meta.get("news_score"),
                ml_probability=meta.get("ml_probability"), composite_score=meta.get("composite_score"),
                extra_data={**meta, "last_checked": c.date.isoformat(), "entry_fees": fees, "signal_date": o.signal_date.isoformat()},
            )
            self.db.add(pos)
            await self.db.flush()
            o.status, o.executed_price, o.executed_quantity = OrderStatus.FILLED, entry, qty
            o.execution_date, o.fees, o.position_id = c.date, fees, pos.id
            await self.db.commit()
            events.append({"order_id": o.id, "ticker": o.ticker, "event": "filled", "qty": qty, "price": entry})
            logger.info(f"PAPER FILL {o.ticker} x{qty} @ {entry:.2f} (stop {stop:.2f} / tp {tp:.2f})")

            # entry-day exit check (stop first when both touched)
            lo, hi = _f(c.low_price), _f(c.high_price)
            if lo <= stop or hi >= tp:
                raw, why = (stop, "stop") if lo <= stop else (tp, "target")
                events.append(await self._close(pos, raw, why, c.date))
            else:
                pos.extra_data = {**pos.extra_data, "last_close": _f(c.close_price)}
                await self.db.commit()
        await self.db.commit()
        return events

    # ------------------------------------------------------------------ exits
    async def manage_positions(self) -> List[dict]:
        events = []
        opens = (await self.db.execute(select(Position).where(
            Position.account_id == self.account.id, Position.status == PositionStatus.OPEN))).scalars().all()
        for p in opens:
            meta = dict(p.extra_data or {})
            last_checked = datetime.fromisoformat(meta.get("last_checked", p.entry_date.isoformat()))
            candles = await self._candles_after(p.ticker, p.entry_date)
            for i, c in enumerate(candles, start=1):
                if c.date <= last_checked:
                    continue
                res = _exit_for_day({"stop": _f(p.stop_loss_price), "tp": _f(p.take_profit_price),
                                     "entry_idx": 0, "max_hold": params.get("risk.max_hold_days")},
                                    _f(c.open_price), _f(c.high_price), _f(c.low_price), _f(c.close_price), i, self.costs)
                if res:
                    events.append(await self._close(p, res[0], res[1], c.date))
                    break
                meta.update(last_checked=c.date.isoformat(), last_close=_f(c.close_price))
                p.extra_data = dict(meta)
            await self.db.commit()
        return events

    async def _close(self, pos: Position, raw_price: float, reason: str, when: datetime) -> dict:
        px = round(self.costs.sell_price(raw_price), 2)
        qty = pos.quantity
        sell_notional = round(px * qty, 2)
        exit_fees = round(self.costs.fees(sell_notional), 2)
        cost = round(_f(pos.entry_price) * qty, 2)
        entry_fees = _f((pos.extra_data or {}).get("entry_fees"))
        gross = round(sell_notional - cost, 2)
        net = round(gross - entry_fees - exit_fees, 2)

        pos.exit_price, pos.exit_date = px, when
        pos.gross_pnl, pos.fees, pos.net_pnl = gross, round(entry_fees + exit_fees, 2), net
        pos.pnl_percent = round(net / cost * 100, 3) if cost else 0.0
        pos.status = PositionStatus.CLOSED
        pos.extra_data = {**(pos.extra_data or {}), "exit_reason": reason}
        self.db.add(Order(account_id=self.account.id, position_id=pos.id, ticker=pos.ticker,
                          operation_type=OperationType.SELL, quantity=qty, target_price=round(raw_price, 2),
                          executed_price=px, executed_quantity=qty, status=OrderStatus.FILLED,
                          execution_date=when, fees=exit_fees, signal_date=when,
                          extra_data={"exit_reason": reason}))
        await cap.release_on_sell(self.db, self.account, cost, sell_notional - exit_fees, net,
                                  {"ticker": pos.ticker, "position_id": pos.id, "reason": reason})
        await self.db.commit()
        logger.info(f"PAPER EXIT {pos.ticker} x{qty} @ {px:.2f} ({reason}) net R$ {net:.2f}")
        return {"position_id": pos.id, "ticker": pos.ticker, "event": "closed", "reason": reason,
                "exit_price": px, "net_pnl": net}
