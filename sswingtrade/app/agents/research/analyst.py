"""RESEARCH ANALYST: consolidates technical + news context into a structured report."""

import json

from app.agents.base import BaseAgent, JSON_RULE
from app.llm.schemas import ResearchReport


class ResearchAnalyst(BaseAgent):
    name = "research_analyst"
    schema = ResearchReport
    system_prompt = (
        "Voce e um analista de research de acoes da B3 para swing trade (1 a 5 dias). "
        "Consolide APENAS os dados fornecidos em um relatorio objetivo. " + JSON_RULE
    )

    def build_prompt(self, ticker: str, context: dict, **_) -> str:
        return f"""Ativo: {ticker}
Dados (indicadores tecnicos e noticias recentes):
{json.dumps(context, ensure_ascii=False, default=str)[:3500]}

Retorne JSON com exatamente estas chaves:
{{"bias": "bullish" | "neutral" | "bearish",
 "thesis_summary": "2 frases",
 "strengths": ["..."], "weaknesses": ["..."], "key_risks": ["..."], "catalysts": ["..."],
 "confidence": numero de 0 a 1}}"""
