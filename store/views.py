import json
import uuid
from datetime import timedelta
from decimal import Decimal
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Avg
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST
from .forms import CheckoutForm, QuestionForm, ReturnForm, ReviewForm, WishForm
from .integrations import get_runtime_config
from .models import BotUser, Category, Favorite, Order, OrderItem, PaymentAttempt, Product, ProductCollection, ProductQuestion, ProductReview, ReturnRequest, Wish
from .security import TelegramAuthError, telegram_id_from_request
from .services import PaymentError, cart_lines, process_payment_event, queue_buyer, queue_order_created, queue_shop, signed_event


def page(request, name, context=None, status=200):
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
    context = {
        "product": item, "form": WishForm(), "review_form": ReviewForm(), "question_form": QuestionForm(),
        "reviews": item.reviews.filter(is_published=True), "questions": item.questions.filter(is_published=True),
        "rating": item.reviews.filter(is_published=True).aggregate(value=Avg("rating"))["value"],
        "delivery_from": today + timedelta(days=item.delivery_min_days),
        "delivery_to": today + timedelta(days=item.delivery_max_days),
        "related": Product.objects.filter(category=item.category, is_visible=True).exclude(pk=item.pk).prefetch_related("images")[:4],
        "accessories": accessories,
        "miniapp_url": miniapp_url,
    }
    context.update(extra)
    return context


def product(request, slug):
    item = get_object_or_404(Product.objects.prefetch_related("images"), slug=slug, is_visible=True)
    return page(request, "product", product_context(item))


def browser_session(request):
    if not request.session.session_key:
        request.session.create()
    return request.session.session_key


def owner(request):
    tg_id = telegram_id_from_request(request)
    return {"telegram_id": tg_id} if tg_id is not None else {"browser_session": browser_session(request)}


@require_POST
def wish(request, slug):
    item = get_object_or_404(Product, slug=slug, is_visible=True, availability=Product.Availability.PREORDER)
    form = WishForm(request.POST)
    if not form.is_valid():
        return page(request, "product", product_context(item, form=form), 400)
    try:
        tg_id = telegram_id_from_request(request)
    except TelegramAuthError:
        return HttpResponseForbidden("Проверка Telegram не пройдена")
    obj = Wish.objects.create(product=item, telegram_id=tg_id, browser_session="" if tg_id else browser_session(request), **form.cleaned_data)
    queue_shop(f"Заявка «Хочу такой» №{obj.pk}: {item.name}, {obj.name}, {obj.contact}.")
    return page(request, "wish_done", {"product": item})


@require_POST
def review(request, slug):
    item = get_object_or_404(Product, slug=slug, is_visible=True)
    try:
        telegram_id_from_request(request)
    except TelegramAuthError:
        return HttpResponseForbidden("Проверка Telegram не пройдена")
    form = ReviewForm(request.POST)
    if not form.is_valid():
        return page(request, "product", product_context(item, review_form=form), 400)
    entry = form.save(commit=False)
    entry.product = item
    entry.save()
    return redirect(reverse("product", args=[slug]) + "?sent=review#reviews")


@require_POST
def question(request, slug):
    item = get_object_or_404(Product, slug=slug, is_visible=True)
    try:
        telegram_id_from_request(request)
    except TelegramAuthError:
        return HttpResponseForbidden("Проверка Telegram не пройдена")
    form = QuestionForm(request.POST)
    if not form.is_valid():
        return page(request, "product", product_context(item, question_form=form), 400)
    entry = form.save(commit=False)
    entry.product = item
    entry.save()
    return redirect(reverse("product", args=[slug]) + "?sent=question#questions")


@require_POST
def favorite_toggle(request, slug):
    item = get_object_or_404(Product, slug=slug, is_visible=True)
    try:
        identity = owner(request)
    except TelegramAuthError:
        return HttpResponseForbidden("Проверка Telegram не пройдена")
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
        identity = owner(request)
    except TelegramAuthError:
        return HttpResponseForbidden("Проверка Telegram не пройдена")
    return JsonResponse({"favorite": Favorite.objects.filter(product=item, **identity).exists()})


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


