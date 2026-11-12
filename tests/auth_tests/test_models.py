import asyncio
import threading
from unittest import mock

from django.conf.global_settings import PASSWORD_HASHERS
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.hashers import (
    PBKDF2PasswordHasher,
    acheck_password,
    check_password,
    get_hasher,
)
from django.contrib.auth.models import (
    AnonymousUser,
    Group,
    Permission,
    User,
    UserManager,
)
from django.contrib.contenttypes.models import ContentType
from django.core import mail
from django.db import connection, connections, migrations
from django.db.migrations.state import ModelState, ProjectState
from django.db.models.signals import post_save
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings

from .models import CustomEmailField, IntegerUsernameUser


class NaturalKeysTestCase(TestCase):
    def test_user_natural_key(self):
        staff_user = User.objects.create_user(username="staff")
        self.assertEqual(User.objects.get_by_natural_key("staff"), staff_user)
        self.assertEqual(staff_user.natural_key(), ("staff",))

    async def test_auser_natural_key(self):
        staff_user = await User.objects.acreate_user(username="staff")
        self.assertEqual(await User.objects.aget_by_natural_key("staff"), staff_user)
        self.assertEqual(staff_user.natural_key(), ("staff",))

    def test_group_natural_key(self):
        users_group = Group.objects.create(name="users")
        self.assertEqual(Group.objects.get_by_natural_key("users"), users_group)

    async def test_agroup_natural_key(self):
        users_group = await Group.objects.acreate(name="users")
        self.assertEqual(await Group.objects.aget_by_natural_key("users"), users_group)


class LoadDataWithoutNaturalKeysTestCase(TestCase):
    fixtures = ["regular.json"]

    def test_user_is_created_and_added_to_group(self):
        user = User.objects.get(username="my_username")
        group = Group.objects.get(name="my_group")
        self.assertEqual(group, user.groups.get())


class LoadDataWithNaturalKeysTestCase(TestCase):
    fixtures = ["natural.json"]

    def test_user_is_created_and_added_to_group(self):
        user = User.objects.get(username="my_username")
        group = Group.objects.get(name="my_group")
        self.assertEqual(group, user.groups.get())


class LoadDataWithNaturalKeysAndMultipleDatabasesTestCase(TestCase):
    databases = {"default", "other"}

    def test_load_data_with_user_permissions(self):
        # Create test contenttypes for both databases
        default_objects = [
            ContentType.objects.db_manager("default").create(
                model="examplemodela",
                app_label="app_a",
            ),
            ContentType.objects.db_manager("default").create(
                model="examplemodelb",
                app_label="app_b",
            ),
        ]
        other_objects = [
            ContentType.objects.db_manager("other").create(
                model="examplemodelb",
                app_label="app_b",
            ),
            ContentType.objects.db_manager("other").create(
                model="examplemodela",
                app_label="app_a",
            ),
        ]

        # Now we create the test UserPermission
        Permission.objects.db_manager("default").create(
            name="Can delete example model b",
            codename="delete_examplemodelb",
            content_type=default_objects[1],
        )
        Permission.objects.db_manager("other").create(
            name="Can delete example model b",
            codename="delete_examplemodelb",
            content_type=other_objects[0],
        )

        perm_default = Permission.objects.get_by_natural_key(
            "delete_examplemodelb",
            "app_b",
            "examplemodelb",
        )

        perm_other = Permission.objects.db_manager("other").get_by_natural_key(
            "delete_examplemodelb",
            "app_b",
            "examplemodelb",
        )

        self.assertEqual(perm_default.content_type_id, default_objects[1].id)
        self.assertEqual(perm_other.content_type_id, other_objects[0].id)


