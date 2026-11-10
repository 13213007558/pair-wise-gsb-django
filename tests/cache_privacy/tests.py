"""
Regression tests for the combination of the two-part cache middleware
(``UpdateCacheMiddleware`` / ``FetchFromCacheMiddleware``) with
``SessionMiddleware``.

The scenario under test is the classic "user B sees user A's page" leak:
three clients hit the *same* path -- two cookie sessions (A and B), one
cookieless anonymous client, and an additional client that sends an
``Authorization`` header. After every response the LocMem cache is inspected
for the header registry and the stored response body, so the assertions are
about what is actually cached (and under which key), not merely about which
headers appear on a response.

Guarantees vs. application responsibility
-----------------------------------------
Framework guarantees (hold as long as the documented middleware ordering is
kept, i.e. UpdateCacheMiddleware first / FetchFromCacheMiddleware last with
SessionMiddleware in between):

* ``Vary: Cookie`` added by SessionMiddleware is learned into the cache key,
  so distinct sessions get distinct entries and cannot share a page.
* A response that sets a cookie while ``Vary: Cookie`` is present is never
  stored (first-visit / session-mint responses cannot poison a cookieless
  client's entry).
* ``Cache-Control: private|no-cache|no-store`` (including qualified forms)
  and ``Vary: *`` keep the response out of the cache.
* 304, streaming and HEAD responses follow the documented caching rules.
* A request bearing ``Authorization`` is made to ``Vary: Authorization``
  unless the application explicitly marks the response ``public``.

Application responsibility (the framework cannot infer these):

* Identity pages whose personalization never goes through ``request.session``
  (e.g. reads a raw cookie or an auth token itself) must declare their own
  caching policy (``private``/``no-store``/an explicit ``Vary``). The
  NoSessionMiddlewareLeakTests class reproduces the raw-cookie leak and shows
  that ``Cache-Control: private`` closes it.
* The middleware ordering above must actually be configured; putting
  SessionMiddleware outside the cache pair removes ``Vary: Cookie`` from the
  key and the cookie-setting guard, and the page is cached once for everyone.
  The MisconfiguredOrderLeakTests class captures the observable failure mode.
"""
import pickle
from contextlib import contextmanager
from unittest import mock

import django
import django.core.cache.backends.locmem as locmem_module
from django.conf import settings
from django.core.cache.backends.locmem import LocMemCache
from django.http import HttpResponseNotModified
from django.test import Client, SimpleTestCase, override_settings
from django.utils.cache import has_vary_header

CACHE_HEADERS_PREFIX = "views.decorators.cache.cache_header."
CACHE_PAGES_PREFIX = "views.decorators.cache.cache_page."

MIDDLEWARE_CORRECT_ORDER = [
    "django.middleware.cache.UpdateCacheMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.cache.FetchFromCacheMiddleware",
]

# SessionMiddleware outside the cache pair: it still patches Vary on the
# final response, but UpdateCacheMiddleware (last in the response phase here)
# never observes that header or the new cookie.
MIDDLEWARE_WRONG_ORDER = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.cache.UpdateCacheMiddleware",
    "django.middleware.cache.FetchFromCacheMiddleware",
]

# No SessionMiddleware at all: identity carried by a raw cookie the view
# reads itself, without any declared Vary/private policy.
MIDDLEWARE_NO_SESSION = [
    "django.middleware.cache.UpdateCacheMiddleware",
    "django.middleware.cache.FetchFromCacheMiddleware",
]


