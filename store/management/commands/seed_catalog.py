from pathlib import Path
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.db.models import F
from store.models import Category, Product, ProductCollection, ProductImage


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
        ("Charge 65W", 4490), ("Power Bank 20K", 5990), ("Magnetic Stand", 2490), ("Cable C 2m", 1490),
        ("Charge GaN 100W", 6990), ("Clear Case", 1790), ("Wallet Case", 2490), ("Laptop Sleeve 14", 3290), ("Laptop Bag 16", 5490)]),
]
PHOTO_ROOT = Path(__file__).resolve().parents[2] / "seed_photos"
SPECIAL_PHOTOS = {
    "accessories-5": "charger-gan.webp",
    "accessories-6": "clear-case.webp",
    "accessories-7": "wallet-case.webp",
    "accessories-8": "laptop-sleeve.webp",
    "accessories-9": "laptop-bag.webp",
}
SPECIAL_DETAILS = {
    "accessories-5": ("Компактная зарядка GaN для ноутбука, смартфона и аксессуаров.", {"Мощность": "100 Вт", "Порты": "2 × USB-C, 1 × USB-A", "Цвет": "Графит", "Гарантия": "12 месяцев"}),
    "accessories-6": ("Прозрачный защитный чехол с усиленными краями.", {"Материал": "Прозрачный TPU", "Цвет": "Прозрачный", "Гарантия": "12 месяцев"}),
    "accessories-7": ("Чехол-книжка с отделениями для карт и магнитной застёжкой.", {"Материал": "Искусственная кожа", "Цвет": "Графит", "Гарантия": "12 месяцев"}),
    "accessories-8": ("Мягкий чехол для ноутбука диагональю до 14 дюймов.", {"Совместимость": "До 14 дюймов", "Цвет": "Песочный", "Гарантия": "12 месяцев"}),
    "accessories-9": ("Сумка для ноутбука диагональю до 16 дюймов с плечевым ремнём.", {"Совместимость": "До 16 дюймов", "Цвет": "Графит", "Гарантия": "12 месяцев"}),
}


class Command(BaseCommand):
    help = "Идемпотентно загружает 29 демотоваров и локальные изображения WebP"

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
                if product_slug in SPECIAL_DETAILS and product.description == f"{name} — продуманный выбор на каждый день. Демонстрационная модель для учебного магазина.":
                    product.description, product.specs = SPECIAL_DETAILS[product_slug]
                    product.save(update_fields=["description", "specs"])
                images = list(product.images.all())
                special = SPECIAL_PHOTOS.get(product_slug)
                demo_photo = any(image.alt.startswith("Демо-фото ") and "ракурс" not in image.alt for image in images)
                add_photo = not special and not demo_photo and (not images or all(image.alt.startswith("Иллюстрация ") for image in images))
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
                if special and not product.images.filter(alt=f"Фото {name} (синтетическое)").exists():
                    if all(image.alt.startswith(("Демо-фото ", "Иллюстрация ")) for image in product.images.all()):
                        ProductImage.objects.filter(product=product).update(order=F("order") + 1)
                        image = ProductImage(product=product, alt=f"Фото {name} (синтетическое)", order=0)
                        image.image = ContentFile((PHOTO_ROOT / special).read_bytes(), name=special)
                        image.save()
                if special and product.images.filter(alt=f"Фото {name} (синтетическое)").exists():
                    product.images.filter(alt__startswith="Демо-фото ").delete()
        for group, accessory_slugs in {
            "phones": ["accessories-1", "accessories-6", "accessories-7", "accessories-4"],
            "laptops": ["accessories-5", "accessories-8", "accessories-9", "accessories-4"],
            "audio": ["accessories-2", "accessories-4"],
        }.items():
            extras = list(Product.objects.filter(slug__in=accessory_slugs))
            for product in Product.objects.filter(category__slug=group):
                if not product.accessories.exists():
                    product.accessories.set(extras)
        for position, title, description, slugs in [
            (0, "В дорогу", "Техника и зарядка для поездок", ["phones-1", "audio-1", "accessories-2", "accessories-5"]),
            (1, "Рабочее место", "Ноутбук и полезные дополнения", ["laptops-1", "accessories-1", "accessories-8", "accessories-4"]),
            (2, "Подарки до 10 000 ₽", "Небольшие вещи, которые пригодятся каждый день", ["audio-1", "smart-home-1", "accessories-3", "accessories-6"]),
        ]:
            collection, created = ProductCollection.objects.get_or_create(title=title, defaults={"description": description, "order": position})
            if created:
                collection.products.set(Product.objects.filter(slug__in=slugs))
        self.stdout.write(self.style.SUCCESS(f"Каталог готов: {Product.objects.count()} товаров, добавлено {count}."))
