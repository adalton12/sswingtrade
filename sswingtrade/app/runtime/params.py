"""
Runtime parameters - everything the trader may want to tune, editable WITHOUT redeploy.

* Defaults come from environment (`app.config.settings`) so Docker env still bootstraps the system.
* Overrides live in the `app_settings` table; the in-memory cache is refreshed on every update
  and before each trading cycle.
* Every value is type/range validated; cross-field rules are checked before anything is saved.
* ENABLE_REAL_TRADING is deliberately NOT a runtime parameter (it can only be set via environment).
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings

DEFAULT_UNIVERSE = [
    "PETR4", "VALE3", "ITUB4", "BBDC4", "ABEV3", "WEGE3", "RENT3", "EQTL3", "BBAS3", "B3SA3",
    "SUZB3", "JBSS3", "LREN3", "MGLU3", "RADL3", "HAPV3", "RAIL3", "VIVT3", "ELET3", "PRIO3",
]

TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


@dataclass
class ParamDef:
    key: str
    type: str                      # float | int | bool | str | choice | time | tickers
    default: Any
    group: str
    label: str
    desc: str = ""
    min: Optional[float] = None
    max: Optional[float] = None
    choices: Optional[List[str]] = None
    unit: str = ""
    profile_managed: bool = False  # changed by aggressiveness profiles
    needs_restart: bool = False


def _d(*a, **k) -> ParamDef:
    return ParamDef(*a, **k)


DEFS: List[ParamDef] = [
    # ---------------------------------------------------------------- capital
    _d("capital.initial_capital", "float", settings.INITIAL_CAPITAL, "Capital", "Capital inicial", "Usado só ao criar a conta (conta nova / reset).", 1, 1e9, unit="R$"),
    _d("capital.weekly_deposit_enabled", "bool", settings.WEEKLY_DEPOSIT_ENABLED, "Capital", "Aporte semanal ligado", "Soma o aporte semanal na segunda-feira."),
    _d("capital.weekly_deposit", "float", settings.WEEKLY_DEPOSIT, "Capital", "Aporte semanal padrão", "Valor padrão; para valores diferentes por data use 'Aportes planejados'.", 0, 1e9, unit="R$"),
    _d("capital.monthly_deposit_enabled", "bool", settings.MONTHLY_DEPOSIT_ENABLED, "Capital", "Aporte mensal ligado", "Soma no 1º dia útil do mês."),
    _d("capital.monthly_deposit", "float", settings.MONTHLY_DEPOSIT, "Capital", "Aporte mensal padrão", "", 0, 1e9, unit="R$"),
    _d("capital.per_op_pct", "float", round(settings.MAX_DAILY_ALLOCATION / settings.MAX_WEEKLY_CAPITAL * 100, 4), "Capital",
       "Orçamento por operação/dia", "% do patrimônio que pode entrar por dia (20% = R$100 de R$500). Escala com os juros compostos.",
       0.5, 100, unit="% do patrimônio", profile_managed=True),
    _d("capital.max_exposure_pct", "float", 100.0, "Capital", "Exposição máxima simultânea", "Teto do total investido, % do patrimônio.", 5, 100, unit="%"),
    _d("capital.max_entries_per_day", "int", settings.MAX_ENTRIES_PER_DAY, "Capital", "Entradas por dia (máx.)", "O orçamento diário é dividido entre as entradas.", 1, 20, profile_managed=True),
    _d("capital.max_open_positions", "int", settings.MAX_OPEN_POSITIONS, "Capital", "Posições abertas (máx.)", "", 1, 30, profile_managed=True),
    # ---------------------------------------------------------------- risk
    _d("risk.profile", "choice", "moderate", "Risco", "Perfil de agressividade", "Aplicar um perfil ajusta vários parâmetros de uma vez; editar um deles manualmente muda para 'custom'.",
       choices=["conservative", "moderate", "aggressive", "very_aggressive", "custom"]),
    _d("risk.max_risk_per_trade_pct", "float", settings.MAX_RISK_PER_TRADE_PCT, "Risco", "Risco máx. por operação", "Perda máxima no stop, % do patrimônio.", 0.1, 25, unit="% do patrimônio", profile_managed=True),
    _d("risk.atr_stop_mult", "float", settings.STOP_LOSS_ATR_MULTIPLIER, "Risco", "Stop (múltiplo do ATR)", "", 0.5, 6, profile_managed=True),
    _d("risk.risk_reward", "float", settings.TAKE_PROFIT_RATIO, "Risco", "Alvo (relação risco/retorno)", "Alvo = entrada + R/R × risco.", 1, 8, profile_managed=True),
    _d("risk.min_rr", "float", settings.MIN_RISK_REWARD, "Risco", "R/R mínimo aceito", "Ordens abaixo disso são vetadas.", 1, 8),
    _d("risk.min_score", "float", settings.MIN_SCORE_TO_TRADE, "Risco", "Score mínimo para operar", "Score composto 0-100.", 0, 100, profile_managed=True),
    _d("risk.max_hold_days", "int", settings.MAX_HOLD_DAYS, "Risco", "Prazo máx. da posição", "Saída por tempo (pregões).", 1, 30, unit="pregões"),
    _d("risk.max_data_age_bdays", "int", 3, "Risco", "Idade máx. dos dados (pregões)",
       "Recusa entradas se o último candle estiver mais velho que isto (coleta de dados falhou). 0 = desligado. "
       "Feriados não são modelados; 3 pregões absorve o Carnaval.", 0, 30, unit="pregões"),
    _d("risk.advocate_block_score", "float", settings.ADVOCATE_BLOCK_SCORE, "Risco", "Veto do Advogado do Diabo", "Nota (0-100) a partir da qual a entrada é vetada.", 1, 100),
    _d("risk.paper_enabled", "bool", settings.ENABLE_PAPER_TRADING, "Risco", "Paper trading ligado", "Chave geral: desligada, nenhuma ordem é aprovada."),
    # ---------------------------------------------------------------- loss limits (0 = off)
    _d("limits.daily_loss_pct", "float", settings.MAX_DAILY_LOSS_PERCENT, "Limites de perda", "Perda diária máxima", "% do patrimônio no início do dia. 0 = desligado.", 0, 100, unit="%", profile_managed=True),
    _d("limits.daily_loss_amount", "float", 0.0, "Limites de perda", "Perda diária máxima (R$)", "Valor absoluto; vale o menor entre % e R$. 0 = desligado.", 0, 1e9, unit="R$"),
    _d("limits.weekly_loss_pct", "float", 4.0, "Limites de perda", "Perda semanal máxima", "% do patrimônio no início da semana. 0 = desligado.", 0, 100, unit="%", profile_managed=True),
    _d("limits.weekly_loss_amount", "float", 0.0, "Limites de perda", "Perda semanal máxima (R$)", "", 0, 1e9, unit="R$"),
    _d("limits.monthly_loss_pct", "float", 8.0, "Limites de perda", "Perda mensal máxima", "% do patrimônio no início do mês. 0 = desligado.", 0, 100, unit="%", profile_managed=True),
    _d("limits.monthly_loss_amount", "float", 0.0, "Limites de perda", "Perda mensal máxima (R$)", "", 0, 1e9, unit="R$"),
    # ---------------------------------------------------------------- score
    _d("score.w_technical", "float", settings.W_TECHNICAL, "Score composto", "Peso técnico", "Os pesos são normalizados (não precisam somar 1).", 0, 10),
    _d("score.w_news", "float", settings.W_NEWS, "Score composto", "Peso notícias/eventos", "", 0, 10),
    _d("score.w_ml", "float", settings.W_ML, "Score composto", "Peso ML", "", 0, 10),
    _d("score.w_volume", "float", settings.W_VOLUME_MOMENTUM, "Score composto", "Peso volume/momentum", "", 0, 10),
    _d("score.ml_prob_full_scale", "float", settings.ML_PROB_FULL_SCALE, "Score composto", "Probabilidade que vale 100 no ML", "", 0.05, 1),
    # ---------------------------------------------------------------- costs
    _d("costs.fee_rate_pct", "float", round(settings.FEE_RATE * 100, 6), "Custos", "Taxas B3 por lado", "Emolumentos + liquidação, % do valor.", 0, 5, unit="%"),
    _d("costs.brokerage", "float", settings.BROKERAGE_PER_ORDER, "Custos", "Corretagem por ordem", "", 0, 1000, unit="R$"),
    _d("costs.slippage_bps", "float", settings.SLIPPAGE_BPS, "Custos", "Slippage", "Pontos-base (1 bp = 0,01%).", 0, 500, unit="bps"),
    # ---------------------------------------------------------------- universe
    _d("universe.tickers", "tickers", list(DEFAULT_UNIVERSE), "Ativos", "Lista de ativos monitorados", "Tickers da B3 separados por vírgula."),
    # ---------------------------------------------------------------- news / ml
    _d("news.lookback_days", "int", settings.NEWS_LOOKBACK_DAYS, "Notícias e IA", "Janela de notícias (dias)", "", 1, 60),
    _d("news.batch_limit", "int", settings.LLM_BATCH_LIMIT, "Notícias e IA", "Notícias analisadas por lote", "Limite do processamento em lote do LLM.", 1, 500),
    _d("news.auto_fetch", "bool", settings.NEWS_AUTO_FETCH, "Notícias e IA", "Buscar notícias (RSS) automaticamente"),
    _d("ml.target", "choice", settings.ML_SCORE_TARGET, "Notícias e IA", "Alvo do ML usado no score", choices=["y_3d_2pct", "y_5d_4pct"]),
    # ---------------------------------------------------------------- schedule (BRT, HH:MM)
    _d("schedule.morning_time", "time", "07:00", "Agenda (horário de Brasília)", "Rotina da manhã", "Aportes e reset dos bloqueios."),
    _d("schedule.collect_daily_time", "time", "18:30", "Agenda (horário de Brasília)", "Coleta de candles diários"),
    _d("schedule.collect_intraday_time", "time", "18:45", "Agenda (horário de Brasília)", "Coleta intraday"),
    _d("schedule.indicators_time", "time", "19:00", "Agenda (horário de Brasília)", "Cálculo de indicadores"),
    _d("schedule.news_time", "time", "19:30", "Agenda (horário de Brasília)", "Notícias + análise LLM"),
    _d("schedule.cycle_time", "time", "20:00", "Agenda (horário de Brasília)", "Ciclo de trading (decisões/ordens)"),
    # ---------------------------------------------------------------- forecast
    _d("forecast.horizon_weeks", "int", 26, "Provisão", "Horizonte da provisão (semanas)", "", 1, 260),
    _d("forecast.trades_per_week", "float", 5.0, "Provisão", "Operações por semana (estimativa)", "Usado na provisão.", 0.5, 50, profile_managed=True),
    _d("forecast.simulations", "int", 2000, "Provisão", "Simulações Monte Carlo", "", 200, 20000),
    _d("forecast.default_atr_pct", "float", 2.0, "Provisão", "ATR típico (fallback)", "% do preço, usado se não houver candles.", 0.3, 15, unit="%"),
    _d("forecast.ml_shrink_max", "float", 0.6, "Provisão", "Confiança máxima no ML", "0 = ignora o ML; 1 = confia totalmente no lift do ML.", 0, 1),
    _d("forecast.evidence_k", "float", 30.0, "Provisão", "Peso da hipótese vs. resultados reais", "Quantas operações reais equivalem à hipótese inicial.", 1, 500),
]
BY_KEY: Dict[str, ParamDef] = {d.key: d for d in DEFS}

# ------------------------------------------------------------------------------------------------
# Aggressiveness profiles ("arriscar menos / mais / muito")
# ------------------------------------------------------------------------------------------------
PROFILES: Dict[str, Dict[str, Any]] = {
    "conservative": {
        "label": "Conservador", "desc": "Posições pequenas, stops largos, poucos trades, limites de perda curtos.",
        "values": {"capital.per_op_pct": 10, "capital.max_entries_per_day": 2, "capital.max_open_positions": 3,
                   "risk.max_risk_per_trade_pct": 1.0, "risk.atr_stop_mult": 2.0, "risk.risk_reward": 2.5, "risk.min_score": 70,
                   "limits.daily_loss_pct": 1.0, "limits.weekly_loss_pct": 3.0, "limits.monthly_loss_pct": 6.0,
                   "forecast.trades_per_week": 3},
    },
    "moderate": {
        "label": "Moderado", "desc": "Padrão do projeto: 20% do patrimônio/dia, risco ~2% por operação.",
        "values": {"capital.per_op_pct": 20, "capital.max_entries_per_day": 5, "capital.max_open_positions": 5,
                   "risk.max_risk_per_trade_pct": 2.0, "risk.atr_stop_mult": 1.5, "risk.risk_reward": 2.0, "risk.min_score": 60,
                   "limits.daily_loss_pct": 1.5, "limits.weekly_loss_pct": 4.0, "limits.monthly_loss_pct": 8.0,
                   "forecast.trades_per_week": 5},
    },
    "aggressive": {
        "label": "Agressivo", "desc": "Posições maiores e mais trades; aceita mais oscilação e limites de perda mais largos.",
        "values": {"capital.per_op_pct": 35, "capital.max_entries_per_day": 5, "capital.max_open_positions": 6,
                   "risk.max_risk_per_trade_pct": 3.0, "risk.atr_stop_mult": 1.5, "risk.risk_reward": 2.0, "risk.min_score": 55,
                   "limits.daily_loss_pct": 3.0, "limits.weekly_loss_pct": 8.0, "limits.monthly_loss_pct": 15.0,
                   "forecast.trades_per_week": 7},
    },
    "very_aggressive": {
        "label": "Muito agressivo", "desc": "Alto risco: posições grandes, muitos trades, score mínimo baixo. Pode perder boa parte do capital.",
        "values": {"capital.per_op_pct": 60, "capital.max_entries_per_day": 8, "capital.max_open_positions": 8,
                   "risk.max_risk_per_trade_pct": 5.0, "risk.atr_stop_mult": 1.25, "risk.risk_reward": 2.0, "risk.min_score": 50,
                   "limits.daily_loss_pct": 5.0, "limits.weekly_loss_pct": 12.0, "limits.monthly_loss_pct": 25.0,
                   "forecast.trades_per_week": 10},
    },
}


# ------------------------------------------------------------------------------------------------
def coerce(d: ParamDef, raw: Any) -> Any:
    """Validate/convert a raw value for definition `d` (raises ValueError with a friendly message)."""
    try:
        if d.type == "bool":
            if isinstance(raw, bool):
                return raw
            if str(raw).strip().lower() in ("true", "1", "sim", "yes", "on"):
                return True
            if str(raw).strip().lower() in ("false", "0", "nao", "não", "no", "off"):
                return False
            raise ValueError("use true/false")
        if d.type in ("float", "int"):
            if isinstance(raw, bool):
                raise ValueError("número esperado")
            v = float(str(raw).replace(",", ".")) if isinstance(raw, str) else float(raw)
            if v != v or v in (float("inf"), float("-inf")):
                raise ValueError("número inválido")
            if d.type == "int":
                if abs(v - round(v)) > 1e-9:
                    raise ValueError("inteiro esperado")
                v = int(round(v))
            if d.min is not None and v < d.min:
                raise ValueError(f"mínimo {d.min}")
            if d.max is not None and v > d.max:
                raise ValueError(f"máximo {d.max}")
            return v
        if d.type == "choice":
            v = str(raw)
            if v not in (d.choices or []):
                raise ValueError(f"escolha entre {d.choices}")
            return v
        if d.type == "time":
            v = str(raw).strip()
            if not TIME_RE.match(v):
                raise ValueError("formato HH:MM")
            return v
        if d.type == "tickers":
            items = raw.replace(";", ",").split(",") if isinstance(raw, str) else list(raw)
            out, seen = [], set()
            for t in items:
                t = re.sub(r"\.SA$", "", str(t).strip().upper())
                if not t:
                    continue
                if not re.fullmatch(r"[A-Z]{4}\d{1,2}[A-Z]?", t):
                    raise ValueError(f"ticker inválido: {t}")
                if t not in seen:
                    seen.add(t)
                    out.append(t)
            if not out:
                raise ValueError("informe ao menos um ticker")
            if len(out) > 100:
                raise ValueError("máximo de 100 tickers")
            return out
        return str(raw)
    except ValueError as e:
        raise ValueError(f"{d.label} ({d.key}): {e}") from None
    except (TypeError, OverflowError):
        raise ValueError(f"{d.label} ({d.key}): valor inválido") from None


def cross_validate(values: Dict[str, Any]) -> List[str]:
    """Rules that involve several parameters. Returns a list of error messages."""
    g = lambda k: values[k]
    errs = []
    if g("risk.risk_reward") + 1e-9 < g("risk.min_rr"):
        errs.append("Alvo (R/R) não pode ser menor que o R/R mínimo aceito — todas as ordens seriam vetadas.")
    if g("capital.per_op_pct") > g("capital.max_exposure_pct") + 1e-9:
        errs.append("Orçamento por operação não pode exceder a exposição máxima.")
    if sum(g(k) for k in ("score.w_technical", "score.w_news", "score.w_ml", "score.w_volume")) <= 0:
        errs.append("Ao menos um peso do score composto deve ser maior que zero.")
    d, w, m = g("limits.daily_loss_pct"), g("limits.weekly_loss_pct"), g("limits.monthly_loss_pct")
    if d and w and d > w:
        errs.append("Limite diário de perda (%) não pode ser maior que o semanal.")
    if w and m and w > m:
        errs.append("Limite semanal de perda (%) não pode ser maior que o mensal.")
    if d and m and d > m:
        errs.append("Limite diário de perda (%) não pode ser maior que o mensal.")
    return errs


class RuntimeParams:
    def __init__(self):
        self._v: Dict[str, Any] = {}
        self.version = 0

    # --- reads (sync, usable from pure code) -------------------------------------------------
    def get(self, key: str) -> Any:
        d = BY_KEY[key]
        v = self._v.get(key, d.default)
        return list(v) if isinstance(v, list) else v

    def all_values(self) -> Dict[str, Any]:
        return {k: self.get(k) for k in BY_KEY}

    def universe(self) -> List[str]:
        return self.get("universe.tickers")

    def reset_cache(self):
        self._v = {}
        self.version += 1

    # --- DB sync -----------------------------------------------------------------------------
    async def refresh(self, db: AsyncSession) -> None:
        from app.models import AppSetting
        rows = (await db.execute(select(AppSetting))).scalars().all()
        new = {}
        for r in rows:
            d = BY_KEY.get(r.key)
            if d is None:
                continue
            try:
                new[r.key] = coerce(d, r.value)
            except ValueError:
                continue                    # ignore corrupt rows, fall back to default
        self._v = new
        self.version += 1

    async def update(self, db: AsyncSession, changes: Dict[str, Any], from_profile: Optional[str] = None) -> Dict[str, Any]:
        """Validate everything first, then persist atomically. Returns the applied (coerced) values."""
        from app.models import AppSetting
        errors, coerced = [], {}
        for k, raw in changes.items():
            d = BY_KEY.get(k)
            if d is None:
                errors.append(f"Parâmetro desconhecido: {k}")
                continue
            try:
                coerced[k] = coerce(d, raw)
            except ValueError as e:
                errors.append(str(e))
        if errors:
            raise ValueError("; ".join(errors))

        merged = self.all_values()
        merged.update(coerced)
        # a manual edit of a profile-managed knob turns the profile into "custom"
        if from_profile:
            coerced["risk.profile"] = merged["risk.profile"] = from_profile
        elif any(BY_KEY[k].profile_managed for k in coerced) and "risk.profile" not in coerced:
            coerced["risk.profile"] = merged["risk.profile"] = "custom"
        cross = cross_validate(merged)
        if cross:
            raise ValueError("; ".join(cross))

        for k, v in coerced.items():
            row = await db.get(AppSetting, k)
            if row:
                row.value = v
            else:
                db.add(AppSetting(key=k, value=v))
        await db.commit()
        self._v.update(coerced)
        self.version += 1
        return coerced

    async def apply_profile(self, db: AsyncSession, name: str) -> Dict[str, Any]:
        if name not in PROFILES:
            raise ValueError(f"Perfil desconhecido: {name}. Opções: {list(PROFILES)}")
        return await self.update(db, dict(PROFILES[name]["values"]), from_profile=name)

    async def reset(self, db: AsyncSession, keys: Optional[List[str]] = None) -> None:
        from app.models import AppSetting
        for r in (await db.execute(select(AppSetting))).scalars().all():
            if keys is None or r.key in keys:
                await db.delete(r)
        await db.commit()
        await self.refresh(db)

    # --- UI snapshot -------------------------------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        groups: Dict[str, List[dict]] = {}
        for d in DEFS:
            groups.setdefault(d.group, []).append({
                "key": d.key, "label": d.label, "desc": d.desc, "type": d.type, "unit": d.unit,
                "value": self.get(d.key), "default": d.default, "min": d.min, "max": d.max, "choices": d.choices,
                "profile_managed": d.profile_managed, "modified": self.get(d.key) != d.default})
        return {"groups": groups, "profile": self.get("risk.profile"),
                "profiles": {k: {"label": v["label"], "desc": v["desc"], "values": v["values"]} for k, v in PROFILES.items()}}


params = RuntimeParams()


def universe() -> List[str]:
    return params.universe()
