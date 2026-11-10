"""
Sync/async behavior comparison for ``RemoteUserMiddleware``.

The middleware runs either :meth:`RemoteUserMiddleware.process_request`
(WSGI, or an ASGI stack forced into sync execution by a later sync-only
middleware) or :meth:`RemoteUserMiddleware.aprocess_request` (ASGI). Both
paths implement the same declared semantics:

* a present, trusted ``REMOTE_USER`` authenticates and logs the user in;
* the same authenticated user on a later request is left untouched;
* a different header value removes the remote user before re-authenticating;
* a missing header logs a remote-user out only when
  ``force_logout_if_no_header`` is true;
* users authenticated through another backend are never removed.

These tests pin both paths to that contract and observe the request user
caches (``request._cached_user`` / ``request._acached_user``), the backend
stored in the session, the ``user_logged_in`` / ``user_logged_out`` /
``user_login_failed`` signals and the session key.

Trusted values are only supplied through the test client's legitimate
entry points: the ``REMOTE_USER`` WSGI environ key (``**extra``) or the
``REMOTE-USER`` HTTP header (``headers=...``), which the ASGI handler
normalizes to ``HTTP_REMOTE_USER``. An HTTP client sending a
``Remote-User`` header is not the same as a web server populating the
trusted ``REMOTE_USER`` WSGI variable: under WSGI a non ``HTTP_`` META key
can only be set by the server, so the default header is not spoofable by a
client, whereas under ASGI every value arrives as an ``HTTP_`` header and
must therefore be sanitized by a reverse proxy.
"""

from importlib import import_module

from asgiref.sync import AsyncToSync

from django.conf import settings
from django.contrib import auth
from django.contrib.auth.backends import RemoteUserBackend
from django.contrib.auth.middleware import (
    AuthenticationMiddleware,
    RemoteUserMiddleware,
)
from django.contrib.auth.models import User
from django.contrib.auth.signals import (
    user_logged_in,
    user_logged_out,
    user_login_failed,
)
from django.test import (
    AsyncClient,
    AsyncRequestFactory,
    Client,
    RequestFactory,
    TestCase,
    modify_settings,
    override_settings,
)

REMOTE_BACKEND = "auth_tests.test_remote_user_middleware.AliasRemoteUserBackend"
NO_CREATE_BACKEND = (
    "auth_tests.test_remote_user_middleware.NoCreateRemoteUserBackend"
)
FAILING_CLEAN_BACKEND = (
    "auth_tests.test_remote_user_middleware.FailingCleanRemoteUserBackend"
)
MODEL_BACKEND = "django.contrib.auth.backends.ModelBackend"
MISSING_BACKEND = "auth_tests.test_remote_user_middleware.DoesNotExistBackend"
HEADER = "REMOTE_USER"


class AliasRemoteUserBackend(RemoteUserBackend):
    """``clean_username()`` maps an email-like value to a login alias."""

    def clean_username(self, username):
        return username.split("@")[0]


class NoCreateRemoteUserBackend(RemoteUserBackend):
    """Backend that never creates missing users and aliases the name."""

    create_unknown_user = False

    def clean_username(self, username):
        return username.split("@")[0]


class FailingCleanRemoteUserBackend(RemoteUserBackend):
    """Backend whose ``clean_username()`` refuses every value."""

    def clean_username(self, username):
        raise ValueError("username cleaning failed")


class SignalRecorder:
    """Collect auth signal payloads for the duration of a request flow."""

    def __init__(self, test_case):
        self.logged_in = []
        self.logged_out = []
        self.login_failed = []
        user_logged_in.connect(self._on_login)
        user_logged_out.connect(self._on_logout)
        user_login_failed.connect(self._on_failed)
        test_case.addCleanup(user_logged_in.disconnect, self._on_login)
        test_case.addCleanup(user_logged_out.disconnect, self._on_logout)
        test_case.addCleanup(user_login_failed.disconnect, self._on_failed)

    def _on_login(self, sender, request, user, **kwargs):
        self.logged_in.append(user.get_username())

    def _on_logout(self, sender, request, user, **kwargs):
        self.logged_out.append(None if user is None else user.get_username())

    def _on_failed(self, sender, credentials, request, **kwargs):
        self.login_failed.append(credentials.get("remote_user"))

    def names(self):
        return {
            "logged_in": list(self.logged_in),
            "logged_out": list(self.logged_out),
            "login_failed": list(self.login_failed),
        }


