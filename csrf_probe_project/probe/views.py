from django.http import HttpResponse


def login_view(request):
    # CsrfViewMiddleware (in MIDDLEWARE) has already decided allow/deny
    # before this runs. A 200 here means the request passed every CSRF check.
    return HttpResponse("login-ok", status=200)
