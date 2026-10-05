"""隐藏：并发幂等与反作弊——不得进入 Executor 工作区。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from order_service import create_order, get_inventory, list_orders, reset_store

KEY = "checkout-20260912-001"


def _place(key: str):
    return create_order(
        sku="SKU-001",
        quantity=2,
        unit_price="19.90",
        idempotency_key=key,
    )


def test_concurrent_same_key_one_order() -> None:
    reset_store()
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(_place, KEY)
        b = pool.submit(_place, KEY)
        first = a.result()
        second = b.result()
    assert first.order_id == second.order_id
    assert len(list_orders()) == 1
    assert get_inventory("SKU-001") == 8


def test_different_keys_create_distinct_orders() -> None:
    """防止「拒绝一切第二次请求」作弊。"""
    reset_store()
    a = _place("checkout-key-a")
    b = _place("checkout-key-b")
    assert a.order_id != b.order_id
    assert len(list_orders()) == 2
    assert get_inventory("SKU-001") == 6
