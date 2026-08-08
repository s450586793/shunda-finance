from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.core.models import AuditLog
from apps.imports.choices import SourceKind
from apps.imports.models import ImportBatch
from apps.imports.services import DuplicateSourceFileError, confirm_batch, stage_upload
from apps.ledger.choices import MoneyChannel
from apps.ledger.models import (
    AccountBalanceSnapshot,
    FundingAccount,
    Invoice,
    MoneyTransaction,
)
from apps.parties.models import Counterparty
from apps.reconciliation.candidates import transaction_candidates
from apps.reconciliation.choices import ReconciliationDirection
from apps.reconciliation.queries import invoice_open_amount, transaction_open_amount
from apps.reconciliation.services import AllocationInput, create_reconciliation

FIXTURES = Path(__file__).parents[1] / "fixtures" / "synthetic_railway"
COMPANY_TAX_ID = "91320281TEST000001"
SUPPLIER_TAX_ID = "91310000TEST000001"
BANK_ACCOUNT_ID = "TEST-BANK-RAIL-0001"
RAILWAY_NAME = "测试铁路物流收款专户"


def _upload(path: Path, *, name: str | None = None, suffix: bytes = b""):
    return SimpleUploadedFile(name or path.name, path.read_bytes() + suffix)


def _import_and_confirm(path, *, source_kind, actor, name=None, suffix=b""):
    batch = stage_upload(
        _upload(path, name=name, suffix=suffix),
        source_kind=source_kind,
        actor=actor,
    )
    result = confirm_batch(batch.id, actor)
    batch.refresh_from_db()
    return batch, result


@pytest.mark.django_db(transaction=True)
def test_synthetic_railway_batch_keeps_explicit_1000_difference(
    finance_user,
    owner_user,
    settings,
    tmp_path,
):
    settings.COMPANY_TAX_ID = COMPANY_TAX_ID
    settings.MEDIA_ROOT = tmp_path / "uploads"
    supplier = Counterparty.objects.create(
        name=RAILWAY_NAME,
        normalized_name=RAILWAY_NAME,
        tax_id=SUPPLIER_TAX_ID,
        is_supplier=True,
    )
    account = FundingAccount.objects.create(
        channel=MoneyChannel.BANK,
        name="脱敏农行结算账户",
        identifier=BANK_ACCOUNT_ID,
        masked_identifier="*********0001",
    )

    invoice_batch, invoice_result = _import_and_confirm(
        FIXTURES / "input_invoices.xlsx",
        source_kind=SourceKind.INPUT_INVOICE,
        actor=finance_user,
    )
    bank_batch, bank_result = _import_and_confirm(
        FIXTURES / "bank_june.xls",
        source_kind=SourceKind.BANK,
        actor=finance_user,
    )

    assert invoice_result.posted_rows == 2
    assert bank_result.posted_rows == 13
    assert Invoice.objects.count() == 2
    assert MoneyTransaction.objects.count() == 13
    assert list(
        Invoice.objects.order_by("total_amount").values_list(
            "issue_date", "total_amount", flat=False
        )
    ) == [
        (date(2026, 7, 7), Decimal("2000.00")),
        (date(2026, 7, 7), Decimal("46050.00")),
    ]
    assert sum(
        MoneyTransaction.objects.values_list("amount", flat=True), Decimal("0.00")
    ) == Decimal("47050.00")
    assert set(MoneyTransaction.objects.values_list("counterparty_id", flat=True)) == {
        supplier.id
    }

    snapshot = AccountBalanceSnapshot.objects.get()
    assert snapshot.account == account
    assert snapshot.balance == Decimal("50000.00")
    assert snapshot.source_batch == bank_batch

    with pytest.raises(PermissionDenied, match="只有财务人员可以执行核销"):
        create_reconciliation(
            actor=owner_user,
            direction=ReconciliationDirection.PURCHASE_PAYMENT,
            allocations=[
                AllocationInput(
                    Invoice.objects.get(total_amount=Decimal("2000.00")).id,
                    MoneyTransaction.objects.get(
                        occurred_at__date=date(2026, 6, 16)
                    ).id,
                    Decimal("2000.00"),
                )
            ],
        )

    invoice_2000 = Invoice.objects.get(total_amount=Decimal("2000.00"))
    payment_2000 = MoneyTransaction.objects.get(
        occurred_at__date=date(2026, 6, 16)
    )
    create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        allocations=[
            AllocationInput(invoice_2000.id, payment_2000.id, Decimal("2000.00"))
        ],
    )

    invoice_46050 = Invoice.objects.get(total_amount=Decimal("46050.00"))
    remaining_june = MoneyTransaction.objects.filter(
        occurred_at__date__range=(date(2026, 6, 1), date(2026, 6, 30))
    ).exclude(pk=payment_2000.pk)
    assert remaining_june.count() == 12
    assert sum(
        (transaction_open_amount(item.id) for item in remaining_june),
        Decimal("0.00"),
    ) == Decimal("45050.00")
    assert invoice_open_amount(invoice_46050.id) == Decimal("46050.00")
    assert [
        (candidate.kind, candidate.total, candidate.difference)
        for candidate in transaction_candidates(
            invoice_46050.id,
            start=date(2026, 6, 1),
            end=date(2026, 6, 30),
        )
    ] == [("PARTIAL", Decimal("45050.00"), Decimal("1000.00"))]

    create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        allocations=[
            AllocationInput(
                invoice_46050.id,
                payment.id,
                transaction_open_amount(payment.id),
            )
            for payment in remaining_june.order_by("occurred_at")
        ],
        note="测试铁路物流 2026 年 6 月结算，尚差 1,000.00 元",
    )
    assert invoice_open_amount(invoice_46050.id) == Decimal("1000.00")
    assert transaction_open_amount(payment_2000.id) == Decimal("0.00")
    with pytest.raises(ValidationError, match="资金可核销金额不足"):
        create_reconciliation(
            actor=finance_user,
            direction=ReconciliationDirection.PURCHASE_PAYMENT,
            allocations=[
                AllocationInput(invoice_46050.id, payment_2000.id, Decimal("0.01"))
            ],
        )
    assert AccountBalanceSnapshot.objects.get().balance == Decimal("50000.00")
    assert invoice_open_amount(invoice_46050.id) == Decimal("1000.00")

    with pytest.raises(DuplicateSourceFileError, match="相同 SHA-256"):
        stage_upload(
            _upload(FIXTURES / "input_invoices.xlsx"),
            source_kind=SourceKind.INPUT_INVOICE,
            actor=finance_user,
        )
    overlap_batch, overlap_result = _import_and_confirm(
        FIXTURES / "bank_june.xls",
        name="bank-june-overlap.xls",
        suffix=b"\x00",
        source_kind=SourceKind.BANK,
        actor=finance_user,
    )
    assert overlap_result.posted_rows == 0
    assert overlap_batch.duplicate_rows == 13
    assert Invoice.objects.count() == 2
    assert MoneyTransaction.objects.count() == 13
    assert AccountBalanceSnapshot.objects.count() == 1

    assert ImportBatch.objects.count() == 3
    assert AuditLog.objects.filter(action="import.staged").count() == 3
    assert AuditLog.objects.filter(action="import.confirmed").count() == 3
    assert AuditLog.objects.filter(action="reconciliation.created").count() == 2
    assert not AuditLog.objects.filter(actor=owner_user).exists()
    assert invoice_batch.confirmed_at is not None
    assert overlap_batch.confirmed_at is not None
