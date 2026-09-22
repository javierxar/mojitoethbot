"""
BitsoExchangeClient — cliente real para la API v3 de Bitso, adaptado a ETH.

Referencia: https://docs.bitso.com/bitso-api/docs

Par por defecto: eth_ars (Bitso opera en Argentina). También soporta eth_mxn / eth_usd.

Las velas OHLCV NO vienen de Bitso: se descargan de la API pública de Binance
(ETHUSDT) porque Bitso no expone klines históricas de forma conveniente.

TODOs pendientes de verificación contra documentación oficial de Bitso:
  - TODO [B2]: Confirmar campo de monto en POST /orders (major=ETH / minor=fiat)
  - TODO [B3]: Confirmar estructura de GET /orders/{oid} response
  - TODO [B6]: Confirmar book name "eth_ars" / "eth_mxn"

NUNCA activar DRY_RUN=false sin revisar todos los TODOs anteriores.
"""

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone

import httpx

from app import config
from app.exchanges.base import ExchangeClient

logger = logging.getLogger(__name__)

_BASE_URL = "https://bitso.com/api/v3"
_BINANCE_URL = "https://api.binance.com/api/v3/klines"
_RETRY_DELAYS = [30, 90, 270]   # segundos entre reintentos (backoff exponencial)
_REQUEST_TIMEOUT = 15            # segundos


