"""参考修复：幂等键映射 + 互斥锁（仅 fixture 自检；非 Agent 提示）。"""

from __future__ import annotations

import threading
from uuid import uuid4

from order_service.models import Order

INITIAL_INVENTORY = {"SKU-001": 10}


class OrderStore:
    def __init__(self) -> None:
        self.inventory: dict[str, int] = dict(INITIAL_INVENTORY)
        self.orders: dict[str, Order] = {}
        self._by_key: dict[str, Order] = {}
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self.inventory = dict(INITIAL_INVENTORY)
            self.orders = {}
            self._by_key = {}

    def create_order(
        self,
        *,
        sku: str,
        quantity: int,
        unit_price: str,
        idempotency_key: str,
    ) -> Order:
        with self._lock:
            existing = self._by_key.get(idempotency_key)
            if existing is not None:
                return existing
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
            self._by_key[idempotency_key] = order
            return order
