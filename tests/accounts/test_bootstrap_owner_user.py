from io import StringIO
from queue import Queue
from threading import Barrier, Lock, Thread
from types import SimpleNamespace

import pytest
from django.contrib.auth.models import AnonymousUser, User
from django.core.exceptions import PermissionDenied
from django.core.management import CommandError, call_command
from django.db import IntegrityError, close_old_connections, connection
from django.http import HttpResponse
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from psycopg.errors import UniqueViolation

from apps.accounts import bootstrap
from apps.accounts.decorators import owner_required
from apps.accounts.roles import Role, assign_role, user_has_role
from apps.core.models import AuditLog

PASSWORD_ENV = "BOOTSTRAP_OWNER_PASSWORD"
USERNAME_ENV = "BOOTSTRAP_OWNER_USERNAME"
STRONG_PASSWORD = "Railway-Owner-2026!"


def _psycopg3_unique_violation(sqlstate: str, constraint_name: str) -> UniqueViolation:
    class Psycopg3UniqueViolation(UniqueViolation):
        pass

    Psycopg3UniqueViolation.sqlstate = sqlstate
    Psycopg3UniqueViolation.diag = property(
        lambda _error: SimpleNamespace(constraint_name=constraint_name)
    )
    return Psycopg3UniqueViolation("duplicate key")


def _owner_view(request):
    return HttpResponse("owner ok")


def test_owner_required_redirects_anonymous_users_to_existing_login_flow():
    request = RequestFactory().get("/system/update/")
    request.user = AnonymousUser()

    response = owner_required(_owner_view)(request)

    assert response.status_code == 302
    assert response["Location"] == "/accounts/login/?next=/system/update/"


@pytest.mark.django_db
@pytest.mark.parametrize("role", [Role.FINANCE, None])
def test_owner_required_rejects_every_authenticated_non_owner(role):
    user = User.objects.create_user("non-owner", password="secret")
    if role is not None:
        assign_role(user, role)
    request = RequestFactory().get("/system/update/")
    request.user = user

    with pytest.raises(PermissionDenied, match="仅老板可以执行此操作"):
        owner_required(_owner_view)(request)


@pytest.mark.django_db
def test_owner_required_allows_owner_and_rejects_superuser_without_owner_role():
    owner = User.objects.create_user("owner", password="secret")
    assign_role(owner, Role.OWNER)
    superuser = User.objects.create_superuser("admin", "admin@example.com", "secret")

    owner_request = RequestFactory().get("/system/update/")
    owner_request.user = owner
    assert owner_required(_owner_view)(owner_request).content == b"owner ok"

    superuser_request = RequestFactory().get("/system/update/")
    superuser_request.user = superuser
    with pytest.raises(PermissionDenied, match="仅老板可以执行此操作"):
        owner_required(_owner_view)(superuser_request)


@pytest.mark.django_db
def test_bootstrap_owner_user_creates_owner_from_stdin_without_leaking_password(monkeypatch):
    monkeypatch.setenv(USERNAME_ENV, "initial-owner")
    monkeypatch.setenv(PASSWORD_ENV, "environment-password-must-not-be-used")
    monkeypatch.setattr("apps.accounts.bootstrap.sys.stdin", StringIO(f"{STRONG_PASSWORD}\n"))
    output = StringIO()

    call_command("bootstrap_owner_user", password_stdin=True, stdout=output)

    user = User.objects.get(username="initial-owner")
    audit = AuditLog.objects.get(action="owner_user.bootstrapped")
    assert user.check_password(STRONG_PASSWORD)
    assert not user.check_password("environment-password-must-not-be-used")
    assert user_has_role(user, Role.OWNER)
    assert audit.actor == user
    assert audit.target_id == str(user.pk)
    assert audit.changes == {"created": True, "role_assigned": True, "password_reset": False}
    assert STRONG_PASSWORD not in output.getvalue()
    assert "environment-password-must-not-be-used" not in output.getvalue()


