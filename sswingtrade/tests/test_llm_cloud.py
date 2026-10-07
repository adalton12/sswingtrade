"""Ollama cloud integration: key handling, cloud-first with local reserve, who uses the cloud, no secret leaks."""

import json
from pathlib import Path

import httpx
import pytest
import yaml
from loguru import logger as loguru_logger

import app.llm.client as llm_client
import app.llm.factory as factory
from app.agents.devil_advocate.advocate import DevilsAdvocate
from app.agents.news.analyst import NewsAnalyst
from app.agents.research.analyst import ResearchAnalyst
from app.config import settings
from app.llm.client import LLMError, LLMUnavailable, OllamaClient
from app.llm.factory import FallbackClient, get_llm
from app.llm.schemas import NewsAnalysis
from app.main import app
from app.runtime.params import BY_KEY, params

KEY = "TESTKEY" + "x1" * 14                     # fake secret, only used to prove it never leaks
CLOUD_URL = "https://ollama.com"
OK_JSON = {"message": {"content": json.dumps({"sentiment": 0.5, "impact_score": 5, "confidence": 0.7})}}

OK = lambda req: httpx.Response(200, json=OK_JSON)                                           # noqa: E731
DOWN = lambda req: httpx.Response(503, json={"error": "down"})                               # noqa: E731
UNAUTH = lambda req: httpx.Response(401, json={"error": "unauthorized"})                     # noqa: E731
BAD_JSON = lambda req: httpx.Response(200, json={"message": {"content": "isto nao e json"}})  # noqa: E731


def world(monkeypatch, cloud=OK, local=OK):
    """Replace the network: ollama.com answers with `cloud`, any other host with `local`. Returns the hit log."""
    hits = []

    def handler(req):
        hits.append((req.url.host, req.url.path, req.headers.get("authorization")))
        return (cloud if req.url.host == "ollama.com" else local)(req)
    real = httpx.AsyncClient
    monkeypatch.setattr(llm_client.httpx, "AsyncClient",
                        lambda *a, **k: real(transport=httpx.MockTransport(handler), timeout=k.get("timeout")))
    return hits


def cloud_hits(hits):
    return [h for h in hits if h[0] == "ollama.com"]


def local_hits(hits):
    return [h for h in hits if h[0] != "ollama.com"]


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setattr(settings, "OLLAMA_API_KEY", KEY)
    factory.reset_cooldown()
    yield
    factory.reset_cooldown()


def make_fallback():
    cloud = OllamaClient(base_url=CLOUD_URL, model="gemma4:31b", api_key=KEY)
    return FallbackClient(cloud, OllamaClient(base_url="http://localhost:11434", model="qwen3:8b"))


# ============================================================================= the key and where it may go
async def test_key_goes_to_the_cloud_only(monkeypatch):
    hits = world(monkeypatch)
    await OllamaClient(base_url=CLOUD_URL, model="gemma4:31b", api_key=KEY)._chat("s", "p")
    await OllamaClient(base_url="http://localhost:11434", model="qwen3:8b")._chat("s", "p")
    assert cloud_hits(hits)[0][2] == f"Bearer {KEY}"
    assert local_hits(hits)[0][2] is None, "the key must never be sent to the local Ollama"


def test_key_is_refused_on_a_clear_text_url():
    with pytest.raises(ValueError):
        OllamaClient(base_url="http://ollama.com", model="m", api_key=KEY)
    assert OllamaClient(base_url="https://ollama.com", model="m", api_key=KEY).is_cloud
    assert not OllamaClient(model="m").is_cloud


# ============================================================================= cloud first, local reserve
async def test_cloud_answers_and_local_is_not_touched(monkeypatch):
    hits = world(monkeypatch)
    llm = make_fallback()
    out = await llm.generate_json("s", "p", NewsAnalysis)
    assert out.sentiment == 0.5 and llm.model == "cloud:gemma4:31b"
    assert local_hits(hits) == []


async def test_cloud_outage_falls_back_to_local_and_pauses_the_cloud(monkeypatch):
    hits = world(monkeypatch, cloud=DOWN)
    llm = make_fallback()
    assert (await llm.generate_json("s", "p", NewsAnalysis)).sentiment == 0.5
    assert llm.model == "local:qwen3:8b"
    assert len(cloud_hits(hits)) == 1
    await llm.generate_json("s", "p", NewsAnalysis)                  # second request: cloud is skipped for a while
    assert len(cloud_hits(hits)) == 1 and len(local_hits(hits)) == 2