@override_settings(ROOT_URLCONF="auth_tests.urls")
@modify_settings(
    AUTHENTICATION_BACKENDS={"append": REMOTE_BACKEND},
    MIDDLEWARE={"append": "django.contrib.auth.middleware.RemoteUserMiddleware"},
)
class RemoteUserMiddlewareFlowTest(TestCase):
    """
    Walk the identity state machine (anonymous -> remote user -> same user
    again -> another user -> missing header) once per execution path.
    """

    def test_sync_identity_flow(self):
        client = Client()
        recorder = SignalRecorder(self)

        # Anonymous session: no header means no user and no session content.
        response = client.get("/remote_user/")
        self.assertTrue(response.context["user"].is_anonymous)
        self.assertNotIn(auth.SESSION_KEY, client.session)
        self.assertEqual(recorder.names(), {
            "logged_in": [], "logged_out": [], "login_failed": [],
        })

        # First trusted REMOTE_USER: user is created, logged in and persisted.
        anonymous_key = client.session.session_key
        response = client.get("/remote_user/", **{HEADER: "alice@example.com"})
        self.assertEqual(response.context["user"].get_username(), "alice")
        self.assertEqual(
            client.session[auth.BACKEND_SESSION_KEY], REMOTE_BACKEND
        )
        # Logging in cycles the anonymous session key.
        self.assertNotEqual(client.session.session_key, anonymous_key)
        self.assertEqual(recorder.logged_in, ["alice"])
        alice_key = client.session.session_key

        # The same header on a later request must be a no-op: no signals, no
        # new session and no second last_login update.
        recorder.logged_in.clear()
        response = client.get("/remote_user/", **{HEADER: "alice@example.com"})
        self.assertEqual(response.context["user"].get_username(), "alice")
        self.assertEqual(client.session.session_key, alice_key)
        self.assertEqual(recorder.names(), {
            "logged_in": [], "logged_out": [], "login_failed": [],
        })

        # The header now names another person: alice is logged out before bob
        # is authenticated, and the session is flushed and recreated.
        response = client.get("/remote_user/", **{HEADER: "bob@example.com"})
        self.assertEqual(response.context["user"].get_username(), "bob")
        self.assertEqual(recorder.logged_out, ["alice"])
        self.assertEqual(recorder.logged_in, ["bob"])
        self.assertNotEqual(client.session.session_key, alice_key)

        # The header disappears within the same browser session: the remote
        # user must be logged out and the session flushed.
        response = client.get("/remote_user/")
        self.assertTrue(response.context["user"].is_anonymous)
        self.assertEqual(recorder.logged_out, ["alice", "bob"])
        self.assertEqual(dict(client.session), {})

    async def test_async_identity_flow(self):
        client = AsyncClient()
        recorder = SignalRecorder(self)

        response = await client.get("/remote_user/")
        session = await client.asession()
        self.assertTrue(response.context["user"].is_anonymous)
        self.assertFalse(await session.ahas_key(auth.SESSION_KEY))
        self.assertEqual(recorder.names(), {
            "logged_in": [], "logged_out": [], "login_failed": [],
        })

        anonymous_key = session.session_key
        response = await client.get(
            "/remote_user/", headers={HEADER: "alice@example.com"}
        )
        session = await client.asession()
        self.assertEqual(response.context["user"].get_username(), "alice")
        self.assertEqual(
            await session.aget(auth.BACKEND_SESSION_KEY), REMOTE_BACKEND
        )
        # Logging in cycles the anonymous session key.
        self.assertNotEqual(session.session_key, anonymous_key)
        self.assertEqual(recorder.logged_in, ["alice"])
        alice_key = session.session_key

        # The same header on a later request must be a no-op: no signals, no
        # new session and no second last_login update.
        recorder.logged_in.clear()
        response = await client.get(
            "/remote_user/", headers={HEADER: "alice@example.com"}
        )
        session = await client.asession()
        self.assertEqual(response.context["user"].get_username(), "alice")
        self.assertEqual(session.session_key, alice_key)
        self.assertEqual(recorder.names(), {
            "logged_in": [], "logged_out": [], "login_failed": [],
        })

        # The header now names another person: alice is logged out before bob
        # is authenticated, and the session is flushed and recreated.
        response = await client.get(
            "/remote_user/", headers={HEADER: "bob@example.com"}
        )
        session = await client.asession()
        self.assertEqual(response.context["user"].get_username(), "bob")
        self.assertEqual(recorder.logged_out, ["alice"])
        self.assertEqual(recorder.logged_in, ["bob"])
        self.assertNotEqual(session.session_key, alice_key)

        # The header disappears within the same browser session: the remote
        # user must be logged out and the session flushed.
        response = await client.get("/remote_user/")
        session = await client.asession()
        self.assertTrue(response.context["user"].is_anonymous)
        self.assertEqual(recorder.logged_out, ["alice", "bob"])
        self.assertEqual(list(await session.akeys()), [])


