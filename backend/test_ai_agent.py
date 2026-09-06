"""Self-check for the pure logic in ai_agent (no network, no API key needed).

Run: python backend/test_ai_agent.py
"""
import pandas as pd

from ai_agent import _rsi, score


def test_rsi_bounds_and_direction():
    rising = pd.Series(range(1, 40))
    falling = pd.Series(range(40, 1, -1))
    assert _rsi(rising).iloc[-1] > 70, "steady uptrend should read overbought"
    assert _rsi(falling).iloc[-1] < 30, "steady downtrend should read oversold"


def test_score_ranks_bullish_above_bearish():
    bullish = {
        "above_sma20": True, "above_sma50": True, "golden_cross": True,
        "momentum_20d_pct": 8.0, "rel_strength_vs_nifty_pct": 3.0, "rsi14": 55.0,
    }
    bearish = {
        "above_sma20": False, "above_sma50": False, "golden_cross": False,
        "momentum_20d_pct": -6.0, "rel_strength_vs_nifty_pct": -4.0, "rsi14": 45.0,
    }
    assert score(bullish) > score(bearish)
    assert -100 <= score(bearish) <= 100 and -100 <= score(bullish) <= 100


def test_overbought_penalised():
    base = {
        "above_sma20": True, "above_sma50": True, "golden_cross": True,
        "momentum_20d_pct": 5.0, "rel_strength_vs_nifty_pct": 1.0, "rsi14": 55.0,
    }
    hot = {**base, "rsi14": 82.0}
    assert score(hot) < score(base), "RSI > 75 should dock the score"


if __name__ == "__main__":
    test_rsi_bounds_and_direction()
    test_score_ranks_bullish_above_bearish()
    test_overbought_penalised()
    print("ai_agent self-check passed")