@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "cache-privacy-regression",
            "KEY_PREFIX": "privacy",
        }
    },
    CACHE_MIDDLEWARE_KEY_PREFIX="",
    CACHE_MIDDLEWARE_SECONDS=30,
    SESSION_ENGINE="django.contrib.sessions.backends.signed_cookies",
    ROOT_URLCONF="cache_privacy.urls",
    MIDDLEWARE=MIDDLEWARE_CORRECT_ORDER,
)
class CorrectOrderCachePrivacyTests(SimpleTestCase):
    """
    Framework guarantees with the documented middleware ordering. Every test
    performs real requests end-to-end and then inspects LocMemCache contents.
    """

    def setUp(self):
        self.locmem = self._locmem_backend()
        self.locmem.clear()

    def tearDown(self):
        self.locmem.clear()

    def _locmem_backend(self):
        from django.core.cache import caches

        backend = caches["default"]
        self.assertIsInstance(backend, LocMemCache)
        return backend

    def _clients(self):
        """Two cookie sessions plus one cookieless anonymous client."""
        return Client(), Client(), Client()

    def _set_identity(self, client, identity):
        session = client.session
        session["identity"] = identity
        session.save()
        client.cookies[settings.SESSION_COOKIE_NAME] = session.session_key

    def _cache_entries(self, prefix):
        return {
            key: value
            for key, value in self.locmem._cache.items()
            if prefix in key
        }

    def _header_entries(self):
        return self._cache_entries(CACHE_HEADERS_PREFIX)

    def _page_entries(self):
        return self._cache_entries(CACHE_PAGES_PREFIX)

    def _stored_page(self, key):
        return pickle.loads(self.locmem._cache[key])

    def _assert_no_pages_cached(self, path):
        pages = self._page_entries()
        self.assertEqual(
            pages,
            {},
            f"{path} must not be stored in the page cache, found {sorted(pages)}",
        )

    # ----- the core "B must not see A" regression -------------------------

    def test_distinct_sessions_get_distinct_cache_entries(self):
        """Two sessions on the same path produce two isolated cache entries."""
        client_a, client_b, anonymous = self._clients()
        self._set_identity(client_a, "user-A")
        self._set_identity(client_b, "user-B")

        response_a = client_a.get("/profile/")
        response_b = client_b.get("/profile/")
        response_anon = anonymous.get("/profile/")

        self.assertEqual(response_a.content, b"profile:user-A")
        self.assertEqual(response_b.content, b"profile:user-B")
        self.assertEqual(response_anon.content, b"profile:anonymous")

        # The header registry learned Vary: Cookie from SessionMiddleware.
        header_entries = self._header_entries()
        self.assertEqual(len(header_entries), 1)
        self.assertEqual(
            pickle.loads(header_entries.popitem()[1]), ["HTTP_COOKIE"]
        )

        # One cached page per distinct Cookie value: A, B and the cookieless
        # marker must never collapse into a single shared entry.
        pages = self._page_entries()
        self.assertEqual(len(pages), 3)
        stored_bodies = {
            self._stored_page(key).content for key in pages
        }
        self.assertEqual(
            stored_bodies,
            {b"profile:user-A", b"profile:user-B", b"profile:anonymous"},
        )

        # Repeat requests are served from the cache and stay isolated: this
        # is the successful-hit control, not a cache-disabled response.
        repeat_b = client_b.get("/profile/")
        self.assertEqual(repeat_b.content, b"profile:user-B")
        self._assert_cache_hit(client_b, "/profile/", b"profile:user-B")
        repeat_anon = anonymous.get("/profile/")
        self.assertEqual(repeat_anon.content, b"profile:anonymous")

    def _assert_cache_hit(self, client, path, expected_body):
        """Force a second identical request and verify the view is bypassed."""
        with mock.patch(
            "cache_privacy.views.profile_view",
            side_effect=AssertionError("view must not run on a cache hit"),
        ):
            response = client.get(path)
        self.assertEqual(response.content, expected_body)

    @contextmanager
    def _frozen_time(self, timestamp):
        """
        Freeze every clock the cache stack consults: LocMemCache expiry and
        the Expires/max-age math in django.middleware.cache and django.utils
        both resolve through their own ``time.time`` imports.
        """
        with (
            mock.patch.object(
                locmem_module.time, "time", return_value=timestamp
            ),
            mock.patch.object(
                django.middleware.cache.time, "time", return_value=timestamp
            ),
            mock.patch.object(
                django.utils.cache.time, "time", return_value=timestamp
            ),
        ):
            yield

    # ----- read-only session ----------------------------------------------

    def test_read_only_session_still_varies_on_cookie(self):
        """Accessing (never writing) the session still forces Vary: Cookie."""
        client_a, client_b, _ = self._clients()
        self._set_identity(client_a, "user-A")
        self._set_identity(client_b, "user-B")

        response_a = client_a.get("/read-only/")
        response_b = client_b.get("/read-only/")

        self.assertTrue(has_vary_header(response_a, "Cookie"))
        self.assertEqual(response_a.content, b"readonly:user-A")
        self.assertEqual(response_b.content, b"readonly:user-B")
        self.assertEqual(len(self._page_entries()), 2)
        # Neither response sets a new cookie: the session was only read.
        self.assertNotIn(settings.SESSION_COOKIE_NAME, response_a.cookies)

    # ----- first-time cookie being set ------------------------------------

    def test_response_setting_session_cookie_is_not_cached(self):
        """
        The first response that mints a session cookie must not be stored,
        otherwise the next cookieless visitor would be served A's first page.
        """
        first_visitor = Client()
        self.assertNotIn(settings.SESSION_COOKIE_NAME, first_visitor.cookies)

        response = first_visitor.get("/first-visit/")
        self.assertEqual(response.content, b"first-visit:user-A")
        self.assertIn(settings.SESSION_COOKIE_NAME, response.cookies)
        # No header registry and no page entry for this path.
        self.assertEqual(self._header_entries(), {})
        self._assert_no_pages_cached("/first-visit/")

        # A different cookieless client must build its own response.
        second_visitor = Client()
        response_b = second_visitor.get("/first-visit/")
        self.assertEqual(response_b.content, b"first-visit:user-A")
        self.assertIn(
            settings.SESSION_COOKIE_NAME, response_b.cookies
        )
        self._assert_no_pages_cached("/first-visit/")

    # ----- private / no-store ---------------------------------------------

    def test_private_response_is_not_cached(self):
        client_a, client_b, _ = self._clients()
        self._set_identity(client_a, "user-A")

        response_a = client_a.get("/private/")
        self.assertIn("private", response_a["Cache-Control"])
        self._assert_no_pages_cached("/private/")
        self.assertEqual(self._header_entries(), {})

        self._set_identity(client_b, "user-B")
        response_b = client_b.get("/private/")
        self.assertEqual(response_b.content, b"private:user-B")
        self._assert_no_pages_cached("/private/")

    def test_no_store_response_is_not_cached(self):
        client_a, _, anonymous = self._clients()
        self._set_identity(client_a, "user-A")

        response_a = client_a.get("/no-store/")
        self.assertIn("no-store", response_a["Cache-Control"])
        self._assert_no_pages_cached("/no-store/")

        response_anon = anonymous.get("/no-store/")
        self.assertEqual(response_anon.content, b"no-store:anonymous")
        self._assert_no_pages_cached("/no-store/")

    # ----- Vary: * ---------------------------------------------------------

    def test_vary_asterisk_response_is_not_cached(self):
        client_a, _, anonymous = self._clients()
        self._set_identity(client_a, "user-A")

        response_a = client_a.get("/vary-star/")
        self.assertTrue(has_vary_header(response_a, "*"))
        self._assert_no_pages_cached("/vary-star/")
        self.assertEqual(self._header_entries(), {})

        response_anon = anonymous.get("/vary-star/")
        self.assertEqual(response_anon.content, b"vary-star:anonymous")
        self._assert_no_pages_cached("/vary-star/")

    # ----- 304 Not Modified ------------------------------------------------

    def test_304_response_is_not_stored_in_page_cache(self):
        client = Client()
        response = client.get(
            "/not-modified/", headers={"If-None-Match": '"static-etag"'}
        )
        self.assertEqual(response.status_code, 304)
        self.assertIsInstance(response, HttpResponseNotModified)
        self._assert_no_pages_cached("/not-modified/")

    def test_conditional_304_uncached_path_does_not_poison_other_clients(self):
        """
        On a path whose 200 representation has never been cached, a
        conditional request that produces a 304 must not store anything that
        another client could later be served.
        """
        client_a, client_b, _ = self._clients()
        self._set_identity(client_a, "user-A")
        self._set_identity(client_b, "user-B")

        not_modified = client_a.get(
            "/conditional/", headers={"If-None-Match": '"profile-etag"'}
        )
        self.assertEqual(not_modified.status_code, 304)
        self.assertIsInstance(not_modified, HttpResponseNotModified)
        self._assert_no_pages_cached("/conditional/")

        # B's unconditional request builds its own 200 entry.
        response_b = client_b.get("/conditional/")
        self.assertEqual(response_b.status_code, 200)
        self.assertEqual(response_b.content, b"conditional:user-B")

        # A's cached 200 and B's stay isolated once A requests unconditionally.
        response_a = client_a.get("/conditional/")
        self.assertEqual(response_a.content, b"conditional:user-A")
        stored_bodies = {
            self._stored_page(key).content for key in self._page_entries()
        }
        self.assertEqual(
            stored_bodies, {b"conditional:user-A", b"conditional:user-B"}
        )

    def test_cached_200_is_served_to_conditional_repeat_without_rebuild(self):
        """
        Once A's 200 is cached, a later conditional request from A is served
        from the cache (the view never reruns); the stored entry is untouched
        and B still has its own.
        """
        client_a, client_b, _ = self._clients()
        self._set_identity(client_a, "user-A")
        self._set_identity(client_b, "user-B")

        first = client_a.get("/conditional/")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.content, b"conditional:user-A")
        pages_before = set(self._page_entries())
        self.assertEqual(len(pages_before), 1)

        with mock.patch(
            "cache_privacy.views.conditional_view",
            side_effect=AssertionError("cached response must bypass the view"),
        ):
            repeat = client_a.get(
                "/conditional/", headers={"If-None-Match": '"profile-etag"'}
            )
        self.assertEqual(repeat.status_code, 200)
        self.assertEqual(repeat.content, b"conditional:user-A")
        self.assertEqual(set(self._page_entries()), pages_before)

        response_b = client_b.get("/conditional/")
        self.assertEqual(response_b.content, b"conditional:user-B")
        self.assertEqual(len(self._page_entries()), 2)

    # ----- HEAD ------------------------------------------------------------

    def test_head_is_served_from_cached_get_without_leak(self):
        client_a, client_b, _ = self._clients()
        self._set_identity(client_a, "user-A")
        self._set_identity(client_b, "user-B")

        get_a = client_a.get("/profile/")
        self.assertEqual(get_a.content, b"profile:user-A")

        head_a = client_a.head("/profile/")
        self.assertEqual(head_a.status_code, 200)
        self.assertEqual(head_a.content, b"")
        # HEAD reuses A's GET cache entry; no extra per-method entry and no
        # cross-session entry.
        pages = self._page_entries()
        self.assertEqual(len(pages), 1)
        stored = self._stored_page(next(iter(pages)))
        self.assertEqual(stored.content, b"profile:user-A")

        # B's HEAD must not resolve to A's GET entry.
        head_b = client_b.head("/profile/")
        self.assertEqual(head_b.content, b"")
        self.assertEqual(len(self._page_entries()), 2)

    # ----- deferred rendering (TemplateResponse) --------------------------

    def test_template_response_is_cached_after_render_with_correct_body(self):
        """
        UpdateCacheMiddleware registers a post-render callback: the stored
        object must be the rendered TemplateResponse carrying A's body, and
        the entry must be keyed on Cookie so B never receives it.
        """
        client_a, client_b, _ = self._clients()
        self._set_identity(client_a, "user-A")
        self._set_identity(client_b, "user-B")

        response_a = client_a.get("/delayed/")
        self.assertEqual(response_a.content, b"delayed:user-A")
        pages = self._page_entries()
        self.assertEqual(len(pages), 1)
        self.assertEqual(
            self._stored_page(next(iter(pages))).content, b"delayed:user-A"
        )

        response_b = client_b.get("/delayed/")
        self.assertEqual(response_b.content, b"delayed:user-B")
        self.assertEqual(len(self._page_entries()), 2)

    # ----- streaming -------------------------------------------------------

    def test_streaming_response_is_not_cached(self):
        client_a, _, anonymous = self._clients()
        self._set_identity(client_a, "user-A")

        response_a = client_a.get("/streaming/")
        self.assertEqual(b"".join(response_a.streaming_content), b"streaming:user-A")
        self._assert_no_pages_cached("/streaming/")

        response_anon = anonymous.get("/streaming/")
        self.assertEqual(
            b"".join(response_anon.streaming_content), b"streaming:anonymous"
        )
        self._assert_no_pages_cached("/streaming/")

    # ----- Authorization ---------------------------------------------------

    def test_authorization_request_varies_and_does_not_share_with_anonymous(self):
        """
        Without an explicit public policy the framework patches
        Vary: Authorization, so the token response gets its own key and the
        cookieless anonymous client cannot read it.
        """
        token_client, _, anonymous = self._clients()

        token_response = token_client.get(
            "/profile/", HTTP_AUTHORIZATION="Bearer secret-token"
        )
        self.assertEqual(token_response.content, b"profile:anonymous")
        self.assertTrue(has_vary_header(token_response, "Authorization"))
        self.assertTrue(has_vary_header(token_response, "Cookie"))

        registry = pickle.loads(next(iter(self._header_entries().values())))
        self.assertEqual(registry, ["HTTP_AUTHORIZATION", "HTTP_COOKIE"])

        # The anonymous client must hit/miss independently and never see the
        # tokenized response.
        anon_response = anonymous.get("/profile/")
        self.assertEqual(anon_response.content, b"profile:anonymous")
        self.assertFalse(has_vary_header(anon_response, "Authorization"))
        stored_bodies = {
            self._stored_page(key).content for key in self._page_entries()
        }
        self.assertEqual(len(stored_bodies), 1)
        self.assertEqual(stored_bodies, {b"profile:anonymous"})

        # A second token request is a real cache hit under its own key.
        with mock.patch(
            "cache_privacy.views.profile_view",
            side_effect=AssertionError("view must not run on a cache hit"),
        ):
            repeat = token_client.get(
                "/profile/", HTTP_AUTHORIZATION="Bearer secret-token"
            )
        self.assertEqual(repeat.status_code, 200)

    def test_two_distinct_authorization_tokens_get_distinct_entries(self):
        """Different bearer tokens never resolve to the same cached page."""
        token_a = Client()
        token_b = Client()

        response_a = token_a.get(
            "/profile/", HTTP_AUTHORIZATION="Bearer token-A"
        )
        response_b = token_b.get(
            "/profile/", HTTP_AUTHORIZATION="Bearer token-B"
        )
        self.assertTrue(has_vary_header(response_a, "Authorization"))
        self.assertTrue(has_vary_header(response_b, "Authorization"))

        # Same URL, same absent cookie marker, but distinct tokens: the two
        # entries must live under distinct keys that must never be exchanged.
        pages = self._page_entries()
        self.assertEqual(len(pages), 2)
        key_hashes = {key.rsplit(".", 2)[-2] for key in pages}
        self.assertEqual(len(key_hashes), 2)
        repeat_b = token_b.get(
            "/profile/", HTTP_AUTHORIZATION="Bearer token-B"
        )
        self.assertEqual(repeat_b.content, b"profile:anonymous")
        self.assertEqual(len(self._page_entries()), 2)

    def test_explicit_public_allows_sharing_authorization_response(self):
        """
        Cache-Control: public is the application's opt-in to sharing the
        response to an authenticated request; the framework must not force
        Vary: Authorization and a single entry must serve both clients.
        """
        token_client, _, anonymous = self._clients()

        token_response = token_client.get(
            "/public-token/", HTTP_AUTHORIZATION="Bearer token"
        )
        self.assertIn("public", token_response["Cache-Control"])
        self.assertFalse(has_vary_header(token_response, "Authorization"))
        self.assertEqual(len(self._page_entries()), 1)

        with mock.patch(
            "cache_privacy.views.public_token_view",
            side_effect=AssertionError("public page must be served from cache"),
        ):
            anon_response = anonymous.get("/public-token/")
        self.assertEqual(anon_response.content, b"public-token:shared-with-everyone")
        self.assertEqual(len(self._page_entries()), 1)

    def test_public_response_is_shared_across_all_clients(self):
        """
        The successful-hit control: a genuinely public, identity-independent
        page with Cache-Control: public is cached once and shared by every
        client -- the fix must not disable the cache wholesale.
        """
        token_client, client_a, anonymous = self._clients()
        self._set_identity(client_a, "user-A")

        first = anonymous.get("/public/")
        self.assertEqual(first.content, b"public:shared-with-everyone")
        self.assertIn("public", first["Cache-Control"])
        self.assertEqual(len(self._page_entries()), 1)
        self.assertEqual(len(self._header_entries()), 1)

        for client, kwargs in (
            (client_a, {}),
            (token_client, {"HTTP_AUTHORIZATION": "Bearer token"}),
        ):
            with self.subTest(client=client):
                with mock.patch(
                    "cache_privacy.views.public_view",
                    side_effect=AssertionError(
                        "public view must be served from cache"
                    ),
                ):
                    response = client.get("/public/", **kwargs)
                self.assertEqual(response.content, b"public:shared-with-everyone")

        # Still exactly one shared entry.
        self.assertEqual(len(self._page_entries()), 1)

    # ----- controllable clock: expiry --------------------------------------

    def test_entry_expires_under_controllable_clock(self):
        client_a, _, _ = self._clients()
        self._set_identity(client_a, "user-A")

        with self._frozen_time(1_000_000):
            response = client_a.get("/profile/")
            self.assertEqual(response.content, b"profile:user-A")
            self.assertEqual(len(self._page_entries()), 1)
            stored_key = next(iter(self._page_entries()))

        # Within the 30s window: still a hit, no rebuild.
        with self._frozen_time(1_000_029):
            hit = client_a.get("/profile/")
            self.assertEqual(hit.content, b"profile:user-A")
            self.assertEqual(len(self._page_entries()), 1)

        # Past the timeout LocMem marks the entry expired; eviction is lazy,
        # so the next get() drops the stale entry and the view rebuilds the
        # page under the same request-derived key with a fresh expiry.
        with self._frozen_time(1_000_031):
            self.assertTrue(self.locmem._has_expired(stored_key))
            rebuilt = client_a.get("/profile/")
            self.assertEqual(rebuilt.content, b"profile:user-A")
            self.assertEqual(len(self._page_entries()), 1)
            self.assertEqual(self.locmem._expire_info[stored_key], 1_000_061)


