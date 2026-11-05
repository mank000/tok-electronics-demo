import json
import re
import uuid
from datetime import timedelta
from decimal import Decimal
from django.conf import settings
from django.middleware.csrf import get_token
from django.db import IntegrityError, transaction
from django.db.models import Avg
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from .forms import CheckoutForm, QuestionForm, ReturnForm, ReviewForm
from .integrations import get_runtime_config, yookassa_configured
from .models import BotUser, Category, CustomerProfile, Favorite, Order, OrderItem, PaymentAttempt, Product, ProductCollection, ProductQuestion, ProductReview, RecentlyViewed, ReturnRequest, Wish
from .phone_login import issue_code, normalize_phone, verify_code
from .security import TelegramAuthError, current_profile, telegram_id_from_request, verified_init_profile
from .telegram_login import complete_login, configured as telegram_login_configured, start_login
from .services import PaymentError, cart_lines, process_payment_event, queue_buyer, queue_order_created, queue_shop, signed_event


def page(request, name, context=None, status=200):
    get_token(request)
    response = render(request, f"store/{name}.html", context or {}, status=status)
    response["Referrer-Policy"] = "no-referrer"
    response["Cache-Control"] = "no-store"
    return response


def catalog(request):
    category = request.GET.get("category", "")
    search = request.GET.get("q", "").strip()[:100]
    sort = request.GET.get("sort", "")
    products = Product.objects.filter(is_visible=True).select_related("category").prefetch_related("images")
    if category:
        products = products.filter(category__slug=category)
    if search:
        products = products.filter(name__icontains=search)
    if sort in ("price", "-price"):
        products = products.order_by(sort, "id")
    return page(request, "catalog", {"categories": Category.objects.all(), "products": products, "active_category": category, "query": search, "sort": sort, "featured": Product.objects.filter(is_visible=True, is_featured=True).prefetch_related("images")[:4], "collections": ProductCollection.objects.filter(is_visible=True).prefetch_related("products__images")[:3]})


def product_context(item, **extra):
    today = timezone.localdate()
    config = get_runtime_config()
    miniapp_url = f"https://t.me/{config.bot_username}/{config.bot_app_short_name}?startapp=product_{item.slug}" if config.bot_username and config.bot_app_short_name else ""
    accessories = list(item.accessories.filter(is_visible=True).prefetch_related("images")[:4])
    if not accessories and item.category.slug != "accessories":
        accessories = list(Product.objects.filter(category__slug="accessories", is_visible=True).exclude(pk=item.pk).prefetch_related("images")[:4])
    published_reviews = item.reviews.filter(is_published=True, telegram_id__isnull=False)
    context = {
        "product": item, "review_form": ReviewForm(), "question_form": QuestionForm(),
        "reviews": published_reviews, "questions": item.questions.filter(is_published=True),
        "rating": published_reviews.aggregate(value=Avg("rating"))["value"],
        "has_demo_reviews": published_reviews.filter(is_demo=True).exists(),
        "delivery_from": today + timedelta(days=item.delivery_min_days),
        "delivery_to": today + timedelta(days=item.delivery_max_days),
        "related": Product.objects.filter(category=item.category, is_visible=True).exclude(pk=item.pk).prefetch_related("images")[:4],
        "accessories": accessories,
        "miniapp_url": miniapp_url,
        "telegram_login_ready": telegram_login_configured(),
    }
    context.update(extra)
    return context


def product(request, slug):
    item = get_object_or_404(Product.objects.prefetch_related("images"), slug=slug, is_visible=True)
    return page(request, "product", product_context(item))


