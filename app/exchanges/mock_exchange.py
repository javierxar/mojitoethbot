"""
Exchange simulado para el bot ETH.

- Los precios y velas OHLCV se toman de la API pública de Binance (ETHUSDT),
  así el simulador opera sobre datos de mercado reales sin ejecutar órdenes.
- Las órdenes (buy/sell) se simulan: no se envía nada a ningún exchange real.
"""

import logging
import random
import uuid
from datetime import datetime, timezone

from app import config
from app.exchanges.base import ExchangeClient
from app.exchanges.bitso_exchange import _fetch_binance_ohlcv

logger = logging.getLogger(__name__)


class MockExchangeClient(ExchangeClient):
    def __init__(self):
        self._price_usd = self._live_price()
        self._usdt_ars_rate = config.MOCK_USDT_ARS_RATE
        self._spread_pct = config.MOCK_SPREAD_PERCENT
        self._orders: dict[str, dict] = {}
        logger.info("[MOCK] inicializado — ETH: USD %.2f | spread %.2f%%", self._price_usd, self._spread_pct)

    @staticmethod
    def _live_price() -> float:
        """Último cierre de ETH/USDT desde Binance; fallback a config."""
        try:
            candles = _fetch_binance_ohlcv(limit=2)
            if candles:
                return candles[-1]["close"]
        except Exception as exc:
            logger.warning("[MOCK] No se pudo obtener precio live: %s", exc)
        return config.MOCK_ETH_PRICE_USD

    def _fiat_currency(self, pair: str) -> str:
        if pair.endswith("_ars"):
            return "ars"
        if pair.endswith("_mxn"):
            return "mxn"
        return "usd"

    def _price_in_pair(self, pair: str) -> float:
        if pair.endswith("_ars"):
            return self._price_usd * self._usdt_ars_rate
        return self._price_usd

    # ── interfaz pública ──────────────────────────────────────────────────────

    def get_ticker(self, pair: str) -> dict:
        self._price_usd = self._live_price()
        price = self._price_in_pair(pair)
        noise = random.uniform(0.9998, 1.0002)
        price *= noise
        return {
            "pair": pair, "last": round(price, 2),
            "bid": round(price * (1 - self._spread_pct / 200), 2),
            "ask": round(price * (1 + self._spread_pct / 200), 2),
            "high_24h": round(price * 1.02, 2), "low_24h": round(price * 0.98, 2),
            "volume_24h": round(random.uniform(1000, 5000), 4),
            "change_24h": round(random.uniform(-3, 3), 4),
        }

    def get_orderbook(self, pair: str) -> dict:
        mid = self._price_in_pair(pair)
        half = mid * (self._spread_pct / 200)
        best_bid, best_ask = mid - half, mid + half
        bids = [{"price": round(best_bid * (1 - i * 0.001), 2), "amount": round(random.uniform(0.1, 5), 4)} for i in range(5)]
        asks = [{"price": round(best_ask * (1 + i * 0.001), 2), "amount": round(random.uniform(0.1, 5), 4)} for i in range(5)]
        return {
            "pair": pair, "best_bid": round(best_bid, 2), "best_ask": round(best_ask, 2),
            "spread_percent": round((best_ask - best_bid) / best_bid * 100, 4),
            "depth_bids": round(sum(b["price"] * b["amount"] for b in bids), 2),
            "depth_asks": round(sum(a["price"] * a["amount"] for a in asks), 2),
            "bids": bids, "asks": asks,
        }

    def get_balances(self) -> dict:
        # El simulador maneja el capital en la lógica de trading (DB), no en el exchange.
        return {
            "eth": {"available": 0.0, "locked": 0.0},
            "usd": {"available": config.ETH_CAPITAL_USD, "locked": 0.0},
        }

    def place_market_buy(self, pair: str, amount: float) -> dict:
        exec_price = self._price_usd * (1 + self._spread_pct / 200)
        eth_bought = amount / exec_price if exec_price else 0.0
        return self._make_order(pair, "buy", amount, eth_bought, exec_price)

    def place_market_sell(self, pair: str, amount_eth: float) -> dict:
        exec_price = self._price_usd * (1 - self._spread_pct / 200)
        total_usd = amount_eth * exec_price
        return self._make_order(pair, "sell", total_usd, amount_eth, exec_price)

    def _make_order(self, pair: str, side: str, amount_usd: float, eth: float, price: float) -> dict:
        order_id = str(uuid.uuid4())
        order = {
            "order_id": order_id, "pair": pair, "side": side, "type": "market",
            "amount": round(amount_usd, 2), "eth_amount": round(eth, 8),
            "price": round(price, 2), "total": round(amount_usd, 2),
            "status": "filled", "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._orders[order_id] = order
        logger.info("[MOCK] place_market_%s(%s) → %.8f ETH @ %.2f", side, pair, eth, price)
        return order

    def get_order(self, order_id: str, pair: str) -> dict:
        order = self._orders.get(order_id)
        if order:
            return dict(order)
        return {
            "order_id": order_id, "pair": pair, "side": "buy", "type": "market",
            "amount": 0.0, "eth_amount": 0.0, "price": 0.0, "total": 0.0,
            "status": "error", "created_at": "",
        }

    def get_ohlcv_15m(self, pair: str, limit: int = 200) -> list[dict]:
        """Velas reales de Binance también en modo mock (son públicas y gratuitas)."""
        return _fetch_binance_ohlcv(limit=limit)
