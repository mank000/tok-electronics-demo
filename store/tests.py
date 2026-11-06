import hashlib
import hmac
import io
import json
import re
import tempfile
import time
import uuid
from datetime import datetime, time as datetime_time, timedelta
from decimal import Decimal
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlparse
from PIL import Image
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.core.management.base import CommandError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from .integrations import encrypt_secret, get_runtime_config
from .analytics import dashboard_data, month_at
from .models import BotUser, BrowserLoginCode, Category, ChatConversation, ChatMessage, Coupon, CustomerProfile, Favorite, IntegrationSettings, Notification, Order, OrderItem, PaymentAttempt, PaymentEvent, Product, ProductImage, ProductQuestion, ProductReview, RecentlyViewed, ReturnRequest, ShopSettings, SupportMessage, SupportTicket, Wish
from .phone_login import normalize_phone
from .security import TelegramAuthError, verify_init_data
from .services import flush_notifications, process_payment_event, queue_buyer, queue_order_changed, queue_shop, retry_buyer_notifications, signed_event
from .support import open_ticket, receive_message


TOKEN = "test-bot-token"
SECRET = "test-payment-secret"


def telegram_data(user_id=123, stamp=None):
    fields = {"auth_date": str(stamp or int(time.time())), "user": json.dumps({"id": user_id, "first_name": "Тест"}, separators=(",", ":"))}
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def image_upload(name, image_format, size=(900, 700)):
    output = io.BytesIO()
    Image.new("RGB", size, "#d0d9dd").save(output, format=image_format)
    return SimpleUploadedFile(name, output.getvalue(), content_type=f"image/{image_format.lower()}")


