from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.db.models import F
from django.utils import timezone

from .models import OrderItem, Product


def refund_amount(entry, order):
    items = list(order.items.order_by("pk"))
    subtotal = sum((item.line_total for item in items), Decimal(0))
    collected = order.payment_state == "paid" or (
        order.payment_method == order.Payment.ON_RECEIPT and order.status == order.Status.DELIVERED
    )
    if not subtotal or not collected:
        return None
    paid = min(order.total, subtotal)
    before = Decimal(0)
    for item in items:
        if item.pk == entry.order_item_id:
            after = before + item.unit_price * entry.quantity
            share_before = (before * paid / subtotal).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            share_after = (after * paid / subtotal).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            return share_after - share_before
        before += item.line_total
    return None


def demo_refund(order):
    return order.payment_gateway == "demo" and settings.DEMO_PAYMENTS


def confirm_refund(entry, order):
    entry.refund_amount = refund_amount(entry, order)
    if not entry.refund_reference and demo_refund(order):
        entry.refund_reference = f"Демо {entry.pk}"
    entry.refunded_at = timezone.now()
    item = entry.order_item
    if entry.restock and item.inventory_state == OrderItem.Inventory.ISSUED:
        Product.objects.filter(pk=item.product_id, stock_quantity__isnull=False).update(
            stock_quantity=F("stock_quantity") + entry.quantity,
        )
