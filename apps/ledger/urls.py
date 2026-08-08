from django.urls import path

from . import views

app_name = "ledger"

urlpatterns = [
    path("invoices/", views.invoice_list, name="invoice-list"),
    path("transactions/", views.transaction_list, name="transaction-list"),
]
