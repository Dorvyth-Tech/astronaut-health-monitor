from django.urls import path
from core import views

app_name = 'core'

urlpatterns = [
    path('', views.dashboard_view, name='dashboard'),
    path('dashboard/', views.dashboard_view, name='dashboard_alias'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('profile/', views.profile_view, name='profile'),
    path('ground-support/', views.ground_support_view, name='ground_support'),
    path('companion/answers/', views.companion_answers_view, name='companion_answers'),
    path('companion/ask/', views.companion_ask_view, name='companion_ask'),
]
