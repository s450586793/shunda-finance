from pathlib import Path

from django import forms
from django.conf import settings

from apps.core.uploads import validate_upload_signature

ALLOWED_EXTENSIONS = {".csv", ".txt", ".xls", ".xlsx"}


class ImportUploadForm(forms.Form):
    file = forms.FileField(
        label="原始文件",
        widget=forms.ClearableFileInput(attrs={"accept": ".csv,.txt,.xls,.xlsx"}),
    )

    def clean_file(self):
        file_obj = self.cleaned_data["file"]
        if Path(file_obj.name).suffix.lower() not in ALLOWED_EXTENSIONS:
            raise forms.ValidationError("仅支持 CSV、TXT、XLS 和 XLSX 文件")
        if file_obj.size > settings.IMPORT_MAX_UPLOAD_BYTES:
            raise forms.ValidationError("文件大小超过系统允许的上限")
        validate_upload_signature(file_obj)
        return file_obj