@pytest.mark.django_db
def test_bootstrap_owner_user_is_idempotent_without_audit_noise(monkeypatch):
    monkeypatch.setenv(USERNAME_ENV, "initial-owner")
    monkeypatch.setattr("apps.accounts.bootstrap.sys.stdin", StringIO(f"{STRONG_PASSWORD}\n"))

    call_command("bootstrap_owner_user", password_stdin=True, stdout=StringIO())
    monkeypatch.setattr("apps.accounts.bootstrap.sys.stdin", StringIO(f"{STRONG_PASSWORD}\n"))
    call_command("bootstrap_owner_user", password_stdin=True, stdout=StringIO())

    user = User.objects.get(username="initial-owner")
    assert User.objects.filter(username="initial-owner").count() == 1
    assert user.groups.filter(name=Role.OWNER.value).count() == 1
    assert AuditLog.objects.filter(action="owner_user.bootstrapped").count() == 1


@pytest.mark.django_db
def test_bootstrap_owner_user_rejects_existing_finance_user_without_side_effects(monkeypatch):
    original_password = "Original-Owner-2026!"
    user = User.objects.create_user("existing-owner", password=original_password)
    assign_role(user, Role.FINANCE)
    monkeypatch.setenv(USERNAME_ENV, user.username)
    monkeypatch.setattr("apps.accounts.bootstrap.sys.stdin", StringIO(f"{STRONG_PASSWORD}\n"))

    output = StringIO()
    with pytest.raises(CommandError, match="角色"):
        call_command("bootstrap_owner_user", password_stdin=True, stdout=output)

    user.refresh_from_db()
    assert user.check_password(original_password)
    assert user_has_role(user, Role.FINANCE)
    assert not user_has_role(user, Role.OWNER)
    assert not AuditLog.objects.filter(action="owner_user.bootstrapped").exists()
    assert STRONG_PASSWORD not in output.getvalue()


@pytest.mark.django_db
def test_bootstrap_owner_user_does_not_reset_existing_finance_user_password(monkeypatch):
    original_password = "Original-Owner-2026!"
    user = User.objects.create_user("finance-owner-conflict", password=original_password)
    assign_role(user, Role.FINANCE)
    monkeypatch.setenv(USERNAME_ENV, user.username)
    monkeypatch.setattr("apps.accounts.bootstrap.sys.stdin", StringIO(f"{STRONG_PASSWORD}\n"))

    with pytest.raises(CommandError, match="角色"):
        call_command(
            "bootstrap_owner_user", password_stdin=True, reset_password=True, stdout=StringIO()
        )

    user.refresh_from_db()
    assert user.check_password(original_password)
    assert not AuditLog.objects.filter(action="owner_user.bootstrapped").exists()


@pytest.mark.django_db
def test_bootstrap_owner_user_resets_existing_password_only_with_explicit_flag(monkeypatch):
    user = User.objects.create_user("reset-owner", password="Original-Owner-2026!")
    monkeypatch.setenv(USERNAME_ENV, user.username)
    monkeypatch.setattr("apps.accounts.bootstrap.sys.stdin", StringIO(f"{STRONG_PASSWORD}\n"))

    call_command("bootstrap_owner_user", password_stdin=True, reset_password=True, stdout=StringIO())

    user.refresh_from_db()
    assert user.check_password(STRONG_PASSWORD)
    assert AuditLog.objects.get(action="owner_user.bootstrapped").changes == {
        "created": False,
        "role_assigned": True,
        "password_reset": True,
    }


@pytest.mark.django_db
@pytest.mark.parametrize("password", ["", "password", "1234567890123456", "   "])
def test_bootstrap_owner_user_fails_closed_for_missing_or_weak_stdin_password(monkeypatch, password):
    monkeypatch.setenv(USERNAME_ENV, "unsafe-owner")
    monkeypatch.setattr("apps.accounts.bootstrap.sys.stdin", StringIO(f"{password}\n"))
    output = StringIO()

    with pytest.raises(CommandError, match="密码") as exc_info:
        call_command("bootstrap_owner_user", password_stdin=True, stdout=output, stderr=output)

    assert not User.objects.filter(username="unsafe-owner").exists()
    assert not AuditLog.objects.exists()
    if password:
        assert password not in str(exc_info.value)
        assert password not in output.getvalue()