class UserManagerTestCase(TransactionTestCase):
    available_apps = [
        "auth_tests",
        "django.contrib.auth",
        "django.contrib.contenttypes",
    ]

    def test_create_user(self):
        email_lowercase = "normal@normal.com"
        user = User.objects.create_user("user", email_lowercase)
        self.assertEqual(user.email, email_lowercase)
        self.assertEqual(user.username, "user")
        self.assertFalse(user.has_usable_password())

    def test_create_user_email_domain_normalize_rfc3696(self):
        # According to RFC 3696 Section 3 the "@" symbol can be part of the
        # local part of an email address.
        returned = UserManager.normalize_email(r"Abc\@DEF@EXAMPLE.com")
        self.assertEqual(returned, r"Abc\@DEF@example.com")

    def test_create_user_email_domain_normalize(self):
        returned = UserManager.normalize_email("normal@DOMAIN.COM")
        self.assertEqual(returned, "normal@domain.com")

    def test_create_user_email_domain_normalize_with_whitespace(self):
        returned = UserManager.normalize_email(r"email\ with_whitespace@D.COM")
        self.assertEqual(returned, r"email\ with_whitespace@d.com")

    def test_empty_username(self):
        with self.assertRaisesMessage(ValueError, "The given username must be set"):
            User.objects.create_user(username="")

    def test_create_user_is_staff(self):
        email = "normal@normal.com"
        user = User.objects.create_user("user", email, is_staff=True)
        self.assertEqual(user.email, email)
        self.assertEqual(user.username, "user")
        self.assertTrue(user.is_staff)

    def test_create_super_user_raises_error_on_false_is_superuser(self):
        with self.assertRaisesMessage(
            ValueError, "Superuser must have is_superuser=True."
        ):
            User.objects.create_superuser(
                username="test",
                email="test@test.com",
                password="test",
                is_superuser=False,
            )

    async def test_acreate_super_user_raises_error_on_false_is_superuser(self):
        with self.assertRaisesMessage(
            ValueError, "Superuser must have is_superuser=True."
        ):
            await User.objects.acreate_superuser(
                username="test",
                email="test@test.com",
                password="test",
                is_superuser=False,
            )

    def test_create_superuser_raises_error_on_false_is_staff(self):
        with self.assertRaisesMessage(ValueError, "Superuser must have is_staff=True."):
            User.objects.create_superuser(
                username="test",
                email="test@test.com",
                password="test",
                is_staff=False,
            )

    async def test_acreate_superuser_raises_error_on_false_is_staff(self):
        with self.assertRaisesMessage(ValueError, "Superuser must have is_staff=True."):
            await User.objects.acreate_superuser(
                username="test",
                email="test@test.com",
                password="test",
                is_staff=False,
            )

    def test_runpython_manager_methods(self):
        def forwards(apps, schema_editor):
            UserModel = apps.get_model("auth", "User")
            user = UserModel.objects.create_user("user1", password="secure")
            self.assertIsInstance(user, UserModel)

        operation = migrations.RunPython(forwards, migrations.RunPython.noop)
        project_state = ProjectState()
        project_state.add_model(ModelState.from_model(User))
        project_state.add_model(ModelState.from_model(Group))
        project_state.add_model(ModelState.from_model(Permission))
        project_state.add_model(ModelState.from_model(ContentType))
        new_state = project_state.clone()
        with connection.schema_editor() as editor:
            operation.state_forwards("test_manager_methods", new_state)
            operation.database_forwards(
                "test_manager_methods",
                editor,
                project_state,
                new_state,
            )
        user = User.objects.get(username="user1")
        self.assertTrue(user.check_password("secure"))


class AbstractBaseUserTests(SimpleTestCase):
    def test_has_usable_password(self):
        """
        Passwords are usable even if they don't correspond to a hasher in
        settings.PASSWORD_HASHERS.
        """
        self.assertIs(User(password="some-gibbberish").has_usable_password(), True)

    def test_normalize_username(self):
        self.assertEqual(IntegerUsernameUser().normalize_username(123), 123)

    def test_clean_normalize_username(self):
        # The normalization happens in AbstractBaseUser.clean()
        ohm_username = "iamtheΩ"  # U+2126 OHM SIGN
        for model in ("auth.User", "auth_tests.CustomUser"):
            with self.subTest(model=model), self.settings(AUTH_USER_MODEL=model):
                User = get_user_model()
                user = User(**{User.USERNAME_FIELD: ohm_username, "password": "foo"})
                user.clean()
                username = user.get_username()
                self.assertNotEqual(username, ohm_username)
                self.assertEqual(
                    username, "iamtheΩ"
                )  # U+03A9 GREEK CAPITAL LETTER OMEGA

    def test_default_email(self):
        self.assertEqual(AbstractBaseUser.get_email_field_name(), "email")

    def test_custom_email(self):
        user = CustomEmailField()
        self.assertEqual(user.get_email_field_name(), "email_address")


