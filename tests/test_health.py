import importlib
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import psycopg
import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import DatabaseError, InterfaceError
from django.urls import reverse

import manage
from scripts import wait_for_db

VALID_PRODUCTION_TAX_ID = "91320281" + "SAFE" + "000001"


def _configure_production_environment(monkeypatch):
    monkeypatch.setenv("DJANGO_ALLOWED_HOSTS", "localhost")
    monkeypatch.setenv("CSRF_TRUSTED_ORIGINS", "https://localhost")
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://finance:long-production-password@db:5432/finance"
    )
    monkeypatch.setenv("DJANGO_SECRET_KEY", "p" * 50)
    monkeypatch.setenv("DJANGO_DEBUG", "false")
    monkeypatch.setenv("COMPANY_TAX_ID", VALID_PRODUCTION_TAX_ID)
    monkeypatch.setenv("SHUNDA_RELEASE_VERSION", "v0.1.0")
    monkeypatch.setenv("SHUNDA_UPDATER_URL", "http://updater:8090")
    monkeypatch.setenv("SHUNDA_UPDATER_TOKEN", "u" * 32)


class _FailingCursor:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, query):
        raise DatabaseError("private database failure")


class _FailingConnection:
    def cursor(self):
        return _FailingCursor()


class _InterfaceErrorCursor:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, query):
        raise InterfaceError("private database interface failure")


class _InterfaceErrorConnection:
    def cursor(self):
        return _InterfaceErrorCursor()


class _UnexpectedResultCursor:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, query):
        return None

    def fetchone(self):
        return (0,)


class _UnexpectedResultConnection:
    def cursor(self):
        return _UnexpectedResultCursor()


@pytest.mark.django_db
def test_health_check(client):
    response = client.get("/health/")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_check_returns_503_when_database_query_fails(client, monkeypatch):
    monkeypatch.setattr(
        "apps.core.views.connections", {"default": _FailingConnection()}, raising=False
    )

    response = client.get("/health/")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


def test_health_check_returns_503_when_database_interface_fails(client, monkeypatch):
    monkeypatch.setattr(
        "apps.core.views.connections", {"default": _InterfaceErrorConnection()}
    )
    client.raise_request_exception = False

    response = client.get("/health/")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


def test_health_check_returns_503_when_database_result_is_unexpected(
    client, monkeypatch
):
    monkeypatch.setattr(
        "apps.core.views.connections",
        {"default": _UnexpectedResultConnection()},
    )

    response = client.get("/health/")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


def test_root_redirects_to_invoice_ledger(client):
    response = client.get("/")

    assert response.status_code == 302
    assert response.headers["Location"] == reverse("ledger:invoice-list")


def test_wait_for_database_requires_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="DATABASE_URL is required"):
        wait_for_db.wait_for_database()


def test_wait_for_database_returns_after_successful_connection(monkeypatch):
    connect = MagicMock()
    database_url = "postgresql://shunda:shunda_dev@db:5432/shunda_finance"
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setattr(wait_for_db.psycopg, "connect", connect)

    wait_for_db.wait_for_database()

    connect.assert_called_once_with(database_url)


def test_wait_for_database_fails_after_thirty_unsuccessful_connections(monkeypatch):
    connect = MagicMock(side_effect=psycopg.OperationalError("database unavailable"))
    sleep = MagicMock()
    monkeypatch.setenv("DATABASE_URL", "postgresql://shunda:shunda_dev@db:5432/shunda_finance")
    monkeypatch.setattr(wait_for_db.psycopg, "connect", connect)
    monkeypatch.setattr(wait_for_db.time, "sleep", sleep)

    with pytest.raises(RuntimeError, match="Database did not become available"):
        wait_for_db.wait_for_database()

    assert connect.call_count == 30
    assert sleep.call_count == 30


def test_production_settings_require_secret_key(monkeypatch):
    _configure_production_environment(monkeypatch)
    monkeypatch.delenv("DJANGO_SECRET_KEY", raising=False)
    sys.modules.pop("config.settings.prod", None)

    with pytest.raises(ImproperlyConfigured, match="DJANGO_SECRET_KEY is required"):
        importlib.import_module("config.settings.prod")


