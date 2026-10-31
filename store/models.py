import io
import secrets
import uuid
from decimal import Decimal
from PIL import Image
from django.core.files.base import ContentFile
from django.db import models
from django.utils import timezone


def new_access_token():
    return secrets.token_urlsafe(32)


class Category(models.Model):
    name = models.CharField("Название", max_length=80)
    slug = models.SlugField("Адрес", unique=True)
    icon = models.CharField("Значок", max_length=8, default="◈")
    order = models.PositiveSmallIntegerField("Порядок", default=0)

    class Meta:
        ordering = ["order", "name"]
        verbose_name = "Раздел"
        verbose_name_plural = "Разделы"

    def __str__(self):
        return self.name


class Product(models.Model):
    class Availability(models.TextChoices):
        IN_STOCK = "stock", "В наличии"
        IN_TRANSIT = "transit", "В пути"
        PREORDER = "preorder", "Под заказ"

    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="products", verbose_name="Раздел")
    name = models.CharField("Название", max_length=160)
    slug = models.SlugField("Адрес", unique=True)
    description = models.TextField("Описание")
    specs = models.JSONField("Характеристики", default=dict, blank=True, help_text='Например: {"Экран": "6.1 дюйма"}')
    price = models.DecimalField("Цена, ₽", max_digits=10, decimal_places=2)
    availability = models.CharField("Наличие", max_length=12, choices=Availability.choices, default=Availability.IN_STOCK)
    is_visible = models.BooleanField("Показывать покупателям", default=True)
    is_featured = models.BooleanField("В подборке", default=False)
    created_at = models.DateTimeField("Добавлен", auto_now_add=True)

    class Meta:
        ordering = ["id"]
        verbose_name = "Товар"
        verbose_name_plural = "Товары"

    def __str__(self):
        return self.name

    @property
    def first_image(self):
        return self.images.first()


class ProductImage(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="images", verbose_name="Товар")
    image = models.ImageField("Изображение WebP", upload_to="products/")
    thumb = models.ImageField("Превью WebP", upload_to="products/thumbs/", blank=True)
    alt = models.CharField("Описание изображения", max_length=180, blank=True)
    order = models.PositiveSmallIntegerField("Порядок", default=0)

    class Meta:
        ordering = ["order", "id"]
        verbose_name = "Изображение товара"
        verbose_name_plural = "Изображения товара"

    def save(self, *args, **kwargs):
        if self.image and (not self.pk or "image" in (kwargs.get("update_fields") or [])):
            self.image.open()
            source = Image.open(self.image).convert("RGB")
            for field, size, quality in (("image", (1200, 1200), 82), ("thumb", (480, 480), 76)):
                photo = source.copy()
                photo.thumbnail(size, Image.Resampling.LANCZOS)
                out = io.BytesIO()
                photo.save(out, format="WEBP", quality=quality, method=5)
                getattr(self, field).save(f"{uuid.uuid4().hex}.webp", ContentFile(out.getvalue()), save=False)
        super().save(*args, **kwargs)


class BotUser(models.Model):
    telegram_id = models.BigIntegerField("Telegram ID", unique=True)
    chat_id = models.BigIntegerField("Чат с ботом")
    started_at = models.DateTimeField("Разрешил сообщения", auto_now_add=True)

    class Meta:
        verbose_name = "Пользователь бота"
        verbose_name_plural = "Пользователи бота"

    def __str__(self):
        return str(self.telegram_id)


