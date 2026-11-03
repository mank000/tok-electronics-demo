from django.contrib import admin
from django.db.models import Count
from .models import BotUser, Category, Notification, Order, OrderItem, PaymentAttempt, PaymentEvent, Product, ProductImage, Wish
from .services import queue_order_changed


admin.site.site_header = "ток. · Управление магазином"
admin.site.site_title = "ток."
admin.site.index_title = "Магазин электроники"


class ResponsiveAdmin(admin.ModelAdmin):
    class Media:
        css = {"all": ("store/admin.css",)}


@admin.register(Category)
class CategoryAdmin(ResponsiveAdmin):
    list_display = ("name", "slug", "order")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}


class ProductImageInline(admin.TabularInline):
    model = ProductImage
    extra = 1


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
