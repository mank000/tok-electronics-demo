import json
import uuid
from decimal import Decimal
from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST
from .forms import CheckoutForm, WishForm
from .models import BotUser, Category, Order, OrderItem, PaymentAttempt, Product, Wish
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
    return page(request, "catalog", {"categories": Category.objects.all(), "products": products, "active_category": category, "query": search, "sort": sort, "featured": Product.objects.filter(is_visible=True, is_featured=True).prefetch_related("images")[:4]})


def product(request, slug):
    item = get_object_or_404(Product.objects.prefetch_related("images"), slug=slug, is_visible=True)
    return page(request, "product", {"product": item, "form": WishForm()})


@require_POST
def wish(request, slug):
    item = get_object_or_404(Product, slug=slug, is_visible=True, availability=Product.Availability.PREORDER)
    form = WishForm(request.POST)
    if not form.is_valid():
        return page(request, "product", {"product": item, "form": form}, 400)
    try:
        tg_id = telegram_id_from_request(request)
    except TelegramAuthError:
        return HttpResponseForbidden("Проверка Telegram не пройдена")
    obj = Wish.objects.create(product=item, telegram_id=tg_id, **form.cleaned_data)
    queue_shop(f"Заявка «Хочу такой» №{obj.pk}: {item.name}, {obj.name}, {obj.contact}.")
    return page(request, "wish_done", {"product": item})


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
        return page(request, "checkout", {"form": CheckoutForm(initial={"checkout_key": uuid.uuid4(), "delivery": "pickup", "payment_method": "online"})})
    form = CheckoutForm(request.POST)
    if not form.is_valid():
        return page(request, "checkout", {"form": form}, 400)
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
            order = Order.objects.create(checkout_key=key, telegram_id=tg_id, name=data["name"].strip(), phone=data["phone"], delivery=data["delivery"], address=data["address"].strip(), comment=data["comment"].strip(), payment_method=data["payment_method"], total=sum((p.price * q for p, q in lines), Decimal(0)), payment_state="receipt" if data["payment_method"] == "receipt" else "pending")
            OrderItem.objects.bulk_create([OrderItem(order=order, product=p, title=p.name, quantity=q, unit_price=p.price) for p, q in lines])
            if order.payment_method == "online":
                PaymentAttempt.objects.create(order=order)
            queue_order_created(order)
    except ValueError as exc:
        form.add_error(None, str(exc))
        return page(request, "checkout", {"form": form}, 400)
    except IntegrityError:
        return HttpResponse("Повторное оформление уже обработано", status=409)
    known[str(key)] = order.access_token
    request.session["checkout_tokens"] = known
    return redirect(reverse("order", args=[order.access_token]) + "?created=1")


def owned_order(request, token):
    order = get_object_or_404(Order.objects.prefetch_related("items"), access_token=token)
    try:
        tg_id = telegram_id_from_request(request)
    except TelegramAuthError:
        return None, HttpResponseForbidden("Проверка Telegram не пройдена")
    if tg_id is not None and order.telegram_id != tg_id:
        return None, HttpResponseForbidden("Это чужой заказ")
    return order, None


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
    if order.payment_method != "online" or order.payment_state == "paid":
        return redirect("order", token=token)
    if request.method == "POST":
        action = request.POST.get("action")
        if action not in ("paid", "declined"):
            return HttpResponseBadRequest("Неверное действие")
        attempt = order.attempts.order_by("-created_at").first()
        if not attempt or attempt.state != "pending":
            attempt = PaymentAttempt.objects.create(order=order)
        raw = json.dumps({"event_id": str(uuid.uuid4()), "attempt": str(attempt.reference), "status": action}, separators=(",", ":")).encode()
        try:
            process_payment_event(raw, signed_event(raw))
        except PaymentError:
            return HttpResponseBadRequest("Ошибка платежа")
        return redirect("order", token=token)
    return page(request, "payment", {"order": order})


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