@override_settings(ROOT_URLCONF="auth_tests.urls")
@modify_settings(
    AUTHENTICATION_BACKENDS={"append": FAILING_CLEAN_BACKEND},
    MIDDLEWARE={"append": "django.contrib.auth.middleware.RemoteUserMiddleware"},
)
class RemoteUserCleaningFailureTest(TestCase):
    """
    ``clean_username()`` is only consulted while comparing an already
    authenticated user with a new header value. A failure there must behave
    identically on both paths and must not leave the previous user silently
    representing the new identity.
    """

    def test_sync_cleaning_failure_does_not_swap_identity(self):
        user = User.objects.create(username="alice")
        client = Client()
        client.force_login(user, backend=FAILING_CLEAN_BACKEND)
        recorder = SignalRecorder(self)

        # Force the mismatch path where clean_username() is consulted.
        with self.assertRaises(ValueError):
            client.get("/remote_user/", **{HEADER: "bob"})

        # No login/logout signal fired and the previous identity survives in
        # the session untouched: the failed swap never committed.
        self.assertEqual(recorder.names(), {
            "logged_in": [], "logged_out": [], "login_failed": [],
        })
        session = client.session
        self.assertEqual(session[auth.SESSION_KEY], str(user.pk))
        self.assertEqual(session[auth.BACKEND_SESSION_KEY], FAILING_CLEAN_BACKEND)

    async def test_async_cleaning_failure_does_not_swap_identity(self):
        user = await User.objects.acreate(username="alice")
        client = AsyncClient()
        await client.aforce_login(user, backend=FAILING_CLEAN_BACKEND)
        recorder = SignalRecorder(self)

        with self.assertRaises(ValueError):
            await client.get("/remote_user/", headers={HEADER: "bob"})

        self.assertEqual(recorder.names(), {
            "logged_in": [], "logged_out": [], "login_failed": [],
        })
        session = await client.asession()
        self.assertEqual(await session.aget(auth.SESSION_KEY), str(user.pk))
        self.assertEqual(
            await session.aget(auth.BACKEND_SESSION_KEY),
            FAILING_CLEAN_BACKEND,
        )


@override_settings(ROOT_URLCONF="auth_tests.urls")
@modify_settings(
    AUTHENTICATION_BACKENDS={"append": NO_CREATE_BACKEND},
    MIDDLEWARE={"append": "django.contrib.auth.middleware.RemoteUserMiddleware"},
)
class RemoteUserSwitchFailureTest(TestCase):
    """
    When the header names a different person but the backend cannot
    authenticate that person, the previous remote user must already have been
    removed from the request, so the request finishes anonymous instead of
    carrying the old identity under the new header.
    """

    def test_sync_failed_switch_leaves_request_anonymous(self):
        User.objects.create(username="alice")
        client = Client()
        recorder = SignalRecorder(self)

        response = client.get("/remote_user/", **{HEADER: "alice@example.com"})
        self.assertEqual(response.context["user"].get_username(), "alice")
        self.assertEqual(recorder.logged_in, ["alice"])

        # Unknown user, backend refuses to create: alice is logged out first,
        # authentication then fails and the request stays anonymous.
        response = client.get("/remote_user/", **{HEADER: "bob@example.com"})
        self.assertTrue(response.context["user"].is_anonymous)
        self.assertEqual(recorder.logged_out, ["alice"])
        self.assertEqual(recorder.login_failed, ["bob@example.com"])
        self.assertEqual(dict(client.session), {})

    async def test_async_failed_switch_leaves_request_anonymous(self):
        await User.objects.acreate(username="alice")
        client = AsyncClient()
        recorder = SignalRecorder(self)

        response = await client.get(
            "/remote_user/", headers={HEADER: "alice@example.com"}
        )
        self.assertEqual(response.context["user"].get_username(), "alice")
        self.assertEqual(recorder.logged_in, ["alice"])

        response = await client.get(
            "/remote_user/", headers={HEADER: "bob@example.com"}
        )
        self.assertTrue(response.context["user"].is_anonymous)
        self.assertEqual(recorder.logged_out, ["alice"])
        self.assertEqual(recorder.login_failed, ["bob@example.com"])
        self.assertEqual(list(await (await client.asession()).akeys()), [])