class Order(models.Model):
    class Status(models.TextChoices):
        NEW = "new", "Новый"
        PAID = "paid", "Оплачен"
        PACKING = "packing", "Собирается"
        SHIPPED = "shipped", "Отправлен"
        DELIVERED = "delivered", "Выдан"
        CANCELLED = "cancelled", "Отменён"

    class Delivery(models.TextChoices):
        PICKUP = "pickup", "Самовывоз"
        CITY = "city", "Доставка по городу"
        REGION = "region", "Отправка в регионы"

    class Payment(models.TextChoices):
        ONLINE = "online", "Онлайн"
        ON_RECEIPT = "receipt", "При получении"

    access_token = models.CharField("Токен доступа", max_length=64, unique=True, default=new_access_token, editable=False)
    checkout_key = models.UUIDField("Ключ оформления", unique=True, default=uuid.uuid4, editable=False)
    telegram_id = models.BigIntegerField("Telegram ID", null=True, blank=True, db_index=True)
    name = models.CharField("Имя", max_length=100)
    phone = models.CharField("Телефон", max_length=32)
    delivery = models.CharField("Получение", max_length=12, choices=Delivery.choices)
    address = models.TextField("Адрес", blank=True)
    comment = models.TextField("Комментарий", blank=True)
    payment_method = models.CharField("Способ оплаты", max_length=12, choices=Payment.choices)
    total = models.DecimalField("Итог, ₽", max_digits=11, decimal_places=2)
    status = models.CharField("Статус", max_length=12, choices=Status.choices, default=Status.NEW)
    payment_state = models.CharField("Состояние оплаты", max_length=12, choices=[("pending", "Ожидается"), ("paid", "Успешно"), ("declined", "Отказ"), ("receipt", "При получении")], default="pending")
    track_number = models.CharField("Трек-номер", max_length=100, blank=True)
    created_at = models.DateTimeField("Создан", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлён", auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Заказ"
        verbose_name_plural = "Заказы"

    def __str__(self):
        return f"Заказ №{self.pk}"


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    title = models.CharField("Название при покупке", max_length=160)
    quantity = models.PositiveSmallIntegerField("Количество")
    unit_price = models.DecimalField("Цена при покупке", max_digits=10, decimal_places=2)

    class Meta:
        verbose_name = "Позиция заказа"
        verbose_name_plural = "Позиции заказа"

    @property
    def line_total(self):
        return self.unit_price * self.quantity


class Wish(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="wishes", verbose_name="Товар")
    name = models.CharField("Имя", max_length=100)
    contact = models.CharField("Телефон или Telegram", max_length=120)
    telegram_id = models.BigIntegerField("Telegram ID", null=True, blank=True)
    created_at = models.DateTimeField("Создана", auto_now_add=True)

    class Meta:
        verbose_name = "Заявка «Хочу такой»"
        verbose_name_plural = "Заявки «Хочу такой»"


class PaymentAttempt(models.Model):
    reference = models.UUIDField("Номер попытки", default=uuid.uuid4, unique=True, editable=False)
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="attempts")
    state = models.CharField("Состояние", max_length=12, choices=[("pending", "Ожидается"), ("paid", "Успешно"), ("declined", "Отказ")], default="pending")
    created_at = models.DateTimeField(auto_now_add=True)


class PaymentEvent(models.Model):
    event_id = models.CharField("ID события провайдера", max_length=100, unique=True)
    attempt = models.ForeignKey(PaymentAttempt, on_delete=models.PROTECT)
    status = models.CharField("Результат", max_length=12)
    received_at = models.DateTimeField(default=timezone.now)


class Notification(models.Model):
    kind = models.CharField("Тип", max_length=20, choices=[("buyer", "Покупателю"), ("shop", "Магазину")])
    telegram_id = models.BigIntegerField("Telegram ID", null=True, blank=True)
    text = models.TextField("Сообщение")
    status = models.CharField("Состояние", max_length=12, choices=[("pending", "Ожидает"), ("sent", "Отправлено"), ("failed", "Ошибка")], default="pending")
    attempts = models.PositiveSmallIntegerField("Попыток", default=0)
    last_error = models.TextField("Ошибка", blank=True)
    created_at = models.DateTimeField("Создано", auto_now_add=True)
    sent_at = models.DateTimeField("Отправлено", null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Уведомление"
        verbose_name_plural = "Уведомления"
