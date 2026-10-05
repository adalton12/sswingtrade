"""Async Ollama client with JSON mode, retries and schema validation - FASE 6."""

import json
import re
from typing import Optional, Type, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.config import settings
from app.services.logger import logger

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    pass


def extract_json(text: str) -> dict:
    """Parse model output into a dict, tolerating code fences / surrounding prose."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, flags=re.S)
        if m:
            return json.loads(m.group(0))
        raise


class OllamaClient:
    def __init__(self, base_url: Optional[str] = None, model: Optional[str] = None,
                 timeout: Optional[float] = None, max_retries: int = 2):
        self.base_url = (base_url or settings.OLLAMA_BASE_URL).rstrip("/")
        self.model = model or settings.OLLAMA_MODEL
        self.timeout = timeout or float(max(settings.OLLAMA_TIMEOUT, 120))  # CPU inference is slow
        self.max_retries = max_retries

    async def available(self) -> dict:
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"{self.base_url}/api/tags")
                names = [m["name"] for m in r.json().get("models", [])]
            return {"reachable": True, "model": self.model, "model_installed": any(n.startswith(self.model) for n in names),
                    "installed": names}
        except Exception as e:
            return {"reachable": False, "error": str(e)}

    async def _chat(self, system: str, prompt: str) -> str:
        payload = {
            "model": self.model, "stream": False, "format": "json",
            "options": {"temperature": 0.1, "num_ctx": 4096},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        }
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.post(f"{self.base_url}/api/chat", json=payload)
            r.raise_for_status()
            return r.json()["message"]["content"]

    async def generate_json(self, system: str, prompt: str, schema: Type[T]) -> T:
        """Call the model and return a validated `schema` instance (retries on bad JSON)."""
        hint = ""
        last_err = None
        for attempt in range(self.max_retries + 1):
            try:
                raw = await self._chat(system, prompt + hint)
                return schema.model_validate(extract_json(raw))
            except (json.JSONDecodeError, ValidationError, ValueError, KeyError) as e:
                last_err = e
                hint = "\n\nSua resposta anterior foi invalida. Responda SOMENTE com um objeto JSON valido no formato pedido."
                logger.warning(f"LLM invalid output (attempt {attempt + 1}): {e}")
            except httpx.HTTPError as e:
                raise LLMError(f"Ollama request failed: {e}") from e
        raise LLMError(f"Invalid LLM output after {self.max_retries + 1} attempts: {last_err}")
