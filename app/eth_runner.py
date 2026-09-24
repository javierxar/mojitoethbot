"""
Runner del bot ETH — ejecuta un ciclo de la estrategia DGT v2.

Flujo de un tick:
  1. Descarga velas OHLCV de 15m (Binance) o usa caché Redis.
  2. Calcula indicadores (ADX, ATR, EMA) y detecta régimen.
  3. Llama a EthStrategyEngine.decide() con contexto de inventario.
  4. Simula o ejecuta la operación.
  5. Guarda estado y actualiza caché.

v2: profit-taking, multi-fill grid, inventory cap, grid dinámico ATR.

SEGURIDAD: el bot arranca en modo SIMULADOR. Solo ejecuta órdenes reales si
dry_run es False de forma explícita en settings.
"""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import desc

from app import config
from app.database import db_session, get_setting, set_setting
from app.eth_cache import cache_get, cache_set
from app.models import EthBotState, EthCandle, EthTrade
from app.strategy_eth import EthStrategyEngine

logger = logging.getLogger(__name__)

_CANDLES_CACHE_KEY = "eth:candles:15m"
_STATE_CACHE_KEY = "eth:state"


# ── Helpers de estado / PnL ───────────────────────────────────────────────────

def _today_start_utc() -> datetime:
    now = datetime.utcnow()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _is_dry_run(db) -> bool:
    val = get_setting(db, "dry_run", None)
    if val is None:
        return config.DRY_RUN
    return val.strip().lower() in ("true", "1", "yes")


def _inventory_and_cost(db, dry_run: bool) -> tuple[float, float]:
    """Devuelve (eth_en_cartera, costo_promedio_móvil_usd) de las operaciones."""
    trades = (db.query(EthTrade)
              .filter(EthTrade.dry_run == (1 if dry_run else 0))
              .order_by(EthTrade.timestamp, EthTrade.id).all())
    inv = 0.0
    cost = 0.0
    for t in trades:
        if t.side == "buy":
            inv += t.amount_eth
            cost += t.amount_usd
        elif inv > 0:
            sold = min(t.amount_eth, inv)
            cost -= cost * sold / inv
            inv -= sold
    if inv < 1e-9:
        return 0.0, 0.0
    return round(inv, 8), round(cost / inv, 4)


def _pnl_summary(db, dry_run: bool) -> tuple[float, float]:
    """Devuelve (daily_pnl, total_pnl) realizados a partir de EthTrade."""
    flag = 1 if dry_run else 0
    day_start = _today_start_utc()
    total = db.query(EthTrade).filter(EthTrade.dry_run == flag).all()
    total_pnl = sum((t.pnl or 0.0) for t in total)
    daily_pnl = sum((t.pnl or 0.0) for t in total if t.timestamp and t.timestamp >= day_start)
    return round(daily_pnl, 4), round(total_pnl, 4)


# ── Velas: descarga + caché + persistencia ────────────────────────────────────

def _get_candles(client) -> list[dict]:
    cached = cache_get(_CANDLES_CACHE_KEY)
    if cached:
        logger.info("[ETH] Velas tomadas de caché (%d)", len(cached))
        for c in cached:
            if isinstance(c.get("timestamp"), str):
                try:
                    c["timestamp"] = datetime.fromisoformat(c["timestamp"])
                except ValueError:
                    c["timestamp"] = datetime.utcnow()
        return cached

    candles = client.get_ohlcv_15m(config.TRADING_PAIR, limit=config.CANDLE_LIMIT)
    ttl = config.CANDLE_CACHE_MINUTES * 60
    cache_set(_CANDLES_CACHE_KEY, candles, ttl)
    return candles


def _persist_candles(db, candles: list[dict]) -> None:
    db.query(EthCandle).delete()
    for c in candles[-100:]:
        ts = c["timestamp"]
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        db.add(EthCandle(
            timestamp=ts, open=c["open"], high=c["high"], low=c["low"],
            close=c["close"], volume=c["volume"], interval=config.CANDLE_INTERVAL,
        ))
    db.flush()


# ── Ejecución de operaciones ──────────────────────────────────────────────────

