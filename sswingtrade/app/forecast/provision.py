"""
Earnings PROVISION (forecast) - pure functions, no I/O, no LLM at call time.

NOT a promise of results. It is a Monte-Carlo scenario analysis whose single "AI" input is the
estimated win probability of a trade (edge), derived from the evidence the system already produced:
  * ML model out-of-sample AUC / calibrated probabilities of today's top candidates (lift over base rate)
  * news sentiment of those candidates (small, bounded adjustment)
  * paper-trading track record and backtests (Bayesian blend)
Without a validated model the edge falls back to the break-even probability, so expected value is
NEGATIVE after costs - the forecast never invents profit.
"""

from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

DISCLAIMER = ("Projeção hipotética baseada em simulação (Monte Carlo) e nas estimativas dos modelos; "
              "NÃO é garantia nem promessa de resultado. Retornos passados e backtests não garantem o futuro; "
              "custos, IR, feriados e gaps podem piorar o resultado. Prejuízo de capital é possível.")


# ----------------------------------------------------------------------------- edge from AI evidence
@dataclass
class EdgeEvidence:
    ml_auc: Optional[float] = None            # out-of-sample AUC of the active model
    ml_base_rate: Optional[float] = None      # OOS positive rate of the model's target
    ml_top_prob: Optional[float] = None       # mean calibrated prob. of today's top-k candidates
    news_signal: Optional[float] = None       # -1..1 mean news sentiment*impact of those candidates
    paper_wins: int = 0
    paper_trades: int = 0
    backtest_win_rate: Optional[float] = None  # 0..1
    backtest_trades: int = 0


@dataclass
class EdgeEstimate:
    p_win: float
    p_breakeven: float
    p_model: float
    lift: Optional[float]
    shrink: float
    sources: List[str]
    concentration: float                      # Beta concentration (higher = more certain)
    notes: List[str] = field(default_factory=list)


def estimate_edge(ev: EdgeEvidence, risk_reward: float, ml_shrink_max: float = 0.6, evidence_k: float = 30.0,
                  p_cap: float = 0.75) -> EdgeEstimate:
    p0 = 1.0 / (1.0 + risk_reward)           # win rate at which E[R] == 0 (before costs)
    p, lift, shrink, src, notes = p0, None, 0.0, [], []
    if ev.ml_auc is not None and ev.ml_base_rate and ev.ml_top_prob is not None and ev.ml_base_rate > 0:
        lift = float(np.clip(ev.ml_top_prob / ev.ml_base_rate, 0.5, 3.0))
        shrink = float(np.clip((ev.ml_auc - 0.5) * 10.0, 0.0, 1.0)) * ml_shrink_max
        p = p0 * (1.0 + shrink * (lift - 1.0))
        src.append(f"ML (AUC {ev.ml_auc:.2f}, lift {lift:.2f}x, confiança {shrink:.0%})")
        if ev.ml_auc < 0.52:
            notes.append("AUC do modelo próximo de 0.5: sem vantagem estatística comprovada.")
    else:
        notes.append("Sem modelo de ML ativo/validado: assumindo apenas o ponto de equilíbrio (EV negativo após custos).")
    if ev.news_signal is not None:
        p += float(np.clip(ev.news_signal, -1, 1)) * 0.03
        src.append("notícias (ajuste ≤ ±3 p.p.)")
    p_model = float(np.clip(p, 0.05, p_cap))

    p_final, k = p_model, evidence_k
    if ev.backtest_win_rate is not None and ev.backtest_trades > 0:
        w = 0.5 * ev.backtest_trades
        p_final = (p_final * k + ev.backtest_win_rate * w) / (k + w)
        k += w
        src.append(f"backtests ({ev.backtest_trades} trades, peso 0.5)")
    if ev.paper_trades > 0:
        p_final = (p_final * k + ev.paper_wins) / (k + ev.paper_trades)
        k += ev.paper_trades
        src.append(f"paper trading ({ev.paper_trades} trades fechados)")
    p_final = float(np.clip(p_final, 0.05, p_cap))
    return EdgeEstimate(round(p_final, 4), round(p0, 4), round(p_model, 4), lift, round(shrink, 3), src, float(k), notes)


# ----------------------------------------------------------------------------- simulation
@dataclass
class SimConfig:
    start_equity: float
    weeks: int = 26
    trades_per_week: float = 5
    per_op_pct: float = 20.0
    max_exposure_pct: float = 100.0
    risk_per_trade_pct: float = 2.0
    atr_stop_mult: float = 1.5
    risk_reward: float = 2.0
    atr_pct: float = 2.0                      # typical ATR as % of price
    hold_days: float = 3.0                    # average holding (for concurrent-exposure scaling)
    fee_rate: float = 0.000325
    slippage_bps: float = 5.0
    brokerage: float = 0.0
    weekly_deposit: float = 0.0
    monthly_deposit: float = 0.0
    planned_deposits: Sequence[Tuple[int, float]] = ()    # (week index 1..N, amount)
    daily_loss_pct: float = 0.0               # 0 = off
    weekly_loss_pct: float = 0.0
    monthly_loss_pct: float = 0.0
    daily_loss_amount: float = 0.0
    weekly_loss_amount: float = 0.0
    monthly_loss_amount: float = 0.0
    simulations: int = 2000
    seed: Optional[int] = 42


