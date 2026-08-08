# ruff: noqa: RUF012

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.db.models.fields.files import FieldFile, FileField

from apps.core.models import ImmutableLedgerModel, UUIDModel
from apps.imports.models import ImportBatch
from apps.parties.models import Counterparty

from .choices import (
    InvoiceDirection,
    InvoiceStatus,
    MoneyChannel,
    MoneyDirection,
)


class AttachmentStatus(models.TextChoices):
    LINKED = "linked", "已关联"
    UNCLAIMED = "unclaimed", "待认领"


class ProtectedAttachmentFieldFile(FieldFile):
    def delete(self, save=True):
        raise RuntimeError("附件原始文件不允许物理删除")


class ProtectedAttachmentFileField(FileField):
    attr_class = ProtectedAttachmentFieldFile

    def deconstruct(self):
        name, _path, args, kwargs = super().deconstruct()
        return name, "django.db.models.FileField", args, kwargs


class FundingAccount(UUIDModel):
    channel = models.CharField(max_length=12, choices=MoneyChannel.choices)
    name = models.CharField(max_length=100)
    identifier = models.CharField(max_length=100)
    masked_identifier = models.CharField(max_length=100)
    active = models.BooleanField(default=True)


class Invoice(ImmutableLedgerModel):
    direction = models.CharField(max_length=10, choices=InvoiceDirection.choices)
    invoice_number = models.CharField(max_length=30)
    seller_tax_id = models.CharField(max_length=32)
    buyer_tax_id = models.CharField(max_length=32)
    issue_date = models.DateField()
    due_date = models.DateField(null=True, blank=True)
    total_amount = models.DecimalField(max_digits=18, decimal_places=2)
    status = models.CharField(max_length=12, choices=InvoiceStatus.choices)
    counterparty = models.ForeignKey(Counterparty, on_delete=models.PROTECT)
    import_batch = models.ForeignKey(ImportBatch, on_delete=models.PROTECT)
    source_row = models.PositiveIntegerField()
    source_payload = models.JSONField(default=dict)

    class Meta(ImmutableLedgerModel.Meta):
        constraints = [
            models.CheckConstraint(
                condition=models.Q(total_amount__gt=0),
                name="invoice_total_amount_positive",
            ),
            models.UniqueConstraint(
                fields=["invoice_number", "seller_tax_id"],
                name="uniq_invoice_number_seller_tax_id",
            ),
        ]


class Attachment(ImmutableLedgerModel):
    file = ProtectedAttachmentFileField(upload_to="attachments/%Y/%m/")
    original_name = models.CharField(max_length=255)
    sha256 = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=12, choices=AttachmentStatus.choices)
    content_type = models.ForeignKey(
        ContentType,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    object_id = models.UUIDField(null=True, blank=True)
    target = GenericForeignKey("content_type", "object_id")
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    disabled_at = models.DateTimeField(null=True, blank=True)

    class Meta(ImmutableLedgerModel.Meta):
        constraints = [
            models.CheckConstraint(
                condition=models.Q(status__in=AttachmentStatus.values),
                name="attachment_status_valid",
            ),
        ]


class MoneyTransaction(ImmutableLedgerModel):
    account = models.ForeignKey(FundingAccount, on_delete=models.PROTECT)
    channel = models.CharField(max_length=12, choices=MoneyChannel.choices)
    direction = models.CharField(max_length=10, choices=MoneyDirection.choices)
    occurred_at = models.DateTimeField()
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    balance_after = models.DecimalField(
        max_digits=18,
        decimal_places=2,
        null=True,
        blank=True,
    )
    transaction_id = models.CharField(max_length=100, blank=True)
    fingerprint = models.CharField(max_length=64, unique=True)
    counterparty = models.ForeignKey(
        Counterparty,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    counterparty_raw_name = models.CharField(max_length=255, blank=True)
    counterparty_account = models.CharField(max_length=100, blank=True)
    import_batch = models.ForeignKey(ImportBatch, on_delete=models.PROTECT)
    source_row = models.PositiveIntegerField()
    source_payload = models.JSONField(default=dict)

    class Meta(ImmutableLedgerModel.Meta):
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name="money_transaction_amount_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(channel__in=MoneyChannel.values),
                name="money_transaction_channel_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(direction__in=MoneyDirection.values),
                name="money_transaction_direction_valid",
            ),
        ]


class AccountBalanceSnapshot(ImmutableLedgerModel):
    account = models.ForeignKey(
        FundingAccount,
        on_delete=models.PROTECT,
        related_name="balance_snapshots",
    )
    as_of = models.DateTimeField()
    balance = models.DecimalField(max_digits=18, decimal_places=2)
    source_batch = models.ForeignKey(ImportBatch, on_delete=models.PROTECT)

    class Meta(ImmutableLedgerModel.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["account", "as_of"],
                name="uniq_account_balance_snapshot",
            ),
        ]