@require_GET
def compare(request):
    slugs = [slug for slug in request.GET.get("items", "")[:1024].split(",") if re.fullmatch(r"[a-z0-9-]{1,50}", slug)]
    slugs = list(dict.fromkeys(slugs))[:20]
    products = Product.objects.filter(slug__in=slugs, is_visible=True).select_related("category").prefetch_related("images")
    by_slug = {item.slug: item for item in products}
    groups = {}
    for slug in slugs:
        item = by_slug.get(slug)
        if item:
            group = groups.setdefault(item.category.slug, {"category": item.category, "products": []})
            if len(group["products"]) < 4:
                group["products"].append(item)
    selected = request.GET.get("category", "")
    if selected not in groups:
        selected = next(iter(groups), "")
    group = groups.get(selected)
    compare_items = [{"slug": item.slug, "category": item.category.slug} for entry in groups.values() for item in entry["products"]]
    rows = []
    if group:
        items = group["products"]
        availability = [item.get_availability_display() for item in items]
        rows.append({"label": "Наличие", "values": availability, "different": len(set(availability)) > 1})
        labels = dict.fromkeys(key for item in items for key in (item.specs if isinstance(item.specs, dict) else {}) if isinstance(key, str))
        for label in labels:
            values = [str((item.specs if isinstance(item.specs, dict) else {}).get(label, "") or "").strip() for item in items]
            rows.append({"label": label, "values": values, "different": len(set(values)) > 1})
    return page(request, "compare", {"groups": list(groups.values()), "selected_category": selected, "products": group["products"] if group else [], "rows": rows, "compare_items": compare_items, "compare_query": ",".join(item["slug"] for item in compare_items)})


def owner(request):
    profile = current_profile(request)
    if not profile:
        raise TelegramAuthError("Войдите через Telegram")
    return {"telegram_id": profile["id"]}


def purchased_item(profile, product):
    return OrderItem.objects.filter(product=product, order__telegram_id=profile["id"], order__is_demo=False, order__status=Order.Status.DELIVERED, order__payment_state__in=("paid", "receipt")).select_related("order").order_by("-order__created_at").first()


@require_POST
def wish(request, slug):
    item = get_object_or_404(Product, slug=slug, is_visible=True, availability=Product.Availability.PREORDER)
    try:
        profile = current_profile(request)
    except TelegramAuthError:
        return HttpResponseForbidden("Проверка Telegram не пройдена")
    if not profile:
        return HttpResponseForbidden("Войдите через Telegram")
    obj, created = Wish.objects.get_or_create(product=item, telegram_id=profile["id"], defaults={"name": profile["name"], "contact": f"Telegram ID {profile['id']}"})
    if created:
        queue_shop(f"Заявка «Хочу такой» №{obj.pk}: {item.name}, {obj.name}, Telegram ID {profile['id']}.")
    return page(request, "wish_done", {"product": item})


@require_POST
def review(request, slug):
    item = get_object_or_404(Product, slug=slug, is_visible=True)
    try:
        profile = current_profile(request)
    except TelegramAuthError:
        return HttpResponseForbidden("Проверка Telegram не пройдена")
    if not profile:
        return HttpResponseForbidden("Войдите через Telegram")
    purchase = purchased_item(profile, item)
    if not purchase:
        return HttpResponseForbidden("Отзыв доступен после покупки и доставки")
    if ProductReview.objects.filter(product=item, telegram_id=profile["id"]).exists():
        return HttpResponseBadRequest("Вы уже оставили отзыв об этом товаре")
    form = ReviewForm(request.POST)
    if not form.is_valid():
        return page(request, "product", product_context(item, review_form=form), 400)
    entry = form.save(commit=False)
    entry.product = item
    entry.telegram_id = profile["id"]
    entry.order_item = purchase
    entry.author = profile["name"]
    entry.save()
    return redirect(reverse("product", args=[slug]) + "?sent=review#reviews")


@require_POST
def question(request, slug):
    item = get_object_or_404(Product, slug=slug, is_visible=True)
    try:
        profile = current_profile(request)
    except TelegramAuthError:
        return HttpResponseForbidden("Проверка Telegram не пройдена")
    if not profile:
        return HttpResponseForbidden("Войдите через Telegram")
    form = QuestionForm(request.POST)
    if not form.is_valid():
        return page(request, "product", product_context(item, question_form=form), 400)
    entry = form.save(commit=False)
    entry.product = item
    entry.telegram_id = profile["id"]
    entry.author = profile["name"]
    entry.save()
    return redirect(reverse("product", args=[slug]) + "?sent=question#questions")


@require_POST
def favorite_toggle(request, slug):
    item = get_object_or_404(Product, slug=slug, is_visible=True)
    try:
        identity = owner(request)
    except TelegramAuthError:
        return JsonResponse({"error": "Войдите через Telegram"}, status=401)
    match = Favorite.objects.filter(product=item, **identity)
    if match.exists():
        match.delete()
        selected = False
    else:
        Favorite.objects.create(product=item, **identity)
        selected = True
    return JsonResponse({"favorite": selected})


