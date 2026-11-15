from django.contrib import admin
from django.urls import path, include
from environment.views import environment_api_view, space_weather_api_view

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('core.urls')),
    path('', include('telemetry.urls')),
    path('', include('alerts.urls')),
    path('environment/', include('environment.urls')),
    path('api/environment/', environment_api_view, name='api_environment'),
    path('api/space-weather/', space_weather_api_view, name='api_space_weather'),
    path('companion/chat/', include('companion.urls')),
]
