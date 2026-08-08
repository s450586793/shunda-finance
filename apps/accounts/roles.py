from enum import StrEnum

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.db import transaction


class Role(StrEnum):
    FINANCE = "财务"
    OWNER = "老板"


def _validate_role(role: Role) -> None:
    if not isinstance(role, Role):
        raise TypeError("角色必须是 Role 枚举值")


@transaction.atomic
def assign_role(user, role: Role) -> None:
    _validate_role(role)
    type(user).objects.select_for_update().get(pk=user.pk)
    group, _ = Group.objects.get_or_create(name=role.value)
    other_role_names = [item.value for item in Role if item != role]
    user.groups.remove(*Group.objects.filter(name__in=other_role_names))
    user.groups.add(group)


def user_has_role(user, role: Role) -> bool:
    _validate_role(role)
    if not user.is_authenticated:
        return False
    assigned_roles = set(
        user.groups.filter(name__in=[item.value for item in Role]).values_list(
            "name", flat=True
        )
    )
    if role == Role.FINANCE and Role.OWNER.value in assigned_roles:
        return False
    return role.value in assigned_roles


def require_finance_actor(
    actor, *, message="只有财务人员可以执行此操作"
) -> None:
    user_model = get_user_model()
    if (
        not isinstance(actor, user_model)
        or actor.pk is None
        or actor._state.adding
        or not user_model.objects.filter(pk=actor.pk).exists()
        or not user_has_role(actor, Role.FINANCE)
    ):
        raise PermissionDenied(message)