def favorite_state(request, slug):
    item = get_object_or_404(Product, slug=slug, is_visible=True)
    try:
        profile = current_profile(request)
    except TelegramAuthError:
        return HttpResponseForbidden("Проверка Telegram не пройдена")
    if not profile:
        return JsonResponse({"authenticated": False, "favorite": False, "can_review": False, "already_reviewed": False})
    reviewed = ProductReview.objects.filter(product=item, telegram_id=profile["id"]).exists()
    return JsonResponse({"authenticated": True, "favorite": Favorite.objects.filter(product=item, telegram_id=profile["id"]).exists(), "can_review": bool(purchased_item(profile, item)) and not reviewed, "already_reviewed": reviewed})


@require_POST
def record_view(request, slug):
    item = get_object_or_404(Product, slug=slug, is_visible=True)
    try:
        profile = current_profile(request)
    except TelegramAuthError:
        return HttpResponseForbidden("Проверка Telegram не пройдена")
    if not profile:
        return JsonResponse({"saved": False})
    RecentlyViewed.objects.update_or_create(product=item, telegram_id=profile["id"])
    return JsonResponse({"saved": True})


@ensure_csrf_cookie
def basket(request):
    return page(request, "basket", {"products": Product.objects.filter(is_visible=True).exclude(availability="preorder").prefetch_related("images")})


@require_POST
def cart_quote(request):
    try:
        telegram_id_from_request(request)
        payload = json.loads(request.body)
        lines = cart_lines(payload.get("cart"))
    except TelegramAuthError:
        return HttpResponseForbidden("Проверка Telegram не пройдена")
    except (ValueError, TypeError) as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({"items": [{"id": p.pk, "name": p.name, "quantity": q, "price": str(p.price), "subtotal": str(p.price * q), "url": reverse("product", args=[p.slug]), "image": p.first_image.thumb.url if p.first_image else ""} for p, q in lines], "total": str(sum((p.price * q for p, q in lines), Decimal(0)))})


def my_contact(request):
    try:
        tg_id = telegram_id_from_request(request)
    except TelegramAuthError:
        return HttpResponseForbidden("Проверка Telegram не пройдена")
    if tg_id is None:
        return HttpResponseForbidden("Требуется Telegram")
    user = BotUser.objects.filter(telegram_id=tg_id).first()
    return JsonResponse({"phone": user.phone if user else ""})


@require_http_methods(["GET", "POST"])
def registration(request):
    try:
        profile = verified_init_profile(request.headers.get("X-Telegram-Init-Data", ""))
    except TelegramAuthError:
        return JsonResponse({"error": "Откройте магазин внутри Telegram"}, status=403)
    bot_user = BotUser.objects.filter(telegram_id=profile["id"]).first()
    has_phone = bool(bot_user and bot_user.phone and bot_user.phone_digits)
    saved = CustomerProfile.objects.filter(telegram_id=profile["id"]).first()
    if request.method == "GET":
        return JsonResponse({"registered": bool(saved and has_phone), "has_phone": has_phone, "name": saved.name if saved else profile["name"]})
    if not has_phone:
        return JsonResponse({"error": "Сначала разрешите Telegram передать номер"}, status=409)
    name = " ".join(request.POST.get("name", "").split())
    if not 2 <= len(name) <= 80:
        return JsonResponse({"error": "Укажите имя от 2 до 80 символов"}, status=400)
    CustomerProfile.objects.update_or_create(telegram_id=profile["id"], defaults={"name": name})
    return JsonResponse({"registered": True, "name": name})


