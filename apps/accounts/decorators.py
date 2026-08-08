from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied

from .roles import Role, user_has_role


def finance_required(view):
    @login_required
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not user_has_role(request.user, Role.FINANCE):
            raise PermissionDenied("仅财务可以执行此操作")
        return view(request, *args, **kwargs)

    return wrapped


def owner_required(view):
    @login_required
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not user_has_role(request.user, Role.OWNER):
            raise PermissionDenied("仅老板可以执行此操作")
        return view(request, *args, **kwargs)

    return wrapped


def owner_or_finance_required(view):
    @login_required
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not any(
            user_has_role(request.user, role) for role in (Role.OWNER, Role.FINANCE)
        ):
            raise PermissionDenied("仅老板或财务可以查看此页面")
        return view(request, *args, **kwargs)

    return wrapped
