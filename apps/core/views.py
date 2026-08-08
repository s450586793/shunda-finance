from django.db import DatabaseError, InterfaceError, connections
from django.http import JsonResponse
from django.shortcuts import redirect


def home(request):
    return redirect("ledger:invoice-list")


def health_check(request):
    try:
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT 1")
            row = cursor.fetchone()
        if row != (1,):
            raise DatabaseError("unexpected health result")
    except (DatabaseError, InterfaceError):
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ok"})
