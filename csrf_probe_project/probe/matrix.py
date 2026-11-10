"""
Declarative scenario matrix. Each item is one probe run plus the exact
@override_settings context that distinguishes it from its paired case.

The same scenarios are rendered by the `csrf_matrix` management command and
kept intentionally independent of the unittest assertions, so the printed
report shows raw framework behavior while tests lock in the contract.
"""

from dataclasses import dataclass

from django.test import override_settings

from .harness import run_asgi, run_wsgi

SSL = ("HTTP_X_FORWARDED_PROTO", "https")
ORIGIN_HTTPS = "https://app.example.com"
ORIGIN_PORT = "https://app.example.com:8443"
ORIGIN_SUBDOMAIN = "https://login.app.example.com"
ORIGIN_LOOKALIKE = "https://evil-app.example.com"
ORIGIN_HTTP_SUBDOMAIN = "http://login.app.example.com"
REFERER_HTTPS = "https://app.example.com/login/"


@dataclass
class Scenario:
    name: str
    entry: str  # "wsgi" | "asgi"
    settings: dict
    kwargs: dict
    expect_accept: bool
    fixed_by: str  # "proxy" (header sanitization) or "django" (setting)


SCENARIOS = [
    # --- Group A: the intermittent proxy failure -------------------------
    Scenario(
        "A1 WSGI proto header honored -> secure, self-origin accepted",
        "wsgi",
        {"SECURE_PROXY_SSL_HEADER": SSL},
        {"headers": {"HTTP_X_FORWARDED_PROTO": "https", "HTTP_ORIGIN": ORIGIN_HTTPS}},
        True,
        "django (enable setting) + proxy (overwrite the header)",
    ),
    Scenario(
        "A2 WSGI same header, setting absent -> treated as http, DENIED",
        "wsgi",
        {},
        {"headers": {"HTTP_X_FORWARDED_PROTO": "https", "HTTP_ORIGIN": ORIGIN_HTTPS}},
        False,
        "django (add SECURE_PROXY_SSL_HEADER)",
    ),
    Scenario(
        "A3 WSGI header spoofed to http -> DENIED despite TLS at proxy",
        "wsgi",
        {"SECURE_PROXY_SSL_HEADER": SSL},
        {"headers": {"HTTP_X_FORWARDED_PROTO": "http", "HTTP_ORIGIN": ORIGIN_HTTPS}},
        False,
        "proxy (strip/overwrite client-supplied X-Forwarded-Proto)",
    ),
    Scenario(
        "A4 ASGI scope scheme=https -> secure without any forwarded header",
        "asgi",
        {},
        {"headers": {"origin": ORIGIN_HTTPS}, "scheme": "https",
         "server": ("app.example.com", 443)},
        True,
        "asgi server (map TLS into scope['scheme'])",
    ),
    Scenario(
        "A5 ASGI internal http + untrusted proto header -> DENIED",
        "asgi",
        {},
        {"headers": {"origin": ORIGIN_HTTPS, "x-forwarded-proto": "https"},
         "scheme": "http", "server": ("app.example.com", 80)},
        False,
        "django (SECURE_PROXY_SSL_HEADER) or asgi server scheme",
    ),
    # --- Group B: Referer-only HTTPS POSTs (no Origin header) -------------
    Scenario(
        "B1 WSGI secure, no Origin and no Referer -> REASON_NO_REFERER",
        "wsgi",
        {"SECURE_PROXY_SSL_HEADER": SSL},
        {"headers": {"HTTP_X_FORWARDED_PROTO": "https"}},
        False,
        "proxy/browser (ensure Referer policy sends it)",
    ),
    Scenario(
        "B2 WSGI secure, Referer downgraded to http -> REASON_INSECURE_REFERER",
        "wsgi",
        {"SECURE_PROXY_SSL_HEADER": SSL},
        {"headers": {"HTTP_X_FORWARDED_PROTO": "https",
                     "HTTP_REFERER": "http://app.example.com/login/"}},
        False,
        "proxy (preserve https scheme; do not terminate to http links)",
    ),
    Scenario(
        "B3 WSGI secure, valid https Referer, same host -> accepted",
        "wsgi",
        {"SECURE_PROXY_SSL_HEADER": SSL},
        {"headers": {"HTTP_X_FORWARDED_PROTO": "https",
                     "HTTP_REFERER": REFERER_HTTPS}},
        True,
        "-",
    ),
    # --- Group C: non-default port ----------------------------------------
    Scenario(
        "C1 WSGI Origin :8443 but Host rewritten to :443 -> DENIED",
        "wsgi",
        {"SECURE_PROXY_SSL_HEADER": SSL},
        {"server_port": "443",
         "headers": {"HTTP_X_FORWARDED_PROTO": "https",
                     "HTTP_HOST": "app.example.com",
                     "HTTP_ORIGIN": ORIGIN_PORT}},
        False,
        "django (add :8443 origin) or proxy (keep Host:port consistent)",
    ),
    Scenario(
        "C2 WSGI one-line CSRF_TRUSTED_ORIGINS entry for :8443 -> accepted",
        "wsgi",
        {"SECURE_PROXY_SSL_HEADER": SSL,
         "CSRF_TRUSTED_ORIGINS": ["https://app.example.com:8443"]},
        {"server_port": "443",
         "headers": {"HTTP_X_FORWARDED_PROTO": "https",
                     "HTTP_HOST": "app.example.com",
                     "HTTP_ORIGIN": ORIGIN_PORT}},
        True,
        "django (CSRF_TRUSTED_ORIGINS with explicit port)",
    ),
    Scenario(
        "C3 WSGI host:port and origin:port agree -> self-origin accepted",
        "wsgi",
        {"SECURE_PROXY_SSL_HEADER": SSL},
        {"server_port": "8443",
         "headers": {"HTTP_X_FORWARDED_PROTO": "https",
                     "HTTP_HOST": "app.example.com:8443",
                     "HTTP_ORIGIN": ORIGIN_PORT}},
        True,
        "-",
    ),
    # --- Group D: subdomain trust vs lookalike suffix ---------------------
    Scenario(
        "D1 WSGI real subdomain under https://*.app.example.com -> accepted",
        "wsgi",
        {"SECURE_PROXY_SSL_HEADER": SSL,
         "CSRF_TRUSTED_ORIGINS": ["https://*.app.example.com"]},
        {"headers": {"HTTP_X_FORWARDED_PROTO": "https",
                     "HTTP_HOST": "app.example.com",
                     "HTTP_ORIGIN": ORIGIN_SUBDOMAIN}},
        True,
        "-",
    ),
    Scenario(
        "D2 WSGI lookalike evil-app.example.com -> DENIED (not a subdomain)",
        "wsgi",
        {"SECURE_PROXY_SSL_HEADER": SSL,
         "CSRF_TRUSTED_ORIGINS": ["https://*.app.example.com"]},
        {"headers": {"HTTP_X_FORWARDED_PROTO": "https",
                     "HTTP_HOST": "app.example.com",
                     "HTTP_ORIGIN": ORIGIN_LOOKALIKE}},
        False,
        "no fix needed: is_same_domain correctly rejects it",
    ),
    Scenario(
        "D3 WSGI http subdomain cannot use an https wildcard -> DENIED",
        "wsgi",
        {"SECURE_PROXY_SSL_HEADER": SSL,
         "CSRF_TRUSTED_ORIGINS": ["https://*.app.example.com"]},
        {"headers": {"HTTP_X_FORWARDED_PROTO": "https",
                     "HTTP_HOST": "app.example.com",
                     "HTTP_ORIGIN": ORIGIN_HTTP_SUBDOMAIN}},
        False,
        "no fix needed: scheme is part of the rule",
    ),
    # --- Group E: forwarded host / untrusted headers ----------------------
    Scenario(
        "E1 WSGI X-Forwarded-Host ignored while USE_X_FORWARDED_HOST=False",
        "wsgi",
        {},
        {"wsgi_url_scheme": "https",
         "headers": {"HTTP_X_FORWARDED_HOST": "attacker.example",
                     "HTTP_HOST": "app.example.com",
                     "HTTP_ORIGIN": ORIGIN_HTTPS}},
        True,
        "-",
    ),
    Scenario(
        "E2 WSGI USE_X_FORWARDED_HOST=True, spoofed host -> ALLOWED_HOSTS block",
        "wsgi",
        {"USE_X_FORWARDED_HOST": True},
        {"wsgi_url_scheme": "https",
         "headers": {"HTTP_X_FORWARDED_HOST": "attacker.example",
                     "HTTP_HOST": "app.example.com",
                     "HTTP_ORIGIN": "https://attacker.example"}},
        False,
        "proxy (sanitize X-Forwarded-Host) before enabling the setting",
    ),
]


def run_matrix():
    """Execute every scenario inside its own override_settings context."""
    results = []
    for scenario in SCENARIOS:
        with override_settings(**scenario.settings):
            if scenario.entry == "wsgi":
                trace = run_wsgi(**scenario.kwargs)
            else:
                trace = run_asgi(**scenario.kwargs)
        results.append((scenario, trace))
    return results
