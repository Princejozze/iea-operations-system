"""
Django settings for the IEA Central Operations & Procurement
Management System.

Single settings file, switched by environment variables (via
django-environ) rather than separate dev/prod modules — simpler to
reason about on a solo, deadline-driven build. Copy .env.example to
.env and fill it in before running anything.
"""

import sys
from pathlib import Path

import environ
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, False),
)
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("SECRET_KEY")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Local apps, in CLAUDE.md §1 dependency order. An app may import only from
    # apps above it. Phase 2-3 apps are installed empty so that adding models to
    # them later does not touch this file.
    "core",
    "accounts",
    "documents",
    "suppliers",
    "tenders",
    "items",
    "audits",
    "approvals",
    "postaward",
    "comms",
    "tasks",
    "dashboards",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    # Compress HTML for slow (3G) links: the item sheet is ~500 KB uncompressed. Below
    # WhiteNoise, which answers static requests itself (serving its own compressed files),
    # so only the app's pages pass through here. BREACH: Django (4.2+) adds random bytes
    # to the gzip header ("Heal the Breach"), and masks the CSRF token per request.
    "django.middleware.gzip.GZipMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Attributes every audited change in the request to request.user (R7).
    "core.audit.AuditActorMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    # R21: a user with must_change_password reaches only the change-password page.
    "accounts.middleware.ForcePasswordChangeMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# --- Database ---
# PostgreSQL is REQUIRED (CLAUDE.md section 1). There is deliberately no default
# and no SQLite fallback: much of this system's enforcement (the audit-trail
# trigger, CHECK constraints, row locking) lives in the database, so a run on
# any other engine proves almost nothing. Unset, empty or non-PostgreSQL
# DATABASE_URL stops startup with an instruction.
_DATABASE_URL_HELP = (
    "Set DATABASE_URL to a PostgreSQL connection string, for example "
    "postgres://iea:PASSWORD@localhost:5432/iea - in the environment or in .env "
    "(copy .env.example to .env). PostgreSQL is required; there is no default and "
    "no SQLite fallback (CLAUDE.md section 1)."
)


def _database_config():
    url = env("DATABASE_URL", default="").strip()
    if not url:
        raise ImproperlyConfigured(f"DATABASE_URL is not set. {_DATABASE_URL_HELP}")
    config = env.db_url_config(url)
    if config.get("ENGINE") != "django.db.backends.postgresql":
        raise ImproperlyConfigured(
            f"DATABASE_URL does not point at PostgreSQL (got engine {config.get('ENGINE')!r}). "
            f"{_DATABASE_URL_HELP}"
        )
    return config


DATABASES = {"default": _database_config()}

AUTH_USER_MODEL = "accounts.User"
# Case-insensitive email sign-in; refuses expired temporary passwords (R21).
AUTHENTICATION_BACKENDS = ["accounts.backends.IEAModelBackend"]
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Africa/Dar_es_Salaam"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

# --- File storage ---
# Per the SRS (Module 6, clause 3): heavy PDFs/brochures/drawings must NOT sit
# on the local server disk — they go to S3. Leave USE_S3=False for local dev;
# flip it on once the S3 bucket + a scoped IAM user exist.
#
# Django 5.1 REMOVED DEFAULT_FILE_STORAGE and STATICFILES_STORAGE. On 6.1 they
# are silently ignored, which would quietly write uploads to the app server.
# The STORAGES dict below is the only setting that works.
USE_S3 = env.bool("USE_S3", default=False)

if USE_S3:
    AWS_ACCESS_KEY_ID = env("AWS_ACCESS_KEY_ID")
    AWS_SECRET_ACCESS_KEY = env("AWS_SECRET_ACCESS_KEY")
    AWS_STORAGE_BUCKET_NAME = env("AWS_STORAGE_BUCKET_NAME")
    AWS_S3_REGION_NAME = env("AWS_S3_REGION_NAME", default="af-south-1")
    AWS_S3_FILE_OVERWRITE = False
    AWS_DEFAULT_ACL = None
    _default_storage = {"BACKEND": "storages.backends.s3boto3.S3Boto3Storage"}
else:
    _default_storage = {"BACKEND": "django.core.files.storage.FileSystemStorage"}
    # Local uploads (tender PDFs carry bid pricing) must never land inside the working tree,
    # where a stray `git add` would stage them. MEDIA_ROOT has no default for that reason, and a
    # path inside the repository is refused. Not needed under S3, or in tests (in-memory storage).
    if "test" not in sys.argv:
        _media = env("MEDIA_ROOT", default="").strip()
        if not _media:
            raise ImproperlyConfigured(
                "MEDIA_ROOT is not set. With USE_S3=False, set MEDIA_ROOT in the environment or "
                "in .env to a directory OUTSIDE the repository, e.g. C:/Users/you/iea-media "
                "(tender documents carry bid pricing and must not sit in the working tree)."
            )
        MEDIA_ROOT = Path(_media).expanduser().resolve()
        if MEDIA_ROOT == BASE_DIR or BASE_DIR in MEDIA_ROOT.parents:
            raise ImproperlyConfigured(
                f"MEDIA_ROOT ({MEDIA_ROOT}) is inside the repository ({BASE_DIR}). "
                "Choose a path outside it."
            )

STORAGES = {
    "default": _default_storage,
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}

# --- Test runs ---
# The manifest storage above needs `collectstatic` to have run, which the test
# runner never does, so any test that renders a template would fail. Under
# `manage.py test`: plain static storage, in-memory uploads (never S3 or the
# app server's disk), and a fast password hasher.
TESTING = "test" in sys.argv  # CLAUDE.md §4 "Testing"
if TESTING:
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# --- Email ---
# All the SRS automations (T-minus alerts, overdue-task emails, NeST
# liquidation alerts) go through this. Console backend for local dev
# so nothing tries to actually send until SMTP creds are set.
EMAIL_BACKEND = env(
    "EMAIL_BACKEND", default="django.core.mail.backends.console.EmailBackend"
)
EMAIL_HOST = env("EMAIL_HOST", default="")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="no-reply@imperialeastafrica.tz")

# --- Live exchange rate API (Module 2 / Module 6) ---
EXCHANGE_RATE_API_KEY = env("EXCHANGE_RATE_API_KEY", default="")

# --- Gemini extraction (CLAUDE.md §4 "External packages") ---
# The model identifier is deliberately NOT in source: names change often enough
# that a pinned one rots. Pick a current Flash-tier model from Google's docs.
GEMINI_API_KEY = env("GEMINI_API_KEY", default="")
GEMINI_MODEL = env("GEMINI_MODEL", default="")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