class AbstractUserTestCase(TestCase):
    @override_settings(
        MAILERS={
            "default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}
        }
    )
    def test_email_user(self):
        # valid send_mail parameters
        kwargs = {
            "fail_silently": False,
            "auth_user": None,
            "auth_password": None,
            "connection": None,
            "html_message": None,
        }
        user = User(email="foo@bar.com")
        user.email_user(
            subject="Subject here",
            message="This is a message",
            from_email="from@domain.com",
            **kwargs,
        )
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.subject, "Subject here")
        self.assertEqual(message.body, "This is a message")
        self.assertEqual(message.from_email, "from@domain.com")
        self.assertEqual(message.to, [user.email])

    def test_last_login_default(self):
        user1 = User.objects.create(username="user1")
        self.assertIsNone(user1.last_login)

        user2 = User.objects.create_user(username="user2")
        self.assertIsNone(user2.last_login)

    def test_user_clean_normalize_email(self):
        user = User(username="user", password="foo", email="foo@BAR.com")
        user.clean()
        self.assertEqual(user.email, "foo@bar.com")

    def test_user_double_save(self):
        """
        Calling user.save() twice should trigger password_changed() once.
        """
        user = User.objects.create_user(username="user", password="foo")
        user.set_password("bar")
        with mock.patch(
            "django.contrib.auth.password_validation.password_changed"
        ) as pw_changed:
            user.save()
            self.assertEqual(pw_changed.call_count, 1)
            user.save()
            self.assertEqual(pw_changed.call_count, 1)

    @override_settings(PASSWORD_HASHERS=PASSWORD_HASHERS)
    def test_check_password_upgrade(self):
        """
        password_changed() shouldn't be called if User.check_password()
        triggers a hash iteration upgrade.
        """
        user = User.objects.create_user(username="user", password="foo")
        initial_password = user.password
        self.assertTrue(user.check_password("foo"))
        hasher = get_hasher("default")
        self.assertEqual("pbkdf2_sha256", hasher.algorithm)

        old_iterations = hasher.iterations
        try:
            # Upgrade the password iterations
            hasher.iterations = old_iterations + 1
            with mock.patch(
                "django.contrib.auth.password_validation.password_changed"
            ) as pw_changed:
                user.check_password("foo")
                self.assertEqual(pw_changed.call_count, 0)
            self.assertNotEqual(initial_password, user.password)
        finally:
            hasher.iterations = old_iterations

    @override_settings(PASSWORD_HASHERS=PASSWORD_HASHERS)
    async def test_acheck_password_upgrade(self):
        user = await User.objects.acreate_user(username="user", password="foo")
        initial_password = user.password
        self.assertIs(await user.acheck_password("foo"), True)
        hasher = get_hasher("default")
        self.assertEqual("pbkdf2_sha256", hasher.algorithm)

        old_iterations = hasher.iterations
        try:
            # Upgrade the password iterations.
            hasher.iterations = old_iterations + 1
            with mock.patch(
                "django.contrib.auth.password_validation.password_changed"
            ) as pw_changed:
                self.assertIs(await user.acheck_password("foo"), True)
                self.assertEqual(pw_changed.call_count, 0)
            self.assertNotEqual(initial_password, user.password)
        finally:
            hasher.iterations = old_iterations


class LegacyPBKDF2SingleIterationHasher(PBKDF2PasswordHasher):
    """
    A cheap, distinct algorithm used to seed legacy hashes in tests. It only
    ever lives inside PASSWORD_HASHERS overrides, so the production default
    iteration count is untouched.
    """

    algorithm = "test_legacy_pbkdf2_sha256"
    iterations = 1


UPGRADE_RACE_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
    "auth_tests.test_models.LegacyPBKDF2SingleIterationHasher",
]