@override_settings(ROOT_URLCONF="auth_tests.urls")
@modify_settings(
    AUTHENTICATION_BACKENDS={"append": REMOTE_BACKEND},
    MIDDLEWARE={"append": "django.contrib.auth.middleware.RemoteUserMiddleware"},
)
class RemoteUserRequestCacheTest(TestCase):
    """
    The sync ``request.user`` lazy object and the async ``request.auser()``
    coroutine keep independent per-request caches; login/logout performed by
    the middleware rebinds both entry points so later access cannot return a
    stale user.
    """

    def test_sync_cache_rebound_after_logout(self):
        User.objects.create(username="alice")
        client = Client()
        client.get("/remote_user/", **{HEADER: "alice@example.com"})

        # The next request loses the header; the middleware logs alice out.
        response = client.get("/remote_user/")
        self.assertTrue(response.context["user"].is_anonymous)
        # The view's context processor resolved request.user after logout, so
        # the rebound lazy object must not resurrect alice.
        self.assertNotIn(auth.SESSION_KEY, client.session)

    async def test_async_cache_rebound_after_logout(self):
        await User.objects.acreate(username="alice")
        client = AsyncClient()
        await client.get("/remote_user/", headers={HEADER: "alice@example.com"})

        response = await client.get("/remote_user/")
        self.assertTrue(response.context["user"].is_anonymous)
        self.assertFalse(
            await (await client.asession()).ahas_key(auth.SESSION_KEY)
        )


@override_settings(ROOT_URLCONF="auth_tests.urls")
@modify_settings(
    AUTHENTICATION_BACKENDS={"append": REMOTE_BACKEND},
    MIDDLEWARE={"append": "django.contrib.auth.middleware.RemoteUserMiddleware"},
)
class RemoteUserMiddlewareDirectRequestTest(TestCase):
    """
    Exercise the middleware on hand-built requests to inspect the per-request
    user caches. ``auth.login()``/``auth.logout()`` rebind both entry points
    via ``_set_auth_user()`` and drop the ``_cached_user`` /
    ``_acached_user`` slots, so neither the rebound entry point nor a later
    resolution can resurrect a user that was resolved before the change.
    """

    def _session(self, user=None, backend_path=REMOTE_BACKEND):
        engine = self.session_engine
        session = engine.SessionStore()
        if user is not None:
            session[auth.SESSION_KEY] = str(user.pk)
            session[auth.BACKEND_SESSION_KEY] = backend_path
            session[auth.HASH_SESSION_KEY] = user.get_session_auth_hash()
        return session

    def setUp(self):
        self.session_engine = import_module(settings.SESSION_ENGINE)

    def test_sync_process_request_login_rebinds_user(self):
        user = User.objects.create(username="alice")
        request = RequestFactory().get("/remote_user/", REMOTE_USER="alice")
        request.session = self._session()
        AuthenticationMiddleware(lambda req: None).process_request(request)
        # Resolve the anonymous lazy object first.
        self.assertTrue(request.user.is_anonymous)
        self.assertTrue(hasattr(request, "_cached_user"))

        RemoteUserMiddleware(lambda req: None).process_request(request)

        # The stale anonymous cache slot was dropped and the rebound entry
        # point now holds the concrete logged-in user directly.
        self.assertFalse(hasattr(request, "_cached_user"))
        self.assertEqual(request.user.get_username(), "alice")
        self.assertEqual(request.user, user)
        self.assertEqual(request.session[auth.BACKEND_SESSION_KEY], REMOTE_BACKEND)

    def test_sync_process_request_logout_rebinds_user(self):
        user = User.objects.create(username="alice")
        request = RequestFactory().get("/remote_user/")
        request.session = self._session(user=user)
        AuthenticationMiddleware(lambda req: None).process_request(request)
        # Force the lazy object to resolve and populate _cached_user.
        self.assertEqual(request.user.get_username(), "alice")
        self.assertTrue(hasattr(request, "_cached_user"))

        RemoteUserMiddleware(lambda req: None).process_request(request)

        self.assertTrue(request.user.is_anonymous)
        self.assertNotIn(auth.SESSION_KEY, request.session)
        # The authenticated user must not linger in the per-request cache.
        self.assertFalse(hasattr(request, "_cached_user"))

    async def test_async_process_request_login_rebinds_auser(self):
        user = await User.objects.acreate(username="alice")
        request = AsyncRequestFactory().get(
            "/remote_user/", REMOTE_USER="alice"
        )
        request.session = self._session()
        AuthenticationMiddleware(lambda req: None).process_request(request)
        self.assertTrue((await request.auser()).is_anonymous)
        self.assertTrue(hasattr(request, "_acached_user"))

        await RemoteUserMiddleware(lambda req: None).aprocess_request(request)

        # The stale anonymous cache slot was dropped; the rebound auser
        # callable returns the logged-in user directly.
        self.assertFalse(hasattr(request, "_acached_user"))
        self.assertEqual((await request.auser()).get_username(), "alice")
        self.assertEqual(await request.auser(), user)
        self.assertEqual(
            await request.session.aget(auth.BACKEND_SESSION_KEY), REMOTE_BACKEND
        )

    async def test_async_process_request_logout_rebinds_auser(self):
        user = await User.objects.acreate(username="alice")
        request = AsyncRequestFactory().get("/remote_user/")
        request.session = self._session(user=user)
        AuthenticationMiddleware(lambda req: None).process_request(request)
        self.assertEqual((await request.auser()).get_username(), "alice")
        self.assertTrue(hasattr(request, "_acached_user"))

        await RemoteUserMiddleware(lambda req: None).aprocess_request(request)

        self.assertTrue((await request.auser()).is_anonymous)
        self.assertFalse(await request.session.ahas_key(auth.SESSION_KEY))
        # The authenticated user must not linger in the per-request cache.
        self.assertFalse(hasattr(request, "_acached_user"))



