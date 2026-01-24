"""
URL patterns for authentication.
"""
from django.urls import path
from core.views.auth import (
    login_view,
    magic_link_sent_view,
    verify_view,
    logout_view,
    accept_invite_view,
    change_password_view,
    forgot_password_view,
    reset_password_view,
)

app_name = "auth"

urlpatterns = [
    path("login/", login_view, name="login"),
    path("magic-link-sent/", magic_link_sent_view, name="magic_link_sent"),
    path("verify/<str:token>/", verify_view, name="verify"),
    path("logout/", logout_view, name="logout"),
    path("invite/<str:token>/", accept_invite_view, name="accept_invite"),
    # Password management
    path("change-password/", change_password_view, name="change_password"),
    path("forgot-password/", forgot_password_view, name="forgot_password"),
    path("reset-password/<str:token>/", reset_password_view, name="reset_password"),
]
