"""
Notificaciones por mail (Gmail SMTP) del bot ETH.

- Configuración en la DB (pestaña Notificaciones), con .env como valor inicial.
- Gmail con contraseña de aplicación: smtp.gmail.com:587 STARTTLS (o SSL en el puerto 465).
- La configuración se lee con la sesión del llamador y el envío corre en un thread aparte
  con reintentos: un problema de mail nunca frena ni rompe un tick.
- Los errores repetidos se avisan como máximo una vez por hora.
- La contraseña nunca se loguea ni se devuelve a la web.
"""

import logging
import smtplib
import threading
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

from app import config
from app.database import get_setting

logger = logging.getLogger(__name__)

_RETRY_DELAYS = [5, 15, 30]
_THROTTLE = timedelta(hours=1)
_last_sent: dict[str, datetime] = {}
_last_test: dict = {"timestamp": None, "recipient": None, "status": None, "error": None}

# event → (setting que lo habilita, título, color)
EVENTS: dict[str, tuple[str, str, str]] = {
    "buy":          ("notify_trades", "Compra ejecutada", "#34d399"),
    "sell":         ("notify_trades", "Venta ejecutada", "#f87171"),
    "order_error":  ("notify_errors", "Error en una orden", "#f87171"),
    "tick_error":   ("notify_errors", "Error en el chequeo del bot", "#f87171"),
    "bot_status":   ("notify_status", "Estado del bot", "#7c8cff"),
    "mode":         ("notify_status", "Modo de operación", "#fbbf24"),
    "cap":          ("notify_cap", "Compras en pausa por límite de ETH", "#fbbf24"),
    "daily":        ("notify_daily", "Resumen del día", "#7c8cff"),
    "test":         ("", "Mail de prueba", "#7c8cff"),
}
TOGGLES = {
    "notify_trades": "Compras y ventas (solo en modo LIVE)",
    "notify_errors": "Errores de órdenes o del chequeo (máximo uno por hora)",
    "notify_status": "Encendido / apagado del bot y cambios de modo",
    "notify_cap": "Compras en pausa por el límite de 80% en ETH",
    "notify_daily": "Resumen diario",
}


def money(value: float, decimals: int = 2) -> str:
    """Formato argentino: 2.679,10"""
    return f"{value:,.{decimals}f}".replace(",", "_").replace(".", ",").replace("_", ".")


def get_smtp_config(db) -> dict:
    return {
        "host": config.SMTP_HOST,
        "port": config.SMTP_PORT,
        "user": get_setting(db, "smtp_user", config.SMTP_USER) or "",
        "password": get_setting(db, "smtp_password", config.SMTP_PASSWORD) or "",
        "to": get_setting(db, "notify_email", config.NOTIFY_EMAIL) or "",
    }


def is_configured(cfg: dict) -> bool:
    return bool(cfg["host"] and cfg["user"] and cfg["password"] and cfg["to"])


def toggle_enabled(db, key: str) -> bool:
    return (get_setting(db, key, "true") or "true").strip().lower() in ("true", "1", "yes", "on")


# ── HTML ──────────────────────────────────────────────────────────────────────

def build_html(title: str, color: str, rows: list[tuple[str, str]], note: str = "") -> str:
    now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    rows_html = "".join(
        f'<tr><td style="padding:9px 0;color:#8b95a9;font-size:14px;border-top:1px solid #1c2331;">{escape(k)}</td>'
        f'<td style="padding:9px 0;color:#e8ecf4;font-size:14px;font-weight:600;text-align:right;'
        f'border-top:1px solid #1c2331;">{escape(v)}</td></tr>'
        for k, v in rows)
    note_html = (f'<p style="margin:16px 0 0;color:#8b95a9;font-size:13px;line-height:1.6;">{escape(note)}</p>'
                 if note else "")
    return f"""<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#080b12;font-family:Inter,-apple-system,'Segoe UI',Roboto,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#080b12;padding:28px 12px;"><tr><td align="center">
<table width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%;background:#0f141e;
       border:1px solid #1c2331;border-radius:14px;overflow:hidden;">
  <tr><td style="padding:22px 26px;border-bottom:3px solid {color};">
    <div style="font-size:13px;color:#8b95a9;font-weight:600;">ETH DGT Bot</div>
    <div style="font-size:20px;color:#e8ecf4;font-weight:700;margin-top:4px;">{escape(title)}</div>
  </td></tr>
  <tr><td style="padding:12px 26px 22px;">
    <table width="100%" cellpadding="0" cellspacing="0">{rows_html}</table>{note_html}
  </td></tr>
  <tr><td style="padding:14px 26px;border-top:1px solid #1c2331;color:#5b657a;font-size:12px;">
    v{escape(config.BOT_VERSION)} · {now}
  </td></tr>
</table></td></tr></table></body></html>"""