async def test_cloud_is_tried_again_after_the_pause(monkeypatch):
    hits = world(monkeypatch, cloud=DOWN)
    now = [1000.0]
    monkeypatch.setattr(factory.time, "monotonic", lambda: now[0])
    llm = make_fallback()
    await llm.generate_json("s", "p", NewsAnalysis)
    now[0] += factory.COOLDOWN_SECONDS + 1
    await llm.generate_json("s", "p", NewsAnalysis)
    assert len(cloud_hits(hits)) == 2


async def test_refused_key_or_quota_also_falls_back(monkeypatch):
    hits = world(monkeypatch, cloud=UNAUTH)
    llm = make_fallback()
    assert (await llm.generate_json("s", "p", NewsAnalysis)).sentiment == 0.5 and llm.model.startswith("local:")


async def test_unusable_cloud_json_falls_back_for_that_request_only(monkeypatch):
    hits = world(monkeypatch, cloud=BAD_JSON)
    llm = make_fallback()
    await llm.generate_json("s", "p", NewsAnalysis)
    await llm.generate_json("s", "p", NewsAnalysis)
    # one bad answer is not an outage: the cloud is asked again (3 attempts each), no pause
    assert len(cloud_hits(hits)) == 6 and len(local_hits(hits)) == 2


async def test_both_down_raises_the_error_the_news_batch_stops_on(monkeypatch):
    world(monkeypatch, cloud=DOWN, local=DOWN)
    with pytest.raises(LLMUnavailable) as e:
        await make_fallback().generate_json("s", "p", NewsAnalysis)
    assert "request failed" in str(e.value)               # news_service stops the batch early on exactly this text
    assert isinstance(e.value, LLMError)


# ============================================================================= secrets never leak
async def test_key_never_appears_in_errors_or_logs(monkeypatch):
    world(monkeypatch, cloud=UNAUTH, local=DOWN)
    captured = []
    sink = loguru_logger.add(captured.append, level="DEBUG")
    try:
        with pytest.raises(LLMError) as e:
            await make_fallback().generate_json("s", "p", NewsAnalysis)
    finally:
        loguru_logger.remove(sink)
    assert KEY not in str(e.value) and KEY not in "".join(str(m) for m in captured)


async def test_status_endpoint_reports_the_cloud_without_the_key(monkeypatch, with_key):
    def tags(req):
        return httpx.Response(200, json={"models": [{"name": "gemma4:31b" if req.url.host == "ollama.com" else "qwen3:8b"}]})

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        def route(req):
            return tags(req) if req.url.path == "/api/tags" else OK(req)
        world(monkeypatch, cloud=route, local=route)
        body = (await client.get("/api/v1/news/llm/status?probe_cloud=true")).text
    data = json.loads(body)
    assert data["cloud"]["configured"] and data["cloud"]["model_listed"] is True and data["cloud"]["probe"]["ok"] is True
    assert data["cloud"]["used_by"] == {"agents": True, "news_batch": False}
    assert data["reachable"] is True and "installed" in data         # the local fields are still there
    assert KEY not in body


# ============================================================================= who uses the cloud
def test_cloud_is_off_without_a_key():
    assert not settings.OLLAMA_API_KEY
    assert not isinstance(get_llm("agents"), FallbackClient) and not isinstance(get_llm("news"), FallbackClient)


def test_default_policy_text_agents_cloud_news_batch_local(with_key):
    assert isinstance(get_llm("agents"), FallbackClient)
    assert not isinstance(get_llm("news"), FallbackClient), "the nightly batch (~40 calls) would drain the free quota"
    assert isinstance(DevilsAdvocate().client, FallbackClient) and isinstance(ResearchAnalyst().client, FallbackClient)
    assert NewsAnalyst().llm_role == "news" and not isinstance(NewsAnalyst().client, FallbackClient)


async def test_policy_is_editable_at_runtime(db, with_key):
    await params.update(db, {"llm.news_use_cloud": True, "llm.agents_use_cloud": False, "llm.cloud_model": "gpt-oss:120b"})
    assert isinstance(get_llm("news"), FallbackClient) and not isinstance(get_llm("agents"), FallbackClient)
    assert get_llm("news").cloud.model == "gpt-oss:120b"
    assert BY_KEY["llm.news_use_cloud"].default is False and BY_KEY["llm.agents_use_cloud"].default is True


# ============================================================================= compose / .env.example
def test_only_the_api_container_receives_the_key():
    root = Path(__file__).resolve().parents[1]
    services = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))["services"]
    assert "OLLAMA_API_KEY" in services["api"]["environment"]
    for name in ("postgres", "redis", "grafana"):
        assert "OLLAMA_API_KEY" not in json.dumps(services[name])
    example = (root / ".env.example").read_text(encoding="utf-8")
    line = next(l for l in example.splitlines() if l.startswith("OLLAMA_API_KEY="))
    assert line == "OLLAMA_API_KEY=", "the example file must never carry a real key"
