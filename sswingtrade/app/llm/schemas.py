"""
Structured outputs for the LLM agents - FASE 6.
The LLM is NEVER in the order-execution path: these outputs only feed scores that the
Risk Engine (FASE 8) may consume. Every field is validated and clamped.
"""

import re
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

HORIZONS = ("short_term_1_5_days", "medium_term", "long_term")


def _clamp(v, lo, hi):
    return max(lo, min(hi, float(v)))


def _num(v):
    if isinstance(v, str):
        v = v.replace(",", ".").strip().rstrip("%")
    return float(v)


def _strlist(v) -> List[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v.strip() else []
    return [str(x).strip() for x in v if str(x).strip()][:8]


class _Base(BaseModel):
    model_config = {"extra": "ignore"}


class NewsAnalysis(_Base):
    sentiment: float = 0.0              # -1 .. +1
    impact_score: float = 0.0           # 0 .. 10
    confidence: float = 0.5             # 0 .. 1
    horizon: str = "short_term_1_5_days"
    event_type: str = "other"
    affected_assets: List[str] = Field(default_factory=list)
    summary: str = ""
    bullish_factors: List[str] = Field(default_factory=list)
    bearish_factors: List[str] = Field(default_factory=list)
    already_priced_risk: float = 0.5    # 0 .. 1

    @field_validator("sentiment", mode="before")
    @classmethod
    def _s(cls, v): return _clamp(_num(v), -1, 1)

    @field_validator("impact_score", mode="before")
    @classmethod
    def _i(cls, v): return _clamp(_num(v), 0, 10)

    @field_validator("confidence", "already_priced_risk", mode="before")
    @classmethod
    def _c(cls, v):
        x = _num(v)
        return _clamp(x / 100 if x > 1 else x, 0, 1)

    @field_validator("horizon", mode="before")
    @classmethod
    def _h(cls, v):
        v = str(v).lower().strip()
        return v if v in HORIZONS else ("long_term" if "long" in v else "medium_term" if "med" in v else "short_term_1_5_days")

    @field_validator("affected_assets", mode="before")
    @classmethod
    def _a(cls, v): return [re.sub(r"\.SA$", "", a.upper().strip()) for a in _strlist(v)]

    @field_validator("bullish_factors", "bearish_factors", mode="before")
    @classmethod
    def _f(cls, v): return _strlist(v)

    @field_validator("event_type", "summary", mode="before")
    @classmethod
    def _t(cls, v): return str(v or "").strip()[:500]


class EventAnalysis(_Base):
    event_type: str = "other"            # earnings, dividend, merger, guidance, regulatory...
    event_date: Optional[str] = None     # YYYY-MM-DD if known
    days_to_event: Optional[int] = None
    expected_impact: float = 0.0         # -1 .. +1
    already_priced_probability: float = 0.5
    confidence: float = 0.5
    summary: str = ""

    @field_validator("expected_impact", mode="before")
    @classmethod
    def _e(cls, v): return _clamp(_num(v), -1, 1)

    @field_validator("already_priced_probability", "confidence", mode="before")
    @classmethod
    def _p(cls, v):
        x = _num(v)
        return _clamp(x / 100 if x > 1 else x, 0, 1)

    @field_validator("days_to_event", mode="before")
    @classmethod
    def _d(cls, v): return None if v in (None, "", "null") else int(float(v))


class ResearchReport(_Base):
    bias: str = "neutral"                # bullish | neutral | bearish
    thesis_summary: str = ""
    strengths: List[str] = Field(default_factory=list)
    weaknesses: List[str] = Field(default_factory=list)
    key_risks: List[str] = Field(default_factory=list)
    catalysts: List[str] = Field(default_factory=list)
    confidence: float = 0.5

    @field_validator("bias", mode="before")
    @classmethod
    def _b(cls, v):
        v = str(v).lower()
        return "bullish" if ("bull" in v or "alta" in v or "compra" in v) else "bearish" if ("bear" in v or "baixa" in v or "venda" in v) else "neutral"

    @field_validator("strengths", "weaknesses", "key_risks", "catalysts", mode="before")
    @classmethod
    def _l(cls, v): return _strlist(v)

    @field_validator("confidence", mode="before")
    @classmethod
    def _c(cls, v):
        x = _num(v)
        return _clamp(x / 100 if x > 1 else x, 0, 1)


class DevilsAdvocateVerdict(_Base):
    counter_arguments: List[str] = Field(default_factory=list)
    key_risks: List[str] = Field(default_factory=list)
    counter_score: float = 50.0          # 0..100: strength of the case AGAINST the trade
    block_recommended: bool = False
    rationale: str = ""

    @field_validator("counter_score", mode="before")
    @classmethod
    def _cs(cls, v): return _clamp(_num(v), 0, 100)

    @field_validator("counter_arguments", "key_risks", mode="before")
    @classmethod
    def _l(cls, v): return _strlist(v)

    @field_validator("block_recommended", mode="before")
    @classmethod
    def _bl(cls, v):
        return v if isinstance(v, bool) else str(v).lower() in ("true", "1", "sim", "yes")
