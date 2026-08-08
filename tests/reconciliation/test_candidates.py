from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from apps.imports.choices import SourceKind
from apps.imports.models import ImportBatch
from apps.ledger.choices import MoneyChannel, MoneyDirection
from apps.ledger.models import AccountBalanceSnapshot, FundingAccount
from apps.reconciliation.candidates import invoice_candidates, transaction_candidates
from apps.reconciliation.choices import ReconciliationDirection
from apps.reconciliation.services import AllocationInput, create_reconciliation
from tests.builders import make_invoice, make_transaction


@pytest.mark.django_db
def test_candidate_finds_only_exact_contiguous_payment_window(
    finance_user, synthetic_railway_party, synthetic_railway_june_transactions
):
    invoice = make_invoice(
        finance_user,
        total_amount=Decimal("7100.00"),
        counterparty=synthetic_railway_party,
    )
    candidates = transaction_candidates(
        invoice.id,
        start=date(2026, 6, 1),
        end=date(2026, 6, 30),
    )

    exact = [item for item in candidates if item.difference == Decimal("0.00")]

    assert [(item.start_at.date(), item.end_at.date(), item.total) for item in exact] == [
        (date(2026, 6, 2), date(2026, 6, 3), Decimal("7100.00"))
    ]


@pytest.mark.django_db
def test_candidate_returns_partial_after_used_payment_leaves_open_amounts(
    finance_user, synthetic_railway_party, synthetic_railway_june_transactions
):
    paid_invoice = make_invoice(
        finance_user,
        total_amount=Decimal("2000.00"),
        counterparty=synthetic_railway_party,
    )
    target = make_invoice(
        finance_user,
        total_amount=Decimal("46050.00"),
        counterparty=synthetic_railway_party,
    )
    used_payment = next(
        item for item in synthetic_railway_june_transactions if item.amount == Decimal("2000.00")
    )
    create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        allocations=[AllocationInput(paid_invoice.id, used_payment.id, Decimal("2000.00"))],
    )

    candidates = transaction_candidates(
        target.id,
        start=date(2026, 6, 1),
        end=date(2026, 6, 30),
    )

    assert [(item.kind, item.total, item.difference) for item in candidates] == [
        ("PARTIAL", Decimal("45050.00"), Decimal("1000.00"))
    ]


@pytest.mark.django_db
def test_candidate_does_not_use_balance_snapshot(finance_user, synthetic_railway_invoice):
    account = FundingAccount.objects.create(
        channel=MoneyChannel.BANK,
        name="候选快照账户",
        identifier="candidate-snapshot",
        masked_identifier="********snapshot",
    )
    source_batch = ImportBatch.objects.create(
        source_kind=SourceKind.BANK,
        created_by=finance_user,
    )
    AccountBalanceSnapshot.objects.create(
        account=account,
        as_of=datetime(2026, 6, 30, tzinfo=UTC),
        balance=Decimal("50000.00"),
        source_batch=source_batch,
    )

    assert transaction_candidates(
        synthetic_railway_invoice.id,
        start=date(2026, 6, 1),
        end=date(2026, 6, 30),
    ) == []


@pytest.mark.django_db
def test_candidate_ignores_zero_open_amount_wrong_direction_and_other_counterparty(
    finance_user, synthetic_railway_invoice, synthetic_railway_june_transactions
):
    paid_invoice = make_invoice(
        finance_user,
        total_amount=Decimal("800.00"),
        counterparty=synthetic_railway_invoice.counterparty,
    )
    create_reconciliation(
        actor=finance_user,
        direction=ReconciliationDirection.PURCHASE_PAYMENT,
        allocations=[
            AllocationInput(
                paid_invoice.id,
                synthetic_railway_june_transactions[0].id,
                Decimal("800.00"),
            )
        ],
    )
    wrong_direction = make_transaction(
        finance_user,
        direction=MoneyDirection.INFLOW,
        amount=Decimal("46050.00"),
        counterparty=synthetic_railway_invoice.counterparty,
    )
    wrong_direction.occurred_at = datetime(2026, 6, 28, tzinfo=UTC)
    wrong_direction.save(update_fields=["occurred_at"])
    wrong_counterparty = make_transaction(
        finance_user,
        direction=MoneyDirection.OUTFLOW,
        amount=Decimal("46050.00"),
    )
    wrong_counterparty.occurred_at = datetime(2026, 6, 29, tzinfo=UTC)
    wrong_counterparty.save(update_fields=["occurred_at"])
    exact_payment = make_transaction(
        finance_user,
        direction=MoneyDirection.OUTFLOW,
        amount=Decimal("46050.00"),
        counterparty=synthetic_railway_invoice.counterparty,
    )
    exact_payment.occurred_at = datetime(2026, 6, 30, tzinfo=UTC)
    exact_payment.save(update_fields=["occurred_at"])

    candidates = transaction_candidates(
        synthetic_railway_invoice.id,
        start=date(2026, 6, 1),
        end=date(2026, 6, 30),
    )

    assert candidates[0].kind == "CONTIGUOUS_EXACT"
    assert synthetic_railway_june_transactions[0].id not in candidates[0].transaction_ids
    assert candidates[0].total == Decimal("46050.00")


@pytest.mark.django_db
def test_invoice_candidates_only_match_contiguous_open_invoices(finance_user, synthetic_railway_party):
    money = make_transaction(
        finance_user,
        direction=MoneyDirection.OUTFLOW,
        amount=Decimal("300.00"),
        counterparty=synthetic_railway_party,
    )
    invoices = [
        make_invoice(
            finance_user,
            total_amount=Decimal(amount),
            counterparty=synthetic_railway_party,
            invoice_number=f"candidate-{index}",
        )
        for index, amount in enumerate(("100.00", "200.00", "300.00"), start=1)
    ]
    for index, invoice in enumerate(invoices, start=1):
        invoice.issue_date = date(2026, 6, index)
        invoice.save(update_fields=["issue_date"])

    candidates = invoice_candidates(
        money.id,
        start=date(2026, 6, 1),
        end=date(2026, 6, 30),
    )

    assert [item.invoice_ids for item in candidates] == [
        (invoices[0].id, invoices[1].id),
        (invoices[2].id,),
    ]


@pytest.mark.django_db
def test_candidate_requires_valid_date_window(synthetic_railway_invoice):
    with pytest.raises(ValueError, match="日期区间不合法"):
        transaction_candidates(
            synthetic_railway_invoice.id,
            start=date(2026, 6, 30),
            end=date(2026, 6, 1),
        )
