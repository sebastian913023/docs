import numpy as np
import pandas as pd
import pytest

from kalshi15 import backtest, features, model
from kalshi15.config import Config
from kalshi15.fees import expected_edge, kelly_fraction, taker_fee
from kalshi15.kalshi import _price
from kalshi15.signal import decide
from kalshi15.synth import make_bars

CFG = Config()


def test_fee_formula():
    assert taker_fee(0.50) == 0.02          # 0.07*.25=0.0175 -> rounds up to $0.02
    assert taker_fee(0.99) == 0.01
    assert taker_fee(0.10) == 0.01


def test_kelly_and_edge():
    assert kelly_fraction(0.5, 0.5) == 0.0
    assert kelly_fraction(0.7, 0.5) > 0
    assert expected_edge(0.6, 0.5) == pytest.approx(0.6 - 0.5 - 0.02)


def test_no_lookahead():
    """Features/labels for window i must not change when bars after the decision time change,
    except the label (which legitimately uses the settlement bar)."""
    bars = make_bars(60 * 24 * 5, seed=5)
    full = features.window_table(bars, 5)
    row = full.index[100]
    cut = bars.copy()
    after = cut.index > row + pd.Timedelta(minutes=5)  # everything after the decision minute
    cut.loc[after, ["open", "high", "low", "close"]] *= 1.5
    alt = features.window_table(cut, 5)
    cols = features.FEATURES + ["p0"]
    np.testing.assert_allclose(full.loc[row, cols].astype(float), alt.loc[row, cols].astype(float))


def test_label_rule_ties_go_yes():
    bars = make_bars(60 * 24 * 3, seed=2)
    t = features.window_table(bars, 5)
    r = t.index[10]
    ref = bars.loc[r - pd.Timedelta(minutes=1), "close"]
    fin = bars.loc[r + pd.Timedelta(minutes=14), "close"]
    assert t.loc[r, "y"] == int(fin >= ref)


def test_random_walk_has_no_edge():
    bars = make_bars(60 * 24 * 90, seed=11, signal_strength=0.0)
    t = features.window_table(bars, 5)
    p = model.walk_forward(t, 2000, 96)
    rep = backtest.run(t, p, CFG)
    assert rep["trading"]["verdict"] != "EDGE_SUPPORTED"


def test_planted_signal_detected():
    bars = make_bars(60 * 24 * 90, seed=3, signal_strength=0.15)
    t = features.window_table(bars, 5)
    p = model.walk_forward(t, 2000, 96)
    rep = backtest.run(t, p, CFG)
    assert rep["trading"]["verdict"] == "EDGE_SUPPORTED"
    assert rep["probability"]["brier_model"] < rep["probability"]["brier_random_walk"]


def test_walk_forward_is_out_of_sample():
    bars = make_bars(60 * 24 * 30, seed=4)
    t = features.window_table(bars, 5)
    p = model.walk_forward(t, 1000, 96)
    assert p.iloc[:1000].isna().all() and p.iloc[1000:].notna().all()


def test_decide_rules():
    s = decide("BTC", "t", 5, 0.70, 0.55, 0.47, CFG)
    assert s.action == "UP" and 0 < s.stake_frac <= CFG.max_stake_frac
    s = decide("BTC", "t", 5, 0.30, 0.55, 0.47, CFG)
    assert s.action == "DOWN"
    assert decide("BTC", "t", 5, 0.52, 0.51, 0.51, CFG).action == "NO-TRADE"
    assert decide("BTC", "t", 1, 0.9, 0.5, 0.5, CFG).action == "WAIT"
    assert decide("BTC", "t", 5, 0.9, None, None, CFG).action == "NO-TRADE"


def test_price_parsing_both_formats():
    assert _price({"yes_ask": 55}, "yes_ask") == 0.55
    assert _price({"yes_ask_dollars": "0.5500"}, "yes_ask") == 0.55
    assert _price({}, "yes_ask") is None