@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "cache-privacy-regression-wrong",
            "KEY_PREFIX": "privacy",
        }
    },
    CACHE_MIDDLEWARE_KEY_PREFIX="",
    CACHE_MIDDLEWARE_SECONDS=30,
    SESSION_ENGINE="django.contrib.sessions.backends.signed_cookies",
    ROOT_URLCONF="cache_privacy.urls",
    MIDDLEWARE=MIDDLEWARE_WRONG_ORDER,
)
class MisconfiguredOrderLeakTests(SimpleTestCase):
    """
    Observable failure mode when SessionMiddleware sits outside the cache
    pair. The full cross-client request sequence is preserved so the leak can
    be reproduced; these tests document the behavior rather than assert it as
    desirable. No library patch can recover Vary: Cookie that was never
    visible to UpdateCacheMiddleware -- correct ordering is the application's
    responsibility.
    """

    def setUp(self):
        from django.core.cache import caches

        self.locmem = caches["default"]
        self.locmem.clear()

    def tearDown(self):
        self.locmem.clear()

    def _page_entries(self):
        return {
            key: value
            for key, value in self.locmem._cache.items()
            if CACHE_PAGES_PREFIX in key
        }

    def test_user_b_and_anonymous_receive_user_a_page(self):
        client_a, client_b, anonymous = Client(), Client(), Client()
        for client, identity in ((client_a, "user-A"), (client_b, "user-B")):
            session = client.session
            session["identity"] = identity
            session.save()
            client.cookies[settings.SESSION_COOKIE_NAME] = session.session_key

        # A populates the single, session-agnostic cache entry.
        response_a = client_a.get("/profile/")
        self.assertEqual(response_a.content, b"profile:user-A")
        pages = self._page_entries()
        self.assertEqual(len(pages), 1)

        # The leak: B and the anonymous client are served A's body from the
        # one shared entry instead of building their own.
        response_b = client_b.get("/profile/")
        response_anon = anonymous.get("/profile/")
        self.assertEqual(response_b.content, b"profile:user-A")
        self.assertEqual(response_anon.content, b"profile:user-A")
        # No new entries were created for B or the anonymous client.
        self.assertEqual(len(self._page_entries()), 1)

    def test_first_visit_cookie_response_gets_shared_when_order_is_wrong(self):
        """
        With the wrong ordering UpdateCacheMiddleware snapshots the response
        before SessionMiddleware runs: the stored object carries neither
        ``Vary: Cookie`` nor the ``Set-Cookie`` header, so the next cookieless
        visitor is served the body rendered for A's freshly minted session and
        never receives its own cookie.
        """
        first_visitor = Client()
        first = first_visitor.get("/first-visit/")
        self.assertEqual(first.content, b"first-visit:user-A")
        self.assertIn(settings.SESSION_COOKIE_NAME, first.cookies)

        pages = self._page_entries()
        self.assertEqual(len(pages), 1)
        stored = pickle.loads(next(iter(pages.values())))
        # The snapshot predates SessionMiddleware's response processing.
        self.assertIsNone(stored.get("Vary"))
        self.assertEqual(stored.cookies, {})

        second_visitor = Client()
        poisoned = second_visitor.get("/first-visit/")
        # A's body, delivered to a different cookieless client from the single
        # shared entry, and no session cookie is ever issued to them.
        self.assertEqual(poisoned.status_code, 200)
        self.assertEqual(poisoned.content, b"first-visit:user-A")
        self.assertNotIn(settings.SESSION_COOKIE_NAME, poisoned.cookies)
        self.assertEqual(len(self._page_entries()), 1)


