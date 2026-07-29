import os
import ssl
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from flask import Flask, request, jsonify
from flask_cors import CORS
from models import db, User, Transaction
from auth import generate_token, token_required
import yfinance as yf
from pandas import MultiIndex

app = Flask(__name__)
CORS(app)

# Reads the connection string from the environment (set DATABASE_URL in Vercel
# project settings). Falls back to local Postgres for local development only.
database_url = os.environ.get(
    'DATABASE_URL', 'postgresql://postgres:1234@localhost:5432/stock_db'
)
# SQLAlchemy needs the pg8000 driver explicitly - psycopg2's compiled binary
# doesn't run reliably in Vercel's serverless Python runtime.
if database_url.startswith('postgresql://'):
    database_url = database_url.replace('postgresql://', 'postgresql+pg8000://', 1)
elif database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql+pg8000://', 1)

# Hosted providers like Neon put libpq-style params (sslmode, channel_binding)
# in the URL. pg8000's driver doesn't accept those as connect kwargs and
# raises immediately, so strip them and configure TLS via connect_args below.
using_pg8000 = 'pg8000' in database_url
if using_pg8000:
    parts = urlsplit(database_url)
    filtered_query = [
        (k, v) for k, v in parse_qsl(parts.query)
        if k not in ('sslmode', 'channel_binding')
    ]
    database_url = urlunsplit(parts._replace(query=urlencode(filtered_query)))

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
# Serverless functions are short-lived, so verify connections before reuse
# and don't hold onto ones that outlive the function instance.
engine_options = {
    'pool_pre_ping': True,
    'pool_recycle': 280,
}
if using_pg8000 and 'localhost' not in database_url and '127.0.0.1' not in database_url:
    engine_options['connect_args'] = {'ssl_context': ssl.create_default_context()}
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = engine_options
db.init_app(app)

try:
    with app.app_context():
        db.create_all()
except Exception as exc:  # noqa: BLE001
    # Don't let a DB outage/misconfiguration take down routes that don't need
    # the DB (e.g. the stock chart endpoint) - surface it per-request instead.
    app.logger.error(f"Database initialization failed: {exc}")

@app.route('/api/_debug_db', methods=['GET'])
def debug_db():
    # TEMPORARY: remove once the Vercel/Neon connection issue is confirmed fixed.
    info = {
        'database_url_set': bool(os.environ.get('DATABASE_URL')),
        'database_url_host': urlsplit(app.config['SQLALCHEMY_DATABASE_URI']).hostname,
        'database_url_scheme': urlsplit(app.config['SQLALCHEMY_DATABASE_URI']).scheme,
    }
    try:
        from sqlalchemy import text
        with app.app_context():
            db.session.execute(text('SELECT 1'))
        info['connection'] = 'ok'
    except Exception as exc:  # noqa: BLE001
        info['connection'] = 'failed'
        info['error_type'] = type(exc).__name__
        info['error'] = str(exc)
    return jsonify(info)

NIFTY_50_TICKERS = [
    "RELIANCE.NS", "HDFCBANK.NS", "BHARTIARTL.NS", "ICICIBANK.NS", "SBIN.NS", 
    "TCS.NS", "BAJFINANCE.NS", "LT.NS", "LICI.NS", "HINDUNILVR.NS", 
    "ADANIPOWER.NS", "SUNPHARMA.NS", "INFY.NS", "ADANIPORTS.NS", "AXISBANK.NS",
    "MARUTI.NS", "KOTAKBANK.NS", "ADANIENT.NS", "TITAN.NS", "M&M.NS", 
    "ITC.NS", "NTPC.NS", "ULTRACEMCO.NS", "JSWSTEEL.NS", "BEL.NS",
    "ONGC.NS", "HCLTECH.NS", "HAL.NS", "BAJAJFINSV.NS", "BAJAJ-AUTO.NS", 
    "DMART.NS", "COALINDIA.NS", "NESTLEIND.NS", "POWERGRID.NS", "ASIANPAINT.NS",
    "TATASTEEL.NS", "ADANIGREEN.NS", "HINDZINC.NS", "SHRIRAMFIN.NS", "HINDALCO.NS", 
    "GRASIM.NS", "EICHERMOT.NS", "IOC.NS", "INDIGO.NS", "WIPRO.NS", 
    "ADANIENSOL.NS", "SBILIFE.NS", "VBL.NS", "VEDL.NS"
]