def _record_trade(db, side: str, price: float, amount_eth: float, amount_usd: float,
                  pnl: float, strategy: str, dry_run: bool, order_id: str | None = None,
                  status: str = "simulated") -> None:
    db.add(EthTrade(
        timestamp=datetime.utcnow(), side=side, price=price,
        amount_eth=round(amount_eth, 8), amount_usd=round(amount_usd, 4),
        pnl=round(pnl, 4), strategy=strategy, status=status,
        order_id=order_id, dry_run=1 if dry_run else 0,
    ))
    logger.info("[ETH] Trade %s registrado: %.6f ETH @ %.2f (pnl=%.2f, %s)",
                side, amount_eth, price, pnl, "SIM" if dry_run else "LIVE")


def _simulate(db, decision, dry_run: bool) -> None:
    inv, avg_cost = _inventory_and_cost(db, dry_run)
    price = decision.current_price
    strat = decision.strategy

    if decision.action == "grid" and decision.grid:
        _simulate_grid(db, decision.grid, inv, avg_cost, dry_run,
                       max_inv_cost_pct=config.MAX_INVENTORY_COST_PCT,
                       capital=decision.amount_usd * config.ETH_GRID_LEVELS)
        return

    if decision.action == "buy" and decision.amount_usd > 0:
        eth = decision.amount_usd / price if price else 0.0
        _record_trade(db, "buy", price, eth, decision.amount_usd, 0.0, strat, dry_run)

    elif decision.action == "sell" and inv > 0:
        eth = min(decision.amount_usd / price if price else 0.0, inv) if decision.amount_usd else inv
        pnl = (price - avg_cost) * eth
        _record_trade(db, "sell", price, eth, eth * price, pnl, strat, dry_run)


def _simulate_grid(db, grid: dict, inv: float, avg_cost: float, dry_run: bool,
                   max_inv_cost_pct: float = 0.80, capital: float = 100.0) -> None:
    """
    Simula el grid con multi-fill y inventory cap.

    v2 mejoras:
      - Llena TODOS los niveles que la vela cruzó (no solo uno).
      - Inventory cap: deja de comprar si el costo del inventario supera el límite.
    """
    amount_usd = grid["amount_per_level"]
    if amount_usd <= 0:
        return

    last_candle = db.query(EthCandle).order_by(desc(EthCandle.timestamp)).first()
    if not last_candle:
        return

    prev_state = db.query(EthBotState).order_by(desc(EthBotState.timestamp)).first()
    has_prev_grid = False
    active_grid = grid
    if prev_state and prev_state.active_grid_levels:
        try:
            stored = json.loads(prev_state.active_grid_levels)
            if stored.get("buy_levels") or stored.get("sell_levels"):
                active_grid = stored
                amount_usd = stored.get("amount_per_level", amount_usd)
                has_prev_grid = True
        except (json.JSONDecodeError, TypeError):
            pass

    candle_low = last_candle.low
    candle_high = last_candle.high
    bought_count = 0

    # Inventory cap: costo total de la posición actual
    inv_cost = inv * avg_cost if inv > 0 and avg_cost > 0 else 0.0
    max_inv_cost = capital * max_inv_cost_pct if capital > 0 else float("inf")

    if not has_prev_grid:
        buy_levels = active_grid.get("buy_levels", [])
        if buy_levels and amount_usd > 0 and inv_cost < max_inv_cost:
            top_buy = max(buy_levels)
            eth = amount_usd / top_buy
            _record_trade(db, "buy", top_buy, eth, amount_usd, 0.0, "grid", dry_run)
            logger.info("[ETH] Grid INICIAL: compra en nivel más cercano %.2f", top_buy)
            inv_cost += amount_usd
            bought_count += 1
    else:
        # Multi-fill: comprar en TODOS los niveles que el low de la vela tocó
        for level in sorted(active_grid.get("buy_levels", []), reverse=True):
            if inv_cost >= max_inv_cost:
                logger.info("[ETH] Inventory cap alcanzado ($%.0f >= $%.0f), saltando compras",
                            inv_cost, max_inv_cost)
                break
            if candle_low <= level and amount_usd > 0:
                eth = amount_usd / level
                _record_trade(db, "buy", level, eth, amount_usd, 0.0, "grid", dry_run)
                logger.info("[ETH] Grid BUY: low=%.2f tocó nivel %.2f", candle_low, level)
                inv_cost += amount_usd
                bought_count += 1

    if bought_count > 0:
        inv, avg_cost = _inventory_and_cost(db, dry_run)

    # Multi-fill: vender en TODOS los niveles que el high de la vela tocó
    sell_count = 0
    for level in sorted(active_grid.get("sell_levels", [])):
        if candle_high >= level and inv > 0:
            eth = min(amount_usd / level, inv)
            pnl = (level - avg_cost) * eth
            _record_trade(db, "sell", level, eth, eth * level, pnl, "grid", dry_run)
            logger.info("[ETH] Grid SELL: high=%.2f tocó nivel %.2f", candle_high, level)
            inv -= eth
            sell_count += 1
            if inv <= 0:
                break

    if bought_count > 0 or sell_count > 0:
        logger.info("[ETH] Grid tick: %d compras, %d ventas", bought_count, sell_count)


