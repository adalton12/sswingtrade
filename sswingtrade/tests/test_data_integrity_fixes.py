"""
Regression tests for the second audit: stale data (yfinance `end` is exclusive), history re-adjustments
(dividends/splits), backtest metrics polluted by deposits, and the news score of a single weak headline.
"""

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

import app.services.market_data_collector as mdc
from app.backtesting.engine import BacktestConfig, run_backtest
from app.backtesting.metrics import compute_metrics
from app.backtesting.strategies import build_strategy
from app.capital import service as cap
from app.execution import pipeline
from app.models import MarketCandle
from app.risk.engine import AccountState, RiskConfig, RiskEngine, TradeRequest
from app.risk.scoring import composite_score
from app.runtime.params import params
from app.services.market_clock import business_days_stale, drop_incomplete_today
from app.services.news_service import aggregate_news_score


# ============================================================================= market clock
def test_business_days_stale():
    fri = date(2026, 10, 2)
    assert business_days_stale(fri, fri) == 0
    assert business_days_stale(fri, date(2026, 10, 3)) == 0       # Saturday: Friday is still the latest session
    assert business_days_stale(fri, date(2026, 10, 4)) == 0       # Sunday
    assert business_days_stale(fri, date(2026, 10, 5)) == 1       # Monday and Friday's candle is the newest
    assert business_days_stale(date(2026, 9, 30), date(2026, 10, 12)) == 8


def test_partial_candle_of_today_is_dropped_until_the_close():
    idx = pd.to_datetime(["2026-10-05", "2026-10-06", "2026-10-07"])
    df = pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=idx)
    assert list(drop_incomplete_today(df, datetime(2026, 10, 7, 14, 0)).index.day) == [5, 6]     # mid-session bar
    assert list(drop_incomplete_today(df, datetime(2026, 10, 7, 18, 30)).index.day) == [5, 6, 7]  # after the close
    assert len(drop_incomplete_today(df.iloc[:2], datetime(2026, 10, 7, 14, 0))) == 2             # nothing from today


# ============================================================================= collector (fakes for yfinance / Postgres upsert)
class _Ctx:
    def __init__(self, s):
        self.s = s

    async def __aenter__(self):
        return self.s

    async def __aexit__(self, *a):
        return False


def _frame(start, end, close):
    idx = pd.bdate_range(start=start, end=end)
    return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": 1_000_000},
                        index=idx)


