from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.utils import timezone

from apps.imports.choices import SourceKind
from apps.ledger.choices import (
    InvoiceDirection,
    InvoiceStatus,
    MoneyChannel,
    MoneyDirection,
)
from apps.ledger.models import FundingAccount
from apps.parties.models import AliasKind, Counterparty, CounterpartyAlias
from apps.parties.normalization import normalize_party_text

from .types import (
    NormalizedInvoiceRow,
    NormalizedTransactionRow,
    ParsedRow,
    RowIssue,
)


@dataclass(frozen=True)
class MappingResult:
    value: object | None
    ambiguous: bool = False


def validate_parsed_row(source_kind: str, parsed_row: ParsedRow) -> tuple[RowIssue, ...]:
    issues = list(parsed_row.issues)
    normalized = parsed_row.normalized
    if normalized is None:
        return tuple(issues or [RowIssue("normalized", "无法标准化该行")])

    if isinstance(normalized, NormalizedInvoiceRow):
        if source_kind not in {
            SourceKind.INPUT_INVOICE,
            SourceKind.OUTPUT_INVOICE,
        }:
            return tuple(issues + [RowIssue("source_kind", "来源类型与标准行类型不匹配")])
        issues.extend(_validate_invoice(source_kind, normalized))
    elif isinstance(normalized, NormalizedTransactionRow):
        if source_kind not in {SourceKind.BANK, SourceKind.WECHAT}:
            return tuple(issues + [RowIssue("source_kind", "来源类型与标准行类型不匹配")])
        issues.extend(_validate_transaction(source_kind, normalized))
    else:
        issues.append(RowIssue("normalized", "无法识别标准行类型"))
    return tuple(issues)


def resolve_invoice_counterparty(row: NormalizedInvoiceRow):
    tax_id = row.seller_tax_id if row.direction == InvoiceDirection.INPUT else row.buyer_tax_id
    role_filter = (
        {"is_supplier": True}
        if row.direction == InvoiceDirection.INPUT
        else {"is_customer": True}
    )
    return _single_match(
        Counterparty.objects.filter(tax_id=tax_id, active=True, **role_filter)
    )


def resolve_transaction_counterparty(row: NormalizedTransactionRow):
    try:
        normalized_name = normalize_party_text(row.counterparty_name)
    except (TypeError, ValueError):
        return MappingResult(None)
    counterparty = _single_match(Counterparty.objects.filter(
        normalized_name=normalized_name, active=True
    ))
    if counterparty.value is not None or counterparty.ambiguous:
        return counterparty
    alias_kind = (
        AliasKind.BANK_ACCOUNT if row.channel == MoneyChannel.BANK else AliasKind.WECHAT_NAME
    )
    alias_value = row.counterparty_account if row.counterparty_account else row.counterparty_name
    try:
        normalized_alias = normalize_party_text(alias_value)
    except (TypeError, ValueError):
        return MappingResult(None)
    alias = _single_match(CounterpartyAlias.objects.filter(
        kind=alias_kind,
        normalized_value=normalized_alias,
        counterparty__active=True,
    ).select_related("counterparty"))
    if alias.value is None:
        return MappingResult(None, alias.ambiguous)
    return MappingResult(alias.value.counterparty)


def resolve_funding_account(row: NormalizedTransactionRow):
    return _single_match(
        FundingAccount.objects.filter(
            channel=row.channel,
            identifier=row.account_identifier,
            active=True,
        )
    )


def _validate_invoice(source_kind: str, row: NormalizedInvoiceRow) -> list[RowIssue]:
    issues = []
    expected_direction = (
        InvoiceDirection.INPUT
        if source_kind == SourceKind.INPUT_INVOICE
        else InvoiceDirection.OUTPUT
    )
    if row.direction != expected_direction:
        issues.append(RowIssue("direction", "发票方向与来源类型不匹配", "direction"))
    if not _has_text(row.invoice_number):
        issues.append(RowIssue("required", "发票号码不能为空", "invoice_number"))
    if not _has_text(row.seller_tax_id):
        issues.append(RowIssue("required", "销售方税号不能为空", "seller_tax_id"))
    if not _has_text(row.buyer_tax_id):
        issues.append(RowIssue("required", "购买方税号不能为空", "buyer_tax_id"))
    if not isinstance(row.issue_date, date) or isinstance(row.issue_date, datetime):
        issues.append(RowIssue("date", "开票日期不合法", "issue_date"))
    if not _positive_decimal(row.total_amount):
        issues.append(RowIssue("amount", "发票金额必须大于零", "total_amount"))
    if row.status not in InvoiceStatus.values:
        issues.append(RowIssue("status", "发票状态不合法", "status"))
    counterparty = resolve_invoice_counterparty(row)
    if not issues and counterparty.value is None:
        code = "counterparty_ambiguous" if counterparty.ambiguous else "counterparty"
        issues.append(RowIssue(code, "往来单位映射不唯一" if counterparty.ambiguous else "无法识别往来单位"))
    return issues


def _validate_transaction(
    source_kind: str, row: NormalizedTransactionRow
) -> list[RowIssue]:
    issues = []
    if row.channel not in MoneyChannel.values or row.channel != source_kind:
        issues.append(RowIssue("channel", "资金渠道与来源类型不匹配", "channel"))
    if row.direction not in MoneyDirection.values:
        issues.append(RowIssue("direction", "资金方向不合法", "direction"))
    if not isinstance(row.occurred_at, datetime) or timezone.is_naive(row.occurred_at):
        issues.append(RowIssue("datetime", "发生时间不合法", "occurred_at"))
    if not _positive_decimal(row.amount):
        issues.append(RowIssue("amount", "资金金额必须大于零", "amount"))
    account = resolve_funding_account(row)
    if not _has_text(row.account_identifier) or account.value is None:
        code = "account_ambiguous" if account.ambiguous else "account"
        issues.append(
            RowIssue(
                code,
                "资金账户映射不唯一" if account.ambiguous else "无法识别资金账户",
                "account_identifier",
            )
        )
    counterparty = resolve_transaction_counterparty(row)
    if counterparty.value is None:
        code = "counterparty_ambiguous" if counterparty.ambiguous else "counterparty"
        issues.append(
            RowIssue(
                code,
                "往来单位映射不唯一" if counterparty.ambiguous else "无法识别往来单位",
                "counterparty_name",
            )
        )
    return issues


def _positive_decimal(value: object) -> bool:
    try:
        return Decimal(value) > 0
    except (InvalidOperation, TypeError, ValueError):
        return False


def _has_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _single_match(queryset) -> MappingResult:
    matches = list(queryset[:2])
    if len(matches) == 1:
        return MappingResult(matches[0])
    return MappingResult(None, ambiguous=len(matches) > 1)
