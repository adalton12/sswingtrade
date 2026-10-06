"""
News ingestion + batch LLM analysis - FASE 6.
The LLM runs in BATCH (market close or when news is registered), never per tick.
Duplicates are prevented with a sha256 content hash (stored in news_events.content_hash).
"""

import hashlib
import html
import math
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Dict, List, Optional
from urllib.parse import quote

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.agents.news.analyst import NewsAnalyst
from app.config import settings
from app.runtime.params import params
from app.database import AsyncSessionLocal, session_scope
from app.llm.client import LLMError
from app.models import NewsEvent
from app.services.logger import logger

MAX_ATTEMPTS = 3


def content_hash(ticker: str, title: str, content: str = "") -> str:
    norm = re.sub(r"\s+", " ", f"{ticker.upper()}|{title}|{content or ''}".lower()).strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- RSS (free source)
def parse_google_news_rss(xml_text: str) -> List[dict]:
    items = []
    root = ET.fromstring(xml_text)
    for it in root.iterfind(".//item"):
        title = html.unescape(it.findtext("title") or "").strip()
        desc = re.sub(r"<[^>]+>", " ", html.unescape(it.findtext("description") or ""))
        pub = it.findtext("pubDate")
        try:
            when = parsedate_to_datetime(pub).replace(tzinfo=None) if pub else datetime.utcnow()
        except (TypeError, ValueError):
            when = datetime.utcnow()
        source = (it.findtext("source") or "").strip() or "google_news"
        if title:
            items.append({"title": title, "content": re.sub(r"\s+", " ", desc).strip(), "source": source, "event_date": when})
    return items


async def fetch_rss_news(ticker: str, days: Optional[int] = None) -> List[dict]:
    days = days or params.get("news.lookback_days")
    q = quote(f"{ticker.upper()} acoes when:{days}d")
    url = f"https://news.google.com/rss/search?q={q}&hl=pt-BR&gl=BR&ceid=BR:pt-419"
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
        r = await c.get(url, headers={"User-Agent": "Mozilla/5.0 SSWingTrade"})
        r.raise_for_status()
    return parse_google_news_rss(r.text)[:15]


# ---------------------------------------------------------------- ingestion
async def ingest_news(ticker: str, items: List[dict]) -> dict:
    ticker = ticker.upper()
    rows = [{
        "ticker": ticker, "title": it["title"][:500], "content": it.get("content"), "source": (it.get("source") or "manual")[:100],
        "event_date": it.get("event_date") or datetime.utcnow(),
        "content_hash": content_hash(ticker, it["title"], it.get("content", "")),
    } for it in items if it.get("title")]
    if not rows:
        return {"received": 0, "inserted": 0, "duplicates": 0}
    async with AsyncSessionLocal() as s:
        stmt = pg_insert(NewsEvent).values(rows).on_conflict_do_nothing(index_elements=["content_hash"]).returning(NewsEvent.id)
        inserted = len((await s.execute(stmt)).fetchall())
        await s.commit()
    return {"received": len(rows), "inserted": inserted, "duplicates": len(rows) - inserted}


# ---------------------------------------------------------------- batch analysis
async def analyze_pending(limit: Optional[int] = None, analyst: Optional[NewsAnalyst] = None) -> dict:
    limit = limit or params.get("news.batch_limit")
    analyst = analyst or NewsAnalyst()
    done, failed = 0, 0
    async with AsyncSessionLocal() as s:
        pending = (await s.execute(
            select(NewsEvent).where(NewsEvent.sentiment_score.is_(None)).order_by(NewsEvent.event_date.desc()).limit(limit * 3)
        )).scalars().all()
        pending = [n for n in pending if (n.analysis_metadata or {}).get("attempts", 0) < MAX_ATTEMPTS][:limit]
        for n in pending:
            try:
                a = await analyst.run(ticker=n.ticker, title=n.title, content=n.content or "")
                n.sentiment_score, n.impact_score, n.confidence = a.sentiment, a.impact_score, a.confidence
                n.horizon, n.event_type = a.horizon, a.event_type
                n.analysis_metadata = {**a.model_dump(), "model": analyst.client.model, "analyzed_at": datetime.utcnow().isoformat()}
                done += 1
            except LLMError as e:
                meta = dict(n.analysis_metadata or {})
                meta.update(attempts=meta.get("attempts", 0) + 1, error=str(e)[:300])
                n.analysis_metadata = meta
                failed += 1
                logger.warning(f"News analysis failed (id={n.id}): {e}")
                if "request failed" in str(e):   # Ollama down: stop the batch early
                    break
            await s.commit()
    return {"analyzed": done, "failed": failed, "pending_considered": len(pending)}


# ---------------------------------------------------------------- aggregation (0-100 news score)
def aggregate_news_score(rows: List[dict], now: Optional[datetime] = None, half_life_days: float = 2.0) -> dict:
    """
    rows: dicts with sentiment_score, impact_score, confidence, event_date.
    Weight = confidence * impact/10 * 2^(-age/half_life). Score = 50 + 50 * weighted mean sentiment.
    No usable news -> neutral 50 with n=0.
    """
    now = now or datetime.utcnow()
    num = den = 0.0
    n = 0
    for r in rows:
        if r.get("sentiment_score") is None:
            continue
        age = max(0.0, (now - r["event_date"]).total_seconds() / 86400)
        w = (r.get("confidence") or 0.5) * ((r.get("impact_score") or 0) / 10) * math.pow(2, -age / half_life_days)
        num += w * r["sentiment_score"]
        den += w
        n += 1
    if n == 0 or den < 1e-9:
        return {"news_score": 50.0, "n_news": n, "weight": round(den, 4)}
    return {"news_score": round(50 + 50 * (num / den), 1), "n_news": n, "weight": round(den, 4)}


async def get_news(ticker: str, days: int = 7, limit: int = 50, session=None) -> List[NewsEvent]:
    cutoff = datetime.utcnow() - timedelta(days=days)
    async with session_scope(session) as s:
        return list((await s.execute(
            select(NewsEvent).where(NewsEvent.ticker == ticker.upper(), NewsEvent.event_date >= cutoff)
            .order_by(NewsEvent.event_date.desc()).limit(limit)
        )).scalars().all())


async def news_score(ticker: str, days: int = 5, session=None) -> dict:
    rows = await get_news(ticker, days, session=session)
    out = aggregate_news_score([{"sentiment_score": r.sentiment_score, "impact_score": r.impact_score,
                                 "confidence": r.confidence, "event_date": r.event_date} for r in rows])
    out.update(ticker=ticker.upper(), days=days)
    return out


async def run_news_batch(tickers: List[str]) -> dict:
    """Nightly job: fetch RSS (optional) -> dedupe/insert -> LLM analysis in batch."""
    fetched = {"inserted": 0, "duplicates": 0, "errors": 0}
    if params.get("news.auto_fetch"):
        for t in tickers:
            try:
                r = await ingest_news(t, await fetch_rss_news(t))
                fetched["inserted"] += r["inserted"]
                fetched["duplicates"] += r["duplicates"]
            except Exception as e:
                fetched["errors"] += 1
                logger.warning(f"RSS fetch failed for {t}: {e}")
    return {"fetch": fetched, "analysis": await analyze_pending()}
