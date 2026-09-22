import logging

from app import config
from app.exchanges.base import ExchangeClient

logger = logging.getLogger(__name__)


def get_exchange_client() -> ExchangeClient:
    """
    Devuelve el cliente de exchange activo.
    Lee exchange_client, exchange_api_key y exchange_api_secret de DB primero,
    con fallback a .env (config.py).

    Valores válidos: "mock" | "bitso"
    """
    client_name = config.EXCHANGE_CLIENT.strip().lower()
    api_key = ""
    api_secret = ""
    try:
        from app.database import db_session, get_setting
        with db_session() as db:
            db_client = get_setting(db, "exchange_client", None)
            db_key = get_setting(db, "exchange_api_key", None)
            db_secret = get_setting(db, "exchange_api_secret", None)
        if db_client:
            client_name = db_client.strip().lower()
        api_key = db_key or config.BITSO_API_KEY
        api_secret = db_secret or config.BITSO_API_SECRET
    except Exception:
        api_key = config.BITSO_API_KEY
        api_secret = config.BITSO_API_SECRET

    if client_name == "bitso":
        from app.exchanges.bitso_exchange import BitsoExchangeClient
        logger.info("Exchange seleccionado: Bitso (REAL)")
        return BitsoExchangeClient(api_key=api_key, api_secret=api_secret)

    if client_name == "mock":
        from app.exchanges.mock_exchange import MockExchangeClient
        logger.info("Exchange seleccionado: Mock (SIMULADO)")
        return MockExchangeClient()

    logger.warning("EXCHANGE_CLIENT='%s' no reconocido — usando Mock por seguridad", client_name)
    from app.exchanges.mock_exchange import MockExchangeClient
    return MockExchangeClient()