# 🛠️ MEMORY CACHE MATRIX: Keeps trades lightning fast without choking network threads
PRICE_CACHE = {}

@app.route('/api/register', methods=['POST'])
def register():
    data = request.json or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    if not username or not password:
        return jsonify({'message': 'Username and password are required'}), 400
    if User.query.filter_by(username=username).first():
        return jsonify({'message': 'Username already exists'}), 400

    new_user = User(username=username)
    new_user.set_password(password)
    db.session.add(new_user)
    db.session.commit()

    token = generate_token(new_user.id)
    return jsonify({'token': token, 'username': new_user.username}), 201

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    user = User.query.filter_by(username=username).first()
    if not user or not user.check_password(password):
        return jsonify({'message': 'Invalid username or password'}), 401
    token = generate_token(user.id)
    return jsonify({'token': token, 'username': user.username})

@app.route('/api/dashboard', methods=['GET'])
@token_required
def get_dashboard(current_user):
    global PRICE_CACHE

    tickers_str = " ".join(NIFTY_50_TICKERS)
    data = yf.download(tickers_str, period="1d", group_by='ticker', progress=False)
    
    stocks_list = []
    for ticker in NIFTY_50_TICKERS:
        try:
            ticker_data = data[ticker]
            current_price = ticker_data['Close'].dropna().iloc[-1]
            high_price = ticker_data['High'].dropna().max()
            low_price = ticker_data['Low'].dropna().min()
            open_price = ticker_data['Open'].dropna().iloc[0]
            
            price_val = round(float(current_price), 2)
            PRICE_CACHE[ticker] = price_val  # Save to internal memory cache
            
            stocks_list.append({
                'ticker': ticker,
                'price': price_val,
                'high': round(float(high_price), 2),
                'low': round(float(low_price), 2),
                'open': round(float(open_price), 2)
            })
        except:
            try:
                info = yf.Ticker(ticker).fast_info
                price_val = round(info['last_price'], 2)
                PRICE_CACHE[ticker] = price_val  # Save to internal memory cache
                stocks_list.append({
                    'ticker': ticker,
                    'price': price_val,
                    'high': round(info['day_high'], 2),
                    'low': round(info['day_low'], 2),
                    'open': round(info['open'] if 'open' in info else info['last_price'], 2)
                })
            except:
                fallback_p = PRICE_CACHE.get(ticker, 0.0)
                stocks_list.append({'ticker': ticker, 'price': fallback_p, 'high': fallback_p, 'low': fallback_p, 'open': fallback_p})

    txs = Transaction.query.filter_by(user_id=current_user.id).all()
    portfolio = {}
    for tx in txs:
        portfolio[tx.ticker] = portfolio.get(tx.ticker, 0) + tx.shares
    
    portfolio = {k: v for k, v in portfolio.items() if v > 0}
    portfolio_value = 0
    open_trades = len(portfolio)
    
    for ticker, shares in portfolio.items():
        live_p = PRICE_CACHE.get(ticker, 0.0)
        portfolio_value += live_p * shares

    return jsonify({
        'balance': round(current_user.balance, 2),
        'portfolio_value': round(portfolio_value, 2),
        'open_trades': open_trades,
        'stocks': stocks_list
    })

