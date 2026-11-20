from django.core.cache import cache
import re

from django import forms
from django.contrib import admin
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db.models import Count, F
from django.template.response import TemplateResponse
from django.utils.decorators import method_decorator
from django.utils.html import format_html
from django.utils import timezone
from django.views.decorators.debug import sensitive_post_parameters
from .analytics import dashboard_data
from .integrations import encrypt_secret
from .models import BotUser, Category, Coupon, CustomerProfile, Favorite, IntegrationSettings, Notification, Order, OrderItem, PaymentAttempt, PaymentEvent, Product, ProductCollection, ProductImage, ProductQuestion, ProductReview, ReturnRequest, ShopAnalytics, ShopSettings, SupportMessage, SupportTicket, Wish
from .refunds import confirm_refund, demo_refund, refund_amount
from .reservations import cancel_unpaid, has_open_provider_payment, issue_inventory, reserved_quantities
from .services import queue_buyer, queue_order_changed, queue_telegram
from .support import reply_to_ticket


admin.site.site_header = "Управление магазином"
admin.site.site_title = "Магазин"
admin.site.index_title = "Магазин электроники"


class ResponsiveAdmin(admin.ModelAdmin):
    class Media:
        css = {"all": ("store/admin.css",)}


@admin.register(ShopSettings)
class ShopSettingsAdmin(ResponsiveAdmin):
    fieldsets = (
        ("Витрина", {"fields": ("name", "subtitle", "logo", "logo_preview")}),
        ("Контакты", {"fields": ("contact_phone", "contact_email", "pickup_address")}),
        ("Реквизиты", {"fields": ("legal_name", "inn", "ogrn", "legal_address")}),
    )
    readonly_fields = ("logo_preview",)

    @admin.display(description="Текущий логотип")
    def logo_preview(self, obj):
        if obj and obj.logo:
            return format_html('<img src="{}" alt="" style="max-width:160px;max-height:100px;object-fit:contain">', obj.logo.url)
        return "Логотип не загружен"

    def has_add_permission(self, request):
        return super().has_add_permission(request) and not ShopSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


class IntegrationSettingsForm(forms.ModelForm):
    bot_token = forms.CharField(
        label="Токен бота",
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="Пустое поле сохраняет текущий токен; значение не показывается повторно.",
    )
    clear_bot_token = forms.BooleanField(label="Удалить значение из админки (вернуться к .env)", required=False)
    payment_webhook_secret = forms.CharField(
        label="Секрет учебной оплаты",
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="Отдельный случайный секрет длиной не менее 32 символов. Пустое поле сохраняет текущий.",
    )
    clear_payment_webhook_secret = forms.BooleanField(label="Удалить значение из админки (вернуться к .env)", required=False)
    telegram_login_client_secret = forms.CharField(
        label="Client Secret Telegram Login", required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="Пустое поле сохраняет текущий секрет; значение не показывается повторно.",
    )
    clear_telegram_login_client_secret = forms.BooleanField(label="Удалить значение из админки (вернуться к .env)", required=False)
    yookassa_secret_key = forms.CharField(
        label="Секретный ключ тестовой ЮKassa", required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="Только ключ тестового магазина. Пустое поле сохраняет текущий ключ; значение не показывается повторно.",
    )
    clear_yookassa_secret_key = forms.BooleanField(label="Удалить значение из админки (вернуться к .env)", required=False)

    class Meta:
        model = IntegrationSettings
        fields = ("bot_username", "bot_app_short_name", "public_base_url", "shop_chat_id", "telegram_login_client_id", "yookassa_shop_id")

    def clean_bot_token(self):
        value = self.cleaned_data["bot_token"].strip()
        if any(character.isspace() for character in value):
            raise forms.ValidationError("Токен не должен содержать пробелы.")
        return value

    def clean_bot_username(self):
        value = self.cleaned_data["bot_username"].strip().lstrip("@")
        if value and not re.fullmatch(r"[A-Za-z0-9_]{5,32}", value):
            raise forms.ValidationError("Укажите username бота без @ (5–32 символа).")
        return value

    def clean_bot_app_short_name(self):
        value = self.cleaned_data["bot_app_short_name"].strip()
        if value and not re.fullmatch(r"[A-Za-z0-9_]{1,64}", value):
            raise forms.ValidationError("Используйте латинские буквы, цифры и подчёркивание.")
        return value

    def clean_public_base_url(self):
        value = self.cleaned_data["public_base_url"].strip().rstrip("/")
        if value and not value.startswith("https://"):
            raise forms.ValidationError("Для Mini App нужен HTTPS-адрес.")
        return value

    def clean_shop_chat_id(self):
        value = self.cleaned_data["shop_chat_id"].strip()
        if value and not re.fullmatch(r"-?\d{1,20}", value):
            raise forms.ValidationError("Укажите числовой ID чата из команды /chatid.")
        return value

    def clean(self):
        data = super().clean()
        for value, clear in (
            ("bot_token", "clear_bot_token"),
            ("payment_webhook_secret", "clear_payment_webhook_secret"),
            ("telegram_login_client_secret", "clear_telegram_login_client_secret"),
            ("yookassa_secret_key", "clear_yookassa_secret_key"),
        ):
            if data.get(value) and data.get(clear):
                self.add_error(clear, "Нельзя одновременно заменить и удалить значение.")
        if data.get("payment_webhook_secret") and len(data["payment_webhook_secret"]) < 32:
            self.add_error("payment_webhook_secret", "Нужно не менее 32 символов.")
        for field in ("telegram_login_client_secret", "yookassa_secret_key"):
            if data.get(field) and any(character.isspace() for character in data[field]):
                self.add_error(field, "Секрет не должен содержать пробелы.")
        return data


