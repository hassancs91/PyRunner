from django.urls import path

from . import views

app_name = "brand_tracker"

urlpatterns = [
    path("", views.index, name="index"),
    path("save/", views.save, name="save"),
    path("run/", views.run, name="run"),
    path("stop/", views.stop, name="stop"),
    path("status/", views.status, name="status"),
    path("test-serper/", views.test_serper, name="test_serper"),
    path("share-report/", views.share_report, name="share_report"),
    path("revoke-report/", views.revoke_report, name="revoke_report"),
]