@pytest.mark.django_db
def test_bootstrap_owner_user_requires_explicit_stdin_and_strict_username(monkeypatch):
    monkeypatch.setenv(USERNAME_ENV, "invalid owner")

    with pytest.raises(CommandError, match="用户名"):
        call_command("bootstrap_owner_user", password_stdin=True, stdout=StringIO())

    monkeypatch.setenv(USERNAME_ENV, "valid-owner")
    monkeypatch.setenv(PASSWORD_ENV, STRONG_PASSWORD)
    with pytest.raises(CommandError, match="--password-stdin"):
        call_command("bootstrap_owner_user", stdout=StringIO())

    assert not User.objects.exists()


@pytest.mark.django_db
def test_bootstrap_owner_user_normalizes_compatibility_username_before_lookup(monkeypatch):
    user = User.objects.create_user("owner", password="Original-Owner-2026!")
    monkeypatch.setenv(USERNAME_ENV, "\uff4f\uff57\uff4e\uff45\uff52")
    monkeypatch.setattr("apps.accounts.bootstrap.sys.stdin", StringIO(f"{STRONG_PASSWORD}\n"))

    call_command("bootstrap_owner_user", password_stdin=True, stdout=StringIO())

    user.refresh_from_db()
    assert User.objects.count() == 1
    assert user.username == "owner"
    assert user_has_role(user, Role.OWNER)


@pytest.mark.django_db
def test_bootstrap_owner_user_uses_canonical_username_targeted_lookup(monkeypatch):
    owner = User.objects.create_user("owner", password="Original-Owner-2026!")
    User.objects.create_user("unrelated", password="Original-Owner-2026!")
    monkeypatch.setenv(USERNAME_ENV, "\uff4f\uff57\uff4e\uff45\uff52")
    monkeypatch.setattr("apps.accounts.bootstrap.sys.stdin", StringIO(f"{STRONG_PASSWORD}\n"))

    with CaptureQueriesContext(connection) as queries:
        call_command("bootstrap_owner_user", password_stdin=True, stdout=StringIO())

    owner.refresh_from_db()
    user_queries = [
        query["sql"].casefold()
        for query in queries
        if 'from "auth_user"' in query["sql"].casefold()
    ]
    assert User.objects.count() == 2
    assert user_has_role(owner, Role.OWNER)
    assert user_queries
    assert all("where" in query for query in user_queries)
    assert any("username" in query for query in user_queries)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "stdin",
    [
        "a" * 4093 + "Z9!" + "\n",
        "a" * 4093 + "Z9!" + "\r\n",
        "a" * 4093 + "Z9!",
    ],
)
def test_bootstrap_owner_user_accepts_one_bounded_password_line(monkeypatch, stdin):
    monkeypatch.setenv(USERNAME_ENV, f"owner-{len(stdin)}-{stdin.endswith(chr(10))}")
    monkeypatch.setattr("apps.accounts.bootstrap.sys.stdin", StringIO(stdin))

    call_command("bootstrap_owner_user", password_stdin=True, stdout=StringIO())

    expected_password = stdin.removesuffix("\n").removesuffix("\r")
    assert User.objects.get(username__startswith="owner-").check_password(expected_password)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "stdin",
    [
        f"{STRONG_PASSWORD}\nunexpected\n",
        "a" * 4094 + "Z9!" + "\n",
    ],
)
def test_bootstrap_owner_user_rejects_extra_or_oversized_stdin_without_leaking_it(
    monkeypatch, stdin
):
    monkeypatch.setenv(USERNAME_ENV, "unsafe-stdin-owner")
    monkeypatch.setattr("apps.accounts.bootstrap.sys.stdin", StringIO(stdin))
    output = StringIO()

    with pytest.raises(CommandError, match="密码") as exc_info:
        call_command("bootstrap_owner_user", password_stdin=True, stdout=output)

    assert not User.objects.filter(username="unsafe-stdin-owner").exists()
    assert stdin not in str(exc_info.value)
    assert stdin not in output.getvalue()