def test_production_settings_use_configured_secret_key(monkeypatch):
    _configure_production_environment(monkeypatch)
    sys.modules.pop("config.settings.prod", None)

    production_settings = importlib.import_module("config.settings.prod")

    assert production_settings.SECRET_KEY == "p" * 50


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SHUNDA_UPDATER_URL", None),
        ("SHUNDA_UPDATER_URL", "http://updater:8090/path"),
        ("SHUNDA_UPDATER_URL", "http://token@updater:8090"),
        ("SHUNDA_UPDATER_URL", "http://updater:8090?next=unsafe"),
        ("SHUNDA_UPDATER_TOKEN", None),
        ("SHUNDA_UPDATER_TOKEN", " " * 32),
        ("SHUNDA_UPDATER_TOKEN", "short-token"),
    ],
)
def test_production_settings_require_strict_updater_configuration(monkeypatch, name, value):
    _configure_production_environment(monkeypatch)
    if value is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, value)
    sys.modules.pop("config.settings.prod", None)

    with pytest.raises(ImproperlyConfigured, match="SHUNDA_UPDATER"):
        importlib.import_module("config.settings.prod")


def test_production_settings_expose_only_fixed_updater_endpoint(monkeypatch):
    _configure_production_environment(monkeypatch)
    sys.modules.pop("config.settings.prod", None)

    production_settings = importlib.import_module("config.settings.prod")

    assert production_settings.SHUNDA_UPDATER_URL == "http://updater:8090"
    assert production_settings.SHUNDA_UPDATER_TOKEN == "u" * 32


@pytest.mark.parametrize("release_version", ["latest", "1.2.3", "v1.2", "v1.2.3.4"])
def test_production_settings_reject_noncanonical_release_versions(
    monkeypatch, release_version
):
    _configure_production_environment(monkeypatch)
    monkeypatch.setenv("SHUNDA_RELEASE_VERSION", release_version)
    sys.modules.pop("config.settings.prod", None)

    with pytest.raises(ImproperlyConfigured, match="SHUNDA_RELEASE_VERSION"):
        importlib.import_module("config.settings.prod")


def test_production_settings_use_canonical_release_version(monkeypatch):
    _configure_production_environment(monkeypatch)
    monkeypatch.setenv("SHUNDA_RELEASE_VERSION", "v12.34.56")
    sys.modules.pop("config.settings.prod", None)

    production_settings = importlib.import_module("config.settings.prod")

    assert production_settings.SHUNDA_RELEASE_VERSION == "v12.34.56"


def test_production_settings_allow_explicit_http_cookie_transport(monkeypatch):
    _configure_production_environment(monkeypatch)
    monkeypatch.setenv("DJANGO_COOKIE_SECURE", "false")
    sys.modules.pop("config.settings.prod", None)

    production_settings = importlib.import_module("config.settings.prod")

    assert production_settings.SESSION_COOKIE_SECURE is False
    assert production_settings.CSRF_COOKIE_SECURE is False


def test_production_settings_reject_weak_database_credentials(monkeypatch):
    _configure_production_environment(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgresql://finance:short@db:5432/finance")
    sys.modules.pop("config.settings.prod", None)

    with pytest.raises(ImproperlyConfigured, match="strong PostgreSQL credentials"):
        importlib.import_module("config.settings.prod")


def test_production_settings_reject_url_encoded_placeholder_credentials(monkeypatch):
    _configure_production_environment(monkeypatch)
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://REPLACE%5FUSER:REPLACE%5FSTRONG%5FPASSWORD@db:5432/finance",
    )
    sys.modules.pop("config.settings.prod", None)

    with pytest.raises(ImproperlyConfigured, match="placeholder credentials"):
        importlib.import_module("config.settings.prod")


def test_env_example_cannot_be_loaded_as_production_configuration(monkeypatch):
    for line in Path(".env.example").read_text().splitlines():
        if line and not line.startswith("#"):
            name, value = line.split("=", 1)
            monkeypatch.setenv(name, value)
    sys.modules.pop("config.settings.prod", None)

    with pytest.raises(ImproperlyConfigured):
        importlib.import_module("config.settings.prod")


def test_development_settings_enable_debug_mode():
    development_settings = importlib.import_module("config.settings.dev")

    assert development_settings.DEBUG is True


def test_wsgi_application_is_configured():
    from config.wsgi import application

    assert application is not None


def test_manage_main_sets_default_settings_and_executes_command(monkeypatch):
    execute_from_command_line = MagicMock()
    monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)
    monkeypatch.setattr(
        "django.core.management.execute_from_command_line", execute_from_command_line
    )
    monkeypatch.setattr(sys, "argv", ["manage.py", "check"])

    manage.main()

    assert os.environ["DJANGO_SETTINGS_MODULE"] == "config.settings.dev"
    execute_from_command_line.assert_called_once_with(["manage.py", "check"])
