import json
from decimal import Decimal, InvalidOperation


def price_bound(value):
    value = value.strip()[:20]
    if not value:
        return None
    try:
        amount = Decimal(value.replace(",", "."))
    except InvalidOperation as exc:
        raise ValueError("Укажите цену числом") from exc
    if not amount.is_finite() or amount < 0 or amount > Decimal("9999999999"):
        raise ValueError("Цена вне допустимого диапазона")
    return amount


def facet_groups(products):
    facets = {}
    for product in products:
        if not isinstance(product.specs, dict):
            continue
        for key, raw in product.specs.items():
            if not isinstance(key, str) or key.casefold() == "модель" or not isinstance(raw, (str, int, float)) or isinstance(raw, bool):
                continue
            value = str(raw).strip()
            if not value or len(key) > 80 or len(value) > 120:
                continue
            facets.setdefault(key, set()).add(value)
    return [{"name": key, "options": [{"value": value, "token": json.dumps([key, value], ensure_ascii=False)} for value in sorted(values)]}
            for key, values in facets.items() if len(values) > 1][:10]


def selected_facets(tokens, groups):
    allowed = {group["name"]: {option["value"] for option in group["options"]} for group in groups}
    selected = {}
    for token in tokens[:20]:
        try:
            key, value = json.loads(token)
        except (ValueError, TypeError):
            continue
        if isinstance(key, str) and isinstance(value, str) and value in allowed.get(key, set()):
            selected.setdefault(key, set()).add(value)
    return selected


def matches_facets(product, selected):
    specs = product.specs if isinstance(product.specs, dict) else {}
    return all(str(specs.get(key, "")).strip() in values for key, values in selected.items())
