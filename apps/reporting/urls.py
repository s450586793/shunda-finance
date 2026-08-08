from django.urls import path

from . import views

app_name = "reporting"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("receivables/", views.receivables, name="receivables"),
    path("payables/", views.payables, name="payables"),
    path("exceptions/", views.exceptions, name="exceptions"),
    path("suppliers/", views.suppliers, name="suppliers"),
    path("suppliers/<uuid:pk>/", views.supplier_detail, name="supplier-detail"),
]
