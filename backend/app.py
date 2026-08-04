import os
import ssl
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from flask import Flask, request, jsonify
from flask_cors import CORS
from models import db, User, Transaction, PriceAlert, AutoOrder
from auth import generate_token, token_required
import yfinance as yf

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

def fetch_live_prices():
    """Refreshes PRICE_CACHE from Yahoo Finance and returns the per-ticker list.
    Shared by /api/dashboard and /api/leaderboard so both see the same prices."""
    global PRICE_CACHE
    tickers_str = " ".join(NIFTY_50_TICKERS)
    try:
        data = yf.download(tickers_str, period="1d", group_by='ticker', progress=False)
    except Exception:
        data = None

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
        except Exception:
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
            except Exception:
                fallback_p = PRICE_CACHE.get(ticker, 0.0)
                stocks_list.append({'ticker': ticker, 'price': fallback_p, 'high': fallback_p, 'low': fallback_p, 'open': fallback_p})
    return stocks_list

def check_price_alerts(user_id):
    """Checks a user's active alerts against the current PRICE_CACHE and marks
    any that have been hit as triggered. Returns the ones that just fired."""
    newly_triggered = []
    alerts = PriceAlert.query.filter_by(user_id=user_id, triggered=False).all()
    for alert in alerts:
        price = PRICE_CACHE.get(alert.ticker)
        if price is None:
            continue
        hit = (
            (alert.direction == 'above' and price >= alert.target_price) or
            (alert.direction == 'below' and price <= alert.target_price)
        )
        if hit:
            alert.triggered = True
            newly_triggered.append({
                'ticker': alert.ticker,
                'target_price': alert.target_price,
                'direction': alert.direction,
                'price': price
            })
    if newly_triggered:
        db.session.commit()
    return newly_triggered

def execute_trade(user, ticker, shares, action, live_price):
    """Shared BUY/SELL logic used by both the manual /api/trade route and
    automated order execution. Adds the Transaction but doesn't commit -
    the caller commits so it can batch multiple orders in one transaction."""
    total_cost = live_price * shares
    if action == 'BUY':
        if user.balance < total_cost:
            return False, 'Insufficient funds!'
        user.balance -= total_cost
        tx = Transaction(user_id=user.id, ticker=ticker, shares=shares, buy_price=live_price)
    elif action == 'SELL':
        txs = Transaction.query.filter_by(user_id=user.id, ticker=ticker).all()
        owned_shares = sum(t.shares for t in txs)
        if owned_shares < shares:
            return False, 'Not enough shares!'
        user.balance += total_cost
        tx = Transaction(user_id=user.id, ticker=ticker, shares=-shares, buy_price=live_price)
    else:
        return False, 'Invalid action'
    db.session.add(tx)
    return True, f'Successfully {action}ED {shares} shares'

def check_auto_orders(user):
    """Checks a user's active auto-orders against PRICE_CACHE and executes any
    that have hit their trigger price. Only runs while that user is polling
    the dashboard - there's no background worker on Vercel's serverless plan."""
    results = []
    orders = AutoOrder.query.filter_by(user_id=user.id, status='active').all()
    for order in orders:
        price = PRICE_CACHE.get(order.ticker)
        if price is None:
            continue
        hit = (
            (order.direction == 'above' and price >= order.target_price) or
            (order.direction == 'below' and price <= order.target_price)
        )
        if not hit:
            continue
        success, message = execute_trade(user, order.ticker, order.shares, order.action, price)
        order.executed_price = price
        order.executed_at = datetime.utcnow()
        order.status = 'executed' if success else 'failed'
        results.append({
            'ticker': order.ticker,
            'action': order.action,
            'shares': order.shares,
            'price': price,
            'status': order.status,
            'message': message
        })
    if results:
        db.session.commit()
    return results

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
    stocks_list = fetch_live_prices()

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

    triggered_alerts = check_price_alerts(current_user.id)
    executed_orders = check_auto_orders(current_user)

    return jsonify({
        'balance': round(current_user.balance, 2),
        'portfolio_value': round(portfolio_value, 2),
        'open_trades': open_trades,
        'stocks': stocks_list,
        'triggered_alerts': triggered_alerts,
        'executed_orders': executed_orders
    })

@app.route('/api/leaderboard', methods=['GET'])
@token_required
def get_leaderboard(current_user):
    fetch_live_prices()

    users = User.query.all()
    all_txs = Transaction.query.all()
    holdings_by_user = {}
    for tx in all_txs:
        holdings_by_user.setdefault(tx.user_id, {})
        holdings_by_user[tx.user_id][tx.ticker] = holdings_by_user[tx.user_id].get(tx.ticker, 0) + tx.shares

    rows = []
    for user in users:
        holdings = holdings_by_user.get(user.id, {})
        portfolio_value = sum(
            PRICE_CACHE.get(ticker, 0.0) * shares
            for ticker, shares in holdings.items() if shares > 0
        )
        net_worth = user.balance + portfolio_value
        rows.append({
            'username': user.username,
            'balance': round(user.balance, 2),
            'portfolio_value': round(portfolio_value, 2),
            'net_worth': round(net_worth, 2),
            'pl': round(net_worth - 100000.0, 2),
            'is_you': user.id == current_user.id
        })

    rows.sort(key=lambda r: r['net_worth'], reverse=True)
    for idx, row in enumerate(rows, start=1):
        row['rank'] = idx

    return jsonify(rows)

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
    data = request.json
    ticker = data['ticker']
    shares = int(data['shares'])
    action = data['action']

    # 🛠️ INSTANT EXECUTION: Read from high-speed memory cache instead of slow web calls
    live_price = PRICE_CACHE.get(ticker, 0.0)
    if live_price == 0.0:
        try:
            live_price = yf.Ticker(ticker).fast_info['last_price']
        except Exception:
            return jsonify({'message': 'Market feed sync pending. Try again in a moment.'}), 400

    success, message = execute_trade(current_user, ticker, shares, action, live_price)
    if not success:
        return jsonify({'message': message}), 400
    db.session.commit()
    return jsonify({'message': message})

