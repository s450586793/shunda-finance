from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from apps.imports.choices import SourceKind
from apps.imports.services import confirm_batch, stage_upload
from apps.imports.types import RowValidationError
from apps.ledger.choices import MoneyChannel, MoneyDirection
from apps.ledger.models import AccountBalanceSnapshot, FundingAccount, MoneyTransaction
from apps.parties.models import Counterparty

FIXTURES_DIR = Path(__file__).parents[1] / "fixtures"


@pytest.fixture
def bank_fixture():
    fixture_path = FIXTURES_DIR / "agricultural_bank.xls"
    return SimpleUploadedFile(fixture_path.name, fixture_path.read_bytes())


def _bank_fixture(filename):
    fixture_path = FIXTURES_DIR / filename
    return SimpleUploadedFile(fixture_path.name, fixture_path.read_bytes())


def test_bank_row_uses_income_or_expense_as_direction(bank_fixture):
    from apps.imports.parsers.agricultural_bank import AgriculturalBankImporter

    rows = [item.normalized for item in AgriculturalBankImporter().parse(bank_fixture)]

    assert rows[0].direction == MoneyDirection.OUTFLOW
    assert rows[0].amount == Decimal("2000.00")
    assert rows[0].counterparty_name == "测试铁路物流收款专户"
    assert rows[1].direction == MoneyDirection.INFLOW


def test_bank_parser_reads_metadata_account_and_header_after_title(bank_fixture):
    from apps.imports.parsers.agricultural_bank import AgriculturalBankImporter

    row = next(AgriculturalBankImporter().parse(bank_fixture))

    assert row.row_number == 4
    assert row.normalized.account_identifier == "test-bank-account"
    assert row.normalized.balance_after == Decimal("100000.00")


def test_bank_parser_matches_renamed_sheet_after_unrelated_first_sheet():
    from apps.imports.parsers.agricultural_bank import AgriculturalBankImporter

    row = next(
        AgriculturalBankImporter().parse(
            _bank_fixture("agricultural_bank_renamed_second_sheet.xls")
        )
    )

    assert row.normalized.account_identifier == "test-bank-account"


@pytest.mark.parametrize(
    "filename",
    [
        "agricultural_bank_invalid_title.xls",
        "agricultural_bank_invalid_metadata.xls",
        "agricultural_bank_invalid_header_position.xls",
    ],
)
def test_bank_parser_rejects_non_adjacent_or_incomplete_structures(filename):
    from apps.imports.parsers.agricultural_bank import AgriculturalBankImporter

    with pytest.raises(RowValidationError, match="无法识别农业银行流水工作表"):
        list(AgriculturalBankImporter().parse(_bank_fixture(filename)))


def test_bank_parser_rejects_ambiguous_matching_sheets():
    from apps.imports.parsers.agricultural_bank import AgriculturalBankImporter

    with pytest.raises(RowValidationError, match="不唯一"):
        list(
            AgriculturalBankImporter().parse(
                _bank_fixture("agricultural_bank_ambiguous_sheets.xls")
            )
        )


@pytest.mark.django_db
def test_stage_upload_uses_matching_bank_sheet_when_it_is_not_first(
    finance_user, settings, tmp_path
):
    settings.MEDIA_ROOT = tmp_path
    FundingAccount.objects.create(
        channel=MoneyChannel.BANK,
        name="测试农行账户",
        identifier="test-bank-account",
        masked_identifier="********account",
    )
    for name in ("测试铁路物流收款专户", "测试收款方"):
        Counterparty.objects.create(name=name, normalized_name=name, is_supplier=True)

    batch = stage_upload(
        _bank_fixture("agricultural_bank_renamed_second_sheet.xls"),
        source_kind=SourceKind.BANK,
        actor=finance_user,
    )

    assert (batch.total_rows, batch.valid_rows, batch.error_rows) == (2, 2, 0)