@override_settings(PASSWORD_HASHERS=UPGRADE_RACE_HASHERS)
class PasswordHashUpgradeRaceTests(TransactionTestCase):
    """
    Deterministic interleavings (barrier controlled, SQLite) between a login
    upgrading a legacy hash and a concurrent password change.
    """

    available_apps = [
        "django.contrib.auth",
        "django.contrib.contenttypes",
        "auth_tests",
    ]

    def setUp(self):
        from django.contrib.auth.hashers import (
            get_hashers,
            get_hashers_by_algorithm,
        )

        # override_settings emits setting_changed, which clears these caches,
        # but clear them explicitly as well and restore the default cache
        # state when the test ends.
        get_hashers.cache_clear()
        get_hashers_by_algorithm.cache_clear()
        self.addCleanup(get_hashers.cache_clear)
        self.addCleanup(get_hashers_by_algorithm.cache_clear)
        legacy_hasher = LegacyPBKDF2SingleIterationHasher()
        self.user = User.objects.create_user(username="alice", password="old-password")
        self.legacy_hash = legacy_hasher.encode("old-password", legacy_hasher.salt())
        self.user.password = self.legacy_hash
        self.user.save(update_fields=["password"])

    def _patched_check_password(self, target, barrier):
        real_check_password = target

        def wrapped(password, encoded, setter=None, preferred="default"):
            def blocking_setter(raw_password):
                # The legacy hash has been read and verified; block until the
                # concurrent password change has been committed before the
                # upgrade write-back runs.
                barrier.wait(timeout=10)
                barrier.wait(timeout=10)
                setter(raw_password)

            return real_check_password(password, encoded, blocking_setter, preferred)

        return wrapped

    def _patched_acheck_password(self, target, barrier):
        real_acheck_password = target

        async def wrapped(password, encoded, setter=None, preferred="default"):
            async def blocking_setter(raw_password):
                barrier.wait(timeout=10)
                barrier.wait(timeout=10)
                await setter(raw_password)

            return await real_acheck_password(
                password, encoded, blocking_setter, preferred
            )

        return wrapped

    def _change_password_concurrently(self, barrier):
        barrier.wait(timeout=10)
        concurrent_user = User.objects.get(pk=self.user.pk)
        concurrent_user.set_password("new-password")
        concurrent_user.save()
        new_hash = concurrent_user.password
        barrier.wait(timeout=10)
        return new_hash

    def test_upgrade_does_not_overwrite_concurrent_password_change(self):
        barrier = threading.Barrier(2)
        result = {}

        def login_flow():
            login_user = User.objects.get(pk=self.user.pk)
            result["returned"] = login_user.check_password("old-password")
            result["in_memory_hash"] = login_user.password
            connections.close_all()

        with mock.patch(
            "django.contrib.auth.base_user.check_password",
            self._patched_check_password(check_password, barrier),
        ):
            thread = threading.Thread(target=login_flow)
            thread.start()
            new_hash = self._change_password_concurrently(barrier)
            thread.join()

        db_hash = User.objects.get(pk=self.user.pk).password
        # check_password still reports the result of verifying the legacy hash.
        self.assertIs(result["returned"], True)
        # The stale upgraded hash is never written back; the new password hash
        # is what the database and the in-memory instance hold.
        self.assertEqual(db_hash, new_hash)
        self.assertEqual(result["in_memory_hash"], new_hash)
        self.assertNotEqual(db_hash, self.legacy_hash)
        refreshed = User.objects.get(pk=self.user.pk)
        self.assertIs(refreshed.check_password("new-password"), True)
        self.assertIs(refreshed.check_password("old-password"), False)

    async def test_aupgrade_does_not_overwrite_concurrent_password_change(self):
        barrier = threading.Barrier(2)
        result = {}

        async def login_flow():
            login_user = await User.objects.aget(pk=self.user.pk)
            result["returned"] = await login_user.acheck_password("old-password")
            result["in_memory_hash"] = login_user.password
            connections.close_all()

        with mock.patch(
            "django.contrib.auth.base_user.acheck_password",
            self._patched_acheck_password(acheck_password, barrier),
        ):
            login_task = asyncio.ensure_future(login_flow())
            new_hash = await asyncio.to_thread(
                self._change_password_concurrently, barrier
            )
            await login_task

        db_hash = (await User.objects.aget(pk=self.user.pk)).password
        self.assertIs(result["returned"], True)
        self.assertEqual(db_hash, new_hash)
        self.assertEqual(result["in_memory_hash"], new_hash)
        self.assertNotEqual(db_hash, self.legacy_hash)
        refreshed = await User.objects.aget(pk=self.user.pk)
        self.assertIs(await refreshed.acheck_password("new-password"), True)
        self.assertIs(await refreshed.acheck_password("old-password"), False)

    def test_normal_upgrade_without_race_still_upgrades(self):
        login_user = User.objects.get(pk=self.user.pk)
        self.assertIs(login_user.check_password("old-password"), True)
        upgraded_hash = User.objects.get(pk=self.user.pk).password
        self.assertEqual(login_user.password, upgraded_hash)
        self.assertNotEqual(upgraded_hash, self.legacy_hash)
        self.assertTrue(upgraded_hash.startswith("md5$"))
        self.assertIs(login_user.check_password("old-password"), True)

    async def test_anormal_upgrade_without_race_still_upgrades(self):
        login_user = await User.objects.aget(pk=self.user.pk)
        self.assertIs(await login_user.acheck_password("old-password"), True)
        upgraded = await User.objects.aget(pk=self.user.pk)
        self.assertEqual(login_user.password, upgraded.password)
        self.assertNotEqual(upgraded.password, self.legacy_hash)
        self.assertTrue(upgraded.password.startswith("md5$"))
        self.assertIs(await login_user.acheck_password("old-password"), True)

    def test_wrong_password_does_not_save_upgraded_hash(self):
        login_user = User.objects.get(pk=self.user.pk)
        with mock.patch.object(type(login_user), "save") as mocked_save:
            self.assertIs(login_user.check_password("wrong-password"), False)
        self.assertEqual(mocked_save.call_count, 0)
        self.assertEqual(User.objects.get(pk=self.user.pk).password, self.legacy_hash)

    async def test_awrong_password_does_not_save_upgraded_hash(self):
        login_user = await User.objects.aget(pk=self.user.pk)
        with mock.patch.object(type(login_user), "asave") as mocked_save:
            self.assertIs(await login_user.acheck_password("wrong-password"), False)
        self.assertEqual(mocked_save.call_count, 0)
        self.assertEqual(
            (await User.objects.aget(pk=self.user.pk)).password,
            self.legacy_hash,
        )

    def test_upgrade_is_not_recorded_as_password_change(self):
        login_user = User.objects.get(pk=self.user.pk)
        with mock.patch(
            "django.contrib.auth.password_validation.password_changed"
        ) as pw_changed:
            login_user.check_password("old-password")
        self.assertEqual(pw_changed.call_count, 0)

    async def test_aupgrade_is_not_recorded_as_password_change(self):
        login_user = await User.objects.aget(pk=self.user.pk)
        with mock.patch(
            "django.contrib.auth.password_validation.password_changed"
        ) as pw_changed:
            await login_user.acheck_password("old-password")
        self.assertEqual(pw_changed.call_count, 0)

    def test_authenticate_survives_concurrent_password_change(self):
        barrier = threading.Barrier(2)
        result = {}

        def login_flow():
            result["user"] = authenticate(username="alice", password="old-password")
            connections.close_all()

        with mock.patch(
            "django.contrib.auth.base_user.check_password",
            self._patched_check_password(check_password, barrier),
        ):
            thread = threading.Thread(target=login_flow)
            thread.start()
            new_hash = self._change_password_concurrently(barrier)
            thread.join()

        # The user authenticated against the hash that was valid at login
        # time; the new password remains the one stored in the database.
        self.assertIsNotNone(result["user"])
        self.assertEqual(User.objects.get(pk=self.user.pk).password, new_hash)
        self.assertIs(
            User.objects.get(pk=self.user.pk).check_password("new-password"),
            True,
        )
        self.assertIs(
            User.objects.get(pk=self.user.pk).check_password("old-password"),
            False,
        )


