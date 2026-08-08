from django.urls import path

from .views import health_check, home

urlpatterns = [
    path("", home, name="home"),
    path("health/", health_check, name="health-check"),
]
