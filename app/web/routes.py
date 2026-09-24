"""
Rutas del dashboard web del ETH bot — protegidas por AuthMiddleware.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc

from app import config
from app.database import db_session, get_setting, set_setting
from app.models import Auth, EthBotState, EthCandle, EthTrade

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _localtime(dt):
    if dt is None:
        return None
    try:
        import pytz
        tz = pytz.timezone(config.TIMEZONE)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(tz)
    except Exception:
        return dt


templates.env.filters["localtime"] = _localtime


# ── Helpers ───────────────────────────────────────────────────────────────────

def _bool_setting(db, key: str, default: bool) -> bool:
    val = get_setting(db, key)
    if val is None:
        return default
    return val.strip().lower() in ("true", "1", "yes")


def _base_ctx(request: Request, db) -> dict:
    from app.scheduler import get_next_tick_time
    next_tick = get_next_tick_time()
    exchange_source = get_setting(db, "exchange_client", config.EXCHANGE_CLIENT)
    return {
        "request": request,
        "bot_version": config.BOT_VERSION,
        "bot_status": get_setting(db, "bot_status", config.BOT_STATUS),
        "dry_run": _bool_setting(db, "dry_run", config.DRY_RUN),
        "username": getattr(request.state, "username", ""),
        "next_tick": next_tick.strftime("%d/%m/%Y %H:%M") if next_tick else "—",
        "exchange_source": exchange_source,
        "is_mock": exchange_source == "mock",
        "trading_pair": get_setting(db, "trading_pair", config.TRADING_PAIR),
    }


def _fee_rate(dry_run: bool) -> float:
    # El simulador no modela comisiones; en LIVE se estiman con el fee taker de Bitso
    return 0.0 if dry_run else config.TAKER_FEE_PCT


def _trade_net(t, fee_rate: float) -> tuple[float, float]:
    """(comisión, resultado neto) de un trade. Las compras solo aportan su comisión."""
    fee = (t.amount_usd or 0.0) * fee_rate
    gross = (t.pnl or 0.0) if t.side == "sell" else 0.0
    return fee, gross - fee


def _baseline(db, dry_run: bool) -> dict | None:
    """Punto de partida para medir resultados: {since(datetime UTC), usdt, eth, value}."""
    if dry_run:
        first = db.query(EthBotState).filter(EthBotState.dry_run == 1) \
            .order_by(EthBotState.timestamp).first()
        cap = float(get_setting(db, "capital", str(config.ETH_CAPITAL_USD)))
        return {"since": first.timestamp if first else datetime.utcnow(),
                "usdt": cap, "eth": 0.0, "value": cap}
    raw = get_setting(db, "live_baseline")
    if not raw:
        return None
    try:
        b = json.loads(raw)
        b["since"] = datetime.fromisoformat(b["since"])
        return b
    except (ValueError, KeyError, TypeError):
        return None


# ── Dashboard ─────────────────────────────────────────────────────────────────

@router.get("/")
async def index():
    return RedirectResponse("/dashboard", status_code=302)


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    with db_session() as db:
        ctx = _base_ctx(request, db)
        dry_flag = 1 if ctx["dry_run"] else 0
        last_state = db.query(EthBotState).filter(
            EthBotState.dry_run == dry_flag
        ).order_by(desc(EthBotState.timestamp)).first()
        ctx.update({
            "state": last_state,
            "capital_initial": float(get_setting(db, "capital", str(config.ETH_CAPITAL_USD))),
            "levels": int(get_setting(db, "levels", str(config.ETH_GRID_LEVELS))),
            "range_pct": float(get_setting(db, "range_pct", str(config.ETH_GRID_RANGE_PCT))),
            "auto_refresh_seconds": config.DASHBOARD_AUTO_REFRESH_SECONDS,
        })
    return templates.TemplateResponse("eth_dashboard.html", ctx)


# ── API: status ───────────────────────────────────────────────────────────────

@router.get("/api/eth/status")
async def api_status(request: Request):
    with db_session() as db:
        bot_status = get_setting(db, "bot_status", config.BOT_STATUS)
        dry_run = _bool_setting(db, "dry_run", config.DRY_RUN)
        dry_flag = 1 if dry_run else 0
        state = db.query(EthBotState).filter(
            EthBotState.dry_run == dry_flag
        ).order_by(desc(EthBotState.timestamp)).first()
        capital_initial = float(get_setting(db, "capital", str(config.ETH_CAPITAL_USD)))
        from app.scheduler import get_next_tick_time
        next_tick = get_next_tick_time()
        next_tick = next_tick.isoformat() if next_tick else None
        if not state:
            return JSONResponse({
                "regime": None, "adx": None, "price": None,
                "capital": capital_initial, "capital_initial": capital_initial,
                "daily_pnl": 0.0, "total_pnl": 0.0, "drawdown_pct": 0.0,
                "bot_status": bot_status, "dry_run": dry_run, "updated_at": None,
                "next_tick": next_tick,
            })
        return JSONResponse({
            "regime": state.regime,
            "adx": round(state.adx_value, 2) if state.adx_value is not None else None,
            "price": state.current_price,
            "capital": state.capital,
            "capital_initial": capital_initial,
            "daily_pnl": state.daily_pnl,
            "total_pnl": state.total_pnl,
            "drawdown_pct": state.drawdown_pct,
            "bot_status": bot_status,
            "dry_run": dry_run,
            "updated_at": _localtime(state.timestamp).isoformat() if state.timestamp else None,
            "next_tick": next_tick,
        })


# ── API: trades ───────────────────────────────────────────────────────────────

@router.get("/api/eth/trades")
async def api_trades(request: Request):
    with db_session() as db:
        dry_run = _bool_setting(db, "dry_run", config.DRY_RUN)
        rows = (
            db.query(EthTrade)
            .filter(EthTrade.dry_run == (1 if dry_run else 0))
            .order_by(desc(EthTrade.timestamp))
            .limit(50)
            .all()
        )
        fee_rate = _fee_rate(dry_run)
        trades = []
        for t in rows:
            fee, net = _trade_net(t, fee_rate)
            trades.append({
                "timestamp": _localtime(t.timestamp).strftime("%d/%m %H:%M") if t.timestamp else "",
                "ts_iso": t.timestamp.replace(tzinfo=timezone.utc).isoformat() if t.timestamp else None,
                "side": t.side, "price": t.price,
                "amount_eth": t.amount_eth, "amount_usd": t.amount_usd,
                "pnl": t.pnl, "fee": round(fee, 4), "net": round(net, 4),
                "strategy": t.strategy, "status": t.status,
            })
    return JSONResponse({"trades": trades, "count": len(trades)})


# ── API: rendimiento (hoy / día a día / histórico) ────────────────────────────

@router.get("/api/eth/performance")
async def api_performance(request: Request):
    with db_session() as db:
        dry_run = _bool_setting(db, "dry_run", config.DRY_RUN)
        flag = 1 if dry_run else 0
        base = _baseline(db, dry_run)
        if base is None:
            return JSONResponse({"ready": False, "dry_run": dry_run})
        since = base["since"]
        states = (db.query(EthBotState.timestamp, EthBotState.capital, EthBotState.current_price)
                  .filter(EthBotState.dry_run == flag, EthBotState.timestamp >= since)
                  .order_by(EthBotState.timestamp).all())
        trades = (db.query(EthTrade)
                  .filter(EthTrade.dry_run == flag, EthTrade.timestamp >= since)
                  .order_by(EthTrade.timestamp).all())

    fee_rate = _fee_rate(dry_run)
    days: dict = {}

    def _day(ts):
        d = _localtime(ts).date()
        return days.setdefault(d, {"close_value": None, "eth_price": None, "buys": 0, "sells": 0,
                                   "bot_net": 0.0, "fees": 0.0})

    for ts, cap, price in states:
        if cap is None:
            continue
        day = _day(ts)
        day["close_value"] = cap
        day["eth_price"] = price

    wins = 0
    for t in trades:
        day = _day(t.timestamp)
        fee, net = _trade_net(t, fee_rate)
        day["fees"] += fee
        day["bot_net"] += net
        if t.side == "buy":
            day["buys"] += 1
        else:
            day["sells"] += 1
            wins += 1 if net > 0 else 0

    start_value = base["value"]
    prev = start_value
    rows = []
    for d in sorted(days):
        day = days[d]
        close = day["close_value"] if day["close_value"] is not None else prev
        change = close - prev
        rows.append({
            "date": d.isoformat(), "close_value": round(close, 4), "eth_price": day["eth_price"],
            "change": round(change, 4), "change_pct": round(change / prev * 100, 3) if prev else 0.0,
            "buys": day["buys"], "sells": day["sells"],
            "bot_net": round(day["bot_net"], 4), "fees": round(day["fees"], 4),
        })
        prev = close

    last_price = next((p for _, c, p in reversed(states) if p), base.get("price") or 0.0)
    current_value = rows[-1]["close_value"] if rows else start_value
    hold_value = base["usdt"] + base["eth"] * last_price
    today_iso = _localtime(datetime.utcnow()).date().isoformat()
    today = next((r for r in rows if r["date"] == today_iso), None)
    n_sells = sum(r["sells"] for r in rows)

    return JSONResponse({
        "ready": True, "dry_run": dry_run,
        "since": _localtime(since).strftime("%d/%m/%Y %H:%M"),
        "start_value": round(start_value, 2),
        "current_value": round(current_value, 4),
        "total_change": round(current_value - start_value, 4),
        "total_change_pct": round((current_value - start_value) / start_value * 100, 3) if start_value else 0.0,
        "hold_value": round(hold_value, 2),
        "vs_hold": round(current_value - hold_value, 4),
        "bot_net": round(sum(r["bot_net"] for r in rows), 4),
        "fees": round(sum(r["fees"] for r in rows), 4),
        "trades": len(trades), "sells": n_sells,
        "win_rate": round(wins / n_sells * 100, 1) if n_sells else None,
        "today": today or {"date": today_iso, "change": 0.0, "change_pct": 0.0,
                           "buys": 0, "sells": 0, "bot_net": 0.0, "fees": 0.0},
        "days": list(reversed(rows)),
    })


@router.post("/api/eth/baseline/reset")
async def api_baseline_reset(request: Request):
    """Reinicia el punto de partida LIVE (usar después de depositar o retirar fondos)."""
    try:
        from app.eth_runner import _get_exchange_balances, set_live_baseline
        from app.exchanges.factory import get_exchange_client
        client = get_exchange_client()
        with db_session() as db:
            pair = get_setting(db, "trading_pair", config.TRADING_PAIR)
        usdt, eth = _get_exchange_balances(client)
        price = float(client.get_ticker(pair).get("last", 0))
        with db_session() as db:
            baseline = set_live_baseline(db, usdt, eth, price)
        logger.info("[WEB] Punto de partida LIVE reiniciado: $%.2f", baseline["value"])
        return JSONResponse({"ok": True, "baseline": baseline})
    except Exception as exc:
        logger.exception("[WEB] Error reiniciando punto de partida: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── API: grid ─────────────────────────────────────────────────────────────────

@router.get("/api/eth/grid")
async def api_grid(request: Request):
    with db_session() as db:
        dry_run = _bool_setting(db, "dry_run", config.DRY_RUN)
        dry_flag = 1 if dry_run else 0
        state = db.query(EthBotState).filter(
            EthBotState.dry_run == dry_flag
        ).order_by(desc(EthBotState.timestamp)).first()
        if not state or not state.active_grid_levels:
            return JSONResponse({"buy_levels": [], "sell_levels": [], "amount_per_level": 0.0,
                                 "current_price": state.current_price if state else None})
        grid = json.loads(state.active_grid_levels)
        grid["current_price"] = state.current_price
        return JSONResponse(grid)


# ── API: candles ──────────────────────────────────────────────────────────────

@router.get("/api/eth/candles")
async def api_candles(request: Request):
    with db_session() as db:
        rows = (
            db.query(EthCandle)
            .order_by(desc(EthCandle.timestamp))
            .limit(100)
            .all()
        )
        rows = list(reversed(rows))
        candles = [{
            "timestamp": c.timestamp.isoformat() if c.timestamp else "",
            "open": c.open, "high": c.high, "low": c.low,
            "close": c.close, "volume": c.volume,
        } for c in rows]
    return JSONResponse({"candles": candles, "count": len(candles)})


# ── API: historial de capital (para el gráfico) ───────────────────────────────

@router.get("/api/eth/history")
async def api_history(request: Request, days: int = 1):
    """Valor del portfolio vs. 'no operar' en los últimos `days` días (0 = desde el inicio)."""
    with db_session() as db:
        dry_run = _bool_setting(db, "dry_run", config.DRY_RUN)
        base = _baseline(db, dry_run)
        if base is None:
            return JSONResponse({"points": []})
        since = base["since"]
        if days > 0:
            since = max(since, datetime.utcnow() - timedelta(days=days))
        rows = (
            db.query(EthBotState.timestamp, EthBotState.capital, EthBotState.current_price)
            .filter(EthBotState.dry_run == (1 if dry_run else 0), EthBotState.timestamp >= since)
            .order_by(EthBotState.timestamp)
            .all()
        )
    stride = max(1, -(-len(rows) // 300))
    sampled = rows[::stride]
    if rows and sampled[-1] is not rows[-1]:
        sampled.append(rows[-1])
    points = [{
        "timestamp": _localtime(ts).strftime("%H:%M" if days == 1 else "%d/%m"),
        "label": _localtime(ts).strftime("%d/%m %H:%M"),
        "capital": cap, "price": price,
        "hold": round(base["usdt"] + base["eth"] * (price or 0), 4),
    } for ts, cap, price in sampled if cap is not None]
    return JSONResponse({"points": points, "start_value": base["value"]})


# ── API: start / stop ─────────────────────────────────────────────────────────

@router.post("/api/eth/bot/start")
async def api_start(request: Request):
    with db_session() as db:
        set_setting(db, "bot_status", "on")
    logger.info("[WEB] Bot ETH → ON")
    return JSONResponse({"ok": True, "status": "on"})


@router.post("/api/eth/bot/stop")
async def api_stop(request: Request):
    with db_session() as db:
        set_setting(db, "bot_status", "off")
    logger.info("[WEB] Bot ETH → OFF")
    return JSONResponse({"ok": True, "status": "off"})


# ── API: config ───────────────────────────────────────────────────────────────

@router.post("/api/eth/config")
async def api_config(
    request: Request,
    capital: float = Form(...),
    levels: int = Form(...),
    range_pct: float = Form(...),
):
    with db_session() as db:
        set_setting(db, "capital", str(capital))
        set_setting(db, "levels", str(levels))
        set_setting(db, "range_pct", str(range_pct))
    logger.info("[WEB] Config ETH guardada: capital=%.2f levels=%d range_pct=%.4f",
                capital, levels, range_pct)
    return JSONResponse({"ok": True, "capital": capital, "levels": levels, "range_pct": range_pct})


# ── API: modo simulador / live ────────────────────────────────────────────────

@router.post("/api/eth/mode")
async def api_mode(request: Request, dry_run: str = Form(...)):
    """Cambia entre SIMULADOR (dry_run=true) y LIVE (dry_run=false)."""
    new_val = "true" if dry_run.strip().lower() in ("true", "1", "yes", "on") else "false"
    with db_session() as db:
        set_setting(db, "dry_run", new_val)
    logger.warning("[WEB] Modo de operación ETH → %s", "SIMULADOR" if new_val == "true" else "LIVE")
    return JSONResponse({"ok": True, "dry_run": new_val == "true"})


# ── API: correr un tick manual ────────────────────────────────────────────────

@router.get("/api/eth/balances")
async def api_balances(request: Request):
    """Devuelve los saldos del exchange real (Bitso) o mock."""
    try:
        from app.exchanges.factory import get_exchange_client
        client = get_exchange_client()

        with db_session() as db:
            pair = get_setting(db, "trading_pair", config.TRADING_PAIR)
            dry_run = _bool_setting(db, "dry_run", config.DRY_RUN)

        balances = client.get_balances()
        ticker = client.get_ticker(pair)
        price = float(ticker.get("last", 0))

        eth_available = 0.0
        eth_locked = 0.0
        usdt_available = 0.0
        usdt_locked = 0.0

        for coin, bal in balances.items():
            c = coin.lower()
            if c == "eth":
                eth_available = bal.get("available", 0)
                eth_locked = bal.get("locked", 0)
            elif c in ("usdt", "usd"):
                usdt_available = bal.get("available", 0)
                usdt_locked = bal.get("locked", 0)

        eth_total = eth_available + eth_locked
        eth_value_usd = eth_total * price if price else 0.0
        portfolio_usd = usdt_available + usdt_locked + eth_value_usd

        return JSONResponse({
            "ok": True,
            "dry_run": dry_run,
            "exchange": type(client).__name__,
            "eth_available": round(eth_available, 8),
            "eth_locked": round(eth_locked, 8),
            "eth_total": round(eth_total, 8),
            "eth_value_usd": round(eth_value_usd, 2),
            "usdt_available": round(usdt_available, 2),
            "usdt_locked": round(usdt_locked, 2),
            "usdt_total": round(usdt_available + usdt_locked, 2),
            "portfolio_usd": round(portfolio_usd, 2),
            "price": price,
            "pair": pair,
        })
    except Exception as exc:
        logger.exception("[WEB] Error obteniendo balances: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)})


@router.post("/api/eth/tick")
def api_tick(request: Request):
    try:
        from app.eth_runner import run_eth_tick
        result = run_eth_tick(manual=True)
        return JSONResponse(result)
    except Exception as exc:
        logger.exception("[WEB] Error en tick manual: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── Exchange Config ──────────────────────────────────────────────────────────

@router.get("/exchange", response_class=HTMLResponse)
async def exchange_page(request: Request):
    with db_session() as db:
        ctx = _base_ctx(request, db)
        exchange_client = get_setting(db, "exchange_client", config.EXCHANGE_CLIENT)
        api_key = get_setting(db, "exchange_api_key", "") or ""
        api_secret = get_setting(db, "exchange_api_secret", "") or ""
        trading_pair = get_setting(db, "trading_pair", config.TRADING_PAIR)
        ctx.update({
            "exchange_client": exchange_client,
            "api_key_masked": api_key[:6] + "•••" if len(api_key) > 6 else api_key,
            "has_secret": bool(api_secret),
            "trading_pair": trading_pair,
        })
    return templates.TemplateResponse("exchange_config.html", ctx)


@router.post("/api/eth/exchange")
async def api_exchange_save(
    request: Request,
    exchange_client: str = Form(...),
    api_key: str = Form(""),
    api_secret: str = Form(""),
    trading_pair: str = Form("eth_usdt"),
):
    with db_session() as db:
        set_setting(db, "exchange_client", exchange_client.strip().lower())
        set_setting(db, "trading_pair", trading_pair.strip().lower())
        if api_key and "•" not in api_key:
            set_setting(db, "exchange_api_key", api_key.strip())
        if api_secret:
            set_setting(db, "exchange_api_secret", api_secret.strip())
    logger.info("[WEB] Exchange config guardada: %s / %s", exchange_client, trading_pair)
    return JSONResponse({"ok": True})


@router.post("/api/eth/exchange/test")
async def api_exchange_test(request: Request):
    try:
        from app.exchanges.factory import get_exchange_client
        client = get_exchange_client()
        client_name = type(client).__name__

        with db_session() as db:
            pair = get_setting(db, "trading_pair", config.TRADING_PAIR)

        result: dict = {"ok": True, "client": client_name}

        try:
            ticker = client.get_ticker(pair)
            result["ticker"] = {
                "pair": pair,
                "last": ticker.get("last"),
                "bid": ticker.get("bid"),
                "ask": ticker.get("ask"),
            }
        except Exception as exc:
            result["ticker_error"] = str(exc)

        try:
            balances = client.get_balances()
            result["balances"] = {
                k: {"available": v["available"], "locked": v["locked"]}
                for k, v in balances.items()
                if v.get("available", 0) > 0 or v.get("locked", 0) > 0
            }
        except Exception as exc:
            result["balances_error"] = str(exc)

        return JSONResponse(result)
    except Exception as exc:
        logger.exception("[WEB] Error en test de exchange: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)})


# ── Change Password ──────────────────────────────────────────────────────────

@router.get("/password", response_class=HTMLResponse)
async def password_page(request: Request):
    with db_session() as db:
        ctx = _base_ctx(request, db)
        needs_change = get_setting(db, "password_needs_change", "false")
        ctx["needs_change"] = needs_change == "true"
    return templates.TemplateResponse("change_password.html", ctx)


@router.post("/api/eth/password")
async def api_change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    username = getattr(request.state, "username", "")
    if not username:
        return JSONResponse({"ok": False, "error": "Sesión inválida"}, status_code=401)

    if new_password != confirm_password:
        return JSONResponse({"ok": False, "error": "Las contraseñas no coinciden"})

    if len(new_password) < 6:
        return JSONResponse({"ok": False, "error": "La contraseña debe tener al menos 6 caracteres"})

    from app.auth import _check_password, change_password

    with db_session() as db:
        user = db.query(Auth).filter(Auth.username == username).first()
        if not user:
            return JSONResponse({"ok": False, "error": "Usuario no encontrado"})
        if not _check_password(current_password, user.password_hash):
            return JSONResponse({"ok": False, "error": "Contraseña actual incorrecta"})

    change_password(username, new_password)
    logger.info("[WEB] Contraseña cambiada para: %s", username)
    return JSONResponse({"ok": True})