@pytest.mark.django_db(transaction=True)
def test_bootstrap_role_user_recovers_a_concurrent_unique_username_race(monkeypatch):
    monkeypatch.setenv("PARALLEL_BOOTSTRAP_PASSWORD", STRONG_PASSWORD)
    barrier = Barrier(2)
    lock = Lock()
    misses = 0
    errors = Queue()
    original_find = bootstrap._find_existing_user

    def synchronize_missing_lookup(*args, **kwargs):
        nonlocal misses
        user = original_find(*args, **kwargs)
        if user is None:
            with lock:
                misses += 1
                should_wait = misses <= 2
            if should_wait:
                barrier.wait(timeout=5)
        return user

    monkeypatch.setattr(bootstrap, "_find_existing_user", synchronize_missing_lookup)

    def run_bootstrap():
        close_old_connections()
        try:
            bootstrap.bootstrap_role_user(
                role=Role.OWNER,
                username_env="UNUSED_USERNAME",
                password_env="PARALLEL_BOOTSTRAP_PASSWORD",
                audit_action="owner_user.bootstrapped",
                options={"username": "parallel-owner", "password_stdin": False, "reset_password": False},
                stdout=StringIO(),
            )
        except Exception as error:  # noqa: BLE001
            errors.put(error)
        finally:
            close_old_connections()

    first = Thread(target=run_bootstrap)
    second = Thread(target=run_bootstrap)
    first.start()
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors.empty()
    user = User.objects.get(username="parallel-owner")
    assert user_has_role(user, Role.OWNER)
    assert AuditLog.objects.filter(action="owner_user.bootstrapped").count() == 1


@pytest.mark.django_db
def test_bootstrap_role_user_reraises_unrelated_integrity_error(monkeypatch):
    monkeypatch.setenv("INTEGRITY_ERROR_PASSWORD", STRONG_PASSWORD)

    def fail_save(self, *args, **kwargs):
        raise IntegrityError("unrelated integrity failure")

    monkeypatch.setattr(User, "save", fail_save)

    with pytest.raises(IntegrityError, match="unrelated integrity failure"):
        bootstrap.bootstrap_role_user(
            role=Role.OWNER,
            username_env="UNUSED_USERNAME",
            password_env="INTEGRITY_ERROR_PASSWORD",
            audit_action="owner_user.bootstrapped",
            options={"username": "integrity-owner", "password_stdin": False, "reset_password": False},
            stdout=StringIO(),
        )

    assert not User.objects.filter(username="integrity-owner").exists()


def test_bootstrap_role_user_resets_connection_before_retrying_database_lock(monkeypatch):
    monkeypatch.setenv("INTEGRITY_ERROR_PASSWORD", STRONG_PASSWORD)
    attempts = 0
    closed_connections = 0

    def fail_once_with_database_lock(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise bootstrap.OperationalError("database table is locked")

    def close_connection():
        nonlocal closed_connections
        closed_connections += 1

    monkeypatch.setattr(bootstrap, "_bootstrap_role_user_once", fail_once_with_database_lock)
    monkeypatch.setattr(bootstrap.connection, "close", close_connection)
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _seconds: None)

    bootstrap.bootstrap_role_user(
        role=Role.OWNER,
        username_env="UNUSED_USERNAME",
        password_env="INTEGRITY_ERROR_PASSWORD",
        audit_action="owner_user.bootstrapped",
        options={"username": "integrity-owner", "password_stdin": False, "reset_password": False},
        stdout=StringIO(),
    )

    assert attempts == 2
    assert closed_connections == 1


