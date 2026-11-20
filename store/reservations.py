from datetime import timedelta

from django.db import transaction
from django.db.models import Exists, F, OuterRef, Q, Sum
from django.utils import timezone

from .models import Coupon, Order, OrderItem, PaymentAttempt, Product


ONLINE_HOLD = timedelta(minutes=30)
RECEIPT_HOLD = timedelta(days=2)


def reserved_quantities(product_ids):
    rows = (OrderItem.objects.filter(product_id__in=product_ids, inventory_state=OrderItem.Inventory.RESERVED)
            .order_by().values("product_id").annotate(quantity=Sum("quantity")))
    return {row["product_id"]: row["quantity"] for row in rows}


def available_quantities(products):
    tracked = [product for product in products if product.stock_quantity is not None]
    reserved = reserved_quantities(product.pk for product in tracked) if tracked else {}
    return {product.pk: max(0, product.stock_quantity - reserved.get(product.pk, 0)) for product in tracked}


def check_stock(lines, available):
    for product, quantity in lines:
        remaining = available.get(product.pk)
        if remaining is not None and remaining < quantity:
            raise ValueError(f"«{product.name}»: доступно {remaining} шт.")


def open_provider_payments():
    return PaymentAttempt.objects.filter(gateway="yookassa", state="pending").filter(
        Q(provider_started_at__isnull=False) | Q(provider_id__gt=""),
    )


def has_open_provider_payment(order):
    return open_provider_payments().filter(order=order).exists()


def record_coupon_use(order):
    if order.coupon_reserved:
        Coupon.objects.filter(pk=order.coupon_id).update(uses=F("uses") + 1)
        order.coupon_reserved = False


def cancel_unpaid(order):
    if order.payment_state == "paid" or has_open_provider_payment(order):
        raise ValueError("Сначала подтвердите итог платежа или оформите возврат.")
    order.items.filter(inventory_state=OrderItem.Inventory.RESERVED).update(
        inventory_state=OrderItem.Inventory.RELEASED,
    )
    order.coupon_reserved = False
    order.reservation_expires_at = None
    order.status = Order.Status.CANCELLED


def issue_inventory(order):
    items = list(order.items.filter(inventory_state=OrderItem.Inventory.RESERVED).order_by("product_id"))
    for item in items:
        updated = Product.objects.filter(pk=item.product_id, stock_quantity__gte=item.quantity).update(
            stock_quantity=F("stock_quantity") - item.quantity,
        )
        if not updated:
            raise ValueError(f"Проверьте остаток «{item.title}» перед выдачей.")
    order.items.filter(pk__in=[item.pk for item in items]).update(inventory_state=OrderItem.Inventory.ISSUED)
    if order.payment_method == Order.Payment.ON_RECEIPT:
        order.payment_state = "paid"
        record_coupon_use(order)
    order.reservation_expires_at = None


def expire_reservations(limit=50):
    from .services import PaymentError, queue_order_changed
    from .yookassa import sync_payment

    now = timezone.now()
    due = Order.objects.filter(status=Order.Status.NEW, reservation_expires_at__lte=now)
    unresolved = open_provider_payments().filter(order_id=OuterRef("pk"))
    with transaction.atomic():
        orders = list(due.filter(~Exists(unresolved)).select_for_update(skip_locked=True)
                      .order_by("reservation_expires_at")[:limit])
        for order in orders:
            # Recheck after locking because a payment could have started while
            # the candidate query was running.
            if has_open_provider_payment(order):
                continue
            cancel_unpaid(order)
            order.save(update_fields=["status", "coupon_reserved", "reservation_expires_at", "updated_at"])
            queue_order_changed(order, Order.Status.NEW, order.track_number)

    # Reconcile at most one provider payment per pass, after releasing the local
    # reservations. Call the provider outside the stock transaction.
    attempt = open_provider_payments().filter(
        order__in=due, provider_id__gt="", provider_next_check_at__lte=now,
    ).order_by("provider_next_check_at").first()
    if attempt and PaymentAttempt.objects.filter(pk=attempt.pk, provider_next_check_at__lte=now).update(
        provider_next_check_at=now + timedelta(minutes=2),
    ):
        try:
            sync_payment(attempt)
        except PaymentError:
            pass
