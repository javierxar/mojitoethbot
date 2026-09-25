"""
Configuración del ETH Trading Bot.

Fork del bot BTC — reutiliza el mismo patrón de carga de variables de entorno
pero con parámetros propios de la estrategia DGT (grid + breakout Donchian).

Variables de entorno separadas del bot BTC (prefijo ETH_) para poder correr
ambos bots en paralelo sin interferencia. Puerto 7001, DB data/eth_bot.db.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _read_version() -> str:
    try:
        return (Path(__file__).parent.parent / "VERSION").read_text().strip()
    except Exception:
        return "0.0.0"


def _get(key: str, default=None):
    return os.environ.get(key, default)


def _get_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _get_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _get_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key)
    if val is None:
        return default
    return val.strip().lower() in ("true", "1", "yes")


# ─── Bot habilitado ──────────────────────────────────────────────────────────
ETH_BOT_ENABLED: bool = _get_bool("ETH_BOT_ENABLED", True)

# ─── Exchange ────────────────────────────────────────────────────────────────
EXCHANGE_CLIENT: str = _get("EXCHANGE_CLIENT", "mock")
BITSO_API_KEY: str = _get("BITSO_API_KEY", "")
BITSO_API_SECRET: str = _get("BITSO_API_SECRET", "")
# Par de trading en Bitso: eth_usdt | eth_mxn | eth_usd
TRADING_PAIR: str = _get("TRADING_PAIR", "eth_usdt")

# ─── Fuente de velas (Binance público, sin auth) ─────────────────────────────
BINANCE_SYMBOL: str = _get("BINANCE_SYMBOL", "ETHUSDT")
CANDLE_INTERVAL: str = _get("CANDLE_INTERVAL", "15m")
CANDLE_LIMIT: int = _get_int("CANDLE_LIMIT", 200)

# ─── Estrategia DGT ──────────────────────────────────────────────────────────
# Capital simulado inicial (USD)
ETH_CAPITAL_USD: float = _get_float("ETH_CAPITAL_USD", 100.0)
# Cantidad de niveles del grid (menos niveles = más ganancia por operación)
ETH_GRID_LEVELS: int = _get_int("ETH_GRID_LEVELS", 5)
# Rango total del grid como fracción (0.06 = ±3%)
ETH_GRID_RANGE_PCT: float = _get_float("ETH_GRID_RANGE_PCT", 0.06)
# Umbral de ADX para separar régimen grid (<25) de breakout (>=25)
ADX_THRESHOLD: float = _get_float("ADX_THRESHOLD", 25.0)
ADX_PERIOD: int = _get_int("ADX_PERIOD", 14)
DONCHIAN_PERIOD: int = _get_int("DONCHIAN_PERIOD", 20)

# ─── Gestión de riesgo ───────────────────────────────────────────────────────
# Máximo del capital comprometido por operación (fracción)
MAX_POSITION_PCT: float = _get_float("MAX_POSITION_PCT", 0.10)
# Límite de drawdown diario: si se supera, el bot pasa a hold (fracción)
DAILY_DRAWDOWN_LIMIT_PCT: float = _get_float("DAILY_DRAWDOWN_LIMIT_PCT", 0.05)
# Stop loss / take profit del breakout (fracción sobre el precio de entrada)
BREAKOUT_STOP_LOSS_PCT: float = _get_float("BREAKOUT_STOP_LOSS_PCT", 0.02)
BREAKOUT_TAKE_PROFIT_PCT: float = _get_float("BREAKOUT_TAKE_PROFIT_PCT", 0.06)
# Distribución asimétrica del grid: fracción de niveles debajo del precio
GRID_BELOW_RATIO: float = _get_float("GRID_BELOW_RATIO", 0.60)
# Profit-taking: vender parte del inventario cuando ganancia no realizada > umbral
PROFIT_TAKE_THRESHOLD_PCT: float = _get_float("PROFIT_TAKE_THRESHOLD_PCT", 0.15)
PROFIT_TAKE_SELL_RATIO: float = _get_float("PROFIT_TAKE_SELL_RATIO", 0.20)
# Inventario máximo como fracción del capital (al costo)
MAX_INVENTORY_COST_PCT: float = _get_float("MAX_INVENTORY_COST_PCT", 0.80)
# Grid dinámico basado en ATR: range = ATR/precio * multiplicador, acotado
GRID_ATR_MULTIPLIER: float = _get_float("GRID_ATR_MULTIPLIER", 3.0)
GRID_RANGE_MIN_PCT: float = _get_float("GRID_RANGE_MIN_PCT", 0.05)
# Comisiones de Bitso en eth_usdt (órdenes a mercado = taker; verificado 0.36% en compras y ventas)
BUY_FEE_PCT: float = _get_float("BUY_FEE_PCT", 0.0036)
SELL_FEE_PCT: float = _get_float("SELL_FEE_PCT", 0.0036)
# Ganancia mínima sobre costo + fees al fijar niveles de venta del inventario
GRID_MIN_MARGIN_PCT: float = _get_float("GRID_MIN_MARGIN_PCT", 0.002)
GRID_RANGE_MAX_PCT: float = _get_float("GRID_RANGE_MAX_PCT", 0.12)
PROFIT_TAKE_COOLDOWN_HOURS: float = _get_float("PROFIT_TAKE_COOLDOWN_HOURS", 4.0)

# ─── Modo de operación ───────────────────────────────────────────────────────
# dry_run=True → simulador (NUNCA ejecuta órdenes reales)
DRY_RUN: bool = _get_bool("DRY_RUN", True)
BOT_STATUS: str = _get("BOT_STATUS", "on")

# ─── Scheduler ───────────────────────────────────────────────────────────────
# Cada cuántos minutos corre el ciclo ETH
ETH_TICK_MINUTES: int = _get_int("ETH_TICK_MINUTES", 15)
TIMEZONE: str = _get("TIMEZONE", "America/Argentina/Buenos_Aires")

# ─── Cache Redis (opcional) ──────────────────────────────────────────────────
REDIS_ENABLED: bool = _get_bool("REDIS_ENABLED", False)
REDIS_URL: str = _get("REDIS_URL", "redis://redis:6379/0")
# Reusar velas cacheadas si tienen menos de N minutos
CANDLE_CACHE_MINUTES: int = _get_int("CANDLE_CACHE_MINUTES", 14)

# ─── Base de datos ───────────────────────────────────────────────────────────
DATABASE_URL: str = _get("DATABASE_URL", "sqlite:///./data/eth_bot.db")

# ─── Web / Auth ──────────────────────────────────────────────────────────────
WEB_USER: str = _get("WEB_USER", "Admin")
WEB_PASSWORD: str = _get("WEB_PASSWORD", "123456")
SESSION_SECRET: str = _get("SESSION_SECRET", "dev-secret-change-me-eth")

# ─── Dashboard ───────────────────────────────────────────────────────────────
DASHBOARD_AUTO_REFRESH_SECONDS: int = _get_int("DASHBOARD_AUTO_REFRESH_SECONDS", 30)

# ─── Logging ─────────────────────────────────────────────────────────────────
LOG_LEVEL: str = _get("LOG_LEVEL", "INFO")

# ─── Mock Exchange ───────────────────────────────────────────────────────────
MOCK_ETH_PRICE_USD: float = _get_float("MOCK_ETH_PRICE_USD", 3000.0)
MOCK_USDT_ARS_RATE: float = _get_float("MOCK_USDT_ARS_RATE", 1050.0)
MOCK_SPREAD_PERCENT: float = _get_float("MOCK_SPREAD_PERCENT", 0.3)

# Versión (leída desde archivo VERSION en la raíz del proyecto)
BOT_VERSION: str = _read_version()


def get_settings_dict() -> dict:
    """Devuelve los parámetros de estrategia como dict para EthStrategyEngine."""
    return {
        "capital": ETH_CAPITAL_USD,
        "levels": ETH_GRID_LEVELS,
        "range_pct": ETH_GRID_RANGE_PCT,
        "adx_threshold": ADX_THRESHOLD,
        "adx_period": ADX_PERIOD,
        "donchian_period": DONCHIAN_PERIOD,
        "max_position_pct": MAX_POSITION_PCT,
        "daily_drawdown_limit_pct": DAILY_DRAWDOWN_LIMIT_PCT,
        "stop_loss_pct": BREAKOUT_STOP_LOSS_PCT,
        "take_profit_pct": BREAKOUT_TAKE_PROFIT_PCT,
        "grid_below_ratio": GRID_BELOW_RATIO,
        "profit_take_threshold_pct": PROFIT_TAKE_THRESHOLD_PCT,
        "profit_take_sell_ratio": PROFIT_TAKE_SELL_RATIO,
        "max_inventory_cost_pct": MAX_INVENTORY_COST_PCT,
        "grid_atr_multiplier": GRID_ATR_MULTIPLIER,
        "grid_range_min_pct": GRID_RANGE_MIN_PCT,
        "grid_range_max_pct": GRID_RANGE_MAX_PCT,
        "profit_take_cooldown_hours": PROFIT_TAKE_COOLDOWN_HOURS,
        "dry_run": DRY_RUN,
        "trading_pair": TRADING_PAIR,
    }