@override_settings(ROOT_URLCONF="auth_tests.urls")
@modify_settings(
    AUTHENTICATION_BACKENDS={"append": REMOTE_BACKEND},
    MIDDLEWARE={"append": "django.contrib.auth.middleware.RemoteUserMiddleware"},
)
class RemoteUserWithModelBackendTest(TestCase):
    """
    Remote-user authentication coexists with password login. A missing header
    must not log users out indiscriminately: a session established through the
    plain ``ModelBackend`` is left alone, while a header that a remote backend
    can resolve takes over the session. The complementary case where the remote
    backend rejects the name is covered by
    ``RemoteUserRejectedHeaderWithModelBackendTest``.
    """

    def test_sync_model_backend_user_survives_missing_header(self):
        User.objects.create_user(username="modeluser", password="foo")
        client = Client()
        self.assertTrue(client.login(username="modeluser", password="foo"))
        recorder = SignalRecorder(self)

        # No header: the password-authenticated session is retained untouched.
        response = client.get("/remote_user/")
        self.assertEqual(response.context["user"].get_username(), "modeluser")
        self.assertEqual(
            client.session[auth.BACKEND_SESSION_KEY], MODEL_BACKEND
        )
        self.assertEqual(recorder.names(), {
            "logged_in": [], "logged_out": [], "login_failed": [],
        })

    async def test_async_model_backend_user_survives_missing_header(self):
        await User.objects.acreate_user(username="modeluser", password="foo")
        client = AsyncClient()
        self.assertTrue(await client.alogin(username="modeluser", password="foo"))
        recorder = SignalRecorder(self)

        response = await client.get("/remote_user/")
        self.assertEqual(response.context["user"].get_username(), "modeluser")
        session = await client.asession()
        self.assertEqual(await session.aget(auth.BACKEND_SESSION_KEY), MODEL_BACKEND)
        self.assertEqual(recorder.names(), {
            "logged_in": [], "logged_out": [], "login_failed": [],
        })

    def test_sync_resolvable_header_takes_over_session(self):
        User.objects.create_user(username="modeluser", password="foo")
        client = Client()
        client.login(username="modeluser", password="foo")
        recorder = SignalRecorder(self)

        # A header the remote backend can resolve takes over the session by
        # logging the remote user in; the password user is never sent a logout
        # signal because only RemoteUserBackend sessions are removed.
        response = client.get("/remote_user/", **{HEADER: "stranger"})
        self.assertEqual(response.context["user"].get_username(), "stranger")
        self.assertEqual(
            client.session[auth.BACKEND_SESSION_KEY], REMOTE_BACKEND
        )
        self.assertEqual(recorder.logged_out, [])
        self.assertEqual(recorder.logged_in, ["stranger"])

    async def test_async_resolvable_header_takes_over_session(self):
        await User.objects.acreate_user(username="modeluser", password="foo")
        client = AsyncClient()
        await client.alogin(username="modeluser", password="foo")
        recorder = SignalRecorder(self)

        response = await client.get(
            "/remote_user/", headers={HEADER: "stranger"}
        )
        self.assertEqual(response.context["user"].get_username(), "stranger")
        session = await client.asession()
        self.assertEqual(
            await session.aget(auth.BACKEND_SESSION_KEY), REMOTE_BACKEND
        )
        self.assertEqual(recorder.logged_out, [])
        self.assertEqual(recorder.logged_in, ["stranger"])


