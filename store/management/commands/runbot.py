import time
import requests
from django.core.management.base import BaseCommand
from store.integrations import get_runtime_config
from store.models import BotUser, ShopSettings
from store.phone_login import normalize_phone
from store.services import flush_notifications, retry_buyer_notifications


class Command(BaseCommand):
    help = "Long polling Telegram-бота и доставка очереди уведомлений"

    def handle(self, *args, **options):
        offset = None
        active_token = ""
        waiting_for = ""
        while True:
            try:
                config = get_runtime_config()
                if not config.bot_token or not ((config.bot_username and config.bot_app_short_name) or config.public_base_url):
                    reason = "BOT_TOKEN" if not config.bot_token else "BOT_USERNAME и BOT_APP_SHORT_NAME или PUBLIC_BASE_URL"
                    if reason != waiting_for:
                        self.stdout.write(f"Бот ожидает настройки: {reason}")
                        waiting_for = reason
                    time.sleep(5)
                    continue
                waiting_for = ""
                if config.bot_token != active_token:
                    offset = None
                    active_token = config.bot_token
                    self.stdout.write("Бот запущен")
                api = f"https://api.telegram.org/bot{config.bot_token}/"
                flush_notifications()
                result = requests.get(api + "getUpdates", params={"timeout": 20, "offset": offset, "allowed_updates": '["message"]'}, timeout=30).json()
                if not result.get("ok"):
                    raise RuntimeError(result.get("description", "Telegram API error"))
                for update in result["result"]:
                    offset = update["update_id"] + 1
                    message = update.get("message", {})
                    chat = message.get("chat", {})
                    text = message.get("text", "")
                    contact = message.get("contact")
                    sender_id = message.get("from", {}).get("id")
                    if contact and chat.get("type") == "private" and contact.get("user_id") == sender_id:
                        phone = contact.get("phone_number", "")[:32]
                        digits = normalize_phone(phone)
                        if digits:
                            BotUser.objects.filter(phone_digits=digits).exclude(telegram_id=sender_id).update(phone="", phone_digits="")
                            BotUser.objects.update_or_create(telegram_id=sender_id, defaults={"chat_id": chat["id"], "phone": phone, "phone_digits": digits})
                    if text.startswith("/chatid"):
                        answer = requests.post(api + "sendMessage", json={"chat_id": chat["id"], "text": f"ID этого чата: {chat['id']}"}, timeout=12)
                        if answer.status_code != 200:
                            raise RuntimeError(f"Telegram HTTP {answer.status_code}")
                    if text.startswith("/start") and chat.get("type") == "private":
                        user_id = sender_id
                        if not user_id:
                            continue
                        BotUser.objects.update_or_create(telegram_id=user_id, defaults={"chat_id": chat["id"]})
                        retry_buyer_notifications(user_id)
                        if config.bot_username and config.bot_app_short_name:
                            url = f"https://t.me/{config.bot_username}/{config.bot_app_short_name}"
                        else:
                            url = config.public_base_url
                        shop = ShopSettings.objects.filter(pk=1).first() or ShopSettings()
                        answer = requests.post(api + "sendMessage", json={"chat_id": chat["id"], "text": f"Привет! Это {shop.name} — {shop.subtitle.lower()}. Открой каталог и выбери устройство.", "reply_markup": {"inline_keyboard": [[{"text": "Открыть каталог", "url": url}]]}}, timeout=12)
                        if answer.status_code != 200:
                            raise RuntimeError(f"Telegram HTTP {answer.status_code}")
            except KeyboardInterrupt:
                break
            except Exception as exc:
                self.stderr.write(f"Ошибка бота: {str(exc).replace(active_token, '[redacted]') if active_token else exc}")
                time.sleep(5)