@pytest.mark.django_db
def test_stage_upload_auto_detects_bank_statement(finance_user, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    FundingAccount.objects.create(
        channel=MoneyChannel.BANK,
        name="测试农行账户",
        identifier="test-bank-account",
        masked_identifier="********account",
    )
    for name in ("测试铁路物流收款专户", "测试收款方"):
        Counterparty.objects.create(name=name, normalized_name=name, is_supplier=True)

    batch = stage_upload(
        _bank_fixture("agricultural_bank_renamed_second_sheet.xls"),
        actor=finance_user,
    )

    assert batch.source_kind == SourceKind.BANK


@pytest.mark.parametrize(
    ("income", "expense"),
    [("", ""), ("1.00", "2.00"), ("0", "0")],
)
def test_bank_row_requires_exactly_one_positive_amount(income, expense):
    from apps.imports.parsers.agricultural_bank import parse_bank_amount
    from apps.imports.types import RowValidationError

    with pytest.raises(RowValidationError, match="必须且只能有一个大于零"):
        parse_bank_amount(income, expense)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1.234", "10000000000000000.00"])
def test_bank_amount_rejects_non_finite_or_out_of_range_values(value):
    from apps.imports.parsers.agricultural_bank import parse_bank_amount
    from apps.imports.types import RowValidationError

    with pytest.raises(RowValidationError):
        parse_bank_amount(value, "")


@pytest.mark.django_db
def test_confirmed_bank_batch_creates_only_latest_balance_snapshot(
    finance_user, bank_fixture, settings, tmp_path
):
    settings.MEDIA_ROOT = tmp_path
    account = FundingAccount.objects.create(
        channel=MoneyChannel.BANK,
        name="测试农行账户",
        identifier="test-bank-account",
        masked_identifier="********account",
    )
    Counterparty.objects.create(
        name="测试铁路物流收款专户",
        normalized_name="测试铁路物流收款专户",
        is_supplier=True,
    )
    Counterparty.objects.create(
        name="测试收款方",
        normalized_name="测试收款方",
        is_customer=True,
    )

    batch = stage_upload(bank_fixture, source_kind=SourceKind.BANK, actor=finance_user)

    assert AccountBalanceSnapshot.objects.count() == 0
    assert confirm_batch(batch.id, finance_user).posted_rows == 2
    assert MoneyTransaction.objects.count() == 2
    snapshot = AccountBalanceSnapshot.objects.get()
    assert snapshot.account == account
    assert snapshot.as_of == datetime(
        2026, 7, 27, 13, 14, 14, tzinfo=timezone.get_current_timezone()
    )
    assert snapshot.balance == Decimal("100000.00")
    assert snapshot.source_batch == batch


@pytest.mark.django_db
def test_overlapping_bank_exports_reuse_matching_balance_snapshot(
    finance_user, bank_fixture, settings, tmp_path
):
    settings.MEDIA_ROOT = tmp_path
    FundingAccount.objects.create(
        channel=MoneyChannel.BANK,
        name="测试农行账户",
        identifier="test-bank-account",
        masked_identifier="********account",
    )
    for name in ("测试铁路物流收款专户", "测试收款方"):
        Counterparty.objects.create(name=name, normalized_name=name, is_supplier=True)

    first = stage_upload(bank_fixture, source_kind=SourceKind.BANK, actor=finance_user)
    confirm_batch(first.id, finance_user)
    second_file = SimpleUploadedFile(
        "agricultural-bank-overlap.xls", bank_fixture.read() + b"\x00"
    )
    second = stage_upload(second_file, source_kind=SourceKind.BANK, actor=finance_user)
    result = confirm_batch(second.id, finance_user)

    assert result.posted_rows == 0
    assert second.duplicate_rows == 2
    assert MoneyTransaction.objects.count() == 2
    assert AccountBalanceSnapshot.objects.count() == 1


@pytest.mark.django_db
def test_conflicting_balance_snapshot_fails_without_mutating_existing_snapshot(
    finance_user, bank_fixture, settings, tmp_path
):
    settings.MEDIA_ROOT = tmp_path
    account = FundingAccount.objects.create(
        channel=MoneyChannel.BANK,
        name="测试农行账户",
        identifier="test-bank-account",
        masked_identifier="********account",
    )
    for name in ("测试铁路物流收款专户", "测试收款方"):
        Counterparty.objects.create(name=name, normalized_name=name, is_supplier=True)
    AccountBalanceSnapshot.objects.create(
        account=account,
        as_of=datetime(
            2026, 7, 27, 13, 14, 14, tzinfo=timezone.get_current_timezone()
        ),
        balance=Decimal("99999.99"),
        source_batch=stage_upload(bank_fixture, source_kind=SourceKind.BANK, actor=finance_user),
    )
    batch = stage_upload(
        SimpleUploadedFile("agricultural-bank-conflict.xls", bank_fixture.read() + b"\x00"),
        source_kind=SourceKind.BANK,
        actor=finance_user,
    )

    with pytest.raises(ValueError, match="余额快照冲突"):
        confirm_batch(batch.id, finance_user)

    snapshot = AccountBalanceSnapshot.objects.get()
    assert snapshot.balance == Decimal("99999.99")
    assert snapshot.source_batch != batch
