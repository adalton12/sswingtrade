"""Async Ollama client with JSON mode, retries and schema validation - FASE 6. Works against the local Ollama or Ollama cloud."""

import json
import re
from typing import Optional, Type, TypeVar
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ValidationError

from app.config import settings
from app.services.logger import logger

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    pass


class LLMUnavailable(LLMError):
    """The service could not be reached or refused the request (network, 401/403, 429, 5xx...).
    Distinct from 'the model answered with unusable JSON': callers use it to pause a failing provider."""


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
                 timeout: Optional[float] = None, max_retries: int = 2, api_key: Optional[str] = None):
        self.base_url = (base_url or settings.OLLAMA_BASE_URL).rstrip("/")
        self.model = model or settings.OLLAMA_MODEL
        self.timeout = timeout or float(max(settings.OLLAMA_TIMEOUT, 120))  # CPU inference is slow
        self.max_retries = max_retries
        self._api_key = api_key or None
        if self._api_key and urlparse(self.base_url).scheme != "https":
            # never put a credential on a clear-text connection
            raise ValueError("an Ollama API key may only be used with an https:// base URL")

    @property
    def is_cloud(self) -> bool:
        return self._api_key is not None

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    async def available(self) -> dict:
        """Reachability + whether the model is there. Cloud: lists models (does not spend quota, does not prove the key)."""
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"{self.base_url}/api/tags", headers=self._headers())
                names = [m["name"] for m in r.json().get("models", [])]
            return {"reachable": True, "model": self.model, "model_installed": any(n.startswith(self.model) for n in names),
                    "installed": names}
        except Exception as e:
            return {"reachable": False, "error": str(e)}

    async def probe(self) -> dict:
        """ONE tiny real request: proves the key and model work (spends a little cloud quota)."""
        try:
            raw = await self._chat("Responda somente com JSON.", 'Devolva {"ok": true}')
            return {"ok": True, "model": self.model, "reply": raw[:80]}
        except httpx.HTTPStatusError as e:
            return {"ok": False, "model": self.model, "http_status": e.response.status_code}
        except Exception as e:
            return {"ok": False, "model": self.model, "error": type(e).__name__}

    async def _chat(self, system: str, prompt: str) -> str:
        payload = {
            "model": self.model, "stream": False, "format": "json",
            # Reasoning models (qwen3, deepseek-r1...) "think" first by default: on CPU that takes minutes per item and
            # blows the timeout. The agents only need the structured JSON, so reasoning is switched off.
            "think": False,
            "options": {"temperature": 0.1, "num_ctx": 4096},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        }
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.post(f"{self.base_url}/api/chat", json=payload, headers=self._headers())
            if r.status_code == 400 and "think" in r.text.lower():      # model/server that rejects the parameter
                payload.pop("think")
                r = await c.post(f"{self.base_url}/api/chat", json=payload, headers=self._headers())
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
                # str(e) carries the URL and status, never the request headers (so never the key)
                raise LLMUnavailable(f"Ollama request failed: {e}") from e
        raise LLMError(f"Invalid LLM output after {self.max_retries + 1} attempts: {last_err}")
