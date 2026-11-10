"""
Minimal settings for the CSRF reverse-proxy diagnostic project.

Nothing here disables CSRF or trusts every origin. Trust lists are supplied
per-test with @override_settings so that a one-line config diff is all that
changes between the success/failure pairs.
"""

SECRET_KEY = "diagnostic-secret-key-not-used-for-real-traffic"
DEBUG = False
ALLOWED_HOSTS = [
    "app.example.com",
    "app.example.com:8443",
    "login.app.example.com",
    ".app.example.com",
    "testserver",
]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "probe",
]

MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
]

ROOT_URLCONF = "probe.urls"

DATABASES = {}
USE_TZ = True

# Cookie-based CSRF (the default): a fixed cookie + matching form token is
# generated once per test run and reused by every scenario.
CSRF_USE_SESSIONS = False
CSRF_COOKIE_NAME = "csrftoken"
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_SECURE = True

# Explicit baseline: no forwarding headers are trusted until a test opts in.
SECURE_PROXY_SSL_HEADER = None
USE_X_FORWARDED_HOST = False
USE_X_FORWARDED_PORT = False
CSRF_TRUSTED_ORIGINS = []

# Keep the probe output readable: the CsrfViewMiddleware logs every expected
# DENY via django.security.csrf; silence it rather than disabling the checks.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"null": {"class": "logging.NullHandler"}},
    "loggers": {
        "django.security.csrf": {"handlers": ["null"], "propagate": False},
        "django.request": {"handlers": ["null"], "propagate": False},
    },
}
