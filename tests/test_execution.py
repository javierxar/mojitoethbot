import json
from datetime import datetime, timedelta

import pytest

import app.eth_runner as r
from app import config
from app.database import set_setting
from app.exchanges.paper_exchange import PaperExchange
from app.models import EthBotState, EthTrade
from app.strategy_eth import Decision

FEE = 0.0036


@pytest.fixture(autouse=True)
def real_fees(monkeypatch):
    monkeypatch.setattr(config, "BUY_FEE_PCT", FEE)
    monkeypatch.setattr(config, "SELL_FEE_PCT", FEE)


class FakeBitso:
    """Se comporta como Bitso: rechaza órdenes mayores al saldo y cobra la comisión en lo recibido."""

    def __init__(self, usdt=0.0, eth=0.0, price=2692.0, confirm=True):
        self.usdt, self.eth, self.price, self.confirm = usdt, eth, price, confirm
        self.orders = []

    def get_balances(self):
        return {"usdt": {"available": self.usdt, "locked": 0.0}, "eth": {"available": self.eth, "locked": 0.0}}

    def get_ticker(self, pair):
        return {"last": self.price}

    def place_market_buy(self, pair, amount):
        if amount > self.usdt + 1e-12:
            raise RuntimeError("Bitso: saldo insuficiente")
        gross = amount / self.price
        fee = gross * FEE
        self.usdt -= amount
        self.eth += gross - fee
        self.orders.append(("buy", amount))
        return self._fill(gross, eth_net=gross - fee, usdt_net=amount, fee_amount=fee,
                          fee_currency="eth", fee_usd=fee * self.price)

    def place_market_sell(self, pair, qty):
        if qty > self.eth + 1e-12:
            raise RuntimeError("Bitso: saldo insuficiente")
        gross = qty * self.price
        fee = gross * FEE
        self.eth -= qty
        self.usdt += gross - fee
        self.orders.append(("sell", qty))
        return self._fill(qty, eth_net=qty, usdt_net=gross - fee, fee_amount=fee,
                          fee_currency="usdt", fee_usd=fee)

    def _fill(self, eth_gross, **fill):
        if not self.confirm:
            return {"order_id": "x", "status": "pending", "confirmed": False}
        return {"order_id": "x", "status": "completed", "confirmed": True, "price": self.price,
                "eth_amount": eth_gross, **fill}


def _grid_decision(price, dynamic_range=0.05):
    return Decision(regime="grid", action="grid", reason="test", current_price=price,
                    dynamic_range_pct=dynamic_range,
                    grid={"buy_levels": [1, 2], "sell_levels": [3, 4, 5], "amount_per_level": 10})


def _store_grid(db, grid, dry_run=False, ts=None):
    db.add(EthBotState(timestamp=ts or datetime.utcnow() - timedelta(minutes=15), regime="grid",
                       current_price=0, capital=0, dry_run=1 if dry_run else 0,
                       active_grid_levels=json.dumps(grid)))
    db.flush()


def _grid_tick(db, ex, price, dry_run=False):
    """Un ciclo de grid que persiste el grid resultante, como hace execute_tick."""
    ex.price = price
    grid = r._execute(db, _grid_decision(price), ex, dry_run=dry_run)
    _store_grid(db, grid, dry_run=dry_run, ts=datetime.utcnow())
    return grid


# ── Registro del fill real y contabilidad neta ────────────────────────────────

def test_buy_records_net_eth_real_fee_and_cost_basis(db):
    ex = FakeBitso(usdt=20.0, price=2692.0)
    fill = r._place_buy(db, ex, 10.0, 2692.0, "grid", dry_run=False)
    trade = db.query(EthTrade).one()
    assert trade.amount_usd == pytest.approx(10.0)
    assert trade.amount_eth == pytest.approx(0.00370134, abs=1e-8)
    assert trade.fee_currency == "eth" and trade.fee_usd == pytest.approx(0.036, abs=1e-6)
    assert trade.status == "completed"
    inv, cost = r._inventory_and_cost(db, dry_run=False)
    assert inv == pytest.approx(ex.eth, abs=1e-8)            # igual a lo disponible en el exchange
    assert cost == pytest.approx(10 / 0.00370134, abs=0.01)   # 2701,72: incluye la comisión de compra
    assert fill["price"] == 2692.0


