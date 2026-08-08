import pytest
from django.contrib.auth.models import User

from apps.core.audit import record_audit
from apps.core.models import AuditLog, ImmutableLedgerModel, ImmutableQuerySet


class FutureLedger(ImmutableLedgerModel):
    class Meta(ImmutableLedgerModel.Meta):
        app_label = "core"


@pytest.mark.django_db
def test_audit_log_records_actor_action_target_and_changes():
    user = User.objects.create_user("finance")

    entry = record_audit(user, "counterparty.alias.created", user, {"value": "铁路专户"})

    saved_entry = AuditLog.objects.get(pk=entry.pk)
    assert saved_entry.actor == user
    assert saved_entry.action == "counterparty.alias.created"
    assert saved_entry.target_type == "User"
    assert saved_entry.target_id == str(user.pk)
    assert saved_entry.changes == {"value": "铁路专户"}


@pytest.mark.django_db
def test_audit_log_instance_delete_is_rejected():
    user = User.objects.create_user("finance")
    entry = record_audit(user, "counterparty.alias.created", user, {})

    with pytest.raises(RuntimeError, match="审计日志不允许删除"):
        entry.delete()

    assert AuditLog.objects.filter(pk=entry.pk).exists()


@pytest.mark.django_db
def test_audit_log_queryset_delete_is_rejected():
    user = User.objects.create_user("finance")
    entry = record_audit(user, "counterparty.alias.created", user, {})

    with pytest.raises(RuntimeError, match="正式财务记录不允许物理删除"):
        AuditLog.objects.filter(pk=entry.pk).delete()

    assert AuditLog.objects.filter(pk=entry.pk).exists()


@pytest.mark.django_db
def test_audit_log_base_manager_delete_is_rejected():
    user = User.objects.create_user("finance")
    entry = record_audit(user, "counterparty.alias.created", user, {})

    with pytest.raises(RuntimeError, match="正式财务记录不允许物理删除"):
        AuditLog._base_manager.filter(pk=entry.pk).delete()

    assert AuditLog.objects.filter(pk=entry.pk).exists()


@pytest.mark.django_db
def test_immutable_ledger_base_manager_uses_immutable_queryset():
    assert isinstance(FutureLedger._base_manager.get_queryset(), ImmutableQuerySet)


@pytest.mark.django_db
@pytest.mark.parametrize("actor", [None, User(username="finance")])
def test_record_audit_rejects_invalid_actor(actor):
    target = User.objects.create_user("target")

    with pytest.raises(ValueError, match="审计操作人必须是已保存的用户"):
        record_audit(actor, "counterparty.alias.created", target, {})

    assert not AuditLog.objects.exists()


@pytest.mark.django_db
def test_record_audit_rejects_actor_deleted_by_queryset():
    actor = User.objects.create_user("finance")
    target = User.objects.create_user("target")

    User.objects.filter(pk=actor.pk).delete()

    assert actor.pk is not None
    with pytest.raises(ValueError, match="审计操作人必须是已保存的用户"):
        record_audit(actor, "counterparty.alias.created", target, {})

    assert not AuditLog.objects.exists()


@pytest.mark.django_db
def test_record_audit_rejects_target_without_persistent_primary_key():
    actor = User.objects.create_user("finance")
    target = User(username="target")

    with pytest.raises(ValueError, match="审计目标必须有持久化主键"):
        record_audit(actor, "counterparty.alias.created", target, {})

    assert not AuditLog.objects.exists()