class CustomModelBackend(ModelBackend):
    def with_perm(
        self, perm, is_active=True, include_superusers=True, backend=None, obj=None
    ):
        if obj is not None and obj.username == "charliebrown":
            return User.objects.filter(pk=obj.pk)
        return User.objects.filter(username__startswith="charlie")


class UserWithPermTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        content_type = ContentType.objects.get_for_model(Group)
        cls.permission = Permission.objects.create(
            name="test",
            content_type=content_type,
            codename="test",
        )
        # User with permission.
        cls.user1 = User.objects.create_user("user 1", "foo@example.com")
        cls.user1.user_permissions.add(cls.permission)
        # User with group permission.
        group1 = Group.objects.create(name="group 1")
        group1.permissions.add(cls.permission)
        group2 = Group.objects.create(name="group 2")
        group2.permissions.add(cls.permission)
        cls.user2 = User.objects.create_user("user 2", "bar@example.com")
        cls.user2.groups.add(group1, group2)
        # Users without permissions.
        cls.user_charlie = User.objects.create_user("charlie", "charlie@example.com")
        cls.user_charlie_b = User.objects.create_user(
            "charliebrown", "charlie@brown.com"
        )
        # Superuser.
        cls.superuser = User.objects.create_superuser(
            "superuser",
            "superuser@example.com",
            "superpassword",
        )
        # Inactive user with permission.
        cls.inactive_user = User.objects.create_user(
            "inactive_user",
            "baz@example.com",
            is_active=False,
        )
        cls.inactive_user.user_permissions.add(cls.permission)

    def test_invalid_permission_name(self):
        msg = "Permission name should be in the form app_label.permission_codename."
        for perm in ("nodots", "too.many.dots", "...", ""):
            with self.subTest(perm), self.assertRaisesMessage(ValueError, msg):
                User.objects.with_perm(perm)

    def test_invalid_permission_type(self):
        msg = "The `perm` argument must be a string or a permission instance."
        for perm in (b"auth.test", object(), None):
            with self.subTest(perm), self.assertRaisesMessage(TypeError, msg):
                User.objects.with_perm(perm)

    def test_invalid_backend_type(self):
        msg = "backend must be a dotted import path string (got %r)."
        for backend in (b"auth_tests.CustomModelBackend", object()):
            with self.subTest(backend):
                with self.assertRaisesMessage(TypeError, msg % backend):
                    User.objects.with_perm("auth.test", backend=backend)

    def test_basic(self):
        active_users = [self.user1, self.user2]
        tests = [
            ({}, [*active_users, self.superuser]),
            ({"obj": self.user1}, []),
            # Only inactive users.
            ({"is_active": False}, [self.inactive_user]),
            # All users.
            ({"is_active": None}, [*active_users, self.superuser, self.inactive_user]),
            # Exclude superusers.
            ({"include_superusers": False}, active_users),
            (
                {"include_superusers": False, "is_active": False},
                [self.inactive_user],
            ),
            (
                {"include_superusers": False, "is_active": None},
                [*active_users, self.inactive_user],
            ),
        ]
        for kwargs, expected_users in tests:
            for perm in ("auth.test", self.permission):
                with self.subTest(perm=perm, **kwargs):
                    self.assertCountEqual(
                        User.objects.with_perm(perm, **kwargs),
                        expected_users,
                    )

    @override_settings(
        AUTHENTICATION_BACKENDS=["django.contrib.auth.backends.BaseBackend"]
    )
    def test_backend_without_with_perm(self):
        self.assertSequenceEqual(User.objects.with_perm("auth.test"), [])

    def test_nonexistent_permission(self):
        self.assertSequenceEqual(User.objects.with_perm("auth.perm"), [self.superuser])

    def test_nonexistent_backend(self):
        with self.assertRaises(ImportError):
            User.objects.with_perm(
                "auth.test",
                backend="invalid.backend.CustomModelBackend",
            )

    def test_invalid_backend_submodule(self):
        with self.assertRaises(TypeError):
            User.objects.with_perm(
                "auth.test",
                backend="json.tool",
            )

    @override_settings(
        AUTHENTICATION_BACKENDS=["auth_tests.test_models.CustomModelBackend"]
    )
    def test_custom_backend(self):
        for perm in ("auth.test", self.permission):
            with self.subTest(perm):
                self.assertCountEqual(
                    User.objects.with_perm(perm),
                    [self.user_charlie, self.user_charlie_b],
                )

    @override_settings(
        AUTHENTICATION_BACKENDS=["auth_tests.test_models.CustomModelBackend"]
    )
    def test_custom_backend_pass_obj(self):
        for perm in ("auth.test", self.permission):
            with self.subTest(perm):
                self.assertSequenceEqual(
                    User.objects.with_perm(perm, obj=self.user_charlie_b),
                    [self.user_charlie_b],
                )

    @override_settings(
        AUTHENTICATION_BACKENDS=[
            "auth_tests.test_models.CustomModelBackend",
            "django.contrib.auth.backends.ModelBackend",
        ]
    )
    def test_multiple_backends(self):
        msg = (
            "You have multiple authentication backends configured and "
            "therefore must provide the `backend` argument."
        )
        with self.assertRaisesMessage(ValueError, msg):
            User.objects.with_perm("auth.test")

        backend = "auth_tests.test_models.CustomModelBackend"
        self.assertCountEqual(
            User.objects.with_perm("auth.test", backend=backend),
            [self.user_charlie, self.user_charlie_b],
        )


