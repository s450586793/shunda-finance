from django.db import models


class BatchStatus(models.TextChoices):
    UPLOADED = "uploaded", "已上传"
    PREVIEWED = "previewed", "已预检"
    PARTIAL = "partial", "部分完成"
    COMPLETED = "completed", "已完成"


FINAL_BATCH_STATUSES = frozenset({BatchStatus.PARTIAL, BatchStatus.COMPLETED})


class SourceKind(models.TextChoices):
    INPUT_INVOICE = "input_invoice", "进项发票"
    OUTPUT_INVOICE = "output_invoice", "销项发票"
    BANK = "bank", "银行"
    WECHAT = "wechat", "微信"
