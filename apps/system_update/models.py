# ruff: noqa: RUF012

import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

_RELEASE_VERSION_PATTERN = re.compile(
    r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)
SYSTEM_UPDATE_RESULT_VALUES = (
    "active",
    "succeeded",
    "failed",
    "manual_intervention",
)


def validate_release_version(value):
    if not isinstance(value, str) or _RELEASE_VERSION_PATTERN.fullmatch(value) is None:
        raise ValidationError("版本号格式不合法")


class SystemUpdateRequest(models.Model):
    class Result(models.TextChoices):
        ACTIVE = "active", "进行中"
        SUCCEEDED = "succeeded", "成功"
        FAILED = "failed", "失败"
        MANUAL_INTERVENTION = "manual_intervention", "需要人工处理"

    task_id = models.UUIDField(unique=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    target_version = models.CharField(
        max_length=32, validators=[validate_release_version]
    )
    result = models.CharField(max_length=24, choices=Result.choices)
    terminal_recorded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(result__in=SYSTEM_UPDATE_RESULT_VALUES),
                name="system_update_request_result_valid",
            )
        ]
