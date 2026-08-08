import os
import sys
import time

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import (
    CommonPasswordValidator,
    MinimumLengthValidator,
    NumericPasswordValidator,
    UserAttributeSimilarityValidator,
)
from django.core.exceptions import ValidationError
from django.core.management.base import CommandError
from django.db import IntegrityError, OperationalError, connection, transaction

from apps.accounts.roles import Role, assign_role
from apps.core.audit import record_audit

PASSWORD_VALIDATORS = (
    MinimumLengthValidator(min_length=12),
    CommonPasswordValidator(),
    NumericPasswordValidator(),
    UserAttributeSimilarityValidator(),
)
MAX_STDIN_PASSWORD_BYTES = 4 * 1024
MAX_STDIN_PASSWORD_CHARACTERS = MAX_STDIN_PASSWORD_BYTES + 3
MAX_DATABASE_LOCK_RETRIES = 20


def add_bootstrap_arguments(parser) -> None:
    parser.add_argument("--username")
    parser.add_argument("--password-stdin", action="store_true")
    parser.add_argument("--reset-password", action="store_true")


def bootstrap_role_user(
    *,
    role: Role,
    username_env: str,
    password_env: str | None,
    audit_action: str,
    options: dict,
    stdout,
    password_from_stdin_only: bool = False,
    strict_username: bool = False,
    reject_other_business_role: bool = False,
) -> None:
    user_model = get_user_model()
    username = _normalize_username(
        user_model, options["username"] or os.environ.get(username_env, "")
    )
    if not username:
        raise CommandError(f"初始{role.value}用户名不能为空，请设置 {username_env} 或 --username")
    validation_user = user_model(username=username)
    if strict_username:
        _validate_username(validation_user)
    password = _read_password(
        from_stdin=options["password_stdin"],
        password_env=password_env,
        required_from_stdin=password_from_stdin_only,
    )
    if not password or not password.strip():
        raise CommandError("初始用户密码不能为空，请使用 --password-stdin")
    _validate_password(password, validation_user)

    for attempt in range(MAX_DATABASE_LOCK_RETRIES):
        try:
            return _bootstrap_role_user_once(
                role=role,
                audit_action=audit_action,
                options=options,
                stdout=stdout,
                reject_other_business_role=reject_other_business_role,
                user_model=user_model,
                username=username,
                validation_user=validation_user,
                password=password,
            )
        except OperationalError as error:
            if not _is_database_locked(error) or attempt == MAX_DATABASE_LOCK_RETRIES - 1:
                raise
            connection.close()
            time.sleep(0.01 * (attempt + 1))


def _bootstrap_role_user_once(
    *,
    role: Role,
    audit_action: str,
    options: dict,
    stdout,
    reject_other_business_role: bool,
    user_model,
    username: str,
    validation_user,
    password: str,
) -> None:
    with transaction.atomic():
        user = _find_existing_user(user_model, username)
        created = user is None
        if created:
            user = validation_user
            user.set_password(password)
            try:
                with transaction.atomic():
                    user.save()
            except IntegrityError as error:
                if not _is_username_unique_violation(error, user_model):
                    raise
                user = _find_existing_user(user_model, username)
                if user is None:
                    raise
                created = False

        if reject_other_business_role and _has_other_business_role(user, role):
            raise CommandError("初始用户已拥有其他业务角色，不能变更角色")

        role_assigned = _requires_role_assignment(user, role)
        if role_assigned:
            assign_role(user, role)

        password_reset = bool(options["reset_password"] and not created)
        if password_reset:
            user.set_password(password)
            user.save(update_fields=["password"])

        if created or role_assigned or password_reset:
            record_audit(
                user,
                audit_action,
                user,
                {
                    "created": created,
                    "role_assigned": role_assigned,
                    "password_reset": password_reset,
                },
            )

    if created:
        message = f"{role.value}用户 {username} 已创建并授权。"
    elif role_assigned or password_reset:
        message = f"{role.value}用户 {username} 已复用并完成授权。"
    else:
        message = f"{role.value}用户 {username} 已存在且权限正确。"
    stdout.write(message)


def _read_password(
    *, from_stdin: bool, password_env: str | None, required_from_stdin: bool
) -> str:
    if required_from_stdin and not from_stdin:
        raise CommandError("老板初始密码只能通过 --password-stdin 从标准输入读取")
    if from_stdin:
        return _read_one_stdin_password_line()
    return os.environ.get(password_env, "") if password_env else ""


def _normalize_username(user_model, raw_username: object) -> str:
    if not isinstance(raw_username, str):
        return ""
    return user_model.normalize_username(raw_username).strip()


def _find_existing_user(user_model, username: str):
    return user_model.objects.select_for_update().filter(username=username).first()


def _read_one_stdin_password_line() -> str:
    line = sys.stdin.readline(MAX_STDIN_PASSWORD_CHARACTERS)
    if line.endswith("\n"):
        password = line[:-1].removesuffix("\r")
    else:
        password = line
    if len(password.encode("utf-8")) > MAX_STDIN_PASSWORD_BYTES:
        raise CommandError("初始用户密码输入过长")
    if sys.stdin.read(1):
        raise CommandError("初始用户密码输入必须只有一行")
    return password


def _validate_username(user) -> None:
    try:
        user._meta.get_field("username").clean(user.username, user)
    except ValidationError as exc:
        raise CommandError("初始老板用户名格式不合法") from exc


def _validate_password(password: str, user) -> None:
    try:
        for validator in PASSWORD_VALIDATORS:
            validator.validate(password, user)
    except ValidationError as exc:
        raise CommandError(
            "初始用户密码强度不足，请使用至少 12 位且不常见的非纯数字密码"
        ) from exc


def _requires_role_assignment(user, role: Role) -> bool:
    assigned_roles = _business_roles_for(user)
    return assigned_roles != {role.value}


def _has_other_business_role(user, role: Role) -> bool:
    return bool(_business_roles_for(user) - {role.value})


def _business_roles_for(user) -> set[str]:
    business_roles = {item.value for item in Role}
    return set(user.groups.filter(name__in=business_roles).values_list("name", flat=True))


def _is_database_locked(error: OperationalError) -> bool:
    return "database is locked" in str(error).casefold() or "database table is locked" in str(
        error
    ).casefold()


def _is_username_unique_violation(error: IntegrityError, user_model) -> bool:
    username_field = user_model._meta.get_field("username")
    table_name = user_model._meta.db_table.casefold()
    column_name = username_field.column.casefold()
    message = str(error).casefold()
    if f"unique constraint failed: {table_name}.{column_name}" in message:
        return True

    cause = error.__cause__
    diagnostics = getattr(cause, "diag", None)
    constraint_name = getattr(diagnostics, "constraint_name", "")
    expected_constraint = f"{table_name}_{column_name}_key"
    return getattr(cause, "sqlstate", None) == "23505" and constraint_name == expected_constraint
