import asyncio
import json
from datetime import datetime, timedelta

import pytest

from app.agents.devil_advocate.advocate import DevilsAdvocate
from app.agents.news.analyst import NewsAnalyst
from app.llm.client import LLMError, OllamaClient, extract_json
from app.llm.schemas import DevilsAdvocateVerdict, NewsAnalysis
from app.services.news_service import aggregate_news_score, content_hash, parse_google_news_rss


def run(coro):
    return asyncio.run(coro)


class ScriptedClient(OllamaClient):
    """OllamaClient whose HTTP call returns scripted outputs (no Ollama needed)."""

    def __init__(self, outputs, **kw):
        super().__init__(base_url="http://x", model="fake", **kw)
        self.outputs, self.calls, self.prompts = list(outputs), 0, []

    async def _chat(self, system, prompt):
        self.calls += 1
        self.prompts.append(prompt)
        return self.outputs.pop(0)


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Claro! {"a": 1} espero ter ajudado') == {"a": 1}
    with pytest.raises(Exception):
        extract_json("sem json")


def test_news_schema_clamps_and_normalises():
    a = NewsAnalysis.model_validate({
        "sentiment": "1,8", "impact_score": 15, "confidence": 85, "already_priced_risk": "0.2",
        "horizon": "curto prazo", "affected_assets": ["petr4.sa", " VALE3 "], "bullish_factors": "lucro maior"})
    assert a.sentiment == 1.0 and a.impact_score == 10.0
    assert a.confidence == pytest.approx(0.85) and a.already_priced_risk == pytest.approx(0.2)
    assert a.horizon == "short_term_1_5_days"
    assert a.affected_assets == ["PETR4", "VALE3"] and a.bullish_factors == ["lucro maior"]


def test_agent_parses_spec_example():
    spec = {"sentiment": 0.75, "impact_score": 8.0, "confidence": 0.85, "horizon": "short_term_1_5_days",
            "event_type": "earnings_release", "affected_assets": ["PETR4"], "summary": "ok",
            "bullish_factors": ["Lucro +15%"], "bearish_factors": ["Petroleo em queda"], "already_priced_risk": 0.2}
    agent = NewsAnalyst(ScriptedClient([json.dumps(spec)]))
    r = run(agent.run(ticker="PETR4", title="Petrobras lucra mais", content="..."))
    assert r.sentiment == 0.75 and r.event_type == "earnings_release"
    assert "PETR4" in agent.client.prompts[0]


def test_retry_on_invalid_json_then_success():
    c = ScriptedClient(["isto nao e json", '{"sentiment": 0.1}'])
    r = run(NewsAnalyst(c).run(ticker="VALE3", title="x" * 10))
    assert c.calls == 2 and r.sentiment == 0.1
    assert "invalida" in c.prompts[1]


def test_gives_up_after_retries():
    c = ScriptedClient(["lixo"] * 3, max_retries=2)
    with pytest.raises(LLMError):
        run(NewsAnalyst(c).run(ticker="VALE3", title="x" * 10))
    assert c.calls == 3


def test_devils_advocate_verdict():
    out = {"counter_arguments": ["RSI esticado"], "key_risks": "balanco amanha", "counter_score": "72",
           "block_recommended": "true", "rationale": "risco de evento"}
    v = run(DevilsAdvocate(ScriptedClient([json.dumps(out)])).run(
        ticker="ITUB4", direction="buy", thesis="rompimento", context={"technical": {"rsi_14": 78}}))
    assert isinstance(v, DevilsAdvocateVerdict)
    assert v.counter_score == 72 and v.block_recommended is True and v.key_risks == ["balanco amanha"]


def test_content_hash_dedupe():
    a = content_hash("PETR4", "Petrobras Lucra  Mais", "texto")
    assert a == content_hash("petr4", "petrobras lucra mais", "texto")
    assert a != content_hash("VALE3", "petrobras lucra mais", "texto")


def test_aggregate_news_score():
    now = datetime(2026, 10, 5, 12)
    pos = {"sentiment_score": 0.8, "impact_score": 8, "confidence": 0.9, "event_date": now - timedelta(hours=3)}
    neg_old = {"sentiment_score": -0.9, "impact_score": 8, "confidence": 0.9, "event_date": now - timedelta(days=6)}
    s = aggregate_news_score([pos, neg_old], now)
    assert s["news_score"] > 70 and s["n_news"] == 2          # recent positive dominates old negative
    assert aggregate_news_score([], now)["news_score"] == 50.0
    assert aggregate_news_score([{"sentiment_score": None, "event_date": now}], now)["n_news"] == 0
    neg = {**pos, "sentiment_score": -0.8}
    assert aggregate_news_score([neg], now)["news_score"] < 30


RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Petrobras &amp; dividendos: lucro sobe</title><pubDate>Mon, 05 Oct 2026 14:00:00 GMT</pubDate>
<description>&lt;a href="x"&gt;Resumo&lt;/a&gt; da noticia</description><source>Valor</source></item>
<item><title></title></item></channel></rss>"""


def test_parse_rss():
    items = parse_google_news_rss(RSS)
    assert len(items) == 1
    assert items[0]["title"] == "Petrobras & dividendos: lucro sobe"
    assert items[0]["source"] == "Valor" and "Resumo" in items[0]["content"] and "<" not in items[0]["content"]
    assert items[0]["event_date"] == datetime(2026, 10, 5, 14, 0)
