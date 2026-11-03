from django.contrib import admin
from django.db.models import Count
from django.utils.html import format_html
from .models import BotUser, Category, Notification, Order, OrderItem, PaymentAttempt, PaymentEvent, Product, ProductImage, ShopSettings, Wish
from .services import queue_order_changed


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


@admin.register(Product)
class ProductAdmin(ResponsiveAdmin):
    list_display = ("name", "category", "price", "availability", "is_visible", "is_featured", "wish_count")
    list_filter = ("category", "availability", "is_visible", "is_featured")
    list_editable = ("price", "availability", "is_visible")
    search_fields = ("name", "slug", "description")
    prepopulated_fields = {"slug": ("name",)}
    inlines = (ProductImageInline,)

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_wish_count=Count("wishes"))

    @admin.display(description="Заявок", ordering="_wish_count")
    def wish_count(self, obj):
        return obj._wish_count


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    readonly_fields = ("product", "title", "quantity", "unit_price")
    can_delete = False


@admin.register(Order)
class OrderAdmin(ResponsiveAdmin):
    list_display = ("id", "created_at", "name", "phone", "total", "delivery", "payment_state", "status", "track_number")
    list_filter = ("status", "payment_state", "delivery", "created_at")
    search_fields = ("=id", "name", "phone", "track_number")
    readonly_fields = ("access_token", "checkout_key", "telegram_id", "name", "phone", "delivery", "address", "comment", "payment_method", "total", "payment_state", "created_at", "updated_at")
    inlines = (OrderItemInline,)

    def save_model(self, request, obj, form, change):
        old = Order.objects.get(pk=obj.pk) if change else None
        super().save_model(request, obj, form, change)
        if old:
            queue_order_changed(obj, old.status, old.track_number)


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


@admin.register(BotUser)
class BotUserAdmin(ResponsiveAdmin):
    list_display = ("telegram_id", "chat_id", "started_at")
    search_fields = ("=telegram_id", "=chat_id")


@admin.register(PaymentAttempt)
class PaymentAttemptAdmin(ResponsiveAdmin):
    list_display = ("reference", "order", "state", "created_at")
    list_filter = ("state",)
    readonly_fields = ("reference", "order", "state", "created_at")


@admin.register(PaymentEvent)
class PaymentEventAdmin(ResponsiveAdmin):
    list_display = ("event_id", "attempt", "status", "received_at")
    list_filter = ("status",)
    readonly_fields = ("event_id", "attempt", "status", "received_at")
