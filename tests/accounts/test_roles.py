import pytest
from django.contrib.auth.models import AnonymousUser, Group, User
from django.core.exceptions import PermissionDenied

from apps.accounts.roles import Role, assign_role, require_finance_actor, user_has_role


@pytest.mark.django_db
def test_post_migrate_creates_finance_and_owner_groups():
    role_names = {role.value for role in Role}
    group_names = set(Group.objects.filter(name__in=role_names).values_list("name", flat=True))

    assert group_names == role_names


@pytest.mark.django_db
def test_finance_and_owner_roles_are_distinct():
    user = User.objects.create_user("finance", password="secret")

    assign_role(user, Role.FINANCE)

    assert user_has_role(user, Role.FINANCE)
    assert not user_has_role(user, Role.OWNER)


@pytest.mark.django_db
def test_assign_role_replaces_the_other_business_role_and_is_idempotent():
    user = User.objects.create_user("role-switch")

    assign_role(user, Role.FINANCE)
    assign_role(user, Role.OWNER)
    assign_role(user, Role.OWNER)

    assert set(user.groups.values_list("name", flat=True)) == {Role.OWNER.value}
    assert user_has_role(user, Role.OWNER)
    assert not user_has_role(user, Role.FINANCE)

    assign_role(user, Role.FINANCE)

    assert set(user.groups.values_list("name", flat=True)) == {Role.FINANCE.value}
    assert user_has_role(user, Role.FINANCE)
    assert not user_has_role(user, Role.OWNER)


@pytest.mark.django_db
def test_existing_dual_role_user_is_owner_priority_read_only():
    user = User.objects.create_user("legacy-dual-role")
    user.groups.add(
        Group.objects.get(name=Role.FINANCE.value),
        Group.objects.get(name=Role.OWNER.value),
    )

    assert user_has_role(user, Role.OWNER)
    assert not user_has_role(user, Role.FINANCE)
    with pytest.raises(PermissionDenied, match="财务"):
        require_finance_actor(user)


def test_anonymous_user_has_no_role():
    assert not user_has_role(AnonymousUser(), Role.FINANCE)


@pytest.mark.django_db
@pytest.mark.parametrize("role", ["财务", object()])
def test_assign_role_rejects_values_outside_role_enum(role):
    user = User.objects.create_user("finance")

    with pytest.raises(TypeError, match="角色必须是 Role 枚举值"):
        assign_role(user, role)


@pytest.mark.django_db
def test_user_has_role_rejects_values_outside_role_enum():
    user = User.objects.create_user("finance")

    with pytest.raises(TypeError, match="角色必须是 Role 枚举值"):
        user_has_role(user, "财务")


@pytest.mark.django_db
def test_require_finance_actor_accepts_current_finance_user(finance_user):
    assert require_finance_actor(finance_user) is None


@pytest.mark.django_db
def test_require_finance_actor_rejects_invalid_and_stale_users():
    ordinary = User.objects.create_user("ordinary")
    unsaved = User(username="unsaved")
    stale = User.objects.create_user("stale-finance")
    assign_role(stale, Role.FINANCE)
    stale.delete()

    for actor in (ordinary, unsaved, stale, AnonymousUser(), None):
        with pytest.raises(PermissionDenied, match="custom denial"):
            require_finance_actor(actor, message="custom denial")