@pytest.fixture
def collector(db, monkeypatch):
    """(calls, set_source): every yfinance call is recorded; the 'provider' is whatever set_source installs."""
    calls, src = [], {}

    def fake_fetch(tickers, start, end):
        calls.append((list(tickers), start, end))
        last = (pd.Timestamp(end) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")   # `end` is exclusive, like yfinance
        return {t: src["fn"](t, start, last) for t in tickers}

    async def fake_save(session, ticker, df):                  # sqlite stand-in for the Postgres ON CONFLICT upsert
        ins = upd = 0
        for r in mdc._rows_from_df(df, "date", ticker):
            row = (await session.execute(select(MarketCandle).where(
                MarketCandle.ticker == ticker, MarketCandle.date == r["date"]))).scalar_one_or_none()
            if row:
                for k, v in r.items():
                    setattr(row, k, v)
                upd += 1
            else:
                session.add(MarketCandle(**r))
                ins += 1
        await session.commit()
        return ins, upd

    monkeypatch.setattr(mdc, "AsyncSessionLocal", lambda: _Ctx(db))
    monkeypatch.setattr(mdc, "_fetch_daily_data", fake_fetch)
    monkeypatch.setattr(mdc, "_fetch_ticker_info", lambda t: None)
    monkeypatch.setattr(mdc, "_save_daily_candles", fake_save)
    monkeypatch.setattr(mdc, "market_now", lambda: datetime(2026, 10, 7, 19, 0))     # Wednesday, after the close

    def set_source(fn):
        src["fn"] = fn
    return calls, set_source


async def _prestore(db, close=10.0, n=60, end="2026-10-06"):
    days = pd.bdate_range(end=end, periods=n)
    db.add_all([MarketCandle(ticker="AAA", date=d.to_pydatetime(), open_price=close, high_price=close * 1.01,
                             low_price=close * 0.99, close_price=close, volume=1_000_000) for d in days])
    await db.commit()
    return days


async def _closes(db):
    return [float(c) for c in (await db.execute(select(MarketCandle.close_price).where(MarketCandle.ticker == "AAA"))).scalars()]


async def test_daily_collection_asks_for_today_and_stores_it(db, collector):
    calls, set_source = collector
    set_source(lambda t, s, e: _frame("2026-10-05", e, 10.0))
    out = await mdc.collect_daily_candles(["AAA"], days_back=5)
    # yfinance `end` is exclusive: it must be TOMORROW, otherwise today's session (the one just closed) never arrives
    assert calls == [(["AAA"], "2026-10-02", "2026-10-08")]
    dates = (await db.execute(select(MarketCandle.date).where(MarketCandle.ticker == "AAA"))).scalars().all()
    assert datetime(2026, 10, 7) in dates and out["tickers_succeeded"] == 1 and out["resynced"] == []


async def test_readjusted_history_triggers_a_full_resync(db, collector):
    calls, set_source = collector
    days = await _prestore(db, close=10.0)                       # stored before a 2% dividend
    set_source(lambda t, s, e: _frame(s, e, 9.8))                # provider now serves the whole history re-adjusted
    out = await mdc.collect_daily_candles(["AAA"], days_back=5)
    assert out["resynced"] == ["AAA"]
    assert len(calls) == 2 and calls[1][1] == days[0].strftime("%Y-%m-%d")      # 2nd call: from the OLDEST stored day
    closes = await _closes(db)
    assert set(closes) == {9.8}, "old rows were left on the pre-dividend scale (patchwork history)"


async def test_consistent_data_needs_no_resync(db, collector):
    calls, set_source = collector
    await _prestore(db, close=10.0)
    set_source(lambda t, s, e: _frame(s, e, 10.0))
    out = await mdc.collect_daily_candles(["AAA"], days_back=5)
    assert len(calls) == 1 and out["resynced"] == []


async def test_cent_rounding_noise_is_not_mistaken_for_an_adjustment(db, collector):
    calls, set_source = collector
    await _prestore(db, close=3.00)
    set_source(lambda t, s, e: _frame(s, e, 3.01))               # 1 cent apart on a R$3 stock = 0.33%
    out = await mdc.collect_daily_candles(["AAA"], days_back=5)
    assert len(calls) == 1 and out["resynced"] == []


async def test_force_full_redownloads_the_whole_stored_history(db, collector):
    calls, set_source = collector
    days = await _prestore(db, close=10.0)
    set_source(lambda t, s, e: _frame(s, e, 10.0))
    out = await mdc.collect_daily_candles(["AAA"], days_back=5, force_full=True)
    assert out["resynced"] == ["AAA"] and calls[1][1] == days[0].strftime("%Y-%m-%d")


# ============================================================================= freshness rule
def _state():
    return AccountState(equity=2000, cash=2000, exposure=0, open_positions=0, open_tickers=set(), daily_spent=0,
                        entries_today=0, circuit_breaker=False)


@pytest.mark.parametrize("cfg_max,age,ok", [(3, 0, True), (3, 3, True), (3, 4, False), (3, None, True), (0, 30, True)])
def test_risk_engine_refuses_stale_data(cfg_max, age, ok):
    eng = RiskEngine(RiskConfig(min_score=60, paper_enabled=True, max_data_age_bdays=cfg_max))
    d = eng.evaluate(TradeRequest("AAA", "buy", 10.0, 0.2, 75.0, data_age_days=age), _state())
    assert d.approved is ok
    if not ok:
        assert any(r.startswith("fresh_data") for r in d.reasons)


async def _flat_history(db):
    days = pd.bdate_range(end="2026-09-30", periods=100)
    db.add_all([MarketCandle(ticker="AAA", date=d.to_pydatetime(), open_price=10, high_price=10.1, low_price=9.9,
                             close_price=10, volume=1_000_000) for d in days])
    await db.commit()


async def test_pipeline_refuses_entries_when_the_collection_stopped(db, monkeypatch):
    await params.update(db, {"risk.max_data_age_bdays": 3})
    limit = RiskConfig.from_params().max_data_age_bdays
    assert limit == 3                                            # the runtime parameter reaches the engine
    # score gate off (flat synthetic series), so only the freshness rule decides here
    monkeypatch.setattr(pipeline, "RiskEngine", lambda: RiskEngine(RiskConfig(min_score=0, max_data_age_bdays=limit)))
    acc = await cap.get_or_create_account(db)
    await _flat_history(db)                                      # last candle: Wed 2026-09-30

    monkeypatch.setattr(pipeline, "_today", lambda: date(2026, 10, 12))          # 8 business days later
    stale = await pipeline.run_decisions(db, acc, ["AAA"], dry_run=True)
    assert stale["data_age_bdays"] == 8 and stale["approved"] == 0
    assert any(r.startswith("fresh_data") for r in stale["decisions"][0]["reasons"])

    monkeypatch.setattr(pipeline, "_today", lambda: date(2026, 10, 1))           # next morning: 1 business day
    fresh = await pipeline.run_decisions(db, acc, ["AAA"], dry_run=True)
    assert fresh["data_age_bdays"] == 1 and fresh["approved"] == 1


async def test_data_age_parameter_is_validated(db):
    with pytest.raises(ValueError):
        await params.update(db, {"risk.max_data_age_bdays": 31})
    with pytest.raises(ValueError):
        await params.update(db, {"risk.max_data_age_bdays": -1})
    assert (await params.update(db, {"risk.max_data_age_bdays": 0}))["risk.max_data_age_bdays"] == 0


# ============================================================================= backtest metrics vs deposits
def _equity_with_deposit(returns, deposit_day, deposit, start=1000.0):
    eq, flows, e = [start], [0.0], start
    for t, r in enumerate(returns, start=1):
        f = deposit if t == deposit_day else 0.0
        e = (e + f) * (1 + r)
        eq.append(e)
        flows.append(f)
    idx = pd.bdate_range("2026-01-05", periods=len(eq))
    return pd.Series(eq, index=idx), pd.Series(flows, index=idx)


def test_deposits_neither_hide_drawdown_nor_create_sharpe():
    rets = np.array([-0.01, -0.02, 0.005] * 7)[:20]                       # a steadily losing strategy
    equity, flows = _equity_with_deposit(rets, deposit_day=10, deposit=500.0)
    m = compute_metrics(equity, [], 1000.0, 500.0, flows)
    idx = np.cumprod(np.r_[1.0, 1 + rets])
    expected_dd = (idx / np.maximum.accumulate(idx) - 1).min() * 100
    assert m["max_drawdown_pct"] == pytest.approx(expected_dd, abs=0.05)
    assert m["twr_return_pct"] == pytest.approx((idx[-1] - 1) * 100, abs=0.05)
    assert m["sharpe"] < 0
    # the old calculation (no flows) treats the deposit as a +50% day: positive average return, tiny drawdown
    naive = compute_metrics(equity, [], 1000.0, 500.0)
    assert naive["max_drawdown_pct"] > m["max_drawdown_pct"] + 5 or naive["sharpe"] > m["sharpe"]


def test_flows_default_to_plain_returns():
    equity = pd.Series([100.0, 101.0, 99.0, 103.0], index=pd.bdate_range("2026-01-05", periods=4))
    a = compute_metrics(equity, [], 100.0)
    b = compute_metrics(equity, [], 100.0, 0.0, pd.Series(0.0, index=equity.index))
    assert a == b


def _random_walk(seed, n=500):
    r = np.random.default_rng(seed)
    idx = pd.bdate_range("2023-01-02", periods=n)
    close = 20 * np.exp(np.cumsum(r.normal(0.0, 0.015, n)))
    open_ = np.r_[close[0], close[:-1]] * (1 + r.normal(0, 0.003, n))
    high = np.maximum(open_, close) * (1 + np.abs(r.normal(0, 0.005, n)))
    low = np.minimum(open_, close) * (1 - np.abs(r.normal(0, 0.005, n)))
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                         "volume": r.integers(800_000, 2_000_000, n)}, index=idx)


