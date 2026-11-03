from django.conf import settings


class LocalAdminNullOriginMiddleware:
    """Allow opaque-origin desktop webviews to submit local demo admin forms.

    Django's normal CSRF token and cookie checks still run after this middleware.
    HTTPS, non-local hosts, and non-admin forms keep the origin check.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (
            settings.DEBUG
            and request.method not in ("GET", "HEAD", "OPTIONS", "TRACE")
            and request.path.startswith("/admin/")
            and request.META.get("HTTP_ORIGIN") == "null"
            and not request.is_secure()
            and request.get_host().partition(":")[0] in ("localhost", "127.0.0.1")
        ):
            request.META.pop("HTTP_ORIGIN")
        return self.get_response(request)
