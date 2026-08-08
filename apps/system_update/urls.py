from django.urls import path

from . import views

app_name = "system-update"

urlpatterns = [
    path("", views.index, name="index"),
    path("status/", views.status, name="status"),
    path("check/", views.check, name="check"),
    path("start/", views.start, name="start"),
]