@admin.register(IntegrationSettings)
class IntegrationSettingsAdmin(ResponsiveAdmin):
    form = IntegrationSettingsForm
    fieldsets = (
        ("Бот и Mini App", {"fields": ("bot_token_status", "bot_token", "clear_bot_token", "bot_username", "bot_app_short_name", "public_base_url", "shop_chat_id")}),
        ("Вход через Telegram Login", {"fields": ("telegram_login_client_id", "telegram_login_secret_status", "telegram_login_client_secret", "clear_telegram_login_client_secret")}),
        ("Учебная оплата", {"fields": ("payment_secret_status", "payment_webhook_secret", "clear_payment_webhook_secret")}),
        ("Тестовая ЮKassa", {"fields": ("yookassa_shop_id", "yookassa_secret_status", "yookassa_secret_key", "clear_yookassa_secret_key")}),
    )
    readonly_fields = ("bot_token_status", "payment_secret_status", "telegram_login_secret_status", "yookassa_secret_status")

    @method_decorator(sensitive_post_parameters("bot_token", "payment_webhook_secret", "telegram_login_client_secret", "yookassa_secret_key"))
    def add_view(self, request, form_url="", extra_context=None):
        return super().add_view(request, form_url, extra_context)

    @method_decorator(sensitive_post_parameters("bot_token", "payment_webhook_secret", "telegram_login_client_secret", "yookassa_secret_key"))
    def change_view(self, request, object_id, form_url="", extra_context=None):
        return super().change_view(request, object_id, form_url, extra_context)

    @admin.display(description="Токен бота")
    def bot_token_status(self, obj):
        if obj and obj.bot_token_ciphertext:
            return "Сохранён в админке"
        return "Задан в .env" if settings.BOT_TOKEN else "Не задан"

    @admin.display(description="Секрет оплаты")
    def payment_secret_status(self, obj):
        if obj and obj.payment_secret_ciphertext:
            return "Сохранён в админке"
        return "Задан в .env" if settings.PAYMENT_WEBHOOK_SECRET else "Не задан"

    @admin.display(description="Client Secret Telegram Login")
    def telegram_login_secret_status(self, obj):
        if obj and obj.telegram_login_secret_ciphertext:
            return "Сохранён в админке"
        return "Задан в .env" if settings.TELEGRAM_LOGIN_CLIENT_SECRET else "Не задан"

    @admin.display(description="Секретный ключ ЮKassa")
    def yookassa_secret_status(self, obj):
        if obj and obj.yookassa_secret_ciphertext:
            return "Сохранён в админке"
        return "Задан в .env" if settings.YOOKASSA_SECRET_KEY else "Не задан"

    def save_model(self, request, obj, form, change):
        for value, clear, field in (
            ("bot_token", "clear_bot_token", "bot_token_ciphertext"),
            ("payment_webhook_secret", "clear_payment_webhook_secret", "payment_secret_ciphertext"),
            ("telegram_login_client_secret", "clear_telegram_login_client_secret", "telegram_login_secret_ciphertext"),
            ("yookassa_secret_key", "clear_yookassa_secret_key", "yookassa_secret_ciphertext"),
        ):
            if form.cleaned_data[clear]:
                setattr(obj, field, "")
            elif form.cleaned_data[value]:
                setattr(obj, field, encrypt_secret(form.cleaned_data[value]))
        super().save_model(request, obj, form, change)

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser and not IntegrationSettings.objects.exists()

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Category)
class CategoryAdmin(ResponsiveAdmin):
    list_display = ("name", "slug", "order")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}


