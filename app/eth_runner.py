"""
Runner del bot ETH — ejecuta un ciclo de la estrategia DGT v2.

Flujo de un tick:
  1. Descarga velas OHLCV de 15m (Binance) o usa caché Redis.
  2. Calcula indicadores (ADX, ATR, EMA) y detecta régimen.
  3. Llama a EthStrategyEngine.decide() con contexto de inventario.
  4. Ejecuta la operación: contra Bitso (live) o contra PaperExchange (simulador),
     con el mismo código y las mismas comisiones.
  5. Guarda estado y actualiza caché.

Contabilidad: cada trade guarda el fill real (precio promedio, ETH y USDT netos,
comisión). El PnL de las ventas es neto de ambas comisiones.

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
from app.exchanges.paper_exchange import PaperExchange
from app.models import EthBotState, EthCandle, EthTrade
from app.strategy_eth import EthStrategyEngine
from app.trading_math import floor_decimals, min_sell_price

logger = logging.getLogger(__name__)

_CANDLES_CACHE_KEY = "eth:candles:15m"
_STATE_CACHE_KEY = "eth:state"
_MIN_ORDER_USD = 1.0


# ── Helpers de estado / PnL ───────────────────────────────────────────────────

def _today_start_utc() -> datetime:
    now = datetime.utcnow()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _is_dry_run(db) -> bool:
    val = get_setting(db, "dry_run", None)
    if val is None:
        return config.DRY_RUN
    return val.strip().lower() in ("true", "1", "yes")


def _mode_since(db, dry_run: bool) -> datetime | None:
    """Inicio de la simulación vigente (los trades simulados anteriores no cuentan). En live: todo."""
    if not dry_run:
        return None
    raw = get_setting(db, "sim_baseline")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(json.loads(raw)["since"])
    except (ValueError, KeyError, TypeError):
        return None


def _mode_trades(db, dry_run: bool):
    query = db.query(EthTrade).filter(EthTrade.dry_run == (1 if dry_run else 0))
    since = _mode_since(db, dry_run)
    return query.filter(EthTrade.timestamp >= since) if since else query


def _inventory_and_cost(db, dry_run: bool) -> tuple[float, float]:
    """
    (ETH netos en cartera, costo promedio móvil por ETH neto).
    El costo incluye la comisión de compra porque se registran USDT gastados y ETH netos recibidos.
    """
    trades = _mode_trades(db, dry_run).order_by(EthTrade.timestamp, EthTrade.id).all()
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
    """Devuelve (daily_pnl, total_pnl) realizados y netos de comisiones."""
    day_start = _today_start_utc()
    total = _mode_trades(db, dry_run).all()
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

def _fill_or_estimate(order: dict, side: str, requested: float, ref_price: float) -> dict:
    """
    Datos de ejecución a registrar. Si el exchange confirmó el fill se usan los reales;
    si no, se estiman con las comisiones configuradas y el trade queda 'unconfirmed'.
    `requested` = USDT a gastar (compra) o ETH a vender (venta).
    """
    if order.get("confirmed"):
        return {"price": order["price"], "eth": order["eth_net"], "usdt": order["usdt_net"],
                "fee_amount": order.get("fee_amount"), "fee_currency": order.get("fee_currency"),
                "fee_usd": order.get("fee_usd", 0.0), "status": "completed"}
    if side == "buy":
        eth_gross = requested / ref_price
        fee_eth = eth_gross * config.BUY_FEE_PCT
        return {"price": ref_price, "eth": eth_gross - fee_eth, "usdt": requested,
                "fee_amount": fee_eth, "fee_currency": "eth", "fee_usd": fee_eth * ref_price,
                "status": "unconfirmed"}
    gross = requested * ref_price
    fee_usd = gross * config.SELL_FEE_PCT
    return {"price": ref_price, "eth": requested, "usdt": gross - fee_usd,
            "fee_amount": fee_usd, "fee_currency": "usdt", "fee_usd": fee_usd,
            "status": "unconfirmed"}


def _record_trade(db, side: str, fill: dict, pnl: float, strategy: str, dry_run: bool,
                  order_id: str | None = None) -> None:
    db.add(EthTrade(
        timestamp=datetime.utcnow(), side=side, price=fill["price"],
        amount_eth=round(fill["eth"], 8), amount_usd=round(fill["usdt"], 8),
        pnl=round(pnl, 6), strategy=strategy, status=fill["status"], order_id=order_id,
        fee_amount=fill["fee_amount"], fee_currency=fill["fee_currency"],
        fee_usd=round(fill["fee_usd"], 8), dry_run=1 if dry_run else 0,
    ))
    # La sesión no hace autoflush: sin esto el inventario y los saldos simulados
    # recalculados en el mismo tick no verían este trade.
    db.flush()
    logger.info("[ETH] Trade %s registrado: %.8f ETH netos @ %.2f, $%.4f netos, fee $%.4f, pnl neto $%.4f (%s, %s)",
                side, fill["eth"], fill["price"], fill["usdt"], fill["fee_usd"], pnl,
                fill["status"], "SIM" if dry_run else "LIVE")


def _place_buy(db, executor, usdt_amount: float, ref_price: float, strategy: str, dry_run: bool) -> dict:
    """Compra a mercado y registra el fill. Devuelve el fill (price, eth, usdt, fee_usd...)."""
    usdt_amount = floor_decimals(usdt_amount, 2)
    order = executor.place_market_buy(config.TRADING_PAIR, _usd_to_quote(executor, config.TRADING_PAIR, usdt_amount))
    fill = _fill_or_estimate(order, "buy", usdt_amount, ref_price)
    _record_trade(db, "buy", fill, 0.0, strategy, dry_run, order.get("order_id"))
    return fill


def _place_sell(db, executor, eth_amount: float, ref_price: float, avg_cost: float,
                strategy: str, dry_run: bool) -> dict:
    """Vende a mercado y registra el fill con PnL neto (USDT recibidos − costo con comisión de compra)."""
    eth_amount = floor_decimals(eth_amount, 8)
    order = executor.place_market_sell(config.TRADING_PAIR, eth_amount)
    fill = _fill_or_estimate(order, "sell", eth_amount, ref_price)
    fill["pnl"] = fill["usdt"] - avg_cost * fill["eth"] if avg_cost > 0 else 0.0
    _record_trade(db, "sell", fill, fill["pnl"], strategy, dry_run, order.get("order_id"))
    return fill


def _execute(db, decision, executor, dry_run: bool) -> dict | None:
    """Ejecuta la decisión (live contra Bitso o simulada contra PaperExchange). Devuelve el grid actualizado."""
    pair = config.TRADING_PAIR
    inv, avg_cost = _inventory_and_cost(db, dry_run)
    price = decision.current_price

    if decision.action == "sell" and inv > 0:
        try:
            _, eth_available = _get_exchange_balances(executor)
            wanted = decision.amount_usd / price if decision.amount_usd and price else inv
            # Nunca más de lo que la estrategia pidió, de lo registrado, ni de lo disponible en el exchange
            qty = floor_decimals(min(wanted, inv, eth_available), 8)
            if qty > 0:
                _place_sell(db, executor, qty, price, avg_cost, decision.strategy, dry_run)
        except Exception as exc:
            logger.exception("[ETH] Error en venta: %s", exc)
        return None

    if decision.action == "buy" and decision.amount_usd > 0:
        try:
            usdt_available, _ = _get_exchange_balances(executor)
            spend = floor_decimals(min(decision.amount_usd, usdt_available), 2)
            if spend >= _MIN_ORDER_USD:
                _place_buy(db, executor, spend, price, decision.strategy, dry_run)
        except Exception as exc:
            logger.exception("[ETH] Error en compra: %s", exc)
        return None

    if decision.action == "grid" and decision.grid:
        return _execute_grid(db, decision, executor, pair, inv, avg_cost, dry_run)
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


def _execute_grid(db, decision, executor, pair: str, inv: float, avg_cost: float,
                  dry_run: bool) -> dict | None:
    """
    Grid con mecánica correcta (idéntica en live y simulador):
    - Niveles ESTABLES (no se recalculan cada tick, solo si el precio sale del rango)
    - Fill tracking: nivel operado se remueve del grid
    - Niveles recíprocos: compra en X → venta en X+step, venta en X → compra en X-step
    - Sizing basado en el saldo real del exchange (o del paper exchange)
    """
    fresh_grid = decision.grid
    if not fresh_grid:
        return None

    current_price = decision.current_price

    # --- Cargar grid guardado o crear nuevo ---
    prev_query = db.query(EthBotState).filter(EthBotState.dry_run == (1 if dry_run else 0))
    since = _mode_since(db, dry_run)
    if since:
        prev_query = prev_query.filter(EthBotState.timestamp >= since)
    prev_state = prev_query.order_by(desc(EthBotState.timestamp)).first()

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
        # Al recentrar nunca vender el inventario existente por debajo de costo + fees + margen.
        # avg_cost ya incluye la comisión de compra (ETH neto recibido), por eso buy_fee=0.
        if inv > 0 and avg_cost > 0:
            min_sell = round(min_sell_price(avg_cost, config.GRID_MIN_MARGIN_PCT, buy_fee=0.0), 2)
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

    usdt_available, eth_available = _get_exchange_balances(executor)
    # Compra automática a mercado SOLO en la primera entrada del bot. Después de vender toda la
    # posición se recompra únicamente por la lógica normal del grid (precio ≤ nivel recíproco).
    is_first_entry = (eth_available < 0.000001 and inv < 0.000001
                      and _mode_trades(db, dry_run).first() is None)

    # Sizing: dividir saldo real entre niveles pendientes (floor: nunca pedir más que el saldo)
    n_pending = len(buy_levels)
    spend_per_level = (floor_decimals(usdt_available / n_pending, 2)
                       if n_pending > 0 and usdt_available >= _MIN_ORDER_USD else 0)

    bought_count = 0
    sold_count = 0
    # (nivel, precio_real_de_ejecución)
    filled_buys: list[tuple[float, float]] = []
    filled_sells: list[tuple[float, float]] = []

    # --- COMPRAS ---
    if is_first_entry and buy_levels and usdt_available >= _MIN_ORDER_USD:
        # Entrada inicial: comprar al nivel más cercano al precio
        top_buy = max(buy_levels)
        spend = floor_decimals(min(spend_per_level, usdt_available), 2)
        try:
            fill = _place_buy(db, executor, spend, current_price, "grid", dry_run)
            usdt_available -= fill["usdt"]
            bought_count += 1
            filled_buys.append((top_buy, fill["price"]))
            logger.info("[ETH] Grid INICIAL: compra $%.2f @ %.2f", fill["usdt"], fill["price"])
        except Exception as exc:
            logger.error("[ETH] Error compra grid inicial: %s", exc)
    else:
        # Solo se compra si el precio ACTUAL está en/bajo el nivel: comprar por una mecha
        # ya pasada ejecuta más arriba del nivel y el recíproco no cubre las fees.
        for level in sorted(buy_levels, reverse=True):
            if usdt_available < _MIN_ORDER_USD:
                break
            if current_price <= level:
                spend = floor_decimals(min(spend_per_level, usdt_available), 2)
                if spend < _MIN_ORDER_USD:
                    break
                try:
                    fill = _place_buy(db, executor, spend, current_price, "grid", dry_run)
                    usdt_available -= fill["usdt"]
                    bought_count += 1
                    filled_buys.append((level, fill["price"]))
                    logger.info("[ETH] Grid BUY: precio=%.2f <= nivel %.2f ($%.2f @ %.2f)",
                                current_price, level, fill["usdt"], fill["price"])
                except Exception as exc:
                    logger.error("[ETH] Error compra grid %.2f: %s", level, exc)
                    break

    if bought_count > 0:
        usdt_available, eth_available = _get_exchange_balances(executor)
        inv, avg_cost = _inventory_and_cost(db, dry_run)

    # --- VENTAS ---
    n_sell_pending = len(sell_levels)
    for level in sorted(sell_levels):
        if eth_available <= 1e-8:
            break
        eth_per_level = eth_available / max(1, n_sell_pending)
        eth_to_sell = floor_decimals(min(eth_per_level, eth_available), 8)
        if current_price >= level and eth_to_sell > 1e-8:
            try:
                fill = _place_sell(db, executor, eth_to_sell, current_price, avg_cost, "grid", dry_run)
                eth_available -= fill["eth"]
                sold_count += 1
                filled_sells.append((level, fill["price"]))
                logger.info("[ETH] Grid SELL: precio=%.2f >= nivel %.2f (pnl neto=$%.4f)",
                            current_price, level, fill["pnl"])
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


def set_sim_baseline(db, usdt: float, since: datetime | None = None) -> dict:
    """Reinicia la billetera simulada: los trades simulados previos dejan de contar."""
    baseline = {"since": (since or datetime.utcnow()).isoformat(), "usdt": round(usdt, 8)}
    set_setting(db, "sim_baseline", json.dumps(baseline))
    return baseline


def _sim_start_usdt(db) -> float:
    capital_setting = get_setting(db, "capital", None)
    return float(capital_setting) if capital_setting else config.ETH_CAPITAL_USD


def _usd_to_quote(client, pair: str, amount_usd: float) -> float:
    if pair.endswith("_usd") or pair.endswith("_usdt"):
        return floor_decimals(amount_usd, 2)
    try:
        fiat = pair.split("_")[1]
        ticker = client.get_ticker(f"usdt_{fiat}")
        rate = float(ticker.get("last", 0)) or config.MOCK_USDT_ARS_RATE
    except Exception:
        rate = config.MOCK_USDT_ARS_RATE
    return floor_decimals(amount_usd * rate, 2)


# ── Tick principal ────────────────────────────────────────────────────────────

def _paper_exchange(db, price: float) -> PaperExchange:
    if get_setting(db, "sim_baseline") is None:
        set_sim_baseline(db, _sim_start_usdt(db))
    baseline = json.loads(get_setting(db, "sim_baseline"))
    return PaperExchange(db, float(baseline["usdt"]), datetime.fromisoformat(baseline["since"]), price)


def _start_of_day_value(db, dry_run: bool, portfolio_now: float, daily_pnl: float) -> float:
    """Valor del portfolio al primer chequeo del día (base del límite de drawdown diario)."""
    since = max(_today_start_utc(), _mode_since(db, dry_run) or datetime.min)
    if not dry_run:
        raw = get_setting(db, "live_baseline")
        if raw:
            try:
                since = max(since, datetime.fromisoformat(json.loads(raw)["since"]))
            except (ValueError, KeyError, TypeError):
                pass
    first = (db.query(EthBotState.capital)
             .filter(EthBotState.dry_run == (1 if dry_run else 0), EthBotState.timestamp >= since,
                     EthBotState.capital.isnot(None))
             .order_by(EthBotState.timestamp).first())
    return first[0] if first and first[0] else portfolio_now - daily_pnl


def run_eth_tick(manual: bool = False) -> dict:
    from app.exchanges.factory import get_exchange_client

    engine = EthStrategyEngine()
    client = get_exchange_client()

    with db_session() as db:
        dry_run = _is_dry_run(db)

        # 1. Velas
        candles = _get_candles(client)
        if not candles or len(candles) < 30:
            logger.warning("[ETH] Velas insuficientes (%d)", len(candles) if candles else 0)
            return {"ok": False, "reason": "velas_insuficientes"}
        _persist_candles(db, candles)

        # SIMULADOR: las órdenes van a PaperExchange, nunca al exchange real
        executor = _paper_exchange(db, candles[-1]["close"]) if dry_run else client
        summary = execute_tick(db, engine, candles, executor, dry_run)

    cache_set(_STATE_CACHE_KEY, summary, config.ETH_TICK_MINUTES * 60 + 60)
    return summary


def execute_tick(db, engine, candles: list[dict], executor, dry_run: bool) -> dict:
    """Un ciclo sobre velas ya obtenidas. executor = Bitso (live) o PaperExchange (simulador)."""
    levels = int(get_setting(db, "levels", str(config.ETH_GRID_LEVELS)))
    range_pct = float(get_setting(db, "range_pct", str(config.ETH_GRID_RANGE_PCT)))
    price = candles[-1]["close"]

    # 2. Portfolio real, PnL, inventario y drawdown
    usdt_bal, eth_bal = _get_exchange_balances(executor)
    portfolio = round(usdt_bal + eth_bal * price, 4)
    if not dry_run and get_setting(db, "live_baseline") is None:
        set_live_baseline(db, usdt_bal, eth_bal, price)
    daily_pnl, total_pnl = _pnl_summary(db, dry_run)
    inv, avg_cost = _inventory_and_cost(db, dry_run)
    start_of_day = _start_of_day_value(db, dry_run, portfolio, daily_pnl)
    drawdown_pct = abs(daily_pnl) / start_of_day if daily_pnl < 0 and start_of_day > 0 else 0.0

    # Cooldown de profit-take: buscar último trade profit_take
    last_pt = (_mode_trades(db, dry_run).filter(EthTrade.strategy == "profit_take")
               .order_by(desc(EthTrade.timestamp)).first())
    hours_since_pt = 999.0
    if last_pt and last_pt.timestamp:
        hours_since_pt = (datetime.utcnow() - last_pt.timestamp).total_seconds() / 3600.0

    cfg = config.get_settings_dict()
    cfg.update({
        "capital": portfolio, "levels": levels, "range_pct": range_pct,
        "daily_drawdown_pct": drawdown_pct,
        "inventory_eth": inv,
        "avg_cost": avg_cost,
        "base_capital": portfolio,
        "hours_since_last_profit_take": hours_since_pt,
    })

    # 3. Decisión
    decision = engine.decide(candles, portfolio, cfg)
    logger.info("[ETH] Decisión: régimen=%s acción=%s (%s)",
                decision.regime, decision.action, decision.reason)

    # 4-5. Ejecución
    if not dry_run:
        logger.warning("[ETH] MODO LIVE — ejecutando órdenes reales")
    new_grid = _execute(db, decision, executor, dry_run)
    if new_grid is not None:
        decision.grid = new_grid
    db.flush()

    # Recalcular PnL (neto) y valor del portfolio tras la operación
    daily_pnl, total_pnl = _pnl_summary(db, dry_run)
    inv, avg_cost = _inventory_and_cost(db, dry_run)
    unrealized_pnl = (decision.current_price - avg_cost) * inv if inv > 0 and avg_cost > 0 else 0.0
    try:
        usdt_bal, eth_bal = _get_exchange_balances(executor)
        capital = round(usdt_bal + eth_bal * decision.current_price, 4)
    except Exception as exc:
        logger.warning("[ETH] No se pudo leer saldo para valuar portfolio: %s", exc)
        capital = portfolio

    # 6. Guardar estado
    grid_json = json.dumps(decision.grid) if decision.grid else None
    db.add(EthBotState(
        timestamp=datetime.utcnow(), regime=decision.regime,
        adx_value=decision.adx, current_price=decision.current_price,
        daily_pnl=round(daily_pnl + unrealized_pnl, 4),
        total_pnl=round(total_pnl + unrealized_pnl, 4),
        drawdown_pct=round(drawdown_pct * 100, 4),
        active_grid_levels=grid_json, capital=capital,
        dry_run=1 if dry_run else 0,
    ))
    db.flush()

    return {
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