def checkout(request):
    if request.method == "GET":
        yoo_enabled = bool(settings.YOOKASSA_SHOP_ID and settings.YOOKASSA_SECRET_KEY)
        return page(request, "checkout", {"form": CheckoutForm(initial={"checkout_key": uuid.uuid4(), "delivery": "pickup", "payment_method": "online" if settings.DEMO_PAYMENTS or yoo_enabled else "receipt", "payment_gateway": "demo" if settings.DEMO_PAYMENTS else "yookassa"}), "demo_enabled": settings.DEMO_PAYMENTS, "yoo_enabled": yoo_enabled})
    form = CheckoutForm(request.POST)
    if not form.is_valid():
        return page(request, "checkout", {"form": form, "demo_enabled": settings.DEMO_PAYMENTS, "yoo_enabled": bool(settings.YOOKASSA_SHOP_ID and settings.YOOKASSA_SECRET_KEY)}, 400)
    try:
        tg_id = telegram_id_from_request(request)
    except TelegramAuthError:
        return HttpResponseForbidden("Проверка Telegram не пройдена")
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
            order = Order.objects.create(checkout_key=key, telegram_id=tg_id, name=data["name"].strip(), phone=data["phone"], delivery=data["delivery"], address=data["address"].strip(), comment=data["comment"].strip(), payment_method=data["payment_method"], payment_gateway=gateway, total=sum((p.price * q for p, q in lines), Decimal(0)), payment_state="receipt" if data["payment_method"] == "receipt" else "pending")
            OrderItem.objects.bulk_create([OrderItem(order=order, product=p, title=p.name, quantity=q, unit_price=p.price) for p, q in lines])
            if order.payment_method == "online":
                PaymentAttempt.objects.create(order=order, gateway=gateway)
            queue_order_created(order)
    except ValueError as exc:
        form.add_error(None, str(exc))
        return page(request, "checkout", {"form": form, "demo_enabled": settings.DEMO_PAYMENTS, "yoo_enabled": bool(settings.YOOKASSA_SHOP_ID and settings.YOOKASSA_SECRET_KEY)}, 400)
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
        tg_id = telegram_id_from_request(request)
    except TelegramAuthError:
        return None, HttpResponseForbidden("Проверка Telegram не пройдена")
    if tg_id is not None and order.telegram_id != tg_id:
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


def account(request):
    return page(request, "account")


def account_data(request):
    try:
        identity = owner(request)
    except TelegramAuthError:
        return HttpResponseForbidden("Проверка Telegram не пройдена")
    if "telegram_id" in identity:
        orders = Order.objects.filter(telegram_id=identity["telegram_id"])
        wishes = Wish.objects.filter(telegram_id=identity["telegram_id"])
    else:
        tokens = request.session.get("browser_order_tokens", []) + list(request.session.get("checkout_tokens", {}).values())
        orders = Order.objects.filter(access_token__in=tokens, telegram_id__isnull=True)
        wishes = Wish.objects.filter(browser_session=identity["browser_session"])
    def item_data(item):
        return {"name": item.title, "quantity": item.quantity, "id": item.pk}
    def order_data(order):
        return {"id": order.pk, "url": reverse("order", args=[order.access_token]), "status": order.get_status_display(), "status_code": order.status, "total": str(order.total), "created": order.created_at.strftime("%d.%m.%Y"), "items": [item_data(item) for item in order.items.all()]}
    orders = list(orders.prefetch_related("items"))
    favorites = Favorite.objects.filter(**identity, product__is_visible=True).select_related("product").order_by("-created_at")
    return JsonResponse({
        "current": [order_data(o) for o in orders if o.status not in (Order.Status.DELIVERED, Order.Status.CANCELLED)],
        "purchases": [order_data(o) for o in orders if o.status == Order.Status.DELIVERED],
        "favorites": [{"name": f.product.name, "url": reverse("product", args=[f.product.slug]), "price": str(f.product.price)} for f in favorites],
        "wishes": [{"name": w.product.name, "url": reverse("product", args=[w.product.slug]), "created": w.created_at.strftime("%d.%m.%Y")} for w in wishes.select_related("product").order_by("-created_at")],
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