def test_sell_pnl_is_net_of_both_fees(db):
    # Reproduce la vuelta real 22-23/09: compra 10 USDT a 2750,40, vende todo a 2723,50
    ex = FakeBitso(usdt=10.0, price=2750.4)
    r._place_buy(db, ex, 10.0, 2750.4, "grid", dry_run=False)
    inv, cost = r._inventory_and_cost(db, dry_run=False)
    ex.price = 2723.5
    fill = r._place_sell(db, ex, min(inv, ex.eth), 2723.5, cost, "grid", dry_run=False)
    assert fill["usdt"] == pytest.approx(9.83101287, abs=1e-5)
    assert fill["pnl"] == pytest.approx(9.83101287 - 10.0, abs=1e-4)
    _, total = r._pnl_summary(db, dry_run=False)
    assert total == pytest.approx(-0.169, abs=0.001)


def test_unconfirmed_fill_is_estimated_with_configured_fees(db):
    ex = FakeBitso(usdt=10.0, price=2692.0, confirm=False)
    r._place_buy(db, ex, 10.0, 2692.0, "grid", dry_run=False)
    trade = db.query(EthTrade).one()
    assert trade.status == "unconfirmed"
    assert trade.amount_eth == pytest.approx(10 / 2692 * (1 - FEE))
    assert trade.fee_usd == pytest.approx(0.036)


# ── Redondeo: nunca pedir más que el saldo ────────────────────────────────────

def test_grid_buy_never_exceeds_available_usdt(db):
    # Con round() se pedían 5.42 teniendo 5.415 y Bitso rechazaba la orden en cada chequeo
    _store_grid(db, {"buy_levels": [1990.0], "sell_levels": [2050.0], "grid_step": 25.0, "amount_per_level": 10})
    ex = FakeBitso(usdt=5.415, eth=0.001, price=1985.0)
    r._execute(db, _grid_decision(1985.0), ex, dry_run=False)
    assert ex.orders == [("buy", 5.41)]
    assert db.query(EthTrade).one().amount_usd == pytest.approx(5.41)


def test_grid_sell_quantity_is_floored_to_available_eth(db):
    _store_grid(db, {"buy_levels": [1900.0], "sell_levels": [2000.0], "grid_step": 50.0, "amount_per_level": 10})
    ex = FakeBitso(usdt=0.5, eth=0.003701349999, price=2001.0)
    r._execute(db, _grid_decision(2001.0), ex, dry_run=False)
    assert ex.orders == [("sell", 0.00370134)]


def test_recenter_floor_counts_buy_fee_once(db):
    # El costo registrado ya incluye la comisión de compra: el piso es costo × (1 + margen) / (1 − fee venta)
    ex = FakeBitso(usdt=10.0, price=2800.0)
    r._place_buy(db, ex, 10.0, 2800.0, "grid", dry_run=False)
    _, cost = r._inventory_and_cost(db, dry_run=False)
    ex.price = 2690.0
    grid = r._execute(db, _grid_decision(2690.0), ex, dry_run=False)
    expected_floor = round(cost * (1 + config.GRID_MIN_MARGIN_PCT) / (1 - FEE), 2)
    assert min(grid["sell_levels"]) == expected_floor
    assert ex.orders == [("buy", 10.0)]   # por debajo del piso no vende


# ── Entrada inicial: solo la primera vez ──────────────────────────────────────

def test_first_entry_buys_at_market_when_bot_never_traded(db):
    ex = FakeBitso(usdt=20.0, price=2000.0)
    grid = _grid_tick(db, ex, 2000.0)          # grid nuevo: compras 1950/1975, ventas 2025/2050
    assert ex.orders == [("buy", 10.0)]        # USDT / 2 niveles de compra, a mercado
    assert grid["buy_levels"] == [1950.0]      # consumió el nivel de compra más alto
    assert 2025.0 in grid["sell_levels"]


def _bot_that_sold_everything(db):
    """Bot con una compra previa que vende TODO su ETH en el único nivel de venta."""
    ex = FakeBitso(usdt=20.0, price=2000.0)
    r._place_buy(db, ex, 10.0, 2000.0, "grid", dry_run=False)
    _store_grid(db, {"buy_levels": [1950.0], "sell_levels": [2025.0], "grid_step": 25.0, "amount_per_level": 10})
    grid = _grid_tick(db, ex, 2030.0)
    assert ex.orders[-1][0] == "sell" and ex.eth == pytest.approx(0.0, abs=1e-12)
    assert grid["buy_levels"] == [1950.0, 2005.0]   # recíproco de la venta: 2030 − 25
    ex.orders.clear()
    return ex


def test_no_automatic_rebuy_after_selling_everything(db):
    ex = _bot_that_sold_everything(db)
    _grid_tick(db, ex, 2015.0)                 # siguiente ciclo, precio sobre el nivel recíproco
    _grid_tick(db, ex, 2010.0)
    assert ex.orders == []


