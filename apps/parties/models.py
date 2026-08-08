# ruff: noqa: RUF012

from django.conf import settings
from django.db import models

from apps.core.models import UUIDModel


class AliasKind(models.TextChoices):
    NAME = "name", "名称"
    BANK_ACCOUNT = "bank_account", "银行账号"
    WECHAT_NAME = "wechat_name", "微信名称"
    COLLECTION_ACCOUNT = "collection_account", "代收账户"


class Counterparty(UUIDModel):
    name = models.CharField(max_length=255)
    normalized_name = models.CharField(max_length=255, db_index=True)
    tax_id = models.CharField(max_length=32, blank=True, db_index=True)
    is_customer = models.BooleanField(default=False)
    is_supplier = models.BooleanField(default=False)
    active = models.BooleanField(default=True)


class CounterpartyAlias(UUIDModel):
    counterparty = models.ForeignKey(
        Counterparty,
        on_delete=models.PROTECT,
        related_name="aliases",
    )
    kind = models.CharField(max_length=20, choices=AliasKind.choices)
    value = models.CharField(max_length=255)
    normalized_value = models.CharField(max_length=255)
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["kind", "normalized_value"],
                name="uniq_party_alias_kind_value",
            ),
        ]
