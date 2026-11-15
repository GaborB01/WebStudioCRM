from django.urls import path

from . import views

app_name = "cloudstorage"

urlpatterns = [
    # Stands in for the bucket's HTTP endpoint (presigned token required).
    path("o/<path:key>", views.serve_object, name="object"),
    # Staff entry point: redirects to a freshly signed URL for one version.
    path("v/<int:pk>/", views.version_download, name="version-download"),
]
