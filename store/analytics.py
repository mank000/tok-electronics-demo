"""Read-only store metrics for the admin dashboard."""

from datetime import date, datetime, time
from decimal import Decimal

from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q, Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone

from .models import Favorite, Order, OrderItem, ReturnRequest, Wish


MONTHS = ("янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")
PERIODS = (6, 12, 24)
PAID = Q(payment_state="paid") & ~Q(status=Order.Status.CANCELLED)


def money(value):
    return f"{Decimal(value or 0):,.0f}".replace(",", "\u00a0") + " ₽"


def month_at(first, offset):
    index = first.year * 12 + first.month - 1 + offset
    return date(index // 12, index % 12 + 1, 1)


def breakdown(queryset, field, choices, total):
    counts = dict(queryset.values(field).annotate(count=Count("pk")).values_list(field, "count"))
    return [{"label": label, "count": counts.get(value, 0), "percent": round(counts.get(value, 0) / total * 100) if total else 0}
            for value, label in choices]


def dashboard_data(month_count=12):
    if month_count not in PERIODS:
        month_count = 12
    first = timezone.localdate().replace(day=1)
    start_date = month_at(first, 1 - month_count)
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(start_date, time.min), tz)
    orders = Order.objects.filter(is_demo=False)
    period_orders = orders.filter(created_at__gte=start)
    grouped = period_orders.annotate(month=TruncMonth("created_at", tzinfo=tz)).values("month").annotate(
        count=Count("pk"),
        placed=Sum("total", filter=~Q(status=Order.Status.CANCELLED)),
        paid=Sum("total", filter=PAID),
        paid_count=Count("pk", filter=PAID),
    )
    by_month = {(row["month"].year, row["month"].month): row for row in grouped}
    monthly = []
    for index in range(month_count):
        month = month_at(start_date, index)
        data = by_month.get((month.year, month.month), {})
        monthly.append({
            "label": f"{MONTHS[month.month - 1]} {month.year}",
            "short": f"{MONTHS[month.month - 1]} {str(month.year)[-2:]}",
            "count": data.get("count", 0),
            "placed": data.get("placed") or Decimal(0),
            "paid": data.get("paid") or Decimal(0),
            "paid_count": data.get("paid_count", 0),
        })
    max_orders = max((row["count"] for row in monthly), default=0)
    max_paid = max((row["paid"] for row in monthly), default=Decimal(0))
    for row in monthly:
        row["orders_height"] = max(3, round(row["count"] / max_orders * 100)) if row["count"] and max_orders else 0
        row["paid_height"] = max(3, round(row["paid"] / max_paid * 100)) if row["paid"] and max_paid else 0
        row["placed_label"] = money(row["placed"])
        row["paid_label"] = money(row["paid"])

    total_orders = sum(row["count"] for row in monthly)
    placed_total = sum((row["placed"] for row in monthly), Decimal(0))
    paid_total = sum((row["paid"] for row in monthly), Decimal(0))
    paid_count = sum(row["paid_count"] for row in monthly)
    line_total = ExpressionWrapper(F("unit_price") * F("quantity"), output_field=DecimalField(max_digits=14, decimal_places=2))
    top_products = list(OrderItem.objects.filter(order__is_demo=False, order__created_at__gte=start)
        .exclude(order__status=Order.Status.CANCELLED)
        .values("product_id", "product__name", "product__slug", "product__is_visible")
        .annotate(units=Sum("quantity"), order_count=Count("order_id", distinct=True), amount=Sum(line_total))
        .order_by("-units", "-amount", "product__name")[:10])
    for product in top_products:
        product["amount_label"] = money(product["amount"])

    top_wishes = list(Wish.objects.filter(created_at__gte=start)
        .values("product_id", "product__name", "product__slug", "product__is_visible")
        .annotate(count=Count("pk")).order_by("-count", "product__name")[:8])
    active = orders.filter(status__in=(Order.Status.NEW, Order.Status.PAID, Order.Status.PACKING, Order.Status.SHIPPED)).count()
    pending_returns = ReturnRequest.objects.filter(order_item__order__is_demo=False, status__in=("new", "review")).count()
    gateway_choices = dict(Order._meta.get_field("payment_gateway").choices)
    gateways = breakdown(period_orders, "payment_gateway", gateway_choices.items(), total_orders)
    return {
        "selected_months": month_count,
        "periods": PERIODS,
        "monthly": monthly,
        "total_orders": total_orders,
        "placed_total": money(placed_total),
        "paid_total": money(paid_total),
        "average_paid": money(paid_total / paid_count if paid_count else 0),
        "paid_count": paid_count,
        "active_orders": active,
        "pending_returns": pending_returns,
        "favorites": Favorite.objects.filter(created_at__gte=start).count(),
        "status_breakdown": breakdown(period_orders, "status", Order.Status.choices, total_orders),
        "delivery_breakdown": breakdown(period_orders, "delivery", Order.Delivery.choices, total_orders),
        "gateway_breakdown": gateways,
        "top_products": top_products,
        "top_wishes": top_wishes,
        "has_orders": bool(total_orders),
    }
