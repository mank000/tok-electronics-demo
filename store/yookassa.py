"""Test-store YooKassa widget integration. Provider status is always read via API."""

from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings
from django.db import transaction

from .models import Order, PaymentAttempt, PaymentEvent
from .services import PaymentError, queue_buyer, queue_shop


API = "https://api.yookassa.ru/v3/payments"


def credentials():
    if not settings.YOOKASSA_SHOP_ID or not settings.YOOKASSA_SECRET_KEY:
        raise PaymentError("Тестовый магазин ЮKassa не настроен")
    return settings.YOOKASSA_SHOP_ID, settings.YOOKASSA_SECRET_KEY


def create_payment(order, attempt, return_url):
    payload = {
        "amount": {"value": str(order.total), "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "embedded"},
        "description": f"Заказ №{order.pk}",
        "metadata": {"order_id": str(order.pk), "attempt": str(attempt.reference)},
    }
    try:
        response = requests.post(API, auth=credentials(), json=payload,
                                 headers={"Idempotence-Key": str(attempt.reference)}, timeout=15)
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise PaymentError("Не удалось создать платёж ЮKassa") from exc
    if data.get("test") is not True or data.get("amount", {}).get("value") != str(order.total) or data.get("amount", {}).get("currency") != "RUB":
        raise PaymentError("Ответ ЮKassa не подтверждает тестовый платёж и сумму")
    provider_id = data.get("id", "")
    token = data.get("confirmation", {}).get("confirmation_token", "")
    if not provider_id or not token or len(provider_id) > 80 or len(token) > 255:
        raise PaymentError("ЮKassa не вернула токен виджета")
    attempt.provider_id = provider_id
    attempt.confirmation_token = token
    attempt.save(update_fields=["provider_id", "confirmation_token"])
    return token


def sync_payment(attempt):
    if attempt.gateway != "yookassa" or not attempt.provider_id:
        raise PaymentError("Платёж ЮKassa не найден")
    try:
        response = requests.get(f"{API}/{attempt.provider_id}", auth=credentials(), timeout=12)
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise PaymentError("Не удалось проверить платёж ЮKassa") from exc
    try:
        amount = Decimal(data["amount"]["value"])
    except (KeyError, InvalidOperation, TypeError) as exc:
        raise PaymentError("ЮKassa вернула неверную сумму") from exc
    if (data.get("id") != attempt.provider_id or data.get("test") is not True
            or data.get("amount", {}).get("currency") != "RUB"
            or amount != attempt.order.total
            or data.get("metadata", {}).get("attempt") != str(attempt.reference)
            or data.get("metadata", {}).get("order_id") != str(attempt.order_id)):
        raise PaymentError("Данные платежа ЮKassa не совпадают с заказом")
    status = data.get("status")
    if status not in ("succeeded", "canceled"):
        return False
    state = "paid" if status == "succeeded" else "declined"
    with transaction.atomic():
        locked = PaymentAttempt.objects.select_for_update().get(pk=attempt.pk)
        order = Order.objects.select_for_update().get(pk=attempt.order_id)
        if locked.state != "pending" or order.status == Order.Status.CANCELLED:
            return False
        if order.payment_state == "paid":
            return False
        PaymentEvent.objects.create(event_id=f"yoo:{locked.provider_id}:{status}", attempt=locked, status=state)
        locked.state = state
        locked.save(update_fields=["state"])
        order.payment_state = state
        if state == "paid":
            order.status = Order.Status.PAID
        order.save(update_fields=["payment_state", "status", "updated_at"])
        queue_shop(f"Заказ №{order.pk}: оплата ЮKassa {'успешна' if state == 'paid' else 'отклонена'}.")
        queue_buyer(order, f"Заказ №{order.pk}: {'оплачен' if state == 'paid' else 'оплата отклонена, можно повторить'}.")
    return True
