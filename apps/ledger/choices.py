from django.db import models


class InvoiceDirection(models.TextChoices):
    INPUT = "input", "进项"
    OUTPUT = "output", "销项"


class InvoiceStatus(models.TextChoices):
    NORMAL = "normal", "正常"
    VOID = "void", "作废"
    RED = "red", "红冲"


class MoneyChannel(models.TextChoices):
    BANK = "bank", "银行"
    WECHAT = "wechat", "微信"


class MoneyDirection(models.TextChoices):
    INFLOW = "inflow", "收入"
    OUTFLOW = "outflow", "支出"
