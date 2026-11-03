import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl
from .integrations import get_runtime_config


class TelegramAuthError(ValueError):
    pass


def verify_init_data(raw, now=None):
    bot_token = get_runtime_config().bot_token
    if not raw or not bot_token or len(raw) > 8192:
        raise TelegramAuthError("Данные Telegram недоступны")
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise TelegramAuthError("Некорректные данные Telegram") from exc
    data = dict(pairs)
    if len(pairs) != len(data) or "hash" not in data or "auth_date" not in data or "user" not in data:
        raise TelegramAuthError("Неполные данные Telegram")
    try:
        stamp = int(data["auth_date"])
        user = json.loads(data["user"])
        user_id = int(user["id"])
        supplied_hash = bytes.fromhex(data["hash"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise TelegramAuthError("Некорректные данные Telegram") from exc
    current = int(now if now is not None else time.time())
    if user_id <= 0 or len(supplied_hash) != 32 or stamp > current + 60 or current - stamp > 86400:
        raise TelegramAuthError("Срок данных Telegram истёк")
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()) if k != "hash")
    key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(key, check.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(supplied_hash, expected):
        raise TelegramAuthError("Подпись Telegram не совпадает")
    return user_id


def verified_init_profile(raw):
    user_id = verify_init_data(raw)
    user = json.loads(dict(parse_qsl(raw, keep_blank_values=True))["user"])
    name = " ".join(str(user.get(field, "")).strip() for field in ("first_name", "last_name")).strip()[:80]
    from .models import CustomerProfile
    saved = CustomerProfile.objects.filter(telegram_id=user_id).only("name").first()
    if saved:
        name = saved.name
    return {"id": user_id, "name": name or "Покупатель Telegram"}


def current_profile(request):
    raw = request.headers.get("X-Telegram-Init-Data") or request.POST.get("tg_init_data", "")
    if raw:
        return verified_init_profile(raw)
    telegram_id = request.session.get("telegram_id")
    if telegram_id:
        return {"id": int(telegram_id), "name": request.session.get("telegram_name", "Покупатель Telegram")}
    return None


def telegram_id_from_request(request):
    raw = request.headers.get("X-Telegram-Init-Data") or request.POST.get("tg_init_data", "")
    if not raw:
        return None
    return verify_init_data(raw)