def test_backtest_of_a_losing_strategy_stays_negative_with_monthly_deposits():
    data = {f"T{i}": _random_walk(i) for i in range(8)}
    plain = run_backtest(data, build_strategy("score"), BacktestConfig(monthly_deposit=0.0))["metrics"]
    funded = run_backtest(data, build_strategy("score"), BacktestConfig(monthly_deposit=500.0))["metrics"]
    assert plain["sharpe"] < 0
    assert funded["net_profit"] < 0 and funded["sharpe"] < 0           # was +2.0 for a strategy that lost money
    assert abs(funded["max_drawdown_pct"] - plain["max_drawdown_pct"]) < 8   # was -6% vs -31%


# ============================================================================= news score
NOW = datetime(2026, 10, 5, 12)


def _item(s, impact, conf, age_days=0.0):
    return {"sentiment_score": s, "impact_score": impact, "confidence": conf, "event_date": NOW - timedelta(days=age_days)}


def test_one_weak_headline_stays_near_neutral():
    for s in (1.0, -1.0):
        score = aggregate_news_score([_item(s, impact=1, conf=0.1)], NOW)["news_score"]
        assert abs(score - 50) < 5, "a single low-impact, low-confidence headline must not swing the score"
    stale = aggregate_news_score([_item(-1.0, impact=0.5, conf=0.2, age_days=4)], NOW)["news_score"]
    assert abs(stale - 50) < 2


def test_strong_evidence_still_moves_the_score_and_more_evidence_moves_it_more():
    one = aggregate_news_score([_item(0.8, 8, 0.9)], NOW)["news_score"]
    two = aggregate_news_score([_item(0.8, 8, 0.9), _item(0.8, 8, 0.9, age_days=0.5)], NOW)["news_score"]
    assert one > 70 and two > one
    assert aggregate_news_score([_item(-0.8, 8, 0.9)], NOW)["news_score"] < 30


def test_weak_headline_can_no_longer_push_a_mediocre_stock_over_the_trade_gate():
    news = aggregate_news_score([_item(1.0, impact=1, conf=0.1)], NOW)["news_score"]
    assert composite_score(55, news, 55, 55).composite < 60          # was 66.25 (news score 100)
