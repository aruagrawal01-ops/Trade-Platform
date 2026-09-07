# StoqNest — AI Agents & Automation

Reference for every AI- and automation-driven feature in the platform: what
problem each one solves, how it works, its inputs/outputs, and its limits.

- **Stack:** Flask API (`backend/`), single-file HTML/JS frontend (`frontend/index.html`),
  Postgres, deployed as a Vercel serverless function.
- **Key constraint:** Vercel's serverless plan has **no background workers**. Nothing
  runs unless an HTTP request triggers it. Every "automation" below is therefore
  *polling-driven* — it executes while a logged-in user has the app open (the
  frontend calls `/api/dashboard` every 10 seconds: `setInterval(refreshData, 10000)`).

---

## 1. AI Stock Analyst  *(AI agent)*

**Files:** `backend/ai_agent.py`, route in `backend/app.py`, UI in `frontend/index.html`

### Problem statement

A beginner paper-trader sees a price and a candlestick chart but has no way to
judge whether a stock is technically strong or weak. Reading RSI, moving-average
crosses, momentum, and market-relative strength by hand takes experience they
don't have yet. They need a plain-English second opinion grounded in real
indicators — not a black-box "buy" button.

### What it does

On demand (one stock at a time), it:

1. Downloads ~6 months of daily closes for the stock **and** the NIFTY-50 index.
2. Computes ~10 technical indicators + a deterministic score (no AI).
3. Sends those numbers to Google Gemini with an analyst prompt.
4. Returns a `BUY` / `HOLD` / `SELL` verdict with a 2-sentence rationale and a
   2-sentence risk note.

The LLM only reasons over the computed numbers — it never sees raw price data —
which keeps the call cheap (~250 tokens in, ~150 out) and the output auditable.

### Inputs (computed in `indicators()`)

| Parameter | Meaning |
|---|---|
| `price` | latest closing price |
| `sma20`, `sma50` | 20- and 50-day simple moving averages |
| `above_sma20`, `above_sma50` | is price above each average (trend direction) |
| `golden_cross` | is `sma20` above `sma50` (bullish structure) |
| `rsi14` | 14-day Relative Strength Index (>70 overbought, <30 oversold) |
| `momentum_5d_pct`, `momentum_20d_pct` | % price change over 5 / 20 trading days |
| `volatility_10d_pct` | std-dev of daily returns over 10 days |
| `range_position_pct` | where price sits in its 6-month low→high band (0–100) |
| `rel_strength_vs_nifty_pct` | stock's 20-day return minus NIFTY's 20-day return |

**`score`** (`score()`): weighted sum of the above, clamped to −100…+100. Trend
flags ±20 each, golden cross ±15, momentum capped ±20, relative strength capped
±15, RSI penalty if >75 / bonus if <30. Used for cheap ranking without an LLM call.

### LLM call (`_gemini()`)

| Setting | Value |
|---|---|
| Provider / model | Google Gemini, `gemini-flash-latest` (override with `AI_MODEL` env var) |
| Transport | REST `POST v1beta/models/{model}:generateContent` via `requests` |
| Response format | `responseMimeType: application/json` (guarantees parseable JSON) |
| Reliability | 3 attempts, 1.5s→3s backoff on HTTP 503 ("high demand" — common on the free tier) |
| Timeout | 30s per attempt |
| Auth | `GEMINI_API_KEY` env var (`x-goog-api-key` header) |

### Output (`analyze()` → JSON)

```json
{
  "verdict": "HOLD",            // BUY | HOLD | SELL          (Gemini)
  "confidence": "medium",       // low | medium | high        (Gemini)
  "rationale": "…2 sentences…", //                            (Gemini)
  "risks": "…2 sentences…",     //                            (Gemini)
  "indicators": { … },          // the full input table
  "score": 56,                  // deterministic score
  "disclaimer": "Educational simulation only. Not investment advice."
}
```

### API

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/api/ai/analyze/<ticker>` | JWT required | `<ticker>` is an NSE symbol, e.g. `RELIANCE.NS` |

Responses: `200` verdict JSON · `400` bad ticker / insufficient history ·
`502` Gemini/network failure (check `GEMINI_API_KEY`) · `503` agent module
failed to import.

Login is required so anonymous traffic can't run up the API bill.

### Limitations

- **Technical signals only.** No fundamentals, news, earnings, or order-book data.
- **Not investment advice.** It's a study aid on a paper-trading simulator.
- **Point-in-time.** Re-run it for a fresh read; nothing is stored or scheduled.
- Needs ≥ ~55 trading days of history, or it returns `400`.

---

## 2. Price Alerts  *(automation)*

**Files:** `PriceAlert` model, `check_price_alerts()` + `/api/alerts*` routes in `backend/app.py`

### Problem statement

A trader wants to act when a stock hits a specific price, but can't stare at 50
tickers all day. They need the platform to watch a price threshold for them and
notify them the moment it's crossed.

### What it does

The user defines a rule: *ticker + direction (`above` / `below`) + target price*.
On every `/api/dashboard` call, `check_price_alerts(user_id)` compares each of
that user's un-triggered alerts against the live price cache. Any alert whose
condition is met is marked `triggered = True` and returned in the dashboard
response; the frontend pops a toast ("🔔 RELIANCE rose above ₹1400").

### Data model — `PriceAlert`

`user_id`, `ticker`, `target_price`, `direction` (`above`/`below`),
`triggered` (bool), `created_at`.

### API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/alerts` | list the user's alerts |
| POST | `/api/alerts` | create one (`ticker`, `direction`, `target_price`) |
| DELETE | `/api/alerts/<id>` | remove one |

