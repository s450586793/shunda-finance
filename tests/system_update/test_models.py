from uuid import UUID

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.system_update.models import SystemUpdateRequest

TASK_ID = UUID("00000000-0000-0000-0000-000000000001")


@pytest.mark.django_db
def test_update_request_keeps_a_unique_task_and_protected_requester(owner_user):
    request = SystemUpdateRequest.objects.create(
        task_id=TASK_ID,
        requested_by=owner_user,
        target_version="v0.2.1",
        result="active",
    )

    assert request.terminal_recorded_at is None
    with pytest.raises(IntegrityError), transaction.atomic():
        SystemUpdateRequest.objects.create(
            task_id=TASK_ID,
            requested_by=owner_user,
            target_version="v0.2.2",
            result="active",
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        owner_user.delete()


@pytest.mark.django_db
def test_update_request_rejects_results_outside_the_terminal_lifecycle(owner_user):
    with pytest.raises(IntegrityError), transaction.atomic():
        SystemUpdateRequest.objects.create(
            task_id=TASK_ID,
            requested_by=owner_user,
            target_version="v0.2.1",
            result="private_failure",
        )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "version", ["0.2.1", "v00.2.1", "v0.02.1", "v0.2.01", "v1.2", "v1.2.3 "]
)
def test_update_request_rejects_noncanonical_target_versions(owner_user, version):
    request = SystemUpdateRequest(
        task_id=TASK_ID,
        requested_by=owner_user,
        target_version=version,
        result="active",
    )

    with pytest.raises(ValidationError, match="版本号格式不合法"):
        request.full_clean()
