import hashlib
import hmac
import io
import json
import tempfile
import time
import uuid
from decimal import Decimal
from unittest.mock import Mock, patch
from urllib.parse import urlencode
from PIL import Image
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from .integrations import encrypt_secret, get_runtime_config
from .models import BotUser, Category, IntegrationSettings, Notification, Order, PaymentAttempt, PaymentEvent, Product, ProductImage, ShopSettings
from .security import TelegramAuthError, verify_init_data
from .services import flush_notifications, process_payment_event, queue_order_changed, queue_shop, retry_buyer_notifications, signed_event


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
    def test_shop_notification_waits_for_chat_configuration(self):
        queue_shop("Проверка доставки")
        with patch("requests.post") as send:
            flush_notifications()
        notification = Notification.objects.get(kind="shop")
        self.assertEqual(notification.status, "pending")
        send.assert_not_called()
        with override_settings(SHOP_CHAT_ID="-100123"), patch("requests.post", return_value=Mock(status_code=200, json=lambda: {"ok": True})):
            flush_notifications()
        notification.refresh_from_db()
        self.assertEqual(notification.status, "sent")

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

    def test_hidden_product_not_visible_or_orderable(self):
        self.product.is_visible = False
        self.product.save()
        self.assertEqual(self.client.get(reverse("product", args=[self.product.slug])).status_code, 404)
        self.assertEqual(self.checkout().status_code, 400)

    def test_preorder_wish_is_saved_and_not_added_to_checkout(self):
        self.product.availability = "preorder"
        self.product.save()
        self.assertEqual(self.checkout().status_code, 400)
        response = self.client.post(reverse("wish", args=[self.product.slug]), {"name": "Тест", "contact": "+7 900 000 00 00"})
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
                "description": "Обновлённое описание", "specs": "{}", "price": "1500.00", "availability": "stock",
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

    def test_csrf_rejects_null_origin_and_accepts_local_origin(self):
        client = Client(enforce_csrf_checks=True)
        client.get(reverse("checkout"), HTTP_HOST="localhost:8008")
        csrf_token = client.cookies["csrftoken"].value
        data = {"name": "Покупатель", "phone": "+7 900 123 45 67", "delivery": "pickup", "address": "", "comment": "", "payment_method": "online", "consent": "on", "cart": json.dumps({str(self.product.pk): 1}), "checkout_key": str(uuid.uuid4()), "csrfmiddlewaretoken": csrf_token}
        self.assertEqual(client.post(reverse("checkout"), data, HTTP_HOST="localhost:8008", HTTP_ORIGIN="null").status_code, 403)
        self.assertEqual(client.post(reverse("checkout"), data, HTTP_HOST="localhost:8008", HTTP_ORIGIN="http://localhost:8008").status_code, 302)

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
            call_command("seed_catalog", verbosity=0)
            self.assertEqual(Product.objects.count(), 24)
            self.assertEqual(ProductImage.objects.count(), 26)
            self.assertTrue(all(product.images.exists() for product in Product.objects.all()))
            image = ProductImage.objects.first()
            with Image.open(image.image.path) as source, Image.open(image.thumb.path) as thumb:
                self.assertEqual(source.format, "WEBP")
                self.assertEqual(thumb.format, "WEBP")
                self.assertLessEqual(max(thumb.size), 480)
