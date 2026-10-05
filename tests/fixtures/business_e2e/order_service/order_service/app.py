"""订单服务应用门面（无 HTTP；供 fixture 测试直接调用）。"""

from __future__ import annotations

from order_service.models import Order
from order_service.store import OrderStore

_STORE = OrderStore()


def reset_store() -> None:
    _STORE.reset()


def create_order(
    *,
    sku: str,
    quantity: int,
    unit_price: str,
    idempotency_key: str,
) -> Order:
    return _STORE.create_order(
        sku=sku,
        quantity=quantity,
        unit_price=unit_price,
        idempotency_key=idempotency_key,
    )


def list_orders() -> list[Order]:
    return list(_STORE.orders.values())


def get_inventory(sku: str) -> int:
    return _STORE.inventory[sku]
