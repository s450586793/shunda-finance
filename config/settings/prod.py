import os
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

from .base import *


def _required_value(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ImproperlyConfigured(f"{name} is required in production")
    if _is_placeholder(value):
        raise ImproperlyConfigured(f"{name} must not use a placeholder in production")
    return value


def _required_list(name: str) -> list[str]:
    values = [item.strip() for item in _required_value(name).split(",") if item.strip()]
    if not values:
        raise ImproperlyConfigured(f"{name} is required in production")
    return values


def _production_database_url() -> str:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise ImproperlyConfigured("DATABASE_URL is required in production")
    parsed = urlsplit(database_url)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise ImproperlyConfigured("DATABASE_URL must use PostgreSQL in production")
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    if _is_placeholder(username) or _is_placeholder(password):
        raise ImproperlyConfigured("DATABASE_URL must not use placeholder credentials")
    if not username or not password or len(password) < 16:
        raise ImproperlyConfigured("DATABASE_URL must contain strong PostgreSQL credentials")
    return database_url


def _is_placeholder(value: str) -> bool:
    normalized = unquote(value).casefold()
    return "replace_" in normalized or "placeholder" in normalized


def _production_company_tax_id() -> str:
    tax_id = _required_value("COMPANY_TAX_ID").upper()
    obvious_placeholders = ("TEST", "EXAMPLE", "DUMMY", "CHANGEME")
    if (
        re.fullmatch(r"[0-9A-Z]{15,20}", tax_id) is None
        or any(marker in tax_id for marker in obvious_placeholders)
        or len(set(tax_id)) < 3
    ):
        raise ImproperlyConfigured(
            "COMPANY_TAX_ID must be a valid Chinese taxpayer identifier in production"
        )
    return tax_id


def _production_updater_url() -> str:
    value = _required_value("SHUNDA_UPDATER_URL")
    if value != "http://updater:8090":
        raise ImproperlyConfigured(
            "SHUNDA_UPDATER_URL must be http://updater:8090 in production"
        )
    return value


def _production_updater_token() -> str:
    value = os.environ.get("SHUNDA_UPDATER_TOKEN", "")
    if not value.strip() or len(value.encode("utf-8")) < 32:
        raise ImproperlyConfigured(
            "SHUNDA_UPDATER_TOKEN must be a non-empty token of at least 32 bytes in production"
        )
    return value


DEBUG = False
if os.environ.get("DJANGO_DEBUG", "false").lower() != "false":
    raise ImproperlyConfigured("DJANGO_DEBUG must be false in production")

SECRET_KEY = _required_value("DJANGO_SECRET_KEY")
if len(SECRET_KEY) < 50:
    raise ImproperlyConfigured("DJANGO_SECRET_KEY must be at least 50 characters in production")

ALLOWED_HOSTS = _required_list("DJANGO_ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = _required_list("CSRF_TRUSTED_ORIGINS")
COMPANY_TAX_ID = _production_company_tax_id()
SHUNDA_RELEASE_VERSION = _required_value("SHUNDA_RELEASE_VERSION")
if (
    re.fullmatch(
        r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)",
        SHUNDA_RELEASE_VERSION,
    )
    is None
):
    raise ImproperlyConfigured(
        "SHUNDA_RELEASE_VERSION must use canonical vX.Y.Z format in production"
    )
SHUNDA_UPDATER_URL = _production_updater_url()
SHUNDA_UPDATER_TOKEN = _production_updater_token()
DATABASES = {
    "default": dj_database_url.parse(
        _production_database_url(), conn_max_age=600, conn_health_checks=True
    )
}

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
COOKIE_SECURE = os.environ.get("DJANGO_COOKIE_SECURE", "true").strip().casefold() != "false"
SESSION_COOKIE_SECURE = COOKIE_SECURE
CSRF_COOKIE_SECURE = COOKIE_SECURE
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
DATA_UPLOAD_MAX_MEMORY_SIZE = 25 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
MEDIA_ROOT = Path("/data/uploads")
EXPORT_ROOT = Path("/data/exports")
BACKUP_ROOT = Path("/data/backups")

STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}
