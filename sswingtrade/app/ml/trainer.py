"""
Training with walk-forward validation, calibration and persistence - FASE 5.

Leakage control:
  * time-ordered expanding-window folds over unique DATES (all tickers of a date stay together)
  * `purge_days` rows removed between train end and test start (targets look 5 days ahead)
Output is a calibrated PROBABILITY (isotonic fitted on out-of-sample predictions), never a BUY/SELL.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import joblib
import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score

from app.ml.dataset import FEATURE_COLS, TARGETS, build_panel, clean_for_training, split_xy


def make_model(algo: str, seed: int = 42):
    if algo == "lightgbm":
        from lightgbm import LGBMClassifier
        return LGBMClassifier(n_estimators=250, learning_rate=0.03, num_leaves=15, max_depth=5,
                              min_child_samples=50, subsample=0.8, subsample_freq=1, colsample_bytree=0.8,
                              reg_lambda=5.0, random_state=seed, verbose=-1)
    if algo == "xgboost":
        from xgboost import XGBClassifier
        return XGBClassifier(n_estimators=250, learning_rate=0.03, max_depth=4, min_child_weight=10,
                             subsample=0.8, colsample_bytree=0.8, reg_lambda=5.0, random_state=seed,
                             eval_metric="logloss", n_jobs=2)
    raise ValueError("algo must be 'lightgbm' or 'xgboost'")


def walk_forward_splits(dates: np.ndarray, n_splits: int = 5, min_train_frac: float = 0.4, purge_days: int = 5):
    """Yield (train_dates, test_dates) arrays; expanding train, consecutive test blocks."""
    u = np.array(sorted(set(dates)))
    n = len(u)
    start = int(n * min_train_frac)
    edges = np.linspace(start, n, n_splits + 1).astype(int)
    for i in range(n_splits):
        ts, te = edges[i], edges[i + 1]
        tr_end = ts - purge_days
        if tr_end < 30 or te <= ts:
            continue
        yield u[:tr_end], u[ts:te]


def _metrics(y, p) -> dict:
    out = {"n": int(len(y)), "base_rate": round(float(np.mean(y)), 4)}
    if len(set(y)) > 1:
        out["auc"] = round(float(roc_auc_score(y, p)), 4)
    out["brier"] = round(float(brier_score_loss(y, p)), 4)
    out["logloss"] = round(float(log_loss(y, np.clip(p, 1e-6, 1 - 1e-6), labels=[0, 1])), 4)
    for thr in (0.5, 0.6):
        sel = p >= thr
        out[f"precision@{thr}"] = round(float(np.mean(y[sel])), 4) if sel.sum() >= 5 else None
        out[f"signals@{thr}"] = int(sel.sum())
    return out


@dataclass
class TrainResult:
    model_id: str
    target: str
    algo: str
    metrics: dict
    path: str


def train(data: Dict[str, pd.DataFrame], target: str = "y_3d_2pct", algo: str = "lightgbm",
          n_splits: int = 5, purge_days: int = 5, model_dir: str = "/app/models",
          min_rows: int = 600) -> TrainResult:
    if target not in TARGETS:
        raise ValueError(f"target must be one of {list(TARGETS)}")
    panel = clean_for_training(build_panel(data, with_targets=True), target)
    if len(panel) < min_rows:
        raise ValueError(f"Not enough labelled rows ({len(panel)} < {min_rows}). Sync more history first.")

    dates = panel.index.values
    oos_p, oos_y, folds = [], [], []
    for k, (tr_d, te_d) in enumerate(walk_forward_splits(dates, n_splits, purge_days=purge_days)):
        tr = panel[panel.index.isin(tr_d)]
        te = panel[panel.index.isin(te_d)]
        Xtr, ytr = split_xy(tr, target)
        Xte, yte = split_xy(te, target)
        if ytr.nunique() < 2 or len(te) == 0:
            continue
        m = make_model(algo).fit(Xtr, ytr)
        p = m.predict_proba(Xte)[:, 1]
        oos_p.append(p)
        oos_y.append(yte.values)
        folds.append({"fold": k, "train_end": str(pd.Timestamp(tr_d[-1]).date()),
                      "test_start": str(pd.Timestamp(te_d[0]).date()), "test_end": str(pd.Timestamp(te_d[-1]).date()),
                      **_metrics(yte.values, p)})
    if not folds:
        raise ValueError("No valid walk-forward folds (need more data or both classes present).")

    p_all, y_all = np.concatenate(oos_p), np.concatenate(oos_y)
    calibrator = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0).fit(p_all, y_all)
    oos_cal = calibrator.predict(p_all)

    X, y = split_xy(panel, target)
    final = make_model(algo).fit(X, y)
    importances = dict(sorted(zip(FEATURE_COLS, [float(v) for v in final.feature_importances_]),
                              key=lambda kv: -kv[1]))

    model_id = f"{target}_{algo}_{datetime.utcnow():%Y%m%d%H%M%S}"
    path = Path(model_dir)
    path.mkdir(parents=True, exist_ok=True)
    file = path / f"{model_id}.joblib"
    metrics = {
        "oos_raw": _metrics(y_all, p_all), "oos_calibrated": _metrics(y_all, oos_cal),
        "folds": folds, "feature_importance": importances,
        "train_rows": int(len(panel)), "tickers": sorted(panel["ticker"].unique().tolist()),
        "date_range": [str(panel.index.min().date()), str(panel.index.max().date())],
        "warning": "Calibrator fitted on OOS predictions; treat probabilities as estimates, not guarantees.",
    }
    joblib.dump({"model": final, "calibrator": calibrator, "features": FEATURE_COLS, "target": target,
                 "algo": algo, "metrics": metrics}, file)
    (path / f"{model_id}.json").write_text(json.dumps({"model_id": model_id, "target": target, "algo": algo,
                                                       "metrics": metrics}, indent=2))
    return TrainResult(model_id, target, algo, metrics, str(file))
