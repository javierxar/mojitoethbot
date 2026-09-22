"""
Autenticación del dashboard web del ETH bot.

- Login con bcrypt, sesión firmada con itsdangerous (cookie HttpOnly).
- Expiración de sesión: 24 horas.
- Bloqueo tras 5 intentos fallidos consecutivos por 15 minutos.
- Middleware que protege todas las rutas excepto EXEMPT_PATHS.
"""

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import bcrypt
from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app import config
from app.database import db_session
from app.models import Auth

logger = logging.getLogger(__name__)

# ── Constantes ────────────────────────────────────────────────────────────────
SESSION_COOKIE = "eth_bot_session"
SESSION_MAX_AGE = 24 * 3600          # 24 horas en segundos
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15

EXEMPT_PATHS = {"/login", "/health"}
EXEMPT_PREFIXES = ("/static",)

_TEMPLATES_DIR = Path(__file__).parent / "web" / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_serializer: URLSafeTimedSerializer | None = None


def _get_serializer() -> URLSafeTimedSerializer:
    global _serializer
    if _serializer is None:
        _serializer = URLSafeTimedSerializer(config.SESSION_SECRET)
    return _serializer


# ── Gestión de usuarios ───────────────────────────────────────────────────────

def _hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def _check_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


def init_admin_user() -> None:
    """Si la tabla auth está vacía, crea el usuario desde WEB_USER/WEB_PASSWORD del .env."""
    from app.database import set_setting
    with db_session() as db:
        count = db.query(Auth).count()
        if count == 0:
            db.add(Auth(
                username=config.WEB_USER,
                password_hash=_hash_password(config.WEB_PASSWORD),
                failed_attempts=0,
            ))
            set_setting(db, "password_needs_change", "true")
            logger.info("[AUTH] Usuario admin creado: %s", config.WEB_USER)
        else:
            logger.debug("[AUTH] Tabla auth ya tiene %d usuario(s)", count)


# ── Sesiones ──────────────────────────────────────────────────────────────────

def create_session_token(username: str) -> str:
    return _get_serializer().dumps({"u": username})


def verify_session(request: Request) -> str | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    try:
        data = _get_serializer().loads(token, max_age=SESSION_MAX_AGE)
        return data.get("u")
    except SignatureExpired:
        logger.debug("[AUTH] Sesión expirada")
        return None
    except BadSignature:
        logger.warning("[AUTH] Cookie de sesión inválida")
        return None


def set_session_cookie(response: Response, username: str) -> None:
    token = create_session_token(username)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE)


# ── Lógica de login ───────────────────────────────────────────────────────────

def _is_locked(user: Auth) -> bool:
    if not user.locked_until:
        return False
    locked_until = user.locked_until
    if locked_until.tzinfo is None:
        locked_until = locked_until.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) < locked_until


def _remaining_lockout(user: Auth) -> int:
    if not user.locked_until:
        return 0
    locked_until = user.locked_until
    if locked_until.tzinfo is None:
        locked_until = locked_until.replace(tzinfo=timezone.utc)
    delta = locked_until - datetime.now(timezone.utc)
    return max(0, int(delta.total_seconds() / 60) + 1)


def attempt_login(username: str, password: str) -> tuple[bool, str]:
    GENERIC_ERROR = "Usuario o contraseña incorrectos"

    with db_session() as db:
        user = db.query(Auth).filter(Auth.username == username).first()

        if not user:
            logger.warning("[AUTH] Intento de login con usuario inexistente")
            return False, GENERIC_ERROR

        if _is_locked(user):
            mins = _remaining_lockout(user)
            logger.warning("[AUTH] Cuenta bloqueada: %s (%d min restantes)", username, mins)
            return False, f"Cuenta bloqueada. Intentá de nuevo en {mins} minuto(s)."

        if not _check_password(password, user.password_hash):
            user.failed_attempts = (user.failed_attempts or 0) + 1
            if user.failed_attempts >= MAX_FAILED_ATTEMPTS:
                user.locked_until = datetime.now(timezone.utc) + timedelta(minutes=LOCKOUT_MINUTES)
                logger.warning("[AUTH] Cuenta bloqueada por %d minutos: %s", LOCKOUT_MINUTES, username)
                return False, f"Demasiados intentos fallidos. Cuenta bloqueada {LOCKOUT_MINUTES} minutos."
            remaining = MAX_FAILED_ATTEMPTS - user.failed_attempts
            logger.warning("[AUTH] Contraseña incorrecta para %s (%d intentos restantes)", username, remaining)
            return False, GENERIC_ERROR

        user.failed_attempts = 0
        user.locked_until = None
        logger.info("[AUTH] Login exitoso: %s", username)
        return True, ""


def change_password(username: str, new_password: str) -> None:
    from app.database import set_setting
    with db_session() as db:
        user = db.query(Auth).filter(Auth.username == username).first()
        if not user:
            raise ValueError(f"Usuario no encontrado: {username}")
        user.password_hash = _hash_password(new_password)
        user.failed_attempts = 0
        user.locked_until = None
        set_setting(db, "password_needs_change", "false")
    logger.info("[AUTH] Contraseña actualizada para: %s", username)


# ── Router ────────────────────────────────────────────────────────────────────

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
async def login_get(request: Request):
    if verify_session(request):
        return RedirectResponse("/dashboard", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login")
async def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    success, error = attempt_login(username, password)

    if not success:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": error},
            status_code=401,
        )

    response = RedirectResponse("/dashboard", status_code=302)
    set_session_cookie(response, username)
    return response


@router.get("/auth/logout")
async def logout(request: Request):
    response = RedirectResponse("/login", status_code=302)
    clear_session_cookie(response)
    logger.info("[AUTH] Logout: %s", verify_session(request) or "sesión inválida")
    return response


# ── Middleware ────────────────────────────────────────────────────────────────

class AuthMiddleware(BaseHTTPMiddleware):
    """Protege todas las rutas excepto EXEMPT_PATHS y rutas con EXEMPT_PREFIXES."""

    def __init__(self, app: ASGIApp):
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if path in EXEMPT_PATHS or any(path.startswith(p) for p in EXEMPT_PREFIXES):
            return await call_next(request)

        username = verify_session(request)
        if not username:
            return RedirectResponse("/login", status_code=302)

        request.state.username = username
        return await call_next(request)
