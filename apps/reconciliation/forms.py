from decimal import Decimal, InvalidOperation
from uuid import UUID

from django import forms

from apps.parties.models import Counterparty

from .choices import ReconciliationDirection
from .services import AllocationInput

MAX_MONEY = Decimal("9999999999999999.99")


def _clean_amount(value):
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise forms.ValidationError("核销金额格式不合法") from exc
    if not amount.is_finite():
        raise forms.ValidationError("核销金额格式不合法")
    if amount <= 0:
        raise forms.ValidationError("核销金额必须大于零")
    if amount > MAX_MONEY:
        raise forms.ValidationError("核销金额超出允许范围")
    if amount.as_tuple().exponent < -2:
        raise forms.ValidationError("核销金额最多保留两位小数")
    return amount


def _clean_uuid(value, label):
    try:
        return UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise forms.ValidationError(f"{label}不合法") from exc


class DirectReconciliationForm(forms.Form):
    invoice_id = forms.UUIDField(
        error_messages={"required": "请选择发票", "invalid": "发票编号不合法"}
    )
    note = forms.CharField(
        required=False,
        max_length=1000,
        error_messages={"max_length": "备注不能超过 1000 个字符"},
    )
    partial_confirm = forms.BooleanField(required=False)
    expected_invoice_open = forms.CharField(
        error_messages={"required": "页面数据已过期，请刷新后重试"}
    )

    def clean(self):
        cleaned_data = super().clean()
        transaction_values = self.data.getlist("transaction_id")
        amount_values = self.data.getlist("amount")
        expected_open_values = self.data.getlist("expected_transaction_open")
        if not transaction_values or not (
            len(transaction_values) == len(amount_values) == len(expected_open_values)
        ):
            raise forms.ValidationError("核销明细不能为空")

        transaction_ids = []
        amounts = []
        expected_open_amounts = []
        for transaction_value, amount_value, expected_open_value in zip(
            transaction_values,
            amount_values,
            expected_open_values,
            strict=True,
        ):
            transaction_ids.append(_clean_uuid(transaction_value, "资金流水"))
            amounts.append(_clean_amount(amount_value))
            expected_open_amounts.append(_clean_amount(expected_open_value))
        if len(transaction_ids) != len(set(transaction_ids)):
            raise forms.ValidationError("资金流水不能重复")
        expected_invoice_open = cleaned_data.get("expected_invoice_open")
        if expected_invoice_open is not None:
            cleaned_data["expected_invoice_open"] = _clean_amount(
                expected_invoice_open
            )
        cleaned_data["allocation_rows"] = tuple(
            zip(transaction_ids, amounts, expected_open_amounts, strict=True)
        )
        return cleaned_data

    def allocation_inputs(self):
        invoice_id = self.cleaned_data["invoice_id"]
        return tuple(
            AllocationInput(invoice_id, transaction_id, amount)
            for transaction_id, amount, _expected_open in self.cleaned_data[
                "allocation_rows"
            ]
        )

    def expected_invoice_open_amounts(self):
        return {
            self.cleaned_data["invoice_id"]: self.cleaned_data["expected_invoice_open"]
        }

    def expected_transaction_open_amounts(self):
        return {
            transaction_id: expected_open
            for transaction_id, _amount, expected_open in self.cleaned_data[
                "allocation_rows"
            ]
        }


class SettlementDraftForm(forms.Form):
    counterparty = forms.ModelChoiceField(
        label="往来单位",
        queryset=Counterparty.objects.none(),
        empty_label="请选择",
        error_messages={
            "required": "请选择往来单位",
            "invalid": "往来单位不合法",
            "invalid_choice": "往来单位不合法",
        },
    )
    direction = forms.ChoiceField(
        label="核销方向",
        choices=ReconciliationDirection.choices,
        error_messages={
            "required": "请选择核销方向",
            "invalid_choice": "核销方向不合法",
        },
    )
    period_start = forms.DateField(
        label="开始日期",
        widget=forms.DateInput(attrs={"type": "date"}),
        error_messages={"required": "请选择开始日期", "invalid": "开始日期不合法"},
    )
    period_end = forms.DateField(
        label="结束日期",
        widget=forms.DateInput(attrs={"type": "date"}),
        error_messages={"required": "请选择结束日期", "invalid": "结束日期不合法"},
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["counterparty"].queryset = Counterparty.objects.order_by(
            "-active", "name", "id"
        )

    def clean(self):
        cleaned_data = super().clean()
        start = cleaned_data.get("period_start")
        end = cleaned_data.get("period_end")
        if start and end and start > end:
            raise forms.ValidationError("开始日期不能晚于结束日期")
        return cleaned_data


class SettlementConfirmForm(forms.Form):
    version = forms.IntegerField(
        min_value=1,
        error_messages={
            "required": "结算批次版本不能为空",
            "invalid": "结算批次版本不合法",
            "min_value": "结算批次版本不合法",
        },
    )

    def clean(self):
        cleaned_data = super().clean()
        invoice_values = self.data.getlist("invoice_id")
        transaction_values = self.data.getlist("transaction_id")
        amount_values = self.data.getlist("amount")
        if not invoice_values or not (
            len(invoice_values) == len(transaction_values) == len(amount_values)
        ):
            raise forms.ValidationError("结算批次核销明细不能为空")

        allocations = []
        pairs = set()
        for invoice_value, transaction_value, amount_value in zip(
            invoice_values, transaction_values, amount_values, strict=True
        ):
            invoice_id = _clean_uuid(invoice_value, "发票")
            transaction_id = _clean_uuid(transaction_value, "资金流水")
            pair = (invoice_id, transaction_id)
            if pair in pairs:
                raise forms.ValidationError("结算批次核销明细不能重复")
            pairs.add(pair)
            allocations.append(
                AllocationInput(invoice_id, transaction_id, _clean_amount(amount_value))
            )
        cleaned_data["allocations"] = tuple(allocations)
        return cleaned_data

    def allocation_inputs(self):
        return self.cleaned_data["allocations"]


class ReversalForm(forms.Form):
    reason = forms.CharField(
        label="撤销原因",
        max_length=1000,
        strip=True,
        widget=forms.Textarea(attrs={"rows": 4}),
        error_messages={
            "required": "撤销原因不能为空",
            "max_length": "撤销原因不能超过 1000 个字符",
        },
    )
