"""Runtime configuration shared by web requests and the polling bot."""

import base64
import hashlib
from dataclasses import dataclass

from cryptography.fernet import Fernet
from django.conf import settings

from .models import IntegrationSettings


def secret_cipher():
    key = base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode()).digest())
    return Fernet(key)


def encrypt_secret(value):
    return secret_cipher().encrypt(value.encode()).decode()


def decrypt_secret(value):
    return secret_cipher().decrypt(value.encode()).decode() if value else ""


@dataclass(frozen=True)
class RuntimeConfig:
    bot_token: str
    bot_username: str
    bot_app_short_name: str
    public_base_url: str
    shop_chat_id: str
    payment_webhook_secret: str


def get_runtime_config():
    """Admin values override .env; blank admin fields retain the .env fallback."""
    saved = IntegrationSettings.objects.filter(pk=1).first()
    return RuntimeConfig(
        bot_token=decrypt_secret(saved.bot_token_ciphertext) if saved and saved.bot_token_ciphertext else settings.BOT_TOKEN,
        bot_username=(saved.bot_username if saved else "") or settings.BOT_USERNAME,
        bot_app_short_name=(saved.bot_app_short_name if saved else "") or settings.BOT_APP_SHORT_NAME,
        public_base_url=(saved.public_base_url if saved else "") or settings.PUBLIC_BASE_URL,
        shop_chat_id=(saved.shop_chat_id if saved else "") or settings.SHOP_CHAT_ID,
        payment_webhook_secret=decrypt_secret(saved.payment_secret_ciphertext) if saved and saved.payment_secret_ciphertext else settings.PAYMENT_WEBHOOK_SECRET,
    )
