"""
CSRF decision-path matrix behind an HTTPS reverse proxy.

Every scenario reuses ONE fixed, legal CSRF cookie/token pair (see
harness.CSRF_SECRET / CSRF_FORM_TOKEN), so a DENY is never caused by a bad
token. Only one setting or one header changes between a paired success and
failure case. CSRF is never disabled and no wildcard-all origin is used.
"""

from django.test import SimpleTestCase, override_settings

from .harness import run_asgi, run_wsgi

SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
ORIGIN = "https://app.example.com"
REFERER = "https://app.example.com/login/"


class ProxySchemeHeaderTests(SimpleTestCase):
    """The intermittent failure: internal hop is plain HTTP."""

    @override_settings(SECURE_PROXY_SSL_HEADER=SSL_HEADER)
    def test_wsgi_trusted_forwarded_proto_marks_https_origin_accepted(self):
        trace = run_wsgi(
            headers={"HTTP_X_FORWARDED_PROTO": "https", "HTTP_ORIGIN": ORIGIN}
        )
        self.assertTrue(trace.secure)
        self.assertEqual(
            trace.origin_branch,
            f"exact self-origin match ({ORIGIN} == {ORIGIN})",
        )
        self.assertTrue(trace.accepted, trace.rejection_reason)

    def test_wsgi_same_header_ignored_without_setting_denies(self):
        # One-line diff: SECURE_PROXY_SSL_HEADER removed. The proxy header is
        # now just an untrusted byte string; wsgi.url_scheme stays http.
        trace = run_wsgi(
            headers={"HTTP_X_FORWARDED_PROTO": "https", "HTTP_ORIGIN": ORIGIN}
        )
        self.assertFalse(trace.secure)
        self.assertEqual(trace.scheme, "http")
        self.assertFalse(trace.accepted)
        self.assertIn("Origin checking failed", trace.rejection_reason)

    @override_settings(SECURE_PROXY_SSL_HEADER=SSL_HEADER)
    def test_wsgi_spoofed_proto_value_denied(self):
        # Header trusted by name, but an attacker-controlled value "http".
        trace = run_wsgi(
            headers={"HTTP_X_FORWARDED_PROTO": "http", "HTTP_ORIGIN": ORIGIN}
        )
        self.assertFalse(trace.secure)
        self.assertFalse(trace.accepted)
        self.assertIn("Origin checking failed", trace.rejection_reason)

    @override_settings(SECURE_PROXY_SSL_HEADER=SSL_HEADER)
    def test_asgi_forwarded_proto_equivalent_to_scope_scheme(self):
        # ASGI: the server already translates TLS into scope["scheme"].
        trace_scope = run_asgi(
            headers={"origin": ORIGIN}, scheme="https", server=("app.example.com", 443)
        )
        self.assertTrue(trace_scope.secure)
        self.assertTrue(trace_scope.accepted, trace_scope.rejection_reason)
        # With the setting enabled, the forwarded header gives the same answer.
        trace_header = run_asgi(
            headers={"origin": ORIGIN, "x-forwarded-proto": "https"},
            scheme="http",
            server=("app.example.com", 80),
        )
        self.assertTrue(trace_header.secure)
        self.assertTrue(trace_header.accepted, trace_header.rejection_reason)

    @override_settings(SECURE_PROXY_SSL_HEADER=SSL_HEADER)
    def test_referer_path_fails_insecure_when_header_not_honored(self):
        # Firefox-style POST: no Origin, only Referer. Over the internal http
        # hop without an honored proto header -> strict HTTPS referer check.
        trace = run_wsgi(headers={"HTTP_REFERER": REFERER})
        self.assertFalse(trace.secure)  # Origin absent so referer only runs if secure
        self.assertTrue(trace.accepted)  # insecure requests skip the referer branch

    @override_settings(SECURE_PROXY_SSL_HEADER=SSL_HEADER)
    def test_referer_path_secure_missing_referer_denied(self):
        trace = run_wsgi(
            headers={"HTTP_X_FORWARDED_PROTO": "https"}
        )  # no Origin, no Referer
        self.assertTrue(trace.secure)
        self.assertFalse(trace.accepted)
        self.assertEqual(
            trace.referer_outcome,
            "REJECT: Referer checking failed - no Referer.",
        )

    @override_settings(SECURE_PROXY_SSL_HEADER=SSL_HEADER)
    def test_referer_path_secure_http_referer_denied(self):
        trace = run_wsgi(
            headers={
                "HTTP_X_FORWARDED_PROTO": "https",
                "HTTP_REFERER": "http://app.example.com/login/",
            }
        )
        self.assertTrue(trace.secure)
        self.assertFalse(trace.accepted)
        self.assertIn(
            "Referer is insecure while host is secure",
            trace.rejection_reason,
        )


