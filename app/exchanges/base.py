from abc import ABC, abstractmethod


class ExchangeClient(ABC):
    """
    Interfaz unificada para todos los exchanges del bot ETH.

    Todos los métodos son síncronos. Las implementaciones deben manejar
    internamente reintentos y timeouts.

    Contratos de los dicts de retorno:

    ticker → {
        "pair": str, "last": float, "bid": float, "ask": float,
        "high_24h": float, "low_24h": float, "volume_24h": float, "change_24h": float,
    }

    orderbook → {
        "pair": str, "best_bid": float, "best_ask": float, "spread_percent": float,
        "depth_bids": float, "depth_asks": float, "bids": list[dict], "asks": list[dict],
    }

    balances → { "<currency>": {"available": float, "locked": float}, ... }

    order → {
        "order_id": str, "pair": str, "side": str, "type": str, "amount": float,
        "status": str, "created_at": str,
        "confirmed": bool,            # True si los datos del fill son reales
        # Solo si confirmed:
        "price": float,               # precio promedio real
        "eth_amount": float,          # ETH ejecutados (bruto)
        "eth_net": float,             # compra: ETH recibidos; venta: ETH entregados
        "usdt_net": float,            # compra: USDT gastados; venta: USDT recibidos
        "fee_amount": float, "fee_currency": str, "fee_usd": float,
    }

    candle (OHLCV) → {
        "timestamp": datetime, "open": float, "high": float,
        "low": float, "close": float, "volume": float,
    }
    """

    @abstractmethod
    def get_ticker(self, pair: str) -> dict:
        """Devuelve el ticker actual del par."""

    @abstractmethod
    def get_orderbook(self, pair: str) -> dict:
        """Devuelve el orderbook del par con spread y profundidad calculados."""

    @abstractmethod
    def get_balances(self) -> dict:
        """Devuelve los saldos de todas las monedas de la cuenta."""

    @abstractmethod
    def place_market_buy(self, pair: str, amount: float) -> dict:
        """Ejecuta una orden de compra a mercado (amount = monto en moneda cotización)."""

    @abstractmethod
    def place_market_sell(self, pair: str, amount_eth: float) -> dict:
        """Ejecuta una orden de venta a mercado (amount_eth = cantidad de ETH a vender)."""

    @abstractmethod
    def get_order(self, order_id: str, pair: str) -> dict:
        """Consulta el estado de una orden por su ID."""

    @abstractmethod
    def get_ohlcv_15m(self, pair: str, limit: int = 200) -> list[dict]:
        """
        Devuelve velas OHLCV de 15 minutos.

        Retorna lista de dicts:
          {"timestamp": datetime, "open": float, "high": float,
           "low": float, "close": float, "volume": float}
        ordenadas de más antigua a más reciente.
        """
