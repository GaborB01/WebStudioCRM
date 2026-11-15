from django.urls import path

from . import views

app_name = "documents"

urlpatterns = [
    # Before the catch-all below, which would otherwise match "health"
    # as a provider name.
    path("signing/health/", views.health, name="signing-health"),
    path("signing/<str:provider_name>/", views.signing_webhook, name="signing-webhook"),
]
