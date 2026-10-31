import json
import re
import uuid
from django import forms
from .models import Order


class CheckoutForm(forms.Form):
    name = forms.CharField(max_length=100, label="Имя")
    phone = forms.CharField(max_length=32, label="Телефон")
    delivery = forms.ChoiceField(choices=Order.Delivery.choices, label="Получение")
    address = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}), label="Адрес")
    comment = forms.CharField(required=False, max_length=1000, widget=forms.Textarea(attrs={"rows": 2}), label="Комментарий")
    payment_method = forms.ChoiceField(choices=Order.Payment.choices, label="Оплата")
    consent = forms.BooleanField(label="Согласие на обработку данных и условия оферты")
    cart = forms.CharField(widget=forms.HiddenInput)
    checkout_key = forms.UUIDField(widget=forms.HiddenInput)

    def clean_phone(self):
        phone = self.cleaned_data["phone"].strip()
        if not re.fullmatch(r"[+\d ()-]{7,32}", phone) or len(re.sub(r"\D", "", phone)) < 7:
            raise forms.ValidationError("Введите телефон с кодом города или страны")
        return phone

    def clean_cart(self):
        try:
            return json.loads(self.cleaned_data["cart"])
        except (ValueError, TypeError) as exc:
            raise forms.ValidationError("Корзина повреждена") from exc

    def clean(self):
        data = super().clean()
        if data.get("delivery") != Order.Delivery.PICKUP and not data.get("address", "").strip():
            self.add_error("address", "Укажите адрес доставки")
        if data.get("delivery") == Order.Delivery.REGION and data.get("payment_method") == Order.Payment.ON_RECEIPT:
            self.add_error("payment_method", "Для отправки в регионы доступна только онлайн-оплата")
        return data


class WishForm(forms.Form):
    name = forms.CharField(max_length=100, label="Имя")
    contact = forms.CharField(max_length=120, label="Телефон или Telegram")
