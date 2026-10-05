import numpy as np
import pandas as pd
import pytest

from app.ml.dataset import FEATURE_COLS, build_panel
from app.ml.predictor import predict_latest
from app.ml.trainer import train, walk_forward_splits
from tests.test_indicators import make_df


def universe(n=700, k=4):
    return {f"T{i}": make_df(n, seed=10 + i) for i in range(k)}


def test_walk_forward_no_overlap_and_purge():
    dates = pd.bdate_range("2024-01-01", periods=500).values
    splits = list(walk_forward_splits(dates, n_splits=4, purge_days=5))
    assert len(splits) == 4
    u = np.array(sorted(set(dates)))
    for tr, te in splits:
        assert tr.max() < te.min()
        gap = np.searchsorted(u, te.min()) - np.searchsorted(u, tr.max()) - 1
        assert gap >= 5                      # purge protects against 5-day forward targets
    for (_, a), (_, b) in zip(splits, splits[1:]):
        assert a.max() < b.min()             # test blocks are consecutive, not overlapping


def test_panel_targets_only_use_future_for_labels():
    data = universe(200, 2)
    p = build_panel(data)
    q = build_panel({k: v.iloc[:150] for k, v in data.items()})
    cols = FEATURE_COLS
    a = p[p.index <= q.index.max()].sort_values("ticker", kind="stable")
    b = q.sort_values("ticker", kind="stable")
    common = a.index.intersection(b.index)
    pd.testing.assert_frame_equal(a.loc[common, cols + ["ticker"]].reset_index().sort_values(["date", "ticker"]).reset_index(drop=True),
                                  b.loc[common, cols + ["ticker"]].reset_index().sort_values(["date", "ticker"]).reset_index(drop=True),
                                  check_exact=False, atol=1e-9)


@pytest.mark.parametrize("algo", ["lightgbm", "xgboost"])
def test_train_and_predict(tmp_path, algo):
    data = universe()
    res = train(data, "y_3d_2pct", algo, n_splits=3, model_dir=str(tmp_path), min_rows=300)
    m = res.metrics
    assert len(m["folds"]) >= 2
    assert 0 <= m["oos_calibrated"]["brier"] <= 1
    # random-walk data: no edge expected -> AUC should hover around 0.5, not be "too good"
    assert 0.40 <= m["oos_raw"]["auc"] <= 0.62
    preds = predict_latest(res.path, data)
    assert set(preds) == set(data)
    assert all(0 <= p["probability"] <= 1 for p in preds.values())


def test_train_requires_enough_data(tmp_path):
    with pytest.raises(ValueError):
        train({"A": make_df(120)}, "y_3d_2pct", "lightgbm", model_dir=str(tmp_path))
