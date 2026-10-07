"""
Risk Engine - FASE 8. INVIOLABLE gate: no order reaches a broker without a signed approval.

`RiskEngine.evaluate` is pure (no I/O, no LLM): same inputs -> same decision, every rule is
recorded in `checks` for audit. The LLM Devil's Advocate only contributes a PRE-COMPUTED
`advocate_counter_score` (from the nightly batch); the engine never calls a model.
"""

import hashlib
import hmac
import time
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Set

from app.capital.rules import CapitalRules, size_position
from app.config import settings
from app.runtime.params import params

APPROVAL_TTL_SECONDS = 600


class RiskViolation(Exception):
    """Raised when someone tries to bypass the Risk Engine."""


@dataclass
class TradeRequest:
    ticker: str
    side: str                      # only "buy" (long-only swing trade)
    price: float                   # reference price (last close)
    atr: Optional[float]
    composite_score: float
    advocate_counter_score: Optional[float] = None
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    custom_take_profit: Optional[float] = None
    split: int = 1
    data_age_days: Optional[int] = None    # business days since the last candle used (None = not checked)


@dataclass
class AccountState:
    equity: float
    cash: float
    exposure: float
    open_positions: int
    open_tickers: Set[str]
    daily_spent: float
    entries_today: int
    circuit_breaker: bool
    daily_pnl: float = 0.0
    remaining_loss_allowance: Optional[float] = None   # R$ still allowed to lose before the tightest limit
    tripped_periods: List[str] = field(default_factory=list)


@dataclass
class RiskConfig:
    min_score: float = 60.0
    atr_stop_mult: float = 1.5
    risk_reward: float = 2.0
    min_rr: float = 2.0
    max_daily_loss_pct: float = 1.5
    max_open_positions: int = 5
    max_entries_per_day: int = 5
    advocate_block_score: float = 80.0
    paper_enabled: bool = True
    real_enabled: bool = settings.ENABLE_REAL_TRADING
    max_data_age_bdays: int = 0            # 0 = freshness rule off

    @classmethod
    def from_params(cls) -> "RiskConfig":
        g = params.get
        return cls(min_score=g("risk.min_score"), atr_stop_mult=g("risk.atr_stop_mult"),
                   risk_reward=g("risk.risk_reward"), min_rr=g("risk.min_rr"),
                   max_daily_loss_pct=g("limits.daily_loss_pct"), max_open_positions=g("capital.max_open_positions"),
                   max_entries_per_day=g("capital.max_entries_per_day"),
                   advocate_block_score=g("risk.advocate_block_score"), paper_enabled=g("risk.paper_enabled"),
                   real_enabled=settings.ENABLE_REAL_TRADING, max_data_age_bdays=g("risk.max_data_age_bdays"))


@dataclass
class RiskApproval:
    """Signed token proving the Risk Engine approved exactly this order."""
    ticker: str
    side: str
    qty: int
    price: float
    issued_at: float
    signature: str


@dataclass
class RiskDecision:
    approved: bool
    ticker: str
    qty: int = 0
    stop_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    risk_reward: Optional[float] = None
    allocation: float = 0.0
    risk_amount: float = 0.0
    sizing_binding: Optional[str] = None
    reasons: List[str] = field(default_factory=list)
    checks: List[Dict] = field(default_factory=list)
    approval: Optional[RiskApproval] = None

    def public(self) -> dict:
        d = asdict(self)
        d.pop("approval", None)
        return d


# ----------------------------------------------------------------------------- signing
def _sign(ticker: str, side: str, qty: int, price: float, issued_at: float) -> str:
    msg = f"{ticker}|{side}|{qty}|{price:.4f}|{issued_at:.3f}".encode()
    return hmac.new(settings.SECRET_KEY.encode(), msg, hashlib.sha256).hexdigest()


def issue_approval(ticker: str, side: str, qty: int, price: float) -> RiskApproval:
    now = time.time()
    return RiskApproval(ticker, side, qty, round(price, 4), now, _sign(ticker, side, qty, round(price, 4), now))


def verify_approval(approval: Optional[RiskApproval], ticker: str, side: str, qty: int, price: float) -> None:
    """Raise RiskViolation unless the approval is authentic, unexpired and matches the order exactly."""
    if approval is None:
        raise RiskViolation("order has no Risk Engine approval")
    if time.time() - approval.issued_at > APPROVAL_TTL_SECONDS:
        raise RiskViolation("Risk Engine approval expired")
    expected = _sign(approval.ticker, approval.side, approval.qty, approval.price, approval.issued_at)
    if not hmac.compare_digest(expected, approval.signature):
        raise RiskViolation("invalid Risk Engine signature")
    if (approval.ticker, approval.side, approval.qty) != (ticker, side, qty) or abs(approval.price - round(price, 4)) > 1e-6:
        raise RiskViolation("order does not match what the Risk Engine approved")