@override_settings(ROOT_URLCONF="auth_tests.urls")
@modify_settings(
    AUTHENTICATION_BACKENDS={"append": NO_CREATE_BACKEND},
    MIDDLEWARE={"append": "django.contrib.auth.middleware.RemoteUserMiddleware"},
)
class RemoteUserRejectedHeaderWithModelBackendTest(TestCase):
    """
    With a remote backend that refuses unknown users, a header naming an
    unknown person must neither log the password user out nor let the request
    appear authenticated as the rejected remote identity.
    """

    def test_sync_rejected_header_preserves_model_backend_user(self):
        User.objects.create_user(username="modeluser", password="foo")
        client = Client()
        client.login(username="modeluser", password="foo")
        recorder = SignalRecorder(self)

        response = client.get("/remote_user/", **{HEADER: "stranger@example.com"})

        self.assertEqual(response.context["user"].get_username(), "modeluser")
        self.assertEqual(
            client.session[auth.BACKEND_SESSION_KEY], MODEL_BACKEND
        )
        self.assertEqual(recorder.logged_out, [])
        self.assertEqual(
            recorder.login_failed, ["stranger@example.com"]
        )

    async def test_async_rejected_header_preserves_model_backend_user(self):
        await User.objects.acreate_user(username="modeluser", password="foo")
        client = AsyncClient()
        await client.alogin(username="modeluser", password="foo")
        recorder = SignalRecorder(self)

        response = await client.get(
            "/remote_user/", headers={HEADER: "stranger@example.com"}
        )

        self.assertEqual(response.context["user"].get_username(), "modeluser")
        session = await client.asession()
        self.assertEqual(await session.aget(auth.BACKEND_SESSION_KEY), MODEL_BACKEND)
        self.assertEqual(recorder.logged_out, [])
        self.assertEqual(
            recorder.login_failed, ["stranger@example.com"]
        )


@override_settings(ROOT_URLCONF="auth_tests.urls")
@modify_settings(
    AUTHENTICATION_BACKENDS={"append": REMOTE_BACKEND},
    MIDDLEWARE={"append": "django.contrib.auth.middleware.RemoteUserMiddleware"},
)
class RemoteUserStoredBackendRemovedTest(TestCase):
    """
    Behavior when the backend path persisted in the session can no longer be
    imported.

    Without a header, ``auth.get_user()``/``aget_user()`` cannot resolve the
    user (the path is absent from ``AUTHENTICATION_BACKENDS``) so the request
    is anonymous before the remote-user middleware runs. The no-header branch
    therefore never reaches ``_remove_invalid_user()`` and the stale session
    is left untouched, identically on both paths.

    With a fresh header, the request is still anonymous when the middleware
    runs (the broken path cannot resolve a user), so the mismatch branch is
    skipped and the new remote user is logged in directly. ``login()`` flushes
    the stale session and recreates it under the working backend; no
    ``user_logged_out`` signal is emitted because there was no authenticated
    user to log out.
    """

    def _corrupt_session_backend(self, client):
        session = client.session
        session[auth.BACKEND_SESSION_KEY] = MISSING_BACKEND
        session.save()
        client.cookies[settings.SESSION_COOKIE_NAME] = session.session_key

    async def _acorrupt_session_backend(self, client):
        session = await client.asession()
        await session.aset(auth.BACKEND_SESSION_KEY, MISSING_BACKEND)
        await session.asave()
        client.cookies[settings.SESSION_COOKIE_NAME] = session.session_key

    def test_sync_unimportable_backend_without_header_is_anonymous(self):
        user = User.objects.create(username="alice")
        client = Client()
        client.force_login(user, backend=REMOTE_BACKEND)
        self._corrupt_session_backend(client)
        recorder = SignalRecorder(self)

        response = client.get("/remote_user/")

        self.assertTrue(response.context["user"].is_anonymous)
        # The unresolvable session is retained; no logout signal is fired.
        self.assertEqual(
            client.session[auth.BACKEND_SESSION_KEY], MISSING_BACKEND
        )
        self.assertEqual(recorder.names(), {
            "logged_in": [], "logged_out": [], "login_failed": [],
        })

    async def test_async_unimportable_backend_without_header_is_anonymous(self):
        user = await User.objects.acreate(username="alice")
        client = AsyncClient()
        await client.aforce_login(user, backend=REMOTE_BACKEND)
        await self._acorrupt_session_backend(client)
        recorder = SignalRecorder(self)

        response = await client.get("/remote_user/")

        self.assertTrue(response.context["user"].is_anonymous)
        session = await client.asession()
        self.assertEqual(
            await session.aget(auth.BACKEND_SESSION_KEY), MISSING_BACKEND
        )
        self.assertEqual(recorder.names(), {
            "logged_in": [], "logged_out": [], "login_failed": [],
        })

    def test_sync_unimportable_backend_with_new_header_relogins(self):
        user = User.objects.create(username="alice")
        client = Client()
        client.force_login(user, backend=REMOTE_BACKEND)
        stale_key = client.session.session_key
        self._corrupt_session_backend(client)
        recorder = SignalRecorder(self)

        response = client.get("/remote_user/", **{HEADER: "bob"})

        self.assertEqual(response.context["user"].get_username(), "bob")
        self.assertEqual(
            client.session[auth.BACKEND_SESSION_KEY], REMOTE_BACKEND
        )
        # The stale session is flushed and recreated by login(), without a
        # logout signal since the broken session resolved to anonymous.
        self.assertNotEqual(client.session.session_key, stale_key)
        self.assertEqual(recorder.logged_out, [])
        self.assertEqual(recorder.logged_in, ["bob"])

    async def test_async_unimportable_backend_with_new_header_relogins(self):
        user = await User.objects.acreate(username="alice")
        client = AsyncClient()
        await client.aforce_login(user, backend=REMOTE_BACKEND)
        stale_key = (await client.asession()).session_key
        await self._acorrupt_session_backend(client)
        recorder = SignalRecorder(self)

        response = await client.get("/remote_user/", headers={HEADER: "bob"})

        self.assertEqual(response.context["user"].get_username(), "bob")
        session = await client.asession()
        self.assertEqual(
            await session.aget(auth.BACKEND_SESSION_KEY), REMOTE_BACKEND
        )
        self.assertNotEqual(session.session_key, stale_key)
        self.assertEqual(recorder.logged_out, [])
        self.assertEqual(recorder.logged_in, ["bob"])


