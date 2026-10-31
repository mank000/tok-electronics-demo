from pathlib import Path
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.db.models import F
from store.models import Category, Product, ProductImage


DATA = [
    ("Смартфоны", "phones", "◉", [
        ("Nova One 128", 32990), ("Nova One Pro", 48990), ("Pixel Air 256", 41990), ("Orbit Fold", 89990), ("Mira Lite", 19990), ("Vector 5G", 27990)]),
    ("Ноутбуки", "laptops", "▣", [
        ("Atlas Book 14", 64990), ("Atlas Book Pro", 99990), ("Mira Slim 13", 55990), ("Vector Game 16", 124990), ("Orbit Studio", 139990)]),
    ("Аудио", "audio", "♫", [
        ("Echo Buds", 6990), ("Echo Buds Pro", 12990), ("Pulse Headphones", 15990), ("Sonar Speaker", 8990), ("Wave Mini", 4990)]),
    ("Умный дом", "smart-home", "⌂", [
        ("Luma Lamp", 2990), ("Luma Strip", 3990), ("Home Hub", 9990), ("Air Sensor", 5490)]),
    ("Аксессуары", "accessories", "✦", [
        ("Charge 65W", 4490), ("Power Bank 20K", 5990), ("Magnetic Stand", 2490), ("Cable C 2m", 1490)]),
]
PHOTO_ROOT = Path(__file__).resolve().parents[2] / "seed_photos"


class Command(BaseCommand):
    help = "Идемпотентно загружает 24 демотовара и оригинальные локальные изображения WebP"

    def handle(self, *args, **options):
        count = 0
        for section_index, (title, slug, icon, products) in enumerate(DATA):
            category, _ = Category.objects.update_or_create(slug=slug, defaults={"name": title, "icon": icon, "order": section_index})
            for index, (name, price) in enumerate(products):
                product_slug = f"{slug}-{index+1}"
                product, created = Product.objects.get_or_create(slug=product_slug, defaults={
                    "category": category, "name": name, "description": f"{name} — продуманный выбор на каждый день. Демонстрационная модель для учебного магазина.",
                    "specs": {"Гарантия": "12 месяцев", "Цвет": ["Графит", "Серебристый", "Песочный"][index % 3], "Модель": name},
                    "price": price, "availability": ["stock", "stock", "transit", "preorder"][index % 4], "is_featured": index == 0,
                })
                if created:
                    count += 1
                images = list(product.images.all())
                demo_photo = any(image.alt.startswith("Демо-фото ") and "ракурс" not in image.alt for image in images)
                add_photo = not demo_photo and (not images or all(image.alt.startswith("Иллюстрация ") for image in images))
                if add_photo:
                    shift = 2 if slug in ("phones", "laptops") and index == 0 else 1
                    ProductImage.objects.filter(product=product).update(order=F("order") + shift)
                    image = ProductImage(product=product, alt=f"Демо-фото {name} (синтетическое)", order=0)
                    image.image = ContentFile((PHOTO_ROOT / f"{slug}.webp").read_bytes(), name=f"{slug}.webp")
                    image.save()
                if slug in ("phones", "laptops") and index == 0 and (add_photo or demo_photo) and not any("ракурс 2" in image.alt for image in images):
                    image = ProductImage(product=product, alt=f"Демо-фото {name}, ракурс 2 (синтетическое)", order=1)
                    image.image = ContentFile((PHOTO_ROOT / f"{slug}-detail.webp").read_bytes(), name=f"{slug}-detail.webp")
                    image.save()
        self.stdout.write(self.style.SUCCESS(f"Каталог готов: {Product.objects.count()} товаров, добавлено {count}."))