class ProductImageInline(admin.StackedInline):
    model = ProductImage
    extra = 1
    fields = ("preview", "image", "alt", "order")
    readonly_fields = ("preview",)

    @admin.display(description="Текущее фото")
    def preview(self, obj):
        if obj and obj.pk and obj.image:
            return format_html('<img src="{}" alt="" style="width:120px;height:120px;object-fit:contain">', (obj.thumb or obj.image).url)
        return "Фото не загружено"


class ProductStockForm(forms.ModelForm):
    stock_snapshot = forms.CharField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["stock_snapshot"].initial = str(self.instance.stock_quantity)

    def clean_stock_quantity(self):
        quantity = self.cleaned_data["stock_quantity"]
        if not self.instance.pk:
            return quantity
        current = Product.objects.select_for_update().get(pk=self.instance.pk)
        snapshot = self.cleaned_data.get("stock_snapshot")
        if snapshot == str(quantity) or self.add_prefix("stock_quantity") not in self.data:
            return current.stock_quantity
        if snapshot and snapshot != str(current.stock_quantity):
            raise forms.ValidationError("Остаток изменился. Обновите страницу и проверьте значение.")
        reserved = reserved_quantities([self.instance.pk]).get(self.instance.pk, 0)
        if reserved and (quantity is None or quantity < reserved):
            raise forms.ValidationError(f"В резерве {reserved} шт. Остаток не может быть меньше.")
        return quantity


@admin.register(Product)
class ProductAdmin(ResponsiveAdmin):
    form = ProductStockForm
    list_display = ("name", "category", "price", "discount_percent", "current_price", "stock_quantity", "availability", "is_visible", "is_featured", "wish_count")
    list_filter = ("category", "availability", "is_visible", "is_featured")
    list_editable = ("price", "availability", "is_visible")
    search_fields = ("name", "slug", "description")
    prepopulated_fields = {"slug": ("name",)}
    inlines = (ProductImageInline,)
    filter_horizontal = ("accessories",)
    fieldsets = ((None, {"fields": ("category", "name", "slug", "description", "specs", "price", "stock_snapshot", "stock_quantity", "availability", "is_visible", "is_featured")}), ("Скидка по расписанию", {"fields": ("discount_percent", "discount_starts_at", "discount_ends_at")}), ("Доставка и дополнения", {"fields": ("delivery_min_days", "delivery_max_days", "accessories")}))

    def save_model(self, request, obj, form, change):
        if change:
            current = Product.objects.select_for_update().get(pk=obj.pk)
            if "stock_quantity" not in form.fields:
                obj.stock_quantity = current.stock_quantity
        super().save_model(request, obj, form, change)

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_wish_count=Count("wishes"))

    @admin.display(description="Заявок", ordering="_wish_count")
    def wish_count(self, obj):
        return obj._wish_count


