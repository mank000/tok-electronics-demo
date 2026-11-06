from django.db import migrations
from django.db.models import F


def backfill(apps, schema_editor):
    apps.get_model("store", "Order").objects.update(subtotal=F("total"))
    apps.get_model("store", "OrderItem").objects.update(list_unit_price=F("unit_price"))


class Migration(migrations.Migration):
    dependencies = [("store", "0015_discounts_coupons")]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
