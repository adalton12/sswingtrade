"""News + LLM agents routes - FASE 6. Agents score and explain; they never trade."""

from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel, Field

from app.agents.devil_advocate.advocate import DevilsAdvocate
from app.agents.events.analyst import EventAnalyst
from app.agents.research.analyst import ResearchAnalyst
from app.config import settings
from app.llm.client import LLMError, OllamaClient
from app.services.agent_service import build_context
from app.services.market_data_collector import DEFAULT_TICKERS
from app.services.news_service import (analyze_pending, fetch_rss_news, get_news, ingest_news, news_score,
                                       run_news_batch)

router = APIRouter()


class NewsItem(BaseModel):
    title: str = Field(..., min_length=3, max_length=500)
    content: Optional[str] = None
    source: Optional[str] = "manual"
    event_date: Optional[datetime] = None


class IngestRequest(BaseModel):
    ticker: str
    items: List[NewsItem]
    analyze_now: bool = True


class EventRequest(BaseModel):
    ticker: str
    text: str = Field(..., min_length=10)


class AdvocateRequest(BaseModel):
    direction: str = Field("buy", pattern="^(buy|sell)$")
    thesis: str = Field(..., min_length=5)


def _need_llm():
    if not settings.ENABLE_LLM_ANALYSIS:
        raise HTTPException(503, "LLM analysis disabled (ENABLE_LLM_ANALYSIS=false)")


async def _guard(coro):
    try:
        return await coro
    except LLMError as e:
        raise HTTPException(502, f"LLM unavailable or invalid output: {e}")


@router.get("/llm/status")
async def llm_status() -> dict:
    """Is Ollama up and is the model downloaded? (docker exec sswingtrade-ollama ollama pull qwen2.5:3b)"""
    return await OllamaClient().available()


@router.post("/ingest")
async def ingest(req: IngestRequest, background: BackgroundTasks) -> dict:
    res = await ingest_news(req.ticker, [i.model_dump() for i in req.items])
    if req.analyze_now and res["inserted"] and settings.ENABLE_LLM_ANALYSIS:
        background.add_task(analyze_pending)     # batch in background: response returns immediately
        res["analysis"] = "scheduled"
    return res


@router.post("/fetch/{ticker}")
async def fetch(ticker: str, background: BackgroundTasks) -> dict:
    try:
        items = await fetch_rss_news(ticker)
    except Exception as e:
        raise HTTPException(502, f"RSS fetch failed: {e}")
    res = await ingest_news(ticker, items)
    if res["inserted"] and settings.ENABLE_LLM_ANALYSIS:
        background.add_task(analyze_pending)
        res["analysis"] = "scheduled"
    return res


@router.post("/analyze")
async def analyze(limit: int = Query(20, ge=1, le=100)) -> dict:
    _need_llm()
    return await _guard(analyze_pending(limit))


@router.post("/batch")
async def batch(background: BackgroundTasks) -> dict:
    """Run the whole nightly batch (RSS -> dedupe -> LLM) in background for all default tickers."""
    _need_llm()
    background.add_task(run_news_batch, DEFAULT_TICKERS)
    return {"status": "started", "tickers": len(DEFAULT_TICKERS)}


@router.get("/{ticker}")
async def list_news(ticker: str, days: int = Query(7, ge=1, le=60)) -> dict:
    rows = await get_news(ticker, days)
    return {"ticker": ticker.upper(), "count": len(rows), "news": [
        {"id": n.id, "date": n.event_date.isoformat(), "title": n.title, "source": n.source,
         "sentiment": n.sentiment_score, "impact": n.impact_score, "confidence": n.confidence,
         "horizon": n.horizon, "event_type": n.event_type, "analysis": n.analysis_metadata} for n in rows]}


@router.get("/{ticker}/score")
async def score(ticker: str, days: int = Query(5, ge=1, le=30)) -> dict:
    """Aggregated news score 0-100 (50 = neutral / no data)."""
    return await news_score(ticker, days)


@router.post("/agents/event")
async def event_agent(req: EventRequest) -> dict:
    _need_llm()
    r = await _guard(EventAnalyst().run(ticker=req.ticker.upper(), text=req.text, today=date.today().isoformat()))
    return r.model_dump()


@router.post("/agents/research/{ticker}")
async def research_agent(ticker: str) -> dict:
    _need_llm()
    ctx = await build_context(ticker)
    r = await _guard(ResearchAnalyst().run(ticker=ticker.upper(), context=ctx))
    return {"ticker": ticker.upper(), "report": r.model_dump(), "inputs": ctx}


@router.post("/agents/devils-advocate/{ticker}")
async def advocate_agent(ticker: str, req: AdvocateRequest) -> dict:
    _need_llm()
    ctx = await build_context(ticker)
    r = await _guard(DevilsAdvocate().run(ticker=ticker.upper(), direction=req.direction, thesis=req.thesis, context=ctx))
    return {"ticker": ticker.upper(), "verdict": r.model_dump(),
            "note": "Advisory only: the Risk Engine (FASE 8) decides; the LLM never sends orders."}
