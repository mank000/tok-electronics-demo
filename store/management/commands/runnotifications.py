import time
from time import monotonic
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from store.reservations import expire_reservations
from store.services import flush_notifications


class Command(BaseCommand):
    help = "Независимая доставка уведомлений из очереди"

    def handle(self, *args, **options):
        next_expiry = 0
        while True:
            try:
                close_old_connections()
                flush_notifications()
                if monotonic() >= next_expiry:
                    next_expiry = monotonic() + 30
                    expire_reservations()
            except KeyboardInterrupt:
                return
            except Exception as exc:
                self.stderr.write(f"Ошибка очереди: {type(exc).__name__}")
            time.sleep(1)
