import io
from PIL import Image, ImageDraw, ImageFont
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
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
PALETTES = [("#e2eef5", "#315d78"), ("#eee6fa", "#5c4682"), ("#fae9df", "#8c5942"), ("#e6f1e9", "#3a6855"), ("#f1ebdf", "#736247")]


def illustration(name, section, index, variant):
    """Original generated vector-like product illustration; no third-party images."""
    bg, ink = PALETTES[index % len(PALETTES)]
    im = Image.new("RGB", (900, 900), bg)
    d = ImageDraw.Draw(im)
    d.ellipse((130, 125, 770, 765), fill="#ffffff")
    off = variant * 20
    if section == "phones":
        d.rounded_rectangle((330+off, 170, 570+off, 710), radius=40, fill=ink)
        d.rounded_rectangle((345+off, 195, 555+off, 680), radius=25, fill="#d4e9e9")
        d.ellipse((433+off, 205, 467+off, 239), fill=ink)
    elif section == "laptops":
        d.rounded_rectangle((230, 270+off, 670, 580+off), radius=15, fill=ink)
        d.rectangle((245, 285+off, 655, 560+off), fill="#c9e1e9")
        d.polygon([(180, 595+off), (720, 595+off), (760, 640+off), (140, 640+off)], fill=ink)
    elif section == "audio":
        d.arc((280, 250, 620, 620), 180, 360, fill=ink, width=50)
        d.rounded_rectangle((265, 420, 355, 635), radius=30, fill=ink)
        d.rounded_rectangle((545, 420, 635, 635), radius=30, fill=ink)
    elif section == "smart-home":
        d.rounded_rectangle((270, 300, 630, 650), radius=100, fill=ink)
        d.ellipse((340, 360, 560, 580), fill="#d4e9e9")
        d.ellipse((420, 440, 480, 500), fill=ink)
    else:
        d.rounded_rectangle((260, 280, 640, 620), radius=80, fill=ink)
        d.rounded_rectangle((330, 340, 570, 560), radius=45, fill="#d4e9e9")
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 30)
    except OSError:
        font = ImageFont.load_default()
    d.text((80, 795), name, fill=ink, font=font)
    out = io.BytesIO()
    im.save(out, format="WEBP", quality=83)
    return ContentFile(out.getvalue(), name=f"demo-{section}-{index}-{variant}.webp")


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
                if not product.images.exists():
                    for variant in range(2 if index % 3 == 0 else 1):
                        image = ProductImage(product=product, alt=f"Иллюстрация {name}", order=variant)
                        image.image = illustration(name, slug, section_index, variant)
                        image.save()
        self.stdout.write(self.style.SUCCESS(f"Каталог готов: {Product.objects.count()} товаров, добавлено {count}."))
