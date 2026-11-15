from django.urls import path
from telemetry import views

app_name = 'telemetry'

urlpatterns = [
    path('check-in/', views.check_in_view, name='check_in'),
    path('history/', views.history_view, name='history'),
    path('telemetry-history/', views.history_view, name='history_alias'),
    path('readings/add/', views.add_reading, name='add_reading'),
    path('api/readings/', views.api_readings, name='api_readings'),
    path('upload/', views.upload_csv_view, name='upload_csv'),
    path('upload/template.csv', views.csv_template_view, name='csv_template'),
    path('upload/rejected.csv', views.csv_rejected_view, name='csv_rejected'),
]
