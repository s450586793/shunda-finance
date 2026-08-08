from datetime import UTC, datetime
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from apps.imports.choices import SourceKind
from apps.imports.models import ImportBatch
from apps.ledger.choices import InvoiceDirection, MoneyChannel, MoneyDirection
from apps.ledger.models import AccountBalanceSnapshot, FundingAccount
from tests.builders import make_invoice, make_transaction


@pytest.mark.django_db
@pytest.mark.parametrize("amount", [Decimal("0.00"), Decimal("-0.01")])
def test_invoice_amount_must_be_positive(finance_user, amount):
    with pytest.raises(IntegrityError), transaction.atomic():
        make_invoice(finance_user, total_amount=amount)


@pytest.mark.django_db
@pytest.mark.parametrize("amount", [Decimal("0.00"), Decimal("-0.01")])
def test_transaction_amount_must_be_positive(finance_user, amount):
    with pytest.raises(IntegrityError), transaction.atomic():
        make_transaction(finance_user, amount=amount)


@pytest.mark.django_db
@pytest.mark.parametrize("direction", [MoneyDirection.INFLOW, MoneyDirection.OUTFLOW])
def test_transaction_direction_is_explicit(finance_user, direction):
    tx = make_transaction(
        finance_user,
        direction=direction,
        amount=Decimal("2044.00"),
    )

    assert tx.amount == Decimal("2044.00")
    assert tx.direction == direction


@pytest.mark.django_db
def test_transaction_rejects_blank_direction(finance_user):
    with pytest.raises(IntegrityError), transaction.atomic():
        make_transaction(finance_user, direction="")


@pytest.mark.django_db
def test_transaction_rejects_unsupported_channel(finance_user):
    money = make_transaction(finance_user)
    money.channel = "cash"

    with pytest.raises(IntegrityError), transaction.atomic():
        money.save(update_fields=["channel"])


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("direction", "source_kind"),
    [
        (InvoiceDirection.INPUT, SourceKind.INPUT_INVOICE),
        (InvoiceDirection.OUTPUT, SourceKind.OUTPUT_INVOICE),
    ],
)
def test_invoice_builder_maps_direction_to_import_source(
    finance_user, direction, source_kind
):
    invoice = make_invoice(finance_user, direction=direction)

    assert invoice.import_batch.source_kind == source_kind


@pytest.mark.django_db
def test_invoice_builder_rejects_unsupported_direction(finance_user):
    with pytest.raises(ValueError, match="发票方向必须是进项或销项"):
        make_invoice(finance_user, direction="invalid")


@pytest.mark.django_db
def test_invoice_number_and_seller_tax_id_are_unique(finance_user):
    first = make_invoice(
        finance_user,
        invoice_number="26000000000000000001",
        seller_tax_id="913200000000000001",
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        make_invoice(
            finance_user,
            counterparty=first.counterparty,
            invoice_number=first.invoice_number,
            seller_tax_id=first.seller_tax_id,
        )


@pytest.mark.django_db
def test_same_invoice_number_is_allowed_for_different_sellers(finance_user):
    first = make_invoice(
        finance_user,
        invoice_number="26000000000000000001",
        seller_tax_id="913200000000000001",
    )
    second = make_invoice(
        finance_user,
        invoice_number=first.invoice_number,
        seller_tax_id="913200000000000002",
    )

    assert first.invoice_number == second.invoice_number


@pytest.mark.django_db
def test_transaction_fingerprint_is_unique(finance_user):
    fingerprint = "a" * 64
    first = make_transaction(finance_user, fingerprint=fingerprint)

    with pytest.raises(IntegrityError), transaction.atomic():
        make_transaction(
            finance_user,
            counterparty=first.counterparty,
            fingerprint=fingerprint,
        )


@pytest.mark.django_db
def test_balance_snapshot_is_unique_per_account_and_timestamp(finance_user):
    account = FundingAccount.objects.create(
        channel=MoneyChannel.BANK,
        name="农业银行",
        identifier="1064330104009859",
        masked_identifier="************9859",
    )
    batch = ImportBatch.objects.create(
        source_kind=SourceKind.BANK,
        created_by=finance_user,
    )
    as_of = datetime(2026, 7, 1, 17, 0, tzinfo=UTC)
    AccountBalanceSnapshot.objects.create(
        account=account,
        as_of=as_of,
        balance=Decimal("50000.00"),
        source_batch=batch,
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        AccountBalanceSnapshot.objects.create(
            account=account,
            as_of=as_of,
            balance=Decimal("50000.00"),
            source_batch=batch,
        )


@pytest.mark.django_db
@pytest.mark.parametrize("ledger_kind", ["invoice", "transaction", "snapshot"])
def test_formal_ledger_instance_cannot_be_deleted(finance_user, ledger_kind):
    record = _make_ledger_record(finance_user, ledger_kind)

    with pytest.raises(RuntimeError, match="正式财务记录不允许物理删除"):
        record.delete()

    assert type(record).objects.filter(pk=record.pk).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("ledger_kind", ["invoice", "transaction", "snapshot"])
def test_formal_ledger_default_manager_cannot_delete(finance_user, ledger_kind):
    record = _make_ledger_record(finance_user, ledger_kind)

    with pytest.raises(RuntimeError, match="正式财务记录不允许物理删除"):
        type(record).objects.filter(pk=record.pk).delete()

    assert type(record).objects.filter(pk=record.pk).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("ledger_kind", ["invoice", "transaction", "snapshot"])
def test_formal_ledger_base_manager_cannot_delete(finance_user, ledger_kind):
    record = _make_ledger_record(finance_user, ledger_kind)

    with pytest.raises(RuntimeError, match="正式财务记录不允许物理删除"):
        type(record)._base_manager.filter(pk=record.pk).delete()

    assert type(record).objects.filter(pk=record.pk).exists()


def _make_ledger_record(actor, ledger_kind):
    if ledger_kind == "invoice":
        return make_invoice(actor)
    if ledger_kind == "transaction":
        return make_transaction(actor)

    account = FundingAccount.objects.create(
        channel=MoneyChannel.BANK,
        name="快照测试账户",
        identifier="snapshot-account",
        masked_identifier="******ount",
    )
    batch = ImportBatch.objects.create(source_kind=SourceKind.BANK, created_by=actor)
    return AccountBalanceSnapshot.objects.create(
        account=account,
        as_of=datetime(2026, 7, 1, 17, 0, tzinfo=UTC),
        balance=Decimal("50000.00"),
        source_batch=batch,
    )
