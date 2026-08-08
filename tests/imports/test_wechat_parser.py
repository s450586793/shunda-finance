from decimal import Decimal
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.imports.choices import SourceKind
from apps.imports.services import confirm_batch, stage_upload
from apps.ledger.choices import MoneyChannel
from apps.ledger.models import FundingAccount, MoneyTransaction
from apps.parties.models import Counterparty

FIXTURES_DIR = Path(__file__).parents[1] / "fixtures"


@pytest.fixture
def wechat_fixture():
    fixture_path = FIXTURES_DIR / "wechat_transactions.csv"
    return SimpleUploadedFile(fixture_path.name, fixture_path.read_bytes())


def test_wechat_amount_strips_currency_symbol_and_preserves_ids(wechat_fixture):
    from apps.imports.parsers.wechat import WechatImporter

    row = next(WechatImporter().parse(wechat_fixture)).normalized

    assert row.amount == Decimal("3200.00")
    assert row.transaction_id == "420000000000000001"
    assert row.channel == MoneyChannel.WECHAT


def test_wechat_non_success_and_neutral_rows_are_staged_as_issues(wechat_fixture):
    from apps.imports.parsers.wechat import WechatImporter

    rows = list(WechatImporter().parse(wechat_fixture))

    assert rows[1].normalized is None
    assert rows[1].issues[0].code == "status"
    assert rows[2].normalized is None
    assert rows[2].issues[0].code == "direction"


def test_wechat_negative_amount_is_isolated_as_an_issue():
    from apps.imports.parsers.wechat import WechatImporter

    file_obj = SimpleUploadedFile(
        "wechat-negative.csv",
        (
            "交易时间,交易类型,交易对方,商品,收/支,金额(元),支付方式,当前状态,交易单号,商户单号,备注\n"
            "2026-07-27 13:14:14,商户消费,测试客户,物流服务,收入,-1.00,零钱,已收钱,TX-001,MCH-001,异常金额\n"
        ).encode(),
    )

    row = next(WechatImporter().parse(file_obj))

    assert row.normalized is None
    assert row.issues[0].code == "amount"


@pytest.mark.django_db
def test_wechat_overlapping_exports_skip_formal_transaction_by_fingerprint(
    finance_user, wechat_fixture, settings, tmp_path
):
    settings.MEDIA_ROOT = tmp_path
    FundingAccount.objects.create(
        channel=MoneyChannel.WECHAT,
        name="微信账户",
        identifier="零钱",
        masked_identifier="零钱",
    )
    Counterparty.objects.create(name="测试客户", normalized_name="测试客户", is_customer=True)

    first = stage_upload(wechat_fixture, source_kind=SourceKind.WECHAT, actor=finance_user)
    assert confirm_batch(first.id, finance_user).posted_rows == 1

    second_file = SimpleUploadedFile(
        "wechat-overlap.csv", "重叠导出文件\n".encode() + wechat_fixture.read()
    )
    second = stage_upload(second_file, source_kind=SourceKind.WECHAT, actor=finance_user)
    result = confirm_batch(second.id, finance_user)

    assert result.posted_rows == 0
    assert second.duplicate_rows == 1
    assert second.error_rows == 2
    assert MoneyTransaction.objects.count() == 1
