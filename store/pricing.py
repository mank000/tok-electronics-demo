from decimal import Decimal, ROUND_HALF_UP

from django.utils import timezone

from .models import Coupon
from .services import cart_lines


def normalize_coupon(value):
    if not isinstance(value, str) or len(value) > 32:
        raise ValueError("Неверный промокод")
    return value.strip().upper()


def cart_totals(cart, code="", lock=False):
    now = timezone.now()
    lines = cart_lines(cart)
    priced = [(product, quantity, product.price_at(now)) for product, quantity in lines]
    subtotal = sum((product.price * quantity for product, quantity, _ in priced), Decimal(0))
    sale_total = sum((price * quantity for _, quantity, price in priced), Decimal(0))
    product_discount = subtotal - sale_total
    code = normalize_coupon(code)
    coupon = None
    coupon_discount = Decimal(0)
    if code:
        queryset = Coupon.objects.select_for_update() if lock else Coupon.objects
        coupon = queryset.filter(code__iexact=code).first()
        if not coupon or not coupon.is_active or (coupon.starts_at and now < coupon.starts_at) or (coupon.ends_at and now >= coupon.ends_at):
            raise ValueError("Промокод не действует")
        if coupon.max_uses and coupon.uses >= coupon.max_uses:
            raise ValueError("Промокод больше недоступен")
        if sale_total < coupon.min_total:
            raise ValueError(f"Промокод действует от {coupon.min_total:.0f} ₽")
        if coupon.kind == Coupon.Kind.PERCENT:
            coupon_discount = (sale_total * coupon.value / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        else:
            coupon_discount = coupon.value
        coupon_discount = min(coupon_discount, max(Decimal(0), sale_total - Decimal("1.00")))
    return {"lines": priced, "subtotal": subtotal, "product_discount": product_discount, "coupon_discount": coupon_discount, "total": sale_total - coupon_discount, "coupon": coupon}
