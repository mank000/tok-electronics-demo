"""Private customer conversations, available through verified Telegram identity."""

import json
import re
import uuid
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .models import BotUser, ChatConversation, ChatMessage, CustomerProfile, Product
from .security import TelegramAuthError, current_profile
from .services import queue_telegram


TAG_PATTERN = re.compile(r"[a-zа-яё][a-zа-яё0-9_]{2,23}\Z")


def chat_page(request):
    from .views import page
    return page(request, "chats")


def conversation_page(request, conversation_id):
    from .views import page
    return page(request, "chats", {"conversation_id": conversation_id})


def share_page(request, slug):
    from .views import page
    product = get_object_or_404(Product, slug=slug, is_visible=True)
    return page(request, "chats", {"share_product": product})


def identity(request):
    try:
        profile = current_profile(request)
    except TelegramAuthError:
        return None, HttpResponseForbidden("Проверка Telegram не пройдена")
    if not profile:
        return None, JsonResponse({"error": "Войдите через Telegram"}, status=401)
    customer, _ = CustomerProfile.objects.get_or_create(
        telegram_id=profile["id"], defaults={"name": profile["name"]},
    )
    return customer, None


def payload(request):
    if len(request.body) > 5000:
        return None
    try:
        value = json.loads(request.body)
    except (UnicodeDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def peer_of(conversation, customer):
    return conversation.right if conversation.left_id == customer.pk else conversation.left


def block_state(conversation, customer):
    if conversation.left_id == customer.pk:
        return conversation.left_blocked, conversation.right_blocked
    return conversation.right_blocked, conversation.left_blocked


def conversation_for(customer, conversation_id):
    return get_object_or_404(
        ChatConversation.objects.select_related("left", "right").filter(Q(left=customer) | Q(right=customer)),
        pk=conversation_id,
    )


def message_data(message, viewer_id):
    product = message.product if message.product and message.product.is_visible else None
    image = product.first_image if product else None
    return {
        "id": message.pk,
        "mine": message.sender_id == viewer_id,
        "text": message.text,
        "created": timezone.localtime(message.created_at).strftime("%d.%m %H:%M"),
        "product": {
            "name": message.product_name,
            "url": reverse("product", args=[product.slug]) if product else "",
            "image": image.thumb.url if image else "",
            "price": str(product.price) if product else "",
        } if message.product_name else None,
    }


@require_GET
def api_chats(request):
    customer, denied = identity(request)
    if denied is not None:
        return denied
    conversations = ChatConversation.objects.filter(Q(left=customer) | Q(right=customer)).select_related("left", "right")[:100]
    rows = []
    for conversation in conversations:
        peer = peer_of(conversation, customer)
        last = conversation.messages.order_by("-id").first()
        read_id = conversation.left_read_message_id if conversation.left_id == customer.pk else conversation.right_read_message_id
        unread = conversation.messages.filter(sender=peer, id__gt=read_id).count()
        rows.append({
            "id": conversation.pk, "url": reverse("chat_conversation", args=[conversation.pk]),
            "peer": {"name": peer.name, "tag": peer.chat_tag or ""},
            "preview": (last.text or last.product_name)[:100] if last else "Начните переписку",
            "unread": unread,
        })
    return JsonResponse({"name": customer.name, "tag": customer.chat_tag or "", "conversations": rows})


@require_POST
def api_chat_tag(request):
    customer, denied = identity(request)
    if denied is not None:
        return denied
    data = payload(request)
    if data is None or not isinstance(data.get("tag"), str):
        return JsonResponse({"error": "Укажите тег"}, status=400)
    tag = data["tag"].strip().lstrip("@").lower()
    if not TAG_PATTERN.fullmatch(tag):
        return JsonResponse({"error": "Тег: 3–24 символа, буквы, цифры и подчёркивание; первый символ — буква"}, status=400)
    customer.chat_tag = tag
    try:
        with transaction.atomic():
            customer.save(update_fields=["chat_tag"])
    except IntegrityError:
        return JsonResponse({"error": "Этот тег уже занят"}, status=409)
    return JsonResponse({"tag": tag})


@require_POST
def api_chat_start(request):
    customer, denied = identity(request)
    if denied is not None:
        return denied
    data = payload(request)
    if data is None or not isinstance(data.get("tag"), str):
        return JsonResponse({"error": "Укажите точный тег"}, status=400)
    tag = data["tag"].strip().lstrip("@").lower()
    if not TAG_PATTERN.fullmatch(tag):
        return JsonResponse({"error": "Проверьте тег"}, status=400)
    peer = CustomerProfile.objects.filter(chat_tag=tag).first()
    if not peer:
        return JsonResponse({"error": "Пользователь с таким тегом не найден"}, status=404)
    if peer.pk == customer.pk:
        return JsonResponse({"error": "Нельзя написать самому себе"}, status=400)
    left, right = sorted((customer, peer), key=lambda person: person.pk)
    conversation, _ = ChatConversation.objects.get_or_create(left=left, right=right)
    return JsonResponse({"id": conversation.pk, "url": reverse("chat_conversation", args=[conversation.pk]), "peer": peer.name})


def message_rows(conversation, customer, request):
    before, after = request.GET.get("before"), request.GET.get("after")
    if before and after:
        return None
    try:
        cursor = int(before or after) if before or after else None
    except ValueError:
        return None
    if cursor is not None and cursor < 1:
        return None
    query = conversation.messages.select_related("sender", "product").prefetch_related("product__images")
    if before:
        query = query.filter(pk__lt=cursor).order_by("-id")
        selected = list(query[:51])
        return list(reversed(selected[:50])), len(selected) > 50, "before"
    if after:
        query = query.filter(pk__gt=cursor).order_by("id")
        selected = list(query[:101])
        return selected[:100], len(selected) > 100, "after"
    selected = list(query.order_by("-id")[:51])
    return list(reversed(selected[:50])), len(selected) > 50, "latest"


@require_GET
def api_chat_detail(request, conversation_id):
    customer, denied = identity(request)
    if denied is not None:
        return denied
    conversation = conversation_for(customer, conversation_id)
    result = message_rows(conversation, customer, request)
    if result is None:
        return JsonResponse({"error": "Некорректная страница сообщений"}, status=400)
    messages, has_more, direction = result
    if direction != "before" and messages:
        field = "left_read_message_id" if conversation.left_id == customer.pk else "right_read_message_id"
        ChatConversation.objects.filter(pk=conversation.pk, **{f"{field}__lt": messages[-1].pk}).update(**{field: messages[-1].pk})
    peer = peer_of(conversation, customer)
    blocked_by_me, blocked_by_peer = block_state(conversation, customer)
    return JsonResponse({
        "peer": {"name": peer.name, "tag": peer.chat_tag or ""},
        "blocked_by_me": blocked_by_me, "blocked_by_peer": blocked_by_peer,
        "messages": [message_data(message, customer.pk) for message in messages],
        "has_more": has_more,
    })


@require_POST
def api_chat_block(request, conversation_id):
    customer, denied = identity(request)
    if denied is not None:
        return denied
    data = payload(request)
    if data is None or not isinstance(data.get("blocked"), bool):
        return JsonResponse({"error": "Укажите состояние блокировки"}, status=400)
    conversation = conversation_for(customer, conversation_id)
    field = "left_blocked" if conversation.left_id == customer.pk else "right_blocked"
    setattr(conversation, field, data["blocked"])
    conversation.save(update_fields=[field])
    return JsonResponse({"blocked_by_me": data["blocked"]})


@require_POST
def api_chat_send(request, conversation_id):
    customer, denied = identity(request)
    if denied is not None:
        return denied
    data = payload(request)
    if data is None or not isinstance(data.get("text", ""), str):
        return JsonResponse({"error": "Некорректное сообщение"}, status=400)
    text = data.get("text", "").strip()
    if len(text) > 1000:
        return JsonResponse({"error": "Сообщение не должно превышать 1000 символов"}, status=400)
    product_id = data.get("product_id")
    if product_id is not None and (isinstance(product_id, bool) or not str(product_id).isdigit()):
        return JsonResponse({"error": "Товар не найден"}, status=400)
    product = Product.objects.filter(pk=product_id, is_visible=True).first() if product_id is not None else None
    if product_id is not None and not product:
        return JsonResponse({"error": "Товар недоступен"}, status=404)
    if not text and not product:
        return JsonResponse({"error": "Напишите сообщение или выберите товар"}, status=400)
    try:
        nonce = uuid.UUID(data["nonce"]) if isinstance(data.get("nonce"), str) else None
    except ValueError:
        return JsonResponse({"error": "Некорректный идентификатор сообщения"}, status=400)
    conversation = conversation_for(customer, conversation_id)
    with transaction.atomic():
        conversation = ChatConversation.objects.select_for_update().select_related("left", "right").get(pk=conversation.pk)
        if any(block_state(conversation, customer)):
            return JsonResponse({"error": "В этом чате отправка сообщений закрыта"}, status=403)
        if nonce:
            previous = ChatMessage.objects.filter(client_nonce=nonce).first()
            if previous:
                if previous.conversation_id != conversation.pk or previous.sender_id != customer.pk:
                    return JsonResponse({"error": "Идентификатор уже использован"}, status=409)
                return JsonResponse({"message": message_data(previous, customer.pk), "duplicate": True})
        if ChatMessage.objects.filter(sender=customer, created_at__gte=timezone.now() - timedelta(minutes=1)).count() >= 20:
            return JsonResponse({"error": "Слишком много сообщений. Попробуйте через минуту"}, status=429)
        peer = peer_of(conversation, customer)
        read_id = conversation.right_read_message_id if conversation.left_id == customer.pk else conversation.left_read_message_id
        already_unread = conversation.messages.filter(sender=customer, id__gt=read_id).exists()
        message = ChatMessage.objects.create(
            conversation=conversation, sender=customer, text=text,
            product=product, product_name=product.name if product else "", client_nonce=nonce,
        )
        conversation.save(update_fields=["updated_at"])
        if not already_unread and BotUser.objects.filter(telegram_id=peer.telegram_id).exists():
            queue_telegram(peer.telegram_id, f"Новое сообщение от {customer.name} в магазине. Откройте раздел «Чаты».")
    return JsonResponse({"message": message_data(message, customer.pk)}, status=201)
