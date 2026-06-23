from django.urls import path

from . import views

app_name = "sdk_showcase"

urlpatterns = [
    path("", views.index, name="index"),
    path("setup/", views.setup, name="setup"),
    path("increment/", views.increment, name="increment"),
    path("run/", views.run_demo, name="run"),
    path("stop/", views.stop, name="stop"),
    path("schedule/", views.set_schedule, name="schedule"),
    path("reset/", views.reset_demo, name="reset"),
    path("status/", views.status, name="status"),
]
