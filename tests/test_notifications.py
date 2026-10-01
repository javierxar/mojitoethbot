import json
from datetime import datetime, timedelta

import pytest

import app.eth_runner as r
import app.notifications as nt
from app import config
from app.database import set_setting
from app.models import EthBotState
from tests.test_execution import FEE, FakeBitso, _grid_decision, _store_grid


@pytest.fixture
def sent(monkeypatch):
    """Captura los mails en vez de enviarlos y ejecuta el envío en el mismo thread."""
    outbox = []

    class InlineThread:
        def __init__(self, target, **_):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(nt.threading, "Thread", InlineThread)
    monkeypatch.setattr(nt, "_send_sync", lambda cfg, subject, html, retries=None: outbox.append((cfg["to"], subject, html)))
    monkeypatch.setattr(config, "BUY_FEE_PCT", FEE)
    monkeypatch.setattr(config, "SELL_FEE_PCT", FEE)
    nt._last_sent.clear()
    return outbox


def _configure(db):
    set_setting(db, "smtp_user", "bot@gmail.com")
    set_setting(db, "smtp_password", "abcdefghijklmnop")
    set_setting(db, "notify_email", "yo@gmail.com")


def test_money_uses_argentine_format():
    assert nt.money(2679.1) == "2.679,10"
    assert nt.money(0.0362, 3) == "0,036"


def test_nothing_is_sent_until_configured(db, sent):
    assert not nt.notify(db, "buy", "x", [("a", "b")])
    _configure(db)
    assert nt.notify(db, "buy", "x", [("a", "b")])
    assert sent[0][0] == "yo@gmail.com"


def test_disabled_event_is_not_sent(db, sent):
    _configure(db)
    set_setting(db, "notify_trades", "false")
    assert not nt.notify(db, "sell", "x", [])
    assert nt.notify(db, "order_error", "x", [])            # otro tipo de aviso sigue activo
    assert len(sent) == 1


def test_errors_are_throttled_to_one_per_hour(db, sent):
    _configure(db)
    assert nt.notify(db, "tick_error", "x", [], throttle_key="tick_error")
    assert not nt.notify(db, "tick_error", "x", [], throttle_key="tick_error")
    nt._last_sent["tick_error"] -= timedelta(hours=2)
    assert nt.notify(db, "tick_error", "x", [], throttle_key="tick_error")
    assert len(sent) == 2


def test_live_trades_are_notified_but_simulated_ones_are_not(db, sent):
    _configure(db)
    ex = FakeBitso(usdt=20.0, price=2700.0)
    r._place_buy(db, ex, 10.0, 2700.0, "grid", dry_run=True)
    assert sent == []
    r._place_buy(db, ex, 5.0, 2700.0, "grid", dry_run=False)
    inv, cost = r._inventory_and_cost(db, dry_run=False)
    ex.price = 2750.0
    r._place_sell(db, ex, inv, 2750.0, cost, "grid", dry_run=False)
    subjects = [s for _, s, _ in sent]
    assert subjects[0].startswith("ETH Bot · Compró") and "$2.700,00" in subjects[0]
    assert subjects[1].startswith("ETH Bot · Vendió") and "(+$" in subjects[1]
    assert "Resultado neto" in sent[1][2]


def test_inventory_cap_is_notified_once_until_the_bot_trades(db, sent, monkeypatch):
    _configure(db)
    monkeypatch.setattr(config, "MAX_INVENTORY_COST_PCT", 0.80)
    grid = {"buy_levels": [2650.0], "sell_levels": [2750.0], "grid_step": 25.0, "amount_per_level": 10}
    _store_grid(db, grid)
    ex = FakeBitso(usdt=5.28, eth=0.00586, price=2640.0)
    r._execute(db, _grid_decision(2640.0), ex, dry_run=False)
    r._execute(db, _grid_decision(2640.0), ex, dry_run=False)
    assert [s for _, s, _ in sent if "pausa" in s] == ["ETH Bot · Compras en pausa: ETH en 75% del portfolio"]
    r._place_buy(db, ex, 2.0, 2640.0, "grid", dry_run=False)          # al operar se rearma el aviso
    r._execute(db, _grid_decision(2640.0), ex, dry_run=False)
    assert len([s for _, s, _ in sent if "pausa" in s]) == 2


def test_daily_summary_reports_the_day(db, sent, monkeypatch):
    _configure(db)
    set_setting(db, "dry_run", "false")
    now = datetime.utcnow()
    db.add(EthBotState(timestamp=now - timedelta(days=1), regime="grid", current_price=2600, capital=20.0, dry_run=0))
    db.add(EthBotState(timestamp=now, regime="grid", current_price=2650, capital=20.5, dry_run=0))
    db.commit()
    monkeypatch.setattr(r, "db_session", _session_factory(db))
    assert r.send_daily_summary()
    to, subject, html = sent[0]
    assert subject == "ETH Bot · Resumen del día: +$0,50"
    assert "LIVE" in html and "$20,50" in html


def _session_factory(db):
    from contextlib import contextmanager

    @contextmanager
    def _session():
        yield db
    return _session


def test_test_mail_explains_a_wrong_gmail_password(db, monkeypatch):
    import smtplib

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            self.host, self.port = host, port

        def starttls(self):
            pass

        def login(self, user, password):
            raise smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(nt.smtplib, "SMTP", FakeSMTP)
    _configure(db)
    result = nt.send_test(db)
    assert not result["ok"] and "contraseña de aplicación" in result["error"]
    assert nt.last_test()["status"] == "error"
