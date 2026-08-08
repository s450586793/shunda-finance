import json

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from apps.parties.models import Counterparty

REPORTING_URLS = (
    "/reporting/",
    "/reporting/receivables/",
    "/reporting/payables/",
    "/reporting/exceptions/",
    "/reporting/suppliers/",
)


@pytest.mark.django_db
@pytest.mark.parametrize("url", REPORTING_URLS)
def test_reporting_pages_require_authentication(client, url):
    response = client.get(url)
    assert response.status_code == 302
    assert response.headers["Location"] == f"/accounts/login/?next={url}"


@pytest.mark.django_db
@pytest.mark.parametrize("url", REPORTING_URLS)
def test_owner_and_finance_can_read_reporting(owner_client, finance_client, url):
    assert owner_client.get(url).status_code == 200
    assert finance_client.get(url).status_code == 200


@pytest.mark.django_db
@pytest.mark.parametrize("url", REPORTING_URLS)
def test_other_authenticated_roles_are_forbidden(client, url):
    client.force_login(User.objects.create_user("auditor"))
    assert client.get(url).status_code == 403


@pytest.mark.django_db
def test_dashboard_uses_safe_local_chart_payload(owner_client):
    response = owner_client.get("/reporting/", {"month": "2026-07"})
    body = response.content.decode()

    assert response.status_code == 200
    assert '/static/vendor/echarts.min.js' in body
    assert '/static/js/dashboard.js' in body
    assert "https://" not in body
    script = response.context["dashboard_chart_data"]
    assert all(isinstance(item["inflow"], str) for item in script["dailyCashflow"])
    assert json.loads(response.context["dashboard_chart_json"])["dailyCashflow"] == script["dailyCashflow"]


@pytest.mark.django_db
def test_reporting_rejects_invalid_date_parameters(owner_client):
    assert owner_client.get("/reporting/", {"month": "2026-13"}).status_code == 400
    assert owner_client.get("/reporting/receivables/", {"as_of": "bad"}).status_code == 400


@pytest.mark.django_db
def test_navigation_links_dashboard_and_reporting_without_owner_write_access(owner_client):
    response = owner_client.get("/reporting/")
    items = {item["label"]: item["href"] for item in response.context["navigation_items"]}
    body = response.content.decode()

    assert items["总览"] == "/reporting/"
    assert items["应收应付"] == "/reporting/receivables/"
    assert items["导入中心"] is None
    assert items["人工核销"] is None
    assert items["操作记录"] is None
    assert 'href="/imports/"' not in body
    assert 'href="/reconciliation/workbench/"' not in body
    assert 'href="/reconciliation/settlements/"' not in body
    assert "导入中心" in body
    assert "人工核销" in body


@pytest.mark.django_db
def test_all_reporting_view_tabs_link_to_supplier_summary(owner_client):
    supplier = Counterparty.objects.create(
        name="标签页供应商",
        normalized_name="标签页供应商",
        is_supplier=True,
    )
    supplier_url = reverse("reporting:suppliers")
    pages = (
        reverse("reporting:receivables"),
        reverse("reporting:payables"),
        reverse("reporting:exceptions"),
        supplier_url,
        reverse("reporting:supplier-detail", args=[supplier.id]),
    )

    for url in pages:
        response = owner_client.get(url, {"as_of": "2026-07-31"})

        assert response.status_code == 200
        body = response.content.decode()
        assert 'class="view-tabs"' in body
        assert supplier_url in body
