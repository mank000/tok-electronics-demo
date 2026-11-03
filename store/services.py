import hashlib
import hmac
import json
from decimal import Decimal
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from .models import BotUser, Notification, Order, PaymentAttempt, PaymentEvent, Product


def cart_lines(cart, lock=False):
    if not isinstance(cart, dict) or not cart or len(cart) > 50:
        raise ValueError("Корзина пуста или слишком велика")
    ids = []
    quantities = {}
    for key, value in cart.items():
        try:
            pk, quantity = int(key), int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Неверное количество товара") from exc
        if pk <= 0 or not 1 <= quantity <= 20:
            raise ValueError("Количество от 1 до 20")
        ids.append(pk)
        quantities[pk] = quantity
    products_query = Product.objects.filter(pk__in=ids, is_visible=True).exclude(availability="preorder")
    products = list(products_query.select_for_update() if lock else products_query)
    if len(products) != len(ids):
        raise ValueError("Один из товаров недоступен")
    return [(p, quantities[p.pk]) for p in products]


def queue_shop(text):
    Notification.objects.create(kind="shop", text=text)


def queue_buyer(order, text):
    if order.telegram_id:
        Notification.objects.create(kind="buyer", telegram_id=order.telegram_id, text=text)


def queue_order_created(order):
    queue_shop(f"Новый заказ №{order.pk}: {order.total} ₽, {order.name}, {order.phone}. Получение: {order.get_delivery_display()}.")
    queue_buyer(order, f"Заказ №{order.pk} принят. Статус: {order.get_status_display()}.")


def queue_order_changed(order, old_status, old_track):
    if old_status != order.status:
        queue_buyer(order, f"Заказ №{order.pk}: статус «{order.get_status_display()}».")
    if order.track_number and old_track != order.track_number:
        queue_buyer(order, f"Заказ №{order.pk}: трек-номер {order.track_number}.")


class PaymentError(ValueError):
    pass


def signed_event(raw):
    if not settings.PAYMENT_WEBHOOK_SECRET:
        raise PaymentError("Секрет платежей не задан")
    return hmac.new(settings.PAYMENT_WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()


def process_payment_event(raw, signature):
    if not signature or not hmac.compare_digest(signed_event(raw), signature):
        raise PaymentError("Подпись события неверна")
    try:
        payload = json.loads(raw)
        event_id = payload["event_id"]
        reference = payload["attempt"]
        status = payload["status"]
        if not isinstance(event_id, str) or not 1 <= len(event_id) <= 100 or status not in ("paid", "declined"):
            raise ValueError
        import uuid
        reference = uuid.UUID(reference)
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise PaymentError("Некорректное событие") from exc
    with transaction.atomic():
        attempt = PaymentAttempt.objects.select_for_update().select_related("order").filter(reference=reference).first()
        if not attempt:
            raise PaymentError("Попытка оплаты не найдена")
        if PaymentEvent.objects.filter(event_id=event_id).exists():
            return False
        order = Order.objects.select_for_update().get(pk=attempt.order_id)
        if attempt.state != "pending" or order.payment_state == "paid" or order.status == Order.Status.CANCELLED:
            raise PaymentError("Попытка уже завершена")
        PaymentEvent.objects.create(event_id=event_id, attempt=attempt, status=status)
        attempt.state = status
        attempt.save(update_fields=["state"])
        order.payment_state = status
        if status == "paid":
            order.status = Order.Status.PAID
        order.save(update_fields=["payment_state", "status", "updated_at"])
        queue_shop(f"Заказ №{order.pk}: оплата {('успешна' if status == 'paid' else 'отклонена')}.")
        queue_buyer(order, f"Заказ №{order.pk}: {('оплачен' if status == 'paid' else 'оплата отклонена, можно повторить')}.")
    return True


def send_notification(notification):
    """One queued notification; a failed send never changes the original order."""
    import requests
    if not settings.BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан")
    if notification.kind == "shop":
        chat_id = settings.SHOP_CHAT_ID
    else:
        user = BotUser.objects.filter(telegram_id=notification.telegram_id).first()
        chat_id = user.chat_id if user else None
    if not chat_id:
        raise RuntimeError("Нет разрешённого чата для отправки")
    response = requests.post(f"https://api.telegram.org/bot{settings.BOT_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": notification.text}, timeout=12)
    if response.status_code != 200:
        raise RuntimeError(f"Telegram HTTP {response.status_code}")
    if not response.json().get("ok"):
        raise RuntimeError("Telegram отклонил сообщение")
    notification.status = "sent"
    notification.attempts += 1
    notification.sent_at = timezone.now()
    notification.last_error = ""
    notification.save(update_fields=["status", "attempts", "sent_at", "last_error"])


def flush_notifications(limit=20):
    for notification in Notification.objects.filter(status="pending").order_by("created_at")[:limit]:
        try:
            send_notification(notification)
        except Exception as exc:
            notification.status = "failed"
            notification.attempts += 1
            notification.last_error = str(exc).replace(settings.BOT_TOKEN, "[redacted]")[:500] if settings.BOT_TOKEN else str(exc)[:500]
            notification.save(update_fields=["status", "attempts", "last_error"])
