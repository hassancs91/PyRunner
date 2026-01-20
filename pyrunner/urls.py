"""
URL configuration for pyrunner project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.contrib import admin
from django.urls import path, include
from django.shortcuts import redirect

from core.views.webhooks import webhook_trigger_view

urlpatterns = [
    path("admin/", admin.site.urls),
    path("auth/", include("core.urls.auth")),
    path("cpanel/", include("core.urls.cpanel")),
    # Public webhook endpoint (no auth required)
    path("webhook/<str:token>/", webhook_trigger_view, name="webhook_trigger"),
    path("", lambda request: redirect("auth:login")),
]
