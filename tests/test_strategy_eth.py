"""
Tests de la estrategia DGT v2 del bot ETH.

La matemática (ADX, ATR, EMA, Donchian) y la lógica de decisión son puras,
así que se testean sin exchange, DB ni red.
"""

import math

from app.strategy_eth import EthStrategyEngine


def _candle(o, h, l, c, v=100.0):
    return {"timestamp": None, "open": o, "high": h, "low": l, "close": c, "volume": v}


def _trending_candles(n=80, start=1000.0, step=10.0):
    """Serie fuertemente alcista → ADX alto (breakout)."""
    candles = []
    price = start
    for _ in range(n):
        o = price
        c = price + step
        h = c + step * 0.2
        l = o - step * 0.1
        candles.append(_candle(o, h, l, c))
        price = c
    return candles


def _choppy_candles(n=80, base=1000.0, amp=5.0):
    """Serie lateral oscilante → ADX bajo (grid)."""
    candles = []
    for i in range(n):
        mid = base + amp * math.sin(i / 2.0)
        o = mid
        c = base + amp * math.sin((i + 1) / 2.0)
        h = max(o, c) + 1.0
        l = min(o, c) - 1.0
        candles.append(_candle(o, h, l, c))
    return candles


# ── ATR ──────────────────────────────────────────────────────────────────────

def test_atr_returns_positive_float():
    eng = EthStrategyEngine()
    candles = _trending_candles()
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    closes = [c["close"] for c in candles]
    atr = eng.calculate_atr(highs, lows, closes, period=14)
    assert isinstance(atr, float)
    assert atr > 0.0


def test_atr_insufficient_data_returns_zero():
    eng = EthStrategyEngine()
    assert eng.calculate_atr([1, 2], [1, 2], [1, 2], period=14) == 0.0


def test_atr_choppy_lower_than_trending():
    eng = EthStrategyEngine()
    trend = _trending_candles()
    chop = _choppy_candles()
    atr_trend = eng.calculate_atr(
        [c["high"] for c in trend], [c["low"] for c in trend], [c["close"] for c in trend])
    atr_chop = eng.calculate_atr(
        [c["high"] for c in chop], [c["low"] for c in chop], [c["close"] for c in chop])
    assert atr_trend > atr_chop


# ── EMA ──────────────────────────────────────────────────────────────────────

def test_ema_returns_float():
    eng = EthStrategyEngine()
    vals = [float(i) for i in range(60)]
    ema = eng.calculate_ema(vals, period=50)
    assert isinstance(ema, float)
    assert ema > 0.0


def test_ema_empty_returns_zero():
    eng = EthStrategyEngine()
    assert eng.calculate_ema([], period=50) == 0.0


def test_ema_short_series_returns_mean():
    eng = EthStrategyEngine()
    vals = [10.0, 20.0, 30.0]
    ema = eng.calculate_ema(vals, period=50)
    assert math.isclose(ema, 20.0, rel_tol=1e-4)


# ── ADX ──────────────────────────────────────────────────────────────────────

def test_adx_returns_float_in_range():
    eng = EthStrategyEngine()
    candles = _trending_candles()
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    closes = [c["close"] for c in candles]
    adx = eng.calculate_adx(highs, lows, closes, period=14)
    assert isinstance(adx, float)
    assert 0.0 <= adx <= 100.0


def test_adx_high_on_strong_trend():
    eng = EthStrategyEngine()
    candles = _trending_candles()
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    closes = [c["close"] for c in candles]
    adx = eng.calculate_adx(highs, lows, closes, period=14)
    assert adx >= 25.0


def test_adx_low_on_choppy_market():
    eng = EthStrategyEngine()
    candles = _choppy_candles()
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    closes = [c["close"] for c in candles]
    adx = eng.calculate_adx(highs, lows, closes, period=14)
    assert adx < 25.0


def test_adx_insufficient_data_returns_zero():
    eng = EthStrategyEngine()
    assert eng.calculate_adx([1, 2, 3], [1, 2, 3], [1, 2, 3], period=14) == 0.0


# ── Donchian ─────────────────────────────────────────────────────────────────

def test_donchian_channel():
    eng = EthStrategyEngine()
    highs = [10, 12, 15, 11, 9]
    lows = [5, 6, 7, 4, 3]
    d = eng.calculate_donchian(highs, lows, period=5)
    assert d["upper"] == 15
    assert d["lower"] == 3
    assert d["middle"] == 9.0


# ── Régimen ──────────────────────────────────────────────────────────────────

def test_detect_regime_breakout():
    eng = EthStrategyEngine()
    assert eng.detect_regime(_trending_candles()) == "breakout"


def test_detect_regime_grid():
    eng = EthStrategyEngine()
    assert eng.detect_regime(_choppy_candles()) == "grid"


# ── Grid ─────────────────────────────────────────────────────────────────────

def test_grid_asymmetric_distribution():
    eng = EthStrategyEngine()
    g = eng.decide_grid(current_price=1000.0, capital=100.0, levels=10, range_pct=0.12)
    # 60% abajo → 6 compras, 40% arriba → 4 ventas
    assert len(g["buy_levels"]) == 6
    assert len(g["sell_levels"]) == 4
    assert g["amount_per_level"] == 10.0
    assert all(p < 1000.0 for p in g["buy_levels"])
    assert all(p > 1000.0 for p in g["sell_levels"])


def test_grid_within_range():
    eng = EthStrategyEngine()
    g = eng.decide_grid(current_price=1000.0, capital=100.0, levels=10, range_pct=0.12)
    # ±6% → [940, 1060]
    assert min(g["buy_levels"]) >= 940.0 - 1e-6
    assert max(g["sell_levels"]) <= 1060.0 + 1e-6


