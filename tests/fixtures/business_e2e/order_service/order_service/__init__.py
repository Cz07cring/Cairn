"""订单服务包入口（E2E fixture）。"""

from order_service.app import create_order, get_inventory, list_orders, reset_store

__all__ = ["create_order", "get_inventory", "list_orders", "reset_store"]
