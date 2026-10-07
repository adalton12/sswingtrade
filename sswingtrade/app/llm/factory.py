"""
Which LLM serves which job.

* Cloud (Ollama cloud, needs OLLAMA_API_KEY) is used first when enabled for the role, the local Ollama is the RESERVE:
  if the cloud call fails (no internet, key refused, quota exhausted, bad JSON) the same request is answered locally.
* After a service failure the cloud is skipped for a few minutes, so a nightly batch does not wait on a dead endpoint
  for every single headline.
* `model` always says who produced the last answer ("cloud:..." / "local:...") and is stored with each news analysis.
Roles: "news" (nightly batch, high volume -> cloud OFF by default to protect the free quota) and "agents" (research,
devil's advocate, event analyst: few calls on demand -> cloud ON by default).
"""

import time
from typing import Optional, Type, TypeVar

from pydantic import BaseModel

from app.config import settings
from app.llm.client import LLMError, LLMUnavailable, OllamaClient
from app.runtime.params import params
from app.services.logger import logger

T = TypeVar("T", bound=BaseModel)

COOLDOWN_SECONDS = 300
_cloud_down_until = 0.0          # process-wide: shared by every agent instance


def reset_cooldown() -> None:
    global _cloud_down_until
    _cloud_down_until = 0.0


def cloud_enabled_for(role: str) -> bool:
    if not settings.OLLAMA_API_KEY:
        return False
    return bool(params.get("llm.news_use_cloud" if role == "news" else "llm.agents_use_cloud"))


class FallbackClient:
    """Cloud first, local reserve. Same surface the agents use: generate_json() and .model."""

    def __init__(self, cloud: OllamaClient, local: OllamaClient):
        self.cloud, self.local = cloud, local
        self.model = f"cloud:{cloud.model}"

    async def generate_json(self, system: str, prompt: str, schema: Type[T]) -> T:
        global _cloud_down_until
        if time.monotonic() >= _cloud_down_until:
            try:
                out = await self.cloud.generate_json(system, prompt, schema)
                self.model = f"cloud:{self.cloud.model}"
                return out
            except LLMUnavailable as e:
                _cloud_down_until = time.monotonic() + COOLDOWN_SECONDS
                logger.warning(f"Ollama cloud unavailable ({e}); using the local model for the next "
                               f"{COOLDOWN_SECONDS // 60} min")
            except LLMError as e:                      # answered, but not with usable JSON: only this request goes local
                logger.warning(f"Ollama cloud gave unusable output ({e}); retrying this request locally")
        out = await self.local.generate_json(system, prompt, schema)
        self.model = f"local:{self.local.model}"
        return out


def get_llm(role: str = "agents", local: Optional[OllamaClient] = None):
    """The client an agent should use for `role`."""
    local = local or OllamaClient()
    if not cloud_enabled_for(role):
        return local
    cloud = OllamaClient(base_url=settings.OLLAMA_CLOUD_URL, model=params.get("llm.cloud_model"),
                         api_key=settings.OLLAMA_API_KEY)
    return FallbackClient(cloud, local)