@app.route('/api/add-money', methods=['POST'])
@token_required
def add_money(current_user):
    amount = float((request.json or {}).get('amount', 0))
    current_user.balance += amount
    db.session.commit()
    return jsonify({'message': 'Success', 'balance': current_user.balance})

@app.route('/api/alerts', methods=['GET'])
@token_required
def get_alerts(current_user):
    alerts = PriceAlert.query.filter_by(user_id=current_user.id).order_by(PriceAlert.created_at.desc()).all()
    return jsonify([{
        'id': a.id,
        'ticker': a.ticker,
        'target_price': a.target_price,
        'direction': a.direction,
        'triggered': a.triggered
    } for a in alerts])

@app.route('/api/alerts', methods=['POST'])
@token_required
def create_alert(current_user):
    data = request.json or {}
    ticker = (data.get('ticker') or '').strip()
    direction = data.get('direction')
    try:
        target_price = float(data.get('target_price'))
    except (TypeError, ValueError):
        return jsonify({'message': 'Invalid target price'}), 400
    if not ticker or direction not in ('above', 'below'):
        return jsonify({'message': "ticker and direction ('above'/'below') are required"}), 400

    alert = PriceAlert(user_id=current_user.id, ticker=ticker, target_price=target_price, direction=direction)
    db.session.add(alert)
    db.session.commit()
    return jsonify({'message': 'Alert created', 'id': alert.id}), 201

@app.route('/api/alerts/<int:alert_id>', methods=['DELETE'])
@token_required
def delete_alert(current_user, alert_id):
    alert = PriceAlert.query.filter_by(id=alert_id, user_id=current_user.id).first()
    if not alert:
        return jsonify({'message': 'Alert not found'}), 404
    db.session.delete(alert)
    db.session.commit()
    return jsonify({'message': 'Alert removed'})

@app.route('/api/auto-orders', methods=['GET'])
@token_required
def get_auto_orders(current_user):
    orders = AutoOrder.query.filter_by(user_id=current_user.id).order_by(AutoOrder.created_at.desc()).all()
    return jsonify([{
        'id': o.id,
        'ticker': o.ticker,
        'target_price': o.target_price,
        'direction': o.direction,
        'action': o.action,
        'shares': o.shares,
        'status': o.status,
        'executed_price': o.executed_price
    } for o in orders])

@app.route('/api/auto-orders', methods=['POST'])
@token_required
def create_auto_order(current_user):
    data = request.json or {}
    ticker = (data.get('ticker') or '').strip()
    direction = data.get('direction')
    action = data.get('action')
    try:
        target_price = float(data.get('target_price'))
        shares = int(data.get('shares'))
    except (TypeError, ValueError):
        return jsonify({'message': 'Invalid target price or share count'}), 400
    if not ticker or direction not in ('above', 'below') or action not in ('BUY', 'SELL') or shares <= 0:
        return jsonify({'message': "ticker, direction ('above'/'below'), action ('BUY'/'SELL'), and a positive share count are required"}), 400

    order = AutoOrder(
        user_id=current_user.id, ticker=ticker, target_price=target_price,
        direction=direction, action=action, shares=shares, status='active'
    )
    db.session.add(order)
    db.session.commit()
    return jsonify({'message': 'Auto order created', 'id': order.id}), 201

@app.route('/api/auto-orders/<int:order_id>', methods=['DELETE'])
@token_required
def delete_auto_order(current_user, order_id):
    order = AutoOrder.query.filter_by(id=order_id, user_id=current_user.id).first()
    if not order:
        return jsonify({'message': 'Order not found'}), 404
    if order.status != 'active':
        return jsonify({'message': 'Only active orders can be cancelled'}), 400
    db.session.delete(order)
    db.session.commit()
    return jsonify({'message': 'Order cancelled'})

@app.route('/api/change-password', methods=['POST'])
@token_required
def change_password(current_user):
    data = request.json or {}
    current_password = data.get('current_password') or ''
    new_password = data.get('new_password') or ''
    if not current_user.check_password(current_password):
        return jsonify({'message': 'Current password is incorrect'}), 401
    if len(new_password) < 6:
        return jsonify({'message': 'New password must be at least 6 characters'}), 400
    current_user.set_password(new_password)
    db.session.commit()
    return jsonify({'message': 'Password updated successfully'})

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