@admin.register(Coupon)
class CouponAdmin(ResponsiveAdmin):
    list_display = ("code", "kind", "value", "min_total", "starts_at", "ends_at", "uses", "max_uses", "is_active")
    list_filter = ("kind", "is_active")
    search_fields = ("code",)
    readonly_fields = ("uses",)
    fieldsets = ((None, {"fields": ("code", "kind", "value", "min_total", "is_active")}), ("Срок и лимиты", {"fields": ("starts_at", "ends_at", "max_uses", "uses")}))


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    readonly_fields = ("product", "title", "quantity", "list_unit_price", "unit_price", "inventory_state")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class OrderFulfillmentForm(forms.ModelForm):
    def clean_status(self):
        status = self.cleaned_data["status"]
        order = Order.objects.select_for_update().get(pk=self.instance.pk)
        if status == order.status:
            return status
        transitions = {
            Order.Status.NEW: (Order.Status.PACKING, Order.Status.CANCELLED),
            Order.Status.PAID: (Order.Status.PACKING,),
            Order.Status.PACKING: (Order.Status.SHIPPED, Order.Status.DELIVERED, Order.Status.CANCELLED),
            Order.Status.SHIPPED: (Order.Status.DELIVERED,),
        }
        if status not in transitions.get(order.status, ()):
            raise forms.ValidationError("Этот переход для текущего статуса недоступен.")
        if status == Order.Status.CANCELLED:
            if order.payment_state == "paid" or has_open_provider_payment(order):
                raise forms.ValidationError("Сначала подтвердите итог внешнего платежа или оформите возврат.")
        elif order.payment_method == Order.Payment.ONLINE and order.payment_state != "paid":
            raise forms.ValidationError("Сначала подтвердите оплату.")
        if (status == Order.Status.PACKING and order.reservation_expires_at
                and order.reservation_expires_at <= timezone.now()):
            raise forms.ValidationError("Резерв истёк. Попросите покупателя оформить новый заказ.")
        return status


@admin.register(Order)
class OrderAdmin(ResponsiveAdmin):
    form = OrderFulfillmentForm
    list_display = ("id", "is_demo", "created_at", "name", "phone", "coupon_code", "total", "delivery", "payment_state", "status", "track_number")
    list_filter = ("is_demo", "status", "payment_state", "delivery", "created_at")
    search_fields = ("=id", "name", "phone", "track_number")
    readonly_fields = ("access_token", "checkout_key", "telegram_id", "name", "phone", "delivery", "address", "comment", "payment_method", "payment_gateway", "subtotal", "product_discount", "coupon_discount", "coupon_code", "coupon", "total", "payment_state", "consent_at", "reservation_expires_at", "created_at", "updated_at")
    inlines = (OrderItemInline,)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        order = Order.objects.select_for_update().get(pk=obj.pk)
        old_status, old_track = order.status, order.track_number
        if "status" in form.changed_data:
            order.status = obj.status
            if order.status == Order.Status.CANCELLED:
                cancel_unpaid(order)
            elif order.status == Order.Status.DELIVERED:
                issue_inventory(order)
            elif order.status == Order.Status.PACKING:
                order.reservation_expires_at = None
        if "track_number" in form.changed_data:
            order.track_number = obj.track_number
        order.save(update_fields=["status", "payment_state", "reservation_expires_at",
                                  "coupon_reserved", "track_number", "updated_at"])
        queue_order_changed(order, old_status, old_track)


