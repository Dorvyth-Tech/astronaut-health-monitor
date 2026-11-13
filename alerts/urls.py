from django.urls import path
from alerts import views

app_name = 'alerts'

urlpatterns = [
    path('alerts/', views.alerts_list, name='list'),
    path('alerts-and-diagnostics/', views.alerts_list, name='list_alias'),
    path('alerts/<int:alert_id>/', views.alert_detail, name='detail'),
    path('alerts/<int:alert_id>/acknowledge/', views.acknowledge_alert, name='acknowledge'),
    path('alerts/acknowledge-all/', views.acknowledge_all_alerts, name='acknowledge_all'),
]
