from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()

class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    balance = db.Column(db.Float, default=100000.0) # Starts with 1,00,000 INR
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, raw_password):
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password):
        return check_password_hash(self.password_hash, raw_password)

class Transaction(db.Model):
    __tablename__ = 'transactions'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    ticker = db.Column(db.String(20), nullable=False)
    shares = db.Column(db.Integer, nullable=False)      # Positive for Buy, Negative for Sell
    buy_price = db.Column(db.Float, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class PriceAlert(db.Model):
    __tablename__ = 'price_alerts'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    ticker = db.Column(db.String(20), nullable=False)
    target_price = db.Column(db.Float, nullable=False)
    direction = db.Column(db.String(10), nullable=False)  # 'above' or 'below'
    triggered = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class AutoOrder(db.Model):
    __tablename__ = 'auto_orders'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    ticker = db.Column(db.String(20), nullable=False)
    target_price = db.Column(db.Float, nullable=False)
    direction = db.Column(db.String(10), nullable=False)  # 'above' or 'below' (condition on price)
    action = db.Column(db.String(4), nullable=False)      # 'BUY' or 'SELL'
    shares = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(10), default='active')   # active, executed, failed, cancelled
    executed_price = db.Column(db.Float, nullable=True)
    executed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)