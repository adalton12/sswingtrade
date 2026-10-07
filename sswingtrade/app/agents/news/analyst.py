"""NEWS ANALYST: sentiment (-1..+1) and 1-5 day impact of one news item."""

from app.agents.base import BaseAgent, JSON_RULE
from app.llm.schemas import NewsAnalysis


class NewsAnalyst(BaseAgent):
    name = "news_analyst"
    llm_role = "news"            # nightly batch: ~40 calls/day, local model unless llm.news_use_cloud is on
    schema = NewsAnalysis
    system_prompt = (
        "Voce e um analista de mercado de acoes da B3 focado em swing trade (1 a 5 dias). "
        "Avalie a noticia quanto a sentimento e impacto no preco do ativo no horizonte curto. "
        "Considere se a informacao ja foi precificada pelo mercado. " + JSON_RULE
    )

    def build_prompt(self, ticker: str, title: str, content: str = "", **_) -> str:
        return f"""Ativo: {ticker}
Titulo: {title}
Texto: {(content or '')[:2500]}

Retorne JSON com exatamente estas chaves:
{{"sentiment": numero de -1.0 (muito negativo) a 1.0 (muito positivo),
 "impact_score": numero de 0 a 10 (impacto esperado em 1-5 dias),
 "confidence": numero de 0 a 1,
 "horizon": "short_term_1_5_days" | "medium_term" | "long_term",
 "event_type": "earnings_release" | "dividend" | "merger_acquisition" | "guidance" | "regulatory" | "macro" | "management" | "other",
 "affected_assets": ["TICKER", ...],
 "summary": "resumo em 1 frase",
 "bullish_factors": ["..."],
 "bearish_factors": ["..."],
 "already_priced_risk": numero de 0 a 1 (chance de ja estar no preco)}}"""
