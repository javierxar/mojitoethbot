"""
Ejecución simulada (modo SIMULADOR) con la misma interfaz y comisiones que Bitso.

Nunca envía órdenes a un exchange: llena al precio actual y los saldos se derivan
de los trades simulados registrados desde el inicio de la simulación.
"""

import uuid
from datetime import datetime

from app import config
from app.models import EthTrade


class PaperExchange:
    def __init__(self, db, start_usdt: float, since: datetime, price: float):
        self._db = db
        self._start_usdt = start_usdt
        self._since = since
        self.price = price

    def get_balances(self) -> dict:
        trades = (self._db.query(EthTrade)
                  .filter(EthTrade.dry_run == 1, EthTrade.timestamp >= self._since).all())
        usdt = self._start_usdt
        eth = 0.0
        for t in trades:
            if t.side == "buy":
                usdt -= t.amount_usd
                eth += t.amount_eth
            else:
                usdt += t.amount_usd
                eth -= t.amount_eth
        return {"usdt": {"available": max(usdt, 0.0), "locked": 0.0},
                "eth": {"available": max(eth, 0.0), "locked": 0.0}}

    def get_ticker(self, pair: str) -> dict:
        return {"pair": pair, "last": self.price, "bid": self.price, "ask": self.price}

    def _available(self, currency: str) -> float:
        return self.get_balances()[currency]["available"]

    def place_market_buy(self, pair: str, amount: float) -> dict:
        if amount > self._available("usdt") + 1e-9:
            raise RuntimeError(f"Saldo USDT simulado insuficiente para comprar ${amount:.2f}")
        eth_gross = amount / self.price
        fee_eth = eth_gross * config.BUY_FEE_PCT
        return self._fill(pair, "buy", eth_gross, eth_net=eth_gross - fee_eth, usdt_net=amount,
                          fee_amount=fee_eth, fee_currency="eth", fee_usd=fee_eth * self.price)

    def place_market_sell(self, pair: str, amount_eth: float) -> dict:
        if amount_eth > self._available("eth") + 1e-12:
            raise RuntimeError(f"Saldo ETH simulado insuficiente para vender {amount_eth:.8f}")
        gross = amount_eth * self.price
        fee_usd = gross * config.SELL_FEE_PCT
        return self._fill(pair, "sell", amount_eth, eth_net=amount_eth, usdt_net=gross - fee_usd,
                          fee_amount=fee_usd, fee_currency="usdt", fee_usd=fee_usd)

    def _fill(self, pair: str, side: str, eth_gross: float, **fill) -> dict:
        return {"order_id": f"paper-{uuid.uuid4().hex[:12]}", "pair": pair, "side": side,
                "type": "market", "status": "completed", "confirmed": True,
                "price": self.price, "eth_amount": eth_gross, **fill}