def _execute_live(db, decision, client) -> dict | None:
    """Ejecuta orden live. Devuelve grid actualizado si fue acción grid."""
    pair = config.TRADING_PAIR
    inv, avg_cost = _inventory_and_cost(db, dry_run=False)
    price = decision.current_price

    if decision.action == "sell" and inv > 0:
        try:
            order = client.place_market_sell(pair, inv)
            eth = order.get("eth_amount", inv)
            fill_price = order.get("price", price) or price
            pnl = (fill_price - avg_cost) * eth
            _record_trade(db, "sell", fill_price, eth, eth * fill_price, pnl,
                          decision.strategy, dry_run=False,
                          order_id=order.get("order_id"), status=order.get("status", "filled"))
        except Exception as exc:
            logger.exception("[ETH] Error en venta live: %s", exc)
        return None

    if decision.action == "buy" and decision.amount_usd > 0:
        try:
            quote_amount = _usd_to_quote(client, pair, decision.amount_usd)
            order = client.place_market_buy(pair, quote_amount)
            eth = order.get("eth_amount", 0.0) or (decision.amount_usd / price if price else 0.0)
            _record_trade(db, "buy", order.get("price", price) or price, eth,
                          decision.amount_usd, 0.0, decision.strategy, dry_run=False,
                          order_id=order.get("order_id"), status=order.get("status", "filled"))
        except Exception as exc:
            logger.exception("[ETH] Error en compra live: %s", exc)
        return None

    if decision.action == "grid" and decision.grid:
        return _execute_live_grid(db, decision, client, pair, inv, avg_cost)
    return None


def _get_exchange_balances(client) -> tuple[float, float]:
    """Lee saldos reales del exchange: (usdt_available, eth_available)."""
    balances = client.get_balances()
    usdt = 0.0
    eth = 0.0
    for coin, bal in balances.items():
        c = coin.lower()
        if c in ("usdt", "usd"):
            usdt = bal.get("available", 0)
        elif c == "eth":
            eth = bal.get("available", 0)
    return usdt, eth


