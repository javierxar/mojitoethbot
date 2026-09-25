"""
Modelos de base de datos del ETH Trading Bot.

Reutiliza los modelos de infraestructura del bot BTC (Auth, Setting) y agrega
los modelos propios de la estrategia DGT: EthCandle, EthTrade, EthBotState.
"""

from datetime import datetime

from sqlalchemy import (
    Column, DateTime, Float, Integer, String, Text,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


# ─── Infraestructura (heredada del bot BTC) ──────────────────────────────────

class Auth(Base):
    __tablename__ = "auth"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(64), unique=True, nullable=False)
    password_hash = Column(String(256), nullable=False)
    failed_attempts = Column(Integer, default=0, nullable=False)
    locked_until = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class Setting(Base):
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(128), unique=True, nullable=False)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


# ─── Modelos de la estrategia ETH ────────────────────────────────────────────

class EthCandle(Base):
    """Vela OHLCV de ETH/USDT cacheada desde Binance."""
    __tablename__ = "eth_candles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, nullable=False, index=True)   # apertura de la vela (UTC)
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Float, nullable=False)
    interval = Column(String(8), nullable=False, default="15m")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class EthTrade(Base):
    """Operación de trading (compra o venta) ejecutada por el bot."""
    __tablename__ = "eth_trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    side = Column(String(4), nullable=False)             # buy | sell
    price = Column(Float, nullable=False)                # precio promedio real de ejecución
    amount_eth = Column(Float, nullable=False)           # ETH netos (compra: recibidos; venta: entregados)
    amount_usd = Column(Float, nullable=False)           # USDT netos (compra: gastados; venta: recibidos)
    pnl = Column(Float, nullable=True, default=0.0)      # resultado realizado NETO de comisiones (USD)
    fee_amount = Column(Float, nullable=True)            # comisión cobrada, en fee_currency
    fee_currency = Column(String(8), nullable=True)      # eth | usdt
    fee_usd = Column(Float, nullable=True)               # comisión valuada en USD (NULL = trade previo sin dato)
    strategy = Column(String(16), nullable=False)        # grid | breakout
    status = Column(String(16), nullable=False, default="simulated")  # simulated | filled | error
    order_id = Column(String(128), nullable=True)        # id de la orden del exchange (live)
    dry_run = Column(Integer, nullable=False, default=1) # 1=simulado, 0=real


class EthBotState(Base):
    """Snapshot del estado del bot ETH en cada ciclo."""
    __tablename__ = "eth_bot_state"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    regime = Column(String(10), nullable=False)          # grid | breakout
    adx_value = Column(Float, nullable=True)
    current_price = Column(Float, nullable=True)
    daily_pnl = Column(Float, nullable=True, default=0.0)
    total_pnl = Column(Float, nullable=True, default=0.0)
    drawdown_pct = Column(Float, nullable=True, default=0.0)
    active_grid_levels = Column(Text, nullable=True)     # JSON con los niveles del grid
    capital = Column(Float, nullable=True)               # capital actual (USD)
    dry_run = Column(Integer, nullable=False, default=1) # 1=simulado, 0=real
