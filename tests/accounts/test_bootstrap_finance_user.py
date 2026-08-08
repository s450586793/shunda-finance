from io import StringIO

import pytest
from django.contrib.auth.models import User
from django.core.management import CommandError, call_command

from apps.accounts.roles import Role, user_has_role
from apps.core.models import AuditLog

PASSWORD_ENV = "BOOTSTRAP_FINANCE_PASSWORD"
USERNAME_ENV = "BOOTSTRAP_FINANCE_USERNAME"
STRONG_PASSWORD = "Railway-Finance-2026!"


@pytest.mark.django_db
def test_bootstrap_finance_user_creates_user_role_and_audit_without_leaking_password(
    monkeypatch,
):
    monkeypatch.setenv(USERNAME_ENV, "initial-finance")
    monkeypatch.setenv(PASSWORD_ENV, STRONG_PASSWORD)
    output = StringIO()

    call_command("bootstrap_finance_user", stdout=output)

    user = User.objects.get(username="initial-finance")
    audit = AuditLog.objects.get(action="finance_user.bootstrapped")
    assert user.check_password(STRONG_PASSWORD)
    assert user_has_role(user, Role.FINANCE)
    assert audit.actor == user
    assert audit.target_id == str(user.pk)
    assert audit.changes == {
        "created": True,
        "role_assigned": True,
        "password_reset": False,
    }
    assert "initial-finance" in output.getvalue()
    assert STRONG_PASSWORD not in output.getvalue()


@pytest.mark.django_db
def test_bootstrap_finance_user_is_idempotent_without_audit_noise(monkeypatch):
    monkeypatch.setenv(USERNAME_ENV, "initial-finance")
    monkeypatch.setenv(PASSWORD_ENV, STRONG_PASSWORD)

    call_command("bootstrap_finance_user", stdout=StringIO())
    call_command("bootstrap_finance_user", stdout=StringIO())

    user = User.objects.get(username="initial-finance")
    assert User.objects.filter(username="initial-finance").count() == 1
    assert user.groups.filter(name=Role.FINANCE.value).count() == 1
    assert AuditLog.objects.filter(action="finance_user.bootstrapped").count() == 1


@pytest.mark.django_db
def test_bootstrap_finance_user_reuses_existing_user_without_resetting_password(
    monkeypatch,
):
    original_password = "Original-Finance-2026!"
    user = User.objects.create_user("existing-finance", password=original_password)
    monkeypatch.setenv(USERNAME_ENV, user.username)
    monkeypatch.setenv(PASSWORD_ENV, STRONG_PASSWORD)

    call_command("bootstrap_finance_user", stdout=StringIO())

    user.refresh_from_db()
    audit = AuditLog.objects.get(action="finance_user.bootstrapped")
    assert user.check_password(original_password)
    assert not user.check_password(STRONG_PASSWORD)
    assert user_has_role(user, Role.FINANCE)
    assert audit.changes == {
        "created": False,
        "role_assigned": True,
        "password_reset": False,
    }


@pytest.mark.django_db
def test_bootstrap_finance_user_resets_existing_password_only_with_explicit_flag(
    monkeypatch,
):
    user = User.objects.create_user(
        "reset-finance", password="Original-Finance-2026!"
    )
    monkeypatch.setenv(USERNAME_ENV, user.username)
    monkeypatch.setenv(PASSWORD_ENV, STRONG_PASSWORD)

    call_command(
        "bootstrap_finance_user", reset_password=True, stdout=StringIO()
    )

    user.refresh_from_db()
    assert user.check_password(STRONG_PASSWORD)
    assert AuditLog.objects.get(action="finance_user.bootstrapped").changes == {
        "created": False,
        "role_assigned": True,
        "password_reset": True,
    }


@pytest.mark.django_db
def test_bootstrap_finance_user_accepts_password_from_stdin(monkeypatch):
    monkeypatch.setenv(USERNAME_ENV, "stdin-finance")
    monkeypatch.delenv(PASSWORD_ENV, raising=False)
    monkeypatch.setattr(
        "apps.accounts.bootstrap.sys.stdin",
        StringIO(f"{STRONG_PASSWORD}\n"),
    )

    call_command(
        "bootstrap_finance_user", password_stdin=True, stdout=StringIO()
    )

    assert User.objects.get(username="stdin-finance").check_password(STRONG_PASSWORD)


@pytest.mark.django_db
@pytest.mark.parametrize("password", [None, "password", "1234567890123456", "   "])
def test_bootstrap_finance_user_fails_closed_for_missing_or_weak_password(
    monkeypatch, password
):
    monkeypatch.setenv(USERNAME_ENV, "unsafe-finance")
    if password is None:
        monkeypatch.delenv(PASSWORD_ENV, raising=False)
    else:
        monkeypatch.setenv(PASSWORD_ENV, password)
    output = StringIO()

    with pytest.raises(CommandError, match="密码") as exc_info:
        call_command("bootstrap_finance_user", stdout=output, stderr=output)

    assert not User.objects.filter(username="unsafe-finance").exists()
    assert not AuditLog.objects.exists()
    if password:
        assert password not in str(exc_info.value)
        assert password not in output.getvalue()


@pytest.mark.django_db
def test_bootstrap_finance_user_requires_username(monkeypatch):
    monkeypatch.delenv(USERNAME_ENV, raising=False)
    monkeypatch.setenv(PASSWORD_ENV, STRONG_PASSWORD)

    with pytest.raises(CommandError, match="用户名"):
        call_command("bootstrap_finance_user", stdout=StringIO())

    assert not User.objects.exists()
