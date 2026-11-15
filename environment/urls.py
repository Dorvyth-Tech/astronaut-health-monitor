from django.urls import path
from . import views

app_name = 'environment'

urlpatterns = [
    path('', views.dashboard_view, name='dashboard'),
    path('api/', views.environment_api_view, name='api'),
    path('space-weather/', views.space_weather_api_view, name='space_weather_api'),
]
