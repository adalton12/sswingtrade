"""Base class for LLM agents. Agents analyse and score; they never place orders."""

from typing import Optional, Type

from pydantic import BaseModel

from app.llm.client import OllamaClient
from app.llm.factory import get_llm

JSON_RULE = "Responda SOMENTE com um objeto JSON valido, sem texto fora do JSON. Nao invente fatos: se o texto nao informa, use valores neutros e confidence baixa."


class BaseAgent:
    name = "base"
    llm_role = "agents"          # which LLM policy applies (see app/llm/factory.py): "agents" (cloud first) or "news"
    system_prompt = ""
    schema: Type[BaseModel] = BaseModel

    def __init__(self, client: Optional[OllamaClient] = None):
        self.client = client or get_llm(self.llm_role)

    def build_prompt(self, **ctx) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    async def run(self, **ctx) -> BaseModel:
        return await self.client.generate_json(self.system_prompt, self.build_prompt(**ctx), self.schema)
