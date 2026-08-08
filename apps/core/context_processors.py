from django.urls import reverse

from apps.accounts.roles import Role, user_has_role


def navigation(request):
    is_finance = user_has_role(request.user, Role.FINANCE)
    can_read_ledger = is_finance or user_has_role(request.user, Role.OWNER)
    items = [
        {
            "label": "总览",
            "href": reverse("reporting:dashboard") if can_read_ledger else None,
            "icon": "layout-dashboard",
        },
        {
            "label": "导入中心",
            "href": reverse("imports:index") if is_finance else None,
            "icon": "upload",
        },
        {
            "label": "人工核销",
            "href": reverse("reconciliation:workbench") if is_finance else None,
            "icon": "split",
        },
        {
            "label": "结算批次",
            "href": reverse("reconciliation:settlement-list") if is_finance else None,
            "icon": "layers-3",
        },
        {
            "label": "应收应付",
            "href": reverse("reporting:receivables") if can_read_ledger else None,
            "icon": "receipt-text",
        },
        {
            "label": "往来单位",
            "href": reverse("parties:list") if can_read_ledger else None,
            "icon": "building-2",
        },
        {"label": "操作记录", "href": None, "icon": "history"},
    ]
    if user_has_role(request.user, Role.OWNER):
        items.append(
            {
                "label": "系统设置",
                "href": reverse("system-update:index"),
                "icon": "settings",
            }
        )
    return {"is_finance": is_finance, "navigation_items": items}
