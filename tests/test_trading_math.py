import pytest

from app import config
from app.trading_math import break_even_price, floor_decimals, min_sell_price, net_return

FEE = 0.0036  # comisión real verificada en Bitso eth_usdt (compra y venta)


def test_break_even_for_real_bitso_buy():
    # Compra real del 24/09: 10 USDT a 2692 → break-even de venta 2711,49
    assert break_even_price(2692, FEE, FEE) == pytest.approx(2711.49, abs=0.01)


def test_break_even_from_cost_basis_that_already_includes_buy_fee():
    cost_per_net_eth = 10 / 0.00370134
    assert break_even_price(cost_per_net_eth, 0.0, FEE) == pytest.approx(2711.49, abs=0.01)


def test_net_return_is_zero_at_break_even():
    assert net_return(2692, break_even_price(2692, FEE, FEE), FEE, FEE) == pytest.approx(0, abs=1e-12)


@pytest.mark.parametrize("net, required_move_pct", [
    (0.0, 0.724), (0.001, 0.825), (0.005, 1.228), (0.01, 1.731), (0.02, 2.738),
])
def test_min_sell_price_required_move(net, required_move_pct):
    assert (min_sell_price(1000, net, FEE, FEE) / 1000 - 1) * 100 == pytest.approx(required_move_pct, abs=0.001)


def test_min_sell_price_round_trip_gives_requested_net():
    assert net_return(2692, min_sell_price(2692, 0.005, FEE, FEE), FEE, FEE) == pytest.approx(0.005, abs=1e-12)


@pytest.mark.parametrize("move, net_pct", [
    (0.003, -0.421), (0.004, -0.322), (0.005, -0.222), (0.006, -0.123), (0.0125, 0.522),
])
def test_small_moves_lose_after_fees(move, net_pct):
    assert net_return(1000, 1000 * (1 + move), FEE, FEE) * 100 == pytest.approx(net_pct, abs=0.001)


def test_real_round_trip_22_23_september():
    # Compró 10 USDT a 2750,40 y vendió a 2723,50: recibió 9,83101287 USDT netos
    assert net_return(2750.4, 2723.5, FEE, FEE) * 100 == pytest.approx(-1.69, abs=0.01)


def test_defaults_come_from_config(monkeypatch):
    monkeypatch.setattr(config, "BUY_FEE_PCT", 0.0036)
    monkeypatch.setattr(config, "SELL_FEE_PCT", 0.0030)
    assert net_return(100, 100) == pytest.approx(0.9964 * 0.9970 - 1)
    assert break_even_price(100) == pytest.approx(100 / (0.9964 * 0.9970))


@pytest.mark.parametrize("value, decimals, expected", [
    (10.835, 2, 10.83), (10.83, 2, 10.83), (10.83101287, 2, 10.83), (5.419999999, 2, 5.41),
    (0.1 + 0.2, 2, 0.3), (0.003701349999, 8, 0.00370134), (0.0, 2, 0.0), (-1.0, 2, 0.0),
])
def test_floor_decimals_never_rounds_up(value, decimals, expected):
    assert floor_decimals(value, decimals) == expected
