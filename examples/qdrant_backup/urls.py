from django.urls import path

from . import views

app_name = "qdrant_backup"

urlpatterns = [
    path("", views.index, name="index"),
    path("save/", views.save, name="save"),
    path("run/", views.run_backup, name="run"),
    path("stop/", views.stop, name="stop"),
    path("status/", views.status, name="status"),
    path("download/", views.download, name="download"),
    path("test-qdrant/", views.test_qdrant, name="test_qdrant"),
    path("test-r2/", views.test_r2, name="test_r2"),
]
