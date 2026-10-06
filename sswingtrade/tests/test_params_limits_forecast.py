"""Runtime parameters, daily/weekly/monthly loss limits, variable deposits, forecast, settings API."""

from datetime import date, datetime, timedelta

import httpx
import numpy as np
import pytest

from app.capital import service as cap
from app.capital.rules import CapitalRules, size_position
from app.database import get_db
from app.forecast.provision import EdgeEvidence, SimConfig, estimate_edge, expected_trade_return, simulate
from app.main import app
from app.risk.engine import AccountState, RiskEngine, TradeRequest
from app.runtime.params import PROFILES, cross_validate, params


# ----------------------------------------------------------------------------- params
async def test_params_persist_validate_and_profile(db):
    assert params.get("capital.per_op_pct") == 20
    await params.update(db, {"capital.weekly_deposit": 137.5, "limits.daily_loss_pct": 2.0})
    params.reset_cache()
    assert params.get("capital.weekly_deposit") == 500.0            # cache cleared -> default until refreshed from DB
    await params.refresh(db)
    assert params.get("capital.weekly_deposit") == 137.5 and params.get("risk.profile") == "custom"

    with pytest.raises(ValueError):
        await params.update(db, {"capital.per_op_pct": 500})         # out of range
    with pytest.raises(ValueError):
        await params.update(db, {"nope.key": 1})
    with pytest.raises(ValueError):                                  # day limit > month limit
        await params.update(db, {"limits.daily_loss_pct": 10, "limits.monthly_loss_pct": 5})
    assert params.get("limits.daily_loss_pct") == 2.0                # atomic: nothing was half-applied

    await params.apply_profile(db, "aggressive")
    assert params.get("risk.profile") == "aggressive" and params.get("capital.per_op_pct") == 35
    await params.update(db, {"capital.per_op_pct": 40})
    assert params.get("risk.profile") == "custom"                    # manual edit of a profile knob
    await params.reset(db)
    assert params.get("capital.per_op_pct") == 20


def test_all_profiles_are_valid_and_ordered():
    base = params.all_values()
    for name, p in PROFILES.items():
        assert cross_validate({**base, **p["values"]}) == [], name
    order = ["conservative", "moderate", "aggressive", "very_aggressive"]
    pct = [PROFILES[n]["values"]["capital.per_op_pct"] for n in order]
    assert pct == sorted(pct)


def test_per_op_pct_drives_sizing_and_risk_engine():
    params._v["capital.per_op_pct"] = 40.0
    r = size_position(1000, 1000, 0, 0, 10.0, None)
    assert r.allocation <= 400 + 1e-6 and CapitalRules.from_params().per_op_pct == 0.4
    eng = RiskEngine()
    st = AccountState(1000, 1000, 0, 0, set(), 0, 0, False)
    d = eng.evaluate(TradeRequest("AAA", "buy", 10.0, 0.3, 80), st)
    assert d.approved and d.allocation <= 400 + 1e-6


# ----------------------------------------------------------------------------- loss limits
async def _loss(db, acc, amount, when):
    eq = cap.equity(acc)
    acc.available_balance = float(acc.available_balance) - amount
    cap._sync_balance(acc)
    await cap._log(db, acc, "trade_pnl", -amount, eq, cap.equity(acc), "test loss", when=when)
    await db.commit()


async def test_daily_limit_latches_and_blocks(db):
    acc = await cap.get_or_create_account(db)                         # equity 500, daily limit 1.5% = R$7.50
    now = datetime.utcnow()
    await _loss(db, acc, 5.0, now)
    cb = await cap.refresh_circuit_breaker(db, acc)
    assert not cb["tripped"] and cb["remaining_allowance"] == pytest.approx(2.5, abs=0.01)
    await _loss(db, acc, 3.0, now)
    cb = await cap.refresh_circuit_breaker(db, acc)
    assert cb["tripped"] and cb["tripped_periods"] == ["day"]
    # latched even if a gain brings the day back above the limit
    eq = cap.equity(acc)
    acc.available_balance = float(acc.available_balance) + 20
    cap._sync_balance(acc)
    await cap._log(db, acc, "trade_pnl", 20, eq, cap.equity(acc), "gain", when=now)
    await db.commit()
    assert (await cap.refresh_circuit_breaker(db, acc))["tripped"]
    await cap.reset_circuit_breaker(db, acc, "day")
    cb = await cap.refresh_circuit_breaker(db, acc)
    assert "day" not in cb["tripped_periods"]


