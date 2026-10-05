"""内存库存与订单仓——故意缺少幂等与并发保护。

缺陷：相同 idempotency_key 可多次建单并多次扣减库存。
参考修复见 reference_fix/store.py；不得由 Executor 上下文直接附带该文件。
"""

from __future__ import annotations

from uuid import uuid4

from order_service.models import Order

INITIAL_INVENTORY = {"SKU-001": 10}


class OrderStore:
    def __init__(self) -> None:
        self.inventory: dict[str, int] = dict(INITIAL_INVENTORY)
        self.orders: dict[str, Order] = {}

    def reset(self) -> None:
        self.inventory = dict(INITIAL_INVENTORY)
        self.orders = {}

    def create_order(
        self,
        *,
        sku: str,
        quantity: int,
        unit_price: str,
        idempotency_key: str,
    ) -> Order:
        # 故意不查 idempotency_key：顺序/并发重复均可建第二单并再扣库存。
        if sku not in self.inventory:
            raise KeyError(f"unknown sku: {sku}")
        if self.inventory[sku] < quantity:
            raise ValueError("insufficient inventory")
        self.inventory[sku] -= quantity
        order = Order(
            order_id=str(uuid4()),
            sku=sku,
            quantity=quantity,
            unit_price=unit_price,
            idempotency_key=idempotency_key,
        )
        self.orders[order.order_id] = order
        return order
