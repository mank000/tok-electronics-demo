import json
import re
from collections import Counter

from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from .models import Product, ShoppingState
from .security import TelegramAuthError, current_profile


MAX_PRODUCT_ID = 2**63 - 1
SLUG = re.compile(r"[a-z0-9-]{1,50}")


def clean_cart(value):
    if not isinstance(value, dict) or len(value) > 50:
        raise ValueError("Неверная корзина")
    cart = {}
    for key, quantity in value.items():
        if not re.fullmatch(r"[1-9][0-9]{0,18}", key) or int(key) > MAX_PRODUCT_ID:
            raise ValueError("Неверный товар")
        if type(quantity) is not int or not 1 <= quantity <= 20:
            raise ValueError("Количество должно быть от 1 до 20")
        cart[key] = quantity
    ids = Product.objects.filter(pk__in=cart, is_visible=True).exclude(
        availability=Product.Availability.PREORDER,
    ).values_list("pk", flat=True)
    return {str(pk): cart[str(pk)] for pk in ids}


def clean_comparison(slugs):
    if (not isinstance(slugs, list) or len(slugs) > 40
            or any(not isinstance(slug, str) or not SLUG.fullmatch(slug) for slug in slugs)):
        raise ValueError("Неверный список сравнения")
    choices = dict(Product.objects.filter(slug__in=slugs, is_visible=True).values_list("slug", "category__slug"))
    counts = Counter()
    result = []
    for slug in dict.fromkeys(slugs):
        category = choices.get(slug)
        if category and counts[category] < 4 and len(result) < 20:
            counts[category] += 1
            result.append({"slug": slug, "category": category})
    return result


def snapshot(state):
    return {"cart": clean_cart(state.cart), "comparison": clean_comparison(state.comparison)}


def mutate(state, operation):
    if not isinstance(operation, dict):
        raise ValueError("Неверное изменение")
    action = operation.get("action")
    comparison = clean_comparison(state.comparison)
    slugs = [item["slug"] for item in comparison]
    cart = clean_cart(state.cart)
    if action == "import":
        incoming = clean_cart(operation.get("cart"))
        for key, quantity in incoming.items():
            if key in cart or len(cart) < 50:
                cart[key] = max(cart.get(key, 0), quantity)
        slugs += [item["slug"] for item in clean_comparison(operation.get("comparison"))]
    elif action == "cart":
        key = operation.get("product")
        delta = operation.get("delta")
        if not isinstance(key, str) or type(delta) is not int or delta not in (-1, 0, 1):
            raise ValueError("Неверное изменение корзины")
        quantity = min(20, cart.get(key, 0) + delta) if delta else 0
        if quantity > 0:
            if len(cart) >= 50 and key not in cart:
                raise ValueError("В корзине уже 50 товаров")
            if not clean_cart({key: quantity}):
                raise ValueError("Товар недоступен")
            cart[key] = quantity
        else:
            cart.pop(key, None)
    elif action == "compare-add":
        slug = operation.get("product")
        changed = clean_comparison(slugs + [slug])
        if not any(item["slug"] == slug for item in changed):
            raise ValueError("Можно сравнить до четырёх товаров из одного раздела и до двадцати всего.")
        slugs.append(slug)
    elif action == "compare-remove":
        removed = operation.get("products")
        if not isinstance(removed, list) or len(removed) > 20 or any(not isinstance(slug, str) for slug in removed):
            raise ValueError("Неверный список сравнения")
        slugs = [slug for slug in slugs if slug not in removed]
    else:
        raise ValueError("Неизвестное изменение")
    state.cart = cart
    state.comparison = [item["slug"] for item in clean_comparison(slugs)]
    state.save(update_fields=["cart", "comparison"])
    return snapshot(state)


@require_http_methods(["GET", "POST"])
def shopping_state(request):
    try:
        profile = current_profile(request)
    except TelegramAuthError:
        return JsonResponse({"error": "Проверка Telegram не пройдена"}, status=403)
    if not profile:
        return JsonResponse({"error": "Войдите через Telegram"}, status=401)
    try:
        if request.method == "POST":
            if request.content_type != "application/json" or len(request.body) > 8192:
                raise ValueError("Неверный запрос")
            operation = json.loads(request.body)
            with transaction.atomic():
                state, _ = ShoppingState.objects.select_for_update().get_or_create(telegram_id=profile["id"])
                result = mutate(state, operation)
        else:
            state = ShoppingState.objects.filter(telegram_id=profile["id"]).first()
            result = snapshot(state) if state else {"cart": {}, "comparison": []}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"error": "Неверный запрос"}, status=400)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse(result, headers={"Cache-Control": "no-store"})


def remove_purchased_items(telegram_id, order):
    state = ShoppingState.objects.select_for_update().filter(telegram_id=telegram_id).first()
    if not state:
        return
    cart = state.cart.copy()
    for item in order.items.all():
        key = str(item.product_id)
        quantity = cart.get(key, 0) - item.quantity
        if quantity > 0:
            cart[key] = quantity
        else:
            cart.pop(key, None)
    state.cart = cart
    state.save(update_fields=["cart"])
