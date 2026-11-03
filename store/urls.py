from django.urls import path
from . import views

urlpatterns = [
    path("", views.catalog, name="catalog"),
    path("product/<slug:slug>/", views.product, name="product"),
    path("product/<slug:slug>/wish/", views.wish, name="wish"),
    path("basket/", views.basket, name="basket"),
    path("api/cart/quote/", views.cart_quote, name="cart_quote"),
    path("api/me/contact/", views.my_contact, name="my_contact"),
    path("checkout/", views.checkout, name="checkout"),
    path("order/<str:token>/", views.order_page, name="order"),
    path("pay/<str:token>/", views.demo_pay, name="demo_pay"),
    path("api/payment/webhook/", views.payment_webhook, name="payment_webhook"),
    path("documents/<slug:kind>/", views.document, name="document"),
]
