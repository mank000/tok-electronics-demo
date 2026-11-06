import time
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from store.services import flush_notifications


class Command(BaseCommand):
    help = "Независимая доставка уведомлений из очереди"

    def handle(self, *args, **options):
        while True:
            try:
                close_old_connections()
                flush_notifications()
            except KeyboardInterrupt:
                return
            except Exception as exc:
                self.stderr.write(f"Ошибка очереди: {type(exc).__name__}")
            time.sleep(1)
