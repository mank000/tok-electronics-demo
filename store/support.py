from django.db import transaction

from .models import SupportMessage, SupportTicket
from .services import queue_shop, queue_telegram


def active_ticket(telegram_id):
    return SupportTicket.objects.filter(telegram_id=telegram_id).exclude(status=SupportTicket.Status.CLOSED).first()


def open_ticket(telegram_id):
    return active_ticket(telegram_id) or SupportTicket.objects.create(telegram_id=telegram_id)


@transaction.atomic
def receive_message(telegram_id, message_id, text):
    text = text.strip()
    if not text or message_id is None:
        return None
    if SupportMessage.objects.filter(ticket__telegram_id=telegram_id, telegram_message_id=message_id).exists():
        return None
    ticket = active_ticket(telegram_id)
    if not ticket:
        return None
    SupportMessage.objects.create(
        ticket=ticket, sender=SupportMessage.Sender.BUYER,
        text=text[:2000], telegram_message_id=message_id,
    )
    ticket.status = SupportTicket.Status.OPEN
    ticket.save(update_fields=["status", "updated_at"])
    queue_shop(f"Новое сообщение в обращении поддержки №{ticket.pk}. Откройте раздел «Поддержка» в админке.")
    return ticket


@transaction.atomic
def reply_to_ticket(ticket, text):
    text = text.strip()
    if not text:
        return
    SupportMessage.objects.create(ticket=ticket, sender=SupportMessage.Sender.STAFF, text=text)
    if ticket.status != SupportTicket.Status.CLOSED:
        ticket.status = SupportTicket.Status.ANSWERED
    ticket.save(update_fields=["status", "updated_at"])
    queue_telegram(ticket.telegram_id, f"Ответ поддержки по обращению №{ticket.pk}:\n{text}")
