"""
Shared Alpaca order helpers.

Why this exists: a GTC stop-loss order reserves ("holds") the position's shares,
so a bare `close_position()` is rejected with "insufficient qty available".
`close_symbol` cancels the symbol's open orders first, then closes.

Also: alpaca-py enums are `str, Enum`, and on Python 3.11+ `str(OrderType.STOP)`
is "OrderType.STOP", not "stop". Always compare with `enum_val(...)`.
"""
import logging
import time

from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus

log = logging.getLogger(__name__)

STOP_TYPES = ("stop", "stop_limit", "trailing_stop")


def enum_val(x):
    """'stop' for OrderType.STOP, 'sell' for OrderSide.SELL, passthrough for plain values."""
    return getattr(x, "value", x)


def open_orders(trading, symbol=None):
    req = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol] if symbol else None, limit=500)
    return trading.get_orders(filter=req)


def is_stop_sell(order) -> bool:
    return enum_val(order.order_type) in STOP_TYPES and enum_val(order.side) == "sell"


def has_live_stop(trading, symbol) -> bool:
    try:
        return any(is_stop_sell(o) for o in open_orders(trading, symbol))
    except Exception as e:
        log.warning(f"{symbol}: could not check open orders ({e})")
        return False


def cancel_open_orders(trading, symbol, wait_s: float = 10.0) -> int:
    """Cancel every open order for `symbol` and wait until Alpaca reports none left."""
    orders = open_orders(trading, symbol)
    for o in orders:
        try:
            trading.cancel_order_by_id(o.id)
        except Exception as e:
            log.warning(f"{symbol}: cancel {o.id} failed ({e})")
    deadline = time.time() + wait_s
    while orders and time.time() < deadline:
        time.sleep(1)
        orders = open_orders(trading, symbol)
    if orders:
        raise RuntimeError(f"{symbol}: {len(orders)} open order(s) still pending cancel")
    return len(orders)


def is_held(trading, symbol) -> bool:
    try:
        trading.get_open_position(symbol)
        return True
    except Exception:
        return False


def close_symbol(trading, symbol):
    """Cancel the symbol's open orders (e.g. the GTC stop), then close the position.

    Returns the close order, or None if the position was already gone.
    Raises on failure so callers can report it.
    """
    if not is_held(trading, symbol):
        log.info(f"{symbol}: no open position — nothing to close")
        return None
    cancel_open_orders(trading, symbol)
    return trading.close_position(symbol)


def wait_until_flat(trading, symbols, wait_s: float = 30.0) -> list:
    """Wait for closes to fill. Returns the symbols that are still held."""
    remaining = list(symbols)
    deadline = time.time() + wait_s
    while remaining and time.time() < deadline:
        time.sleep(2)
        remaining = [s for s in remaining if is_held(trading, s)]
    return remaining
