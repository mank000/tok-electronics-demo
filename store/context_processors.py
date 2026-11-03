from .models import ShopSettings


def shop_settings(request):
    return {"shop_settings": ShopSettings.objects.filter(pk=1).first() or ShopSettings()}
