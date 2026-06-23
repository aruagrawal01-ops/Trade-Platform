# 📈 StoqNest — Real-Time Nifty 50 Trade Simulator

StoqNest is a high-performance, real-time paper trading workspace focused on the Indian stock market (Nifty 50). Built with a lightning-fast Python Flask backend and a modern Vanilla JS/Tailwind CSS frontend, it allows users to practice asset management and technical strategies with actual market price data under zero capital risk conditions.

---

## ✨ Features

- **⚡ Instant Memory-Cached Execution:** Trade processing executes at memory speeds ($0\text{ ms}$ routing latency) using a localized price matrix cache.
- **📊 Interactive Candlestick Charts:** High-density 5-day interval $OHLC$ historical charting driven by the ApexCharts engine.
- **⭐️ Pinned Watchlist Feed:** Bookmark and track key assets side-by-side using local storage persistence.
- **📁 Dynamic Sector Filtering:** Sort through the 50-asset market matrix instantly via dedicated macro tabs (Banking, Tech, Energy, Adani Group).
- **💼 Unified Account Analytics:** Overall portfolio health dashboard tracking total invested capital, current valuation, and real-time unrealized P&L updates.
- **📜 Chronological Ledger Logs:** Complete history panel providing transparent, unalterable auditing of all previous transactions.

---

## 🛠️ Tech Stack

- **Frontend:** Vanilla JavaScript (ES6+), HTML5, Tailwind CSS, ApexCharts Engine
- **Backend:** Python 3.14+, Flask, Flask-CORS, yfinance (Yahoo Finance API Feed)
- **Database:** PostgreSQL, SQLAlchemy (ORM Core Mapping)

---

## 🏗️ System Architecture

```text
  [ Browser Client (HTML5 / Tailwind CSS) ]
                    │
           JSON API Requests (HTTP)
                    ▼
       [ Flask Application Layer ]
         ├── Memory-Cached Matrix (PRICE_CACHE)
         └── SQLAlchemy ORM Handler
                    │
         ┌──────────┴──────────┐
         ▼                     ▼
 [ PostgreSQL DB ]    [ Yahoo Finance API Feed ]
 (Order Ledger Logs)    (Live Ticker Sync Array)



 1. Prerequisites
Ensure you have the following installed on your local environment:

Python 3.10 or higher

PostgreSQL Database Server

2. Clone the Repository
3. Backend Database Configuration
Open pgAdmin4 or your psql terminal and create a new database named stock_db.

Open backend/app.py and replace YOUR_ACTUAL_POSTGRES_PASSWORD on line 12 with your true database master password:
4. Install Dependencies & Run Backend
5. Launch the Frontend

