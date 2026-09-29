from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path

urlpatterns = [path("admin/", admin.site.urls), path("api/", include("core.urls")),
               path("health/", lambda request: JsonResponse({"status": "ok"}))]
