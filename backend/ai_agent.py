"""AI analyst agent: turns a stock's recent price action into a plain-English view.

One Gemini call per request. Technical indicators are computed here (pandas only,
no extra deps) so the model reasons over a handful of numbers instead of raw
candles. Consumed by the /api/ai/* routes in app.py.

Env:
  GEMINI_API_KEY  - required for the LLM call (set in Vercel project settings)
  AI_MODEL        - optional, defaults to gemini-flash-latest
"""
import json
import math
import os
import time

import yfinance as yf
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

AI_MODEL = os.environ.get("AI_MODEL", "gemini-flash-latest")
DISCLAIMER = "Educational simulation only. Not investment advice."

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = genai.Client()  # reads GEMINI_API_KEY (or GOOGLE_API_KEY)
    return _client


def _rsi(series, window=14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(window).mean()
    loss = (-delta.clip(upper=0)).rolling(window).mean()
    rs = gain / loss
    return 100 - 100 / (1 + rs)


def _series(ticker, period="6mo"):
    df = yf.download(ticker, period=period, interval="1d", auto_adjust=True, progress=False)
    if df.empty:
        raise ValueError(f"No price history for {ticker}")
    return df["Close"].squeeze()


def indicators(ticker):
    """~6 months of daily bars -> a dict of signals, plus the NIFTY-50 comparison."""
    close = _series(ticker)
    if len(close) < 55:
        raise ValueError(f"Not enough price history for {ticker}")
    nifty = _series("^NSEI")

    last = float(close.iloc[-1])
    sma20 = float(close.rolling(20).mean().iloc[-1])
    sma50 = float(close.rolling(50).mean().iloc[-1])
    rsi14 = float(_rsi(close).iloc[-1])
    mom5 = float(close.pct_change(5).iloc[-1] * 100)
    mom20 = float(close.pct_change(20).iloc[-1] * 100)
    vol10 = float(close.pct_change().rolling(10).std().iloc[-1] * 100)
    hi = float(close.max())
    lo = float(close.min())
    range_pos = (last - lo) / (hi - lo) * 100 if hi > lo else 50.0
    rel_strength = float((close.pct_change(20).iloc[-1] - nifty.pct_change(20).iloc[-1]) * 100)

    ind = {
        "ticker": ticker,
        "price": round(last, 2),
        "sma20": round(sma20, 2),
        "sma50": round(sma50, 2),
        "above_sma20": last > sma20,
        "above_sma50": last > sma50,
        "golden_cross": sma20 > sma50,
        "rsi14": round(rsi14, 1),
        "momentum_5d_pct": round(mom5, 2),
        "momentum_20d_pct": round(mom20, 2),
        "volatility_10d_pct": round(vol10, 2),
        "range_position_pct": round(range_pos, 1),
        "rel_strength_vs_nifty_pct": round(rel_strength, 2),
    }
    if any(isinstance(v, float) and math.isnan(v) for v in ind.values()):
        raise ValueError(f"Indicator computation produced NaN for {ticker}")
    return ind


def score(ind):
    """Cheap composite score in [-100, 100] for ranking without an LLM call."""
    s = 0
    s += 20 if ind["above_sma20"] else -10
    s += 20 if ind["above_sma50"] else -10
    s += 15 if ind["golden_cross"] else -15
    s += max(-20, min(20, ind["momentum_20d_pct"]))
    s += max(-15, min(15, ind["rel_strength_vs_nifty_pct"]))
    r = ind["rsi14"]
    if r > 75:
        s -= 15
    elif r < 30:
        s += 10
    return max(-100, min(100, round(s)))


def analyze(ticker):
    """Indicators + one Gemini call -> {verdict, confidence, rationale, risks, ...}."""
    ind = indicators(ticker)
    prompt = (
        "You are a cautious equity analyst. Below are technical indicators for "
        f"{ticker} on the NSE. Give a short, balanced read for a beginner "
        "paper-trader.\n\n"
        f"{json.dumps(ind, indent=2)}\n\n"
        "Return a JSON object with keys:\n"
        '  verdict: one of "BUY", "HOLD", "SELL"\n'
        '  confidence: one of "low", "medium", "high"\n'
        "  rationale: at most 2 sentences\n"
        "  risks: at most 2 sentences"
    )
    cfg = types.GenerateContentConfig(response_mime_type="application/json")
    # Gemini's free tier throws transient 503s under load - retry a few times.
    for attempt in range(3):
        try:
            resp = _get_client().models.generate_content(model=AI_MODEL, contents=prompt, config=cfg)
            break
        except genai_errors.ServerError:
            if attempt == 2:
                raise
            time.sleep(1.5 * (attempt + 1))
    view = json.loads(resp.text)
    view["indicators"] = ind
    view["score"] = score(ind)
    view["disclaimer"] = DISCLAIMER
    return view


if __name__ == "__main__":
    print(json.dumps(analyze("RELIANCE.NS"), indent=2))