@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "cache-privacy-regression-nosession",
            "KEY_PREFIX": "privacy",
        }
    },
    CACHE_MIDDLEWARE_KEY_PREFIX="",
    CACHE_MIDDLEWARE_SECONDS=30,
    SESSION_ENGINE="django.contrib.sessions.backends.signed_cookies",
    ROOT_URLCONF="cache_privacy.urls",
    MIDDLEWARE=MIDDLEWARE_NO_SESSION,
)
class NoSessionMiddlewareLeakTests(SimpleTestCase):
    """
    Without SessionMiddleware the framework has no signal that the response
    is identity-dependent. A view that personalizes on a raw cookie without
    declaring its own policy is cached once for everyone. The application
    owns this decision; declaring Cache-Control: private closes the leak and
    is demonstrated in the second test.
    """

    def setUp(self):
        from django.core.cache import caches

        self.locmem = caches["default"]
        self.locmem.clear()

    def tearDown(self):
        self.locmem.clear()

    def _page_entries(self):
        return {
            key: value
            for key, value in self.locmem._cache.items()
            if CACHE_PAGES_PREFIX in key
        }

    def test_undeclared_raw_cookie_identity_is_shared(self):
        """The full cross-client sequence for an undeclared identity page."""
        client_a = Client()
        client_a.cookies["identity"] = "user-A"
        client_b = Client()
        client_b.cookies["identity"] = "user-B"

        # A's personalized body populates the single, cookie-agnostic entry.
        first = client_a.get("/raw-cookie/")
        self.assertEqual(first.content, b"raw-cookie:user-A")
        self.assertEqual(len(self._page_entries()), 1)

        # B receives A's body: nothing in the key distinguishes the cookies.
        second = client_b.get("/raw-cookie/")
        self.assertEqual(second.content, b"raw-cookie:user-A")
        self.assertEqual(len(self._page_entries()), 1)

        # A cookieless visitor is poisoned the same way.
        anonymous = Client()
        third = anonymous.get("/raw-cookie/")
        self.assertEqual(third.content, b"raw-cookie:user-A")
        self.assertEqual(len(self._page_entries()), 1)

    def test_application_can_close_leak_with_private_policy(self):
        client_a = Client()
        client_a.cookies["identity"] = "user-A"
        client_b = Client()
        client_b.cookies["identity"] = "user-B"

        first = client_a.get("/raw-cookie-private/")
        self.assertEqual(first.content, b"raw-cookie-private:user-A")
        self.assertEqual(self._page_entries(), {})

        second = client_b.get("/raw-cookie-private/")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.content, b"raw-cookie-private:user-B")
        self.assertEqual(self._page_entries(), {})
