import json
import re
import uuid
from django import forms
from django.conf import settings
from .integrations import yookassa_configured
from .models import Order, ProductQuestion, ProductReview, ReturnRequest


class CheckoutForm(forms.Form):
    name = forms.CharField(max_length=100, label="Имя", required=False)
    phone = forms.CharField(max_length=32, label="Телефон")
    delivery = forms.ChoiceField(choices=Order.Delivery.choices, label="Получение")
    address = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}), label="Адрес")
    comment = forms.CharField(required=False, max_length=1000, widget=forms.Textarea(attrs={"rows": 2}), label="Комментарий")
    payment_method = forms.ChoiceField(choices=Order.Payment.choices, label="Оплата")
    payment_gateway = forms.ChoiceField(choices=[("demo", "Демооплата"), ("yookassa", "ЮKassa (тестовый магазин)")], label="Платёжный шлюз", required=False)
    coupon_code = forms.CharField(max_length=32, required=False, label="Промокод")
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

    def clean_coupon_code(self):
        return self.cleaned_data["coupon_code"].strip().upper()

    def clean(self):
        data = super().clean()
        data["payment_gateway"] = data.get("payment_gateway") or "demo"
        if data.get("delivery") != Order.Delivery.PICKUP and not data.get("address", "").strip():
            self.add_error("address", "Укажите адрес доставки")
        if data.get("delivery") == Order.Delivery.REGION and data.get("payment_method") == Order.Payment.ON_RECEIPT:
            self.add_error("payment_method", "Для отправки в регионы доступна только онлайн-оплата")
        if data.get("payment_method") == Order.Payment.ONLINE:
            if data.get("payment_gateway") == "demo" and not settings.DEMO_PAYMENTS:
                self.add_error("payment_gateway", "Демооплата выключена")
            if data.get("payment_gateway") == "yookassa":
                if not yookassa_configured():
                    self.add_error("payment_gateway", "ЮKassa ещё не настроена")
        return data


class ReviewForm(forms.ModelForm):
    class Meta:
        model = ProductReview
        fields = ("rating", "text")
        widgets = {"text": forms.Textarea(attrs={"rows": 4})}


class QuestionForm(forms.ModelForm):
    class Meta:
        model = ProductQuestion
        fields = ("text",)
        widgets = {"text": forms.Textarea(attrs={"rows": 3})}


class ReturnForm(forms.ModelForm):
    class Meta:
        model = ReturnRequest
        fields = ("quantity", "reason", "comment")
        widgets = {"comment": forms.Textarea(attrs={"rows": 3})}