class BitsoExchangeClient(ExchangeClient):
    """
    Cliente real para Bitso API v3.

    Autenticación HMAC-SHA256:
      message  = nonce + HTTP_METHOD + request_path + body
      signature = HMAC-SHA256(api_secret, message).hexdigest()
      header   = "Bitso {api_key}:{nonce}:{signature}"
    """

    def __init__(self, api_key: str = "", api_secret: str = ""):
        self._api_key = api_key or config.BITSO_API_KEY
        self._api_secret = api_secret or config.BITSO_API_SECRET
        if not self._api_key or not self._api_secret:
            raise ValueError(
                "BITSO_API_KEY y BITSO_API_SECRET son requeridos para BitsoExchangeClient"
            )
        logger.info("[BITSO] BitsoExchangeClient inicializado (API key: %s***)", self._api_key[:6])

    # ── autenticación ─────────────────────────────────────────────────────────

    def _auth_header(self, method: str, path: str, body: str = "") -> dict:
        nonce = str(int(time.time() * 1000))
        request_path = "/api/v3" + path
        message = nonce + method.upper() + request_path + body
        signature = hmac.new(
            key=self._api_secret.encode("utf-8"),
            msg=message.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).hexdigest()
        return {
            "Authorization": f"Bitso {self._api_key}:{nonce}:{signature}",
            "Content-Type": "application/json",
        }

    # ── HTTP con retry ────────────────────────────────────────────────────────

    def _get(self, path: str, params: dict | None = None, private: bool = False) -> dict:
        url = _BASE_URL + path
        headers = self._auth_header("GET", path) if private else {}

        for attempt, delay in enumerate([0] + _RETRY_DELAYS, start=1):
            if delay:
                logger.warning("[BITSO] Reintento %d/%d en %ds — GET %s", attempt, len(_RETRY_DELAYS) + 1, delay, path)
                time.sleep(delay)
            try:
                with httpx.Client(timeout=_REQUEST_TIMEOUT) as client:
                    resp = client.get(url, params=params, headers=headers)
                if resp.status_code in (400, 401, 403):
                    raise RuntimeError(f"Bitso {resp.status_code}: {resp.text[:200]}")
                resp.raise_for_status()
                data = resp.json()
                if not data.get("success", True):
                    raise RuntimeError(f"Bitso error: {data.get('error', data)}")
                return data.get("payload", data)
            except RuntimeError:
                raise
            except (httpx.HTTPError, Exception) as exc:
                logger.error("[BITSO] GET %s falló (intento %d): %s", path, attempt, exc)
                if attempt > len(_RETRY_DELAYS):
                    raise

    def _post(self, path: str, body: dict) -> dict:
        url = _BASE_URL + path
        body_str = json.dumps(body)
        headers = self._auth_header("POST", path, body_str)

        for attempt, delay in enumerate([0] + _RETRY_DELAYS, start=1):
            if delay:
                logger.warning("[BITSO] Reintento %d/%d en %ds — POST %s", attempt, len(_RETRY_DELAYS) + 1, delay, path)
                time.sleep(delay)
            try:
                with httpx.Client(timeout=_REQUEST_TIMEOUT) as client:
                    resp = client.post(url, content=body_str, headers=headers)
                if resp.status_code in (400, 401, 403):
                    raise RuntimeError(f"Bitso {resp.status_code}: {resp.text[:200]}")
                resp.raise_for_status()
                data = resp.json()
                if not data.get("success", True):
                    raise RuntimeError(f"Bitso error: {data.get('error', data)}")
                return data.get("payload", data)
            except RuntimeError:
                raise
            except (httpx.HTTPError, Exception) as exc:
                logger.error("[BITSO] POST %s falló (intento %d): %s", path, attempt, exc)
                if attempt > len(_RETRY_DELAYS):
                    raise

    # ── interfaz pública ──────────────────────────────────────────────────────

    def get_ticker(self, pair: str) -> dict:
        payload = self._get("/ticker/", params={"book": pair})
        last = float(payload.get("last", 0))
        bid = float(payload.get("bid", 0))
        ask = float(payload.get("ask", 0))
        high = float(payload.get("high", 0))
        low = float(payload.get("low", 0))
        volume = float(payload.get("volume", 0))
        change_24h = ((last - low) / low * 100) if low else 0.0

        result = {
            "pair": pair, "last": last, "bid": bid, "ask": ask,
            "high_24h": high, "low_24h": low, "volume_24h": volume,
            "change_24h": round(change_24h, 4),
        }
        logger.info("[BITSO] get_ticker(%s) → last=%.2f change_24h=%.2f%%", pair, last, change_24h)
        return result

    def get_orderbook(self, pair: str) -> dict:
        payload = self._get("/order_book/", params={"book": pair})
        raw_bids = payload.get("bids", [])
        raw_asks = payload.get("asks", [])

        def _parse(entry):
            if isinstance(entry, dict):
                return {"price": float(entry["price"]), "amount": float(entry["amount"])}
            return {"price": float(entry[0]), "amount": float(entry[1])}

        bids = [_parse(b) for b in raw_bids[:20]]
        asks = [_parse(a) for a in raw_asks[:20]]

        best_bid = bids[0]["price"] if bids else 0.0
        best_ask = asks[0]["price"] if asks else 0.0
        spread_pct = ((best_ask - best_bid) / best_bid * 100) if best_bid else 0.0
        depth_bids = sum(b["price"] * b["amount"] for b in bids)
        depth_asks = sum(a["price"] * a["amount"] for a in asks)

        return {
            "pair": pair, "best_bid": best_bid, "best_ask": best_ask,
            "spread_percent": round(spread_pct, 4),
            "depth_bids": round(depth_bids, 2), "depth_asks": round(depth_asks, 2),
            "bids": bids, "asks": asks,
        }

    def get_balances(self) -> dict:
        payload = self._get("/balance/", private=True)
        raw_balances = payload.get("balances", [])
        result = {}
        for entry in raw_balances:
            currency = entry.get("currency", "").lower()
            result[currency] = {
                "available": float(entry.get("available", 0)),
                "locked": float(entry.get("locked", 0)),
            }
        logger.info("[BITSO] get_balances() → %s", {k: v["available"] for k, v in result.items()})
        return result

    def place_market_buy(self, pair: str, amount: float) -> dict:
        """
        POST /api/v3/orders/  (privado) — compra a mercado.

        TODO [B2]: Confirmar que el campo para monto fiat es "minor".
        Cuerpo: {"book": "eth_ars", "side": "buy", "type": "market", "minor": "5000.00"}
        """
        body = {
            "book": pair, "side": "buy", "type": "market",
            "minor": str(round(amount, 2)),
        }
        logger.info("[BITSO] place_market_buy(%s, %.2f) — enviando orden...", pair, amount)
        payload = self._post("/orders/", body)
        return self._finalize_order(payload, pair, side="buy", requested_amount=amount)

    def place_market_sell(self, pair: str, amount_eth: float) -> dict:
        """
        POST /api/v3/orders/  (privado) — venta a mercado de ETH.

        En una venta a mercado se especifica el monto en la moneda mayor (ETH)
        vía el campo "major".
        TODO [B2]: Confirmar que type="market" + side="sell" + major={eth} es correcto.
        Cuerpo: {"book": "eth_ars", "side": "sell", "type": "market", "major": "0.05"}
        """
        body = {
            "book": pair, "side": "sell", "type": "market",
            "major": str(round(amount_eth, 8)),
        }
        logger.info("[BITSO] place_market_sell(%s, %.8f ETH) — enviando orden...", pair, amount_eth)
        payload = self._post("/orders/", body)
        return self._finalize_order(payload, pair, side="sell", requested_amount=amount_eth)

    def _finalize_order(self, payload: dict, pair: str, side: str, requested_amount: float) -> dict:
        """Normaliza la respuesta de una orden y hace polling hasta estado final."""
        order_id = payload.get("oid", payload.get("order_id", ""))
        result = {
            "order_id": str(order_id), "pair": pair, "side": side, "type": "market",
            "amount": requested_amount,
            "eth_amount": float(payload.get("major", 0) or 0),
            "price": float(payload.get("price", 0) or 0),
            "total": float(payload.get("minor", 0) or 0),
            "status": payload.get("status", "pending"),
            "created_at": payload.get("created_at", datetime.now(timezone.utc).isoformat()),
        }
        logger.info("[BITSO] orden %s enviada → order_id=%s status=%s", side, order_id, result["status"])

        _FINAL_STATUSES = {"completed", "filled", "cancelled", "failed"}
        if order_id and result["status"] not in _FINAL_STATUSES:
            for poll_attempt in range(1, 11):   # hasta 10 × 3s = ~30s
                time.sleep(3)
                try:
                    filled = self.get_order(str(order_id), pair)
                    result["status"] = filled["status"]
                    if filled["status"] in _FINAL_STATUSES:
                        result["eth_amount"] = filled.get("eth_amount", result["eth_amount"])
                        result["price"] = filled.get("price", result["price"])
                        result["total"] = filled.get("total", result["total"])
                        logger.info("[BITSO] Orden %s confirmada: status=%s", order_id, result["status"])
                        break
                except Exception as exc:
                    logger.warning("[BITSO] Error en polling %d/10: %s", poll_attempt, exc)
            else:
                logger.warning("[BITSO] Polling agotado — orden %s en estado '%s'", order_id, result["status"])
        return result

    def get_order(self, order_id: str, pair: str) -> dict:
        raw = self._get(f"/orders/{order_id}/", private=True)
        payload = raw[0] if isinstance(raw, list) and raw else (raw if isinstance(raw, dict) else {})
        total = float(payload.get("original_value", 0) or 0)
        return {
            "order_id": order_id, "pair": payload.get("book", pair),
            "side": payload.get("side", "buy"), "type": payload.get("type", "market"),
            "amount": total, "eth_amount": float(payload.get("major", 0) or 0),
            "price": float(payload.get("price", 0) or 0), "total": total,
            "status": payload.get("status", "unknown"),
            "created_at": payload.get("created_at", ""),
        }

    def get_ohlcv_15m(self, pair: str, limit: int = 200) -> list[dict]:
        """
        Descarga velas de 15 minutos de ETH/USDT desde la API pública de Binance.

        GET https://api.binance.com/api/v3/klines?symbol=ETHUSDT&interval=15m&limit=200

        El parámetro `pair` de Bitso se ignora para el símbolo de Binance: las
        velas siempre se piden sobre ETHUSDT (configurable vía BINANCE_SYMBOL).
        """
        return _fetch_binance_ohlcv(limit=limit)


def _fetch_binance_ohlcv(limit: int = 200) -> list[dict]:
    """
    Descarga velas OHLCV de 15m desde Binance (público, sin auth).

    Respuesta de Binance (lista de listas):
      [ open_time, open, high, low, close, volume, close_time, ... ]
    """
    params = {
        "symbol": config.BINANCE_SYMBOL,
        "interval": config.CANDLE_INTERVAL,
        "limit": limit,
    }
    last_exc: Exception | None = None
    for attempt in range(1, 4):
        try:
            with httpx.Client(timeout=_REQUEST_TIMEOUT) as client:
                resp = client.get(_BINANCE_URL, params=params)
            resp.raise_for_status()
            raw = resp.json()
            candles = []
            for k in raw:
                candles.append({
                    "timestamp": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc),
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                })
            logger.info("[BINANCE] get_ohlcv_15m(%s) → %d velas", config.BINANCE_SYMBOL, len(candles))
            return candles
        except Exception as exc:
            last_exc = exc
            logger.warning("[BINANCE] klines falló (intento %d/3): %s", attempt, exc)
            time.sleep(2 * attempt)
    raise RuntimeError(f"No se pudieron descargar velas de Binance: {last_exc}")
