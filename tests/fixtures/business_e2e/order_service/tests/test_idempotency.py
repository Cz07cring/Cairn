"""公开：顺序幂等——同 key 两次只能一单、扣一次库存。

初始缺陷下本文件必须稳定失败。
"""

from order_service import create_order, get_inventory, list_orders, reset_store

KEY = "checkout-20260912-001"


def test_same_idempotency_key_twice_creates_one_order() -> None:
    reset_store()
    first = create_order(
        sku="SKU-001",
        quantity=2,
        unit_price="19.90",
        idempotency_key=KEY,
    )
    second = create_order(
        sku="SKU-001",
        quantity=2,
        unit_price="19.90",
        idempotency_key=KEY,
    )
    assert first.order_id == second.order_id
    assert len(list_orders()) == 1
    assert list_orders()[0].quantity == 2
    assert get_inventory("SKU-001") == 8
