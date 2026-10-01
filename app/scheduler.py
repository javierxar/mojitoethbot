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
        try:
            from app.notifications import notify
            with db_session() as db:
                notify(db, "tick_error", "ETH Bot · Error en el chequeo", [("Error", str(exc)[:300])],
                       "El bot vuelve a intentar en el próximo chequeo.", throttle_key="tick_error")
        except Exception:
            logger.exception("[SCHEDULER] No se pudo avisar el error por mail")
    finally:
        release_lock()


def _run_summary() -> None:
    try:
        from app.eth_runner import send_summary
        from app.notifications import summary_schedule
        with db_session() as db:
            days, _ = summary_schedule(db)
        send_summary(days)
    except Exception as exc:
        logger.exception("[SCHEDULER] Error en resumen por mail: %s", exc)


def _next_at(hhmm: str, tz: ZoneInfo) -> datetime:
    hour, minute = (int(x) for x in hhmm.split(":"))
    now = datetime.now(tz)
    first = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return first if first > now else first + timedelta(days=1)


def schedule_summary() -> datetime | None:
    """(Re)programa el resumen según la configuración guardada. Devuelve el próximo envío."""
    if not _scheduler or not _scheduler.running:
        return None
    from app.notifications import summary_schedule
    with db_session() as db:
        days, hhmm = summary_schedule(db)
    tz = ZoneInfo(config.TIMEZONE)
    job = _scheduler.add_job(
        _run_summary,
        trigger=IntervalTrigger(days=days, start_date=_next_at(hhmm, tz), timezone=config.TIMEZONE),
        id="eth_summary",
        name=f"Resumen por mail (cada {days} día(s) a las {hhmm})",
        replace_existing=True,
        misfire_grace_time=600,
    )
    logger.info("[SCHEDULER] Resumen por mail cada %d día(s) a las %s — próximo: %s", days, hhmm, job.next_run_time)
    return job.next_run_time


def get_next_summary_time() -> datetime | None:
    if not _scheduler or not _scheduler.running:
        return None
    job = _scheduler.get_job("eth_summary")
    return job.next_run_time if job else None


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
    schedule_summary()
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
