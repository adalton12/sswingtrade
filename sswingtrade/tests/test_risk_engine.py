import time

import pytest

from app.risk import engine as eng
from app.risk.engine import (AccountState, RiskConfig, RiskEngine, RiskViolation, TradeRequest, issue_approval,
                             verify_approval)
from app.risk.scoring import composite_score, ml_score


def state(**kw):
    base = dict(equity=500, cash=500, exposure=0, open_positions=0, open_tickers=set(), daily_spent=0,
                entries_today=0, circuit_breaker=False)
    base.update(kw)
    return AccountState(**base)


def req(**kw):
    base = dict(ticker="PETR4", side="buy", price=10.0, atr=0.2, composite_score=75.0)
    base.update(kw)
    return TradeRequest(**base)


E = RiskEngine(RiskConfig(min_score=60, paper_enabled=True))


def failed(dec):
    return {c["rule"] for c in dec.checks if not c["passed"]}


def test_approves_good_trade_with_atr_levels():
    d = E.evaluate(req(), state())
    assert d.approved and d.approval is not None
    assert d.stop_price == pytest.approx(9.7)                 # 1.5 x ATR
    assert d.take_profit_price == pytest.approx(10.6)         # R:R 1:2
    assert d.risk_reward == pytest.approx(2.0)
    assert d.qty == 9 and d.allocation == pytest.approx(90.0)  # R$100 budget at R$500 equity
    assert all(c["passed"] for c in d.checks)


@pytest.mark.parametrize("kwargs,st,rule", [
    ({"composite_score": 59.9}, {}, "min_score"),
    ({}, {"circuit_breaker": True}, "circuit_breaker"),
    ({}, {"open_tickers": {"PETR4"}}, "no_duplicate"),
    ({}, {"open_positions": 5}, "max_open_positions"),
    ({}, {"entries_today": 5}, "max_entries_per_day"),
    ({"atr": None}, {}, "atr_available"),
    ({"atr": 0.0}, {}, "atr_available"),
    ({"advocate_counter_score": 85}, {}, "devils_advocate"),
    ({"custom_take_profit": 10.3}, {}, "risk_reward"),       # 0.3 reward vs 0.3 risk = 1:1
    ({"price": 900.0, "atr": 10.0}, {}, "sizing"),             # cannot afford one share under R$100 budget
    ({"min_price": 20.0}, {}, "price_band"),
    ({"side": "sell"}, {}, "long_only"),
])
def test_rejections(kwargs, st, rule):
    d = E.evaluate(req(**kwargs), state(**st))
    assert not d.approved and d.approval is None and d.qty == 0
    assert rule in failed(d)
    assert d.reasons


def test_sizing_uses_remaining_daily_budget_and_exposure():
    d = E.evaluate(req(), state(daily_spent=70))
    assert d.approved and d.qty == 2                          # only R$30 of the daily budget left
    d = E.evaluate(req(), state(daily_spent=100))
    assert not d.approved and "sizing" in failed(d)
    d = E.evaluate(req(), state(exposure=500))
    assert not d.approved


def test_trading_disabled_blocks_everything():
    off = RiskEngine(RiskConfig(paper_enabled=False, real_enabled=False))
    assert "trading_enabled" in failed(off.evaluate(req(), state()))


def test_decision_is_deterministic_and_public_hides_token():
    a, b = E.evaluate(req(), state()), E.evaluate(req(), state())
    assert (a.qty, a.stop_price, a.take_profit_price) == (b.qty, b.stop_price, b.take_profit_price)
    assert "approval" not in a.public()


def test_approval_cannot_be_bypassed_or_forged():
    ap = issue_approval("PETR4", "buy", 5, 10.0)
    verify_approval(ap, "PETR4", "buy", 5, 10.0)               # exact match passes
    for bad in [("PETR4", "buy", 6, 10.0), ("VALE3", "buy", 5, 10.0), ("PETR4", "buy", 5, 11.0)]:
        with pytest.raises(RiskViolation):
            verify_approval(ap, *bad)
    with pytest.raises(RiskViolation):
        verify_approval(None, "PETR4", "buy", 5, 10.0)
    forged = eng.RiskApproval("PETR4", "buy", 50, 10.0, time.time(), "deadbeef")
    with pytest.raises(RiskViolation):
        verify_approval(forged, "PETR4", "buy", 50, 10.0)
    old = eng.RiskApproval(ap.ticker, ap.side, ap.qty, ap.price, ap.issued_at - 10_000, ap.signature)
    with pytest.raises(RiskViolation):
        verify_approval(old, "PETR4", "buy", 5, 10.0)


def test_composite_score_weights_and_missing():
    assert composite_score(100, 100, 100, 100).composite == 100
    assert composite_score(0, 0, 0, 0).composite == 0
    s = composite_score(80, None, None, 60)                   # news/ML missing -> neutral 50
    assert set(s.missing) == {"news", "ml"}
    assert s.composite == pytest.approx(80 * .25 + 50 * .25 + 50 * .30 + 60 * .20)
    assert ml_score(0.25) == pytest.approx(50.0) and ml_score(0.9) == 100.0 and ml_score(None) is None
