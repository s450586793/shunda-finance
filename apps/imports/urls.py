from django.urls import path

from . import views

app_name = "imports"

urlpatterns = [
    path("", views.upload, name="index"),
    path("<uuid:pk>/", views.preview, name="preview"),
    path("<uuid:pk>/confirm/", views.confirm, name="confirm"),
    path("<uuid:pk>/issues.csv", views.issue_csv, name="issues"),
    path("<uuid:pk>/source/", views.source_file, name="source"),
]
