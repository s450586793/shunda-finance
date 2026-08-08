from django.urls import include, path

urlpatterns = [
    path("accounts/", include("django.contrib.auth.urls")),
    path("imports/", include("apps.imports.urls")),
    path("ledger/", include("apps.ledger.urls")),
    path("parties/", include("apps.parties.urls")),
    path("reconciliation/", include("apps.reconciliation.urls")),
    path("reporting/", include("apps.reporting.urls")),
    path("system/update/", include("apps.system_update.urls")),
    path("", include("apps.core.urls")),
]