def checkout(request):
    if request.method == "GET":
        yoo_enabled = yookassa_configured()
        saved_phone = BotUser.objects.filter(telegram_id=request.session.get("telegram_id")).values_list("phone", flat=True).first() if request.session.get("telegram_id") else ""
        return page(request, "checkout", {"form": CheckoutForm(initial={"checkout_key": uuid.uuid4(), "delivery": "pickup", "payment_method": "online" if settings.DEMO_PAYMENTS or yoo_enabled else "receipt", "payment_gateway": "demo" if settings.DEMO_PAYMENTS else "yookassa", "name": request.session.get("telegram_name", ""), "phone": saved_phone}), "demo_enabled": settings.DEMO_PAYMENTS, "yoo_enabled": yoo_enabled})
    form = CheckoutForm(request.POST)
    if not form.is_valid():
        return page(request, "checkout", {"form": form, "demo_enabled": settings.DEMO_PAYMENTS, "yoo_enabled": yookassa_configured()}, 400)
    try:
        profile = current_profile(request)
    except TelegramAuthError:
        return HttpResponseForbidden("Проверка Telegram не пройдена")
    tg_id = profile["id"] if profile else None
    if not profile and not form.cleaned_data["name"].strip():
        form.add_error("name", "Укажите имя или войдите через Telegram")
        return page(request, "checkout", {"form": form, "demo_enabled": settings.DEMO_PAYMENTS, "yoo_enabled": yookassa_configured()}, 400)
    key = form.cleaned_data["checkout_key"]
    existing = Order.objects.filter(checkout_key=key).first()
    known = request.session.get("checkout_tokens", {})
    if existing:
        if known.get(str(key)) == existing.access_token:
            return redirect(reverse("order", args=[existing.access_token]) + "?created=1")
        return HttpResponse("Повторное оформление уже обработано", status=409)
    data = form.cleaned_data
    try:
        with transaction.atomic():
            lines = cart_lines(data["cart"], lock=True)
            gateway = data["payment_gateway"] if data["payment_method"] == "online" else "receipt"
            order = Order.objects.create(checkout_key=key, telegram_id=tg_id, name=profile["name"] if profile else data["name"].strip(), phone=data["phone"], delivery=data["delivery"], address=data["address"].strip(), comment=data["comment"].strip(), payment_method=data["payment_method"], payment_gateway=gateway, total=sum((p.price * q for p, q in lines), Decimal(0)), payment_state="receipt" if data["payment_method"] == "receipt" else "pending")
            OrderItem.objects.bulk_create([OrderItem(order=order, product=p, title=p.name, quantity=q, unit_price=p.price) for p, q in lines])
            if order.payment_method == "online":
                PaymentAttempt.objects.create(order=order, gateway=gateway)
            queue_order_created(order)
    except ValueError as exc:
        form.add_error(None, str(exc))
        return page(request, "checkout", {"form": form, "demo_enabled": settings.DEMO_PAYMENTS, "yoo_enabled": yookassa_configured()}, 400)
    except IntegrityError:
        return HttpResponse("Повторное оформление уже обработано", status=409)
    known[str(key)] = order.access_token
    request.session["checkout_tokens"] = known
    if tg_id is None:
        request.session["browser_order_tokens"] = list(dict.fromkeys(request.session.get("browser_order_tokens", []) + [order.access_token]))[-100:]
    return redirect(reverse("order", args=[order.access_token]) + "?created=1")


def owned_order(request, token):
    order = get_object_or_404(Order.objects.prefetch_related("items__return_requests"), access_token=token)
    try:
        profile = current_profile(request)
    except TelegramAuthError:
        return None, HttpResponseForbidden("Проверка Telegram не пройдена")
    if profile is not None and order.telegram_id is not None and order.telegram_id != profile["id"]:
        return None, HttpResponseForbidden("Это чужой заказ")
    return order, None


@ensure_csrf_cookie
def order_page(request, token):
    order, denied = owned_order(request, token)
    if denied is not None:
        return denied
    return page(request, "order", {"order": order, "demo": settings.DEMO_PAYMENTS})


def demo_pay(request, token):
    if not settings.DEMO_PAYMENTS:
        return HttpResponse(status=404)
    order, denied = owned_order(request, token)
    if denied is not None:
        return denied
    if order.payment_gateway != "demo" or order.payment_method != "online" or order.payment_state == "paid":
        return redirect("order", token=token)
    if request.method == "POST":
        action = request.POST.get("action")
        if action not in ("paid", "declined"):
            return HttpResponseBadRequest("Неверное действие")
        attempt = order.attempts.order_by("-created_at").first()
        if not attempt or attempt.state != "pending":
            attempt = PaymentAttempt.objects.create(order=order, gateway="demo")
        raw = json.dumps({"event_id": str(uuid.uuid4()), "attempt": str(attempt.reference), "status": action}, separators=(",", ":")).encode()
        try:
            process_payment_event(raw, signed_event(raw))
        except PaymentError:
            return HttpResponseBadRequest("Ошибка платежа")
        return redirect("order", token=token)
    return page(request, "payment", {"order": order})