@override_settings(ROOT_URLCONF="auth_tests.urls")
@modify_settings(
    AUTHENTICATION_BACKENDS={"append": REMOTE_BACKEND},
    MIDDLEWARE={
        "append": "django.contrib.auth.middleware.PersistentRemoteUserMiddleware"
    },
)
class PersistentRemoteUserMiddlewareFlowTest(TestCase):
    """
    PersistentRemoteUserMiddleware sets force_logout_if_no_header=False: a
    missing header preserves the session, but a header naming a different user
    still forces the switch, on both execution paths.
    """

    def test_sync_persistent_session(self):
        User.objects.create(username="alice")
        client = Client()
        recorder = SignalRecorder(self)

        response = client.get("/remote_user/", **{HEADER: "alice@example.com"})
        self.assertEqual(response.context["user"].get_username(), "alice")
        self.assertEqual(recorder.logged_in, ["alice"])
        recorder.logged_in.clear()
        alice_key = client.session.session_key

        # No header on a later request: the session is retained untouched.
        response = client.get("/remote_user/")
        self.assertEqual(response.context["user"].get_username(), "alice")
        self.assertEqual(client.session.session_key, alice_key)
        self.assertEqual(recorder.logged_out, [])

        # A different header still forces the logout/login switch.
        response = client.get("/remote_user/", **{HEADER: "bob@example.com"})
        self.assertEqual(response.context["user"].get_username(), "bob")
        self.assertEqual(recorder.logged_out, ["alice"])
        self.assertEqual(recorder.logged_in, ["bob"])
        self.assertNotEqual(client.session.session_key, alice_key)

    async def test_async_persistent_session(self):
        await User.objects.acreate(username="alice")
        client = AsyncClient()
        recorder = SignalRecorder(self)

        response = await client.get(
            "/remote_user/", headers={HEADER: "alice@example.com"}
        )
        self.assertEqual(response.context["user"].get_username(), "alice")
        self.assertEqual(recorder.logged_in, ["alice"])
        recorder.logged_in.clear()
        alice_key = (await client.asession()).session_key

        response = await client.get("/remote_user/")
        self.assertEqual(response.context["user"].get_username(), "alice")
        self.assertEqual((await client.asession()).session_key, alice_key)
        self.assertEqual(recorder.logged_out, [])

        response = await client.get(
            "/remote_user/", headers={HEADER: "bob@example.com"}
        )
        self.assertEqual(response.context["user"].get_username(), "bob")
        self.assertEqual(recorder.logged_out, ["alice"])
        self.assertEqual(recorder.logged_in, ["bob"])
        self.assertNotEqual(
            (await client.asession()).session_key, alice_key
        )


