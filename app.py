from flask import Flask, request, jsonify
from flask_cors import CORS
from models import db, User, Transaction
import yfinance as yf
from pandas import MultiIndex

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": ["https://aruagrawal01-ops.github.io"]}})

# Connect to your local PostgreSQL database
app.config['SQLALCHEMY_DATABASE_URI'] = 'postgresql://postgres:DATA_BASE_PASSWORD@localhost:5432/DATABASE_NAME'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)

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
    data = request.json
    if User.query.filter_by(username=data['username']).first():
        return jsonify({'message': 'Username already exists'}), 400
    new_user = User(username=data['username'], password_hash=data['password'])
    db.session.add(new_user)
    db.session.commit()
    return jsonify({'message': 'Registered successfully'}), 201

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    user = User.query.filter_by(username=data['username']).first()
    if not user or user.password_hash != data['password']:
        return jsonify({'message': 'Invalid username or password'}), 401
    return jsonify({'token': 'BypassToken_Success', 'username': user.username})

@app.route('/api/dashboard', methods=['GET'])
def get_dashboard():
    global PRICE_CACHE
    current_user = User.query.first()
    if not current_user:
        return jsonify({'message': 'No users exist yet'}), 404

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
def trade_stock():
    global PRICE_CACHE
    current_user = User.query.first()
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
def add_money():
    current_user = User.query.first()
    amount = float(request.json.get('amount', 0))
    current_user.balance += amount
    db.session.commit()
    return jsonify({'message': 'Success', 'balance': current_user.balance})

@app.route('/api/portfolio', methods=['GET'])
def get_portfolio():
    global PRICE_CACHE
    current_user = User.query.first()
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
def get_history():
    current_user = User.query.first()
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
