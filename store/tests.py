import hashlib
import hmac
import json
import time
import uuid
from decimal import Decimal
from unittest.mock import Mock, patch
from urllib.parse import urlencode
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from .models import BotUser, Category, Notification, Order, PaymentAttempt, PaymentEvent, Product
from .security import TelegramAuthError, verify_init_data
from .services import flush_notifications, process_payment_event, queue_order_changed, signed_event


TOKEN = "test-bot-token"
SECRET = "test-payment-secret"


def telegram_data(user_id=123, stamp=None):
    fields = {"auth_date": str(stamp or int(time.time())), "user": json.dumps({"id": user_id, "first_name": "Тест"}, separators=(",", ":"))}
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


@override_settings(BOT_TOKEN=TOKEN, PAYMENT_WEBHOOK_SECRET=SECRET, DEMO_PAYMENTS=True)
class ShopTests(TestCase):
    def setUp(self):
        category = Category.objects.create(name="Техника", slug="tech")
        self.product = Product.objects.create(category=category, name="Товар", slug="item", description="Описание", price=Decimal("1500.00"), availability="stock")

    def checkout(self, **changes):
        data = {"name": "Покупатель", "phone": "+7 900 123 45 67", "delivery": "pickup", "address": "", "comment": "", "payment_method": "online", "consent": "on", "cart": json.dumps({str(self.product.pk): 2}), "checkout_key": str(uuid.uuid4())}
        data.update(changes)
        return self.client.post(reverse("checkout"), data)

    def test_telegram_auth_valid_forged_expired(self):
        self.assertEqual(verify_init_data(telegram_data(), now=int(time.time())), 123)
        forged = telegram_data().replace("123", "999")
        with self.assertRaises(TelegramAuthError):
            verify_init_data(forged)
        with self.assertRaises(TelegramAuthError):
            verify_init_data(telegram_data(stamp=int(time.time()) - 90000))

    def test_checkout_server_price_and_delivery_payment_rule(self):
        bad = self.checkout(delivery="region", address="Барнаул", payment_method="receipt")
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(Order.objects.count(), 0)
        self.product.price = Decimal("1750.00")
        self.product.save()
        response = self.checkout(delivery="region", address="Барнаул", total="1")
        self.assertEqual(response.status_code, 302)
        order = Order.objects.get()
        self.assertEqual(order.total, Decimal("3500.00"))
        self.assertEqual(order.items.get().unit_price, Decimal("1750.00"))
        self.assertEqual(order.payment_state, "pending")

    def test_checkout_invalid_telegram_denied(self):
        response = self.checkout(tg_init_data=telegram_data().replace("123", "999"))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Order.objects.count(), 0)

    def test_checkout_repeat_is_idempotent_for_same_session(self):
        key = str(uuid.uuid4())
        first = self.checkout(checkout_key=key)
        second = self.checkout(checkout_key=key)
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(Order.objects.count(), 1)

    def test_webhook_fake_and_duplicate(self):
        self.checkout()
        order = Order.objects.get()
        attempt = order.attempts.get()
        body = json.dumps({"event_id": "evt-1", "attempt": str(attempt.reference), "status": "paid"}).encode()
        url = reverse("payment_webhook")
        self.assertEqual(self.client.post(url, body, content_type="application/json", HTTP_X_PAYMENT_SIGNATURE="bad").status_code, 403)
        self.assertEqual(order.payment_state, "pending")
        signature = signed_event(body)
        self.assertEqual(self.client.post(url, body, content_type="application/json", HTTP_X_PAYMENT_SIGNATURE=signature).status_code, 200)
        self.assertTrue(self.client.post(url, body, content_type="application/json", HTTP_X_PAYMENT_SIGNATURE=signature).json()["duplicate"])
        self.assertEqual(PaymentEvent.objects.count(), 1)
        order.refresh_from_db()
        self.assertEqual(order.payment_state, "paid")
        self.assertEqual(order.status, "paid")

    def test_decline_and_retry_demo_payment(self):
        self.checkout()
        order = Order.objects.get()
        url = reverse("demo_pay", args=[order.access_token])
        self.assertEqual(self.client.post(url, {"action": "declined"}).status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.payment_state, "declined")
        self.assertEqual(self.client.post(url, {"action": "paid"}).status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.payment_state, "paid")
        self.assertEqual(order.attempts.count(), 2)

    def test_foreign_telegram_user_cannot_access_order_with_token(self):
        self.checkout(tg_init_data=telegram_data(user_id=123))
        order = Order.objects.get()
        url = reverse("order", args=[order.access_token])
        self.assertEqual(self.client.get(url, HTTP_X_TELEGRAM_INIT_DATA=telegram_data(user_id=456)).status_code, 403)
        self.assertEqual(self.client.get(url, HTTP_X_TELEGRAM_INIT_DATA=telegram_data(user_id=123)).status_code, 200)
        self.assertEqual(self.client.get(reverse("order", args=["wrong-token"])).status_code, 404)

    def test_track_number_queues_buyer_notification(self):
        self.checkout(tg_init_data=telegram_data(user_id=123))
        order = Order.objects.get()
        order.track_number = "RU123456789"
        order.status = "shipped"
        order.save()
        queue_order_changed(order, "new", "")
        self.assertTrue(Notification.objects.filter(kind="buyer", text__contains="RU123456789").exists())

    def test_track_notification_delivery_and_retry(self):
        self.checkout(tg_init_data=telegram_data(user_id=123))
        order = Order.objects.get()
        order.track_number = "RU123456789"
        order.save()
        queue_order_changed(order, "new", "")
        notification = Notification.objects.get(kind="buyer", text__contains="RU123456789")
        flush_notifications()
        notification.refresh_from_db()
        self.assertEqual(notification.status, "failed")
        self.assertTrue(Order.objects.filter(pk=order.pk).exists())
        BotUser.objects.create(telegram_id=123, chat_id=123)
        notification.status = "pending"
        notification.save(update_fields=["status"])
        with patch("requests.post", return_value=Mock(status_code=200, json=lambda: {"ok": True})) as send:
            flush_notifications()
        notification.refresh_from_db()
        self.assertEqual(notification.status, "sent")
        self.assertEqual(send.call_args.kwargs["json"]["chat_id"], 123)

    def test_hidden_product_not_visible_or_orderable(self):
        self.product.is_visible = False
        self.product.save()
        self.assertEqual(self.client.get(reverse("product", args=[self.product.slug])).status_code, 404)
        self.assertEqual(self.checkout().status_code, 400)

    def test_catalog_basket_checkout_pages(self):
        for name in ("catalog", "basket", "checkout"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_csrf_rejects_null_origin_and_accepts_local_origin(self):
        client = Client(enforce_csrf_checks=True)
        client.get(reverse("checkout"), HTTP_HOST="localhost:8008")
        csrf_token = client.cookies["csrftoken"].value
        data = {"name": "Покупатель", "phone": "+7 900 123 45 67", "delivery": "pickup", "address": "", "comment": "", "payment_method": "online", "consent": "on", "cart": json.dumps({str(self.product.pk): 1}), "checkout_key": str(uuid.uuid4()), "csrfmiddlewaretoken": csrf_token}
        self.assertEqual(client.post(reverse("checkout"), data, HTTP_HOST="localhost:8008", HTTP_ORIGIN="null").status_code, 403)
        self.assertEqual(client.post(reverse("checkout"), data, HTTP_HOST="localhost:8008", HTTP_ORIGIN="http://localhost:8008").status_code, 302)
