"""
BitsoExchangeClient — cliente real para la API v3 de Bitso, adaptado a ETH/USDT.

Referencia: https://docs.bitso.com/bitso-api/docs

Par por defecto: eth_usdt. También soporta eth_mxn / eth_usd.

Las velas OHLCV NO vienen de Bitso: se descargan de la API pública de Binance
(ETHUSDT) porque Bitso no expone klines históricas de forma conveniente.

TODOs pendientes de verificación contra documentación oficial de Bitso:
  - TODO [B2]: Confirmar campo de monto en POST /orders (major=ETH / minor=USDT)
  - Fills: GET /orders/{oid} devuelve "OID incorrecto" para órdenes a mercado ya
    ejecutadas; el fill real (precio, cantidades, comisión) sale de /order_trades/{oid}.
  - TODO [B6]: Confirmar book name "eth_usdt" en Bitso

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
from app.trading_math import floor_decimals

logger = logging.getLogger(__name__)

_FILL_POLL_ATTEMPTS = 5
_FILL_POLL_DELAY = 2             # segundos entre consultas del fill
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
        """POST /api/v3/orders/ — compra a mercado gastando `amount` de la moneda cotización (minor)."""
        amount = floor_decimals(amount, 2)
        body = {"book": pair, "side": "buy", "type": "market", "minor": f"{amount:.2f}"}
        logger.info("[BITSO] place_market_buy(%s, %.2f) — enviando orden...", pair, amount)
        payload = self._post("/orders/", body)
        return self._finalize_order(payload, pair, side="buy", requested_amount=amount)

    def place_market_sell(self, pair: str, amount_eth: float) -> dict:
        """POST /api/v3/orders/ — venta a mercado de `amount_eth` ETH (major)."""
        amount_eth = floor_decimals(amount_eth, 8)
        body = {"book": pair, "side": "sell", "type": "market", "major": f"{amount_eth:.8f}"}
        logger.info("[BITSO] place_market_sell(%s, %.8f ETH) — enviando orden...", pair, amount_eth)
        payload = self._post("/orders/", body)
        return self._finalize_order(payload, pair, side="sell", requested_amount=amount_eth)

    def _finalize_order(self, payload: dict, pair: str, side: str, requested_amount: float) -> dict:
        """Obtiene el fill REAL desde /order_trades/{oid}/ (GET /orders/{oid}/ no funciona en eth_usdt)."""
        order_id = str(payload.get("oid") or payload.get("order_id") or "")
        result = {
            "order_id": order_id, "pair": pair, "side": side, "type": "market",
            "amount": requested_amount, "status": "pending", "confirmed": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        logger.info("[BITSO] orden %s enviada → order_id=%s", side, order_id)
        if not order_id:
            return result
        for attempt in range(1, _FILL_POLL_ATTEMPTS + 1):
            time.sleep(_FILL_POLL_DELAY)
            try:
                fill = parse_order_trades(self._get(f"/order_trades/{order_id}/", private=True))
            except Exception as exc:
                logger.warning("[BITSO] order_trades %s intento %d falló: %s", order_id, attempt, exc)
                continue
            if fill:
                result.update(fill, status="completed", confirmed=True)
                logger.info("[BITSO] Fill real %s: %.8f ETH netos @ %.2f, fee %.8f %s ($%.4f)",
                            order_id, fill["eth_net"], fill["price"], fill["fee_amount"],
                            fill["fee_currency"], fill["fee_usd"])
                return result
        logger.warning("[BITSO] Sin fill confirmado para la orden %s", order_id)
        return result

    def get_order(self, order_id: str, pair: str) -> dict:
        fill = parse_order_trades(self._get(f"/order_trades/{order_id}/", private=True))
        result = {"order_id": order_id, "pair": pair, "type": "market",
                  "status": "completed" if fill else "unknown", "confirmed": bool(fill)}
        if fill:
            result.update(fill)
        return result

    def get_ohlcv_15m(self, pair: str, limit: int = 200) -> list[dict]:
        """
        Descarga velas de 15 minutos de ETH/USDT desde la API pública de Binance.

        GET https://api.binance.com/api/v3/klines?symbol=ETHUSDT&interval=15m&limit=200

        El parámetro `pair` de Bitso se ignora para el símbolo de Binance: las
        velas siempre se piden sobre ETHUSDT (configurable vía BINANCE_SYMBOL).
        """
        return _fetch_binance_ohlcv(limit=limit)


def parse_order_trades(trades) -> dict | None:
    """
    Agrega los fills de /order_trades/{oid}/ en un único resultado neto.

    Bitso descuenta la comisión de lo que se RECIBE: en una compra la cobra en ETH
    y en una venta en USDT. major/minor vienen con signo según el lado.
    """
    if not isinstance(trades, list) or not trades:
        return None
    major = sum(abs(float(t.get("major") or 0)) for t in trades)
    minor = sum(abs(float(t.get("minor") or 0)) for t in trades)
    if major <= 0 or minor <= 0:
        return None
    side = str(trades[0].get("side", "")).lower()
    major_ccy = str(trades[0].get("major_currency", "eth")).lower()
    fee_ccy = str(trades[0].get("fees_currency", "")).lower()
    fee = sum(float(t.get("fees_amount") or 0) for t in trades)
    price = minor / major
    fee_in_major = fee_ccy == major_ccy

    if side == "buy":
        eth_net = major - fee if fee_in_major else major
        usdt_net = minor if fee_in_major else minor + fee      # USDT gastados
    else:
        eth_net = major + fee if fee_in_major else major       # ETH entregados
        usdt_net = minor if fee_in_major else minor - fee      # USDT recibidos

    return {
        "side": side, "price": price, "eth_amount": major, "total": minor,
        "eth_net": eth_net, "usdt_net": usdt_net,
        "fee_amount": fee, "fee_currency": fee_ccy,
        "fee_usd": fee * price if fee_in_major else fee,
    }


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
