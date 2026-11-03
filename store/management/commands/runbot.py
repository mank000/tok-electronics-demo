import time
import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from store.models import BotUser
from store.services import flush_notifications


class Command(BaseCommand):
    help = "Long polling Telegram-бота и доставка очереди уведомлений"

    def handle(self, *args, **options):
        if not settings.BOT_TOKEN:
            raise CommandError("BOT_TOKEN не задан")
        if not ((settings.BOT_USERNAME and settings.BOT_APP_SHORT_NAME) or settings.PUBLIC_BASE_URL):
            raise CommandError("Задайте BOT_USERNAME и BOT_APP_SHORT_NAME или PUBLIC_BASE_URL")
        api = f"https://api.telegram.org/bot{settings.BOT_TOKEN}/"
        offset = None
        self.stdout.write("Бот запущен")
        while True:
            try:
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
                        BotUser.objects.update_or_create(telegram_id=sender_id, defaults={"chat_id": chat["id"], "phone": contact.get("phone_number", "")[:32]})
                    if text.startswith("/chatid"):
                        answer = requests.post(api + "sendMessage", json={"chat_id": chat["id"], "text": f"ID этого чата: {chat['id']}"}, timeout=12)
                        if answer.status_code != 200:
                            raise RuntimeError(f"Telegram HTTP {answer.status_code}")
                    if text.startswith("/start") and chat.get("type") == "private":
                        user_id = sender_id
                        if not user_id:
                            continue
                        BotUser.objects.update_or_create(telegram_id=user_id, defaults={"chat_id": chat["id"]})
                        if settings.BOT_USERNAME and settings.BOT_APP_SHORT_NAME:
                            url = f"https://t.me/{settings.BOT_USERNAME}/{settings.BOT_APP_SHORT_NAME}"
                        else:
                            url = settings.PUBLIC_BASE_URL
                        answer = requests.post(api + "sendMessage", json={"chat_id": chat["id"], "text": "Привет! Это ток. — учебный магазин электроники. Открой каталог и выбери устройство.", "reply_markup": {"inline_keyboard": [[{"text": "Открыть каталог", "url": url}]]}}, timeout=12)
                        if answer.status_code != 200:
                            raise RuntimeError(f"Telegram HTTP {answer.status_code}")
            except KeyboardInterrupt:
                break
            except Exception as exc:
                self.stderr.write(f"Ошибка бота: {str(exc).replace(settings.BOT_TOKEN, '[redacted]') if settings.BOT_TOKEN else exc}")
                time.sleep(5)
