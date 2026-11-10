from django.urls import path

from . import views

urlpatterns = [
    path("profile/", views.profile_view),
    path("raw-cookie/", views.raw_cookie_profile_view),
    path("raw-cookie-private/", views.raw_cookie_private_view),
    path("read-only/", views.read_only_session_view),
    path("first-visit/", views.first_visit_view),
    path("private/", views.private_view),
    path("no-store/", views.no_store_view),
    path("vary-star/", views.vary_asterisk_view),
    path("conditional/", views.conditional_view),
    path("not-modified/", views.not_modified_view),
    path("delayed/", views.delayed_render_view),
    path("streaming/", views.streaming_view),
    path("public/", views.public_view),
    path("public-token/", views.public_token_view),
]
