"""Idempotent sample feedback, deliberately limited to DEBUG demo installations."""

import uuid

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from store.demo_content import FEATURE_QUESTIONS, PRODUCT_COPY, REVIEW_COPY, REVIEW_HIGHLIGHTS
from store.models import Order, OrderItem, Product, ProductQuestion, ProductReview


DEMO_AUTHORS = ((-900001, "Демо · Анна"), (-900002, "Демо · Максим"))


class Command(BaseCommand):
    help = "Добавляет явно помеченные примеры вопросов и отзывов только в локальный демомагазин"

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("Демоотзывы разрешены только при DJANGO_DEBUG=1")
        products = list(Product.objects.filter(slug__in=PRODUCT_COPY).select_related("category").order_by("category__slug", "slug"))
        if len(products) != len(PRODUCT_COPY):
            raise CommandError("Сначала выполните seed_catalog")
        grouped = {}
        for product in products:
            grouped.setdefault(product.category.slug, []).append(product)
        added_reviews = added_questions = added_orders = updated_reviews = 0
        with transaction.atomic():
            for category_slug, category_products in grouped.items():
                for author_index, (telegram_id, author) in enumerate(DEMO_AUTHORS):
                    key = uuid.uuid5(uuid.NAMESPACE_URL, f"tok-demo-feedback:{category_slug}:{author_index}")
                    order, created = Order.objects.get_or_create(checkout_key=key, defaults={
                        "telegram_id": telegram_id, "is_demo": True, "name": author,
                        "phone": "+7 000 000-00-00", "delivery": Order.Delivery.PICKUP,
                        "comment": "Учебный заказ для демонстрационного наполнения карточек. Не реальная продажа.",
                        "payment_method": Order.Payment.ON_RECEIPT, "payment_gateway": "receipt",
                        "payment_state": "receipt", "status": Order.Status.DELIVERED,
                        "total": sum((product.price for product in category_products), start=0),
                    })
                    if not order.is_demo or order.telegram_id != telegram_id:
                        raise CommandError("Ключ демозаказа уже занят другим заказом")
                    added_orders += int(created)
                    for product in category_products:
                        item, _ = OrderItem.objects.get_or_create(order=order, product=product, defaults={
                            "title": product.name, "quantity": 1, "unit_price": product.price,
                        })
                        base_text = f"{product.name}: {REVIEW_COPY[category_slug][author_index]}"
                        review_text = f"{product.name}: {REVIEW_HIGHLIGHTS[product.slug][author_index]} {REVIEW_COPY[category_slug][author_index]}"
                        existing_review = ProductReview.objects.filter(product=product, telegram_id=telegram_id).first()
                        if existing_review and existing_review.is_demo and existing_review.text == base_text:
                            existing_review.text = review_text
                            existing_review.save(update_fields=["text"])
                            updated_reviews += 1
                        elif not existing_review:
                            review = ProductReview(product=product, author=author, telegram_id=telegram_id,
                                order_item=item, is_demo=True, rating=5 if author_index == 0 else 4,
                                text=review_text, is_published=True)
                            review.full_clean()
                            review.save()
                            added_reviews += 1
                        if not ProductQuestion.objects.filter(product=product, telegram_id=telegram_id, is_demo=True).exists():
                            if author_index == 0:
                                _, specs = PRODUCT_COPY[product.slug]
                                feature, value = next(iter(specs.items()))
                                question = FEATURE_QUESTIONS.get(feature, "Какая основная характеристика у {name}?").format(name=product.name)
                                answer = f"{feature}: {value}. Остальные параметры указаны в характеристиках карточки."
                            elif product.availability == Product.Availability.PREORDER:
                                question = f"Можно ли сейчас заказать {product.name}?"
                                answer = "Это товар под заказ. Нажмите «Хочу заказать», и мы сообщим о доступности; сроки пока ориентировочные."
                            else:
                                question = f"Когда примерно доставят {product.name}?"
                                answer = f"Ориентир по городу — от {product.delivery_min_days} до {product.delivery_max_days} дней. Точную дату подтвердим после оформления заказа."
                            ProductQuestion.objects.create(product=product, author=author, telegram_id=telegram_id,
                                is_demo=True, text=question, answer=answer, is_published=True)
                            added_questions += 1
        self.stdout.write(self.style.SUCCESS(
            f"Демоотзывы: +{added_reviews}, обновлено {updated_reviews}; демовопросы: +{added_questions}, демозаказы: +{added_orders}."
        ))
