"""
Rutas del dashboard web del ETH bot — protegidas por AuthMiddleware.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc

from app import config
from app.database import db_session, get_setting, set_setting
from app.models import EthBotState, EthCandle, EthTrade

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


def _win_rate(trades) -> float:
    sells = [t for t in trades if t.side == "sell"]
    if not sells:
        return 0.0
    wins = sum(1 for t in sells if (t.pnl or 0) > 0)
    return round(wins / len(sells) * 100, 1)


# ── Dashboard ─────────────────────────────────────────────────────────────────

@router.get("/")
async def index():
    return RedirectResponse("/dashboard", status_code=302)


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    with db_session() as db:
        ctx = _base_ctx(request, db)
        last_state = db.query(EthBotState).order_by(desc(EthBotState.timestamp)).first()
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
        state = db.query(EthBotState).order_by(desc(EthBotState.timestamp)).first()
        bot_status = get_setting(db, "bot_status", config.BOT_STATUS)
        dry_run = _bool_setting(db, "dry_run", config.DRY_RUN)
        capital_initial = float(get_setting(db, "capital", str(config.ETH_CAPITAL_USD)))
        if not state:
            return JSONResponse({
                "regime": None, "adx": None, "price": None,
                "capital": capital_initial, "capital_initial": capital_initial,
                "daily_pnl": 0.0, "total_pnl": 0.0, "drawdown_pct": 0.0,
                "bot_status": bot_status, "dry_run": dry_run, "updated_at": None,
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
        trades = [{
            "timestamp": _localtime(t.timestamp).strftime("%d/%m %H:%M") if t.timestamp else "",
            "side": t.side, "price": t.price,
            "amount_eth": t.amount_eth, "amount_usd": t.amount_usd,
            "pnl": t.pnl, "strategy": t.strategy, "status": t.status,
        } for t in rows]
        win_rate = _win_rate(rows)
    return JSONResponse({"trades": trades, "win_rate": win_rate, "count": len(trades)})


# ── API: grid ─────────────────────────────────────────────────────────────────

@router.get("/api/eth/grid")
async def api_grid(request: Request):
    with db_session() as db:
        state = db.query(EthBotState).order_by(desc(EthBotState.timestamp)).first()
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
async def api_history(request: Request):
    """Últimos snapshots de estado (capital / precio) — ~24h a 15min = 96 puntos."""
    with db_session() as db:
        rows = (
            db.query(EthBotState)
            .order_by(desc(EthBotState.timestamp))
            .limit(96)
            .all()
        )
        rows = list(reversed(rows))
        points = [{
            "timestamp": _localtime(s.timestamp).strftime("%d/%m %H:%M") if s.timestamp else "",
            "capital": s.capital, "price": s.current_price, "regime": s.regime,
        } for s in rows]
    return JSONResponse({"points": points})


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

@router.post("/api/eth/tick")
def api_tick(request: Request):
    try:
        from app.eth_runner import run_eth_tick
        result = run_eth_tick(manual=True)
        return JSONResponse(result)
    except Exception as exc:
        logger.exception("[WEB] Error en tick manual: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
