from django.urls import path

from . import views

app_name = "reconciliation"

urlpatterns = [
    path("workbench/", views.workbench, name="workbench"),
    path("candidates/", views.candidate_list, name="candidates"),
    path("confirm/", views.direct_confirm, name="confirm"),
    path("settlements/", views.settlement_list, name="settlement-list"),
    path("settlements/create/", views.settlement_create, name="settlement-create"),
    path("settlements/<uuid:pk>/", views.settlement_detail, name="settlement-detail"),
    path(
        "settlements/<uuid:pk>/confirm/",
        views.settlement_confirm,
        name="settlement-confirm",
    ),
    path("<uuid:pk>/", views.reconciliation_detail, name="detail"),
    path("<uuid:pk>/reverse/", views.reverse, name="reverse"),
]