@app.route('/api/stock/<ticker>/chart', methods=['GET'])
def get_stock_chart(ticker):
    stock = yf.Ticker(ticker)
    hist = stock.history(period="5d", interval="15m")
    
    ohlc_data = []
    for idx, row in hist.iterrows():
        ohlc_data.append({
            'x': int(idx.timestamp() * 1000),
            'y': [round(row['Open'], 2), round(row['High'], 2), round(row['Low'], 2), round(row['Close'], 2)]
        })
    return jsonify(ohlc_data)

@app.route('/api/trade', methods=['POST'])
@token_required
def trade_stock(current_user):
    global PRICE_CACHE
    data = request.json
    ticker = data['ticker']
    shares = int(data['shares'])
    action = data['action']
    
    # 🛠️ INSTANT EXECUTION: Read from high-speed memory cache instead of slow web calls
    live_price = PRICE_CACHE.get(ticker, 0.0)
    if live_price == 0.0:
        try:
            live_price = yf.Ticker(ticker).fast_info['last_price']
        except:
            return jsonify({'message': 'Market feed sync pending. Try again in a moment.'}), 400
            
    total_cost = live_price * shares

    if action == 'BUY':
        if current_user.balance < total_cost:
            return jsonify({'message': 'Insufficient funds!'}), 400
        current_user.balance -= total_cost
        tx = Transaction(user_id=current_user.id, ticker=ticker, shares=shares, buy_price=live_price)
    elif action == 'SELL':
        txs = Transaction.query.filter_by(user_id=current_user.id, ticker=ticker).all()
        owned_shares = sum([t.shares for t in txs])
        if owned_shares < shares:
            return jsonify({'message': 'Not enough shares!'}), 400
        current_user.balance += total_cost
        tx = Transaction(user_id=current_user.id, ticker=ticker, shares=-shares, buy_price=live_price)

    db.session.add(tx)
    db.session.commit()
    return jsonify({'message': f'Successfully {action}ED {shares} shares'})

@app.route('/api/add-money', methods=['POST'])
@token_required
def add_money(current_user):
    amount = float((request.json or {}).get('amount', 0))
    current_user.balance += amount
    db.session.commit()
    return jsonify({'message': 'Success', 'balance': current_user.balance})

@app.route('/api/portfolio', methods=['GET'])
@token_required
def get_portfolio(current_user):
    global PRICE_CACHE
    txs = Transaction.query.filter_by(user_id=current_user.id).all()
    holdings = {}
    for tx in txs:
        if tx.ticker not in holdings:
            holdings[tx.ticker] = {'total_shares': 0, 'total_cost': 0.0}
        if tx.shares > 0:
            holdings[tx.ticker]['total_shares'] += tx.shares
            holdings[tx.ticker]['total_cost'] += (tx.shares * tx.buy_price)
        else:
            holdings[tx.ticker]['total_shares'] += tx.shares

    portfolio_data = []
    total_invested = 0
    for ticker, data in holdings.items():
        if data['total_shares'] > 0:
            avg_price = data['total_cost'] / data['total_shares']
            total_invested += data['total_cost']
            live_price = PRICE_CACHE.get(ticker, avg_price)
            
            portfolio_data.append({
                'ticker': ticker,
                'shares': data['total_shares'],
                'avg_buy_price': round(avg_price, 2),
                'current_value': round(live_price * data['total_shares'], 2),
                'current_price': round(live_price, 2)
            })
    return jsonify({
        'holdings': portfolio_data,
        'total_invested': round(total_invested, 2)
    })

@app.route('/api/history', methods=['GET'])
@token_required
def get_history(current_user):
    txs = Transaction.query.filter_by(user_id=current_user.id).order_by(Transaction.timestamp.desc()).all()
    history_data = []
    for tx in txs:
        history_data.append({
            'ticker': tx.ticker,
            'shares': abs(tx.shares),
            'price': round(tx.buy_price, 2),
            'action': 'BUY' if tx.shares > 0 else 'SELL',
            'time': tx.timestamp.strftime('%Y-%m-%d %H:%M:%S')
        })
    return jsonify(history_data)

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True, port=5001)