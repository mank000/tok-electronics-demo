import secrets
import re
import uuid
from decimal import Decimal, ROUND_HALF_UP
from django.core.cache import cache
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Lower
from django.utils import timezone
from .images import open_raster, webp_content


def new_access_token():
    return secrets.token_urlsafe(32)


class ShopSettings(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    name = models.CharField("Название магазина", max_length=100, default="ток.")
    subtitle = models.CharField("Подпись магазина", max_length=160, default="Учебный магазин электроники")
    logo = models.ImageField("Логотип WebP", upload_to="shop/", blank=True)
    contact_phone = models.CharField("Телефон магазина", max_length=32, blank=True)
    contact_email = models.EmailField("Электронная почта", blank=True)
    pickup_address = models.CharField("Адрес самовывоза", max_length=255, blank=True)
    legal_name = models.CharField("Наименование организации или ИП", max_length=255, blank=True)
    inn = models.CharField("ИНН", max_length=12, blank=True, validators=[RegexValidator(r"^(?:\d{10}|\d{12})$", "ИНН должен состоять из 10 или 12 цифр")])
    ogrn = models.CharField("ОГРН или ОГРНИП", max_length=15, blank=True, validators=[RegexValidator(r"^(?:\d{13}|\d{15})$", "ОГРН или ОГРНИП должен состоять из 13 или 15 цифр")])
    legal_address = models.CharField("Юридический адрес", max_length=255, blank=True)

    class Meta:
        verbose_name = "Настройки магазина"
        verbose_name_plural = "Настройки магазина"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.pk = 1
        if self.logo and not self.logo._committed:
            image = open_raster(self.logo)
            self.logo.save(f"{uuid.uuid4().hex}.webp", webp_content(image, 640), save=False)
        super().save(*args, **kwargs)


class IntegrationSettings(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    bot_token_ciphertext = models.TextField(blank=True, editable=False)
    payment_secret_ciphertext = models.TextField(blank=True, editable=False)
    telegram_login_client_id = models.CharField("Client ID Telegram Login", max_length=100, blank=True)
    telegram_login_secret_ciphertext = models.TextField(blank=True, editable=False)
    yookassa_shop_id = models.CharField("ID тестового магазина ЮKassa", max_length=100, blank=True)
    yookassa_secret_ciphertext = models.TextField(blank=True, editable=False)
    bot_username = models.CharField("Username бота", max_length=32, blank=True, help_text="Без @")
    bot_app_short_name = models.CharField("Короткое имя Mini App", max_length=64, blank=True)
    public_base_url = models.URLField("Публичный HTTPS-адрес", blank=True)
    shop_chat_id = models.CharField("ID чата магазина", max_length=32, blank=True)

    class Meta:
        verbose_name = "Подключение Telegram и оплаты"
        verbose_name_plural = "Подключение Telegram и оплаты"

    def __str__(self):
        return "Подключение Telegram и оплаты"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)


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
    price = models.DecimalField("Цена, ₽", max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    discount_percent = models.PositiveSmallIntegerField("Скидка, %", default=0, validators=[MaxValueValidator(90)])
    discount_starts_at = models.DateTimeField("Скидка с", null=True, blank=True)
    discount_ends_at = models.DateTimeField("Скидка до", null=True, blank=True)
    availability = models.CharField("Наличие", max_length=12, choices=Availability.choices, default=Availability.IN_STOCK)
    is_visible = models.BooleanField("Показывать покупателям", default=True)
    is_featured = models.BooleanField("В подборке", default=False)
    delivery_min_days = models.PositiveSmallIntegerField("Доставка от, дней", default=2, blank=True)
    delivery_max_days = models.PositiveSmallIntegerField("Доставка до, дней", default=5, blank=True)
    accessories = models.ManyToManyField("self", blank=True, symmetrical=False, related_name="paired_with", verbose_name="Покупают вместе")
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

    def clean(self):
        super().clean()
        if self.discount_starts_at and self.discount_ends_at and self.discount_ends_at <= self.discount_starts_at:
            raise ValidationError({"discount_ends_at": "Окончание скидки должно быть позже начала."})

    def has_discount_at(self, now):
        return bool(self.discount_percent and (not self.discount_starts_at or self.discount_starts_at <= now) and (not self.discount_ends_at or now < self.discount_ends_at))

    @property
    def has_discount(self):
        return self.has_discount_at(timezone.now())

    def price_at(self, now):
        if not self.has_discount_at(now):
            return self.price
        return (self.price * (Decimal(100 - self.discount_percent) / 100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @property
    def current_price(self):
        return self.price_at(timezone.now())


class Coupon(models.Model):
    class Kind(models.TextChoices):
        PERCENT = "percent", "Процент"
        FIXED = "fixed", "Сумма в рублях"

    code = models.CharField("Промокод", max_length=32)
    kind = models.CharField("Тип скидки", max_length=8, choices=Kind.choices)
    value = models.DecimalField("Размер скидки", max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    min_total = models.DecimalField("Минимальная сумма товаров, ₽", max_digits=11, decimal_places=2, default=0, validators=[MinValueValidator(0)])
    starts_at = models.DateTimeField("Действует с", null=True, blank=True)
    ends_at = models.DateTimeField("Действует до", null=True, blank=True)
    max_uses = models.PositiveIntegerField("Максимум применений (0 — без лимита)", default=0)
    uses = models.PositiveIntegerField("Применений", default=0, editable=False)
    is_active = models.BooleanField("Включён", default=True)

    class Meta:
        constraints = [models.UniqueConstraint(Lower("code"), name="unique_coupon_code_ci")]
        verbose_name = "Промокод"
        verbose_name_plural = "Промокоды"

    def __str__(self):
        return self.code

    def clean(self):
        super().clean()
        self.code = self.code.strip().upper()
        if not self.code or not re.fullmatch(r"[A-Z0-9_-]{3,32}", self.code):
            raise ValidationError({"code": "От 3 до 32 латинских букв, цифр, _ или -."})
        if Coupon.objects.filter(code__iexact=self.code).exclude(pk=self.pk).exists():
            raise ValidationError({"code": "Такой промокод уже существует."})
        if self.kind == self.Kind.PERCENT and self.value > 90:
            raise ValidationError({"value": "Процент скидки не может быть больше 90."})
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValidationError({"ends_at": "Окончание должно быть позже начала."})

    def save(self, *args, **kwargs):
        self.code = self.code.strip().upper()
        super().save(*args, **kwargs)


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
        if self.image and not self.image._committed:
            image = open_raster(self.image)
            for field, size, quality in (("image", 1200, 82), ("thumb", 480, 76)):
                getattr(self, field).save(f"{uuid.uuid4().hex}.webp", webp_content(image, size, quality), save=False)
        super().save(*args, **kwargs)


class BotUser(models.Model):
    telegram_id = models.BigIntegerField("Telegram ID", unique=True)
    chat_id = models.BigIntegerField("Чат с ботом")
    phone = models.CharField("Телефон из разрешённого контакта", max_length=32, blank=True)
    phone_digits = models.CharField("Номер для поиска", max_length=15, blank=True, db_index=True)
    started_at = models.DateTimeField("Разрешил сообщения", auto_now_add=True)

    class Meta:
        verbose_name = "Пользователь бота"
        verbose_name_plural = "Пользователи бота"

    def __str__(self):
        return str(self.telegram_id)


class SupportTicket(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Открыто"
        ANSWERED = "answered", "Ответили"
        CLOSED = "closed", "Закрыто"

    telegram_id = models.BigIntegerField("Telegram ID покупателя", db_index=True)
    status = models.CharField("Состояние", max_length=12, choices=Status.choices, default=Status.OPEN)
    created_at = models.DateTimeField("Создано", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлено", auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "Обращение в поддержку"
        verbose_name_plural = "Поддержка"

    def __str__(self):
        return f"Обращение №{self.pk}"


class SupportMessage(models.Model):
    class Sender(models.TextChoices):
        BUYER = "buyer", "Покупатель"
        STAFF = "staff", "Сотрудник"

    ticket = models.ForeignKey(SupportTicket, on_delete=models.CASCADE, related_name="messages")
    sender = models.CharField("Отправитель", max_length=8, choices=Sender.choices)
    text = models.TextField("Сообщение", max_length=2000)
    telegram_message_id = models.BigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField("Создано", auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [models.UniqueConstraint(fields=["ticket", "telegram_message_id"], name="unique_support_telegram_message")]
        verbose_name = "Сообщение поддержки"
        verbose_name_plural = "Сообщения поддержки"


class CustomerProfile(models.Model):
    telegram_id = models.BigIntegerField("Telegram ID", unique=True)
    name = models.CharField("Имя покупателя", max_length=80)
    chat_tag = models.CharField("Тег в чате", max_length=24, unique=True, null=True, blank=True)
    registered_at = models.DateTimeField("Зарегистрирован", auto_now_add=True)

    class Meta:
        verbose_name = "Покупатель Telegram"
        verbose_name_plural = "Покупатели Telegram"

    def __str__(self):
        return self.name


class ChatConversation(models.Model):
    left = models.ForeignKey(CustomerProfile, on_delete=models.CASCADE, related_name="chats_as_left")
    right = models.ForeignKey(CustomerProfile, on_delete=models.CASCADE, related_name="chats_as_right")
    left_blocked = models.BooleanField(default=False)
    right_blocked = models.BooleanField(default=False)
    left_read_message_id = models.PositiveBigIntegerField(default=0)
    right_read_message_id = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [models.Index(fields=["left", "-updated_at"], name="chat_left_updated"), models.Index(fields=["right", "-updated_at"], name="chat_right_updated")]
        constraints = [
            models.UniqueConstraint(fields=["left", "right"], name="unique_chat_pair"),
            models.CheckConstraint(condition=Q(left_id__lt=F("right_id")), name="ordered_chat_pair"),
        ]
        verbose_name = "Переписка покупателей"
        verbose_name_plural = "Переписки покупателей"


class ChatMessage(models.Model):
    conversation = models.ForeignKey(ChatConversation, on_delete=models.CASCADE, related_name="messages")
    sender = models.ForeignKey(CustomerProfile, on_delete=models.CASCADE, related_name="sent_chat_messages")
    text = models.TextField("Текст или подпись", max_length=1000, blank=True)
    product = models.ForeignKey(Product, on_delete=models.SET_NULL, null=True, blank=True)
    product_name = models.CharField("Название товара при отправке", max_length=160, blank=True)
    client_nonce = models.UUIDField(null=True, blank=True, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        indexes = [models.Index(fields=["conversation", "id"], name="chat_conversation_id"), models.Index(fields=["sender", "created_at"], name="chat_sender_created")]
        constraints = [models.CheckConstraint(condition=~Q(text="") | ~Q(product_name=""), name="chat_message_has_content")]
        verbose_name = "Сообщение покупателей"
        verbose_name_plural = "Сообщения покупателей"


class BrowserLoginCode(models.Model):
    telegram_id = models.BigIntegerField("Telegram ID", unique=True)
    code_hash = models.CharField(max_length=64)
    sent_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    window_started_at = models.DateTimeField()
    sends_in_window = models.PositiveSmallIntegerField(default=0)


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
    is_demo = models.BooleanField("Демонстрационный заказ", default=False)
    name = models.CharField("Имя", max_length=100)
    phone = models.CharField("Телефон", max_length=32)
    delivery = models.CharField("Получение", max_length=12, choices=Delivery.choices)
    address = models.TextField("Адрес", blank=True)
    comment = models.TextField("Комментарий", blank=True)
    payment_method = models.CharField("Способ оплаты", max_length=12, choices=Payment.choices)
    payment_gateway = models.CharField("Платёжный шлюз", max_length=12, choices=[("demo", "Демооплата"), ("yookassa", "ЮKassa"), ("receipt", "При получении")], default="demo")
    subtotal = models.DecimalField("Стоимость без скидок, ₽", max_digits=11, decimal_places=2, default=0)
    product_discount = models.DecimalField("Скидка на товары, ₽", max_digits=11, decimal_places=2, default=0)
    coupon_discount = models.DecimalField("Скидка по промокоду, ₽", max_digits=11, decimal_places=2, default=0)
    coupon_code = models.CharField("Применённый промокод", max_length=32, blank=True)
    coupon = models.ForeignKey(Coupon, on_delete=models.PROTECT, null=True, blank=True, related_name="orders")
    total = models.DecimalField("Итог, ₽", max_digits=11, decimal_places=2)
    status = models.CharField("Статус", max_length=12, choices=Status.choices, default=Status.NEW)
    payment_state = models.CharField("Состояние оплаты", max_length=12, choices=[("pending", "Ожидается"), ("paid", "Успешно"), ("declined", "Отказ"), ("receipt", "При получении")], default="pending")
    consent_at = models.DateTimeField("Согласие получено", default=timezone.now)
    track_number = models.CharField("Трек-номер", max_length=100, blank=True)
    created_at = models.DateTimeField("Создан", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлён", auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["is_demo", "created_at"], name="order_demo_created"), models.Index(fields=["telegram_id", "-created_at"], name="order_customer_created")]
        verbose_name = "Заказ"
        verbose_name_plural = "Заказы"

    def __str__(self):
        return f"Заказ №{self.pk}"


class ShopAnalytics(Order):
    class Meta:
        proxy = True
        verbose_name = "Аналитика магазина"
        verbose_name_plural = "Аналитика магазина"


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    title = models.CharField("Название при покупке", max_length=160)
    quantity = models.PositiveSmallIntegerField("Количество")
    unit_price = models.DecimalField("Цена при покупке", max_digits=10, decimal_places=2)
    list_unit_price = models.DecimalField("Цена до скидки", max_digits=10, decimal_places=2, default=0)

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
    telegram_id = models.BigIntegerField("Telegram ID", null=True, blank=True, db_index=True)
    browser_session = models.CharField("Сессия браузера", max_length=40, blank=True, db_index=True)
    created_at = models.DateTimeField("Создана", auto_now_add=True)

    class Meta:
        verbose_name = "Заявка «Хочу такой»"
        verbose_name_plural = "Заявки «Хочу такой»"


class PaymentAttempt(models.Model):
    reference = models.UUIDField("Номер попытки", default=uuid.uuid4, unique=True, editable=False)
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="attempts")
    state = models.CharField("Состояние", max_length=12, choices=[("pending", "Ожидается"), ("paid", "Успешно"), ("declined", "Отказ")], default="pending")
    gateway = models.CharField("Шлюз", max_length=12, default="demo")
    provider_id = models.CharField("ID платежа ЮKassa", max_length=80, blank=True, db_index=True)
    confirmation_token = models.CharField("Токен виджета", max_length=255, blank=True)
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
    status = models.CharField("Состояние", max_length=12, choices=[("pending", "Ожидает"), ("sent", "Отправлено"), ("failed", "Ошибка"), ("internal", "Только в админке")], default="pending")
    attempts = models.PositiveSmallIntegerField("Попыток", default=0)
    last_error = models.TextField("Ошибка", blank=True)
    created_at = models.DateTimeField("Создано", auto_now_add=True)
    sent_at = models.DateTimeField("Отправлено", null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "created_at"], name="notification_pending")]
        verbose_name = "Уведомление"
        verbose_name_plural = "Уведомления"


class Favorite(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="favorites")
    telegram_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    browser_session = models.CharField(max_length=40, blank=True, null=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["product", "telegram_id"], name="unique_telegram_favorite"),
            models.UniqueConstraint(fields=["product", "browser_session"], name="unique_browser_favorite"),
        ]
        verbose_name = "Избранное"
        verbose_name_plural = "Избранное"


class ProductReview(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="reviews")
    author = models.CharField("Имя", max_length=80)
    telegram_id = models.BigIntegerField("Telegram ID", null=True, blank=True, db_index=True)
    is_demo = models.BooleanField("Демонстрационный отзыв", default=False)
    order_item = models.ForeignKey(OrderItem, on_delete=models.PROTECT, null=True, blank=True, related_name="reviews", verbose_name="Подтверждённая покупка")
    rating = models.PositiveSmallIntegerField("Оценка", choices=[(n, str(n)) for n in range(1, 6)])
    text = models.TextField("Отзыв", max_length=2000)
    is_published = models.BooleanField("Опубликован", default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["product", "telegram_id"], name="one_review_per_telegram_buyer")]
        verbose_name = "Отзыв"
        verbose_name_plural = "Отзывы"

    def clean(self):
        if self.is_published and (not self.telegram_id or not self.order_item_id
                or self.order_item.product_id != self.product_id
                or self.order_item.order.telegram_id != self.telegram_id
                or self.order_item.order.is_demo != self.is_demo
                or self.order_item.order.status != Order.Status.DELIVERED
                or self.order_item.order.payment_state not in ("paid", "receipt")):
            raise ValidationError("Опубликовать можно только отзыв о доставленной покупке этого пользователя")


class ProductQuestion(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="questions")
    author = models.CharField("Имя", max_length=80)
    telegram_id = models.BigIntegerField("Telegram ID", null=True, blank=True, db_index=True)
    is_demo = models.BooleanField("Демонстрационный вопрос", default=False)
    text = models.TextField("Вопрос", max_length=1000)
    answer = models.TextField("Ответ магазина", blank=True)
    is_published = models.BooleanField("Опубликован", default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Вопрос о товаре"
        verbose_name_plural = "Вопросы о товарах"


class ReturnRequest(models.Model):
    order_item = models.ForeignKey(OrderItem, on_delete=models.PROTECT, related_name="return_requests")
    quantity = models.PositiveSmallIntegerField("Количество", default=1)
    reason = models.CharField("Причина", max_length=40, choices=[("defect", "Неисправность"), ("wrong", "Не тот товар"), ("other", "Другая причина")])
    comment = models.TextField("Комментарий", max_length=1000)
    status = models.CharField("Статус", max_length=20, choices=[("new", "Новая"), ("review", "На рассмотрении"), ("approved", "Одобрена"), ("rejected", "Отклонена"), ("closed", "Закрыта")], default="new")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Заявка на возврат"
        verbose_name_plural = "Заявки на возврат"


class ProductCollection(models.Model):
    title = models.CharField("Название подборки", max_length=100)
    description = models.CharField("Описание", max_length=240, blank=True)
    products = models.ManyToManyField(Product, related_name="collections", verbose_name="Товары")
    is_visible = models.BooleanField("Показывать", default=True)
    order = models.PositiveSmallIntegerField("Порядок", default=0)

    class Meta:
        ordering = ["order", "id"]
        verbose_name = "Подборка товаров"
        verbose_name_plural = "Подборки товаров"

    def __str__(self):
        return self.title


class RecentlyViewed(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="recent_views")
    telegram_id = models.BigIntegerField("Telegram ID", db_index=True)
    viewed_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["product", "telegram_id"], name="unique_recent_telegram_product")]
        ordering = ["-viewed_at"]
        verbose_name = "Просмотренный товар"
        verbose_name_plural = "Просмотренные товары"


# Facets contain public product attributes only; expire across processes within 60s.


@receiver(post_save, sender=Product)
@receiver(post_delete, sender=Product)
def invalidate_product_facets(sender, instance, **kwargs):
    cache.delete(f"catalog-facets:{instance.category_id}")