class NonDefaultPortTests(SimpleTestCase):
    @override_settings(SECURE_PROXY_SSL_HEADER=SSL_HEADER)
    def test_wsgi_non_default_port_origin_includes_port_denied_by_default(self):
        trace = run_wsgi(
            server_port="8443",
            headers={
                "HTTP_X_FORWARDED_PROTO": "https",
                "HTTP_HOST": "app.example.com:8443",
                "HTTP_ORIGIN": "https://app.example.com:8443",
            },
        )
        self.assertEqual(trace.host, "app.example.com:8443")
        # Self-origin is built from the same host:port, so it actually matches.
        self.assertTrue(trace.accepted, trace.rejection_reason)

    @override_settings(SECURE_PROXY_SSL_HEADER=SSL_HEADER)
    def test_wsgi_port_mismatch_between_origin_and_host_denied(self):
        # Proxy rewrites Host:443 but the browser Origin keeps :8443.
        trace = run_wsgi(
            server_port="443",
            headers={
                "HTTP_X_FORWARDED_PROTO": "https",
                "HTTP_HOST": "app.example.com",
                "HTTP_ORIGIN": "https://app.example.com:8443",
            },
        )
        self.assertFalse(trace.accepted)
        self.assertIn("does not match any trusted origins", trace.rejection_reason)

    @override_settings(
        SECURE_PROXY_SSL_HEADER=SSL_HEADER,
        CSRF_TRUSTED_ORIGINS=["https://app.example.com:8443"],
    )
    def test_wsgi_port_fixed_by_one_trusted_origin_entry(self):
        trace = run_wsgi(
            server_port="443",
            headers={
                "HTTP_X_FORWARDED_PROTO": "https",
                "HTTP_HOST": "app.example.com",
                "HTTP_ORIGIN": "https://app.example.com:8443",
            },
        )
        self.assertIn("CSRF_TRUSTED_ORIGINS exact entry", trace.origin_branch)
        self.assertTrue(trace.accepted, trace.rejection_reason)


class TrustedOriginBoundaryTests(SimpleTestCase):
    @override_settings(
        SECURE_PROXY_SSL_HEADER=SSL_HEADER,
        CSRF_TRUSTED_ORIGINS=["https://*.app.example.com"],
    )
    def test_legitimate_subdomain_accepted_via_wildcard(self):
        # Cross-subdomain POST: the page Host differs from the Origin, so the
        # self-origin rule cannot fire and only the wildcard rule can accept.
        trace = run_wsgi(
            headers={
                "HTTP_X_FORWARDED_PROTO": "https",
                "HTTP_HOST": "app.example.com",
                "HTTP_ORIGIN": "https://login.app.example.com",
            }
        )
        self.assertTrue(trace.host_ok)
        self.assertIn("wildcard rule", trace.origin_branch)
        self.assertTrue(trace.accepted, trace.rejection_reason)

    @override_settings(
        SECURE_PROXY_SSL_HEADER=SSL_HEADER,
        CSRF_TRUSTED_ORIGINS=["https://*.app.example.com"],
    )
    def test_lookalike_suffix_rejected(self):
        # evil-app.example.com is a *different* registrable domain, not a
        # subdomain. is_same_domain() must not treat it as one.
        trace = run_wsgi(
            headers={
                "HTTP_X_FORWARDED_PROTO": "https",
                "HTTP_HOST": "app.example.com",
                "HTTP_ORIGIN": "https://evil-app.example.com",
            }
        )
        self.assertFalse(trace.accepted)
        self.assertIn("does not match any trusted origins", trace.rejection_reason)

    @override_settings(
        SECURE_PROXY_SSL_HEADER=SSL_HEADER,
        CSRF_TRUSTED_ORIGINS=["https://*.app.example.com"],
    )
    def test_scheme_is_part_of_the_wildcard_rule(self):
        # An http origin cannot ride in on an https wildcard entry.
        trace = run_wsgi(
            headers={
                "HTTP_X_FORWARDED_PROTO": "https",
                "HTTP_HOST": "app.example.com",
                "HTTP_ORIGIN": "http://login.app.example.com",
            }
        )
        self.assertFalse(trace.accepted)
        self.assertIn("does not match any trusted origins", trace.rejection_reason)


