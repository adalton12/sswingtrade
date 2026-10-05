"""Machine learning routes - FASE 5 (probabilities only, never BUY/SELL)."""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

import asyncio

from app.config import settings
from app.database import get_db
from app.ml.dataset import TARGETS
from app.ml.predictor import predict_latest
from app.ml.trainer import train
from app.models import MLModel
from app.services.indicator_service import load_candles_df
from app.services.market_data_collector import DEFAULT_TICKERS

router = APIRouter()


class TrainRequest(BaseModel):
    target: str = "y_3d_2pct"
    algo: str = Field("lightgbm", pattern="^(lightgbm|xgboost)$")
    tickers: Optional[List[str]] = None
    n_splits: int = Field(5, ge=2, le=10)
    purge_days: int = Field(5, ge=1, le=20)
    activate: bool = True


async def _load(tickers: List[str]) -> dict:
    out = {}
    for t in tickers:
        df = await load_candles_df(t.upper(), limit=3000)
        if len(df) >= 80:
            out[t.upper()] = df
    return out


@router.get("/targets")
async def targets() -> dict:
    return {"targets": TARGETS}


@router.post("/train")
async def train_model(req: TrainRequest, db: AsyncSession = Depends(get_db)) -> dict:
    if req.target not in TARGETS:
        raise HTTPException(400, f"target must be one of {list(TARGETS)}")
    data = await _load(req.tickers or DEFAULT_TICKERS)
    if not data:
        raise HTTPException(404, "No candles. Run POST /api/v1/market/sync/daily with days_back >= 500 first.")
    try:
        # CPU-bound: keep the event loop free
        res = await asyncio.to_thread(train, data, req.target, req.algo, req.n_splits, req.purge_days,
                                      settings.ML_MODEL_PATH, settings.ML_MIN_TRAIN_ROWS)
    except ValueError as e:
        raise HTTPException(422, str(e))

    if req.activate:
        await db.execute(update(MLModel).where(MLModel.target == req.target).values(is_active=False))
    db.add(MLModel(model_id=res.model_id, target=res.target, algo=res.algo, path=res.path,
                   metrics=res.metrics, is_active=req.activate))
    await db.commit()
    m = res.metrics
    return {"model_id": res.model_id, "active": req.activate, "oos_calibrated": m["oos_calibrated"],
            "oos_raw": m["oos_raw"], "folds": len(m["folds"]), "train_rows": m["train_rows"],
            "top_features": list(m["feature_importance"])[:5]}


@router.get("/models")
async def list_models(db: AsyncSession = Depends(get_db)) -> dict:
    rows = (await db.execute(select(MLModel).order_by(MLModel.trained_at.desc()).limit(50))).scalars().all()
    return {"models": [{"model_id": r.model_id, "target": r.target, "algo": r.algo, "active": r.is_active,
                        "auc": (r.metrics or {}).get("oos_calibrated", {}).get("auc"),
                        "trained_at": r.trained_at.isoformat()} for r in rows]}


@router.post("/models/{model_id}/activate")
async def activate(model_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    m = (await db.execute(select(MLModel).where(MLModel.model_id == model_id))).scalar_one_or_none()
    if not m:
        raise HTTPException(404, "Model not found")
    await db.execute(update(MLModel).where(MLModel.target == m.target).values(is_active=False))
    m.is_active = True
    await db.commit()
    return {"model_id": model_id, "active": True}


async def _active(db, target):
    m = (await db.execute(select(MLModel).where(MLModel.target == target, MLModel.is_active == True)
                          .order_by(MLModel.trained_at.desc()))).scalars().first()
    if not m:
        raise HTTPException(404, f"No active model for {target}. Run POST /api/v1/ml/train first.")
    return m


@router.get("/predict")
async def predict_all(target: str = Query("y_3d_2pct"), limit: int = Query(10, ge=1, le=50),
                      db: AsyncSession = Depends(get_db)) -> dict:
    """Tickers ranked by calibrated probability for the latest candle."""
    m = await _active(db, target)
    preds = predict_latest(m.path, await _load(DEFAULT_TICKERS))
    rank = sorted(({"ticker": t, **p} for t, p in preds.items()), key=lambda r: -r["probability"])
    return {"model_id": m.model_id, "target": target, "description": TARGETS[target], "ranking": rank[:limit]}


@router.get("/predict/{ticker}")
async def predict_one(ticker: str, target: str = Query("y_3d_2pct"), db: AsyncSession = Depends(get_db)) -> dict:
    m = await _active(db, target)
    preds = predict_latest(m.path, await _load([ticker]))
    if ticker.upper() not in preds:
        raise HTTPException(404, f"Not enough data to predict {ticker}")
    return {"model_id": m.model_id, "description": TARGETS[target], **preds[ticker.upper()]}
