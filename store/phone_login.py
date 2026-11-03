"""One-time browser codes delivered only to a verified Telegram bot chat."""

import hashlib
import hmac
import re
import secrets
from datetime import timedelta

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .integrations import get_runtime_config
from .models import BotUser, BrowserLoginCode, CustomerProfile


def normalize_phone(value):
    value = str(value or "").strip()
    if not re.fullmatch(r"\+?[\d\s()\-]{10,32}", value):
        return ""
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10 and digits.startswith("9"):
        digits = "7" + digits
    return digits if 11 <= len(digits) <= 15 else ""


def code_digest(telegram_id, challenge, code):
    raw = f"{telegram_id}:{challenge}:{code}".encode()
    return hmac.new(settings.SECRET_KEY.encode(), raw, hashlib.sha256).hexdigest()


def issue_code(request, phone, next_path):
    """Return whether Telegram accepted a message; caller gives the same UI reply either way."""
    digits = normalize_phone(phone)
    if not digits:
        return False
    matches = list(BotUser.objects.filter(phone_digits=digits).exclude(phone="")[:2])
    if len(matches) != 1 or not CustomerProfile.objects.filter(telegram_id=matches[0].telegram_id).exists():
        return False
    user = matches[0]
    config = get_runtime_config()
    if not config.bot_token:
        return False
    now = timezone.now()
    with transaction.atomic():
        BrowserLoginCode.objects.get_or_create(telegram_id=user.telegram_id, defaults={
            "code_hash": "", "sent_at": now - timedelta(days=1), "expires_at": now,
            "window_started_at": now - timedelta(days=1),
        })
        entry = BrowserLoginCode.objects.select_for_update().get(telegram_id=user.telegram_id)
        if now - entry.sent_at < timedelta(seconds=60):
            return False
        if now - entry.window_started_at >= timedelta(hours=1):
            entry.window_started_at = now
            entry.sends_in_window = 0
        if entry.sends_in_window >= 5:
            return False
        challenge = secrets.token_urlsafe(24)
        code = f"{secrets.randbelow(1_000_000):06d}"
        entry.code_hash = code_digest(user.telegram_id, challenge, code)
        entry.sent_at = now
        entry.expires_at = now + timedelta(minutes=5)
        entry.attempts = 0
        entry.sends_in_window += 1
        entry.save()
    request.session["phone_login"] = {"telegram_id": user.telegram_id, "challenge": challenge, "next": next_path}
    try:
        response = requests.post(f"https://api.telegram.org/bot{config.bot_token}/sendMessage", json={
            "chat_id": user.chat_id,
            "text": f"Код входа в магазин: {code}. Он действует 5 минут. Никому его не сообщайте.",
            "protect_content": True,
        }, timeout=12)
        response.raise_for_status()
        if not response.json().get("ok"):
            raise ValueError("Telegram не доставил код")
    except (requests.RequestException, ValueError):
        BrowserLoginCode.objects.filter(telegram_id=user.telegram_id, code_hash=entry.code_hash).update(code_hash="")
        request.session.pop("phone_login", None)
        return False
    return True


def verify_code(request, submitted):
    flow = request.session.get("phone_login")
    if not flow or not re.fullmatch(r"\d{6}", str(submitted or "")):
        return None
    now = timezone.now()
    with transaction.atomic():
        entry = BrowserLoginCode.objects.select_for_update().filter(telegram_id=flow["telegram_id"]).first()
        if not entry or not entry.code_hash or entry.expires_at <= now or entry.attempts >= 5:
            request.session.pop("phone_login", None)
            return None
        entry.attempts += 1
        valid = hmac.compare_digest(entry.code_hash, code_digest(flow["telegram_id"], flow["challenge"], submitted))
        if valid:
            entry.code_hash = ""
        entry.save(update_fields=["attempts", "code_hash"])
    if not valid:
        if entry.attempts >= 5:
            request.session.pop("phone_login", None)
        return None
    request.session.pop("phone_login", None)
    profile = CustomerProfile.objects.filter(telegram_id=flow["telegram_id"]).first()
    if not profile:
        return None
    return {"id": profile.telegram_id, "name": profile.name}, flow["next"]
