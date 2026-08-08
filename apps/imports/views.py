import csv
import logging
from pathlib import Path

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import FileResponse, HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.accounts.decorators import finance_required

from .forms import ImportUploadForm
from .models import ImportBatch, SourceFile, StagedRow
from .services import confirm_batch, stage_upload
from .types import (
    DuplicateSourceFileError,
    RowValidationError,
    UnsupportedTemplateError,
)

logger = logging.getLogger(__name__)
SAFE_IMPORT_ERRORS = (
    DuplicateSourceFileError,
    RowValidationError,
    UnsupportedTemplateError,
)


@finance_required
@require_http_methods(["GET", "POST"])
def upload(request):
    form = ImportUploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            batch = stage_upload(
                form.cleaned_data["file"],
                actor=request.user,
            )
        except SAFE_IMPORT_ERRORS as exc:
            form.add_error("file", str(exc))
        except Exception:
            logger.exception("Import staging failed")
            form.add_error("file", "文件处理失败，请检查文件格式后重试。")
        else:
            return redirect("imports:preview", pk=batch.pk)

    recent_batches = ImportBatch.objects.select_related("created_by").order_by(
        "-created_at"
    )[:10]
    return render(
        request,
        "imports/index.html",
        {"form": form, "recent_batches": recent_batches},
        status=400 if request.method == "POST" else 200,
    )


@finance_required
@require_GET
def preview(request, pk):
    batch = get_object_or_404(
        ImportBatch.objects.select_related("created_by"),
        pk=pk,
    )
    issue_rows = (
        StagedRow.objects.filter(batch_id=batch.pk)
        .exclude(issues=[])
        .only("row_number", "issues")
        .order_by("row_number")
    )
    issue_page = Paginator(issue_rows, 100).get_page(request.GET.get("issue_page"))
    return render(
        request,
        "imports/preview.html",
        {"batch": batch, "issue_page": issue_page},
    )


@finance_required
@require_POST
def confirm(request, pk):
    get_object_or_404(ImportBatch, pk=pk)
    try:
        confirm_batch(pk, request.user)
    except SAFE_IMPORT_ERRORS as exc:
        messages.error(request, str(exc))
    except Exception:
        logger.exception("Import confirmation failed", extra={"batch_id": str(pk)})
        messages.error(request, "确认导入失败，请稍后重试。")
    else:
        messages.success(request, "导入已确认。")
    return redirect("imports:preview", pk=pk)


class _CsvEcho:
    def write(self, value):
        return value


def _issue_csv_content(batch):
    writer = csv.writer(_CsvEcho())
    yield "\ufeff"
    yield writer.writerow(["行号", "字段", "问题代码", "问题说明"])
    issue_rows = (
        StagedRow.objects.filter(batch_id=batch.pk)
        .exclude(issues=[])
        .only("row_number", "issues")
        .order_by("row_number")
        .iterator(chunk_size=500)
    )
    for row in issue_rows:
        for issue in row.issues:
            yield writer.writerow(
                (
                    row.row_number,
                    issue.get("field", ""),
                    issue.get("code", ""),
                    issue.get("message", ""),
                )
            )


@finance_required
@require_GET
def issue_csv(request, pk):
    batch = get_object_or_404(ImportBatch, pk=pk)
    response = StreamingHttpResponse(
        _issue_csv_content(batch), content_type="text/csv; charset=utf-8"
    )
    response["Content-Disposition"] = (
        f'attachment; filename="import-{batch.pk}-issues.csv"'
    )
    return response


@finance_required
@require_GET
def source_file(request, pk):
    source = get_object_or_404(SourceFile, batch_id=pk)
    log_context = {"batch_id": str(pk), "source_file_id": str(source.pk)}
    try:
        exists = bool(source.file.name) and source.file.storage.exists(source.file.name)
    except OSError:
        logger.warning(
            "Source file storage unavailable", extra=log_context, exc_info=True
        )
        return HttpResponse("原文件暂时无法读取，请稍后重试。", status=503)
    if not exists:
        logger.warning("Source file is missing", extra=log_context)
        return HttpResponse("原文件已丢失。", status=404)
    filename = Path(source.original_name).name
    try:
        source.file.open("rb")
    except FileNotFoundError:
        logger.warning("Source file disappeared before opening", extra=log_context)
        return HttpResponse("原文件已丢失。", status=404)
    except OSError:
        logger.warning("Source file cannot be opened", extra=log_context, exc_info=True)
        return HttpResponse("原文件暂时无法读取，请稍后重试。", status=503)
    return FileResponse(source.file, as_attachment=True, filename=filename)
