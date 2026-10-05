"""订单与库存内存模型。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Order:
    order_id: str
    sku: str
    quantity: int
    unit_price: str
    idempotency_key: str
