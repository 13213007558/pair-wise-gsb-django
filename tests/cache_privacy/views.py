"""
Views used by the cache-privacy regression tests.

Each view renders content that is unique to the caller's identity so that a
cross-identity cache hit is directly visible in the response body. Identity
comes from either the session (two cookie clients) or the Authorization
header (the token client).
"""
from datetime import datetime, timezone as dt_timezone

from django.http import HttpResponse, HttpResponseNotModified, StreamingHttpResponse
from django.template import engines
from django.template.response import TemplateResponse
from django.utils.cache import patch_vary_headers
from django.views.decorators.http import condition


def identity_of(request):
    """The identity the page is personalized for, visible in the body."""
    session = getattr(request, "session", None)
    if session is None:
        # No SessionMiddleware in the stack (misconfiguration tests).
        return "anonymous"
    return session.get("identity", "anonymous")


def profile_view(request):
    """Personalized page. Relies on SessionMiddleware's Vary: Cookie."""
    return HttpResponse(f"profile:{identity_of(request)}")


def read_only_session_view(request):
    """Touches the session but never modifies it."""
    request.session.get("identity", "anonymous")
    return HttpResponse(f"readonly:{identity_of(request)}")


def first_visit_view(request):
    """Mints a session cookie on the first call for a cookieless visitor."""
    if "identity" not in request.session:
        request.session["identity"] = "user-A"
    return HttpResponse(f"first-visit:{identity_of(request)}")


def raw_cookie_profile_view(request):
    """
    Personalization from a raw cookie without SessionMiddleware and without
    any declared caching policy. Used to show where the framework's
    information ends and the application's responsibility begins.
    """
    return HttpResponse(f"raw-cookie:{request.COOKIES.get('identity', 'anonymous')}")


def raw_cookie_private_view(request):
    """Same raw-cookie personalization, but the app declares it private."""
    response = HttpResponse(
        f"raw-cookie-private:{request.COOKIES.get('identity', 'anonymous')}"
    )
    response["Cache-Control"] = "private"
    return response


def private_view(request):
    """Application explicitly marks the identity page private."""
    response = HttpResponse(f"private:{identity_of(request)}")
    response["Cache-Control"] = "private"
    return response


def no_store_view(request):
    """Application explicitly forbids any caching of the identity page."""
    response = HttpResponse(f"no-store:{identity_of(request)}")
    response["Cache-Control"] = "no-store"
    return response


def vary_asterisk_view(request):
    """Vary: * forbids shared caching of this identity page."""
    response = HttpResponse(f"vary-star:{identity_of(request)}")
    patch_vary_headers(response, ("*",))
    return response


_LAST_MODIFIED = datetime(2023, 1, 2, tzinfo=dt_timezone.utc)


def _etag(request):
    return '"profile-etag"'


@condition(etag_func=_etag, last_modified_func=lambda request: _LAST_MODIFIED)
def conditional_view(request):
    return HttpResponse(f"conditional:{identity_of(request)}")


def not_modified_view(request):
    """Always answers 304, which still carries personalized headers."""
    response = HttpResponseNotModified()
    response["ETag"] = '"static-etag"'
    response["Cache-Control"] = "private"
    patch_vary_headers(response, ("Cookie",))
    return response


def delayed_render_view(request):
    """TemplateResponse: Vary headers exist before render, content after."""
    template = engines["django"].from_string("delayed:{{ identity }}")
    response = TemplateResponse(request, template, {"identity": identity_of(request)})
    return response


def streaming_view(request):
    """Streaming responses must never enter the page cache."""
    identity = identity_of(request)
    return StreamingHttpResponse(
        iter([f"streaming:{identity}".encode()]),
    )


def public_view(request):
    """Genuinely public, identity-independent page; sharing is intended."""
    response = HttpResponse("public:shared-with-everyone")
    response["Cache-Control"] = "public, max-age=30"
    return response


def public_token_view(request):
    """
    Public page that may be requested with an Authorization header. The
    explicit public directive is the application's statement that sharing is
    safe even for authenticated requests (RFC 9111, Section 3.5).
    """
    response = HttpResponse("public-token:shared-with-everyone")
    response["Cache-Control"] = "public, max-age=30"
    return response