# ── Envío ─────────────────────────────────────────────────────────────────────

def _send_sync(cfg: dict, subject: str, html: str, retries: list[int] | None = None) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"ETH DGT Bot <{cfg['user']}>"
    msg["To"] = cfg["to"]
    msg.attach(MIMEText(html, "html", "utf-8"))
    last_exc = None
    for attempt, delay in enumerate([0] + (_RETRY_DELAYS if retries is None else retries), start=1):
        if delay:
            time.sleep(delay)
        try:
            if cfg["port"] == 465:
                server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=15)
            else:
                server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=15)
                server.starttls()
            with server:
                server.login(cfg["user"], cfg["password"])
                server.sendmail(cfg["user"], [cfg["to"]], msg.as_bytes())
            logger.info("[NOTIFY] Mail enviado a %s: %s", cfg["to"], subject)
            return
        except smtplib.SMTPAuthenticationError as exc:
            raise RuntimeError("Gmail rechazó la cuenta o la contraseña. Usá una contraseña de aplicación "
                               "de 16 caracteres, no la contraseña normal de Gmail.") from exc
        except Exception as exc:
            last_exc = exc
            logger.warning("[NOTIFY] Envío falló (intento %d): %s", attempt, type(exc).__name__)
    raise RuntimeError(f"No se pudo enviar el mail: {last_exc}") from last_exc


def notify(db, event: str, subject: str, rows: list[tuple[str, str]], note: str = "",
           throttle_key: str | None = None) -> bool:
    """Encola un mail si está configurado y el evento está habilitado. Nunca lanza excepciones."""
    try:
        toggle, title, color = EVENTS[event]
        cfg = get_smtp_config(db)
        if not is_configured(cfg) or (toggle and not toggle_enabled(db, toggle)):
            return False
        if throttle_key:
            now = datetime.now(timezone.utc)
            if throttle_key in _last_sent and now - _last_sent[throttle_key] < _THROTTLE:
                return False
            _last_sent[throttle_key] = now
        html = build_html(title, color, rows, note)

        def _worker():
            try:
                _send_sync(cfg, subject, html)
            except Exception as exc:
                logger.error("[NOTIFY] %s", exc)

        threading.Thread(target=_worker, daemon=True, name="notify-mail").start()
        return True
    except Exception as exc:
        logger.error("[NOTIFY] No se pudo preparar la notificación %s: %s", event, exc)
        return False


def send_test(db) -> dict:
    """Mail de prueba sincrónico (sin reintentos) para validar la configuración desde la web."""
    cfg = get_smtp_config(db)
    now = datetime.now(timezone.utc)
    _last_test.update(timestamp=now.isoformat(timespec="seconds"), recipient=cfg["to"] or None)
    if not is_configured(cfg):
        _last_test.update(status="error", error="Falta completar la cuenta Gmail, la contraseña o el destino.")
        return {"ok": False, "error": _last_test["error"]}
    html = build_html("Mail de prueba", "#7c8cff", [
        ("Destino", cfg["to"]), ("Enviado desde", cfg["user"]), ("Servidor", f"{cfg['host']}:{cfg['port']}"),
    ], "Si estás leyendo esto, las notificaciones del bot están funcionando.")
    try:
        _send_sync(cfg, "ETH Bot · Notificaciones funcionando", html, retries=[])
        _last_test.update(status="ok", error=None)
        return {"ok": True, "recipient": cfg["to"]}
    except Exception as exc:
        _last_test.update(status="error", error=str(exc))
        return {"ok": False, "error": str(exc)}


def last_test() -> dict:
    return dict(_last_test)
