from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from apps.imports.choices import SourceKind
from apps.imports.models import ImportBatch
from apps.ledger.choices import (
    InvoiceDirection,
    InvoiceStatus,
    MoneyChannel,
    MoneyDirection,
)
from apps.ledger.models import AccountBalanceSnapshot, FundingAccount
from apps.parties.models import Counterparty
from apps.reconciliation.choices import ReconciliationDirection, SettlementStatus
from apps.reconciliation.models import Reconciliation, SettlementBatch
from apps.reconciliation.services import AllocationInput, create_reconciliation
from tests.builders import make_invoice, make_transaction


@pytest.fixture
def settlement_records(finance_user):
    party = Counterparty.objects.create(
        name="结算测试单位",
        normalized_name="结算测试单位",
        is_supplier=True,
    )
    invoice = make_invoice(
        finance_user,
        total_amount=Decimal("100.00"),
        counterparty=party,
    )
    invoice.issue_date = date(2026, 6, 10)
    invoice.save(update_fields=["issue_date"])
    money = make_transaction(
        finance_user,
        direction=MoneyDirection.OUTFLOW,
        amount=Decimal("100.00"),
        counterparty=party,
    )
    money.occurred_at = datetime(2026, 6, 15, 9, tzinfo=UTC)
    money.save(update_fields=["occurred_at"])
    return party, invoice, money


def _draft_payload(party):
    return {
        "counterparty": str(party.id),
        "direction": ReconciliationDirection.PURCHASE_PAYMENT,
        "period_start": "2026-06-01",
        "period_end": "2026-06-30",
    }


@pytest.mark.django_db
def test_create_settlement_draft_adds_only_open_actual_matching_records(
    finance_client, finance_user, settlement_records
):
    party, invoice, money = settlement_records
    wrong_invoice = make_invoice(
        finance_user,
        direction=InvoiceDirection.OUTPUT,
        total_amount=Decimal("100.00"),
        counterparty=party,
    )
    wrong_invoice.issue_date = date(2026, 6, 11)
    wrong_invoice.save(update_fields=["issue_date"])
    void_invoice = make_invoice(
        finance_user,
        total_amount=Decimal("100.00"),
        counterparty=party,
    )
    void_invoice.issue_date = date(2026, 6, 12)
    void_invoice.status = InvoiceStatus.VOID
    void_invoice.save(update_fields=["issue_date", "status"])
    outside_invoice = make_invoice(
        finance_user,
        total_amount=Decimal("100.00"),
        counterparty=party,
    )
    outside_invoice.issue_date = date(2026, 7, 1)
    outside_invoice.save(update_fields=["issue_date"])
    other_party_invoice = make_invoice(finance_user, total_amount=Decimal("100.00"))
    used_invoice = make_invoice(
        finance_user,
        total_amount=Decimal("50.00"),
        counterparty=party,
    )
    used_invoice.issue_date = date(2026, 6, 13)
    used_invoice.save(update_fields=["issue_date"])
    used_money = make_transaction(
        finance_user,
        direction=MoneyDirection.OUTFLOW,
        amount=Decimal("50.00"),
        counterparty=party,
    )
    used_money.occurred_at = datetime(2026, 6, 13, 9, tzinfo=UTC)
    used_money.save(update_fields=["occurred_at"])
    create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        allocations=[AllocationInput(used_invoice.id, used_money.id, Decimal("50.00"))],
    )
    wrong_money = make_transaction(
        finance_user,
        direction=MoneyDirection.INFLOW,
        amount=Decimal("100.00"),
        counterparty=party,
    )
    wrong_money.occurred_at = datetime(2026, 6, 16, 9, tzinfo=UTC)
    wrong_money.save(update_fields=["occurred_at"])
    outside_money = make_transaction(
        finance_user,
        direction=MoneyDirection.OUTFLOW,
        amount=Decimal("100.00"),
        counterparty=party,
    )
    outside_money.occurred_at = datetime(2026, 7, 1, 9, tzinfo=UTC)
    outside_money.save(update_fields=["occurred_at"])
    account = FundingAccount.objects.create(
        channel=MoneyChannel.BANK,
        name="余额账户",
        identifier="balance-only",
        masked_identifier="********0010",
    )
    source_batch = ImportBatch.objects.create(
        source_kind=SourceKind.BANK,
        created_by=finance_user,
    )
    snapshot = AccountBalanceSnapshot.objects.create(
        account=account,
        as_of=datetime(2026, 6, 30, tzinfo=UTC),
        balance=Decimal("100.00"),
        source_batch=source_batch,
    )

    response = finance_client.post(
        "/reconciliation/settlements/create/", _draft_payload(party)
    )

    assert response.status_code == 302
    batch = SettlementBatch.objects.get()
    assert list(batch.invoice_items.values_list("invoice_id", flat=True)) == [invoice.id]
    assert list(batch.transaction_items.values_list("transaction_id", flat=True)) == [
        money.id
    ]
    assert not batch.invoice_items.filter(invoice_id=other_party_invoice.id).exists()
    serialized = finance_client.get(response.headers["Location"]).content.decode()
    assert str(snapshot.id) not in serialized
    assert "balance-only" not in serialized