def _execute_live_grid(db, decision, client, pair: str, inv: float, avg_cost: float) -> dict | None:
    """
    Grid live con mecánica correcta:
    - Niveles ESTABLES (no se recalculan cada tick, solo si el precio sale del rango)
    - Fill tracking: nivel operado se remueve del grid
    - Niveles recíprocos: compra en X → venta en X+step, venta en X → compra en X-step
    - Sizing basado en saldo real del exchange
    """
    fresh_grid = decision.grid
    if not fresh_grid:
        return None

    current_price = decision.current_price

    # --- Cargar grid guardado o crear nuevo ---
    prev_state = db.query(EthBotState).filter(
        EthBotState.dry_run == 0
    ).order_by(desc(EthBotState.timestamp)).first()

    active_grid = None
    if prev_state and prev_state.active_grid_levels:
        try:
            stored = json.loads(prev_state.active_grid_levels)
            all_lvls = stored.get("buy_levels", []) + stored.get("sell_levels", [])
            if all_lvls:
                grid_low, grid_high = min(all_lvls), max(all_lvls)
                grid_range = grid_high - grid_low
                buffer = grid_range * 0.3
                if grid_low - buffer <= current_price <= grid_high + buffer:
                    active_grid = stored
                else:
                    logger.info("[ETH] Precio $%.2f fuera del grid [%.2f-%.2f], recalculando",
                                current_price, grid_low, grid_high)
        except (json.JSONDecodeError, TypeError):
            pass

    if active_grid is None:
        # Crear grid UNIFORME centrado en precio actual
        range_pct = decision.dynamic_range_pct or 0.05
        half = range_pct / 2.0
        lower = current_price * (1 - half)
        upper = current_price * (1 + half)
        n_levels = len(fresh_grid.get("buy_levels", [])) + len(fresh_grid.get("sell_levels", []))
        if n_levels < 3:
            n_levels = 5
        step = (upper - lower) / (n_levels - 1) if n_levels > 1 else (upper - lower)
        buy_levels = []
        sell_levels = []
        for i in range(n_levels):
            level = round(lower + step * i, 2)
            if level < current_price - 1:
                buy_levels.append(level)
            elif level > current_price + 1:
                sell_levels.append(level)
        # Al recentrar nunca vender el inventario existente por debajo de costo + fees
        if inv > 0 and avg_cost > 0:
            min_sell = round(avg_cost * (1 + 2 * config.TAKER_FEE_PCT + config.GRID_MIN_MARGIN_PCT), 2)
            sell_levels = [max(lvl, min_sell) for lvl in sell_levels]
        active_grid = {
            "buy_levels": buy_levels, "sell_levels": sell_levels,
            "grid_step": round(step, 2),
            "amount_per_level": fresh_grid.get("amount_per_level", 10),
        }
        logger.info("[ETH] Grid NUEVO: %d buy + %d sell, step=$%.2f, rango=%.1f%%",
                    len(buy_levels), len(sell_levels), step, range_pct * 100)

    buy_levels = list(active_grid.get("buy_levels", []))
    sell_levels = list(active_grid.get("sell_levels", []))
    grid_step = active_grid.get("grid_step", 0)

    # Calcular step si no está guardado
    if grid_step <= 0:
        all_sorted = sorted(set(buy_levels + sell_levels))
        if len(all_sorted) >= 2:
            steps = [all_sorted[i + 1] - all_sorted[i] for i in range(len(all_sorted) - 1)]
            grid_step = sum(steps) / len(steps)

    usdt_available, eth_available = _get_exchange_balances(client)
    MIN_ORDER_USD = 1.0
    no_position = eth_available < 0.000001 and inv < 0.000001

    # Sizing: dividir saldo real entre niveles pendientes
    n_pending = len(buy_levels)
    spend_per_level = round(usdt_available / n_pending, 2) if n_pending > 0 and usdt_available >= MIN_ORDER_USD else 0

    bought_count = 0
    sold_count = 0
    # (nivel, precio_real_de_ejecución)
    filled_buys: list[tuple[float, float]] = []
    filled_sells: list[tuple[float, float]] = []

    # --- COMPRAS ---
    if no_position and buy_levels and usdt_available >= MIN_ORDER_USD:
        # Entrada inicial: comprar al nivel más cercano al precio
        top_buy = max(buy_levels)
        spend = min(spend_per_level, usdt_available)
        try:
            quote = _usd_to_quote(client, pair, spend)
            order = client.place_market_buy(pair, quote)
            fill_price = order.get("price") or current_price
            eth_bought = order.get("eth_amount", 0.0) or (spend / fill_price)
            _record_trade(db, "buy", fill_price, eth_bought, spend, 0.0, "grid",
                          dry_run=False, order_id=order.get("order_id"),
                          status=order.get("status", "filled"))
            usdt_available -= spend
            bought_count += 1
            filled_buys.append((top_buy, fill_price))
            logger.info("[ETH] Grid INICIAL: compra $%.2f @ %.2f", spend, fill_price)
        except Exception as exc:
            logger.error("[ETH] Error compra grid inicial: %s", exc)
    else:
        # Solo se compra si el precio ACTUAL está en/bajo el nivel: comprar por una mecha
        # ya pasada ejecuta más arriba del nivel y el recíproco no cubre las fees.
        for level in sorted(buy_levels, reverse=True):
            if usdt_available < MIN_ORDER_USD:
                break
            if current_price <= level:
                spend = min(spend_per_level, usdt_available)
                if spend < MIN_ORDER_USD:
                    break
                try:
                    quote = _usd_to_quote(client, pair, spend)
                    order = client.place_market_buy(pair, quote)
                    fill_price = order.get("price") or current_price
                    eth_bought = order.get("eth_amount", 0.0) or (spend / fill_price)
                    _record_trade(db, "buy", fill_price, eth_bought, spend, 0.0, "grid",
                                  dry_run=False, order_id=order.get("order_id"),
                                  status=order.get("status", "filled"))
                    usdt_available -= spend
                    bought_count += 1
                    filled_buys.append((level, fill_price))
                    logger.info("[ETH] Grid BUY: precio=%.2f <= nivel %.2f ($%.2f)",
                                current_price, level, spend)
                except Exception as exc:
                    logger.error("[ETH] Error compra grid %.2f: %s", level, exc)
                    break

    if bought_count > 0:
        usdt_available, eth_available = _get_exchange_balances(client)
        inv, avg_cost = _inventory_and_cost(db, dry_run=False)

    # --- VENTAS ---
    n_sell_pending = len(sell_levels)
    for level in sorted(sell_levels):
        if eth_available <= 1e-8:
            break
        eth_per_level = eth_available / max(1, n_sell_pending)
        eth_to_sell = min(eth_per_level, eth_available)
        if current_price >= level and eth_to_sell > 1e-8:
            try:
                order = client.place_market_sell(pair, eth_to_sell)
                eth_sold = order.get("eth_amount") or eth_to_sell
                fill_price = order.get("price") or current_price
                pnl = (fill_price - avg_cost) * eth_sold if avg_cost > 0 else 0.0
                _record_trade(db, "sell", fill_price, eth_sold, eth_sold * fill_price, pnl,
                              "grid", dry_run=False, order_id=order.get("order_id"),
                              status=order.get("status", "filled"))
                eth_available -= eth_sold
                sold_count += 1
                filled_sells.append((level, fill_price))
                logger.info("[ETH] Grid SELL: precio=%.2f >= nivel %.2f (pnl=$%.4f)",
                            current_price, level, pnl)
            except Exception as exc:
                logger.error("[ETH] Error venta grid %.2f: %s", level, exc)
                break

    # --- Actualizar grid: remover fills, agregar recíprocos desde el precio real ---
    for lvl, fill in filled_buys:
        if lvl in buy_levels:
            buy_levels.remove(lvl)
        if grid_step > 0:
            sell_target = round(fill + grid_step, 2)
            if sell_target not in sell_levels:
                sell_levels.append(sell_target)

    for lvl, fill in filled_sells:
        if lvl in sell_levels:
            sell_levels.remove(lvl)
        if grid_step > 0:
            buy_target = round(fill - grid_step, 2)
            if buy_target not in buy_levels:
                buy_levels.append(buy_target)

    if bought_count > 0 or sold_count > 0:
        logger.info("[ETH] Grid tick: %d compras, %d ventas (quedan %d buy + %d sell)",
                    bought_count, sold_count, len(buy_levels), len(sell_levels))
    else:
        logger.info("[ETH] Grid: esperando (USDT=$%.2f, ETH=%.6f, %d buy + %d sell)",
                    usdt_available, eth_available, len(buy_levels), len(sell_levels))

    return {
        "buy_levels": sorted(buy_levels),
        "sell_levels": sorted(sell_levels),
        "grid_step": grid_step,
        "amount_per_level": active_grid.get("amount_per_level", 10),
    }