# ── Breakout ─────────────────────────────────────────────────────────────────

def test_breakout_buy_signal():
    eng = EthStrategyEngine()
    donchian = {"upper": 1000.0, "lower": 900.0, "middle": 950.0}
    sig = eng.decide_breakout([], donchian, current_price=1005.0)
    assert sig["action"] == "buy"
    assert math.isclose(sig["stop_loss"], 1005.0 * 0.98, rel_tol=1e-4)
    assert math.isclose(sig["take_profit"], 1005.0 * 1.06, rel_tol=1e-4)


def test_breakout_sell_signal():
    eng = EthStrategyEngine()
    donchian = {"upper": 1000.0, "lower": 900.0, "middle": 950.0}
    sig = eng.decide_breakout([], donchian, current_price=895.0)
    assert sig["action"] == "sell"


def test_breakout_hold_signal():
    eng = EthStrategyEngine()
    donchian = {"upper": 1000.0, "lower": 900.0, "middle": 950.0}
    sig = eng.decide_breakout([], donchian, current_price=950.0)
    assert sig["action"] == "hold"
    assert sig["stop_loss"] is None


# ── decide() — riesgo ────────────────────────────────────────────────────────

def _cfg(**over):
    base = {
        "adx_period": 14, "adx_threshold": 25.0, "donchian_period": 20,
        "levels": 10, "range_pct": 0.12, "grid_below_ratio": 0.60,
        "max_position_pct": 0.10, "daily_drawdown_limit_pct": 0.05,
        "stop_loss_pct": 0.02, "take_profit_pct": 0.06, "daily_drawdown_pct": 0.0,
        "profit_take_threshold_pct": 0.15, "profit_take_sell_ratio": 0.20,
        "profit_take_cooldown_hours": 4.0,
        "hours_since_last_profit_take": 999.0,
        "grid_atr_multiplier": 3.0, "grid_range_min_pct": 0.03,
        "grid_range_max_pct": 0.12,
        "inventory_eth": 0.0, "avg_cost": 0.0, "base_capital": 100.0,
    }
    base.update(over)
    return base


def test_decide_daily_limit_blocks():
    eng = EthStrategyEngine()
    dec = eng.decide(_choppy_candles(), capital=100.0, config=_cfg(daily_drawdown_pct=0.06))
    assert dec.action == "hold"
    assert "daily_limit" in dec.reason


def test_decide_grid_regime():
    eng = EthStrategyEngine()
    dec = eng.decide(_choppy_candles(), capital=100.0, config=_cfg())
    assert dec.regime == "grid"
    assert dec.action == "grid"
    assert dec.grid is not None
    assert dec.amount_usd <= 100.0 * 0.10 + 1e-6


def test_decide_breakout_regime():
    eng = EthStrategyEngine()
    dec = eng.decide(_trending_candles(), capital=100.0, config=_cfg())
    assert dec.regime == "breakout"
    assert dec.action in ("buy", "sell", "grid")
    assert dec.amount_usd <= 100.0 * 0.10 + 1e-6


# ── Profit-taking ────────────────────────────────────────────────────────────

def test_profit_take_triggers_sell():
    eng = EthStrategyEngine()
    candles = _choppy_candles(base=1000.0)
    # Inventario comprado a 800, precio ~1000 → ganancia 25% > umbral 15%
    dec = eng.decide(candles, capital=100.0, config=_cfg(
        inventory_eth=0.5, avg_cost=800.0, base_capital=100.0,
        hours_since_last_profit_take=999.0,
    ))
    assert dec.action == "sell"
    assert dec.strategy == "profit_take"
    assert "Profit-take" in dec.reason


def test_profit_take_no_trigger_when_below_threshold():
    eng = EthStrategyEngine()
    candles = _choppy_candles(base=1000.0)
    # Inventario comprado a 999, ganancia ~0.1% < umbral 15% → no se dispara
    dec = eng.decide(candles, capital=100.0, config=_cfg(
        inventory_eth=0.5, avg_cost=999.0, base_capital=100.0,
    ))
    assert dec.action != "sell" or dec.strategy != "profit_take"


def test_profit_take_blocked_by_cooldown():
    eng = EthStrategyEngine()
    candles = _choppy_candles(base=1000.0)
    # Ganancia 25% > umbral 15%, pero cooldown activo (0.5h < 4h)
    dec = eng.decide(candles, capital=100.0, config=_cfg(
        inventory_eth=0.5, avg_cost=800.0, base_capital=100.0,
        hours_since_last_profit_take=0.5,
        profit_take_cooldown_hours=4.0,
    ))
    assert dec.action != "sell" or dec.strategy != "profit_take"


def test_profit_take_after_cooldown_expires():
    eng = EthStrategyEngine()
    candles = _choppy_candles(base=1000.0)
    # Ganancia 25% > umbral 15%, cooldown expirado (5h > 4h)
    dec = eng.decide(candles, capital=100.0, config=_cfg(
        inventory_eth=0.5, avg_cost=800.0, base_capital=100.0,
        hours_since_last_profit_take=5.0,
        profit_take_cooldown_hours=4.0,
    ))
    assert dec.action == "sell"
    assert dec.strategy == "profit_take"


# ── Dynamic range ────────────────────────────────────────────────────────────

def test_decide_returns_dynamic_range():
    eng = EthStrategyEngine()
    dec = eng.decide(_choppy_candles(), capital=100.0, config=_cfg())
    assert dec.dynamic_range_pct >= 0.03
    assert dec.dynamic_range_pct <= 0.12


def test_decide_returns_atr_and_ema():
    eng = EthStrategyEngine()
    dec = eng.decide(_choppy_candles(), capital=100.0, config=_cfg())
    assert dec.atr >= 0.0
    assert dec.ema50 > 0.0
