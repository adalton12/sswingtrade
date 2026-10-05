"""EVENT ANALYST: detects earnings/dividends/corporate announcements and whether they are priced in."""

from app.agents.base import BaseAgent, JSON_RULE
from app.llm.schemas import EventAnalysis


class EventAnalyst(BaseAgent):
    name = "event_analyst"
    schema = EventAnalysis
    system_prompt = (
        "Voce e um analista de eventos corporativos da B3 (balancos, proventos, fatos relevantes, M&A). "
        "Identifique o evento, a data (se houver) e estime se o mercado ja o precificou. " + JSON_RULE
    )

    def build_prompt(self, ticker: str, text: str, today: str = "", **_) -> str:
        return f"""Ativo: {ticker}
Data de hoje: {today or 'desconhecida'}
Comunicado/Texto: {text[:3000]}

Retorne JSON com exatamente estas chaves:
{{"event_type": "earnings" | "dividend" | "merger_acquisition" | "guidance" | "regulatory" | "other",
 "event_date": "YYYY-MM-DD" ou null,
 "days_to_event": inteiro ou null,
 "expected_impact": numero de -1.0 a 1.0,
 "already_priced_probability": numero de 0 a 1,
 "confidence": numero de 0 a 1,
 "summary": "resumo em 1 frase"}}"""
