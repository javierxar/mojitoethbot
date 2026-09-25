"""Cálculos de comisiones y redondeo de órdenes (funciones puras)."""

from decimal import ROUND_DOWN, Decimal

from app import config


def _fees(buy_fee: float | None, sell_fee: float | None) -> tuple[float, float]:
    return (config.BUY_FEE_PCT if buy_fee is None else buy_fee,
            config.SELL_FEE_PCT if sell_fee is None else sell_fee)


def net_return(buy_price: float, sell_price: float,
               buy_fee: float | None = None, sell_fee: float | None = None) -> float:
    """Rendimiento neto (fracción) de comprar a buy_price y vender a sell_price pagando ambas comisiones."""
    bf, sf = _fees(buy_fee, sell_fee)
    return (sell_price / buy_price) * (1 - bf) * (1 - sf) - 1


def break_even_price(buy_price: float, buy_fee: float | None = None, sell_fee: float | None = None) -> float:
    """Precio de venta que deja resultado neto cero.

    Si buy_price ya es un costo por ETH neto (comisión de compra incluida), pasar buy_fee=0.
    """
    return min_sell_price(buy_price, 0.0, buy_fee, sell_fee)


def min_sell_price(buy_price: float, min_net_pct: float,
                   buy_fee: float | None = None, sell_fee: float | None = None) -> float:
    """Precio de venta mínimo para obtener min_net_pct neto (fracción) después de comisiones."""
    bf, sf = _fees(buy_fee, sell_fee)
    return buy_price * (1 + min_net_pct) / ((1 - bf) * (1 - sf))


def floor_decimals(value: float, decimals: int) -> float:
    """Trunca hacia abajo sin errores de coma flotante (10.83 → 10.83, 10.839 → 10.83)."""
    if value <= 0:
        return 0.0
    quantum = Decimal(1).scaleb(-decimals)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_DOWN))