class TokenCheckStillEnforcedTests(SimpleTestCase):
    """Prove the harness observes, rather than bypasses, CsrfViewMiddleware."""

    def test_wsgi_wrong_form_token_is_rejected_after_origin_passes(self):
        from django.test import RequestFactory

        from .harness import (
            CSRF_COOKIE_NAME,
            CSRF_SECRET,
            LOGIN_PATH,
            ProbeCsrfMiddleware,
            Trace,
            _common_request_attributes,
            _run_middleware,
        )

        rf = RequestFactory()
        request = rf.post(
            LOGIN_PATH, data={"csrfmiddlewaretoken": "a" * 64}, secure=True
        )
        request.META["HTTP_HOST"] = "app.example.com"
        request.META["HTTP_ORIGIN"] = "https://app.example.com"
        request.COOKIES.update({CSRF_COOKIE_NAME: CSRF_SECRET})
        trace = Trace(entry="WSGI (WSGIRequest)")
        _common_request_attributes(request, trace)
        _run_middleware(ProbeCsrfMiddleware(trace), request)
        self.assertTrue(trace.secure)  # origin stage would pass
        self.assertFalse(trace.accepted)
        self.assertIn("CSRF token", trace.rejection_reason)


class UntrustedForwardedHeaderTests(SimpleTestCase):
    def test_wsgi_x_forwarded_host_ignored_unless_use_x_forwarded_host(self):
        trace = run_wsgi(
            headers={
                "HTTP_X_FORWARDED_HOST": "attacker.example",
                "HTTP_HOST": "app.example.com",
                "HTTP_ORIGIN": "https://app.example.com",
            },
            wsgi_url_scheme="https",
        )
        self.assertEqual(trace.host, "app.example.com")
        self.assertTrue(trace.accepted, trace.rejection_reason)

    @override_settings(USE_X_FORWARDED_HOST=True)
    def test_wsgi_x_forwarded_host_honored_and_host_validated(self):
        trace = run_wsgi(
            headers={
                "HTTP_X_FORWARDED_HOST": "attacker.example",
                "HTTP_HOST": "app.example.com",
                "HTTP_ORIGIN": "https://attacker.example",
            },
            wsgi_url_scheme="https",
        )
        # The spoofed host is now consumed but fails ALLOWED_HOSTS first.
        self.assertFalse(trace.host_ok)
        self.assertFalse(trace.accepted)
        self.assertIn("Origin checking failed", trace.rejection_reason)

    def test_asgi_underscore_header_dropped_by_request_parser(self):
        # ASGIRequest discards any header name containing "_" (anti-spoofing).
        from django.test import AsyncRequestFactory

        arf = AsyncRequestFactory()
        request = arf.request(
            method="POST",
            path="/login/",
            server=("app.example.com", 80),
            scheme="http",
            headers=[(b"x_forwarded_proto", b"https"), (b"host", b"app.example.com")],
        )
        self.assertNotIn("HTTP_X_FORWARDED_PROTO", request.META)

        trace = run_asgi(
            headers={
                "x_forwarded_proto": "https",  # underscore -> dropped
                "origin": "https://app.example.com",
            },
            scheme="http",
        )
        self.assertFalse(trace.secure)  # header never reached META
        self.assertFalse(trace.accepted)

    def test_wsgi_underscore_header_is_not_specially_dropped(self):
        # WSGI servers decide underscore handling; Django sees it in environ.
        # Without SECURE_PROXY_SSL_HEADER it still has no security effect.
        trace = run_wsgi(
            headers={
                "HTTP_X_FORWARDED_PROTO": "https",
                "HTTP_ORIGIN": "https://app.example.com",
            }
        )
        self.assertFalse(trace.secure)
        self.assertFalse(trace.accepted)
