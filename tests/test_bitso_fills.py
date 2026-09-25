import pytest

from app.exchanges.bitso_exchange import BitsoExchangeClient, parse_order_trades

# Respuestas reales de /order_trades/ de esta cuenta
REAL_BUY = [{
    "fees_amount": "0.00001337", "side": "buy", "minor": "-10.00000000", "major_currency": "eth",
    "book": "eth_usdt", "minor_currency": "usdt", "oid": "xE3OPeHeOtyPB9di", "maker_side": "sell",
    "major": "0.00371471", "price": "2692", "fees_currency": "eth",
}]
REAL_SELL = [{
    "book": "eth_usdt", "major": "-0.00362274", "minor": "9.86653239", "major_currency": "eth",
    "minor_currency": "usdt", "price": "2723.5", "side": "sell", "maker_side": "buy",
    "fees_currency": "usdt", "fees_amount": "0.03551952", "oid": "kFCC6pzGHdXIe0As",
}]


def test_buy_fill_fee_charged_in_eth():
    f = parse_order_trades(REAL_BUY)
    assert f["price"] == pytest.approx(2692.0, abs=0.01)
    assert f["eth_amount"] == pytest.approx(0.00371471)
    assert f["eth_net"] == pytest.approx(0.00370134, abs=1e-12)
    assert f["usdt_net"] == pytest.approx(10.0)
    assert f["fee_currency"] == "eth"
    assert f["fee_amount"] == pytest.approx(0.00001337)
    assert f["fee_usd"] / f["usdt_net"] == pytest.approx(0.0036, abs=0.00002)


def test_sell_fill_fee_charged_in_usdt():
    f = parse_order_trades(REAL_SELL)
    assert f["price"] == pytest.approx(2723.5, abs=0.01)
    assert f["eth_net"] == pytest.approx(0.00362274)
    assert f["usdt_net"] == pytest.approx(9.83101287, abs=1e-8)
    assert f["fee_currency"] == "usdt"
    assert f["fee_usd"] == pytest.approx(0.03551952)
    assert f["fee_usd"] / 9.86653239 == pytest.approx(0.0036, abs=0.000001)


def test_partial_fills_are_aggregated_with_weighted_price():
    trades = [
        {"side": "buy", "major": "0.001", "minor": "-2.000", "major_currency": "eth",
         "fees_currency": "eth", "fees_amount": "0.0000036"},
        {"side": "buy", "major": "0.002", "minor": "-4.006", "major_currency": "eth",
         "fees_currency": "eth", "fees_amount": "0.0000072"},
    ]
    f = parse_order_trades(trades)
    assert f["price"] == pytest.approx(2002.0)
    assert f["eth_net"] == pytest.approx(0.003 - 0.0000108)
    assert f["usdt_net"] == pytest.approx(6.006)


@pytest.mark.parametrize("payload", [None, [], {}, [{"side": "buy", "major": "0", "minor": "0"}]])
def test_no_fill_returns_none(payload):
    assert parse_order_trades(payload) is None


def _client(monkeypatch, order_trades_responses):
    client = BitsoExchangeClient(api_key="key", api_secret="secret")
    sent = {}
    responses = iter(order_trades_responses)
    monkeypatch.setattr(client, "_post", lambda path, body: sent.update(body) or {"oid": "abc"})
    monkeypatch.setattr(client, "_get", lambda path, params=None, private=False: next(responses))
    monkeypatch.setattr("app.exchanges.bitso_exchange.time.sleep", lambda s: None)
    return client, sent


def test_market_buy_floors_amount_and_returns_real_fill(monkeypatch):
    client, sent = _client(monkeypatch, [[], REAL_BUY])  # el primer intento todavía no tiene trades
    order = client.place_market_buy("eth_usdt", 10.839)
    assert sent["minor"] == "10.83"
    assert order["confirmed"] is True and order["status"] == "completed"
    assert order["eth_net"] == pytest.approx(0.00370134, abs=1e-12)
    assert order["fee_usd"] == pytest.approx(0.036, abs=0.0001)


def test_market_sell_floors_amount(monkeypatch):
    client, sent = _client(monkeypatch, [REAL_SELL])
    order = client.place_market_sell("eth_usdt", 0.003622749)
    assert sent["major"] == "0.00362274"
    assert order["usdt_net"] == pytest.approx(9.83101287, abs=1e-8)


def test_order_without_trades_stays_unconfirmed(monkeypatch):
    client, _ = _client(monkeypatch, [[]] * 10)
    order = client.place_market_buy("eth_usdt", 5)
    assert order["confirmed"] is False and order["status"] == "pending"