class IsActiveTestCase(TestCase):
    """
    Tests the behavior of the guaranteed is_active attribute
    """

    def test_builtin_user_isactive(self):
        user = User.objects.create(username="foo", email="foo@bar.com")
        # is_active is true by default
        self.assertIs(user.is_active, True)
        user.is_active = False
        user.save()
        user_fetched = User.objects.get(pk=user.pk)
        # the is_active flag is saved
        self.assertFalse(user_fetched.is_active)

    @override_settings(AUTH_USER_MODEL="auth_tests.IsActiveTestUser1")
    def test_is_active_field_default(self):
        """
        tests that the default value for is_active is provided
        """
        UserModel = get_user_model()
        user = UserModel(username="foo")
        self.assertIs(user.is_active, True)
        # you can set the attribute - but it will not save
        user.is_active = False
        # there should be no problem saving - but the attribute is not saved
        user.save()
        user_fetched = UserModel._default_manager.get(pk=user.pk)
        # the attribute is always true for newly retrieved instance
        self.assertIs(user_fetched.is_active, True)


class TestCreateSuperUserSignals(TestCase):
    """
    Simple test case for ticket #20541
    """

    def post_save_listener(self, *args, **kwargs):
        self.signals_count += 1

    def setUp(self):
        self.signals_count = 0
        post_save.connect(self.post_save_listener, sender=User)
        self.addCleanup(post_save.disconnect, self.post_save_listener, sender=User)

    def test_create_user(self):
        User.objects.create_user("JohnDoe")
        self.assertEqual(self.signals_count, 1)

    def test_create_superuser(self):
        User.objects.create_superuser("JohnDoe", "mail@example.com", "1")
        self.assertEqual(self.signals_count, 1)