def yoo_pay(request, token):
    from .yookassa import create_payment
    order, denied = owned_order(request, token)
    if denied is not None:
        return denied
    if order.payment_gateway != "yookassa" or order.payment_method != "online" or order.payment_state == "paid":
        return redirect("order", token=token)
    attempt = order.attempts.order_by("-created_at").first()
    if not attempt or attempt.state != "pending":
        attempt = PaymentAttempt.objects.create(order=order, gateway="yookassa")
    if not attempt.confirmation_token:
        try:
            create_payment(order, attempt, request.build_absolute_uri(reverse("order", args=[token])))
        except PaymentError as exc:
            return page(request, "yoo_payment", {"order": order, "error": str(exc)}, 503)
    return page(request, "yoo_payment", {"order": order, "confirmation_token": attempt.confirmation_token})


@require_POST
def yoo_status(request, token):
    from .yookassa import sync_payment
    order, denied = owned_order(request, token)
    if denied is not None:
        return denied
    attempt = order.attempts.filter(gateway="yookassa", provider_id__gt="").order_by("-created_at").first()
    if not attempt:
        return JsonResponse({"error": "Платёж не найден"}, status=404)
    try:
        sync_payment(attempt)
    except PaymentError as exc:
        return JsonResponse({"error": str(exc)}, status=503)
    order.refresh_from_db()
    return JsonResponse({"state": order.payment_state})


@csrf_exempt
@require_POST
def yoo_webhook(request):
    from .yookassa import sync_payment
    try:
        data = json.loads(request.body)
        provider_id = data["object"]["id"]
        if not isinstance(provider_id, str) or len(provider_id) > 80:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        return HttpResponseBadRequest("Неверное событие")
    attempt = PaymentAttempt.objects.filter(gateway="yookassa", provider_id=provider_id).first()
    if not attempt:
        return HttpResponseBadRequest("Платёж не найден")
    try:
        sync_payment(attempt)
    except PaymentError:
        return HttpResponse(status=503)
    return JsonResponse({"ok": True})


@require_GET
def telegram_login_start(request):
    next_path = safe_login_next(request, request.GET.get("next"))
    try:
        return redirect(start_login(request, next_path))
    except TelegramAuthError as exc:
        return page(request, "login_unavailable", {"message": str(exc)}, 503)


@require_GET
def telegram_login_callback(request):
    try:
        profile, next_path = complete_login(request)
    except TelegramAuthError:
        return redirect(reverse("account") + "?login_error=1")
    saved = CustomerProfile.objects.filter(telegram_id=profile["id"]).only("name").first()
    if saved:
        profile["name"] = saved.name
    link_browser_account(request, profile)
    return redirect(next_path)


