import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

_APP_DIR = Path(__file__).parent

from app import config
from app.auth import AuthMiddleware, init_admin_user, router as auth_router
from app.web.routes import router as web_router
from app.database import init_db
from app.scheduler import start_scheduler, stop_scheduler

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="ETH DGT Bot", version=config.BOT_VERSION)

app.add_middleware(AuthMiddleware)
app.mount("/static", StaticFiles(directory=str(_APP_DIR / "web" / "static")), name="static")
app.include_router(auth_router)
app.include_router(web_router)


@app.on_event("startup")
async def startup():
    logger.info("Iniciando ETH DGT Bot v%s", config.BOT_VERSION)
    init_db()
    init_admin_user()
    start_scheduler()
    logger.info(
        "Bot listo. Exchange: %s | Par: %s | DRY_RUN: %s | Capital: $%.2f",
        config.EXCHANGE_CLIENT, config.TRADING_PAIR, config.DRY_RUN, config.ETH_CAPITAL_USD,
    )


@app.on_event("shutdown")
async def shutdown():
    stop_scheduler()
    logger.info("Bot detenido.")


@app.get("/health", tags=["system"])
async def health():
    return JSONResponse({"status": "ok", "version": config.BOT_VERSION, "bot": "eth"})