def set_live_baseline(db, usdt: float, eth: float, price: float, since: datetime | None = None) -> dict:
    """Punto de partida para medir resultados reales (se reinicia tras depósitos/retiros)."""
    baseline = {
        "since": (since or datetime.utcnow()).isoformat(),
        "usdt": round(usdt, 8), "eth": round(eth, 8), "price": round(price, 2),
        "value": round(usdt + eth * price, 4),
    }
    set_setting(db, "live_baseline", json.dumps(baseline))
    return baseline


def _usd_to_quote(client, pair: str, amount_usd: float) -> float:
    if pair.endswith("_usd") or pair.endswith("_usdt"):
        return round(amount_usd, 2)
    try:
        fiat = pair.split("_")[1]
        ticker = client.get_ticker(f"usdt_{fiat}")
        rate = float(ticker.get("last", 0)) or config.MOCK_USDT_ARS_RATE
    except Exception:
        rate = config.MOCK_USDT_ARS_RATE
    return round(amount_usd * rate, 2)


# ── Tick principal ────────────────────────────────────────────────────────────

def run_eth_tick(manual: bool = False) -> dict:
    from app.exchanges.factory import get_exchange_client

    engine = EthStrategyEngine()
    client = get_exchange_client()

    with db_session() as db:
        dry_run = _is_dry_run(db)
        capital_setting = get_setting(db, "capital", None)
        base_capital = float(capital_setting) if capital_setting else config.ETH_CAPITAL_USD
        levels = int(get_setting(db, "levels", str(config.ETH_GRID_LEVELS)))
        range_pct = float(get_setting(db, "range_pct", str(config.ETH_GRID_RANGE_PCT)))

        # 1. Velas
        candles = _get_candles(client)
        if not candles or len(candles) < 30:
            logger.warning("[ETH] Velas insuficientes (%d)", len(candles) if candles else 0)
            return {"ok": False, "reason": "velas_insuficientes"}
        _persist_candles(db, candles)

        # 2. PnL, inventario y drawdown
        daily_pnl, total_pnl = _pnl_summary(db, dry_run)
        inv, avg_cost = _inventory_and_cost(db, dry_run)
        capital = round(base_capital + total_pnl, 4)
        start_of_day_capital = base_capital + (total_pnl - daily_pnl)
        drawdown_pct = 0.0
        if daily_pnl < 0 and start_of_day_capital > 0:
            drawdown_pct = abs(daily_pnl) / start_of_day_capital

        # Cooldown de profit-take: buscar último trade profit_take
        last_pt = db.query(EthTrade).filter(
            EthTrade.strategy == "profit_take",
            EthTrade.dry_run == (1 if dry_run else 0),
        ).order_by(desc(EthTrade.timestamp)).first()
        hours_since_pt = 999.0
        if last_pt and last_pt.timestamp:
            elapsed = (datetime.utcnow() - last_pt.timestamp).total_seconds()
            hours_since_pt = elapsed / 3600.0

        cfg = config.get_settings_dict()
        cfg.update({
            "capital": capital, "levels": levels, "range_pct": range_pct,
            "daily_drawdown_pct": drawdown_pct,
            "inventory_eth": inv,
            "avg_cost": avg_cost,
            "base_capital": base_capital,
            "hours_since_last_profit_take": hours_since_pt,
        })

        # 3. Decisión (ahora con contexto de inventario)
        decision = engine.decide(candles, capital, cfg)
        logger.info("[ETH] Decisión: régimen=%s acción=%s (%s)",
                    decision.regime, decision.action, decision.reason)

        # 4-5. Ejecución
        if dry_run:
            _simulate(db, decision, dry_run=True)
        else:
            logger.warning("[ETH] MODO LIVE — ejecutando órdenes reales")
            live_grid = _execute_live(db, decision, client)
            if live_grid is not None:
                decision.grid = live_grid
        db.flush()

        # Recalcular PnL tras la operación
        daily_pnl, total_pnl = _pnl_summary(db, dry_run)
        inv, avg_cost = _inventory_and_cost(db, dry_run)
        unrealized_pnl = (decision.current_price - avg_cost) * inv if inv > 0 and avg_cost > 0 else 0.0
        capital = round(base_capital + total_pnl + unrealized_pnl, 4)
        if not dry_run:
            # En LIVE el capital es el valor real del portfolio en el exchange
            try:
                usdt_bal, eth_bal = _get_exchange_balances(client)
                capital = round(usdt_bal + eth_bal * decision.current_price, 4)
                if get_setting(db, "live_baseline") is None:
                    set_live_baseline(db, usdt_bal, eth_bal, decision.current_price)
            except Exception as exc:
                logger.warning("[ETH] No se pudo leer saldo para valuar portfolio: %s", exc)

        # 6. Guardar estado
        grid_json = json.dumps(decision.grid) if decision.grid else None
        state = EthBotState(
            timestamp=datetime.utcnow(), regime=decision.regime,
            adx_value=decision.adx, current_price=decision.current_price,
            daily_pnl=round(daily_pnl + unrealized_pnl, 4),
            total_pnl=round(total_pnl + unrealized_pnl, 4),
            drawdown_pct=round(drawdown_pct * 100, 4),
            active_grid_levels=grid_json, capital=capital,
            dry_run=1 if dry_run else 0,
        )
        db.add(state)

        summary = {
            "ok": True, "regime": decision.regime, "action": decision.action,
            "adx": decision.adx, "price": decision.current_price,
            "capital": capital,
            "daily_pnl": round(daily_pnl + unrealized_pnl, 4),
            "total_pnl": round(total_pnl + unrealized_pnl, 4),
            "dry_run": dry_run, "reason": decision.reason,
            "atr": decision.atr, "ema50": decision.ema50,
            "dynamic_range_pct": decision.dynamic_range_pct,
            "inventory_eth": inv,
        }

    cache_set(_STATE_CACHE_KEY, summary, config.ETH_TICK_MINUTES * 60 + 60)
    return summary
