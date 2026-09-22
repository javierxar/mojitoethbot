"""
Motor de estrategia DGT v2 para trading de ETH.

Mejoras sobre v1:
  - ATR dinámico para ajustar el rango del grid a la volatilidad.
  - EMA-50 como filtro de tendencia: ajusta el sesgo compra/venta del grid.
  - Profit-taking: vende parte del inventario cuando la ganancia no realizada
    supera un umbral configurable.
  - Inventario máximo: limita la acumulación de posiciones.

Regímenes (según ADX):
  - ADX < 25  → "grid": mercado lateral, grilla adaptativa.
  - ADX >= 25 → "breakout": tendencia fuerte, señales Donchian.

Toda la matemática es pura, sin dependencias externas.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class Decision:
    regime: str
    action: str                      # "grid" | "buy" | "sell" | "hold"
    reason: str
    adx: float = 0.0
    current_price: float = 0.0
    amount_usd: float = 0.0
    grid: dict | None = None
    donchian: dict | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    strategy: str = "grid"
    atr: float = 0.0
    ema50: float = 0.0
    dynamic_range_pct: float = 0.0


def _extract(candles: list[dict]) -> tuple[list[float], list[float], list[float]]:
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    closes = [float(c["close"]) for c in candles]
    return highs, lows, closes


class EthStrategyEngine:

    # ── ATR (Wilder) ─────────────────────────────────────────────────────────

    def calculate_atr(self, highs, lows, closes, period: int = 14) -> float:
        n = len(closes)
        if n < period + 1 or len(highs) != n or len(lows) != n:
            return 0.0
        trs: list[float] = []
        for i in range(1, n):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            trs.append(tr)
        atr = sum(trs[:period]) / period
        for i in range(period, len(trs)):
            atr = (atr * (period - 1) + trs[i]) / period
        return round(atr, 4)

    # ── EMA ───────────────────────────────────────────────────────────────────

    def calculate_ema(self, values: list[float], period: int = 50) -> float:
        if not values:
            return 0.0
        if len(values) < period:
            return round(sum(values) / len(values), 4)
        k = 2.0 / (period + 1)
        ema = sum(values[:period]) / period
        for v in values[period:]:
            ema = v * k + ema * (1 - k)
        return round(ema, 4)

    # ── ADX (Wilder) ─────────────────────────────────────────────────────────

    def calculate_adx(self, highs, lows, closes, period: int = 14) -> float:
        n = len(closes)
        if n < 2 * period + 1 or len(highs) != n or len(lows) != n:
            return 0.0

        trs: list[float] = []
        plus_dm: list[float] = []
        minus_dm: list[float] = []
        for i in range(1, n):
            up_move = highs[i] - highs[i - 1]
            down_move = lows[i - 1] - lows[i]
            plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
            minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            trs.append(tr)

        atr = sum(trs[:period])
        sm_plus = sum(plus_dm[:period])
        sm_minus = sum(minus_dm[:period])

        dxs: list[float] = []

        def _dx(atr_v, plus_v, minus_v) -> float:
            if atr_v == 0:
                return 0.0
            plus_di = 100.0 * plus_v / atr_v
            minus_di = 100.0 * minus_v / atr_v
            denom = plus_di + minus_di
            if denom == 0:
                return 0.0
            return 100.0 * abs(plus_di - minus_di) / denom

        dxs.append(_dx(atr, sm_plus, sm_minus))

        for i in range(period, len(trs)):
            atr = atr - (atr / period) + trs[i]
            sm_plus = sm_plus - (sm_plus / period) + plus_dm[i]
            sm_minus = sm_minus - (sm_minus / period) + minus_dm[i]
            dxs.append(_dx(atr, sm_plus, sm_minus))

        if len(dxs) < period:
            return round(sum(dxs) / len(dxs), 4) if dxs else 0.0

        adx = sum(dxs[:period]) / period
        for i in range(period, len(dxs)):
            adx = (adx * (period - 1) + dxs[i]) / period
        return round(adx, 4)

    # ── Donchian ─────────────────────────────────────────────────────────────

    def calculate_donchian(self, highs, lows, period: int = 20) -> dict:
        if not highs or not lows:
            return {"upper": 0.0, "lower": 0.0, "middle": 0.0}
        window_h = highs[-period:]
        window_l = lows[-period:]
        upper = max(window_h)
        lower = min(window_l)
        middle = (upper + lower) / 2.0
        return {"upper": round(upper, 2), "lower": round(lower, 2), "middle": round(middle, 2)}

    # ── Detección de régimen ─────────────────────────────────────────────────

    def detect_regime(self, candles: list[dict], period: int = 14, threshold: float = 25.0) -> str:
        highs, lows, closes = _extract(candles)
        adx = self.calculate_adx(highs, lows, closes, period=period)
        return "breakout" if adx >= threshold else "grid"

    # ── Grid ─────────────────────────────────────────────────────────────────

    def decide_grid(self, current_price: float, capital: float, levels: int,
                    range_pct: float, below_ratio: float = 0.60) -> dict:
        if levels <= 0 or current_price <= 0:
            return {"buy_levels": [], "sell_levels": [], "amount_per_level": 0.0}

        half = range_pct / 2.0
        lower_bound = current_price * (1 - half)
        upper_bound = current_price * (1 + half)

        n_buy = max(1, round(levels * below_ratio)) if levels > 1 else 1
        n_sell = max(0, levels - n_buy)

        buy_levels: list[float] = []
        if n_buy > 0:
            step_buy = (current_price - lower_bound) / n_buy
            for i in range(1, n_buy + 1):
                buy_levels.append(round(current_price - step_buy * i, 2))

        sell_levels: list[float] = []
        if n_sell > 0:
            step_sell = (upper_bound - current_price) / n_sell
            for i in range(1, n_sell + 1):
                sell_levels.append(round(current_price + step_sell * i, 2))

        amount_per_level = round(capital / levels, 4)
        return {
            "buy_levels": buy_levels,
            "sell_levels": sell_levels,
            "amount_per_level": amount_per_level,
        }

    # ── Breakout Donchian ────────────────────────────────────────────────────

    def decide_breakout(self, candles: list[dict], donchian: dict, current_price: float,
                        stop_loss_pct: float = 0.02, take_profit_pct: float = 0.06) -> dict:
        upper = donchian.get("upper", 0.0)
        lower = donchian.get("lower", 0.0)

        if upper and current_price >= upper:
            return {
                "action": "buy",
                "reason": f"Breakout alcista: precio ${current_price:,.2f} rompió techo Donchian ${upper:,.2f}",
                "stop_loss": round(current_price * (1 - stop_loss_pct), 2),
                "take_profit": round(current_price * (1 + take_profit_pct), 2),
            }

        if lower and current_price <= lower:
            return {
                "action": "sell",
                "reason": f"Breakout bajista: precio ${current_price:,.2f} rompió piso Donchian ${lower:,.2f}",
                "stop_loss": round(current_price * (1 + stop_loss_pct), 2),
                "take_profit": round(current_price * (1 - take_profit_pct), 2),
            }

        return {
            "action": "hold",
            "reason": f"Sin ruptura: precio ${current_price:,.2f} dentro del canal [{lower:,.2f}, {upper:,.2f}]",
            "stop_loss": None,
            "take_profit": None,
        }

    # ── Método principal ─────────────────────────────────────────────────────

    def decide(self, candles: list[dict], capital: float, config: dict) -> Decision:
        adx_period = int(config.get("adx_period", 14))
        adx_threshold = float(config.get("adx_threshold", 25.0))
        donchian_period = int(config.get("donchian_period", 20))
        levels = int(config.get("levels", 10))
        range_pct = float(config.get("range_pct", 0.12))
        below_ratio = float(config.get("grid_below_ratio", 0.60))
        max_position_pct = float(config.get("max_position_pct", 0.10))
        drawdown_limit = float(config.get("daily_drawdown_limit_pct", 0.05))
        stop_loss_pct = float(config.get("stop_loss_pct", 0.02))
        take_profit_pct = float(config.get("take_profit_pct", 0.06))
        daily_drawdown = float(config.get("daily_drawdown_pct", 0.0))

        # Nuevos parámetros v2
        profit_take_threshold = float(config.get("profit_take_threshold_pct", 0.15))
        profit_take_sell_ratio = float(config.get("profit_take_sell_ratio", 0.20))
        atr_multiplier = float(config.get("grid_atr_multiplier", 3.0))
        range_min = float(config.get("grid_range_min_pct", 0.03))
        range_max = float(config.get("grid_range_max_pct", 0.12))
        inventory_eth = float(config.get("inventory_eth", 0.0))
        avg_cost = float(config.get("avg_cost", 0.0))
        base_capital = float(config.get("base_capital", capital))

        highs, lows, closes = _extract(candles)
        current_price = closes[-1] if closes else 0.0
        adx = self.calculate_adx(highs, lows, closes, period=adx_period)
        atr = self.calculate_atr(highs, lows, closes, period=adx_period)
        ema50 = self.calculate_ema(closes, period=50)
        regime = "breakout" if adx >= adx_threshold else "grid"

        # ── ATR → rango dinámico del grid ────────────────────────────────────
        atr_pct = atr / current_price if current_price > 0 else 0.0
        if atr_multiplier > 0 and atr_pct > 0:
            dynamic_range = max(range_min, min(atr_pct * atr_multiplier, range_max))
        else:
            dynamic_range = range_pct
        logger.info(
            "[STRATEGY] ATR=%.2f (%.2f%%) EMA50=%.2f rango_dinámico=%.2f%%",
            atr, atr_pct * 100, ema50, dynamic_range * 100,
        )

        # ── Drawdown diario ──────────────────────────────────────────────────
        if daily_drawdown >= drawdown_limit:
            logger.warning(
                "[STRATEGY] Drawdown diario %.2f%% >= %.2f%% — HOLD",
                daily_drawdown * 100, drawdown_limit * 100,
            )
            return Decision(
                regime=regime, action="hold",
                reason=f"daily_limit: drawdown diario {daily_drawdown * 100:.2f}% "
                       f"superó el límite {drawdown_limit * 100:.2f}%",
                adx=adx, current_price=current_price, amount_usd=0.0,
                strategy=regime, atr=atr, ema50=ema50, dynamic_range_pct=dynamic_range,
            )

        # ── Profit-taking ────────────────────────────────────────────────────
        cooldown_hours = float(config.get("profit_take_cooldown_hours", 4.0))
        hours_since_pt = float(config.get("hours_since_last_profit_take", 999.0))
        min_inventory_eth = 0.001

        if (inventory_eth > min_inventory_eth and avg_cost > 0
                and current_price > avg_cost):
            pct_gain = (current_price - avg_cost) / avg_cost
            in_cooldown = hours_since_pt < cooldown_hours
            if pct_gain > profit_take_threshold and not in_cooldown:
                sell_eth = inventory_eth * profit_take_sell_ratio
                sell_usd = sell_eth * current_price
                unrealized = (current_price - avg_cost) * inventory_eth
                logger.info(
                    "[STRATEGY] PROFIT-TAKE: +%.1f%% > umbral %.1f%%, "
                    "vendiendo %.1f%% inv (%.6f ETH, unrealized $%.2f)",
                    pct_gain * 100, profit_take_threshold * 100,
                    profit_take_sell_ratio * 100, sell_eth, unrealized,
                )
                return Decision(
                    regime=regime, action="sell",
                    reason=f"Profit-take: +{pct_gain * 100:.1f}% > umbral "
                           f"{profit_take_threshold * 100:.0f}%, "
                           f"vendiendo {profit_take_sell_ratio * 100:.0f}% inventario",
                    adx=adx, current_price=current_price, amount_usd=sell_usd,
                    strategy="profit_take", atr=atr, ema50=ema50,
                    dynamic_range_pct=dynamic_range,
                )
            elif pct_gain > profit_take_threshold and in_cooldown:
                logger.info(
                    "[STRATEGY] Profit-take en cooldown (%.1fh de %.1fh), skip",
                    hours_since_pt, cooldown_hours,
                )

        max_position_usd = round(capital * max_position_pct, 4)

        # ── EMA trend filter → ajustar sesgo del grid ────────────────────────
        trend_up = current_price >= ema50
        if not trend_up and ema50 > 0:
            below_ratio = max(0.30, 1.0 - below_ratio)
            logger.info(
                "[STRATEGY] Precio $%.2f < EMA50 $%.2f → sesgo venta (below_ratio=%.2f)",
                current_price, ema50, below_ratio,
            )

        # ── Régimen breakout ─────────────────────────────────────────────────
        if regime == "breakout":
            donchian = self.calculate_donchian(highs, lows, period=donchian_period)
            signal = self.decide_breakout(
                candles, donchian, current_price,
                stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct,
            )
            action = signal["action"]

            if action in ("buy", "sell"):
                logger.info(
                    "[STRATEGY] BREAKOUT | ADX=%.1f | precio=%.2f | acción=%s",
                    adx, current_price, action,
                )
                return Decision(
                    regime=regime, action=action, reason=signal["reason"],
                    adx=adx, current_price=current_price, amount_usd=max_position_usd,
                    donchian=donchian,
                    stop_loss=signal["stop_loss"], take_profit=signal["take_profit"],
                    strategy="breakout", atr=atr, ema50=ema50,
                    dynamic_range_pct=dynamic_range,
                )

            logger.info(
                "[STRATEGY] BREAKOUT-HOLD (ADX=%.1f) → fallback a grid",
                adx,
            )

        # ── Grid (régimen lateral o fallback de breakout-hold) ───────────────
        grid = self.decide_grid(current_price, capital, levels, dynamic_range, below_ratio)
        amount_per_level = min(grid["amount_per_level"], max_position_usd)
        grid["amount_per_level"] = amount_per_level
        trend_tag = "↑" if trend_up else "↓"
        reason_prefix = f"Grid {trend_tag} (ADX={adx:.1f}, rango={dynamic_range * 100:.1f}%)" \
            if regime == "grid" else f"Grid fallback {trend_tag} (ADX={adx:.1f})"
        n_buy = len(grid["buy_levels"])
        n_sell = len(grid["sell_levels"])
        logger.info(
            "[STRATEGY] GRID | ADX=%.1f | rango=%.1f%% | %d compras / %d ventas | %.4f USD/nivel",
            adx, dynamic_range * 100, n_buy, n_sell, amount_per_level,
        )
        return Decision(
            regime=regime, action="grid",
            reason=f"{reason_prefix}: {n_buy} compras, {n_sell} ventas",
            adx=adx, current_price=current_price, amount_usd=amount_per_level,
            grid=grid, strategy="grid", atr=atr, ema50=ema50,
            dynamic_range_pct=dynamic_range,
        )
