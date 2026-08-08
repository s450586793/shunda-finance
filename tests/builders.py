from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256
from uuid import uuid4

from apps.imports.choices import SourceKind
from apps.imports.models import ImportBatch
from apps.ledger.choices import (
    InvoiceDirection,
    InvoiceStatus,
    MoneyChannel,
    MoneyDirection,
)
from apps.ledger.models import FundingAccount, Invoice, MoneyTransaction
from apps.parties.models import Counterparty


def make_invoice(
    actor,
    *,
    direction=InvoiceDirection.INPUT,
    total_amount=Decimal("1000.00"),
    counterparty=None,
    invoice_number=None,
    seller_tax_id="913200000000000001",
):
    if direction == InvoiceDirection.INPUT:
        source_kind = SourceKind.INPUT_INVOICE
    elif direction == InvoiceDirection.OUTPUT:
        source_kind = SourceKind.OUTPUT_INVOICE
    else:
        raise ValueError("发票方向必须是进项或销项")
    batch = ImportBatch.objects.create(source_kind=source_kind, created_by=actor)
    party = counterparty or Counterparty.objects.create(
        name="测试单位",
        normalized_name=uuid4().hex,
        is_supplier=True,
    )
    return Invoice.objects.create(
        direction=direction,
        invoice_number=invoice_number or uuid4().hex[:20],
        seller_tax_id=seller_tax_id,
        buyer_tax_id="91320281TEST000001",
        issue_date=date(2026, 7, 1),
        total_amount=total_amount,
        status=InvoiceStatus.NORMAL,
        counterparty=party,
        import_batch=batch,
        source_row=1,
    )


def make_transaction(
    actor,
    *,
    direction=MoneyDirection.OUTFLOW,
    amount=Decimal("1000.00"),
    counterparty=None,
    fingerprint=None,
):
    batch = ImportBatch.objects.create(source_kind=SourceKind.BANK, created_by=actor)
    party = counterparty or Counterparty.objects.create(
        name="测试收款方",
        normalized_name=uuid4().hex,
        is_supplier=True,
    )
    account, _ = FundingAccount.objects.get_or_create(
        identifier="1064330104009859",
        defaults={
            "channel": MoneyChannel.BANK,
            "name": "测试银行账户",
            "masked_identifier": "************9859",
        },
    )
    return MoneyTransaction.objects.create(
        account=account,
        channel=MoneyChannel.BANK,
        direction=direction,
        occurred_at=datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
        amount=amount,
        fingerprint=fingerprint or sha256(uuid4().bytes).hexdigest(),
        counterparty=party,
        counterparty_raw_name=party.name,
        import_batch=batch,
        source_row=1,
    )