@pytest.mark.django_db
def test_bootstrap_role_user_does_not_misclassify_non_username_integrity_error(
    monkeypatch,
):
    existing = User.objects.create_user("integrity-owner", password="Original-Owner-2026!")
    monkeypatch.setenv("INTEGRITY_ERROR_PASSWORD", STRONG_PASSWORD)
    original_find = bootstrap._find_existing_user
    calls = 0

    def simulate_missing_then_existing(*args, **kwargs):
        nonlocal calls
        calls += 1
        return None if calls == 1 else original_find(*args, **kwargs)

    def fail_save(self, *args, **kwargs):
        raise IntegrityError("check constraint failed")

    monkeypatch.setattr(bootstrap, "_find_existing_user", simulate_missing_then_existing)
    monkeypatch.setattr(User, "save", fail_save)

    with pytest.raises(IntegrityError, match="check constraint failed"):
        bootstrap.bootstrap_role_user(
            role=Role.OWNER,
            username_env="UNUSED_USERNAME",
            password_env="INTEGRITY_ERROR_PASSWORD",
            audit_action="owner_user.bootstrapped",
            options={"username": existing.username, "password_stdin": False, "reset_password": False},
            stdout=StringIO(),
        )

    existing.refresh_from_db()
    assert not user_has_role(existing, Role.OWNER)
    assert not AuditLog.objects.filter(action="owner_user.bootstrapped").exists()


@pytest.mark.django_db
def test_bootstrap_role_user_recovers_psycopg3_username_unique_violation(monkeypatch):
    existing = User.objects.create_user("integrity-owner", password="Original-Owner-2026!")
    monkeypatch.setenv("INTEGRITY_ERROR_PASSWORD", STRONG_PASSWORD)
    original_find = bootstrap._find_existing_user
    calls = 0

    def simulate_missing_then_existing(*args, **kwargs):
        nonlocal calls
        calls += 1
        return None if calls == 1 else original_find(*args, **kwargs)

    def fail_save(self, *args, **kwargs):
        error = IntegrityError("duplicate key value violates unique constraint")
        raise error from _psycopg3_unique_violation("23505", "auth_user_username_key")

    monkeypatch.setattr(bootstrap, "_find_existing_user", simulate_missing_then_existing)
    monkeypatch.setattr(User, "save", fail_save)

    bootstrap.bootstrap_role_user(
        role=Role.OWNER,
        username_env="UNUSED_USERNAME",
        password_env="INTEGRITY_ERROR_PASSWORD",
        audit_action="owner_user.bootstrapped",
        options={"username": existing.username, "password_stdin": False, "reset_password": False},
        stdout=StringIO(),
    )

    existing.refresh_from_db()
    assert user_has_role(existing, Role.OWNER)
    assert AuditLog.objects.filter(action="owner_user.bootstrapped").count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("sqlstate", "constraint_name"),
    [
        ("23514", "auth_user_username_key"),
        ("23505", "auth_user_email_key"),
    ],
)
def test_bootstrap_role_user_reraises_nonmatching_psycopg3_integrity_error(
    monkeypatch, sqlstate, constraint_name
):
    monkeypatch.setenv("INTEGRITY_ERROR_PASSWORD", STRONG_PASSWORD)

    def fail_save(self, *args, **kwargs):
        error = IntegrityError("database integrity error")
        raise error from _psycopg3_unique_violation(sqlstate, constraint_name)

    monkeypatch.setattr(User, "save", fail_save)

    with pytest.raises(IntegrityError, match="database integrity error"):
        bootstrap.bootstrap_role_user(
            role=Role.OWNER,
            username_env="UNUSED_USERNAME",
            password_env="INTEGRITY_ERROR_PASSWORD",
            audit_action="owner_user.bootstrapped",
            options={"username": "integrity-owner", "password_stdin": False, "reset_password": False},
            stdout=StringIO(),
        )

    assert not User.objects.filter(username="integrity-owner").exists()