def trade_geometry(c: SimConfig) -> Dict[str, float]:
    s = c.atr_stop_mult * c.atr_pct / 100.0                    # stop distance (fraction of price)
    pos = min(c.per_op_pct / 100.0, (c.risk_per_trade_pct / 100.0) / s if s > 0 else 1.0)
    concurrent = max(1.0, c.trades_per_week * c.hold_days / 5.0)
    pos = min(pos, c.max_exposure_pct / 100.0 / concurrent)    # simultaneous exposure cap
    cost = 2 * (c.fee_rate + c.slippage_bps / 1e4)             # round trip, fraction of notional
    return {"stop_frac": s, "pos_frac": pos, "cost_frac": cost,
            "win_ret": c.risk_reward * s - cost, "loss_ret": -(s + cost)}


def expected_trade_return(p_win: float, c: SimConfig) -> float:
    g = trade_geometry(c)
    return p_win * g["win_ret"] + (1 - p_win) * g["loss_ret"]


def _limit(pct: float, amt: float, base: np.ndarray) -> np.ndarray:
    """R$ allowed loss per sim (inf when off): min of pct*base and fixed amount."""
    lim = np.full_like(base, np.inf)
    if pct > 0:
        lim = np.minimum(lim, base * pct / 100.0)
    if amt > 0:
        lim = np.minimum(lim, amt)
    return lim


def simulate(c: SimConfig, p_win: float, concentration: float = 30.0) -> Dict:
    rng = np.random.default_rng(c.seed)
    n, g = c.simulations, trade_geometry(c)
    # parameter uncertainty: each path has its own true win probability ~ Beta
    a, b = max(p_win * concentration, 1e-3), max((1 - p_win) * concentration, 1e-3)
    p_path = rng.beta(a, b, size=n)

    eq = np.full(n, float(c.start_equity))
    curve = [eq.copy()]
    deposits_paid = np.full(n, float(c.start_equity))
    peak, max_dd = eq.copy(), np.zeros(n)
    hit_d = np.zeros(n); hit_w = np.zeros(n); hit_m = np.zeros(n)
    planned = {}
    for w, amt in c.planned_deposits:
        planned[int(w)] = planned.get(int(w), 0.0) + float(amt)
    tpw_int = int(np.floor(c.trades_per_week)); frac = c.trades_per_week - tpw_int
    month_pnl = np.zeros(n); month_base = eq.copy(); cur_month = 0

    for w in range(1, c.weeks + 1):
        dep = c.weekly_deposit + planned.get(w, 0.0)
        month_idx = int((w - 1) * 12 / 52)
        if month_idx != cur_month:
            cur_month, month_pnl, month_base = month_idx, np.zeros(n), eq.copy()
            dep += c.monthly_deposit
        eq = eq + dep
        deposits_paid += dep
        week_pnl = np.zeros(n); week_base = eq.copy()
        n_tr = tpw_int + (1 if rng.random() < frac else 0)
        day_pnls, day_bases = {}, {}
        for i in range(n_tr):
            day = i % 5                                      # trades are spread over the 5 sessions of the week
            if day not in day_pnls:
                day_pnls[day], day_bases[day] = np.zeros(n), eq.copy()
            day_pnl, day_base = day_pnls[day], day_bases[day]
            d_lim = _limit(c.daily_loss_pct, c.daily_loss_amount, day_base)
            w_lim = _limit(c.weekly_loss_pct, c.weekly_loss_amount, week_base)
            m_lim = _limit(c.monthly_loss_pct, c.monthly_loss_amount, month_base)
            rem = np.minimum.reduce([d_lim + day_pnl, w_lim + week_pnl, m_lim + month_pnl])  # R$ still lossable
            blocked = rem <= 1e-9
            hit_d += (blocked & np.isfinite(d_lim) & (d_lim + day_pnl <= 1e-9))
            hit_w += (blocked & np.isfinite(w_lim) & (w_lim + week_pnl <= 1e-9))
            hit_m += (blocked & np.isfinite(m_lim) & (m_lim + month_pnl <= 1e-9))
            pos_val = eq * g["pos_frac"]
            worst = pos_val * (-g["loss_ret"])                       # R$ lost if the stop is hit
            scale = np.where(np.isfinite(rem), np.clip(rem / np.maximum(worst, 1e-9), 0.0, 1.0), 1.0)
            scale = np.where(blocked, 0.0, scale)
            win = rng.random(n) < p_path
            pnl = pos_val * scale * np.where(win, g["win_ret"], g["loss_ret"]) - c.brokerage * 2 * (scale > 0)
            eq = eq + pnl
            day_pnl += pnl; week_pnl += pnl; month_pnl += pnl
            peak = np.maximum(peak, eq)
            max_dd = np.maximum(max_dd, 1 - eq / np.maximum(peak, 1e-9))
        curve.append(eq.copy())

    curve = np.array(curve)                                          # (weeks+1, n)
    q = lambda arr, p: np.percentile(arr, p, axis=-1)
    final, paid = curve[-1], deposits_paid
    prof = final - paid
    fan = {f"p{p}": [round(float(v), 2) for v in q(curve, p)] for p in (10, 25, 50, 75, 90)}
    return {
        "fan": fan,
        "final": {f"p{p}": round(float(np.percentile(final, p)), 2) for p in (10, 25, 50, 75, 90)},
        "profit": {f"p{p}": round(float(np.percentile(prof, p)), 2) for p in (10, 25, 50, 75, 90)},
        "total_deposited": round(float(paid.mean()), 2),
        "expected_profit": round(float(prof.mean()), 2),
        "prob_loss": round(float((prof < 0).mean()), 4),
        "prob_drawdown_20": round(float((max_dd > 0.20).mean()), 4),
        "max_drawdown_p50": round(float(np.percentile(max_dd, 50)), 4),
        "max_drawdown_p90": round(float(np.percentile(max_dd, 90)), 4),
        "limit_hit_paths_pct": {"day": round(float((hit_d > 0).mean()), 4), "week": round(float((hit_w > 0).mean()), 4),
                                "month": round(float((hit_m > 0).mean()), 4)},
        "geometry": {k: round(v, 5) for k, v in g.items()},
    }