@override_settings(BOT_TOKEN=TOKEN, PAYMENT_WEBHOOK_SECRET=SECRET, DEMO_PAYMENTS=True)
class ShopTests(TestCase):
    def setUp(self):
        category = Category.objects.create(name="Техника", slug="tech")
        self.product = Product.objects.create(category=category, name="Товар", slug="item", description="Описание", price=Decimal("1500.00"), availability="stock")

    def test_compare_groups_visible_products_and_marks_differences(self):
        self.product.specs = {"Экран": "OLED", "Память": "128 ГБ"}
        self.product.save(update_fields=["specs"])
        second = Product.objects.create(category=self.product.category, name="Другой", slug="other", description="Описание", price=2000, specs={"Экран": "OLED", "Память": "256 ГБ"})
        hidden = Product.objects.create(category=self.product.category, name="Скрытый", slug="hidden", description="Описание", price=3000, is_visible=False)
        category = Category.objects.create(name="Аудио", slug="audio")
        audio = Product.objects.create(category=category, name="Наушники", slug="headphones", description="Описание", price=500)
        response = self.client.get(reverse("compare"), {"items": "item,other,hidden,headphones,other", "category": "tech"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item.slug for item in response.context["products"]], ["item", "other"])
        self.assertEqual([(row["label"], row["different"]) for row in response.context["rows"]], [("Наличие", False), ("Экран", False), ("Память", True)])
        self.assertEqual(response.context["compare_query"], "item,other,headphones")
        self.assertContains(response, "Только различия")
        self.assertNotContains(response, hidden.name)
        audio_response = self.client.get(reverse("compare"), {"items": "item,other,headphones", "category": "audio"})
        self.assertEqual([item.slug for item in audio_response.context["products"]], [audio.slug])

    def test_compare_limits_to_four_products_per_category_and_has_empty_state(self):
        for index in range(5):
            Product.objects.create(category=self.product.category, name=f"Товар {index}", slug=f"item-{index}", description="Описание", price=1000)
        response = self.client.get(reverse("compare"), {"items": "item,item-0,item-1,item-2,item-3,item-4"})
        self.assertEqual(len(response.context["products"]), 4)
        self.assertNotContains(response, "item-4")
        self.assertContains(self.client.get(reverse("compare")), "Здесь пока пусто")

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

    def test_scheduled_product_discount_is_recalculated_and_saved(self):
        now = timezone.now()
        self.product.discount_percent = 25
        self.product.discount_starts_at = now - timedelta(hours=1)
        self.product.discount_ends_at = now + timedelta(hours=1)
        self.product.save()
        quote = self.client.post(reverse("cart_quote"), json.dumps({"cart": {str(self.product.pk): 2}}), content_type="application/json").json()
        self.assertEqual((quote["subtotal"], quote["product_discount"], quote["total"]), ("3000.00", "750.00", "2250.00"))
        self.assertEqual(quote["items"][0]["price"], "1125.00")
        self.checkout(total="1")
        order = Order.objects.get()
        self.assertEqual((order.subtotal, order.product_discount, order.total), (Decimal("3000"), Decimal("750"), Decimal("2250")))
        self.assertEqual((order.items.get().list_unit_price, order.items.get().unit_price), (Decimal("1500"), Decimal("1125")))
        self.product.discount_ends_at = now - timedelta(seconds=1)
        self.product.save(update_fields=["discount_ends_at"])
        self.assertEqual(self.product.current_price, Decimal("1500"))

    def test_catalog_price_sort_uses_active_discount(self):
        cheaper = Product.objects.create(category=self.product.category, name="Со скидкой", slug="sale", description="Описание", price=2000, discount_percent=50)
        response = self.client.get(reverse("catalog"), {"sort": "price"})
        self.assertEqual([item.pk for item in response.context["products"]], [cheaper.pk, self.product.pk])

    def test_coupon_stack_limit_and_idempotent_checkout(self):
        self.product.discount_percent = 20
        self.product.save(update_fields=["discount_percent"])
        coupon = Coupon.objects.create(code="save10", kind="percent", value=10, min_total=2000, max_uses=1)
        quote = self.client.post(reverse("cart_quote"), json.dumps({"cart": {str(self.product.pk): 2}, "coupon_code": "save10"}), content_type="application/json").json()
        self.assertEqual((quote["product_discount"], quote["coupon_discount"], quote["total"]), ("600.00", "240.00", "2160.00"))
        key = str(uuid.uuid4())
        self.assertEqual(self.checkout(checkout_key=key, coupon_code="save10").status_code, 302)
        self.assertEqual(self.checkout(checkout_key=key, coupon_code="save10").status_code, 302)
        order = Order.objects.get()
        self.assertEqual((order.subtotal, order.product_discount, order.coupon_discount, order.total, order.coupon_code), (Decimal("3000"), Decimal("600"), Decimal("240"), Decimal("2160"), "SAVE10"))
        coupon.refresh_from_db()
        self.assertEqual(coupon.uses, 1)
        self.assertEqual(self.checkout(coupon_code="save10").status_code, 400)
        self.assertEqual(Order.objects.count(), 1)

    def test_coupon_fixed_amount_minimum_and_expiry(self):
        coupon = Coupon.objects.create(code="fixed", kind="fixed", value=5000, min_total=3000)
        data = {"cart": {str(self.product.pk): 2}, "coupon_code": "fixed"}
        quote = self.client.post(reverse("cart_quote"), json.dumps(data), content_type="application/json").json()
        self.assertEqual((quote["coupon_discount"], quote["total"]), ("2999.00", "1.00"))
        coupon.min_total = 3001
        coupon.save(update_fields=["min_total"])
        self.assertEqual(self.client.post(reverse("cart_quote"), json.dumps(data), content_type="application/json").status_code, 400)
        coupon.min_total = 0
        coupon.ends_at = timezone.now() - timedelta(seconds=1)
        coupon.save(update_fields=["min_total", "ends_at"])
        self.assertEqual(self.checkout(coupon_code="fixed").status_code, 400)
        self.assertEqual(Order.objects.count(), 0)

    def test_checkout_invalid_telegram_denied(self):
        response = self.checkout(tg_init_data=telegram_data().replace("123", "999"))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Order.objects.count(), 0)

    def test_malformed_telegram_data_is_denied_without_server_error(self):
        with self.assertRaises(TelegramAuthError):
            verify_init_data("broken")
        self.assertEqual(self.checkout(tg_init_data="broken").status_code, 403)
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
        retry_buyer_notifications(123)
        with patch("requests.post", return_value=Mock(status_code=200, json=lambda: {"ok": True})) as send:
            flush_notifications()
        notification.refresh_from_db()
        self.assertEqual(notification.status, "sent")
        self.assertEqual(send.call_args.kwargs["json"]["chat_id"], 123)

    @override_settings(SHOP_CHAT_ID="-100123")
    def test_transient_shop_notification_retries_before_failing(self):
        queue_shop("Проверка доставки")
        notification = Notification.objects.get(kind="shop")
        with patch("requests.post", return_value=Mock(status_code=503)):
            flush_notifications()
            notification.refresh_from_db()
            self.assertEqual((notification.status, notification.attempts), ("pending", 1))
            flush_notifications()
            notification.refresh_from_db()
            self.assertEqual((notification.status, notification.attempts), ("pending", 2))
        with patch("requests.post", return_value=Mock(status_code=200, json=lambda: {"ok": True})):
            flush_notifications()
        notification.refresh_from_db()
        self.assertEqual((notification.status, notification.attempts), ("sent", 3))

    @override_settings(SHOP_CHAT_ID="")
    def test_shop_notifications_stay_in_admin_without_optional_chat(self):
        queue_shop("Проверка доставки")
        Notification.objects.create(kind="shop", text="Старое сообщение", status="pending")
        with patch("requests.post") as send:
            flush_notifications()
        notification = Notification.objects.get(kind="shop", text="Проверка доставки")
        self.assertEqual(notification.status, "internal")
        self.assertEqual(Notification.objects.get(kind="shop", text="Старое сообщение").status, "internal")
        send.assert_not_called()
        with override_settings(SHOP_CHAT_ID="-100123"), patch("requests.post", return_value=Mock(status_code=200, json=lambda: {"ok": True})):
            queue_shop("Новое сообщение")
            flush_notifications()
        notification.refresh_from_db()
        self.assertEqual(notification.status, "internal")
        self.assertEqual(Notification.objects.get(kind="shop", text="Новое сообщение").status, "sent")

    @override_settings(BOT_USERNAME="test_bot", BOT_APP_SHORT_NAME="shop", SHOP_CHAT_ID="-100123")
    def test_start_command_delivers_previous_buyer_notifications(self):
        ShopSettings.objects.create(name="Моя техника", subtitle="Учебный магазин")
        notification = Notification.objects.create(kind="buyer", telegram_id=123, text="Статус заказа")
        flush_notifications()
        notification.refresh_from_db()
        self.assertEqual(notification.status, "failed")
        updates = {"ok": True, "result": [{"update_id": 1, "message": {"text": "/start", "from": {"id": 123}, "chat": {"id": 123, "type": "private"}}}]}
        with patch("requests.get", side_effect=[Mock(json=lambda: updates), KeyboardInterrupt]), patch("requests.post", return_value=Mock(status_code=200, json=lambda: {"ok": True})) as send:
            call_command("runbot")
        notification.refresh_from_db()
        self.assertEqual(notification.status, "sent")
        self.assertTrue(BotUser.objects.filter(telegram_id=123, chat_id=123).exists())
        self.assertTrue(any(call.kwargs.get("json", {}).get("text") == "Статус заказа" for call in send.call_args_list))
        self.assertTrue(any("Моя техника" in call.kwargs.get("json", {}).get("text", "") for call in send.call_args_list))

    @override_settings(BOT_USERNAME="test_bot", BOT_APP_SHORT_NAME="shop")
    def test_bot_keeps_only_own_shared_contact_for_phone_login(self):
        updates = {"ok": True, "result": [
            {"update_id": 1, "message": {"from": {"id": 123}, "chat": {"id": 123, "type": "private"}, "contact": {"user_id": 456, "phone_number": "+7 999 111-22-33"}}},
            {"update_id": 2, "message": {"from": {"id": 123}, "chat": {"id": 123, "type": "private"}, "contact": {"user_id": 123, "phone_number": "+7 900 123-45-67"}}},
        ]}
        with patch("requests.get", side_effect=[Mock(json=lambda: updates), KeyboardInterrupt]):
            call_command("runbot")
        self.assertEqual(BotUser.objects.get(telegram_id=123).phone_digits, "79001234567")
        self.assertFalse(BotUser.objects.filter(phone_digits="79991112233").exists())

    def test_hidden_product_not_visible_or_orderable(self):
        self.product.is_visible = False
        self.product.save()
        self.assertEqual(self.client.get(reverse("product", args=[self.product.slug])).status_code, 404)
        self.assertEqual(self.checkout().status_code, 400)

    def test_preorder_wish_is_saved_and_not_added_to_checkout(self):
        self.product.availability = "preorder"
        self.product.save()
        self.assertEqual(self.checkout().status_code, 400)
        response = self.client.post(reverse("wish", args=[self.product.slug]), HTTP_X_TELEGRAM_INIT_DATA=telegram_data())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.product.wishes.count(), 1)
        self.assertTrue(Notification.objects.filter(kind="shop", text__contains="Хочу такой").exists())

    def test_catalog_basket_checkout_pages(self):
        for name in ("catalog", "basket", "checkout"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_admin_shop_settings_update_storefront_and_convert_logo(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            admin_user = get_user_model().objects.create_superuser("admin", "admin@example.test", "test-password")
            self.client.force_login(admin_user)
            response = self.client.post(reverse("admin:store_shopsettings_add"), {
                "name": "Моя техника", "subtitle": "Учебный магазин", "logo": image_upload("logo.png", "PNG"),
                "contact_phone": "+7 900 123 45 67", "contact_email": "shop@example.test", "pickup_address": "Барнаул",
                "legal_name": "ООО Тест", "inn": "1234567890", "ogrn": "1234567890123", "legal_address": "Барнаул",
                "_save": "Сохранить",
            })
            self.assertEqual(response.status_code, 302)
            shop = ShopSettings.objects.get(pk=1)
            with Image.open(shop.logo.path) as logo:
                self.assertEqual(logo.format, "WEBP")
                self.assertLessEqual(max(logo.size), 640)
            page = self.client.get(reverse("catalog"))
            self.assertContains(page, "Моя техника")
            self.assertContains(page, "ИНН 1234567890")
            self.assertContains(page, shop.logo.url)
            self.assertContains(self.client.get(reverse("document", args=["offer"])), "Заглушка для учебного проекта")
            self.assertEqual(self.client.get(reverse("admin:store_shopsettings_add")).status_code, 403)

    def test_admin_integrations_keep_secrets_hidden_and_apply_without_restart(self):
        secret = "a-separate-payment-secret-with-at-least-32-characters"
        with override_settings(BOT_TOKEN="", BOT_USERNAME="", BOT_APP_SHORT_NAME="", SHOP_CHAT_ID="", PAYMENT_WEBHOOK_SECRET=""):
            user = get_user_model().objects.create_superuser("admin", "admin@example.test", "test-password")
            self.client.force_login(user)
            data = {
                "bot_token": TOKEN, "bot_username": "test_bot", "bot_app_short_name": "shop",
                "public_base_url": "https://shop.example.test", "shop_chat_id": "-100123",
                "payment_webhook_secret": secret, "_save": "Сохранить",
            }
            self.assertEqual(self.client.post(reverse("admin:store_integrationsettings_add"), data).status_code, 302)
            saved = IntegrationSettings.objects.get(pk=1)
            self.assertNotIn(TOKEN, saved.bot_token_ciphertext)
            self.assertNotIn(secret, saved.payment_secret_ciphertext)
            self.assertEqual(get_runtime_config().bot_token, TOKEN)
            self.assertEqual(get_runtime_config().payment_webhook_secret, secret)
            self.assertEqual(verify_init_data(telegram_data()), 123)
            raw = b"payment-event"
            self.assertEqual(signed_event(raw), hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest())
            change_url = reverse("admin:store_integrationsettings_change", args=[1])
            page = self.client.get(change_url)
            self.assertNotContains(page, TOKEN)
            self.assertNotContains(page, secret)
            queue_shop("Проверка новых настроек")
            with patch("requests.post", return_value=Mock(status_code=200, json=lambda: {"ok": True})) as send:
                flush_notifications()
            self.assertIn(TOKEN, send.call_args.args[0])
            self.assertEqual(send.call_args.kwargs["json"]["chat_id"], "-100123")
            data["bot_token"] = ""
            data["payment_webhook_secret"] = ""
            self.assertEqual(self.client.post(change_url, data).status_code, 302)
            self.assertEqual(get_runtime_config().bot_token, TOKEN)
            self.assertEqual(get_runtime_config().payment_webhook_secret, secret)
            data["bot_token"] = "new-token"
            self.assertEqual(self.client.post(change_url, data).status_code, 302)
            self.assertEqual(get_runtime_config().bot_token, "new-token")
            data["bot_token"] = ""
            data["clear_bot_token"] = "on"
            self.assertEqual(self.client.post(change_url, data).status_code, 302)
            self.assertEqual(get_runtime_config().bot_token, "")

    def test_bot_waits_for_admin_token_then_uses_it(self):
        with override_settings(BOT_TOKEN="", BOT_USERNAME="", BOT_APP_SHORT_NAME="", PUBLIC_BASE_URL=""):
            def configure_during_wait(_seconds):
                IntegrationSettings.objects.create(
                    bot_token_ciphertext=encrypt_secret(TOKEN),
                    bot_username="test_bot", bot_app_short_name="shop",
                )

            with patch("time.sleep", side_effect=configure_during_wait) as wait, patch("requests.get", side_effect=[Mock(json=lambda: {"ok": True, "result": []}), KeyboardInterrupt]) as poll:
                call_command("runbot")
            wait.assert_called_once_with(5)
            self.assertIn(TOKEN, poll.call_args_list[0].args[0])

    def test_admin_integrations_rejects_http_url_and_short_payment_secret(self):
        user = get_user_model().objects.create_superuser("admin", "admin@example.test", "test-password")
        self.client.force_login(user)
        response = self.client.post(reverse("admin:store_integrationsettings_add"), {
            "bot_token": TOKEN, "public_base_url": "http://shop.example.test",
            "payment_webhook_secret": "short", "_save": "Сохранить",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Для Mini App нужен HTTPS-адрес")
        self.assertEqual(IntegrationSettings.objects.count(), 0)

    @override_settings(
        DEBUG=True, TELEGRAM_LOGIN_CLIENT_ID="", TELEGRAM_LOGIN_CLIENT_SECRET="",
        YOOKASSA_SHOP_ID="", YOOKASSA_SECRET_KEY="",
    )
    def test_admin_payment_and_login_credentials_apply_without_restart(self):
        from .telegram_login import configured as telegram_login_configured
        from .yookassa import credentials as yookassa_credentials

        user = get_user_model().objects.create_superuser("admin", "admin@example.test", "test-password")
        self.client.force_login(user)
        add_url = reverse("admin:store_integrationsettings_add")
        data = {
            "telegram_login_client_id": "telegram-client",
            "telegram_login_client_secret": "telegram-secret-value",
            "yookassa_shop_id": "test-shop",
            "yookassa_secret_key": "test-key-value",
            "_save": "Сохранить",
        }
        self.assertEqual(self.client.post(add_url, data).status_code, 302)
        saved = IntegrationSettings.objects.get(pk=1)
        self.assertNotIn(data["telegram_login_client_secret"], saved.telegram_login_secret_ciphertext)
        self.assertNotIn(data["yookassa_secret_key"], saved.yookassa_secret_ciphertext)
        config = get_runtime_config()
        self.assertEqual(config.telegram_login_client_id, "telegram-client")
        self.assertEqual(config.telegram_login_client_secret, "telegram-secret-value")
        self.assertEqual(config.yookassa_shop_id, "test-shop")
        self.assertEqual(yookassa_credentials(), ("test-shop", "test-key-value"))
        self.assertTrue(telegram_login_configured())
        self.assertContains(self.client.get(reverse("checkout")), "ЮKassa работает через тестовый магазин")
        login = self.client.get(reverse("telegram_login_start"))
        self.assertEqual(login.status_code, 302)
        self.assertEqual(parse_qs(urlparse(login["Location"]).query)["client_id"], ["telegram-client"])

        change_url = reverse("admin:store_integrationsettings_change", args=[1])
        page = self.client.get(change_url)
        self.assertNotContains(page, "telegram-secret-value")
        self.assertNotContains(page, "test-key-value")
        data["telegram_login_client_secret"] = ""
        data["yookassa_secret_key"] = ""
        self.assertEqual(self.client.post(change_url, data).status_code, 302)
        self.assertEqual(yookassa_credentials(), ("test-shop", "test-key-value"))
        self.assertTrue(telegram_login_configured())

        with override_settings(TELEGRAM_LOGIN_CLIENT_ID="env-client", TELEGRAM_LOGIN_CLIENT_SECRET="env-secret",
                           YOOKASSA_SHOP_ID="env-shop", YOOKASSA_SECRET_KEY="env-key"):
            data.update({"telegram_login_client_id": "", "yookassa_shop_id": "",
                         "clear_telegram_login_client_secret": "on", "clear_yookassa_secret_key": "on"})
            self.assertEqual(self.client.post(change_url, data).status_code, 302)
            self.assertEqual(yookassa_credentials(), ("env-shop", "env-key"))
            self.assertEqual(get_runtime_config().telegram_login_client_secret, "env-secret")
            self.assertTrue(telegram_login_configured())

    def test_integration_secrets_admin_requires_superuser_even_with_model_permissions(self):
        staff = get_user_model().objects.create_user("staff", password="test-password", is_staff=True)
        staff.user_permissions.set(Permission.objects.filter(content_type__app_label="store", content_type__model="integrationsettings"))
        self.client.force_login(staff)
        self.assertEqual(self.client.get(reverse("admin:store_integrationsettings_add")).status_code, 403)
        self.assertNotContains(self.client.get(reverse("admin:index")), "Подключение Telegram и оплаты")

    def test_admin_product_photo_preview_uses_converted_webp(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            admin_user = get_user_model().objects.create_superuser("admin", "admin@example.test", "test-password")
            self.client.force_login(admin_user)
            url = reverse("admin:store_product_change", args=[self.product.pk])
            response = self.client.post(url, {
                "category": self.product.category_id, "name": self.product.name, "slug": self.product.slug,
                "description": "Обновлённое описание", "specs": "{}", "price": "1500.00", "discount_percent": "0", "availability": "stock",
                "is_visible": "on", "images-TOTAL_FORMS": "1", "images-INITIAL_FORMS": "0",
                "images-MIN_NUM_FORMS": "0", "images-MAX_NUM_FORMS": "1000",
                "images-0-image": image_upload("camera.jpg", "JPEG", (1500, 1000)),
                "images-0-alt": "Тестовый снимок", "images-0-order": "0", "_save": "Сохранить",
            })
            self.assertEqual(response.status_code, 302)
            self.product.refresh_from_db()
            self.assertEqual(self.product.description, "Обновлённое описание")
            photo = self.product.images.get()
            with Image.open(photo.image.path) as main, Image.open(photo.thumb.path) as thumb:
                self.assertEqual((main.format, thumb.format), ("WEBP", "WEBP"))
                self.assertLessEqual(max(main.size), 1200)
                self.assertLessEqual(max(thumb.size), 480)
            page = self.client.get(url)
            self.assertContains(page, "Текущее фото")
            self.assertContains(page, photo.thumb.url)

    def test_category_sort_form_keeps_selected_category(self):
        response = self.client.get(reverse("catalog"), {"category": "tech", "sort": "price"})
        self.assertContains(response, 'name="category" value="tech"')
        self.assertContains(response, "Товар")

    @override_settings(DEBUG=True)
    def test_local_checkout_and_cart_accept_null_origin_with_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.get(reverse("checkout"), HTTP_HOST="localhost:8008")
        csrf_token = client.cookies["csrftoken"].value
        data = {"name": "Покупатель", "phone": "+7 900 123 45 67", "delivery": "pickup", "address": "", "comment": "", "payment_method": "online", "consent": "on", "cart": json.dumps({str(self.product.pk): 1}), "checkout_key": str(uuid.uuid4()), "csrfmiddlewaretoken": csrf_token}
        self.assertEqual(client.post(reverse("checkout"), data, HTTP_HOST="localhost:8008", HTTP_ORIGIN="null").status_code, 302)
        self.assertEqual(Order.objects.count(), 1)
        quote = client.post(reverse("cart_quote"), json.dumps({"cart": {str(self.product.pk): 1}}), content_type="application/json", HTTP_HOST="localhost:8008", HTTP_ORIGIN="null", HTTP_X_CSRFTOKEN=csrf_token)
        self.assertEqual(quote.status_code, 200)
        self.assertEqual(client.post(reverse("cart_quote"), "{}", content_type="application/json", HTTP_HOST="localhost:8008", HTTP_ORIGIN="null").status_code, 403)
        with override_settings(DEBUG=False):
            self.assertEqual(client.post(reverse("checkout"), data, HTTP_HOST="localhost:8008", HTTP_ORIGIN="null").status_code, 403)
        self.assertEqual(client.post(reverse("checkout"), data, HTTP_HOST="localhost:8008", HTTP_ORIGIN="null", HTTP_X_FORWARDED_PROTO="https").status_code, 403)

    @override_settings(DEBUG=True, ALLOWED_HOSTS=["shop.example.test"], CSRF_TRUSTED_ORIGINS=["https://shop.example.test"], DEMO_NULL_ORIGIN_HOSTS=["shop.example.test"])
    def test_demo_https_tunnel_checkout_accepts_null_origin_with_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        request_headers = {"HTTP_HOST": "shop.example.test", "HTTP_X_FORWARDED_PROTO": "https", "HTTP_ORIGIN": "null"}
        client.get(reverse("checkout"), **request_headers)
        token = client.cookies["csrftoken"].value
        data = {"name": "Покупатель", "phone": "+7 900 123 45 67", "delivery": "pickup", "address": "", "comment": "", "payment_method": "online", "consent": "on", "cart": json.dumps({str(self.product.pk): 1}), "checkout_key": str(uuid.uuid4()), "csrfmiddlewaretoken": token}
        self.assertEqual(client.post(reverse("checkout"), data, **request_headers).status_code, 302)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(client.post(reverse("checkout"), {"name": "Покупатель"}, **request_headers).status_code, 403)
        with override_settings(DEMO_NULL_ORIGIN_HOSTS=[]):
            self.assertEqual(client.post(reverse("checkout"), data, **request_headers).status_code, 403)
        with override_settings(DEBUG=False):
            self.assertEqual(client.post(reverse("checkout"), data, **request_headers).status_code, 403)

    @override_settings(DEBUG=True)
    def test_local_admin_login_allows_null_origin_only_with_csrf_token(self):
        login_url = reverse("admin:login")
        get_user_model().objects.create_superuser("local_admin", "admin@example.test", "test-password")
        client = Client(enforce_csrf_checks=True)
        client.get(login_url, HTTP_HOST="localhost:8008")
        token = client.cookies["csrftoken"].value
        credentials = {"username": "local_admin", "password": "test-password", "csrfmiddlewaretoken": token}
        self.assertEqual(client.post(login_url, credentials, HTTP_HOST="localhost:8008", HTTP_ORIGIN="null").status_code, 302)
        self.assertEqual(client.get(reverse("admin:index"), HTTP_HOST="localhost:8008").status_code, 200)
        credentials["csrfmiddlewaretoken"] = client.cookies["csrftoken"].value
        self.assertEqual(client.post(login_url, {"username": "wrong", "password": "wrong"}, HTTP_HOST="localhost:8008", HTTP_ORIGIN="null").status_code, 403)
        with override_settings(DEBUG=False):
            self.assertEqual(client.post(login_url, credentials, HTTP_HOST="localhost:8008", HTTP_ORIGIN="null").status_code, 403)
        with override_settings(ALLOWED_HOSTS=["example.test"]):
            self.assertEqual(client.post(login_url, credentials, HTTP_HOST="example.test", HTTP_ORIGIN="null").status_code, 403)
        self.assertEqual(client.post(login_url, credentials, HTTP_HOST="localhost:8008", HTTP_ORIGIN="null", HTTP_X_FORWARDED_PROTO="https").status_code, 403)
        self.assertEqual(client.post(login_url, credentials, HTTP_HOST="localhost:8008", HTTP_ORIGIN="http://attacker.test").status_code, 403)


class SeedTests(TestCase):
    def test_seed_is_idempotent_and_images_are_local_webp(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            call_command("seed_catalog", verbosity=0)
            self.assertIn("Компактный смартфон", Product.objects.get(slug="phones-1").description)
            edited = Product.objects.get(slug="phones-2")
            edited.description = "Текст магазина"
            edited.save(update_fields=["description"])
            call_command("seed_catalog", verbosity=0)
            self.assertEqual(Product.objects.get(slug="phones-2").description, "Текст магазина")
            self.assertEqual(Product.objects.count(), 29)
            self.assertEqual(ProductImage.objects.count(), 31)
            self.assertTrue(all(product.images.exists() for product in Product.objects.all()))
            image = ProductImage.objects.first()
            with Image.open(image.image.path) as source, Image.open(image.thumb.path) as thumb:
                self.assertEqual(source.format, "WEBP")
                self.assertEqual(thumb.format, "WEBP")
                self.assertLessEqual(max(thumb.size), 480)

            with override_settings(DEBUG=True):
                call_command("seed_demo_feedback", verbosity=0)
                self.assertEqual(ProductReview.objects.filter(is_demo=True).count(), 58)
                self.assertEqual(ProductQuestion.objects.filter(is_demo=True).count(), 58)
                self.assertEqual(Order.objects.filter(is_demo=True).count(), 10)
                queue_buyer(Order.objects.filter(is_demo=True).first(), "Учебное сообщение")
                self.assertFalse(Notification.objects.filter(text="Учебное сообщение").exists())
                for review in ProductReview.objects.filter(is_demo=True).select_related("order_item__order"):
                    review.full_clean()
                hidden = ProductReview.objects.filter(is_demo=True).first()
                hidden.is_published = False
                hidden.text = "Текст модератора"
                hidden.save(update_fields=["is_published", "text"])
                call_command("seed_demo_feedback", verbosity=0)
                hidden.refresh_from_db()
                self.assertFalse(hidden.is_published)
                self.assertEqual(hidden.text, "Текст модератора")
                self.assertEqual(ProductReview.objects.filter(is_demo=True).count(), 58)
            with override_settings(DEBUG=False):
                with self.assertRaises(CommandError):
                    call_command("seed_demo_feedback", verbosity=0)

@override_settings(BOT_TOKEN=TOKEN, PAYMENT_WEBHOOK_SECRET=SECRET, DEMO_PAYMENTS=True)
class CommerceFeaturesTests(TestCase):
    def setUp(self):
        category = Category.objects.create(name="Смартфоны", slug="phones")
        self.product = Product.objects.create(category=category, name="Телефон", slug="phone", description="Описание", price=Decimal("1500.00"))

    def checkout(self, client=None, **changes):
        data = {"name": "Покупатель", "phone": "+7 900 123 45 67", "delivery": "pickup", "address": "", "comment": "", "payment_method": "online", "payment_gateway": "demo", "consent": "on", "cart": json.dumps({str(self.product.pk): 1}), "checkout_key": str(uuid.uuid4())}
        data.update(changes)
        return (client or self.client).post(reverse("checkout"), data)

    def test_browser_account_requires_telegram_login(self):
        self.assertEqual(self.checkout().status_code, 302)
        self.assertEqual(self.client.get(reverse("account_data")).status_code, 401)
        self.assertEqual(self.client.post(reverse("favorite_toggle", args=[self.product.slug])).status_code, 401)
        stranger = Client()
        self.assertEqual(stranger.get(reverse("account_data")).status_code, 401)
        self.assertEqual(Favorite.objects.count(), 0)

    def test_telegram_cabinet_and_favorites_are_identity_scoped(self):
        self.assertEqual(self.checkout(tg_init_data=telegram_data(123)).status_code, 302)
        self.assertEqual(Order.objects.get().name, "Тест")
        self.client.post(reverse("favorite_toggle", args=[self.product.slug]), HTTP_X_TELEGRAM_INIT_DATA=telegram_data(123))
        mine = self.client.get(reverse("account_data"), HTTP_X_TELEGRAM_INIT_DATA=telegram_data(123)).json()
        others = self.client.get(reverse("account_data"), HTTP_X_TELEGRAM_INIT_DATA=telegram_data(456)).json()
        self.assertEqual(len(mine["current"]), 1)
        self.assertEqual(len(mine["favorites"]), 1)
        self.assertEqual(others["current"], [])
        self.assertEqual(others["favorites"], [])
        self.assertEqual(self.client.get(reverse("account_data"), HTTP_X_TELEGRAM_INIT_DATA="bad").status_code, 403)

    def test_reviews_questions_moderation_and_related_products(self):
        sibling = Product.objects.create(category=self.product.category, name="Другой", slug="other", description="Описание", price=Decimal("900.00"))
        auth = {"HTTP_X_TELEGRAM_INIT_DATA": telegram_data(123)}
        review_url = reverse("review", args=[self.product.slug])
        self.assertEqual(self.client.post(review_url, {"rating": 5, "text": "Хороший товар"}, **auth).status_code, 403)
        self.assertEqual(self.checkout(tg_init_data=telegram_data(123)).status_code, 302)
        order = Order.objects.get()
        self.assertEqual(self.client.post(review_url, {"rating": 5, "text": "Хороший товар"}, **auth).status_code, 403)
        order.status = Order.Status.DELIVERED
        order.save()
        self.assertEqual(self.client.post(review_url, {"rating": 5, "text": "Хороший товар"}, **auth).status_code, 403)
        order.payment_state = "paid"
        order.save()
        self.assertEqual(self.client.post(review_url, {"author": "Подмена", "rating": 5, "text": "Хороший товар"}, **auth).status_code, 302)
        self.assertEqual(self.client.post(review_url, {"rating": 4, "text": "Повтор"}, **auth).status_code, 400)
        self.assertEqual(self.client.post(reverse("question", args=[self.product.slug]), {"author": "Подмена", "text": "Есть гарантия?"}, **auth).status_code, 302)
        review = ProductReview.objects.get()
        self.assertEqual(review.author, "Тест")
        self.assertEqual(review.order_item, order.items.get())
        self.assertEqual(ProductQuestion.objects.get().author, "Тест")
        page = self.client.get(reverse("product", args=[self.product.slug]))
        self.assertContains(page, "Другой")
        self.assertContains(page, "Примерная дата доставки")
        self.assertNotContains(page, "Хороший товар")
        self.assertNotContains(page, "Есть гарантия?")
        admin_user = get_user_model().objects.create_superuser("review_admin", "admin@example.test", "test-password")
        self.client.force_login(admin_user)
        from django.contrib import admin
        model_admin = admin.site._registry[ProductReview]
        model_admin.publish_reviews(None, ProductReview.objects.all())
        self.assertTrue(ProductReview.objects.get().is_published)
        ProductQuestion.objects.update(is_published=True, answer="Да")
        page = self.client.get(reverse("product", args=[self.product.slug]))
        self.assertContains(page, "Хороший товар")
        self.assertContains(page, "Есть гарантия?")
        self.assertContains(page, "Да")
        self.assertEqual(sibling.category_id, self.product.category_id)

    def test_account_sections_and_recent_views(self):
        auth = {"HTTP_X_TELEGRAM_INIT_DATA": telegram_data(123)}
        self.assertEqual(self.client.post(reverse("record_view", args=[self.product.slug]), **auth).status_code, 200)
        self.assertEqual(self.client.post(reverse("favorite_toggle", args=[self.product.slug]), **auth).json()["favorite"], True)
        self.assertEqual(RecentlyViewed.objects.filter(telegram_id=123).count(), 1)
        for section in ("orders", "purchases", "wishes", "favorites"):
            self.assertEqual(self.client.get(reverse("account_section", args=[section])).status_code, 200)
        data = self.client.get(reverse("account_data"), **auth).json()
        self.assertEqual(data["profile"]["name"], "Тест")
        self.assertEqual(len(data["recent"]), 1)
        self.assertEqual(len(data["favorites"]), 1)
        self.assertNotIn("phone", data["profile"])

    @override_settings(DEBUG=True, TELEGRAM_LOGIN_CLIENT_ID="12345", TELEGRAM_LOGIN_CLIENT_SECRET="oidc-secret")
    def test_browser_telegram_login_links_previous_order_and_uses_verified_claims(self):
        self.assertEqual(self.checkout().status_code, 302)
        order = Order.objects.get()
        self.assertIsNone(order.telegram_id)
        start = self.client.get(reverse("telegram_login_start"), {"next": reverse("account_section", args=["purchases"])})
        self.assertEqual(start.status_code, 302)
        params = parse_qs(urlparse(start["Location"]).query)
        self.assertEqual(params["scope"], ["openid profile"])
        self.assertEqual(params["code_challenge_method"], ["S256"])
        flow = self.client.session["telegram_oidc_flow"]
        claims = {"id": 123, "sub": "telegram-user", "name": "Имя из Telegram", "nonce": flow["nonce"]}
        with patch("store.telegram_login.requests.post", return_value=Mock(status_code=200, json=lambda: {"id_token": "signed-token"})) as post, \
                patch("store.telegram_login.jwt.PyJWKClient") as jwks, \
                patch("store.telegram_login.jwt.decode", return_value=claims) as decode:
            jwks.return_value.get_signing_key_from_jwt.return_value.key = "public-key"
            callback = self.client.get(reverse("telegram_login_callback"), {"state": params["state"][0], "code": "short-code"})
        self.assertEqual(callback.status_code, 302)
        self.assertEqual(callback["Location"], reverse("account_section", args=["purchases"]))
        self.assertEqual(post.call_args.kwargs["data"]["code_verifier"], flow["verifier"])
        self.assertEqual(decode.call_args.kwargs["algorithms"], ["RS256"])
        self.assertEqual(decode.call_args.kwargs["audience"], "12345")
        order.refresh_from_db()
        self.assertEqual(order.telegram_id, 123)
        account = self.client.get(reverse("account_data")).json()
        self.assertEqual(account["profile"], {"name": "Имя из Telegram"})
        self.assertEqual(account["current"][0]["id"], order.pk)
        self.assertNotIn("phone", account["profile"])
        self.assertEqual(self.client.post(reverse("favorite_toggle", args=[self.product.slug])).json()["favorite"], True)
        self.assertEqual(self.client.post(reverse("telegram_logout")).status_code, 302)
        self.assertEqual(self.client.get(reverse("account_data")).status_code, 401)

    @override_settings(DEBUG=True, TELEGRAM_LOGIN_CLIENT_ID="12345", TELEGRAM_LOGIN_CLIENT_SECRET="oidc-secret")
    def test_browser_telegram_login_rejects_wrong_state_and_nonce(self):
        self.client.get(reverse("telegram_login_start"))
        with patch("store.telegram_login.requests.post") as post:
            result = self.client.get(reverse("telegram_login_callback"), {"state": "forged", "code": "code"})
        self.assertEqual(result.status_code, 302)
        post.assert_not_called()
        self.assertEqual(self.client.get(reverse("account_data")).status_code, 401)
        start = self.client.get(reverse("telegram_login_start"))
        state = parse_qs(urlparse(start["Location"]).query)["state"][0]
        with patch("store.telegram_login.requests.post", return_value=Mock(status_code=200, json=lambda: {"id_token": "signed-token"})), \
                patch("store.telegram_login.jwt.PyJWKClient") as jwks, \
                patch("store.telegram_login.jwt.decode", return_value={"id": 123, "sub": "telegram-user", "name": "Тест", "nonce": "wrong"}):
            jwks.return_value.get_signing_key_from_jwt.return_value.key = "public-key"
            result = self.client.get(reverse("telegram_login_callback"), {"state": state, "code": "code"})
        self.assertEqual(result.status_code, 302)
        self.assertEqual(self.client.get(reverse("account_data")).status_code, 401)

    def test_miniapp_registration_needs_own_shared_contact(self):
        auth = {"HTTP_X_TELEGRAM_INIT_DATA": telegram_data(123)}
        url = reverse("registration")
        self.assertEqual(self.client.get(url, **auth).json()["registered"], False)
        self.assertEqual(self.client.post(url, {"name": "Аня"}, **auth).status_code, 409)
        self.assertEqual(self.client.post(url, {"name": "Аня"}).status_code, 403)
        BotUser.objects.create(telegram_id=123, chat_id=123, phone="+7 900 123-45-67", phone_digits="79001234567")
        self.assertEqual(self.client.get(url, **auth).json()["has_phone"], True)
        self.assertEqual(self.client.post(url, {"name": "  Анна   Тест  "}, **auth).json()["registered"], True)
        self.assertEqual(CustomerProfile.objects.get(telegram_id=123).name, "Анна Тест")
        self.assertEqual(self.client.get(reverse("account_data"), **auth).json()["profile"]["name"], "Анна Тест")
        self.assertNotIn("phone", self.client.get(reverse("account_data"), **auth).json()["profile"])

    def test_browser_phone_code_links_registered_account_and_guest_order(self):
        self.assertEqual(normalize_phone("8 (900) 123-45-67"), "79001234567")
        BotUser.objects.create(telegram_id=123, chat_id=987, phone="+7 900 123-45-67", phone_digits="79001234567")
        CustomerProfile.objects.create(telegram_id=123, name="Анна Тест")
        browser = Client()
        self.assertEqual(self.checkout(client=browser).status_code, 302)
        order = Order.objects.get()
        with patch("store.phone_login.requests.post", return_value=Mock(status_code=200, json=lambda: {"ok": True})) as send:
            response = browser.post(reverse("phone_login_request"), {"phone": "8 900 123 45 67", "next": reverse("account")})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(browser.post(reverse("phone_login_request"), {"phone": "+7 900 123-45-67"}).status_code, 200)
        send.assert_called_once()
        self.assertEqual(send.call_args.kwargs["json"]["chat_id"], 987)
        code = re.search(r"\b\d{6}\b", send.call_args.kwargs["json"]["text"]).group()
        self.assertNotEqual(BrowserLoginCode.objects.get().code_hash, code)
        self.assertEqual(browser.post(reverse("phone_login_verify"), {"code": "999999" if code != "999999" else "888888"}).status_code, 400)
        success = browser.post(reverse("phone_login_verify"), {"code": code})
        self.assertEqual(success.status_code, 200)
        self.assertEqual(success.json()["redirect"], reverse("account"))
        order.refresh_from_db()
        self.assertEqual(order.telegram_id, 123)
        self.assertEqual(browser.get(reverse("account_data")).json()["profile"]["name"], "Анна Тест")
        self.assertEqual(browser.post(reverse("phone_login_verify"), {"code": code}).status_code, 400)

    def test_phone_code_does_not_send_to_unknown_number_and_expires(self):
        BotUser.objects.create(telegram_id=123, chat_id=123, phone="+7 900 123-45-67", phone_digits="79001234567")
        CustomerProfile.objects.create(telegram_id=123, name="Анна")
        with patch("store.phone_login.requests.post", return_value=Mock(status_code=200, json=lambda: {"ok": True})) as send:
            unknown = self.client.post(reverse("phone_login_request"), {"phone": "+7 999 111-22-33"})
            self.assertEqual(unknown.status_code, 200)
            send.assert_not_called()
            self.client.post(reverse("phone_login_request"), {"phone": "+7 900 123-45-67"})
        BrowserLoginCode.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.client.post(reverse("phone_login_verify"), {"code": "000000"}).status_code, 400)
        self.assertEqual(self.client.get(reverse("account_data")).status_code, 401)

    def test_phone_login_requests_require_csrf_token(self):
        browser = Client(enforce_csrf_checks=True)
        browser.get(reverse("account"))
        self.assertEqual(browser.post(reverse("phone_login_request"), {"phone": "+7 900 123-45-67"}).status_code, 403)
        self.assertEqual(browser.post(reverse("phone_login_verify"), {"code": "123456"}).status_code, 403)

    def test_phone_code_locks_after_five_wrong_attempts(self):
        BotUser.objects.create(telegram_id=123, chat_id=123, phone="+7 900 123-45-67", phone_digits="79001234567")
        CustomerProfile.objects.create(telegram_id=123, name="Анна")
        with patch("store.phone_login.requests.post", return_value=Mock(status_code=200, json=lambda: {"ok": True})) as send:
            self.client.post(reverse("phone_login_request"), {"phone": "+7 900 123-45-67"})
        code = re.search(r"\b\d{6}\b", send.call_args.kwargs["json"]["text"]).group()
        wrong = "999999" if code != "999999" else "888888"
        for _ in range(5):
            self.assertEqual(self.client.post(reverse("phone_login_verify"), {"code": wrong}).status_code, 400)
        self.assertEqual(self.client.post(reverse("phone_login_verify"), {"code": code}).status_code, 400)
        self.assertEqual(self.client.get(reverse("account_data")).status_code, 401)

    def test_return_request_only_for_delivered_order_and_once_per_item(self):
        self.checkout()
        order = Order.objects.get()
        item = order.items.get()
        url = reverse("request_return", args=[order.access_token, item.pk])
        payload = {"quantity": 1, "reason": "defect", "comment": "Не включается"}
        self.assertEqual(self.client.post(url, payload).status_code, 400)
        order.status = "delivered"
        order.save()
        self.assertEqual(self.client.post(url, payload).status_code, 302)
        self.assertEqual(self.client.post(url, payload).status_code, 400)
        self.assertEqual(ReturnRequest.objects.count(), 1)
        self.assertTrue(Notification.objects.filter(kind="shop", text__contains="Заявка на возврат").exists())

    @override_settings(YOOKASSA_SHOP_ID="test-shop", YOOKASSA_SECRET_KEY="test-secret")
    def test_yookassa_widget_and_verified_status(self):
        self.assertEqual(self.checkout(payment_gateway="yookassa").status_code, 302)
        order = Order.objects.get()
        attempt = order.attempts.get()
        def payment(status):
            return {"id": "provider-1", "test": True, "amount": {"value": "1500.00", "currency": "RUB"}, "metadata": {"order_id": str(order.pk), "attempt": str(attempt.reference)}, "status": status, "confirmation": {"confirmation_token": "ct-test"}}
        with patch("requests.post", return_value=Mock(status_code=200, json=lambda: payment("pending"))) as create:
            response = self.client.get(reverse("yoo_pay", args=[order.access_token]))
        self.assertContains(response, "ct-test")
        self.assertEqual(create.call_args.kwargs["headers"]["Idempotence-Key"], str(attempt.reference))
        with patch("requests.get", return_value=Mock(status_code=200, json=lambda: payment("succeeded"))):
            response = self.client.post(reverse("yoo_status", args=[order.access_token]))
            self.assertEqual(response.json()["state"], "paid")
            self.assertEqual(self.client.post(reverse("yoo_webhook"), json.dumps({"object": {"id": "provider-1"}}), content_type="application/json").status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, "paid")
        self.assertEqual(PaymentEvent.objects.filter(event_id="yoo:provider-1:succeeded").count(), 1)

    @override_settings(YOOKASSA_SHOP_ID="test-shop", YOOKASSA_SECRET_KEY="test-secret")
    def test_yookassa_rejects_wrong_amount_and_live_store(self):
        self.checkout(payment_gateway="yookassa")
        order = Order.objects.get()
        attempt = order.attempts.get()
        live = {"id": "provider-1", "test": False, "amount": {"value": "1500.00", "currency": "RUB"}, "confirmation": {"confirmation_token": "ct-test"}}
        with patch("requests.post", return_value=Mock(status_code=200, json=lambda: live)):
            self.assertEqual(self.client.get(reverse("yoo_pay", args=[order.access_token])).status_code, 503)
        attempt.provider_id = "provider-1"
        attempt.save()
        fake = {"id": "provider-1", "test": True, "amount": {"value": "1.00", "currency": "RUB"}, "metadata": {"order_id": str(order.pk), "attempt": str(attempt.reference)}, "status": "succeeded"}
        with patch("requests.get", return_value=Mock(status_code=200, json=lambda: fake)):
            self.assertEqual(self.client.post(reverse("yoo_status", args=[order.access_token])).status_code, 503)
        order.refresh_from_db()
        self.assertEqual(order.payment_state, "pending")

    @override_settings(PRODUCTION=True, ALLOW_TEST_PAYMENTS=False,
                       YOOKASSA_SHOP_ID="test-shop", YOOKASSA_SECRET_KEY="test-secret")
    def test_public_server_does_not_offer_test_payments_by_default(self):
        from .integrations import yookassa_configured
        from .services import PaymentError
        from .yookassa import credentials

        self.assertFalse(yookassa_configured())
        self.assertContains(self.client.get(reverse("checkout")), "ЮKassa сейчас недоступна")
        with self.assertRaises(PaymentError):
            credentials()


class AnalyticsTests(TestCase):
    def setUp(self):
        category = Category.objects.create(name="Техника", slug="analytics-tech")
        self.phone = Product.objects.create(category=category, name="Телефон", slug="analytics-phone", description="Тест", price=Decimal("500.00"))
        self.laptop = Product.objects.create(category=category, name="Ноутбук", slug="analytics-laptop", description="Тест", price=Decimal("2000.00"))

    def make_order(self, product, *, total, quantity=1, status="new", payment_state="pending", is_demo=False):
        order = Order.objects.create(name="Тест", phone="+7 900 000-00-00", telegram_id=111 if not is_demo else -111,
            is_demo=is_demo, delivery=Order.Delivery.PICKUP, payment_method=Order.Payment.ONLINE,
            payment_gateway="demo", total=Decimal(total), status=status, payment_state=payment_state)
        item = OrderItem.objects.create(order=order, product=product, title=product.name, quantity=quantity,
            unit_price=Decimal(total) / quantity)
        return order, item

    def test_dashboard_excludes_demo_and_separates_orders_from_paid_amount(self):
        paid, paid_item = self.make_order(self.phone, total="1000.00", quantity=2, status="delivered", payment_state="paid")
        self.make_order(self.phone, total="3000.00", quantity=3, status="cancelled", payment_state="paid")
        self.make_order(self.laptop, total="2000.00")
        previous, _ = self.make_order(self.laptop, total="500.00", status="delivered", payment_state="paid")
        self.make_order(self.phone, total="999999.00", status="delivered", payment_state="paid", is_demo=True)
        first = timezone.localdate().replace(day=1)
        previous_date = month_at(first, -1)
        previous_time = timezone.make_aware(datetime.combine(previous_date, datetime_time(hour=12)), timezone.get_current_timezone())
        Order.objects.filter(pk=previous.pk).update(created_at=previous_time)
        Wish.objects.create(product=self.laptop, name="Тест", contact="Telegram ID 111", telegram_id=111)
        Favorite.objects.create(product=self.phone, telegram_id=111)
        Favorite.objects.create(product=self.laptop, browser_session="analytics-browser")
        ReturnRequest.objects.create(order_item=paid_item, quantity=1, reason="defect")

        data = dashboard_data(12)
        self.assertEqual(data["total_orders"], 4)
        self.assertEqual(data["placed_total"], "3\u00a0500 ₽")
        self.assertEqual(data["paid_total"], "1\u00a0500 ₽")
        self.assertEqual(data["average_paid"], "750 ₽")
        self.assertEqual(data["paid_count"], 2)
        self.assertEqual(data["monthly"][-1]["count"], 3)
        self.assertEqual(data["monthly"][-2]["count"], 1)
        self.assertEqual(data["pending_returns"], 1)
        self.assertEqual(data["favorites"], 2)
        self.assertEqual(data["top_wishes"][0]["product__name"], "Ноутбук")
        quantities = {row["product__name"]: row["units"] for row in data["top_products"]}
        self.assertEqual(quantities, {"Ноутбук": 2, "Телефон": 2})
        self.assertEqual(dashboard_data(24)["selected_months"], 24)
        self.assertEqual(dashboard_data(7)["selected_months"], 12)

    def test_admin_analytics_is_read_only_and_requires_order_view_permission(self):
        url = reverse("admin:store_shopanalytics_changelist")
        self.assertEqual(self.client.get(url).status_code, 302)
        admin_user = get_user_model().objects.create_superuser("analytics_admin", "admin@example.test", "test-password")
        self.client.force_login(admin_user)
        response = self.client.get(url, {"months": "6"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Чаще всего заказывают")
        self.assertContains(response, "Оплаты по месяцам")
        self.assertEqual(response.context["selected_months"], 6)
        self.assertContains(self.client.get(reverse("admin:index")), "Аналитика магазина")
        self.assertEqual(self.client.get(reverse("admin:store_shopanalytics_add")).status_code, 403)

        staff = get_user_model().objects.create_user("order_viewer", password="test-password", is_staff=True)
        staff.user_permissions.add(Permission.objects.get(codename="view_order", content_type__app_label="store", content_type__model="order"))
        self.client.force_login(staff)
        self.assertEqual(self.client.get(url).status_code, 200)
        other_staff = get_user_model().objects.create_user("product_viewer", password="test-password", is_staff=True)
        other_staff.user_permissions.add(Permission.objects.get(codename="view_product", content_type__app_label="store", content_type__model="product"))
        self.client.force_login(other_staff)
        self.assertEqual(self.client.get(url).status_code, 403)


@override_settings(BOT_TOKEN=TOKEN, BOT_USERNAME="test_shop_bot", BOT_APP_SHORT_NAME="shop")
class SupportTests(TestCase):
    def test_bot_support_conversation_is_private_persisted_and_idempotent(self):
        def incoming(update_id, chat_id, kind, text, message_id):
            return {"update_id": update_id, "message": {
                "message_id": message_id, "text": text,
                "from": {"id": chat_id}, "chat": {"id": chat_id, "type": kind},
            }}

        updates = {"ok": True, "result": [
            incoming(1, -100, "supergroup", "/support", 1),
            incoming(2, 123, "private", "/start support", 10),
            incoming(3, 123, "private", "Где мой заказ №7?", 11),
            incoming(4, 123, "private", "Где мой заказ №7?", 11),
            incoming(5, 123, "private", "/done", 12),
        ]}
        with patch("requests.get", side_effect=[Mock(json=lambda: updates), KeyboardInterrupt]), patch(
            "requests.post", return_value=Mock(status_code=200, json=lambda: {"ok": True})
        ) as send:
            call_command("runbot")
        ticket = SupportTicket.objects.get()
        self.assertEqual((ticket.telegram_id, ticket.status), (123, SupportTicket.Status.CLOSED))
        self.assertEqual(list(ticket.messages.values_list("text", flat=True)), ["Где мой заказ №7?"])
        self.assertTrue(Notification.objects.filter(kind="shop", status="internal", text__contains=f"№{ticket.pk}").exists())
        self.assertEqual(send.call_count, 3)
        self.assertEqual(send.call_args.kwargs["json"]["chat_id"], 123)
        self.assertNotEqual(open_ticket(123).pk, ticket.pk)

    def test_staff_reply_is_saved_and_queued_to_buyer(self):
        BotUser.objects.create(telegram_id=123, chat_id=123)
        ticket = open_ticket(123)
        self.assertEqual(receive_message(123, 41, "Нужна помощь"), ticket)
        self.assertIsNone(receive_message(123, 41, "Нужна помощь"))
        staff = get_user_model().objects.create_superuser("support_admin", "staff@example.test", "test-password")
        self.client.force_login(staff)
        url = reverse("admin:store_supportticket_change", args=[ticket.pk])
        self.assertContains(self.client.get(url), "Нужна помощь")
        response = self.client.post(url, {
            "status": "open", "reply_text": "Проверим заказ и напишем вам.",
            "messages-TOTAL_FORMS": "0", "messages-INITIAL_FORMS": "0",
            "messages-MIN_NUM_FORMS": "0", "messages-MAX_NUM_FORMS": "0",
        })
        self.assertEqual(response.status_code, 302)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, SupportTicket.Status.ANSWERED)
        self.assertEqual(ticket.messages.filter(sender=SupportMessage.Sender.STAFF).get().text, "Проверим заказ и напишем вам.")
        self.assertTrue(Notification.objects.filter(kind="buyer", telegram_id=123, text__contains="Проверим заказ").exists())
        with patch("requests.post", return_value=Mock(status_code=200, json=lambda: {"ok": True})) as send:
            flush_notifications()
        self.assertEqual(send.call_args.kwargs["json"]["chat_id"], 123)
        self.client.post(url, {
            "status": "closed", "reply_text": "",
            "messages-TOTAL_FORMS": "0", "messages-INITIAL_FORMS": "0",
            "messages-MIN_NUM_FORMS": "0", "messages-MAX_NUM_FORMS": "0",
        })
        self.assertTrue(Notification.objects.filter(kind="buyer", telegram_id=123, text__contains="закрыто").exists())

    def test_support_link_uses_configured_bot(self):
        self.assertContains(self.client.get(reverse("support")), "https://t.me/test_shop_bot?start=support")
        with override_settings(BOT_TOKEN=""):
            self.assertContains(self.client.get(reverse("support")), "после настройки бота")


@override_settings(BOT_TOKEN=TOKEN)
class CustomerChatTests(TestCase):
    def setUp(self):
        self.alice = CustomerProfile.objects.create(telegram_id=123, name="Алиса", chat_tag="alisa_01")
        self.bob = CustomerProfile.objects.create(telegram_id=456, name="Борис", chat_tag="boris_02")
        self.category = Category.objects.create(name="Телефоны", slug="phones")
        self.product = Product.objects.create(category=self.category, name="Смартфон", slug="phone", description="Описание", price=Decimal("1000"))

    def auth(self, user_id):
        return {"HTTP_X_TELEGRAM_INIT_DATA": telegram_data(user_id)}

    def post_json(self, url, user_id, data):
        return self.client.post(url, json.dumps(data), content_type="application/json", **self.auth(user_id))

    def start(self):
        response = self.post_json(reverse("api_chat_start"), 123, {"tag": "@boris_02"})
        self.assertEqual(response.status_code, 200)
        return response.json()["id"]

    def test_exact_tag_and_private_conversation(self):
        self.assertEqual(self.client.get(reverse("api_chats")).status_code, 401)
        self.assertEqual(self.client.get(reverse("api_chats"), HTTP_X_TELEGRAM_INIT_DATA="bad").status_code, 403)
        self.assertEqual(self.post_json(reverse("api_chat_start"), 123, {"tag": "boris"}).status_code, 404)
        self.assertEqual(self.post_json(reverse("api_chat_start"), 123, {"tag": "alisa_01"}).status_code, 400)
        chat_id = self.start()
        self.assertEqual(self.start(), chat_id)
        self.assertEqual(ChatConversation.objects.count(), 1)
        self.assertEqual(self.client.get(reverse("api_chat_detail", args=[chat_id]), **self.auth(789)).status_code, 404)
        self.assertEqual(self.post_json(reverse("api_chat_send", args=[chat_id]), 789, {"text": "Чужое сообщение"}).status_code, 404)
        self.assertEqual(self.client.get(reverse("api_chats"), **self.auth(456)).json()["conversations"][0]["peer"]["tag"], "alisa_01")
        block = reverse("api_chat_block", args=[chat_id])
        self.assertEqual(self.post_json(block, 456, {"blocked": True}).json()["blocked_by_me"], True)
        self.assertEqual(self.post_json(reverse("api_chat_send", args=[chat_id]), 123, {"text": "После блокировки"}).status_code, 403)
        self.assertTrue(self.client.get(reverse("api_chat_detail", args=[chat_id]), **self.auth(123)).json()["blocked_by_peer"])
        self.assertEqual(self.post_json(block, 456, {"blocked": False}).json()["blocked_by_me"], False)

    def test_tag_can_be_chosen_and_must_be_unique(self):
        url = reverse("api_chat_tag")
        self.assertEqual(self.post_json(url, 123, {"tag": "@Новый_Тег"}).json()["tag"], "новый_тег")
        self.assertEqual(self.post_json(url, 456, {"tag": "новый_тег"}).status_code, 409)
        self.assertEqual(self.post_json(url, 456, {"tag": "a"}).status_code, 400)
        self.alice.refresh_from_db()
        self.assertEqual(self.alice.chat_tag, "новый_тег")

    def test_product_with_caption_text_reply_unread_and_idempotency(self):
        chat_id = self.start()
        BotUser.objects.create(telegram_id=456, chat_id=456)
        send = reverse("api_chat_send", args=[chat_id])
        nonce = str(uuid.uuid4())
        response = self.post_json(send, 123, {"product_id": self.product.pk, "text": "Как тебе?", "nonce": nonce})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.post_json(send, 123, {"product_id": self.product.pk, "text": "Как тебе?", "nonce": nonce}).json()["duplicate"], True)
        self.assertEqual(self.post_json(send, 123, {"text": "Напиши, что думаешь"}).status_code, 201)
        self.assertEqual(ChatMessage.objects.count(), 2)
        self.assertEqual(Notification.objects.filter(kind="buyer", telegram_id=456).count(), 1)
        bob_list = self.client.get(reverse("api_chats"), **self.auth(456)).json()
        self.assertEqual(bob_list["conversations"][0]["unread"], 2)
        detail = self.client.get(reverse("api_chat_detail", args=[chat_id]), **self.auth(456)).json()
        self.assertEqual(detail["messages"][0]["product"]["name"], "Смартфон")
        self.assertEqual(detail["messages"][0]["text"], "Как тебе?")
        self.assertEqual(self.client.get(reverse("api_chats"), **self.auth(456)).json()["conversations"][0]["unread"], 0)
        self.assertEqual(self.post_json(send, 456, {"text": "Нравится!"}).status_code, 201)
        self.assertEqual(self.client.get(reverse("api_chat_detail", args=[chat_id]), **self.auth(123)).json()["messages"][-1]["text"], "Нравится!")
        self.product.is_visible = False
        self.product.save(update_fields=["is_visible"])
        hidden = self.client.get(reverse("api_chat_detail", args=[chat_id]), **self.auth(123)).json()["messages"][0]["product"]
        self.assertEqual(hidden["name"], "Смартфон")
        self.assertEqual(hidden["url"], "")

    def test_message_validation_and_history_pages(self):
        chat_id = self.start()
        send = reverse("api_chat_send", args=[chat_id])
        self.assertEqual(self.post_json(send, 123, {"text": " "}).status_code, 400)
        self.assertEqual(self.post_json(send, 123, {"text": "x" * 1001}).status_code, 400)
        self.product.is_visible = False
        self.product.save(update_fields=["is_visible"])
        self.assertEqual(self.post_json(send, 123, {"product_id": self.product.pk}).status_code, 404)
        self.product.is_visible = True
        self.product.save(update_fields=["is_visible"])
        chat = ChatConversation.objects.get(pk=chat_id)
        ChatMessage.objects.bulk_create([ChatMessage(conversation=chat, sender=self.alice, text=f"Сообщение {n}") for n in range(55)])
        first_page = self.client.get(reverse("api_chat_detail", args=[chat_id]), **self.auth(456)).json()
        self.assertEqual(len(first_page["messages"]), 50)
        self.assertTrue(first_page["has_more"])
        before = first_page["messages"][0]["id"]
        older = self.client.get(reverse("api_chat_detail", args=[chat_id]) + f"?before={before}", **self.auth(456)).json()
        self.assertEqual(len(older["messages"]), 5)
        self.assertFalse(older["has_more"])
        self.assertEqual(self.client.get(reverse("api_chat_detail", args=[chat_id]) + "?after=bad", **self.auth(456)).status_code, 400)

    def test_share_screen_and_browser_session(self):
        self.assertContains(self.client.get(reverse("product", args=[self.product.slug])), "Отправить в чат")
        self.assertContains(self.client.get(reverse("chat_share", args=[self.product.slug])), "Подпись к товару")
        session = self.client.session
        session["telegram_id"] = 123
        session["telegram_name"] = "Алиса"
        session.save()
        self.assertEqual(self.client.get(reverse("api_chats")).json()["tag"], "alisa_01")