@override_settings(ROOT_URLCONF="auth_tests.urls")
@modify_settings(
    AUTHENTICATION_BACKENDS={"append": REMOTE_BACKEND},
    MIDDLEWARE={"append": "django.contrib.auth.middleware.RemoteUserMiddleware"},
)
class RemoteUserTrustedEntryPointTest(TestCase):
    """
    The trusted identity only enters through the server-provided value.

    * Under WSGI the default key is the bare ``REMOTE_USER`` environ variable:
      a normal HTTP request header named ``Remote-User`` would surface as
      ``HTTP_REMOTE_USER`` and is therefore not read, so a client cannot forge
      the trusted variable.
    * Under ASGI there is no separate environ channel; the same header arrives
      as ``HTTP_REMOTE_USER`` and the middleware reads it explicitly, which is
      why an ASGI deployment must rely on a sanitizing reverse proxy.
    """

    def test_sync_reads_bare_remote_user_not_http_header(self):
        client = Client()

        # The legitimate server variable authenticates the user.
        response = client.get("/remote_user/", REMOTE_USER="alice")
        self.assertEqual(response.context["user"].get_username(), "alice")

        # A fresh, anonymous client sending the same-named HTTP header does not
        # authenticate, because the middleware looks up REMOTE_USER, not
        # HTTP_REMOTE_USER, on a WSGI request.
        client = Client()
        response = client.get("/remote_user/", HTTP_REMOTE_USER="mallory")
        self.assertTrue(response.context["user"].is_anonymous)
        self.assertNotIn(auth.SESSION_KEY, client.session)

    async def test_async_reads_normalized_http_remote_user(self):
        client = AsyncClient()

        # Under ASGI the client header is normalized to HTTP_REMOTE_USER and is
        # treated as the trusted value (proxy sanitization is assumed).
        response = await client.get(
            "/remote_user/", headers={"Remote-User": "alice"}
        )
        self.assertEqual(response.context["user"].get_username(), "alice")
        self.assertEqual(
            await (await client.asession()).aget(auth.BACKEND_SESSION_KEY),
            REMOTE_BACKEND,
        )

    def test_sync_custom_header_requires_explicit_http_meta_key(self):
        class CustomHeaderMiddleware(RemoteUserMiddleware):
            header = "HTTP_X_PROXY_REMOTE_USER"

        engine = import_module(settings.SESSION_ENGINE)
        request = RequestFactory().get(
            "/remote_user/", HTTP_X_PROXY_REMOTE_USER="alice"
        )
        request.session = engine.SessionStore()
        AuthenticationMiddleware(lambda req: None).process_request(request)
        CustomHeaderMiddleware(lambda req: None).process_request(request)

        self.assertEqual(request.user.get_username(), "alice")


@override_settings(ROOT_URLCONF="auth_tests.urls")
@modify_settings(
    AUTHENTICATION_BACKENDS={"append": REMOTE_BACKEND},
    MIDDLEWARE={
        "append": [
            "django.contrib.auth.middleware.RemoteUserMiddleware",
            "auth_tests.test_remote_user.sync_middleware",
        ]
    },
)
class RemoteUserAsgiSyncStackFlowTest(TestCase):
    """
    An async test method driven through a stack that contains a later sync-only
    middleware executes the sync ``process_request`` path even under ASGI. The
    observable identity transitions must match the pure sync and pure async
    paths.
    """

    def setUp(self):
        method = getattr(self, self._testMethodName)
        if not isinstance(method, AsyncToSync):
            self.skipTest("Only meaningful when wrapped for sync execution")

    async def test_identity_flow_through_sync_stack(self):
        client = AsyncClient()
        recorder = SignalRecorder(self)

        response = await client.get(
            "/remote_user/", headers={HEADER: "alice@example.com"}
        )
        self.assertEqual(response.context["user"].get_username(), "alice")
        self.assertEqual(recorder.logged_in, ["alice"])
        alice_key = (await client.asession()).session_key

        response = await client.get(
            "/remote_user/", headers={HEADER: "bob@example.com"}
        )
        self.assertEqual(response.context["user"].get_username(), "bob")
        self.assertEqual(recorder.logged_out, ["alice"])
        self.assertEqual(recorder.logged_in, ["alice", "bob"])
        self.assertNotEqual(
            (await client.asession()).session_key, alice_key
        )

        response = await client.get("/remote_user/")
        self.assertTrue(response.context["user"].is_anonymous)
        self.assertEqual(recorder.logged_out, ["alice", "bob"])
