"""
URL configuration for webstudiocrm project.

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
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path('admin/', admin.site.urls),
    # Stands in for the storage provider's endpoint. Uploads are not served
    # from disk: every read needs a presigned URL from the storage backend.
    path('cloud/', include('cloudstorage.urls')),
    # Signature provider callbacks; authenticated by the provider's own
    # scheme, not by a session (see documents.views).
    path('webhooks/', include('documents.urls')),
]

if settings.DEBUG:
    # Legacy local uploads only (see MEDIA_ROOT in settings); new files live
    # in the bucket and are reached through /cloud/.
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