def scenarios(c: SimConfig, edge: EdgeEstimate) -> Dict[str, Dict]:
    """Pessimistic / base / optimistic = deterministic compounding at three win rates (+ the MC distribution)."""
    out = {}
    spread = 0.5 * float(np.sqrt(edge.p_win * (1 - edge.p_win) / max(edge.concentration, 1.0))) * 2
    for name, p in (("pessimista", max(0.05, edge.p_win - spread)), ("base", edge.p_win),
                    ("otimista", min(0.80, edge.p_win + spread))):
        ev = expected_trade_return(p, c)
        eq, paid = c.start_equity, c.start_equity
        g = trade_geometry(c)
        monthly = {}
        planned = {}
        for w_, a_ in c.planned_deposits:
            planned[int(w_)] = planned.get(int(w_), 0.0) + float(a_)
        cur_m = 0
        for w in range(1, c.weeks + 1):
            dep = c.weekly_deposit + planned.get(w, 0.0)
            m = int((w - 1) * 12 / 52)
            if m != cur_m:
                cur_m = m
                dep += c.monthly_deposit
            eq += dep; paid += dep
            eq *= 1 + c.trades_per_week * g["pos_frac"] * ev
        out[name] = {"p_win": round(p, 4), "ev_per_trade_pct": round(ev * 100, 3),
                     "ev_per_trade_pct_of_equity": round(ev * g["pos_frac"] * 100, 3),
                     "final_equity": round(eq, 2), "total_deposited": round(paid, 2), "profit": round(eq - paid, 2)}
    return out


def build_forecast(c: SimConfig, edge: EdgeEstimate) -> Dict:
    mc = simulate(c, edge.p_win, edge.concentration)
    return {"edge": {"p_win": edge.p_win, "p_breakeven": edge.p_breakeven, "p_model": edge.p_model, "lift": edge.lift,
                     "ml_confidence": edge.shrink, "sources": edge.sources, "notes": edge.notes,
                     "ev_per_trade_pct": round(expected_trade_return(edge.p_win, c) * 100, 3)},
            "scenarios": scenarios(c, edge), "monte_carlo": mc, "weeks": c.weeks,
            "assumptions": {"start_equity": c.start_equity, "trades_per_week": c.trades_per_week,
                            "per_op_pct": c.per_op_pct, "risk_per_trade_pct": c.risk_per_trade_pct,
                            "atr_stop_mult": c.atr_stop_mult, "risk_reward": c.risk_reward, "atr_pct": c.atr_pct,
                            "weekly_deposit": c.weekly_deposit, "monthly_deposit": c.monthly_deposit,
                            "loss_limits_pct": {"day": c.daily_loss_pct, "week": c.weekly_loss_pct, "month": c.monthly_loss_pct},
                            "simulations": c.simulations},
            "disclaimer": DISCLAIMER}
