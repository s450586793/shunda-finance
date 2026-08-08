import os
from pathlib import Path

import dj_database_url
from django.core.management.utils import get_random_secret_key

BASE_DIR = Path(__file__).resolve().parents[2]

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", get_random_secret_key())
DEBUG = os.environ.get("DJANGO_DEBUG", "false").lower() == "true"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
COMPANY_TAX_ID = os.environ.get("COMPANY_TAX_ID", "")
SHUNDA_RELEASE_VERSION = os.environ.get("SHUNDA_RELEASE_VERSION", "v0.0.0")
SHUNDA_UPDATER_URL = os.environ.get("SHUNDA_UPDATER_URL", "http://updater:8090")
SHUNDA_UPDATER_TOKEN = os.environ.get("SHUNDA_UPDATER_TOKEN", "")
SYSTEM_UPDATE_MAX_REQUEST_BYTES = 1024
IMPORT_MAX_UPLOAD_BYTES = int(os.environ.get("IMPORT_MAX_UPLOAD_BYTES", "20971520"))
IMPORT_MAX_ROWS = int(os.environ.get("IMPORT_MAX_ROWS", "100000"))

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "apps.accounts.apps.AccountsConfig",
    "apps.core.apps.CoreConfig",
    "apps.imports.apps.ImportsConfig",
    "apps.parties.apps.PartiesConfig",
    "apps.ledger.apps.LedgerConfig",
    "apps.reconciliation.apps.ReconciliationConfig",
    "apps.reporting.apps.ReportingConfig",
    "apps.system_update.apps.SystemUpdateConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
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
                "apps.core.context_processors.navigation",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
        conn_health_checks=True,
    ),
}

LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
EXPORT_ROOT = BASE_DIR / "exports"
BACKUP_ROOT = BASE_DIR / "backups"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/ledger/invoices/"
LOGOUT_REDIRECT_URL = "/accounts/login/"