async def test_weekly_and_monthly_limits_survive_daily_reset(db):
    await params.update(db, {"limits.daily_loss_pct": 50, "limits.weekly_loss_pct": 60, "limits.monthly_loss_pct": 70,
                             "limits.weekly_loss_amount": 20.0})   # fixed R$20 per week (< 60% of 500)
    acc = await cap.get_or_create_account(db)
    today = date.today()
    monday = datetime.combine(today - timedelta(days=today.weekday()), datetime.min.time()) + timedelta(hours=10)
    await _loss(db, acc, 25.0, monday)
    cb = await cap.refresh_circuit_breaker(db, acc)
    assert "week" in cb["tripped_periods"] and cb["periods"]["week"]["limit"] == 20.0
    await cap.reset_circuit_breaker(db, acc, "day")                  # morning job only clears the day latch
    cb = await cap.refresh_circuit_breaker(db, acc)
    assert "week" in cb["tripped_periods"] and cb["tripped"]


async def test_limit_off_when_zero_and_allowance_caps_sizing(db):
    await params.update(db, {"limits.daily_loss_pct": 0, "limits.weekly_loss_pct": 0, "limits.monthly_loss_pct": 0})
    acc = await cap.get_or_create_account(db)
    cb = await cap.refresh_circuit_breaker(db, acc)
    assert cb["remaining_allowance"] is None and not cb["tripped"]
    r = size_position(1000, 1000, 0, 0, 10.0, 9.5, 1, CapitalRules(), max_loss_amount=2.0)   # 0.5 per share
    assert r.qty == 4 and r.binding == "loss_limit"


# ----------------------------------------------------------------------------- variable deposits
async def test_planned_deposits_any_amount_applied_once(db):
    acc = await cap.get_or_create_account(db)
    today = date.today()
    await cap.add_planned_deposit(db, acc, today, 237.9, "bonus")
    await cap.add_planned_deposit(db, acc, today + timedelta(days=30), 1000, "future")
    out = await cap.apply_planned_deposits(db, acc, today)
    assert [o["amount"] for o in out] == [237.9] and cap.equity(acc) == pytest.approx(737.9)
    assert await cap.apply_planned_deposits(db, acc, today) == []     # idempotent
    out2 = await cap.apply_planned_deposits(db, acc, today + timedelta(days=31))
    assert out2[0]["amount"] == 1000 and cap.equity(acc) == pytest.approx(1737.9)


async def test_weekly_deposit_follows_parameter(db):
    await params.update(db, {"capital.weekly_deposit": 321.0})
    acc = await cap.get_or_create_account(db)
    nxt = date.today() + timedelta(days=14)
    assert await cap.start_week(db, acc, nxt) is not None
    assert cap.equity(acc) == pytest.approx(500 + 321.0)


# ----------------------------------------------------------------------------- forecast
def _cfg(**k):
    return SimConfig(start_equity=500, weeks=26, weekly_deposit=0, simulations=1500, seed=7, **k)


def test_no_edge_means_negative_expected_value():
    e = estimate_edge(EdgeEvidence(), 2.0)
    assert e.p_win == pytest.approx(1 / 3, abs=1e-3)
    assert expected_trade_return(e.p_win, _cfg()) < 0
    assert simulate(_cfg(), e.p_win)["expected_profit"] < 0


def test_ml_edge_raises_winrate_but_bad_auc_does_not():
    good = estimate_edge(EdgeEvidence(ml_auc=0.62, ml_base_rate=0.2, ml_top_prob=0.3), 2.0)
    bad = estimate_edge(EdgeEvidence(ml_auc=0.50, ml_base_rate=0.2, ml_top_prob=0.3), 2.0)
    assert good.p_win > bad.p_win and bad.p_win == pytest.approx(1 / 3, abs=1e-3)


def test_paper_track_record_shifts_estimate():
    base = EdgeEvidence(ml_auc=0.6, ml_base_rate=0.2, ml_top_prob=0.3)
    worse = EdgeEvidence(**{**base.__dict__, "paper_trades": 60, "paper_wins": 6})
    assert estimate_edge(worse, 2.0).p_win < estimate_edge(base, 2.0).p_win