def test_rebuy_happens_when_price_reaches_reciprocal_level(db):
    ex = _bot_that_sold_everything(db)
    _grid_tick(db, ex, 2015.0)
    assert ex.orders == []
    grid = _grid_tick(db, ex, 2004.0)          # precio ≤ 2005 (nivel recíproco)
    assert [side for side, _ in ex.orders] == ["buy"]
    assert 2005.0 not in grid["buy_levels"]
    assert round(2004.0 + 25.0, 2) in grid["sell_levels"]
    assert db.query(EthTrade).filter(EthTrade.side == "buy").count() == 2


def test_live_history_never_triggers_automatic_entry(db):
    db.add(EthTrade(timestamp=datetime.utcnow() - timedelta(days=3), side="buy", price=2000, amount_eth=0.005,
                    amount_usd=10.0, pnl=0, strategy="grid", status="completed", fee_usd=0.036, dry_run=0))
    db.add(EthTrade(timestamp=datetime.utcnow() - timedelta(days=2), side="sell", price=2100, amount_eth=0.005,
                    amount_usd=10.46, pnl=0.46, strategy="grid", status="completed", fee_usd=0.038, dry_run=0))
    db.flush()
    ex = FakeBitso(usdt=20.46, eth=0.0, price=2200.0)
    for price in (2200.0, 2400.0, 2600.0):     # sin posición y con grids rearmados en cada salto
        _grid_tick(db, ex, price)
    assert ex.orders == []


def test_restarted_simulation_gets_a_new_first_entry(db):
    db.add(EthTrade(timestamp=datetime.utcnow() - timedelta(days=5), side="buy", price=2000, amount_eth=1.0,
                    amount_usd=2000, pnl=0, strategy="grid", status="simulated", dry_run=1))
    set_setting(db, "sim_baseline", json.dumps({"since": datetime.utcnow().isoformat(), "usdt": 100.0}))
    paper = r._paper_exchange(db, 2000.0)
    r._execute(db, _grid_decision(2000.0), paper, dry_run=True)
    sim_buys = r._mode_trades(db, dry_run=True).filter(EthTrade.side == "buy").all()
    assert len(sim_buys) == 1 and sim_buys[0].amount_usd == pytest.approx(50.0)


# ── Ventas fuera del grid (toma de ganancia / tendencia) ──────────────────────

def _legacy_gross_buy(db, eth=0.004, usdt=10.0):
    db.add(EthTrade(timestamp=datetime.utcnow() - timedelta(hours=1), side="buy", price=usdt / eth,
                    amount_eth=eth, amount_usd=usdt, pnl=0, strategy="grid", status="completed", dry_run=0))
    db.flush()


def test_sell_never_exceeds_real_balance(db):
    # Registros brutos (0.004) mayores al saldo neto real (0.0037): antes Bitso rechazaba la venta
    _legacy_gross_buy(db)
    ex = FakeBitso(eth=0.0037, price=2600.0)
    decision = Decision(regime="breakout", action="sell", reason="t", current_price=2600.0,
                        amount_usd=0.0, strategy="breakout")
    r._execute(db, decision, ex, dry_run=False)
    assert ex.orders == [("sell", 0.0037)]


def test_sell_honors_amount_requested_by_strategy(db):
    _legacy_gross_buy(db)
    ex = FakeBitso(eth=0.0037, price=2600.0)
    wanted_eth = 0.0037 * 0.20
    decision = Decision(regime="grid", action="sell", reason="t", current_price=2600.0,
                        amount_usd=wanted_eth * 2600.0, strategy="profit_take")
    r._execute(db, decision, ex, dry_run=False)
    assert ex.orders == [("sell", pytest.approx(wanted_eth, abs=2e-8))]
    assert db.query(EthTrade).filter(EthTrade.side == "sell").one().amount_eth == pytest.approx(wanted_eth, abs=2e-8)


def test_buy_outside_grid_is_capped_to_available_usdt(db):
    ex = FakeBitso(usdt=4.567, price=2600.0)
    decision = Decision(regime="breakout", action="buy", reason="t", current_price=2600.0,
                        amount_usd=10.0, strategy="breakout")
    r._execute(db, decision, ex, dry_run=False)
    assert ex.orders == [("buy", 4.56)]


# ── Capital real en vez de $100 ficticios ─────────────────────────────────────

class SpyEngine:
    def decide(self, candles, capital, cfg):
        self.capital, self.cfg = capital, cfg
        return Decision(regime="grid", action="hold", reason="spy", current_price=candles[-1]["close"])


def test_execute_tick_uses_real_portfolio_as_capital(db):
    engine = SpyEngine()
    ex = FakeBitso(usdt=15.0, eth=0.002, price=2500.0)
    summary = r.execute_tick(db, engine, [{"close": 2500.0}], ex, dry_run=False)
    assert engine.capital == pytest.approx(20.0)
    assert engine.cfg["base_capital"] == pytest.approx(20.0)
    assert summary["capital"] == pytest.approx(20.0)
    assert db.query(EthBotState).one().capital == pytest.approx(20.0)