def safe_login_next(request, value):
    if not value or not value.startswith("/") or "\\" in value or not url_has_allowed_host_and_scheme(value, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return reverse("account")
    return value


def link_browser_account(request, profile):
    old_session = request.session.session_key
    tokens = request.session.get("browser_order_tokens", []) + list(request.session.get("checkout_tokens", {}).values())
    with transaction.atomic():
        Order.objects.filter(access_token__in=tokens, telegram_id__isnull=True).update(telegram_id=profile["id"])
        if old_session:
            for favorite in Favorite.objects.filter(browser_session=old_session):
                Favorite.objects.get_or_create(product=favorite.product, telegram_id=profile["id"])
            Favorite.objects.filter(browser_session=old_session).delete()
            Wish.objects.filter(browser_session=old_session, telegram_id__isnull=True).update(telegram_id=profile["id"], browser_session="")
    request.session.cycle_key()
    request.session["telegram_id"] = profile["id"]
    request.session["telegram_name"] = profile["name"]
    request.session.set_expiry(60 * 60 * 24 * 30)


@require_POST
def phone_login_request(request):
    phone = request.POST.get("phone", "")
    if not normalize_phone(phone):
        return JsonResponse({"error": "Укажите номер с кодом страны"}, status=400)
    issue_code(request, phone, safe_login_next(request, request.POST.get("next")))
    return JsonResponse({"ok": True, "message": "Если номер зарегистрирован, код придёт в чат с ботом. Он действует 5 минут."})


@require_POST
def phone_login_verify(request):
    result = verify_code(request, request.POST.get("code"))
    if not result:
        return JsonResponse({"error": "Код неверен или истёк"}, status=400)
    profile, next_path = result
    link_browser_account(request, profile)
    return JsonResponse({"ok": True, "redirect": next_path})


@require_POST
def telegram_logout(request):
    request.session.flush()
    return redirect("account")


def account(request, section=None):
    if section not in (None, "orders", "purchases", "wishes", "favorites"):
        return HttpResponse(status=404)
    return page(request, "account", {"account_section": section, "telegram_login_ready": telegram_login_configured(), "phone_login_ready": bool(get_runtime_config().bot_token)})


def support(request):
    config = get_runtime_config()
    username = config.bot_username
    if not config.bot_token or not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        username = ""
    return page(request, "support", {"bot_username": username})


def account_data(request):
    try:
        profile = current_profile(request)
    except TelegramAuthError:
        return HttpResponseForbidden("Проверка Telegram не пройдена")
    if not profile:
        return JsonResponse({"error": "Войдите через Telegram"}, status=401)
    orders = Order.objects.filter(telegram_id=profile["id"], is_demo=False)
    wishes = Wish.objects.filter(telegram_id=profile["id"])
    def item_data(item):
        return {"name": item.title, "quantity": item.quantity, "id": item.pk}
    def order_data(order):
        return {"id": order.pk, "url": reverse("order", args=[order.access_token]), "status": order.get_status_display(), "status_code": order.status, "total": str(order.total), "created": order.created_at.strftime("%d.%m.%Y"), "items": [item_data(item) for item in order.items.all()]}
    orders = list(orders.prefetch_related("items"))
    favorites = Favorite.objects.filter(telegram_id=profile["id"], product__is_visible=True).select_related("product").order_by("-created_at")
    recent = RecentlyViewed.objects.filter(telegram_id=profile["id"], product__is_visible=True).select_related("product").prefetch_related("product__images")[:12]
    return JsonResponse({
        "profile": {"name": profile["name"]},
        "current": [order_data(o) for o in orders if o.status not in (Order.Status.DELIVERED, Order.Status.CANCELLED)],
        "purchases": [order_data(o) for o in orders if o.status == Order.Status.DELIVERED],
        "favorites": [{"name": f.product.name, "url": reverse("product", args=[f.product.slug]), "price": str(f.product.price)} for f in favorites],
        "wishes": [{"name": w.product.name, "url": reverse("product", args=[w.product.slug]), "created": w.created_at.strftime("%d.%m.%Y")} for w in wishes.select_related("product").order_by("-created_at")],
        "recent": [{"name": v.product.name, "url": reverse("product", args=[v.product.slug]), "price": str(v.product.price), "image": v.product.first_image.thumb.url if v.product.first_image else ""} for v in recent],
    })


@require_POST
def request_return(request, token, item_id):
    order, denied = owned_order(request, token)
    if denied is not None:
        return denied
    item = get_object_or_404(OrderItem, pk=item_id, order=order)
    if order.status != Order.Status.DELIVERED:
        return HttpResponseBadRequest("Заявку на возврат можно подать после выдачи заказа")
    if item.return_requests.exists():
        return HttpResponseBadRequest("Заявка по этой позиции уже есть")
    form = ReturnForm(request.POST)
    if not form.is_valid() or not 1 <= form.cleaned_data["quantity"] <= item.quantity:
        return HttpResponseBadRequest("Проверьте данные возврата")
    entry = form.save(commit=False)
    entry.order_item = item
    entry.save()
    queue_shop(f"Заявка на возврат №{entry.pk}: заказ №{order.pk}, {item.title} × {entry.quantity}.")
    return redirect(reverse("order", args=[token]) + "#returns")


@csrf_exempt
@require_POST
def payment_webhook(request):
    try:
        changed = process_payment_event(request.body, request.headers.get("X-Payment-Signature", ""))
    except PaymentError as exc:
        return JsonResponse({"error": str(exc)}, status=403)
    return JsonResponse({"ok": True, "duplicate": not changed})


def document(request, kind):
    if kind not in ("privacy", "offer"):
        return HttpResponse(status=404)
    return page(request, "document", {"kind": kind})