### Limitations

- **Fires only while the app is open.** No open tab → no dashboard poll → no check.
- One-shot: once `triggered`, it stays triggered (delete and recreate to reuse).
- Resolution is the 10-second poll interval, and prices are ~1-day-delayed
  Yahoo Finance data, not a real-time feed.

---

## 3. Auto Orders  *(automation)*

**Files:** `AutoOrder` model, `check_auto_orders()` + `execute_trade()` + `/api/auto-orders*` routes in `backend/app.py`

### Problem statement

Alerts still require the user to manually place the trade after the notification —
by which time the price may have moved. A trader wants a rule that *executes* the
buy or sell automatically when the trigger price is hit, so a plan survives even
if they're not watching closely.

### What it does

The user defines: *ticker + direction + target price + action (`BUY`/`SELL`) +
share count*. On every `/api/dashboard` call, `check_auto_orders(user)` checks
each `active` order against the live price cache. On a hit it calls the shared
`execute_trade()` logic (same path as a manual trade — balance check, share
check, `Transaction` row), then sets the order's `status` to `executed` or
`failed` and records `executed_price` / `executed_at`. Results are returned in
the dashboard response and shown as a toast.

### Data model — `AutoOrder`

`user_id`, `ticker`, `target_price`, `direction` (`above`/`below`),
`action` (`BUY`/`SELL`), `shares`, `status`
(`active` → `executed` / `failed` / `cancelled`), `executed_price`,
`executed_at`, `created_at`.

### API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/auto-orders` | list the user's orders |
| POST | `/api/auto-orders` | create one (`ticker`, `direction`, `action`, `target_price`, `shares`) |
| DELETE | `/api/auto-orders/<id>` | cancel an `active` order |

### Limitations

- **Fires only while the app is open** (same polling constraint as alerts).
- Executes at the *current cached price* when the check runs, which may differ
  from the exact target (gap-throughs, poll granularity).
- `failed` if funds/shares are insufficient at execution time — it does not retry.

---

## 4. Live Price Cache  *(supporting automation)*

**Files:** `PRICE_CACHE`, `fetch_live_prices()` in `backend/app.py`

### Problem statement

Calling Yahoo Finance per-ticker on every trade makes trades slow and flaky, and
different endpoints could see different prices for the same stock in one request.

### What it does

A module-level dict, refreshed by one bulk `yfinance` download whenever
`/api/dashboard` or `/api/leaderboard` runs. Trades, alerts, and auto-orders all
read from it, so execution is instant and every feature sees a consistent price.
Fallback chain per ticker: bulk download → `yf.Ticker().fast_info` → last known
cached value.

### Limitations

- In-memory and per-instance: a cold serverless instance starts with an empty
  cache until the first dashboard call populates it.
- As fresh as the last dashboard poll (≤ 10s old for an active user), on top of
  Yahoo's own delay.

---

## 5. Predictive Model (XGBoost) — *trained, not yet integrated*

**Files:** separate project, `../stock_ai/` (`src/training/train_model.py`, `models/xgboost_v3.pkl`)

### Problem statement

The AI Analyst gives a qualitative read. A quantitative counterpart —
"historically, how often did this indicator pattern precede a gain?" — would let
the platform show a probability, not just an opinion.

### What exists

An XGBoost classifier trained on NIFTY-50 history to predict
**P(10-day forward return > 2%)** from 19 features (RSI, MACD, EMA/SMA, ATR,
Bollinger width, volume change, volatility, momentum, plus NIFTY-level features
and relative strength). Pipeline: `collectors → feature_engineering →
label_generation → train_model`.

### Why it isn't wired in yet

- `xgboost` is a large binary wheel that risks Vercel's serverless size limit.
- Its feature pipeline (incl. NIFTY merge + `ta` indicators) would need porting
  into the request path.

### Integration path (future)

Bundle `xgboost_v3.pkl`, port the feature computation, expose
`/api/ai/predict/<ticker>` returning the model's probability, and let the AI
Analyst prompt include it as one more input.

---

## Cross-cutting constraints

| Constraint | Effect |
|---|---|
| No background workers on Vercel | Alerts & auto-orders only run on user-triggered dashboard polls |
| In-memory price cache | Cold starts begin with no prices until the first poll |
| Yahoo Finance data | ~1-day delayed, occasionally gaps; not a trading-grade feed |
| Gemini free tier | Sporadic 503s (handled by retry); rate limits under heavy use |
| Paper trading only | No real money, no broker, no regulatory layer — educational use |

## Environment variables

| Name | Used by | Required |
|---|---|---|
| `DATABASE_URL` | all DB routes | yes |
| `JWT_SECRET_KEY` | auth | yes (defaults to an insecure dev value) |
| `GEMINI_API_KEY` | AI Stock Analyst | yes, for `/api/ai/*` |
| `AI_MODEL` | AI Stock Analyst | no (defaults to `gemini-flash-latest`) |