def test_daily_drawdown_is_measured_against_real_portfolio(db):
    engine = SpyEngine()
    db.add(EthBotState(timestamp=datetime.utcnow(), regime="grid", current_price=2500, capital=20.0, dry_run=0))
    db.add(EthTrade(timestamp=datetime.utcnow(), side="sell", price=2500, amount_eth=0.001, amount_usd=2.5,
                    pnl=-1.0, strategy="grid", status="completed", fee_usd=0.009, dry_run=0))
    db.flush()
    set_setting(db, "live_baseline", json.dumps({"since": (datetime.utcnow() - timedelta(days=1)).isoformat(),
                                                 "usdt": 20, "eth": 0, "price": 2500, "value": 20}))
    r.execute_tick(db, engine, [{"close": 2500.0}], FakeBitso(usdt=19.0, price=2500.0), dry_run=False)
    assert engine.cfg["daily_drawdown_pct"] == pytest.approx(1.0 / 20.0)   # 5% real, no 1% de $100


# ── Simulador: misma lógica, comisiones reales, nunca toca el exchange real ───

def test_paper_exchange_charges_real_fees(db):
    set_setting(db, "sim_baseline", json.dumps({"since": (datetime.utcnow() - timedelta(minutes=1)).isoformat(),
                                                "usdt": 100.0}))
    paper = r._paper_exchange(db, 2000.0)
    r._place_buy(db, paper, 50.0, 2000.0, "grid", dry_run=True)
    usdt, eth = r._get_exchange_balances(paper)
    assert usdt == pytest.approx(50.0) and eth == pytest.approx(0.025 * (1 - FEE))
    inv, cost = r._inventory_and_cost(db, dry_run=True)
    r._place_sell(db, paper, inv, 2000.0, cost, "grid", dry_run=True)
    usdt, eth = r._get_exchange_balances(paper)
    assert usdt == pytest.approx(50 + 0.025 * (1 - FEE) * 2000 * (1 - FEE))
    assert eth == pytest.approx(0.0, abs=1e-12)
    with pytest.raises(RuntimeError):
        paper.place_market_buy("eth_usdt", usdt + 1)


def test_sim_ignores_trades_before_simulation_start(db):
    db.add(EthTrade(timestamp=datetime.utcnow() - timedelta(days=5), side="buy", price=2000, amount_eth=1.0,
                    amount_usd=2000, pnl=0, strategy="grid", status="simulated", dry_run=1))
    set_setting(db, "sim_baseline", json.dumps({"since": datetime.utcnow().isoformat(), "usdt": 100.0}))
    db.flush()
    assert r._inventory_and_cost(db, dry_run=True) == (0.0, 0.0)
    assert r._pnl_summary(db, dry_run=True) == (0.0, 0.0)


class ForbiddenExchange:
    """Exchange real: en modo simulador no debe recibir órdenes ni consultas de saldo."""

    def get_ohlcv_15m(self, pair, limit=200):
        raise AssertionError("las velas se inyectan en el test")

    def get_balances(self):
        raise AssertionError("el simulador no debe leer saldos reales")

    def place_market_buy(self, *a, **k):
        raise AssertionError("el simulador envió una compra al exchange real")

    def place_market_sell(self, *a, **k):
        raise AssertionError("el simulador envió una venta al exchange real")


def _choppy_candles(n=200, base=2000.0):
    out = []
    for i in range(n):
        close = base + (8 if i % 2 else -8)
        out.append({"timestamp": datetime(2026, 1, 1) + timedelta(minutes=15 * i), "open": base,
                    "high": close + 4, "low": close - 4, "close": close, "volume": 1.0})
    return out


def test_dry_run_tick_never_touches_real_exchange(db, monkeypatch):
    set_setting(db, "dry_run", "true")
    db.commit()
    monkeypatch.setattr("app.exchanges.factory.get_exchange_client", lambda: ForbiddenExchange())
    monkeypatch.setattr(r, "_get_candles", lambda client: _choppy_candles())
    summary = r.run_eth_tick(manual=True)
    assert summary["ok"] and summary["dry_run"] is True
    db.expire_all()
    sim_trades = db.query(EthTrade).filter(EthTrade.dry_run == 1).all()
    assert sim_trades, "la entrada inicial del grid debe ejecutarse en el paper exchange"
    assert all(t.fee_usd and t.fee_usd > 0 for t in sim_trades)
    assert db.query(EthTrade).filter(EthTrade.dry_run == 0).count() == 0