# ----------------------------------------------------------------------------- engine
class RiskEngine:
    def __init__(self, cfg: Optional[RiskConfig] = None, rules: Optional[CapitalRules] = None):
        self.cfg = cfg or RiskConfig.from_params()
        self.rules = rules or CapitalRules.from_params()

    def evaluate(self, req: TradeRequest, st: AccountState) -> RiskDecision:
        c = self.cfg
        checks: List[Dict] = []
        reasons: List[str] = []

        def check(name: str, ok: bool, detail: str):
            checks.append({"rule": name, "passed": bool(ok), "detail": detail})
            if not ok:
                reasons.append(f"{name}: {detail}")

        ticker = req.ticker.upper()
        check("trading_enabled", c.paper_enabled or c.real_enabled, "paper/real trading flags")
        check("long_only", req.side == "buy", f"side={req.side}")
        if c.max_data_age_bdays > 0 and req.data_age_days is not None:
            check("fresh_data", req.data_age_days <= c.max_data_age_bdays,
                  f"last candle is {req.data_age_days} business day(s) old (max {c.max_data_age_bdays}); "
                  f"data collection may have failed" if req.data_age_days > c.max_data_age_bdays
                  else f"last candle {req.data_age_days} business day(s) old")
        check("circuit_breaker", not st.circuit_breaker,
              (f"loss limit reached ({', '.join(st.tripped_periods) or 'manual'})" if st.circuit_breaker else "ok"))
        if st.remaining_loss_allowance is not None:
            check("loss_allowance", st.remaining_loss_allowance > 0, f"R$ {st.remaining_loss_allowance:.2f} left before the day/week/month limit")
        check("min_score", req.composite_score >= c.min_score,
              f"score {req.composite_score:.1f} vs min {c.min_score:.1f}")
        check("no_duplicate", ticker not in st.open_tickers, "already holds/has pending order" if ticker in st.open_tickers else "ok")
        check("max_open_positions", st.open_positions < c.max_open_positions,
              f"{st.open_positions}/{c.max_open_positions}")
        check("max_entries_per_day", st.entries_today < c.max_entries_per_day,
              f"{st.entries_today}/{c.max_entries_per_day}")
        if req.min_price is not None or req.max_price is not None:
            lo = req.min_price if req.min_price is not None else 0
            hi = req.max_price if req.max_price is not None else float("inf")
            check("price_band", lo <= req.price <= hi, f"{req.price:.2f} within [{lo}, {hi}]")
        if req.advocate_counter_score is not None:
            check("devils_advocate", req.advocate_counter_score < c.advocate_block_score,
                  f"counter_score {req.advocate_counter_score:.0f} vs veto {c.advocate_block_score:.0f}")

        # Stop / take-profit from ATR (mandatory: no ATR -> no trade)
        stop = tp = rr = None
        atr_ok = req.atr is not None and req.atr > 0 and req.price > 0
        check("atr_available", atr_ok, "ATR needed for stop-loss" if not atr_ok else f"ATR {req.atr:.4f}")
        if atr_ok:
            risk = c.atr_stop_mult * req.atr
            stop = round(req.price - risk, 2)
            tp_exact = req.custom_take_profit if req.custom_take_profit else req.price + c.risk_reward * risk
            tp = round(tp_exact, 2)
            # R:R is judged on the exact levels. Recomputing it from the cent-rounded stop/target would push a
            # 2.00 target under a 2.00 minimum (e.g. 1.97) and veto sound trades on rounding noise alone.
            rr_exact = (tp_exact - req.price) / max(risk, 1e-9)
            rr = round(rr_exact, 2)
            check("stop_valid", 0 < stop < req.price, f"stop {stop}")
            check("risk_reward", rr_exact + 1e-9 >= c.min_rr, f"R:R {rr:.2f} vs min {c.min_rr:.2f}")

        # Capital sizing (daily budget, exposure, cash, risk per trade)
        sz = size_position(st.equity, st.cash, st.exposure, st.daily_spent, req.price, stop, req.split, self.rules,
                           max_loss_amount=st.remaining_loss_allowance)
        check("sizing", sz.qty >= 1, "; ".join(sz.reasons) if sz.reasons else f"{sz.qty} sh, limit={sz.binding}")

        approved = not reasons
        dec = RiskDecision(approved, ticker, sz.qty if approved else 0, stop, tp, rr,
                           sz.allocation if approved else 0.0, sz.risk_amount if approved else 0.0,
                           sz.binding, reasons, checks)
        if approved:
            dec.approval = issue_approval(ticker, req.side, sz.qty, req.price)
        return dec
