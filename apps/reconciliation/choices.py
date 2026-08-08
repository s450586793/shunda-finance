from django.db import models


class ReconciliationDirection(models.TextChoices):
    PURCHASE_PAYMENT = "purchase_payment", "采购付款"
    SALES_RECEIPT = "sales_receipt", "销售收款"


class ReconciliationMode(models.TextChoices):
    DIRECT = "direct", "直接核销"
    BATCH = "batch", "批次核销"


class SettlementStatus(models.TextChoices):
    DRAFT = "draft", "草稿"
    CONFIRMED = "confirmed", "已确认"