@pytest.mark.django_db
def test_empty_settlement_draft_is_rejected_without_persisting_batch(
    finance_client, finance_user
):
    party = Counterparty.objects.create(
        name="空结算单位",
        normalized_name="空结算单位",
        is_supplier=True,
    )

    response = finance_client.post(
        "/reconciliation/settlements/create/", _draft_payload(party)
    )

    assert response.status_code == 400
    assert "结算期间内没有可核销的发票" in response.content.decode()
    assert not SettlementBatch.objects.exists()


@pytest.mark.django_db
def test_settlement_form_rejects_reversed_period(finance_client, settlement_records):
    party, _invoice, _money = settlement_records
    payload = _draft_payload(party)
    payload.update(period_start="2026-07-01", period_end="2026-06-30")

    response = finance_client.post("/reconciliation/settlements/create/", payload)

    assert response.status_code == 400
    assert "开始日期不能晚于结束日期" in response.content.decode()
    assert not SettlementBatch.objects.exists()


@pytest.mark.django_db
def test_settlement_form_uses_safe_chinese_errors_for_invalid_fields(finance_client):
    response = finance_client.post(
        "/reconciliation/settlements/create/",
        {
            "counterparty": "not-a-uuid",
            "direction": "invalid",
            "period_start": "not-a-date",
            "period_end": "also-not-a-date",
        },
    )

    body = response.content.decode()
    assert response.status_code == 400
    assert "不是一个有效的UUID" in body
    assert "核销方向不合法" in body
    assert "开始日期不合法" in body
    assert "结束日期不合法" in body
    assert "valid choice" not in body


@pytest.mark.django_db
def test_settlement_confirm_rejects_duplicate_allocation_pair(
    finance_client, settlement_records
):
    party, invoice, money = settlement_records
    finance_client.post("/reconciliation/settlements/create/", _draft_payload(party))
    batch = SettlementBatch.objects.get()

    response = finance_client.post(
        f"/reconciliation/settlements/{batch.id}/confirm/",
        {
            "version": str(batch.version),
            "invoice_id": [str(invoice.id), str(invoice.id)],
            "transaction_id": [str(money.id), str(money.id)],
            "amount": ["50.00", "50.00"],
        },
    )

    assert response.status_code == 400
    assert "结算批次核销明细不能重复" in response.content.decode()
    assert not Reconciliation.objects.exists()


@pytest.mark.django_db
def test_settlement_detail_shows_totals_and_explicit_allocation_rows(
    finance_client, settlement_records
):
    party, invoice, money = settlement_records
    created = finance_client.post(
        "/reconciliation/settlements/create/", _draft_payload(party)
    )

    detail = finance_client.get(created.headers["Location"])
    body = detail.content.decode()
    assert detail.status_code == 200
    assert "发票合计" in body
    assert "资金合计" in body
    assert "100.00" in body
    assert f'name="invoice_id" value="{invoice.id}"' in body
    assert f'name="transaction_id" value="{money.id}"' in body
    assert 'name="amount" value="100.00"' in body
    assert 'name="version" value="1"' in body