@admin.register(ShopAnalytics)
class ShopAnalyticsAdmin(ResponsiveAdmin):
    change_list_template = "admin/store/analytics.html"

    def has_module_permission(self, request):
        return self.has_view_permission(request)

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser or request.user.has_perm("store.view_order")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return super().get_queryset(request).none()

    def changelist_view(self, request, extra_context=None):
        if not self.has_view_permission(request):
            raise PermissionDenied
        try:
            months = int(request.GET.get("months", 12))
        except (TypeError, ValueError):
            months = 12
        cache_key = f"admin-analytics:{months}"
        metrics = cache.get(cache_key)
        if metrics is None:
            metrics = dashboard_data(months)
            cache.set(cache_key, metrics, 30)
        request.current_app = self.admin_site.name
        context = {
            **self.admin_site.each_context(request),
            "title": "Аналитика магазина",
            "opts": self.model._meta,
            "app_label": self.model._meta.app_label,
            **metrics,
            **(extra_context or {}),
        }
        return TemplateResponse(request, self.change_list_template, context)


@admin.register(Wish)
class WishAdmin(ResponsiveAdmin):
    list_display = ("id", "created_at", "product", "name", "contact")
    list_filter = ("created_at", "product__category")
    search_fields = ("product__name", "name", "contact")
    readonly_fields = ("product", "name", "contact", "telegram_id", "created_at")


@admin.action(description="Повторить отправку выбранных уведомлений")
def retry_notifications(modeladmin, request, queryset):
    queryset.exclude(status="sent").update(status="pending", attempts=0, last_error="")


@admin.register(Notification)
class NotificationAdmin(ResponsiveAdmin):
    list_display = ("id", "created_at", "kind", "status", "attempts", "last_error")
    list_filter = ("status", "kind")
    search_fields = ("text", "last_error", "=telegram_id")
    readonly_fields = ("kind", "telegram_id", "text", "status", "attempts", "last_error", "created_at", "sent_at")
    actions = (retry_notifications,)


class SupportTicketForm(forms.ModelForm):
    reply_text = forms.CharField(
        label="Ответ покупателю", required=False, max_length=2000,
        widget=forms.Textarea(attrs={"rows": 5}),
        help_text="Ответ сохранится в переписке и уйдёт покупателю в личный чат с ботом.",
    )

    class Meta:
        model = SupportTicket
        fields = ("status",)


class SupportMessageInline(admin.TabularInline):
    model = SupportMessage
    extra = 0
    fields = ("created_at", "sender", "text")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SupportTicket)
class SupportTicketAdmin(ResponsiveAdmin):
    form = SupportTicketForm
    list_display = ("id", "telegram_id", "status", "updated_at", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("=telegram_id", "messages__text")
    readonly_fields = ("telegram_id", "created_at", "updated_at")
    fields = ("telegram_id", "status", "created_at", "updated_at", "reply_text")
    inlines = (SupportMessageInline,)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        reply = form.cleaned_data["reply_text"].strip()
        was_closed = SupportTicket.objects.filter(pk=obj.pk, status=SupportTicket.Status.CLOSED).exists()
        super().save_model(request, obj, form, change)
        if reply:
            reply_to_ticket(obj, reply)
        elif obj.status == SupportTicket.Status.CLOSED and not was_closed:
            queue_telegram(obj.telegram_id, f"Обращение поддержки №{obj.pk} закрыто. Если нужна помощь, отправьте /support.")


@admin.register(BotUser)
class BotUserAdmin(ResponsiveAdmin):
    list_display = ("telegram_id", "chat_id", "started_at")
    search_fields = ("=telegram_id", "=chat_id")


@admin.register(CustomerProfile)
class CustomerProfileAdmin(ResponsiveAdmin):
    list_display = ("name", "chat_tag", "telegram_id", "registered_at")
    search_fields = ("name", "chat_tag", "=telegram_id")
    readonly_fields = ("telegram_id", "name", "chat_tag", "registered_at")

    def has_add_permission(self, request):
        return False


@admin.register(PaymentAttempt)
class PaymentAttemptAdmin(ResponsiveAdmin):
    list_display = ("reference", "order", "gateway", "state", "created_at")
    list_filter = ("state",)
    readonly_fields = ("reference", "order", "gateway", "provider_id", "confirmation_token", "state", "provider_started_at", "provider_next_check_at", "created_at")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PaymentEvent)
