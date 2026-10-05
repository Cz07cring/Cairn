"""公开：正常建单路径。"""

from order_service import create_order, get_inventory, list_orders, reset_store


def test_create_order_deducts_inventory() -> None:
    reset_store()
    order = create_order(
        sku="SKU-001",
        quantity=2,
        unit_price="19.90",
        idempotency_key="checkout-unique-1",
    )
    assert order.quantity == 2
    assert get_inventory("SKU-001") == 8
    assert len(list_orders()) == 1
