"""Builds the shared context (indicators + news) consumed by Research / Devil's Advocate agents."""

from app.services.indicator_service import get_latest_indicators
from app.services.news_service import get_news, news_score


async def build_context(ticker: str) -> dict:
    ind = await get_latest_indicators(ticker, 2)
    ctx: dict = {"ticker": ticker.upper()}
    if ind:
        i = ind[0]
        ctx["technical"] = {k: (round(getattr(i, k), 4) if getattr(i, k) is not None else None) for k in
                            ("close", "rsi_14", "sma_20", "sma_50", "ema_9", "ema_21", "macd_hist", "atr_14",
                             "bb_pctb", "volume_ratio", "technical_score")}
        ctx["technical"]["date"] = i.date.date().isoformat()
    news = await get_news(ticker, days=7, limit=8)
    ctx["news"] = [{"date": n.event_date.date().isoformat(), "title": n.title, "sentiment": n.sentiment_score,
                    "impact": n.impact_score, "summary": (n.analysis_metadata or {}).get("summary")} for n in news]
    ctx["news_score"] = await news_score(ticker)
    return ctx
