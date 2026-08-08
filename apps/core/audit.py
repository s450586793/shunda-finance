from django.contrib.auth import get_user_model
from django.db import models

from .models import AuditLog


def record_audit(actor, action: str, target, changes: dict) -> AuditLog:
    user_model = get_user_model()
    if (
        not isinstance(actor, user_model)
        or actor.pk is None
        or actor._state.adding
        or not user_model.objects.filter(pk=actor.pk).exists()
    ):
        raise ValueError("审计操作人必须是已保存的用户")
    if not isinstance(target, models.Model) or target.pk is None or target._state.adding:
        raise ValueError("审计目标必须有持久化主键")

    return AuditLog.objects.create(
        actor=actor,
        action=action,
        target_type=target.__class__.__name__,
        target_id=str(target.pk),
        changes=changes,
    )
