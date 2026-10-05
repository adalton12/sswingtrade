"""HTTP-level tests (FastAPI + SQLite session) for FASE 7/8/9 routes."""

import httpx
import pandas as pd
import pytest

from app.database import get_db
from app.execution import pipeline
from app.main import app
from app.models import MarketCandle
from app.risk.engine import RiskConfig, RiskEngine


@pytest.fixture
async def client(db, monkeypatch):
    async def override():
        yield db
    app.dependency_overrides[get_db] = override
    monkeypatch.setattr(pipeline, "RiskEngine", lambda: RiskEngine(RiskConfig(min_score=0)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c
    app.dependency_overrides.clear()


async def seed(db, n=100, ticker="AAA"):
    days = pd.bdate_range(end="2026-09-30", periods=n)
    db.add_all([MarketCandle(ticker=ticker, date=d.to_pydatetime(), open_price=10, high_price=10.1, low_price=9.9,
                             close_price=10, volume=1_000_000) for d in days])
    await db.commit()
    return days


async def test_dashboard_page_and_empty_summary(client):
    r = await client.get("/dashboard")
    assert r.status_code == 200 and "SSWingTrade" in r.text and "/api/v1/dashboard/summary" in r.text
    s = (await client.get("/api/v1/dashboard/summary")).json()
    assert s["mode"] == "PAPER" and s["equity"] == 500.0 and s["trades_closed"] == 0
    assert s["circuit_breaker"]["tripped"] is False and len(s["equity_curve"]) == 1


async def test_capital_endpoints(client):
    s = (await client.get("/api/v1/capital/summary")).json()
    assert s["equity"] == 500.0 and s["per_operation_budget_today"] == 100.0
    sz = (await client.post("/api/v1/capital/sizing", json={"price": 10, "stop_price": 9.7})).json()
    assert sz["qty"] == 9 and sz["binding"] == "daily_budget"
    assert (await client.post("/api/v1/capital/deposit", json={"amount": 500})).json()["equity"] == 1000.0
    s2 = (await client.get("/api/v1/capital/summary")).json()
    assert s2["per_operation_budget_today"] == 200.0 and s2["total_deposited"] == 1000.0   # budget doubles with equity
    p = (await client.get("/api/v1/capital/projection?weeks=8&weekly_return_pct=1")).json()
    assert len(p["projection"]) == 8 and "disclaimer" in p
    chk = (await client.post("/api/v1/capital/allocation/check",
                             json={"account_id": 1, "operation_amount": 150, "operation_type": "buy"})).json()
    assert chk["is_approved"] is True                       # budget 200 now
    chk = (await client.post("/api/v1/capital/allocation/check",
                             json={"account_id": 1, "operation_amount": 250, "operation_type": "buy"})).json()
    assert chk["is_approved"] is False and "Daily budget" in chk["reason"]


async def test_trading_flow_over_http(client, db):
    await seed(db)
    prev = (await client.post("/api/v1/trading/preview", json={"tickers": ["AAA"]})).json()
    assert prev["dry_run"] and prev["approved"] == 1
    assert (await client.get("/api/v1/trading/orders")).json()["orders"] == []     # preview creates nothing
    assert (await client.get("/api/v1/trading/decisions")).json()["decisions"] == []

    cyc = (await client.post("/api/v1/trading/cycle", json={"tickers": ["AAA"]})).json()
    assert cyc["decisions"]["approved"] == 1
    orders = (await client.get("/api/v1/trading/orders")).json()["orders"]
    assert len(orders) == 1 and orders[0]["status"] == "pending"
    dec = (await client.get("/api/v1/trading/decisions?approved=true")).json()["decisions"]
    assert dec and all(c["passed"] for c in dec[0]["checks"])

    # advocate veto supplied by the batch blocks the next day's entry
    days = await seed_next(db)
    veto = (await client.post("/api/v1/trading/preview", json={"tickers": ["AAA"], "advocate_scores": {"AAA": 95}})).json()
    assert veto["approved"] == 0 and any("devils_advocate" in r for r in veto["decisions"][0]["reasons"])
    assert (await client.get("/api/v1/trading/status")).json()["real_enabled"] is False
    assert (await client.post("/api/v1/trading/circuit-breaker/reset")).status_code == 200


async def seed_next(db):
    d = pd.bdate_range(start="2026-10-01", periods=1)
    db.add(MarketCandle(ticker="AAA", date=d[0].to_pydatetime(), open_price=10, high_price=10.1, low_price=9.9,
                        close_price=10, volume=1_000_000))
    await db.commit()
    return d
