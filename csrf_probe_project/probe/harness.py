"""
Diagnostic harness that walks a *real* request through the *real*
CsrfViewMiddleware and records every branch of the decision path.

No framework code is patched. We only subclass the middleware to observe its
own private helpers and to capture the exact reason string passed to
_reject(); the request objects are produced by Django's own test factories
(WSGIRequest via RequestFactory, ASGIRequest via AsyncRequestFactory).
"""

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import DisallowedHost
from django.middleware.csrf import CsrfViewMiddleware
from django.test import AsyncRequestFactory, RequestFactory
from django.test.client import FakePayload
from django.utils.http import is_same_domain

# One fixed, legal CSRF pair reused by every scenario. The cookie holds the
# 32-char secret; the form holds a masked token derived from that secret.
from django.middleware.csrf import _get_new_csrf_string, _mask_cipher_secret

CSRF_SECRET = _get_new_csrf_string()
CSRF_FORM_TOKEN = _mask_cipher_secret(CSRF_SECRET)
CSRF_COOKIE_NAME = "csrftoken"
LOGIN_PATH = "/login/"


@dataclass
class Trace:
    """The observed decision path for one request."""

    entry: str
    method: str = "POST"
    secure: bool = None
    scheme: str = ""
    host: str = ""
    host_ok: bool = None
    raw_host: str = ""
    origin_header: str = ""
    referer_header: str = ""
    origin_verified: object = None
    origin_branch: str = ""
    referer_outcome: str = ""
    trusted_origins: list = field(default_factory=list)
    rejection_reason: str = ""
    status_code: int = None

    @property
    def accepted(self):
        return self.rejection_reason == ""


class ProbeCsrfMiddleware(CsrfViewMiddleware):
    """CsrfViewMiddleware that records, rather than hides, its decisions."""

    def __init__(self, trace):
        super().__init__(lambda req: None)
        self.trace = trace

    def _origin_verified(self, request):
        trace = self.trace
        trace.origin_header = request.META.get("HTTP_ORIGIN", "")
        result = super()._origin_verified(request)
        trace.origin_verified = result
        trace.trusted_origins = list(settings.CSRF_TRUSTED_ORIGINS)
        # Re-run the exact sub-steps so the report shows *which* rule hit.
        request_origin = request.META["HTTP_ORIGIN"]
        try:
            good_host = request.get_host()
        except DisallowedHost:
            trace.origin_branch = (
                "get_host() raised DisallowedHost -> exact-host rule skipped"
            )
        else:
            good_origin = "%s://%s" % (
                "https" if request.is_secure() else "http",
                good_host,
            )
            if request_origin == good_origin:
                trace.origin_branch = (
                    f"exact self-origin match ({request_origin} == {good_origin})"
                )
        if not trace.origin_branch:
            if request_origin in self.allowed_origins_exact:
                trace.origin_branch = (
                    f"CSRF_TRUSTED_ORIGINS exact entry: {request_origin}"
                )
            else:
                parsed = urlsplit(request_origin)
                hits = [
                    host
                    for host in self.allowed_origin_subdomains.get(parsed.scheme, ())
                    if is_same_domain(parsed.netloc, host)
                ]
                if hits:
                    trace.origin_branch = (
                        f"CSRF_TRUSTED_ORIGINS wildcard rule *.{hits[0]} matched"
                    )
                else:
                    trace.origin_branch = (
                        "no exact, wildcard, or self-origin rule matched"
                    )
        return result

    def _check_referer(self, request):
        trace = self.trace
        trace.referer_header = request.META.get("HTTP_REFERER", "")
        try:
            super()._check_referer(request)
        except Exception as exc:
            trace.referer_outcome = f"REJECT: {exc.reason}"
            raise
        trace.referer_outcome = "accepted"

    def _reject(self, request, reason):
        self.trace.rejection_reason = reason
        response = super()._reject(request, reason)
        self.trace.status_code = response.status_code
        return response


def _common_request_attributes(request, trace):
    trace.scheme = request.scheme
    trace.secure = request.is_secure()
    trace.raw_host = request._get_raw_host()
    try:
        trace.host = request.get_host()
        trace.host_ok = True
    except DisallowedHost as exc:
        trace.host = str(exc)
        trace.host_ok = False
    trace.origin_header = request.META.get("HTTP_ORIGIN", "")
    trace.referer_header = request.META.get("HTTP_REFERER", "")


def _run_middleware(middleware, request):
    # Mirror BaseHandler: process_request() then process_view() with a plain
    # non-exempt callback. The view itself is never reached on a rejection.
    middleware.process_request(request)
    response = middleware.process_view(
        request, callback=_dummy_view, callback_args=(), callback_kwargs={}
    )
    if response is None:
        middleware.trace.status_code = 200


def _dummy_view(request):
    from django.http import HttpResponse

    return HttpResponse("login-ok")


def _post_data():
    return {"csrfmiddlewaretoken": CSRF_FORM_TOKEN}


def _cookies():
    return {CSRF_COOKIE_NAME: CSRF_SECRET}


def run_wsgi(*, headers=None, server_port="80", wsgi_url_scheme="http"):
    """
    Build a WSGIRequest the way WSGIHandler would, from a WSGI environ.

    headers maps HTTP_* META keys -> values (e.g. HTTP_X_FORWARDED_PROTO).
    The dotted wsgi.* keys are written into the environ after construction,
    exactly as a WSGI server would supply them.
    """
    rf = RequestFactory()
    request = rf.post(LOGIN_PATH, data=_post_data(), secure=False)
    # META *is* the WSGI environ for WSGIRequest.
    request.META["wsgi.url_scheme"] = wsgi_url_scheme
    request.META["SERVER_PORT"] = str(server_port)
    request.META.setdefault("HTTP_HOST", "app.example.com")
    request.META.update(headers or {})
    request.COOKIES.update(_cookies())
    trace = Trace(entry="WSGI (WSGIRequest)")
    _common_request_attributes(request, trace)
    _run_middleware(ProbeCsrfMiddleware(trace), request)
    return trace


def run_asgi(*, headers=None, server=("app.example.com", 80), scheme="http"):
    """
    Build an ASGIRequest the way ASGIHandler would, from an ASGI scope.

    headers maps plain header names -> values (e.g. "x-forwarded-proto").
    Headers travel through the real ASGIRequest header parser, so a name
    containing an underscore is discarded exactly as in production (the
    anti-spoofing rule in ASGIRequest.__init__).
    """
    arf = AsyncRequestFactory()
    body = ("csrfmiddlewaretoken=" + CSRF_FORM_TOKEN).encode()
    header_pairs = [
        (name.lower().encode("latin1"), str(value).encode("latin1"))
        for name, value in (("host", server[0]), *(headers or {}).items())
    ]
    header_pairs += [
        (b"content-type", b"application/x-www-form-urlencoded"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    request = arf.request(
        method="POST",
        path=LOGIN_PATH,
        query_string="",
        server=server,
        scheme=scheme,
        headers=header_pairs,
        _body_file=FakePayload(body.decode()),
    )
    request.COOKIES.update(_cookies())
    trace = Trace(entry="ASGI (ASGIRequest)")
    _common_request_attributes(request, trace)
    _run_middleware(ProbeCsrfMiddleware(trace), request)
    return trace
