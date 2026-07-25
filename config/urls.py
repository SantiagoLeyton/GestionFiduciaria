from django.contrib import admin
from django.urls import include, path


urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("core.urls")),
    path("accounts/", include("users.urls")),
    path("real-estate/", include("real_estate.urls")),
    path("fiduciary/", include("fiduciary.urls")),
]