@pytest.mark.django_db
def test_settlement_confirm_uses_version_and_creates_no_partial_state_on_stale(
    finance_client, settlement_records
):
    party, invoice, money = settlement_records
    finance_client.post(
        "/reconciliation/settlements/create/", _draft_payload(party)
    )
    batch = SettlementBatch.objects.get()
    confirm_url = f"/reconciliation/settlements/{batch.id}/confirm/"
    payload = {
        "version": "2",
        "invoice_id": str(invoice.id),
        "transaction_id": str(money.id),
        "amount": "100.00",
    }

    stale = finance_client.post(confirm_url, payload)

    assert stale.status_code == 409
    assert "结算批次版本已过期" in stale.content.decode()
    batch.refresh_from_db()
    assert batch.status == SettlementStatus.DRAFT
    assert batch.version == 1
    assert not Reconciliation.objects.exists()


@pytest.mark.django_db
def test_settlement_confirm_rejects_incomplete_rows_without_partial_ledger(
    finance_client, finance_user, settlement_records
):
    party, invoice, money = settlement_records
    second_invoice = make_invoice(
        finance_user,
        total_amount=Decimal("50.00"),
        counterparty=party,
    )
    second_invoice.issue_date = date(2026, 6, 11)
    second_invoice.save(update_fields=["issue_date"])
    second_money = make_transaction(
        finance_user,
        direction=MoneyDirection.OUTFLOW,
        amount=Decimal("50.00"),
        counterparty=party,
    )
    second_money.occurred_at = datetime(2026, 6, 16, 9, tzinfo=UTC)
    second_money.save(update_fields=["occurred_at"])
    finance_client.post("/reconciliation/settlements/create/", _draft_payload(party))
    batch = SettlementBatch.objects.get()

    response = finance_client.post(
        f"/reconciliation/settlements/{batch.id}/confirm/",
        {
            "version": str(batch.version),
            "invoice_id": str(invoice.id),
            "transaction_id": str(money.id),
            "amount": "100.00",
        },
    )

    assert response.status_code == 400
    assert "结算批次明细不完整" in response.content.decode()
    assert not Reconciliation.objects.exists()
    batch.refresh_from_db()
    assert batch.status == SettlementStatus.DRAFT


@pytest.mark.django_db
def test_settlement_confirm_redirects_to_accessible_reconciliation_detail(
    finance_client, settlement_records
):
    party, invoice, money = settlement_records
    finance_client.post("/reconciliation/settlements/create/", _draft_payload(party))
    batch = SettlementBatch.objects.get()

    confirmed = finance_client.post(
        f"/reconciliation/settlements/{batch.id}/confirm/",
        {
            "version": str(batch.version),
            "invoice_id": str(invoice.id),
            "transaction_id": str(money.id),
            "amount": "100.00",
        },
    )

    assert confirmed.status_code == 302
    detail = finance_client.get(confirmed.headers["Location"])
    assert detail.status_code == 200
    assert "批次核销" in detail.content.decode()
    batch.refresh_from_db()
    assert batch.status == SettlementStatus.CONFIRMED
    assert batch.version == 2


@pytest.mark.django_db
def test_owner_cannot_create_or_confirm_settlement(
    owner_client, owner_user, settlement_records
):
    party, invoice, money = settlement_records
    create_response = owner_client.post(
        "/reconciliation/settlements/create/", _draft_payload(party)
    )
    assert create_response.status_code == 403
    batch = SettlementBatch.objects.create(
        counterparty=party,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 30),
        created_by=owner_user,
    )
    confirm_response = owner_client.post(
        f"/reconciliation/settlements/{batch.id}/confirm/",
        {
            "version": "1",
            "invoice_id": str(invoice.id),
            "transaction_id": str(money.id),
            "amount": "100.00",
        },
    )
    assert confirm_response.status_code == 403
    assert not Reconciliation.objects.exists()
