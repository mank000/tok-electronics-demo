import json
from urllib.parse import urlencode
from xml.etree import ElementTree

from django.conf import settings
from django.http import HttpResponse
from django.urls import reverse
from django.views.decorators.http import require_GET

from .integrations import get_runtime_config
from .models import Category, Product, ShopSettings


def site_origin(request):
    configured = get_runtime_config().public_base_url if settings.SEO_INDEXABLE else ""
    return (configured or request.build_absolute_uri("/")).rstrip("/")


def public_url(request, path):
    return site_origin(request) + path


def catalog_metadata(request, category=None):
    shop = ShopSettings.objects.filter(pk=1).first() or ShopSettings()
    if category:
        title = f"{category.name} — каталог {shop.name}"
        description = f"{category.name}: товары, цены и характеристики в каталоге {shop.name}. Выберите подходящую модель и оформите заказ."
        path = reverse("catalog") + "?" + urlencode({"category": category.slug})
    else:
        title = f"{shop.name} — {shop.subtitle}"
        description = shop.subtitle
        path = reverse("catalog")
    clean_query = not request.GET or (category is not None and set(request.GET) == {"category"})
    return {"seo_title": title, "seo_description": description[:160], "canonical_url": public_url(request, path),
            "robots_directive": "index,follow" if settings.SEO_INDEXABLE and clean_query else "noindex,follow",
            "og_type": "website"}


def product_metadata(request, product):
    shop = ShopSettings.objects.filter(pk=1).first() or ShopSettings()
    canonical = public_url(request, reverse("product", args=[product.slug]))
    photo = product.first_image
    image = public_url(request, photo.image.url) if photo else ""
    description = " ".join(product.description.split())[:160]
    meta = {"seo_title": f"{product.name} — {shop.name}", "seo_description": description,
            "canonical_url": canonical, "robots_directive": "index,follow" if settings.SEO_INDEXABLE and not request.GET else "noindex,follow",
            "og_type": "product", "og_image": image}
    if settings.SEO_INDEXABLE:
        schema = {"@context": "https://schema.org", "@type": "Product", "name": product.name,
                  "description": product.description, "url": canonical, "category": product.category.name}
        if image:
            schema["image"] = [image]
        if product.availability != Product.Availability.PREORDER:
            schema["offers"] = {"@type": "Offer", "url": canonical, "priceCurrency": "RUB",
                                "price": str(product.current_price), "availability": "https://schema.org/InStock" if product.availability == Product.Availability.IN_STOCK else "https://schema.org/BackOrder"}
        meta["product_schema"] = json.dumps(schema, ensure_ascii=False).replace("<", "\\u003C")
    return meta


@require_GET
def robots_txt(request):
    if not settings.SEO_INDEXABLE:
        return HttpResponse("User-agent: *\nDisallow: /\n", content_type="text/plain; charset=utf-8")
    lines = ["User-agent: *", "Allow: /", "Disallow: /admin/", "Disallow: /account/", "Disallow: /api/", "Disallow: /basket/", "Disallow: /checkout/", "Disallow: /compare/", "Disallow: /messages/", "Disallow: /order/", "Disallow: /pay/", "Disallow: /support/", "Sitemap: " + public_url(request, "/sitemap.xml")]
    return HttpResponse("\n".join(lines) + "\n", content_type="text/plain; charset=utf-8")


@require_GET
def sitemap_xml(request):
    if not settings.SEO_INDEXABLE:
        return HttpResponse(status=404)
    root = ElementTree.Element("urlset", xmlns="http://www.sitemaps.org/schemas/sitemap/0.9")
    paths = [reverse("catalog")]
    paths += [reverse("catalog") + "?" + urlencode({"category": slug}) for slug in Category.objects.filter(products__is_visible=True).values_list("slug", flat=True).distinct()]
    paths += [reverse("product", args=[slug]) for slug in Product.objects.filter(is_visible=True).values_list("slug", flat=True)]
    origin = site_origin(request)
    for path in paths:
        ElementTree.SubElement(ElementTree.SubElement(root, "url"), "loc").text = origin + path
    return HttpResponse(ElementTree.tostring(root, encoding="utf-8", xml_declaration=True), content_type="application/xml; charset=utf-8")