class PaymentEventAdmin(ResponsiveAdmin):
    list_display = ("event_id", "attempt", "status", "received_at")
    list_filter = ("status",)
    readonly_fields = ("event_id", "attempt", "status", "received_at")


@admin.register(ProductReview)
class ProductReviewAdmin(ResponsiveAdmin):
    list_display = ("product", "author", "rating", "is_demo", "order_item", "is_published", "created_at")
    list_filter = ("is_demo", "is_published", "rating")
    search_fields = ("product__name", "author", "text")
    readonly_fields = ("product", "author", "telegram_id", "is_demo", "order_item", "rating", "text", "created_at")
    actions = ("publish_reviews", "hide_reviews")

    @admin.action(description="Опубликовать проверенные отзывы")
    def publish_reviews(self, request, queryset):
        queryset.filter(telegram_id__isnull=False, order_item__order__status=Order.Status.DELIVERED,
            order_item__order__payment_state__in=("paid", "receipt"),
            order_item__order__telegram_id=F("telegram_id"), order_item__order__is_demo=F("is_demo"), order_item__product_id=F("product_id")).update(is_published=True)

    @admin.action(description="Скрыть выбранные отзывы")
    def hide_reviews(self, request, queryset):
        queryset.update(is_published=False)


@admin.register(ProductQuestion)
class ProductQuestionAdmin(ResponsiveAdmin):
    list_display = ("product", "author", "is_demo", "is_published", "created_at")
    list_filter = ("is_demo", "is_published")
    list_editable = ("is_published",)
    search_fields = ("product__name", "author", "text")


class ReturnSettlementForm(forms.ModelForm):
    def clean(self):
        data = super().clean()
        order_id = self.instance.order_item.order_id
        order = Order.objects.select_for_update().get(pk=order_id)
        current = ReturnRequest.objects.select_for_update().get(pk=self.instance.pk)
        status = data.get("status")
        if current.refunded_at or current.status == "closed":
            if self.has_changed():
                raise forms.ValidationError("Подтверждённый возврат уже зафиксирован.")
            return data
        if status == "closed" and current.status != "closed":
            if current.status != "approved":
                raise forms.ValidationError("Сначала одобрите заявку.")
            if refund_amount(current, order) is None:
                raise forms.ValidationError("Сумма возврата доступна после подтверждения оплаты.")
            if not data.get("refund_reference") and not demo_refund(order):
                raise forms.ValidationError("Укажите номер чека или документа после фактического возврата денег.")
        return data


@admin.register(ReturnRequest)
class ReturnRequestAdmin(ResponsiveAdmin):
    form = ReturnSettlementForm
    list_display = ("id", "order_item", "quantity", "reason", "status", "refund_amount", "refunded_at", "created_at")
    list_filter = ("status", "reason", "created_at")
    readonly_fields = ("order_item", "quantity", "reason", "comment", "expected_refund", "refund_amount", "refunded_at", "created_at")

    @admin.display(description="Ожидаемая сумма возврата, ₽")
    def expected_refund(self, obj):
        return refund_amount(obj, obj.order_item.order)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        order = Order.objects.select_for_update().get(pk=obj.order_item.order_id)
        current = ReturnRequest.objects.select_for_update().get(pk=obj.pk)
        if current.refunded_at or current.status == "closed":
            return
        if obj.status == "closed" and current.status == "approved":
            confirm_refund(obj, order)
        super().save_model(request, obj, form, change)
        if current.status != obj.status:
            queue_buyer(order, f"Заявка на возврат №{obj.pk}: статус «{obj.get_status_display()}».")


@admin.register(ProductCollection)
class ProductCollectionAdmin(ResponsiveAdmin):
    list_display = ("title", "is_visible", "order")
    list_editable = ("is_visible", "order")
    filter_horizontal = ("products",)


@admin.register(Favorite)
class FavoriteAdmin(ResponsiveAdmin):
    list_display = ("product", "telegram_id", "browser_session", "created_at")
    readonly_fields = ("product", "telegram_id", "browser_session", "created_at")
