from uuid import uuid4

from django.conf import settings
from django.db import models


class UUIDModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)

    class Meta:
        abstract = True


class ImmutableQuerySet(models.QuerySet):
    def delete(self):
        raise RuntimeError("正式财务记录不允许物理删除")


class ImmutableLedgerModel(UUIDModel):
    objects = ImmutableQuerySet.as_manager()

    class Meta:
        abstract = True
        base_manager_name = "objects"

    def delete(self, *args, **kwargs):
        raise RuntimeError("正式财务记录不允许物理删除")


class AuditLog(models.Model):
    objects = ImmutableQuerySet.as_manager()

    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    action = models.CharField(max_length=100)
    target_type = models.CharField(max_length=100)
    target_id = models.CharField(max_length=64)
    changes = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        base_manager_name = "objects"

    def delete(self, *args, **kwargs):
        raise RuntimeError("审计日志不允许删除")
