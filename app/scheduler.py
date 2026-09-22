"""
Scheduler del bot ETH.

- Tarea ETH cada ETH_TICK_MINUTES (default 15 min): corre run_eth_tick().
- Lock en DB para evitar corridas simultáneas.
"""

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app import config
from app.database import db_session, get_setting, set_setting

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None

_LOCK_KEY = "eth_bot_lock_run_id"
_LOCK_TS_KEY = "eth_bot_lock_started_at"
_LOCK_STALE_MINUTES = 30


# ── Lock DB ──────────────────────────────────────────────────────────────────

def acquire_lock(run_id: str) -> bool:
    with db_session() as db:
        current = get_setting(db, _LOCK_KEY, "")
        ts_str = get_setting(db, _LOCK_TS_KEY, "")
        if current:
            stale = True
            if ts_str:
                try:
                    ts = datetime.fromisoformat(ts_str)
                    stale = (datetime.now(timezone.utc) - ts) > timedelta(minutes=_LOCK_STALE_MINUTES)
                except ValueError:
                    stale = True
            if not stale:
                logger.warning("[SCHEDULER] Lock activo (run_id=%s) — abortando", current)
                return False
            logger.warning("[SCHEDULER] Lock stale — sobreescribiendo")
        set_setting(db, _LOCK_KEY, run_id)
        set_setting(db, _LOCK_TS_KEY, datetime.now(timezone.utc).isoformat())
    return True


def release_lock() -> None:
    with db_session() as db:
        set_setting(db, _LOCK_KEY, "")
        set_setting(db, _LOCK_TS_KEY, "")


# ── Job ETH ──────────────────────────────────────────────────────────────────

def _run_eth_job() -> None:
    with db_session() as db:
        status = get_setting(db, "bot_status", config.BOT_STATUS)
    if status != "on":
        logger.info("[SCHEDULER] Tick ETH omitido — bot_status=%s", status)
        return

    import uuid
    run_id = str(uuid.uuid4())
    if not acquire_lock(run_id):
        return
    try:
        from app.eth_runner import run_eth_tick
        result = run_eth_tick()
        logger.info("[SCHEDULER] Tick ETH: %s", result)
    except Exception as exc:
        logger.exception("[SCHEDULER] Error en tick ETH: %s", exc)
    finally:
        release_lock()


# ── Inicio / parada ──────────────────────────────────────────────────────────

def start_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        logger.warning("[SCHEDULER] Ya estaba corriendo — ignorado")
        return
    if not config.ETH_BOT_ENABLED:
        logger.info("[SCHEDULER] ETH_BOT_ENABLED=False — scheduler no iniciado")
        return

    _scheduler = BackgroundScheduler(timezone=config.TIMEZONE)
    interval = config.ETH_TICK_MINUTES
    tz = ZoneInfo(config.TIMEZONE)
    _scheduler.add_job(
        _run_eth_job,
        trigger=IntervalTrigger(minutes=interval, timezone=config.TIMEZONE),
        id="eth_tick",
        name=f"Tick estrategia ETH (cada {interval}min)",
        replace_existing=True,
        misfire_grace_time=120,
        next_run_time=datetime.now(tz),
    )
    _scheduler.start()
    logger.info("[SCHEDULER] Iniciado — tick ETH cada %d min (%s), primer tick inmediato", interval, config.TIMEZONE)


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("[SCHEDULER] Detenido")


def get_next_tick_time() -> datetime | None:
    if not _scheduler or not _scheduler.running:
        return None
    job = _scheduler.get_job("eth_tick")
    return job.next_run_time if job else None