class AnonymousUserTests(SimpleTestCase):
    no_repr_msg = "Django doesn't provide a DB representation for AnonymousUser."

    def setUp(self):
        self.user = AnonymousUser()

    def test_properties(self):
        self.assertIsNone(self.user.pk)
        self.assertEqual(self.user.username, "")
        self.assertEqual(self.user.get_username(), "")
        self.assertIs(self.user.is_anonymous, True)
        self.assertIs(self.user.is_authenticated, False)
        self.assertIs(self.user.is_staff, False)
        self.assertIs(self.user.is_active, False)
        self.assertIs(self.user.is_superuser, False)
        self.assertEqual(self.user.groups.count(), 0)
        self.assertEqual(self.user.user_permissions.count(), 0)
        self.assertEqual(self.user.get_user_permissions(), set())
        self.assertEqual(self.user.get_group_permissions(), set())

    async def test_properties_async_versions(self):
        self.assertEqual(await self.user.groups.acount(), 0)
        self.assertEqual(await self.user.user_permissions.acount(), 0)
        self.assertEqual(await self.user.aget_user_permissions(), set())
        self.assertEqual(await self.user.aget_group_permissions(), set())

    def test_str(self):
        self.assertEqual(str(self.user), "AnonymousUser")

    def test_eq(self):
        self.assertEqual(self.user, AnonymousUser())
        self.assertNotEqual(self.user, User("super", "super@example.com", "super"))

    def test_hash(self):
        self.assertEqual(hash(self.user), 1)

    def test_int(self):
        msg = (
            "Cannot cast AnonymousUser to int. Are you trying to use it in "
            "place of User?"
        )
        with self.assertRaisesMessage(TypeError, msg):
            int(self.user)

    def test_delete(self):
        with self.assertRaisesMessage(NotImplementedError, self.no_repr_msg):
            self.user.delete()

    def test_save(self):
        with self.assertRaisesMessage(NotImplementedError, self.no_repr_msg):
            self.user.save()

    def test_set_password(self):
        with self.assertRaisesMessage(NotImplementedError, self.no_repr_msg):
            self.user.set_password("password")

    def test_check_password(self):
        with self.assertRaisesMessage(NotImplementedError, self.no_repr_msg):
            self.user.check_password("password")


class GroupTests(SimpleTestCase):
    def test_str(self):
        g = Group(name="Users")
        self.assertEqual(str(g), "Users")


class PermissionTests(TestCase):
    def test_str(self):
        p = Permission.objects.get(codename="view_customemailfield")
        self.assertEqual(
            str(p), "Auth_Tests | custom email field | Can view custom email field"
        )

    def test_user_perm_str(self):
        p = Permission.objects.get(codename="view_customemailfield")
        self.assertEqual(p.user_perm_str, "auth_tests.view_customemailfield")
