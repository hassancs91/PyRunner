"""
URL patterns for authentication.
"""
from django.urls import path
from core.views.auth import (
    login_view,
    magic_link_sent_view,
    verify_view,
    logout_view,
)

app_name = "auth"

urlpatterns = [
    path("login/", login_view, name="login"),
    path("magic-link-sent/", magic_link_sent_view, name="magic_link_sent"),
    path("verify/<str:token>/", verify_view, name="verify"),
    path("logout/", logout_view, name="logout"),
]
