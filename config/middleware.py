from django.conf import settings


class DemoNullOriginMiddleware:
    """Allow opaque-origin webviews on explicitly named demo hosts.

    Django's normal CSRF token and cookie checks still run after this middleware.
    HTTPS hosts additionally require an exact trusted origin.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if settings.DEBUG and request.method not in ("GET", "HEAD", "OPTIONS", "TRACE") and request.META.get("HTTP_ORIGIN") == "null":
            host = request.get_host()
            expected_origin = f"{request.scheme}://{host}"
            if host.partition(":")[0] in settings.DEMO_NULL_ORIGIN_HOSTS and (
                not request.is_secure() or expected_origin in settings.CSRF_TRUSTED_ORIGINS
            ):
                request.META["HTTP_ORIGIN"] = expected_origin
        return self.get_response(request)