def test_aggressive_has_wider_spread_and_limits_reduce_tail_loss():
    p = 0.42
    cons = simulate(_cfg(per_op_pct=10, risk_per_trade_pct=1, trades_per_week=3), p)
    aggr = simulate(_cfg(per_op_pct=60, risk_per_trade_pct=5, trades_per_week=10), p)
    spread = lambda r: r["final"]["p90"] - r["final"]["p10"]
    assert spread(aggr) > spread(cons)
    assert aggr["max_drawdown_p90"] > cons["max_drawdown_p90"]
    free = simulate(_cfg(per_op_pct=60, risk_per_trade_pct=5, trades_per_week=10), 0.30)
    capped = simulate(_cfg(per_op_pct=60, risk_per_trade_pct=5, trades_per_week=10, daily_loss_pct=3,
                           weekly_loss_pct=6, monthly_loss_pct=10), 0.30)
    assert capped["final"]["p10"] > free["final"]["p10"]
    assert capped["limit_hit_paths_pct"]["month"] > 0
    tight = simulate(_cfg(per_op_pct=60, risk_per_trade_pct=5, trades_per_week=10, daily_loss_pct=0.4), 0.30)
    assert tight["limit_hit_paths_pct"]["day"] > 0


def test_fan_ordered_and_reproducible():
    a, b = simulate(_cfg(), 0.45), simulate(_cfg(), 0.45)
    assert a == b
    for i in range(27):
        assert a["fan"]["p10"][i] <= a["fan"]["p50"][i] <= a["fan"]["p90"][i]


def test_deposits_enter_total_deposited():
    r = simulate(SimConfig(start_equity=500, weeks=8, weekly_deposit=100, planned_deposits=[(3, 400)], simulations=200, seed=1), 0.4)
    assert r["total_deposited"] == pytest.approx(500 + 800 + 400)


# ----------------------------------------------------------------------------- HTTP
@pytest.fixture
async def client(db):
    async def override():
        yield db
    app.dependency_overrides[get_db] = override
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c
    app.dependency_overrides.clear()


async def test_settings_api_roundtrip_and_pages(client):
    s = (await client.get("/api/v1/settings")).json()
    assert "Capital" in s["groups"] and s["profile"] == "moderate" and s["real_trading_enabled"] is False
    r = await client.put("/api/v1/settings", json={"values": {"capital.weekly_deposit": 250, "limits.weekly_loss_pct": 5}})
    assert r.status_code == 200 and r.json()["profile"] == "custom"
    assert (await client.put("/api/v1/settings", json={"values": {"capital.per_op_pct": 0}})).status_code == 422
    assert (await client.put("/api/v1/settings", json={"values": {"enable_real_trading": True}})).status_code == 422
    assert (await client.post("/api/v1/settings/profile", json={"profile": "very_aggressive"})).status_code == 200
    assert (await client.get("/api/v1/trading/status")).json()["loss_limits"]["daily_pct"] == 5.0
    assert (await client.post("/api/v1/settings/profile", json={"profile": "x"})).status_code == 422
    assert (await client.post("/api/v1/settings/reset", json={})).status_code == 200
    for page in ("/settings", "/forecast", "/dashboard"):
        assert (await client.get(page)).status_code == 200


async def test_planned_deposit_and_limits_and_forecast_api(client):
    r = await client.post("/api/v1/capital/planned-deposits", json={"due_date": date.today().isoformat(), "amount": 77.7})
    assert r.status_code == 200 and r.json()["applied_now"]
    s = (await client.get("/api/v1/capital/summary")).json()
    assert s["equity"] == pytest.approx(577.7) and s["circuit_breaker"]["periods"]["month"]["limit"] is not None
    ll = (await client.get("/api/v1/capital/loss-limits")).json()
    assert set(ll["periods"]) == {"day", "week", "month"}
    f = (await client.post("/api/v1/forecast", json={"profile": "aggressive", "weeks": 12})).json()
    assert f["weeks"] == 12 and "disclaimer" in f and set(f["scenarios"]) == {"pessimista", "base", "otimista"}
    assert f["edge"]["p_win"] > 0 and f["profile"] == "aggressive"
    c = (await client.get("/api/v1/forecast/compare?weeks=12")).json()
    assert set(c["profiles"]) == set(PROFILES)
    assert (await client.post("/api/v1/forecast", json={"profile": "zzz"})).status_code == 422
