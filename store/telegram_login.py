"""Telegram OIDC authorization-code login with PKCE and signed ID-token validation."""

import base64
import hashlib
import hmac
import secrets
import time
from urllib.parse import urlencode

import jwt
import requests
from django.conf import settings
from django.urls import reverse

from .security import TelegramAuthError


AUTH_URL = "https://oauth.telegram.org/auth"
TOKEN_URL = "https://oauth.telegram.org/token"
JWKS_URL = "https://oauth.telegram.org/.well-known/jwks.json"


def configured():
    return bool(settings.TELEGRAM_LOGIN_CLIENT_ID and settings.TELEGRAM_LOGIN_CLIENT_SECRET)


def redirect_uri(request):
    return request.build_absolute_uri(reverse("telegram_login_callback"))


def start_login(request, next_path="/account/"):
    if not configured():
        raise TelegramAuthError("Вход через Telegram ещё не настроен")
    if not request.is_secure() and not settings.DEBUG:
        raise TelegramAuthError("Для входа нужен HTTPS")
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    nonce = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    request.session["telegram_oidc_flow"] = {"state": state, "verifier": verifier, "nonce": nonce, "created": int(time.time()), "next": next_path}
    params = {"client_id": settings.TELEGRAM_LOGIN_CLIENT_ID, "redirect_uri": redirect_uri(request), "response_type": "code", "scope": "openid profile", "state": state, "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256"}
    return f"{AUTH_URL}?{urlencode(params)}"


def complete_login(request):
    flow = request.session.pop("telegram_oidc_flow", None)
    state = request.GET.get("state", "")
    code = request.GET.get("code", "")
    if (not flow or not state or not code or len(code) > 2048
            or not hmac.compare_digest(state, flow["state"])
            or int(time.time()) - flow["created"] > 600):
        raise TelegramAuthError("Вход Telegram не подтверждён")
    try:
        response = requests.post(TOKEN_URL, data={"grant_type": "authorization_code", "code": code,
            "redirect_uri": redirect_uri(request), "client_id": settings.TELEGRAM_LOGIN_CLIENT_ID,
            "code_verifier": flow["verifier"]},
            auth=(settings.TELEGRAM_LOGIN_CLIENT_ID, settings.TELEGRAM_LOGIN_CLIENT_SECRET), timeout=12)
        response.raise_for_status()
        token = response.json()["id_token"]
        signing_key = jwt.PyJWKClient(JWKS_URL).get_signing_key_from_jwt(token)
        claims = jwt.decode(token, signing_key.key, algorithms=["RS256"],
            audience=settings.TELEGRAM_LOGIN_CLIENT_ID, issuer="https://oauth.telegram.org",
            options={"require": ["iss", "aud", "sub", "exp", "iat", "nonce"]})
    except (requests.RequestException, KeyError, TypeError, ValueError, jwt.PyJWTError) as exc:
        raise TelegramAuthError("Не удалось проверить вход Telegram") from exc
    try:
        telegram_id = int(claims["id"])
    except (KeyError, ValueError, TypeError) as exc:
        raise TelegramAuthError("Telegram не вернул ID пользователя") from exc
    if telegram_id <= 0 or not str(claims["sub"]) or not hmac.compare_digest(str(claims["nonce"]), flow["nonce"]):
        raise TelegramAuthError("Данные входа Telegram не совпадают")
    name = str(claims.get("name") or claims.get("given_name") or "Покупатель Telegram").strip()[:80]
    return {"id": telegram_id, "name": name}, flow["next"]
