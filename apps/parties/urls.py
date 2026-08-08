from django.urls import path

from . import views

app_name = "parties"

urlpatterns = [path("", views.counterparty_list, name="list")]
