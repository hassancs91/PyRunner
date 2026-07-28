from django.urls import path

from . import views

app_name = "yt_comments"

urlpatterns = [
    path("", views.index, name="index"),
    path("save/", views.save, name="save"),
    path("run/", views.run, name="run"),
    path("stop/", views.stop, name="stop"),
    path("status/", views.status, name="status"),
    path("test-youtube/", views.test_youtube, name="test_youtube"),
    path("export/testimonials.<str:fmt>", views.export_testimonials, name="export_testimonials"),
    path("oauth/start/", views.oauth_start, name="oauth_start"),
    path("oauth/callback", views.oauth_callback, name="oauth_callback"),
    path("oauth/disconnect/", views.oauth_disconnect, name="oauth_disconnect"),
    path("brain/save/", views.brain_save, name="brain_save"),
    path("replies/<int:reply_id>/approve/", views.reply_approve, name="reply_approve"),
    path("replies/<int:reply_id>/reject/", views.reply_reject, name="reply_reject"),
]
