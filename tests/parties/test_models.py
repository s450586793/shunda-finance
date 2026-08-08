import pytest
from django.db import IntegrityError, transaction

from apps.parties.models import AliasKind, Counterparty, CounterpartyAlias
from apps.parties.normalization import normalize_party_text


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("  ACME\u3000CO.\tLTD  ", "acme co. ltd"),
        ("ＡＣＭＥ", "acme"),
        ("Straße", "strasse"),
        ("测试　单位", "测试 单位"),
    ],
)
def test_normalize_party_text_canonicalizes_text(value, expected):
    assert normalize_party_text(value) == expected


@pytest.mark.parametrize("value", ["", " ", "\t\r\n", "\u3000"])
def test_normalize_party_text_rejects_blank_values(value):
    with pytest.raises(ValueError, match="往来单位文本不能为空"):
        normalize_party_text(value)


@pytest.mark.parametrize("value", [None, 123, b"ACME"])
def test_normalize_party_text_rejects_non_strings(value):
    with pytest.raises(TypeError, match="往来单位文本必须是字符串"):
        normalize_party_text(value)


@pytest.mark.django_db
def test_counterparty_alias_kind_and_normalized_value_are_globally_unique(finance_user):
    first_party = Counterparty.objects.create(
        name="第一单位", normalized_name="第一单位", is_customer=True
    )
    second_party = Counterparty.objects.create(
        name="第二单位", normalized_name="第二单位", is_supplier=True
    )
    CounterpartyAlias.objects.create(
        counterparty=first_party,
        kind=AliasKind.BANK_ACCOUNT,
        value="6222 0000 0000 0001",
        normalized_value="6222000000000001",
        confirmed_by=finance_user,
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        CounterpartyAlias.objects.create(
            counterparty=second_party,
            kind=AliasKind.BANK_ACCOUNT,
            value="6222000000000001",
            normalized_value="6222000000000001",
            confirmed_by=finance_user,
        )

    assert CounterpartyAlias.objects.count() == 1


@pytest.mark.django_db
def test_same_normalized_alias_can_be_used_by_different_kinds(finance_user):
    party = Counterparty.objects.create(
        name="测试单位", normalized_name="测试单位", is_supplier=True
    )

    CounterpartyAlias.objects.create(
        counterparty=party,
        kind=AliasKind.NAME,
        value="测试别名",
        normalized_value="测试别名",
        confirmed_by=finance_user,
    )
    CounterpartyAlias.objects.create(
        counterparty=party,
        kind=AliasKind.WECHAT_NAME,
        value="测试别名",
        normalized_value="测试别名",
        confirmed_by=finance_user,
    )

    assert CounterpartyAlias.objects.count() == 2
