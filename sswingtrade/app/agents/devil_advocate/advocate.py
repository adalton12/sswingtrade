"""DEVIL'S ADVOCATE: actively tries to contradict a trade thesis before entry."""

import json

from app.agents.base import BaseAgent, JSON_RULE
from app.llm.schemas import DevilsAdvocateVerdict


class DevilsAdvocate(BaseAgent):
    name = "devils_advocate"
    schema = DevilsAdvocateVerdict
    system_prompt = (
        "Voce e o ADVOGADO DO DIABO de uma mesa de swing trade. Seu trabalho e CONTRADIZER a tese de entrada: "
        "procure divergencias tecnicas, noticias negativas, risco regulatorio, eventos proximos e sinais de que "
        "o movimento ja foi precificado. Use SOMENTE os dados fornecidos. " + JSON_RULE
    )

    def build_prompt(self, ticker: str, direction: str, thesis: str, context: dict, **_) -> str:
        return f"""Ativo: {ticker}
Tese proposta: {direction} - {thesis}
Dados disponiveis:
{json.dumps(context, ensure_ascii=False, default=str)[:3500]}

Retorne JSON com exatamente estas chaves:
{{"counter_arguments": ["..."],
 "key_risks": ["..."],
 "counter_score": numero de 0 a 100 (forca do argumento CONTRA a operacao),
 "block_recommended": true | false,
 "rationale": "1-2 frases"}}"""
